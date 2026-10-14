"""Pydantic request/response schemas."""

from datetime import datetime
from pydantic import BaseModel, Field


# ================== Auth ==================

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    password: str = Field(..., min_length=6, max_length=100)


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str
    username: str


# ================== Chat ==================

class ChatRequest(BaseModel):
    query: str
    conversation_id: int | None = None
    # ★ 「知识库搜索」：用户手动要求【本条】强制走 RAG，覆盖 classify 的自动判断。
    #   默认 False ⇒ 行为与改造前完全一致（仍由 classify 逐题判断）。
    #   只影响本条请求，不改变任何全局状态（§39.27.3 用户拍板的形态）。
    force_rag: bool = False
    # ★ 「联网搜索」（§39.38）：本条去公开网页搜一次，结果作为【另一类资料】交给回答模型
    #   （明确标注"不是学校文件"、不许当作校规依据、冲突时以学校文件为准）。
    #   与 force_rag **相互独立**：只查库 / 只联网 / 两个都要，三种组合都支持。
    #   默认 False ⇒ 不开时 prompt 与改造前**逐字相同**（不新增任何 token）。
    web_search: bool = False


# ================== Conversation ==================

class ConversationCreate(BaseModel):
    title: str = "新对话"


class ConversationOut(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    """单条消息。

    sources 只在 assistant 消息上有值 —— 引用溯源用它渲染"来源"面板，
    也是"回答到底依据了哪几段"的唯一证据。历史接口必须一起回，
    否则一换会话/刷新页面引用就全丢了（只剩正文里悬空的 [1] 编号）。
    """
    id: int
    role: str
    content: str
    created_at: datetime
    sources: list[dict] = []

    model_config = {"from_attributes": True}


class ConversationDetail(BaseModel):
    id: int
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[MessageOut]

    model_config = {"from_attributes": True}


# ================== Knowledge Documents ==================

class DocumentOut(BaseModel):
    """用户已入库的文档（`GET /documents`）。

    `chunk_count` 是【可检索性】的直接指标，所以必须回给前端：
    入库"成功"但 chunk_count=0，意味着这份文档在知识库里等于不存在
    （用户会以为传上去了、其实问什么都不会命中）。status 同理 ——
    json_to_db 失败时会把文档标成 "failed"，前端要能看到。
    """

    id: int
    filename: str
    title: str | None = None
    status: str
    chunk_count: int = 0
    created_at: datetime


class DocumentDeleteOut(BaseModel):
    """删除文档的结果（`DELETE /documents/{id}`）。

    刻意把条数回给前端：删除是破坏性操作，用户必须能确认"到底删掉了什么"，
    而不是只收到一个 200。`vectors_left > 0` 表示 Chroma 里仍有残留向量
    （幽灵数据）—— 它虽已被 SQL 那道过滤挡住、召回不到，但必须如实暴露。
    """

    deleted: bool
    document_id: int
    filename: str | None = None
    chunks_deleted: int = 0
    vectors_deleted: int = 0
    vectors_left: int = 0
    bm25_warning: str | None = None
