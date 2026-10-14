from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text
from datetime import datetime, timezone

from langchain_project import route, make_conversation_title
from conversation_context import ConversationContext
from database import (
    init_db, get_db, Session, User, Conversation, Message, MessageSource,
    KnowledgeDocument, KnowledgeChunk,
)
from auth import hash_password, verify_password, create_access_token, get_current_user
# 降级计数出口用（§39.27.6-4）：llm_roles 是底层模块，不反向依赖本文件 ⇒ 无循环导入
from llm_roles import degraded_counts, pool_status
from schemas import (
    RegisterRequest, LoginRequest, TokenResponse,
    ChatRequest, ConversationCreate,
    ConversationOut, ConversationDetail, MessageOut, DocumentOut,
    DocumentDeleteOut,
)

# ---- 用户隔离改造新增的导入 ----
from agent.agent import create_knowledge_agent
from config import UPLOAD_ROOT, user_upload_root
from skills.knowledge_ingestion import ingest_pdf_to_knowledge_base
from skills.knowledge_ingestion.json_to_db import delete_document

# 上传文件按用户分目录存放：不同用户上传同名 PDF 不会互相覆盖。
# 注意：这个目录【不是】知识库本身，只是入库的输入暂存区。
# 它同时是 agent 入库工具的允许根目录（见下面的 /agent 路由）。

app = FastAPI()

# ---- CORS ----
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---- Startup ----
@app.on_event("startup")
def on_startup():
    init_db()


# ============================================================================
#  Auth Endpoints
# ============================================================================

@app.post("/auth/register", response_model=TokenResponse)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="用户名已存在")

    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id, user.username)
    return TokenResponse(token=token, username=user.username)


@app.post("/auth/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.execute(
        select(User).where(User.username == body.username)
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    token = create_access_token(user.id, user.username)
    return TokenResponse(token=token, username=user.username)


# ============================================================================
#  Conversation Endpoints
# ============================================================================

@app.post("/conversations", response_model=ConversationOut)
def create_conversation(
    body: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = Conversation(user_id=current_user.id, title=body.title)
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv


def _sources_by_message(db: Session, message_ids: list[int]) -> dict:
    """批量取消息的检索来源，避免 N+1 查询。

    payload 存的是 JSON 字符串，这里解回 dict；坏行直接跳过 ——
    不能因为一条脏数据让整个历史接口 500（历史只是展示，不该被数据瑕疵拖垮）。
    """
    if not message_ids:
        return {}
    rows = db.execute(
        select(MessageSource)
        .where(MessageSource.message_id.in_(message_ids))
        .order_by(MessageSource.message_id, MessageSource.ordinal)
    ).scalars().all()
    out: dict = {}
    for r in rows:
        try:
            item = json.loads(r.payload)
        except (ValueError, TypeError):
            continue
        if isinstance(item, dict):
            out.setdefault(r.message_id, []).append(item)
    return out


@app.get("/conversations", response_model=list[ConversationOut])
def list_conversations(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    convs = db.execute(
        select(Conversation)
        .where(Conversation.user_id == current_user.id)
        .order_by(Conversation.updated_at.desc())
    ).scalars().all()
    return convs


@app.get("/conversations/{conv_id}", response_model=ConversationDetail)
def get_conversation(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")
    # ★ 手工组装：MessageOut.sources 要的是 list[dict]，而 ORM 上 Message.sources
    #   是 MessageSource 行（payload 还是 JSON 字符串），直接用 from_attributes
    #   序列化会校验失败 —— 所以在这里解一次 JSON。
    msgs = list(conv.messages)
    grouped = _sources_by_message(db, [m.id for m in msgs])
    return ConversationDetail(
        id=conv.id, title=conv.title,
        created_at=conv.created_at, updated_at=conv.updated_at,
        messages=[MessageOut(id=m.id, role=m.role, content=m.content,
                             created_at=m.created_at,
                             sources=grouped.get(m.id, [])) for m in msgs],
    )


@app.put("/conversations/{conv_id}", response_model=ConversationOut)
def update_conversation(
    conv_id: int,
    body: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")
    conv.title = body.title
    conv.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return conv


@app.delete("/conversations/{conv_id}")
def delete_conversation(
    conv_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="对话不存在")
    db.delete(conv)
    db.commit()
    return {"ok": True}


# ============================================================================
#  Chat Endpoint (SSE, protected)
# ============================================================================

def _sse_event(data: str, *, event: str | None = None) -> str:
    """把一段文本编码成【符合 SSE 规范】的 frame。

    ★ 规范要求 data 字段里不能出现裸换行：多行内容必须每行各自带 "data:" 前缀，
      接收端再按 "\n" 拼回。原来的 f"data:{text}\n\n" 在 text 含换行时会产生
      非法帧 —— 帧被提前结束、后续行成了"未知字段"，任何符合规范的客户端都会丢掉它们。
      前端此前只能靠"删掉所有换行"糊过去，代价是段落/列表/表格结构在页面上全部丢失
      （实测：库里 8/25 条助手消息含空行，UI 上一律被压成一行；
        verify_chat_e2e_fix.py 也记着"旧的按行过滤写法会吃掉 34 个字符"）。

    event 参数留给后续的带类型事件（如引用来源 event: sources），现在默认不传。
    """
    out: list[str] = []
    if event:
        out.append(f"event:{event}\n")
    for line in data.split("\n"):
        out.append(f"data:{line}\n")
    out.append("\n")   # 空行 = 一帧结束
    return "".join(out)


async def rag_response(query: str, user_id: int, context: ConversationContext | None = None,
                       trace: dict | None = None, force_rag: bool = False,
                       web_search: bool = False):
    """RAG 回答的【原始文本】分片 —— 不负责 SSE 编码。

    ★ 刻意把"取文本"和"编 SSE 帧"分成两层：以前在同一个生成器里直接
      f"data:{text}\n\n"，/chat 落库时只能靠 removeprefix/removesuffix 反向剥壳，
      而 text 含换行时剥出来的是带 data: 前缀的脏文本。分开后落库拿到的一定是干净文本。

    user_id 必须来自鉴权后的 current_user —— 检索只能命中该用户自己的文档（∪ 公共库）。
    context：已校验归属的对话上下文（多轮追问的判定与改写都依赖它）。
      不传 ⇒ 与改造前行为一致（单轮）。
    trace：可选 dict，透传给 route()；reranked 会把命中的来源写进去。
    force_rag：用户点了前端「知识库搜索」按钮 ⇒ 本条强制走 RAG（透传给 route，覆盖 classify）。
    web_search：用户点了前端「联网搜索」按钮 ⇒ 本条去公开网页搜一次（透传给 route）。
      ★ 与 force_rag 相互独立；两条链都接（RAG 与直答），避免"走了闲聊就没搜"的静默失效。
      ★ 联网结果不会触发任何 LLM 调用（只是 HTTP + 解析）⇒ 它不增加调用次数，
        只在 prompt 里多出有硬截断的网络块。

    产出两类事件（**不做 SSE 编码**，编码统一在 /chat）：
      ("text", 文本分片) / ("sources", 检索来源载荷)
    ★ 来源事件必须在第一个文本分片【之前】发出：检索发生在 chain.astream 内部，
      第一个分片产出时 trace 必然已经写好；若回答一个分片都没有，则收尾补发。
    """
    print("开始处理:", query, "user_id:", user_id,
          "上下文轮数:", len(context) if context is not None else 0)
    try:
        chain = route(query, user_id, context, trace, force_rag=force_rag, web=web_search)
        sources_sent = False
        # ★ RAG 链期望 dict 输入（好把 context 闭包进去）；direct_chain 两种都接受。
        async for chunk in chain.astream({"question": query}):
            if not sources_sent:
                sources_sent = True
                if trace and trace.get("attempted"):
                    yield ("sources", trace)
            text = chunk.content if hasattr(chunk, "content") else chunk
            if text:
                yield ("text", text)
        if not sources_sent and trace and trace.get("attempted"):
            yield ("sources", trace)          # 空回答兜底
    except Exception as e:
        yield ("text", f"发生错误:{str(e)}")


# ============================================================================
#  会话标题（方案 B）：首轮问答之后，用 1 次轻量调用补一个真正的标题
# ============================================================================
#  三条硬约束：
#    (1) 不 await —— 放后台任务，回答的【感知延迟】不受影响；
#    (2) 失败静默 —— 退回 A 的截断标题，绝不影响已经发出去的回答；
#    (3) 写入前二次确认 —— 这约 1 秒里用户可能刚手动改过名，覆盖它就是 §39.20.3 那个坑。
#  ★ 默认【关】（用户决定）：实测下来「用户的问题本身就是很好的标题」，
#  而 B 要多花 1 次调用、多两个失败面（已实际亏过 1 次）。代码与测试都保留，
#  想开就设 TITLE_LLM=1 重启后端；关闭时【绝不发任何调用】。
TITLE_LLM_ENABLED = os.getenv("TITLE_LLM", "0").strip().lower() not in ("", "0", "false", "off", "no")

_BG_TASKS: set = set()          # ★ 必须持引用，否则后台任务可能在运行中被 GC 掉


def apply_generated_title(conv_id: int, base_title: str, new_title: str | None) -> bool:
    """把生成的标题写入库，返回是否真的写了。

    ★ 只有【标题仍是 base_title】时才写 —— 否则说明用户在这期间改过名，必须让位。
    """
    if not new_title:
        return False
    db2 = next(get_db())        # 请求自己的会话此时已关闭，后台任务必须另开一个
    try:
        c = db2.get(Conversation, conv_id)
        if c is None or c.title != base_title:
            return False
        c.title = new_title
        db2.commit()
        return True
    finally:
        db2.close()


async def _finish_title(conv_id: int, base_title: str, query: str, answer: str) -> None:
    """后台补标题。任何异常都吞掉：起名是锦上添花，不允许它把任何东西搞坏。"""
    try:
        t = await asyncio.to_thread(make_conversation_title, query, answer)
        apply_generated_title(conv_id, base_title, t)
    except Exception:
        pass


def _spawn_title_task(conv_id: int, base_title: str, query: str, answer: str) -> None:
    task = asyncio.create_task(_finish_title(conv_id, base_title, query, answer))
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


# ============================================================================
#  会话标题：首轮提问时自动起名（★ 零 API，且绝不覆盖用户手动改的名字）
# ============================================================================
#  修的 bug：前端点「新对话」会先 POST 一个 title='新对话' 的空会话，
#  于是之后提问走的是「已有会话」分支 —— 下面 else 里那段自动起名再也不触发，
#  => 侧栏当场看着像新问题，但库里始终是「新对话」，一刷新就退回去。
CONV_TITLE_MAX = 30
DEFAULT_CONV_TITLE = "新对话"   # ★ 必须与 database.py 里 Conversation.title 的 default 一致（有测试锁住）


def auto_title_from_query(query: str) -> str:
    """把首个问题截成标题。规则与前端 ChatView.vue 保持一致（q 前 30 字 + ...）。"""
    q = (query or "").strip()
    if len(q) <= CONV_TITLE_MAX:
        return q
    return q[:CONV_TITLE_MAX] + "..."


def should_auto_title(title: str | None, prior_user_msgs: int) -> bool:
    """该不该自动起名。两个条件缺一不可：

      ① 这是该会话的【第一条】用户消息 —— 否则标题会随对话不断变；
      ② 标题仍是【默认值】 —— 否则会把用户手动改的名字覆盖掉（最容易埋的 bug）。
    """
    if prior_user_msgs != 0:
        return False
    return (title or "").strip() in ("", DEFAULT_CONV_TITLE)


@app.post("/chat")
async def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = request.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="问题不能为空")

    # ---- Resolve conversation ----
    #  auto_titled_now / base_title 供方案 B 使用：本轮是否刚起过名、当时的标题是什么。
    auto_titled_now = False
    base_title = ""
    conv_id = request.conversation_id
    if conv_id:
        conv = db.get(Conversation, conv_id)
        if conv is None or conv.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="对话不存在")
    else:
        # Auto-create a new conversation
        title = query[:30] + ("..." if len(query) > 30 else "")
        conv = Conversation(user_id=current_user.id, title=title)
        db.add(conv)
        db.commit()
        db.refresh(conv)
        conv_id = conv.id
        auto_titled_now = True      # 新会话的首轮：B 也适用
        base_title = title

    # ★ 先数一遍「本轮之前有几条用户消息」——必须在插入之前数，
    #   否则恒 >= 1，新建会话的首轮也会被当成非首轮。
    prior_user_msgs = db.scalar(
        select(func.count()).select_from(Message).where(
            Message.conversation_id == conv_id, Message.role == "user"
        )
    ) or 0

    # Save user message
    user_msg = Message(conversation_id=conv_id, role="user", content=query)
    db.add(user_msg)
    db.commit()

    # ---- 首轮提问时补上会话标题（★ 零 API；绝不覆盖用户手动改的名字）----
    if should_auto_title(conv.title, prior_user_msgs):
        conv.title = auto_title_from_query(query)
        db.commit()
        auto_titled_now = True      # 本轮刚按问题起了名 => 交给 B 生成真正的标题
        base_title = conv.title

    # ---- 读出本轮之前的上下文（★ 必须在这一句之后读）----
    # 上面的 user_msg 刚刚写库，所以 context 的【最后一轮就是本轮提问本身】、
    # 其助手回答此时还不存在（assistant=""）。这与 §35 实验装置的输入形态一致
    # （实测 4 臂 160 次调用即按此形态得到"多轮追问误判 4/7 → 0/7"）。
    # ★ 归属校验在 ConversationContext.load 内部用 JOIN 再做一次 ——
    #   这里上面已查过一道（L192），两道是刻意的纵深防御，不是重复。
    context = ConversationContext.load(conv_id, current_user.id)

    # Collect assistant response chunks for persisting
    full_response: list[str] = []

    # 检索来源的可观测记录 —— ★ 按请求创建，绝不共享全局变量（并发会串档）。
    trace: dict = {}

    async def event_stream():
        nonlocal full_response
        # ★ 整条流有没有发过"来源"帧 —— 没发过就说明这一问压根没走检索
        #   （classify 判为无需查库，走了 direct 直答）。那样前端拿不到任何信号，
        #   用户就无法分辨"这条答案是查了资料，还是模型自己答的"。
        #   所以流结束时补发一个 attempted=False 的帧（见 SESSION_HANDOFF §39.27）。
        sent_sources = False
        async for kind, payload in rag_response(query, current_user.id, context, trace,
                                                 force_rag=request.force_rag,
                                                 web_search=request.web_search):
            if kind == "sources":
                sent_sources = True
                # 带类型的帧，前端据此渲染来源面板。
                # ★ 用 ensure_ascii=False 保住中文；即使载荷里含换行，
                #   _sse_event 也会按规范拆成多行 data，前端拼回后仍是合法 JSON。
                yield _sse_event(json.dumps(payload, ensure_ascii=False), event="sources")
                continue
            # ★ 落库只收文本：rag_response 不再做 SSE 包装，无需反向剥壳。
            full_response.append(payload)
            yield _sse_event(payload)   # ← 全项目唯一的 SSE 编码点

        # B: no retrieval frame was sent => this question went the direct path.
        # Emit a status frame so the frontend can say "no retrieval for this one"
        # instead of showing nothing (users then think retrieval is broken).
        # ★ 但【出错】时绝不能补发这一帧：错误流同样不会发来源帧，若照样补发，
        #   界面上就会在「发生错误:xxx」下面多出一句"判断为无需查库" ——
        #   那等于把"崩了"说成"我判断过、不需要查库"，正是 §39.25 要消灭的假信号。
        #   判据沿用本文件落库处的同一个约定（错误文本以「发生错误:」开头）。
        is_error = "".join(full_response).startswith("发生错误:")
        if not sent_sources and not is_error:
            yield _sse_event(
                json.dumps(_status_payload(trace), ensure_ascii=False),
                event="sources",
            )

        # Persist assistant message after stream completes
        assistant_text = "".join(full_response)
        # Don't persist error messages
        # ★ 落库前先确认会话还在：用户可能在流进行中把会话删掉，此时继续落库会
        #   插出"没有会话的消息"，而它的 message_sources 会在 id 被复用时出现在
        #   别人的来源面板里。这里刻意用 count() 而不是 db.get() —— db.get 会命中
        #   身份映射缓存，拿到的可能是已删除的旧对象，判断会形同虚设。
        #   见 SESSION_HANDOFF §39.26。
        if db.query(Conversation).filter(Conversation.id == conv_id).count() and not assistant_text.startswith("发生错误:"):
            ai_msg = Message(
                conversation_id=conv_id,
                role="assistant",
                content=assistant_text,
            )
            db.add(ai_msg)
            # ★ flush 先拿到 ai_msg.id —— 来源行要挂在它上面，没 id 插不进去。
            db.flush()
            # ★★ 写新来源之前，先清掉这个 message_id 上的历史来源行。
            #   messages.id 没有 AUTOINCREMENT（删掉末尾行后 id 会被复用），
            #   而 SQLite 默认不开外键级联 => 上一轮生命周期残留的来源行会被
            #   新消息"继承"，界面上就出现不属于这条回答的引用依据。
            #   实测铁证：新消息拿到 id=83 时，孤儿来源行数从 30 掉到 28（被它吞了）。
            #   显式清一次 = 无论历史多脏，落库后的来源只属于本条回答。
            #   见 SESSION_HANDOFF §39.26。
            db.execute(delete(MessageSource).where(MessageSource.message_id == ai_msg.id))
            for item in (trace.get("sources") or []):
                db.add(MessageSource(
                    message_id=ai_msg.id,
                    ordinal=int(item.get("n") or 0),
                    payload=json.dumps(item, ensure_ascii=False),
                ))
            # Update conversation timestamp
            conv_now = db.get(Conversation, conv_id)
            if conv_now:
                conv_now.updated_at = datetime.now(timezone.utc)
            db.commit()

            # ---- 方案 B：首轮问答之后补标题（不 await、失败静默）----
            if auto_titled_now and TITLE_LLM_ENABLED:
                _spawn_title_task(conv_id, base_title, query, assistant_text)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================================
#  Knowledge Ingestion Endpoints (protected) —— 用户隔离改造新增
# ============================================================================

class AgentRequest(BaseModel):
    """放在这里而不是 schemas.py，是为了少改一个文件；
    若日后 schemas.py 成了统一入口，再挪过去即可。"""
    query: str


@app.post("/documents")
def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    current_user: User = Depends(get_current_user),
):
    """上传 PDF 并入库到【当前用户】的知识库。

    · 用 def 而不是 async def：入库是同步阻塞的重活，FastAPI 会把它丢到线程池，
      不会卡住事件循环（否则整个服务在入库期间无响应）。
    · user_id 取自 current_user，不接受客户端传入 —— 否则谁都能往别人库里塞文档。
    """
    upload_dir = user_upload_root(current_user.id)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 只取 basename：防止 filename 里带 ../ 做路径穿越写到目录外
    safe_name = Path(file.filename or "upload.pdf").name
    if Path(safe_name).suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="只支持 PDF 文件")
    destination = upload_dir / safe_name

    with destination.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    result = ingest_pdf_to_knowledge_base(
        pdf_path=str(destination), title=title, user_id=current_user.id
    )
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error") or "入库失败")
    return result


@app.get("/documents", response_model=list[DocumentOut])
def list_documents(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出【当前用户】已入库的文档（新入库的在前）。

    为什么需要这个端点：`POST /documents` 只回一个 document_id ——
    用户传完之后没有任何办法确认东西到底进没进库、能不能被检索到。
    前端的上传面板必须能看见已有文档，这个功能才算可用。

    隔离口径与 `/conversations` 完全一致：只按 `current_user.id` 过滤，
    user_id 【不】由客户端传入（否则能看到别人的知识库）。

    `chunk_count` 用一次 outer join + group by 聚合，而不是逐个
    `len(doc.chunks)`（那是 N+1 次查询）。它同时能暴露两种"假成功"：
      · status='failed'      —— json_to_db 中途失败
      · chunk_count == 0     —— 入库了但检索不到任何内容
    这两种情况必须让用户直接看见，而不是等下次提问答不出来才怀疑。
    """
    rows = db.execute(
        select(
            KnowledgeDocument,
            func.count(KnowledgeChunk.id).label("chunk_count"),
        )
        .outerjoin(KnowledgeChunk,
                   KnowledgeChunk.document_id == KnowledgeDocument.id)
        .where(KnowledgeDocument.user_id == current_user.id)
        .group_by(KnowledgeDocument.id)
        .order_by(KnowledgeDocument.id.desc())
    ).all()

    return [
        DocumentOut(
            id=doc.id,
            filename=doc.filename,
            title=doc.title,
            status=doc.status,
            chunk_count=chunk_count,
            created_at=doc.created_at,
        )
        for doc, chunk_count in rows
    ]


@app.delete("/documents/{document_id}", response_model=DocumentDeleteOut)
def delete_user_document(
    document_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """删除【自己名下】的一份文档：Chroma 向量 + SQL 行 + BM25 缓存三处一起清。

    · 归属校验：只允许删 user_id == current_user.id 的文档；别人的（含公共库文档）
      一律回 404 —— 刻意用 404 而非 403，避免暴露"这个 id 存在、只是不属于你"。
    · 为什么必须有它：没有这个端点时，误传的无关文档会【永久】留在库里拉低检索
      质量，用户没有任何自救手段（此前只能由维护者手写脚本改 SQLite + Chroma + BM25）。
    · 上传的 PDF 原件【保留】在用户自己的上传目录里：这里删的是"可检索状态"，
      原件保留让误删可恢复、也便于审计。需要连原件一起清时另加参数。
    · 同步 def → 走线程池（与上传一致，避免阻塞事件循环）。
    """
    doc = db.get(KnowledgeDocument, document_id)
    if doc is None or doc.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="文档不存在")

    # 三处一致的清理由持久层的 delete_document 统一负责，这里只管归属。
    return DocumentDeleteOut(**delete_document(document_id, db=db))


@app.post("/agent")
def run_ingestion_agent(
    body: AgentRequest,
    current_user: User = Depends(get_current_user),
):
    """让 LLM 自己决定是否调用知识库入库 skill（同样按用户隔离）。

    · agent 是【每次请求按 user_id 构造】的：入库工具把 user_id 闭包在里面，
      所以 user_id 不会出现在工具 Schema 里，LLM 无法指定别人的库。
    · 同时把 allowed_root 钉死在该用户自己的上传目录：否则 pdf_path 由 LLM 提供，
      用户B 能让 agent 去读用户A 的上传文件并入库到 B 的库，再用 /chat 读出来。
    · 同样是同步 def → 走线程池。
    """
    query = body.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="问题不能为空")

    agent = create_knowledge_agent(
        current_user.id, allowed_root=user_upload_root(current_user.id)
    )
    result = agent.invoke({"messages": [{"role": "user", "content": query}]})

    messages = []
    for msg in result["messages"]:
        tool_calls = getattr(msg, "tool_calls", None) or []
        messages.append({
            "type": type(msg).__name__,
            "content": str(getattr(msg, "content", "") or ""),
            "tool_calls": [
                {"name": tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None),
                 "args": tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)}
                for tc in tool_calls
            ],
        })
    return {"messages": messages}


# ============ 可观测：降级计数出口（§39.27.6-4）============
# 为什么需要它：降级（换 key / 池耗尽 / 永久失败）以前**只能人肉读服务器日志** ——
#   想确认"这一轮有没有偷偷降级"就得盯控制台。有了这个出口，脚本与 e2e 才能**断言**
#   "本次 0 次非预期降级"，而不是靠眼睛看（§39.3 / §35.14.4 都是被静默降级坑过的例子）。
#
# ★ 诚实边界（必须写进接口，不能靠文档兜）：计数存在**进程内存**里（`llm_roles._DEGRADED`），
#   **重启即清零**。所以响应里带 `scope="process"` 与 `pid`，不给出"历史累计"的假象；
#   看到数字比日志少，先想想是不是重启过。
# ★ 鉴权：与其它接口一致（要登录 token）。它不含 key 原文（`pool_status()` 已打码），
#   但仍会暴露角色名与池结构，不该匿名可读。
# ★ reason 字典：实测踩过一次 —— `key_used` 挂在 "[降级]" 横幅下读起来像故障，
#   其实只是"本次用了哪把 key"的正常记录（第十二轮多轮 e2e 时我差点把它当异常报上去）。
#   把这件事写进接口，省得下一个人再去翻源码。
_DEGRADATION_LEGEND = {
    "key_used": "本次调用用的是哪把 key —— 正常记录，不是故障",
    "key_rotated": "额度耗尽，已自动换到池里的下一把 key",
    "all_keys_exhausted": "池内 key 全部耗尽：需要新 key 或等额度恢复",
    "permanent_fail": "重试后仍失败（模型 / 网络 / 提示词问题）",
    # ★ 联网搜索（§39.38）：与 LLM 降级走**同一个出口**，所以这里也要有人话解释。
    #   刻意复用这套出口而不是另造一个：另造一个就等于"用户看不到的那半边"。
    "no_results": "联网搜索：搜索引擎没有返回结果（如实说「没搜到」）",
    "irrelevant": "联网搜索：搜到的内容与问题不相关，已丢弃（防「结构正常但全无关」的诱饵结果）",
    "parse_empty": "联网搜索：结果页结构变了，解析不出结果（不是「没内容」，是解析失效）",
    "timeout": "联网搜索：搜索引擎响应超时",
    "http_error": "联网搜索：请求失败（网络 / 反爬 / 接口变化）",
    "rate_limited": "联网搜索：触发本机限速（每分钟外呼上限），本次不再外呼",
    "empty_query": "联网搜索：查询为空",
}


class DegradationOut(BaseModel):
    counts: dict[str, int]          # "<role>:<reason>" -> 次数
    by_reason: dict[str, int]       # 只按 reason 汇总（跨角色）
    total: int
    unknown_reasons: list[str]      # 字典里没有的原因：如实冒出来，不静默吞掉（防"字典悄悄过期"）
    legend: dict[str, str]
    scope: str                      # 恒为 "process"
    pid: int
    keys: str
    note: str


@app.get("/health/degradations", response_model=DegradationOut)
def health_degradations(current_user: User = Depends(get_current_user)):
    counts = degraded_counts()
    by_reason: dict[str, int] = {}
    for full, n in counts.items():
        reason = full.split(":", 1)[1] if ":" in full else full
        by_reason[reason] = by_reason.get(reason, 0) + n
    return DegradationOut(
        counts=counts,
        by_reason=by_reason,
        total=sum(counts.values()),
        unknown_reasons=sorted(r for r in by_reason if r not in _DEGRADATION_LEGEND),
        legend=dict(_DEGRADATION_LEGEND),
        scope="process",
        pid=os.getpid(),
        keys=pool_status(),
        note="进程内计数，重启即清零；key 已打码",
    )


# uvicorn fastapi_lianxi:app --reload  (启动后端)
# npm run dev                           (启动前端, 先 cd rag_frontend)


def _status_payload(trace) -> dict:
    """直答分支的状态帧载荷（§39.36）。

    ★ 不变量照旧：`attempted=False`（§39.29 的契约）。
    附加的 direct_history_* 只是把"这一问给直答链喂了多少历史"暴露到线上，
    让 e2e/前端能断言"闲聊记忆到底有没有生效"（此前只能靠机制装置）。
    ★ 联网搜索（§39.38）：直答分支**也要**带上 `web`。为什么必须：
      用户开了「联网搜索」，若这条被判为闲聊、而帧里又不带 web，
      前端就"什么都不知道" ⇒ 又一次"点了没反应"（正是这一整轮要消灭的东西）。
      `attempted` 仍然恒为 False：它表达的是"有没有查**知识库**"，与联网是两件事。
    """
    out = {"attempted": False, "reason": "no_retrieval"}
    if isinstance(trace, dict) and trace.get("direct_history_chars"):
        out["direct_history_turns"] = trace.get("direct_history_turns")
        out["direct_history_chars"] = trace.get("direct_history_chars")
    if isinstance(trace, dict) and isinstance(trace.get("web"), dict):
        out["web"] = trace["web"]
    return out
