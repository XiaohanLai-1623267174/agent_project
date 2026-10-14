from langchain_community.embeddings import DashScopeEmbeddings
from langchain_openai import ChatOpenAI
from langchain_chroma import Chroma
from langchain_core.output_parsers import StrOutputParser
import math
from collections import Counter
import os
from datetime import datetime
from langchain_core.runnables import RunnablePassthrough, RunnableLambda, RunnableBranch
from langchain_core.prompts import ChatPromptTemplate
import jieba
from langchain_community.retrievers import BM25Retriever
from langchain_community.document_compressors import DashScopeRerank
from dashscope import TextReRank
import requests
import json
import sys
from pathlib import Path

from langchain_core.documents import Document
from sqlalchemy import or_

from database import KnowledgeDocument, Session, engine
# ★ 本模块在 import 期就要用 QW_API_KEY 构造 DashScopeEmbeddings，
#   若完全依赖调用方先加载 .env，则任何"直接 import 本模块"的路径都会
#   在 L29 抛出 pydantic ValidationError（例如新的测试装置）。
#   env_bootstrap 用 override=False（变量级），所以：
#     · 生产路径（fastapi_lianxi.py 先 load_dotenv）行为【不变】—— 已有值不被覆盖；
#     · 独立 import 时能自给自足（从服务端代码目录的 .env 取）。
#   ⇒ 保留"启动配置注入"的意图，同时不再脆弱。幂等，重复调用无副作用。
import env_bootstrap                                     # noqa: E402
env_bootstrap.load_env()

from llm_roles import (make_model, with_retry, invoke_with_retry,
                       loads_json_list, note_degradation, degraded_counts)
# 对话上下文：把"给模型看的上文"收敛到唯一一处（含 user_id 归属校验）
from conversation_context import ConversationContext
# 联网搜索（§39.38）：无 key 默认可用 —— provider 抽象 + 两道闸 + 降级计价 + 成本闸。
# ★ 它自己**不做任何 LLM 调用**（只是 HTTP + 正则解析），所以"联网搜索"不增加 token 成本，
#   只在 prompt 里多出有硬截断的网络块（MAX_BLOCK_CHARS）。
import websearch                                          # noqa: E402

# 需要 LangSmith 追踪时，自行 export LANGSMITH_TRACING / LANGSMITH_API_KEY /
# LANGSMITH_PROJECT。★ 具体 key 一律不要写进代码 —— 写进来就会随版本库泄漏。

embeddings = DashScopeEmbeddings(
    model="qwen3.7-text-embedding",
    dashscope_api_key=os.getenv("QW_API_KEY"),
)

# 🔴 Chroma 的路径与集合名统一从 config.py 取（唯一来源）。
#    原先这里写的是相对路径 "./.chroma_db"，而 json_to_db.py 写的是绝对路径；
#    chromadb 以【路径字符串】为键复用/新建进程内 System ⇒ 同一目录被当成两套索引，
#    实测后果（repro_mixed_clients_full.py，全程无 API）：
#      经 json_to_db 的客户端入库 1 块后，本模块的 vectorstore 一旦让 filter 覆盖到
#      新写入的 document_id，max_marginal_relevance_search 就抛
#      chromadb.errors.InternalError: Error executing plan: Internal error: Error finding id；
#      把两边路径统一后，同一查询立即恢复正常。
#    ⚠️ 该 bug 只在"入库与检索在同一进程"时发作（例如 POST /agent、POST /documents）；
#      用 PyCharm 单独跑入库脚本（另一个进程）会掩盖它。
from config import CHROMA_DIRECTORY, COLLECTION_NAME

vectorstore = Chroma(
    persist_directory=str(CHROMA_DIRECTORY),
    embedding_function=embeddings,
    collection_name=COLLECTION_NAME
)

# ============ 用户隔离（本轮改造核心） ============
# 原实现的两个隐患（沙箱实测见 filter_validation.md / filter_impl_validation.md）：
#   1) vector_retriever 只是"默认不带 filter 的检索器"，它本身不构成任何过滤；
#   2) bm25_retriever 在 import 期用【全库】建好，天然没有用户概念 ——
#      只给向量加 filter 时，所有挤进 top5 的泄漏 chunk 全部由 BM25 那一路供给
#      （"假过滤"：候选池泄漏 211→157 看似改善，top5 泄漏仍 24 条、命中仍 3/6，与完全不过滤一模一样）。
# 因此：向量显式传 filter + BM25 按用户懒建并缓存 + 重排前护栏，三道一起上。

VECTOR_K = 10
VECTOR_FETCH_K = 15
BM25_K = 10

# 0 = 关闭（默认）。§7.6 实测：必需 chunk 分数区间 [0.7638, 1.0] 与不相关 chunk 完全重叠，
# 不存在可分离的阈值 → 建议不设。若一定要开，用 0.4 并保留熔断，绝不能 ≥ 0.76。
MIN_RERANK_SCORE = 0.0

# ============ 重排后端：默认本地 ============
# 背景（2026-10-11 实测）：云端 qwen3.7-text-rerank 的免费额度已耗尽 ——
#   直连返回 403 / code=AllocationQuota.FreeTierOnly / "Free quota exhausted"，
#   于是云端 output=None，旧代码每次都静默退化成"未重排"（并集插入序）。
#   而本地 rerank_server.py（bge-reranker-v2-m3，127.0.0.1:8090）当初就是
#   为替换这一处而写的：/rerank 返回 dashscope 风格的
#   results=[{index,relevance_score}]，且做了 sigmoid 归一化到 [0,1] ——
#   与 MIN_RERANK_SCORE 的语义空间一致，所以能直接换。
# ⇒ 默认 local；cloud 保留为可选备用；off 用于"不重排"的对照实验。
#   切换只改环境变量：RERANK_BACKEND=local|cloud|off
RERANK_BACKEND = os.getenv("RERANK_BACKEND", "local").strip().lower()
RERANK_LOCAL_URL = os.getenv("RERANK_LOCAL_URL", "http://127.0.0.1:8090").rstrip("/")
RERANK_TIMEOUT_S = float(os.getenv("RERANK_TIMEOUT_S", "30"))

# ---- 文档级预筛（doc router）：把明显不相关的文档【整份】排除 ----
# 依据 §39.15 的零 API 标定（100 份合成语料 + 构造性 ground truth，非循环）：
#   · idf-set 打分 + ratio 阈值：候选从 100 份砍到 3.3 份/题，recall 仍 102/102
#   · 核心词门：文档必须命中问题里 IDF 最高的两个词之一（真实库里正是靠它把
#     《大学生生涯发展报告》筛掉：idf-set 1.28 且不含「综测」）
#   · ★ top-M 已被数据否决：泛问（「奖学金怎么申请」构造上就有 8 份相关文档）
#     按定义会被截断 ⇒ 泛问达标 0/2。所以这里只用 ratio，不用 top-M。
DOC_ROUTER_ENABLED = os.getenv("DOC_ROUTER", "on").strip().lower() not in ("off", "0", "false")
DOC_ROUTER_RATIO = float(os.getenv("DOC_ROUTER_RATIO", "0.3"))
# 核心词门：0 = 关闭。★ 默认关闭是【扫描测出来的】，不是图省事：
#   test_doc_router.py 第 4 节用「近重复相关文档 + 塞满通用词的干扰文档」扫 N=2..100，
#   门开时在 N=3/4/5 掉召回，门关只在 N=3/4 掉；且在合成语料上开不开门 recall 与
#   保留份数完全相同（102/102、3.3 份）⇒ 门只带来风险、没有收益。
DOC_ROUTER_CORE_TOP = int(os.getenv("DOC_ROUTER_CORE_TOP", "0"))
# 语料太小时 IDF 不稳（近重复会把核心词自己的 IDF 压低）⇒ 宁可不筛（返回 None）。
# ★ 2 是【知情取舍】，不是忽略证据：test_doc_router.py 第 4 节扫出失败 N = 3/4（门关口径），
#   第 5 节又测了 k=N（全部文档都相关）——N=2..20 全部一份不砍；
#   且真实库（2 份）在 §39.15.5 实测正确：doc 1 留、doc 13 砍。
#   取 2 是为了让“只有两份文档”的真实用户就能用上；代价是 3~4 份文档的库会落进已知失败区。
#   回退到保守值：设环境变量 DOC_ROUTER_MIN_DOCS=6。
DOC_ROUTER_MIN_DOCS = int(os.getenv("DOC_ROUTER_MIN_DOCS", "2"))


class _RerankScore:
    """把本地返回的 {index, relevance_score} 包成与 dashscope 结果同形的对象。

    下游（reranked）只读 .index 与 .relevance_score，所以本地/云端两条路
    可以共用同一段排序与阈值代码，不必写两套分支。
    """
    __slots__ = ("index", "relevance_score")

    def __init__(self, index: int, relevance_score: float):
        self.index = index
        self.relevance_score = relevance_score


def _rerank_local(query, documents):
    """调本地重排服务，返回 list[_RerankScore]。

    ★ 与云端的一个关键差异：**不传 top_n**。
      云端传了 top_n 就只回前 N 条，MIN_RERANK_SCORE 的过滤遍历不到被截掉的
      候选，阈值形同虚设。本地服务返回【全部】候选的分数，切片统一由调用方
      （reranked 里的 results[:top_n]）做 —— 两条路的语义因此才真正等价。
    """
    resp = requests.post(RERANK_LOCAL_URL + "/rerank",
                         json={"query": query, "documents": documents},
                         timeout=RERANK_TIMEOUT_S)
    if resp.status_code != 200:
        raise RuntimeError("HTTP %d %s" % (resp.status_code, resp.text[:140]))
    data = resp.json()
    raw = data.get("results")
    if not raw:
        raise RuntimeError("空 results: %s" % str(data)[:140])
    if data.get("truncated"):
        # 截断必须记账（rerank_server 设计第 4 条）：长 chunk 的尾部可能被切掉
        note_degradation("rerank", "truncated", "%s 篇超长被截断" % data["truncated"])
    return [_RerankScore(int(it["index"]), float(it["relevance_score"]))
            for it in raw]


def _rerank_cloud(query, documents, top_n):
    """调 DashScope 云端重排（仅 RERANK_BACKEND=cloud 时走）。"""
    resp = TextReRank.call(model="qwen3.7-text-rerank",
                           api_key=os.getenv("QW_API_KEY"),
                           top_n=top_n, query=query, documents=documents)
    output = getattr(resp, "output", None)
    results = getattr(output, "results", None)
    if not results:
        raise RuntimeError("无结果 status=%s code=%s msg=%s" % (
            getattr(resp, "status_code", "?"), getattr(resp, "code", "?"),
            str(getattr(output, "message", "")
                or getattr(resp, "message", ""))[:100]))
    return results


def _rerank(query, documents, top_n):
    """按 RERANK_BACKEND 选后端；返回带 .index/.relevance_score 的结果列表。

    失败一律抛异常，由 reranked() 统一记录为"可观测降级"——
    绝不在这里静默返回空列表（那正是改造前的病）。
    """
    if RERANK_BACKEND == "off":
        raise RuntimeError("RERANK_BACKEND=off（显式关闭重排）")
    if RERANK_BACKEND == "cloud":
        return _rerank_cloud(query, documents, top_n)
    return _rerank_local(query, documents)


def rerank_status() -> dict:
    """可观测：当前重排后端配置 + 累计降级计数（供诊断脚本/日志用）。"""
    return {"backend": RERANK_BACKEND, "local_url": RERANK_LOCAL_URL,
            "timeout_s": RERANK_TIMEOUT_S, "degraded": degraded_counts()}


def user_doc_ids(user_id: int) -> list[int]:
    """当前用户【可检索】的 document_id 列表 —— 元数据过滤的唯一数据源。

    = 自己名下的文档 ∪ 公共库文档（is_shared=1）。
    公共库用于"学校统一评定办法"这类所有人都该看到的资料。
    fails closed：既不是自己名下、也没被显式标记共享的文档，谁都查不到。
    """
    with Session(engine) as s:
        rows = s.query(KnowledgeDocument.id).filter(
            or_(KnowledgeDocument.user_id == user_id,
                KnowledgeDocument.is_shared.is_(True))
        ).all()
        return [r[0] for r in rows]


_MISSING = object()          # 哨兵：用来区分"缓存里没有这个用户"与"缓存里的值是 None"

_BM25_CACHE: dict[int, "BM25Retriever | None"] = {}

# 世代号：user_id -> 该用户被失效的次数。只存版本号、不存语料，极小。
# 用途见 bm25_for 的注释：解决"失效被并发构建覆盖"的竞态（§22 实测）。
_BM25_GEN: dict[int, int] = {}


def _build_bm25(docs: list[Document]) -> BM25Retriever:
    r = BM25Retriever.from_documents(docs, preprocess_func=lambda x: jieba.lcut(x))
    r.k = BM25_K
    return r


def bm25_for(user_id: int) -> "BM25Retriever | None":
    """按用户懒建并缓存 BM25 —— 替换掉原来 import 期建好的全局 bm25_retriever。

    该用户没有任何向量时返回 None（空语料不能喂给 BM25 的 idf 计算）。

    ⚠️ 并发安全（下面两条都由 §22 的实测竞态驱动，不是凭空加的）：
      · 返回值一律取自【局部变量 built】，不再最后查一次 dict。
        否则"检查命中"与"读取"之间若被另一线程 pop，会抛 KeyError ——
        异常会冒到 reranked() → /chat 变成"发生错误:…"，即用户提问直接失败。
      · 写缓存前比对世代号：构建期间若发生过失效，就【不写】缓存。
        否则会把【旧语料】写回缓存，而此后无人再失效它 → 该用户 BM25
        永久少召回新入库的文档（实测构建窗口 70~100ms，上传后立刻提问就会中）。
      · 命中路径不加锁：一次冷构建约 100ms，若用全局锁会把**其他所有用户**的
        检索一起阻塞。世代号方案的代价只是可能重复构建一次。
    """
    cached = _BM25_CACHE.get(user_id, _MISSING)
    if cached is not _MISSING:
        return cached

    gen_at_start = _BM25_GEN.get(user_id, 0)

    ids = user_doc_ids(user_id)
    docs: list[Document] = []
    if ids:
        raw = vectorstore.get(where={"document_id": {"$in": ids}})
        docs = [Document(page_content=t, metadata=m)
                for t, m in zip(raw["documents"], raw["metadatas"])]
    built = _build_bm25(docs) if docs else None

    if _BM25_GEN.get(user_id, 0) == gen_at_start:
        _BM25_CACHE[user_id] = built
    # 世代号变了 ⇒ 这份 built 已经过期，直接返回给本次调用但不缓存
    return built


def invalidate_bm25(user_id: int) -> None:
    """该用户入库成功后【必须】调用，否则新文档在 BM25 这一路召回不到。

    ⚠️ 向量那一路每次现查 Chroma，所以新上传的文档不会完全搜不到 ——
    漏的只是 BM25 那一路的召回（实测：不失效时 new_001 不在结果里，失效后排第 1）。
    """
    _BM25_GEN[user_id] = _BM25_GEN.get(user_id, 0) + 1   # 先递增世代号，再清缓存
    _BM25_CACHE.pop(user_id, None)
    invalidate_doc_index(user_id)      # ★ doc router 必须跟着失效，否则新文档被路由漏掉


def invalidate_all_bm25() -> None:
    """清空【所有】用户的 BM25 缓存。

    用于"共享范围变化"这类影响每个用户的改动（database.set_document_shared），
    否则各用户缓存里仍是旧的文档集合。

    ⚠️ 必须遍历 _BM25_CACHE ∪ _BM25_GEN，不能只看缓存键：
    只看缓存会漏掉"当前没有缓存、但**正被别的线程构建**"的用户 ——
    那些构建完成时会把旧语料写回来（§22 缺陷1 的同一机制）。
    """
    for uid in set(_BM25_CACHE) | set(_BM25_GEN):
        _BM25_GEN[uid] = _BM25_GEN.get(uid, 0) + 1
    _BM25_CACHE.clear()
    for uid in set(_DOC_INDEX_CACHE) | set(_DOC_INDEX_GEN):
        _DOC_INDEX_GEN[uid] = _DOC_INDEX_GEN.get(uid, 0) + 1
    _DOC_INDEX_CACHE.clear()

# ==================== doc router：文档级索引与打分 ====================
# 与 BM25 缓存同构（世代号防竞态），并且【共用同一套失效入口】——
# 否则新上传的文档会被路由漏掉（与 §22 的 BM25 缺陷同一机理）。

_DOC_INDEX_CACHE: dict[int, "dict | None"] = {}
_DOC_INDEX_GEN: dict[int, int] = {}
_DOC_INDEX_LAST_ERROR: dict[int, str] = {}   # 最近一次构建失败原因（供 trace 如实上报）


def _doc_tokens(text: str) -> list[str]:
    """与标定脚本逐字一致的分词口径（单字词丢弃）——口径一变，标定结论就失效。"""
    return [w for w in jieba.lcut(text or "") if len(w.strip()) > 1]


def _build_doc_index(user_id: int):
    """文档级索引：{document_id: (标题, 全文词频 Counter)}，只覆盖该用户【可检索】的文档。"""
    ids = set(user_doc_ids(user_id))
    if not ids:
        return None
    raw = vectorstore.get(where={"document_id": {"$in": list(ids)}})
    agg: dict[int, tuple[str, str]] = {}
    for text, meta in zip(raw["documents"], raw["metadatas"]):
        did = meta.get("document_id")
        if did not in ids:            # 护栏：索引里也只许出现该用户可检索的文档
            continue
        title, body = agg.get(did, ("", ""))
        title = title or (meta.get("doc_title") or meta.get("filename") or "")
        agg[did] = (title, body + " " + (text or ""))
    if not agg:
        return None
    return {did: (title, Counter(_doc_tokens(body))) for did, (title, body) in agg.items()}


def doc_index_for(user_id: int):
    """按用户懒建并缓存文档级索引（与 bm25_for 同样的并发约定）。"""
    cached = _DOC_INDEX_CACHE.get(user_id, _MISSING)
    if cached is not _MISSING:
        return cached
    gen_at_start = _DOC_INDEX_GEN.get(user_id, 0)
    try:
        built = _build_doc_index(user_id)
    except Exception as exc:
        # ★ fail-open：文档级筛选只是【优化】，绝不能让它把整个提问变成报错。
        #   最坏结果只是退回「不过滤」= 改动前的行为。失败原因照常记下并在 trace 里如实上报。
        _DOC_INDEX_LAST_ERROR[user_id] = "%s: %s" % (type(exc).__name__, exc)
        return None
    _DOC_INDEX_LAST_ERROR.pop(user_id, None)
    if _DOC_INDEX_GEN.get(user_id, 0) == gen_at_start:
        _DOC_INDEX_CACHE[user_id] = built
    return built


def invalidate_doc_index(user_id: int) -> None:
    """入库成功后必须调用（已挂进 invalidate_bm25，一般不单独调）。"""
    _DOC_INDEX_GEN[user_id] = _DOC_INDEX_GEN.get(user_id, 0) + 1
    _DOC_INDEX_CACHE.pop(user_id, None)


def route_doc_ids_pure(query: str, docs: "dict[int, tuple[str, object]]"):
    """★ 纯函数：给定问题与 {doc_id: (标题, 词频 Counter)}，返回应检索的 doc_id 集合。

    返回 None 表示【不筛】（任一安全条件不满足就走这条路）：
      · 文档数 < DOC_ROUTER_MIN_DOCS（语料太小，IDF 不稳）
      · 问题分词后没有可用词
      · 最高分为 0（没有任何文档命中）
      · 筛完为空（★ 兜底：宁可不过滤，也绝不因此答"未检索到资料"）
    """
    docs = {d: tf for d, (t, tf) in docs.items() if tf}
    if len(docs) < DOC_ROUTER_MIN_DOCS:
        return None
    qterms = set(_doc_tokens(query))
    if not qterms:
        return None

    sets = [set(tf) for tf in docs.values()]
    n = len(docs)
    idf = {}
    for w in set().union(*sets):
        df = sum(1 for st in sets if w in st)
        idf[w] = math.log(1 + (n - df + 0.5) / (df + 0.5))

    score = {d: sum(idf.get(t, 0.0) for t in qterms if tf[t]) for d, tf in docs.items()}
    top = max(score.values())
    if top <= 0:
        return None
    # 核心词门：DOC_ROUTER_CORE_TOP <= 0 表示关闭该门（只留 ratio）。
    # ★ 门是【有条件】有效：§39.16 扫描实测，用户文档少且相关文档互为近重复时它会选错核心词
    #   （近重复抬高「综测」的 df ⇒ 压低其 IDF ⇒ 门改挑通用词「提高/成绩」⇒ 相关文档全灭）。
    core = (sorted(qterms, key=lambda t: -idf.get(t, 0.0))[:DOC_ROUTER_CORE_TOP]
            if DOC_ROUTER_CORE_TOP > 0 else [])
    keep = {d for d, s in score.items()
            if s > 0 and s >= top * DOC_ROUTER_RATIO
            and (not core or any(docs[d][t] for t in core))}
    return keep or None


def route_doc_ids(query: str, user_id: int, trace: dict | None = None):
    """检索层入口：返回本题应检索的 doc_id 集合；None = 不过滤（现状行为）。"""
    if not DOC_ROUTER_ENABLED:
        if isinstance(trace, dict):
            trace["doc_router"] = {"reason": "disabled_by_env"}
        return None
    idx = doc_index_for(user_id)
    if not idx:
        if isinstance(trace, dict):
            err = _DOC_INDEX_LAST_ERROR.get(user_id)
            trace["doc_router"] = {"reason": ("index_error" if err else "no_index"),
                                   **({"error": err} if err else {})}
        return None
    too_few = len(idx) < DOC_ROUTER_MIN_DOCS
    kept = None if too_few else route_doc_ids_pure(query, idx)
    if isinstance(trace, dict):
        # ★ 说真话：被安全开关挡下时必须写明原因，不能报成「已启用且没筛掉任何东西」。
        if too_few:
            reason = "too_few_docs"
        elif kept is None:
            reason = "no_match_or_would_be_empty"
        else:
            reason = "filtered"
        trace["doc_router"] = {
            "reason": reason, "docs_total": len(idx),
            "docs_kept": (len(kept) if kept is not None else len(idx)),
            "filtered": kept is not None, "min_docs": DOC_ROUTER_MIN_DOCS,
        }
    return kept


# ============ 模型角色：唯一构造点在 llm_roles.make_model ============
# 角色 → 后端映射集中在 config.ROLE_MODELS；重试退避 / JSON 容错 / 降级计数在 llm_roles。
# 本会话实测（100+ 次调用）：
#   · enable_thinking=False 让改写 18.0s → 1.4s（13.1×），JSON 可靠性不变
#   · 魔搭免费额度【按模型限流】→ 顺序连打会 429，必须重试退避
#   · 模型可能输出 ``` 围栏：原 cleanup 剥不掉 → 静默降级成单查询
classify_model, CLASSIFY_CFG = make_model("classify")   # Qwen3.5-35B-A3B（实测判对 20/20）
rewrite_model, REWRITE_CFG = make_model("rewrite")      # DeepSeek-V4.1-Flash
answer_model, ANSWER_CFG = make_model("answer")         # DeepSeek-V4.1-Flash
direct_model, DIRECT_CFG = make_model("direct")         # Qwen3.8-Flash-Next

# 兼容别名：既有实验脚本会 patch lp.model / lp.model1
model = answer_model
model1 = direct_model
# ============ 新增：问题分类 ============

# ---------- 分类 prompt：两个模板（无上文 / 有上文）----------
# ★ 为什么是"两个模板"而不是"一个模板 + partial 注入"：
#   ChatPromptTemplate.partial() 是【一层】变量替换，**不会递归展开**
#   注入字符串里的 {history} —— 那样 {history} 会以字面量留在发给模型的 prompt 里。
#   双模板最直白，且无隐式行为。
#
# ★ 为什么这两段一定有效（§35 4 臂交错 160 次调用的实测）：
#   多轮追问的误判 4/7 → 0/7，总体漏判 20.7% → 3.4%，**代价是 0 次额外调用**。
_CLASSIFY_HEAD = """你是一个问题分类助手。请判断以下用户问题是否与「奖学金」或「综测」（综合素质测评）等学校问题相关。

分类规则：
- 如果问题涉及奖学金申请、评定、发放、条件、综测加分、综测计算、学校等相关内容，返回 "YES"
- 如果问题完全不涉及奖学金或综测或学校，返回 "NO"
"""
_CLASSIFY_HIST_RULE = '- 如果当前问题单独看无法判断、但结合【上文对话】明显是在追问同一话题，返回 "YES"\n'
_CLASSIFY_TAIL = '- 只返回 YES 或 NO，不要包含任何其他内容\n'

classify_prompt = ChatPromptTemplate.from_template(
    _CLASSIFY_HEAD + _CLASSIFY_TAIL
    + "\n【用户问题】: {question}\n\n【分类结果】:"
)
classify_prompt_hist = ChatPromptTemplate.from_template(
    _CLASSIFY_HEAD + _CLASSIFY_HIST_RULE + _CLASSIFY_TAIL
    + "\n【上文对话】: {history}\n\n【用户问题】: {question}\n\n【分类结果】:"
)
classify_chain = classify_prompt | classify_model | StrOutputParser()
classify_chain_hist = classify_prompt_hist | classify_model | StrOutputParser()


# ============ 会话标题生成（方案 B）：1 次轻量调用，失败一律退回截断标题 ============
TITLE_PROMPT = """你是会话标题助手。请为下面这段对话起一个中文短标题。

要求：
- 6~14 个字，一眼看出主题；
- 只输出标题本身：不要引号、不要句号、不要「标题：」、不要解释、不要换行；
- 如实概括，不要编造资料里没有的信息。

学生提问：{q}
助手回答的开头：{a}
标题："""

_TITLE_STRIP = "「」『』\"'《》*·-—　 \t"
_TITLE_REFUSAL = ("抱歉", "无法回答", "不能提供", "作为一个", "作为AI", "作为 AI")


def sanitize_generated_title(raw: str | None, max_len: int = 24) -> str | None:
    """把模型输出整理成可用的会话标题；不合格一律返回 None（调用方保留截断标题）。

    ★ 判得严的理由：标题直接显示在侧栏，出现「抱歉，我无法…」这类东西比不生成更糟。
    """
    if not raw:
        return None
    t = raw.strip().splitlines()[0].strip()
    for pre in ("标题：", "标题:", "Title:", "title:"):
        if t.startswith(pre):
            t = t[len(pre):].strip()
    t = t.strip(_TITLE_STRIP)
    t = t.rstrip("。.!！?？,，;；:：")
    t = " ".join(t.split())
    if not t or len(t) > max_len:
        return None
    if any(bad in t for bad in _TITLE_REFUSAL):
        return None
    return t


def make_conversation_title(question: str, answer: str) -> str | None:
    """用 1 次轻量调用（role=title = Qwen3.5-35B-A3B）生成会话标题。

    ★ 任何失败（限流/超时/输出不合格）都返回 None —— 调用方保留 A 的截断标题。
      起名失败绝不能让已经发出去的回答受影响，这是本函数的硬约束。
    """
    try:
        # ★ make_model 返回的是【(model, cfg)】元组，必须拆包！
        #   第一版写成 `llm = make_model("title")` 再直接传给 invoke_with_retry，
        #   实侧必然 AttributeError: 'tuple' object has no attribute 'invoke'
        #   —— 不会报错给用户，但 B 等于永远不生效。测试里有专门的回归锁。
        model, _cfg = make_model("title")
        prompt = TITLE_PROMPT.format(q=(question or "")[:300], a=(answer or "")[:300])
        result, _attempts = invoke_with_retry(model, prompt, role="title", max_attempts=2)
        # ★ 传裸模型时返回的是 AIMessage，不是 str！
        #   classify 传的是 chain（链尾有输出解析）所以拿到 str。
        #   第一版直接 sanitize(result) => AttributeError: 'AIMessage' object has no attribute 'strip'
        #   —— 这一次真白花了 1 次调用，所以不再靠猜。
        text = result if isinstance(result, str) else getattr(result, "content", None)
        if not isinstance(text, str):
            note_degradation("title", "content_not_str", type(result).__name__)
            return None
        title = sanitize_generated_title(text)
        if title is None:
            note_degradation("title", "output_rejected", repr(str(text)[:60]))
        return title
    except Exception as e:
        # ★ 记进降级计数（同 classify）：否则 B 失败就完全不可观测。
        note_degradation("title", "call_failed", "%s: %s" % (type(e).__name__, str(e)[:120]))
        return None


def classify(question: str, context: "ConversationContext | None" = None) -> bool:
    """返回 True 表示与奖学金/综测相关。

    失败时【fail-open 到检索】—— 与改造前相反，且是刻意的：
      改造前：异常或格式异常都判 NO → 走 direct_chain → 【整轮检索被静默跳过】。
      现在：判不准就走 RAG（rag_prompt 已写明非奖学金问题可忽略参考资料）。
    实测：魔搭按模型限流，classify 连打 3/20 触发 429（重试后全部恢复）。

    context：可选对话上下文（多轮追问的判定主要靠它）。
      不给 / 为空 ⇒ **走原模板，行为与改造前逐字节一致**（不是"塞个空历史"）。
      ★ 兼容性：旧调用方 `classify(question)` 一行不用改。
    """
    history = context.render() if context is not None else ""
    if history:
        chain, payload = classify_chain_hist, {"question": question, "history": history}
    else:
        chain, payload = classify_chain, {"question": question}
    try:
        # ★ 用 invoke_with_retry 而不是 with_retry：前者多带【额度耗尽自动换 key】
        #   （池来自 make_model，见 llm_roles）。classify 也走这条路，
        #   否则 classify 会是全链上唯一不会换 key 的一环。
        result, _attempts = invoke_with_retry(chain, payload, role="classify")
    except Exception as e:
        note_degradation("classify", "call_failed", "%s: %s" % (type(e).__name__, str(e)[:120]))
        return True
    if not isinstance(result, str):
        note_degradation("classify", "content_not_str", type(result).__name__)
        return True
    text = result.strip().upper()
    if "YES" in text:
        return True
    if "NO" in text:
        return False
    note_degradation("classify", "unparsable_answer", repr(result[:80]))
    return True


# ============ 原有的 RAG 逻辑 ============

def rewrite(query, context=None):
    """把用户提问改写成 5 个检索关键词。

    context：可选对话上下文。
      ★ 为什么 rewrite 尤其需要它（§35 实测）：指代型追问（"那要是补助呢？"）
        单看无法改写 —— 正确答案 19% → **86%**（加了【上文对话】后）。
      ★ 规则 5 的单独贡献【已测出】= **+14 个点**（71% → 86%，`HIST` vs `HIST_RULE` 对照，
        7 用例，三个独立信号同向）：残留指代 1 → 0、上文对象吸收 26 → 36、
        目标概念丢失 0 例。**⇒ 保留规则 5**（零额外 API 成本，只是 prompt 变长）。
        ⚠️ 但规则 5 与规则 4（"不可超过原问题长度"）**相互冲突**：超长 query 10 → 14，
        因为它要求"让没看过上文的人也能看懂"。**可接受的理由**：`queries` 只喂召回
        （BM25 + MMR），重排收 `query=original_query`；召回侧天生偏好更长更明确的 query。
        ⚠️ 幅度不可当精确值（样本仅 7 例）；机制级结论可迁移，换后端须重测。
      ⚠️ 早期我曾记"规则贡献为零"，那是**无效对照**导致的：`test_rewrite_depth.py` 的
        `D0c` 深度=0、`{hist}` 是空串，"用上文替换指代"这条规则**无法执行** ⇒
        与 `D0` 相同是定义上的必然，推不出"规则无用"。
    """
    hist = context.render() if context is not None else ""
    hist_block = "【上文对话】:\n%s\n\n" % hist if hist else ""
    prompt = f"""你是一个搜索引擎优化专家。请将用户的提问先抽取问题最关键的词组，并改写为 5 个近义或同义的检索关键词，以便在知识库中精准匹配。
【改写策略】：
1. 如果问题只有一个条件就直接简写问题，并提取关键词，删去非关键成分(人物代称等)
2. 如果问题有多个条件，那就有几个条件就拆分成几个原始问题，然后再进行关键词改写
3. 改写关键字首先考虑同义或近义词，并且不要出现两个近义词在同一个句子里
4. 改写后的问题不可超过原问题长度，可大胆删减不重要的字
5. 如果用户提问里有指代（那/这个/它/上述/该/其）或省略了主语，必须先用【上文对话】把它替换成实际对象，再改写；改写结果要让一个没看过上文的人也能看懂在问什么。

{hist_block}【用户提问】: {query}
【输出要求】: 
必须严格输出为一个 JSON 格式的字符串数组（List），不要包含任何额外的解释或 Markdown 标签。
例如：["改写关键词1", "改写关键词2", "改写关键词3", "改写关键词4", "改写关键词5"]

【输出 JSON】:"""
    try:
        response, _attempts = invoke_with_retry(rewrite_model, prompt, role="rewrite")
    except Exception as e:
        note_degradation("rewrite", "call_failed", "%s: %s" % (type(e).__name__, str(e)[:120]))
        return [query]
    if not isinstance(response.content, str):
        note_degradation("rewrite", "content_not_str", type(response.content).__name__)
        return [query]
    queries = loads_json_list(response.content)   # 容错：剥掉 ``` 与三引号围栏
    if not queries:
        note_degradation("rewrite", "json_unparsable", repr(response.content[:120]))
        return [query]
    if "综测" not in query:
        queries.append(query)
    print(queries)
    return queries


# RAG 模式的 prompt（带参考资料）
# ============ 权威「当前日期」注入（§39.38.16）============
# ★ 为什么必须有（§39.38.14 取证，msg140）：本模块的 prompt 原本**没有任何时间锚点**
#   （`datetime / now() / 今天` 在整个模块里一处都搜不到），于是模型对「今天几号 / 最近 / 最新」
#   只能靠猜或抄搜索结果 ⇒ 实测给出过「当前日期为 2026年9月24日」（**错 19 天**，语气确定），
#   以及把 **2025 年**的旧闻当成「最近一周」。
# ★ 为什么是「算」而不是「写死」：日期必须**每次请求按系统时钟算**。写死一个字符串就等于
#   每天都要人来改（而且必然忘记）—— 那只是把旧 bug 换成另一种形状。装置里有专门一条
#   「源码里不许出现当天的字面日期」来钉住这一点（见 verify_today_line.py ④）。
# ★ 为什么不用 ZoneInfo("Asia/Shanghai")：本机**缺 tzdata** ⇒ `ZoneInfoNotFoundError`（实测）。
#   本机时区已是 `China Standard Time`(UTC+8)，所以 `datetime.now()` 取到的就是本地时间。
# ★ 为什么星期要自己映射：`strftime("%A")` 在本机输出英文 `Wednesday`（locale 相关），
#   直接进中文 prompt 很突兀。
# ★ 可注入：`now=` 让装置传**假时钟** ⇒ 「午夜翻转 / 跨年 / 七个星期」都能断言，不必等到明天。
_WEEKDAY_CN = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
TODAY_SLOT = "{today}"


def _today_line(now=None) -> str:
    """返回注入 prompt 的「当前日期」行（权威、每次请求现算）。

    `now=None` ⇒ 读系统时钟；装置传假时钟即可测边界（不会"今天测不了明天"）。
    """
    t = now if now is not None else datetime.now()
    # ★ 返回值**不带换行**：模板里 {today} 所在那行已经提供了换行（它顶替的正是原来那个空行），
    #   这里再带一个就会多出空行 ⇒ 装置⑥「today="" 时逐字回到旧模板」也会失效。
    return ("【当前日期】%s %s（由系统时钟提供，权威；涉及「今天/最近/最新/今年」这类"
            "时效性问题时以此为准，不要凭记忆或搜索结果推测）"
            % (t.strftime("%Y-%m-%d"), _WEEKDAY_CN[t.weekday()]))


rag_template = """你是学校奖学金/综测知识库助手。第一原则是【切题】，其次才是完整。
{today}
【第一步：先判断问题类型 —— 这决定你答多细】
· A 查规则型：问"有哪些 / 是多少 / 什么条件 / 怎么算 / 谁可以"
  ⇒ 逐条覆盖，不许漏条目（照 A 组规则答）。
· B 求建议型：问"怎么提高 / 怎么拿分 / 如何准备 / 有什么建议 / 怎样才能"
  ⇒ **先给重点与优先级**，再给依据；**可以概括，不要罗列全部条款**（照 B 组规则答）。
· 拿不准时按 A 处理。

【A 组 · 查规则型 —— 完整性优先】
1. 参考资料中与问题相关的每一条条款、每一个条件、每一个数字都要出现。
2. 编号列举的内容必须逐条覆盖，不得跳过、不得合并、不得用"等"概括。
3. 条款里的限定语和例外本身就是信息（如"处分未解除""因病或残疾除外"），必须保留。
4. 【压缩书写】逐条覆盖时写"要点短语"，不要照抄整句：长条款压成短句，
   但**数字、比例、名额、条件、例外必须一字不改地保留，不能把限定成分删掉**。
   例如"上学期所学课程（不含通识教育选修课、跨学科选修课）有补考或重修者"可写成"有补考或重修"；
   但"**必修课和限定性选修课**各科成绩学时加权平均成绩达 70 分"里的"必修课和限定性选修课"
   是**申报条件本身**，一个字都不能省。
5. 目标：让用户在最短篇幅内看全所有条目。

【B 组 · 求建议型 —— 重点优先】
6. **开头 3–5 句直接给"最该做的几件事"**，按【权重 / 门槛 / 易得分】排序。
   不要先铺陈制度介绍。
7. **明确点出"哪里最容易丢分"** —— 基本分与减分项通常比加分项更影响总分。
8. 主体按【可操作动作】组织（如"先把基本分拿满""优先冲哪类加分"），
   **不要按资料的章节顺序**逐条罗列。
9. 细则只在直接支撑某条建议时展开；**不逐条罗列**全部加分项。
10. **结构上限（比字数更好执行）**：开头一段 3–5 句重点 + **最多 5 条建议**，
    每条建议正文不超过 2 行，细节点到为止。全文尽量控制在 800 字以内。
    宁可少说几条，也不要退化成条款清单。
11. 若资料没有给出权重，就写"资料未给权重，以下按加分幅度排序"——**不要编造权重**。

【两组共同】
12. **不要用用户的问题当标题或开头第一句**，也不写"根据参考资料""综上所述"这类套话，
    不做资料之外的发挥。直接进入内容。
13. 与奖学金/综测无关的问题：忽略资料直接回答，字数 100 字以内。
14. 【引用规则】一个小节只标一次来源编号，放在该小节标题末尾（如"一、基本条件 [2]"）；
    **小节内部不再逐条标注**。求建议型的回答没有小节标题，**整篇最多标 2–3 处**
    （放在最需要出处的那句末尾），**不要每一行都挂 [1]**。
    确实来自多个来源时才并列（[1][3]）；写半角方括号；
    只能标真正用到的段落，绝不编造编号；完全没用资料就不标。

【参考资料】
{context}
【用户问题】
{question}
【你的回答】："""

rag_prompt = ChatPromptTemplate.from_template(rag_template)

# ★ 多轮版模板（§39.35）：把"本轮检索是怎么理解这个问题的"交给**回答侧**。
#   为什么需要：改写属**召回侧**，它已经把"那/它"解成了具体话题；但回答侧拿到的
#   只有【用户原问题】—— 于是实测出现"检索命中了加分上限，回答却写
#   「'它'指代不明确，请补充你想问的是哪一项的上限」"（§39.33.6 第 3 轮）。
#   ★ 做法：**从 rag_template 机械插入一个槽位**得到（单一事实来源，避免两份模板日后漂移）。
#     位置刻意选在【用户问题】**之前**：提示紧邻问题，模型更容易把它当成"对问题的理解"。
#   ★ 槽位为空时渲染结果与旧模板**逐字相同** ⇒ 单轮请求的行为一字不变
#     （这条性质由 kb_bench/verify_answer_side_hint.py 钉着，不是靠自觉）。
#   ★ 只动回答侧：检索与重排仍全部使用【原始提问】（§36.4-1 的实测结论不变）。
ASK_HINT_SLOT = "{ask_hint}"
rag_template_multi = rag_template.replace("【用户问题】", ASK_HINT_SLOT + "【用户问题】", 1)
rag_prompt_multi = ChatPromptTemplate.from_template(rag_template_multi)

# ★ 联网搜索块（§39.38）：**同一手法再插一个槽**。
#   `websearch.render_web_block()` 返回 "" 时（没开联网搜索）渲染结果与 `rag_template_multi`
#   **逐字相同** ⇒ 既有行为一个字节不变（这条性质由 kb_bench/verify_websearch.py 钉着）。
#   位置：紧贴【用户问题】之前、`{ask_hint}` 之外侧 —— 与"参考资料"相邻，模型更容易把它
#   当成"另一类资料"而不是问题的一部分。
WEB_SLOT = "{web_block}"
_WEB_ANCHOR = ASK_HINT_SLOT + "【用户问题】"
rag_template_web = rag_template_multi.replace(_WEB_ANCHOR, WEB_SLOT + _WEB_ANCHOR, 1)
rag_prompt_web = ChatPromptTemplate.from_template(rag_template_web)


# ============ 来源溯源（引用） ============
#
# ★ 为什么元数据只能当"有则用"：json_to_db.py 会把 doc_title / section_path /
#   score_type 等写进 Chroma metadata，但【早期入库的文档没有这些字段】——
#   实测库里 document_id 1/2/3/4/5 只有 {chunk_id, document_id}，
#   而 6/8/9/10/11/12/13 才有全套。偏偏 id=1 就是最常用的《奖学金评定办法》。
#   所以走"元数据优先、正文富头部兜底"，文件名一律以 SQLite 为准。

_HEADER_KEYS = ("文档路径", "适用对象", "规则类别")


def parse_chunk_header(text: str) -> dict:
    """解析入库时写在 chunk 正文开头的富头部（【文档路径】/【适用对象】/【规则类别】）。"""
    out = {}
    for line in (text or "").split("\n"):
        for key in _HEADER_KEYS:
            prefix = "【" + key + "】:"
            if line.startswith(prefix):
                out[key] = line[len(prefix):].strip()
    return out


def _doc_filenames(document_ids) -> dict:
    """按 document_id 批量取文件名（SQLite 是命名与归属的权威来源）。"""
    ids = sorted({i for i in document_ids if isinstance(i, int)})
    if not ids:
        return {}
    try:
        with Session(engine) as s:
            rows = (s.query(KnowledgeDocument.id, KnowledgeDocument.filename)
                     .filter(KnowledgeDocument.id.in_(ids)).all())
        return {r[0]: r[1] for r in rows}
    except Exception as e:            # 溯源失败不该拖垮回答本身
        print("取文件名失败:", e)
        return {}


def build_sources(docs, scores=None) -> list:
    """把重排后的 chunk 转成前端可展示的来源列表（顺序即引用序号）。"""
    labels = _doc_filenames([(d.metadata or {}).get("document_id") for d in docs])
    out = []
    for i, doc in enumerate(docs):
        md = doc.metadata or {}
        text = doc.page_content or ""
        head = parse_chunk_header(text)
        path = head.get("文档路径") or ""
        body = text.split("【正文内容】:", 1)
        snippet = " ".join((body[1] if len(body) > 1 else text).split())
        did = md.get("document_id")
        score = None
        if scores and i < len(scores) and scores[i] is not None:
            try:
                score = round(float(scores[i]), 4)
            except (TypeError, ValueError):
                pass
        out.append({
            "n": i + 1,
            "document_id": did,
            "chunk_id": md.get("chunk_id") or "",
            "filename": labels.get(did) or "",
            "doc_title": md.get("doc_title") or path.split(" -> ")[0].strip(),
            "section_path": md.get("section_path") or path,
            "score_type": md.get("score_type") or head.get("规则类别") or "",
            "target_audience": md.get("target_audience") or head.get("适用对象") or "",
            "score": score,
            "snippet": snippet[:120],
        })
    return out


def format_docs(docs):
    """拼接参考资料，并给每段加【n】编号。

    ★ 编号是引用溯源的前提：rag_template 要求模型【按小节】用半角方括号 [1] 标注依据。
      这里用全角 【n】 是刻意的：正文里大量出现 "1. 2. 3." 式条款编号，
      同用半角会与正文编号撞车；全角白括号能一眼区分。
      序号必须与这里、以及前端展示的来源列表三者一致。
    """
    return "\n\n".join(
        "【%d】%s" % (i + 1, doc.page_content) for i, doc in enumerate(docs)
    )


def _record(trace, candidates, sources):
    """把本轮检索结果写回调用方（供 /chat 用 SSE 的 sources 事件推给前端）。

    ★ 刻意用"调用方传入的 dict"而不是模块级全局：每个请求自己持有一份，
      并发请求之间不会串档。
    """
    if trace is None:
        return
    trace["attempted"] = True
    trace["candidates"] = candidates
    trace["sources"] = sources or []


def _mark_degraded(trace, reason):
    """把"静默降级"变成"可观测降级"（§23.1 的教训）：写进 trace，随 SSE 到前端。

    ★ 为什么必须写进 trace 而不是只 print：print 只进服务端 stdout，
      前端和用户都看不到"这次的来源顺序其实没经过重排"。
    """
    if isinstance(trace, dict):
        trace["reranked"] = False
        trace["degraded_reason"] = reason
        trace["rerank_backend"] = RERANK_BACKEND


def _fallback_unranked(all_docs_list, top_n, trace, reason):
    """重排不可用时的统一回退：按原始召回顺序取前 top_n 段，并【标记降级】。

    刻意不做成"假装成功"：回退仍然给出可用答案（绝不返回空 context），
    但 trace["reranked"]=False 会让前端提示用户这次的排序未经重排。
    """
    fallback = all_docs_list[:top_n]
    _record(trace, len(all_docs_list), build_sources(fallback))
    _mark_degraded(trace, reason)
    return format_docs(fallback)


def _assert_context_owner(context, user_id: int, where: str) -> None:
    """隔离不变式（§36.2 第 5 项）：对话上下文必须属于 `user_id`。

    为什么必须【结构性】校验，而不是靠 docstring 约定：
      `rewrite()` 会把 `context.render()`（**对话历史原文**）放进发往 LLM 的 prompt，
      `classify()` 也吃 context —— 一旦有人传错一个 context 对象，
      **别人的对话历史就会进入第三方模型的请求里**。
      约定拦不住脚本、测试和以后新增的调用方；`ConversationContext.load()` 的
      归属校验（失败→空上下文）只覆盖"盗用 conv_id"，覆盖不了"误传别人的 context 对象"。

    行为：**显式报错**（与 `user_id` 必填不给默认值同一哲学 ——
      开发期就炸，绝不静默退化成"用别人的上文改写"）。
    """
    if context is None:
        return
    ctx_uid = getattr(context, "user_id", None)
    if ctx_uid != user_id:
        raise ValueError(
            "隔离违规：%s 收到不属于该用户的对话上下文"
            "（context.user_id=%r, user_id=%r）。"
            "请用 ConversationContext.load(conv_id, user_id) 取得上下文；"
            "不要跨用户复用或手工拼装 context。" % (where, ctx_uid, user_id)
        )


def reranked(original_query, user_id, top_n=5, context=None, trace=None):
    """用户隔离版检索：向量 + BM25 都只在该用户自己的文档里检索。

    user_id 为必填 —— 故意不给默认值，让任何遗漏调用方在开发期就报错，
    而不是静默退化成"全库检索"（那正是改造前的行为）。

    context：可选对话上下文，**只喂给 rewrite**（召回侧）。
      ★ 不喂给重排：§35.15 实测（L300，本地重排，6 用例 4 策略）——
        `query=original_query` 在每个截断档上都最好；改成改写结果反而更差
        （S1 让 Q4 的 chunk_003 掉到第 45 名）。
        机理：rewrite 属【召回侧】（召回要多样性 ⇒ 5 条），重排属【排序侧】
        （排序要忠实于真实意图 ⇒ 原始问题）。

    trace：可选 dict，本函数把本轮检索的可观测结果写进去
      {"attempted": True, "candidates": N, "sources": [...]}，供 /chat 经 SSE 推给前端。
      ★ 由调用方【按请求】创建，绝不用模块级全局 —— 否则并发请求会互相串档。
    """
    _assert_context_owner(context, user_id, "reranked()")
    doc_ids = user_doc_ids(user_id)
    if not doc_ids:                       # 早退：省掉 rewrite 的 LLM 调用
        _record(trace, 0, [])
        return "未检索到资料"

    allowed = set(doc_ids)

    # ★ 文档级预筛（§39.15 标定）：把明显不相关的文档整份排除，避免它们白占 top_n 槽位。
    # 实测病灶：用户库里一份《大学生生涯发展报告》占了 top-5 里的 3 个槽，
    # 而真正相关的文档有 8 个块却只进 2 个 —— 害处是"挤占"，不是"答错"。
    # ★ 用【原始问题】而非改写结果：标定的真实语料复现就是按原始问题做的。
    routed = route_doc_ids(original_query, user_id, trace)
    if routed is not None:
        narrowed = [d for d in doc_ids if d in routed]
        if narrowed:                      # ★ 兜底：筛完为空 ⇒ 退回不筛
            doc_ids = narrowed
            allowed = set(doc_ids)
        elif isinstance(trace, dict):
            trace["doc_router"]["fell_back"] = True

    queries = rewrite(original_query, context)
    if isinstance(trace, dict):
        # ★ 把改写结果暴露出来（§39.35）。它同时服务两件事：
        #   ① **回答侧**消解追问指代（build_ask_hint 用它，零额外 LLM 调用）；
        #   ② **可观测**：以前改写结果只 print 在服务器 stdout 里，
        #      于是"改写到底有没有用上文"**无法从响应里断言**（§39.33.6 记的那个缺口）。
        #      trace 本来就是 sources 帧推给前端的整个载荷（§39.28）⇒ 前端与 e2e 都能看到。
        trace["queries"] = list(queries)
    all_docs_dict = {}
    for query in queries:
        vector_docs = vectorstore.max_marginal_relevance_search(
            query, k=VECTOR_K, fetch_k=VECTOR_FETCH_K,
            filter={"document_id": {"$in": doc_ids}},   # ← 显式 filter（第一道）
        )
        bm = bm25_for(user_id)
        bm25_docs = bm.invoke(query) if bm is not None else []

        for doc in bm25_docs + vector_docs:
            # 护栏（第二道）：即使将来某一路的 filter 被改坏，别人的 chunk 也进不了候选池。
            # 同时解决 all_docs_dict 按 page_content 去重时"两个用户上传同一份文件"的 key 撞车串档。
            if doc.metadata.get("document_id") in allowed:
                all_docs_dict[doc.page_content] = doc

    all_docs_list = list(all_docs_dict.values())
    if not all_docs_list:
        _record(trace, 0, [])
        return "未检索到资料"
    try:
        # 后端由 RERANK_BACKEND 决定（默认 local，见文件上方常量区）。
        # 空结果 / HTTP 非 200 / 连接失败都在 _rerank 内部转成异常，
        # 由下面的 except 统一记录为"可观测降级"——不再有静默退回。
        reranked_raw = _rerank(original_query,
                              [doc.page_content for doc in all_docs_list],
                              top_n)

        results = sorted(reranked_raw,
                         key=lambda doc: doc.relevance_score,
                         reverse=True)

        # (chunk, 相关度) 成对保留 —— 这样引用序号 n 与展示用的 score 一定对齐。
        ranked = [(all_docs_list[r.index], r.relevance_score) for r in results[:top_n]]
        if MIN_RERANK_SCORE > 0:          # 默认 0 = 关闭，理由见 MIN_RERANK_SCORE 注释
            kept = [(all_docs_list[r.index], r.relevance_score) for r in results
                    if r.relevance_score >= MIN_RERANK_SCORE][:top_n]
            ranked = kept or ranked        # 熔断：全被切掉就回退，绝不返回空 context
        docs_list = [d for d, _ in ranked]
        scores = [sc for _, sc in ranked]
        _record(trace, len(all_docs_list), build_sources(docs_list, scores))
        if isinstance(trace, dict):
            trace["reranked"] = True
            trace["rerank_backend"] = RERANK_BACKEND
        return format_docs(docs_list)
    except Exception as e:
        note_degradation("rerank", "call_failed",
                         "backend=%s %s: %s"
                         % (RERANK_BACKEND, type(e).__name__, str(e)[:120]))
        # 失败原因必须【可操作】：只说"重排调用失败"，运维无从下手。
        if RERANK_BACKEND == "off":
            reason = "重排已显式关闭（RERANK_BACKEND=off）"
        elif RERANK_BACKEND == "cloud":
            reason = "云端重排调用失败"
        else:
            reason = "本地重排服务不可用（%s）" % RERANK_LOCAL_URL
        return _fallback_unranked(all_docs_list, top_n, trace, reason)


# RAG 链（按用户构建）
# ⚠️ 原来这里是模块级单例 rag_chain，它的 lambda 只能调 reranked(x)，
#    拿不到 user_id —— 所以"给 reranked 加 user_id 参数"必须同时改这里，
#    否则过滤形同虚设（§10 改动清单漏了这一处）。
#
# ⚠️ 输入从"裸 question 字符串"改成 dict（好让 lambda 能拿到 context），
#    因此**不能再用 RunnablePassthrough()**：它只是恒等函数，
#    会把整个 dict 原样交给 prompt，于是 rag_template 的 {question} 被格式化成
#    "{'context': …, 'question': …}" 这种字符串（静默塞进模型上下文）。
#    已核对 langchain_core 源码：RunnableSequence._transform 逐步传流、无 dict 强转，
#    RunnablePassthrough 的 transform 就是 identity ⇒ 这个坑是真的。
#    现在显式取出 question，**检索与回答看到的是同一个字符串**（都不含改写结果）。
def build_ask_hint(trace, context, question) -> str:
    """多轮追问时，把"本轮检索是怎么理解这个问题的"交给**回答侧**（§39.35）。

    病灶（§39.33.6 实测）：改写属**召回侧**，它已经把"那/它"解成了具体话题
    （第 3 轮实测关键词含"专业奖学金加分上限""社会工作素质拓展加分上限"），
    但回答侧此前只拿到**用户原问题** ⇒ 出现"检索命中了加分上限，
    回答却写「'它'指代不明确，请补充你想问的是哪一项的上限」"。

    这里用的关键词是 rewrite() **已经算好的**结果（写在 trace["queries"] 里）
    ⇒ **零额外 LLM 调用、零额外嵌入调用**。

    ★ 只在**【有历史】**时给：单轮请求返回 ""，渲染出的 prompt 与改动前**逐字相同**。
      理由：§39.1/§39.2 的生成侧结论（20/20）是在**没有这个提示**的前提下测的，
      不该顺手改掉单轮行为（blast radius 越小越好，也才好归因）。
    ★ 认不出上下文类型 / 取不到关键词 ⇒ 一律返回 ""（保守：宁可少改，不要瞎猜）。
    ★ 不碰检索与重排（§36.4-1）：本函数产出的只是给模型的说明文字。
    """
    if context is None:
        return ""
    is_empty = getattr(context, "is_empty", None)
    if is_empty is None:
        return ""                      # 认不出上下文类型 ⇒ 保守：不给提示（宁可少改）
    try:
        # ★ `ConversationContext.is_empty` 是 **@property**（不是方法）——
        #   第一版我写成 `if not callable(is_empty): return ""`，于是 property 返回的
        #   bool 被判成"不可调用" ⇒ **提示永远是空的**：一个**静默失效**的接线 bug，
        #   靠 verify_answer_side_hint.py 在零 API 阶段就抓住了（见 §39.35）。
        #   现在两种形状都支持，将来谁把它改成方法也不会再断。
        empty = is_empty() if callable(is_empty) else bool(is_empty)
    except Exception:                                    # noqa: BLE001
        return ""
    if empty:
        return ""
    if not isinstance(trace, dict):
        return ""
    raw = trace.get("queries") or []
    q0 = (question or "").strip()
    # ★ 去掉与用户原问题重复的那条（rewrite 会把原问题附在末尾）：
    #   它已经出现在 {question} 里，再列一遍只会让模型以为"这是两条不同的线索"。
    kws = [q.strip() for q in raw
           if isinstance(q, str) and q.strip() and q.strip() != q0]
    if not kws:
        return ""
    return ("【本轮检索时对本问题的理解】这些是检索时用的关键词，反映「用户在问什么」"
            "（供你消解追问里的指代）：**不要照抄这些词**，也不要据此改变结论或引用编号。\n"
            "检索关键词：%s\n\n" % "、".join(kws))


def _web_block(question, trace, web: bool) -> str:
    """联网搜索块（§39.38）。**`web=False` ⇒ 返回 ""**（prompt 因此与改造前逐字相同）。

    ★ 搜索失败/没有任何相关结果时**也要返回一段话**：诚实规则是"没搜到就说没搜到"，
      而不是让模型自己编一个网络来源（那比不搜更糟）。这段"没搜到"由
      `websearch.render_web_block()` 统一产出，这里不另写一套文案。
    ★ 本函数**绝不抛异常**：联网只是"锦上添花"，绝不能把整条回答搞崩
      （真出错也走"没搜到"这条路，并在 trace 里留下原因 —— 降级可见，不是静默）。
    """
    if not web:
        return ""
    try:
        outcome = websearch.search(question, trace=trace)
    except Exception as e:                                # noqa: BLE001
        outcome = websearch.WebOutcome(attempted=True, reason="http_error",
                                       query=str(question)[:80])
        outcome.reason = "http_error"
        if isinstance(trace, dict):
            status = outcome.to_status()
            status["reason_text"] = "联网搜索内部异常：%s" % type(e).__name__
            trace["web"] = status
        print("[联网搜索] 内部异常（已降级为「没搜到」）：%s: %s" % (type(e).__name__, e))
    return websearch.render_web_block(outcome)


def build_rag_chain(user_id: int, context=None, trace=None, web=False):
    def _question(x):
        """输入可以是 {"question":…}（带上下文时）或裸字符串（兼容旧调用）。"""
        return x["question"] if isinstance(x, dict) else x

    def _prepare(x):
        """★ 顺序必须**显式**（§39.35 的坑）：

        先检索（`reranked` 会把改写关键词写进 `trace["queries"]`），
        再据此构造回答侧提示 —— 所以这里用 `RunnableLambda` 串起来，
        **不能**写回 `{"context": _retrieve, "ask_hint": _hint}` 那种 dict 形式：
        LCEL 会把它当成 `RunnableParallel` **并发**求值，`ask_hint` 可能在
        `trace["queries"]` 被写入**之前**就算完了（时而有时而无，最难查的那种）。
        """
        q = _question(x)
        # ★ 检索用的仍是**用户的原始提问**（不是改写结果）——
        #   §35.15 实测：L300 用 original_query 在每个截断档上都最好。
        #   而 rewrite 自己会拿到 context（见 reranked → rewrite(original_query, context)）。
        docs = reranked(q, user_id, context=context, trace=trace)
        return {
            "context": docs,
            "question": q,
            "ask_hint": build_ask_hint(trace, context, q),
            # ★ 权威当前日期（§39.38.16）：**每次请求现算**，与检索结果无关，所以放在哪都行；
            #   放在这里只是与 context/ask_hint 一起凑齐 prompt 的全部槽位（少一个槽就 KeyError）。
            "today": _today_line(),
            # ★ 联网搜索（§39.38）：放在**检索之后**跑（`_prepare` 本身就是显式顺序，
            #   见上面那段"LCEL 会并发求值"的坑）—— 它顺带把 trace["web"] 写好，
            #   而来源帧正是在第一个文本分片发出**之前**从 trace 取的（rag_response 的约定）。
            "web_block": _web_block(q, trace, web),
        }

    return (
        RunnableLambda(_prepare)
        | rag_prompt_web          # ★ 多一个 {web_block} 槽（空时与 rag_template_multi 逐字相同）
        | answer_model
        | StrOutputParser()
    )

# 非 RAG 的普通对话链（无参考资料）
direct_template = """你是一个 AI 助手。回答必须【完整】且【简洁】：
{today}
把用户问题涉及的要点全部答到，但不写套话、不重复问题、不做无关发挥。

★ 本链用于与奖学金/综测【无关】的问题，篇幅控制在 200 字以内。
  注意是"在 200 字内尽量说全"，不是"为了短而砍掉要点" ——
  字数是上限而非目标；信息量与字数冲突时优先保留信息。
【用户问题】
{question}
【你的回答】："""

direct_prompt = ChatPromptTemplate.from_template(direct_template)
# ★ §39.38.16 起**生产路径不再使用**这条单例链（它没有 {today} 槽 ⇒ 喂不出权威日期）。
#   保留名字只为兼容外部引用；生产一律走 `build_direct_chain()`（每请求新建、带日期）。
#   想 invoke 单例的话得自己补 today，例如直答测试装置里的 `direct_prompt.format(..., today="")`。
direct_chain = direct_prompt | model1 | StrOutputParser()

# ★ 带上文的直答模板（§39.36）：与 rag_template_multi **同一手法** ——
#   从 direct_template **机械插入**一个 {history} 槽（单一事实来源，防两份模板日后漂移），
#   槽位紧贴【用户问题】之前。
#   ★ 空历史时渲染结果与旧模板**逐字相同** ⇒ 单轮闲聊的行为一字不变（由装置钉住）。
#   ★ 为什么闲聊也要历史：用户实测（msg109/msg113）——先说「我叫张三」，
#     隔一轮问「我的名字是什么」，旧行为答「我无法获取您的个人信息」（msg114）。
#     直答链此前**一个字的历史都没有**（它当初只被当作"无关问题的短答"）。
DIRECT_HIST_SLOT = "{history}"
direct_template_hist = direct_template.replace("【用户问题】", DIRECT_HIST_SLOT + "【用户问题】", 1)
direct_prompt_hist = ChatPromptTemplate.from_template(direct_template_hist)

# ★ 联网搜索块（§39.38）：**闲聊链也要接**。
#   为什么：用户开了「联网搜索」却因为"这条被判为闲聊"而没搜，就是"点了没反应"——
#   正是本项目最想消灭的静默失效（§39.29）。接了之后，无论走 RAG 还是直答，都是"照做或如实说没搜到"。
#   空值 ⇒ 渲染与 direct_template_hist 逐字相同。
_DIRECT_WEB_ANCHOR = DIRECT_HIST_SLOT + "【用户问题】"
direct_template_web = direct_template_hist.replace(_DIRECT_WEB_ANCHOR, WEB_SLOT + _DIRECT_WEB_ANCHOR, 1)
direct_prompt_web = ChatPromptTemplate.from_template(direct_template_web)

# 历史块的使用规则。★ 刻意放在【块里面】而不是模板静态文本里：
#   这样"没有历史"的请求，prompt 里连这些规则都不会出现 ⇒ 单轮逐字不变成立。
_DIRECT_HIST_RULE = (
    "【上文对话使用规则】下面是你与用户本次会话此前的对话，只用于理解指代、延续话题：\n"
    "  · **不要主动复述**它，也不要把用户以前说过的话当成这一次说的；\n"
    "  · 用户问的事若不在下面，就如实说「这次对话里看不到」，"
    "不要声称自己无法获取用户信息（你有对话记忆，只是窗口有限）。\n"
)


def direct_history_block(context, trace=None, question=None) -> str:
    """直答链要用的历史块；**无历史时返回 ""**（⇒ 渲染与旧模板逐字相同）。

    ★ 复用与 classify / rewrite **同一份** `ConversationContext.render()`
      （同一套上限：`DEFAULT_MAX_TURNS=3` / 用户 500 / 助手 2000）——
      不新增第二个"上下文来源"，口径天然一致，且**不会被无界增长拖慢**
      （3 轮已饱和是实测结论，见 conversation_context 模块 docstring）。
    ★ 零额外 LLM 调用：只是把已经存在的文本塞进 prompt。
    ★ 认不出上下文类型 / 渲染失败 ⇒ 一律返回 ""（保守：宁可少改，不要瞎猜）。
    """
    if context is None:
        return ""
    # ★ `ConversationContext.is_empty` 是 @property（不是方法）—— 两种形状都支持
    #   （§39.35.3 我在这里踩过一次：写成 callable() 判断会让功能静默失效）。
    is_empty = getattr(context, "is_empty", None)
    if is_empty is None:
        return ""
    try:
        empty = is_empty() if callable(is_empty) else bool(is_empty)
    except Exception:                                    # noqa: BLE001
        return ""
    if empty:
        return ""
    # ★ 生产路径上【当前这条提问】本就在上下文里（_pair_turns 刻意保留
    #   “无助手的最后一轮”，供 classify/rewrite 用）⇒ 若照样塞进直答的
    #   【上文对话】，就与【用户问题】重复一遍，单轮也会“看起来有历史”。
    #   这里把“就是当前问句、且还没有回答”的那一轮去掉。
    turns = list(getattr(context, "turns", []) or [])
    last = turns[-1] if turns else None
    if (last is not None and question is not None
            and not (last.assistant or "").strip()
            and last.user.strip() == str(question).strip()):
        turns = turns[:-1]
    if not turns:
        return ""
    try:
        hist = ConversationContext(
            user_id=getattr(context, "user_id", None), turns=tuple(turns)).render()
    except Exception:                                    # noqa: BLE001
        return ""
    if not hist:
        return ""
    if isinstance(trace, dict):
        # ★ 可断言：e2e/前端能核"到底喂了多少历史"。
        #   刻意**不设** `attempted` —— /chat 只在 trace["attempted"] 为真时才发 sources 帧，
        #   直答分支必须保持"不发帧"（前端据此区分"没检索"与"检索了没命中"）。
        try:
            trace["direct_history_turns"] = len(turns)
        except Exception:                                # noqa: BLE001
            trace["direct_history_turns"] = None
        trace["direct_history_chars"] = len(hist)
    return _DIRECT_HIST_RULE + "【上文对话】:\n%s\n\n" % hist


def build_direct_chain(context=None, trace=None, question=None, web=False):
    """直答链：**每条请求都新建**（带上文、带联网块、带权威当前日期）。

    ★ §39.38.16 起**不再返回单例** `direct_chain`：prompt 里多了 {today} 槽，而日期必须
      **每次请求现算**。用模块级单例 prompt 装日期只有两条路，两条都不能走：
        · 写死日期 ⇒ 每天要人改（这正是要修掉的病）；
        · 请求里改全局对象 ⇒ 并发串档（本项目明令禁止的做法）。
      ⇒ 代价是"对象同一性"那条老断言作废 —— 已由装置改成断言**"日期是当次算的"**
        （`verify_today_line.py` ⑤，用假时钟注入可证），不是删了了事。
    ★ 绝不用模块级单例存历史/日期：那会让并发请求互相串档
      （与 `ConversationContext` 的"每请求一份、绝不做进程级全局"是同一条硬约束）。
    ★ 联网搜索（§39.38）：`web=True` 时多一个 {web_block} 槽。搜索在这里（建链时）就跑完：
      它只影响"本条回答能用哪些资料"，与历史块同理。
    """
    block = direct_history_block(context, trace, question)
    web_block = _web_block(question, trace, web)

    def _prepare(x):
        return {"question": x["question"] if isinstance(x, dict) else x,
                "history": block,
                "web_block": web_block,
                # ★ 权威当前日期（§39.38.16）：每次请求现算 —— 闲聊路径也会被问"今天几号"，
                #   而实测那条路的回答是「我无法直接获取实时日期」（msg142/144）。
                "today": _today_line()}

    return RunnableLambda(_prepare) | direct_prompt_web | model1 | StrOutputParser()


# ============ 核心：根据分类结果路由 ============

def route(question: str, user_id: int, context=None, trace=None, force_rag=False, web=False):
    """按用户路由 —— user_id 必填，理由同 reranked()。

    context：可选对话上下文（`conversation_context.ConversationContext`）。
      ★ 两个分支都要传：classify 判定需要它（多轮追问 4/7 → 0/7 误判），
        RAG 分支的 rewrite 也需要它（指代型改写 19% → 86%）。
      ★ `context.user_id == user_id` **已下沉为结构性校验**（§36.2 第 5 项）：
        本函数与 `reranked()` 入口都会走 `_assert_context_owner()`，
        不属于该用户的 context **直接 ValueError**，不再只靠调用方自觉 ——
        因为 `classify()` 与 `rewrite()` 都会把 `context.render()`
        （**对话历史原文**）发进 LLM prompt。
        `ConversationContext.load()` 的归属校验（失败→空上下文）仍是第一道；
        本校验针对的是【误传了别人的 context 对象】这一类错误。

    trace：可选 dict，透传给 reranked()，用于把检索来源经 SSE 推给前端。
      direct 分支不会检索，因此**不写检索字段** —— 前端据此区分"没检索"与"检索了但没命中"。
      ★ 但第十五轮起 direct 分支会写 `direct_history_turns/chars`（§39.36）：
        它记的是"给直答链喂了多少历史"，不是检索结果；`attempted` 仍然不会被设，
        所以 `attempted=False` 那个"没检索"信号不受影响。

    force_rag：用户点了前端「知识库搜索」按钮 ⇒ **覆盖 classify 的结论**，本条强制走 RAG。
      ★ 刻意**连 classify 都不调**：结论既然已被覆盖，那一次 LLM 调用纯属浪费
        （所以这条路比自动判断还便宜，而不是多花一次）。
      ★ 代价：库里没有相关内容时会给不出依据 —— 但那是**可见**的失败
        （前端显示「未检索到相关资料」，§39.29 已让"没检索"与"没数据"可分辨），
        比"静默地不查、还照答"安全得多（§39.27.3 的取舍）。
      ★ 不改变任何全局状态：只影响本次调用（用户拍板的形态）。

    web：用户点了前端「联网搜索」按钮 ⇒ 本条去公开网页搜一次，把结果作为**另一类资料**
      交给回答模型（见 `_web_block()` 与 `websearch.render_web_block()` 的使用规则）。
      ★ 与 force_rag **相互独立**：只联网、只查库、两个都要，三种组合都支持。
      ★ 两个分支都接（RAG 与直答）—— 否则会出现"开了联网搜索却因为走了闲聊而没搜"
        这种**点了没反应**的静默失效（§39.29 的教训）。
      ★ 结果写进 `trace["web"]`（随 sources 帧到前端）；**不碰 `attempted`** ——
        那是"有没有查知识库"的信号，两者必须可分辨（前端也因此能把两类来源分开显示）。
    """
    _assert_context_owner(context, user_id, "route()")
    if force_rag:
        print("[路由] 用户点了「知识库搜索」，本条强制走 RAG 检索（已跳过 classify）")
        return build_rag_chain(user_id, context, trace, web=web)
    if classify(question, context):
        print("[路由] 问题与奖学金/综测相关，使用 RAG 检索回答")
        return build_rag_chain(user_id, context, trace, web=web)
    else:
        print("[路由] 问题与奖学金/综测无关，直接回答")
        # ★ 有历史 ⇒ 走【带上文】的直答链（§39.36）；无历史 ⇒ 原样返回单例 `direct_chain`。
        #   两条都经 build_direct_chain，避免"忘了传 context"这类静默缺口。
        return build_direct_chain(context, trace, question, web=web)


if __name__ == "__main__":
    # 交互调试用：python -m langchain_project <user_id> [conv_id]
    # 真实服务里 user_id / conv_id 必须来自鉴权后的 current_user 与已校验归属的会话，
    # 绝不能由用户/LLM 传入（conv_id 传错 = 串档风险，见 conversation_context.py）。
    if len(sys.argv) < 2:
        print("用法: python -m langchain_project <user_id> [conv_id]")
        raise SystemExit(2)
    demo_user_id = int(sys.argv[1])
    demo_conv_id = int(sys.argv[2]) if len(sys.argv) > 2 else None
    demo_ctx = ConversationContext.load(demo_conv_id, demo_user_id)
    print("[调试] 上下文：%d 轮%s" % (len(demo_ctx), "" if demo_conv_id else "（未指定 conv_id）"))
    while True:
        user_input = input("用户：")
        if not user_input:
            break
        else:
            try:
                selected_chain = route(user_input, demo_user_id, demo_ctx)
                # ★ RAG 链现在期望 dict 输入（好把 context 传进去）；
                #   direct_chain 仍接受裸字符串，两种都给同样能跑。
                for chunk in selected_chain.stream({"question": user_input}):
                    print(chunk, end="", flush=True)
                print()
            except Exception as e:
                print(e)

