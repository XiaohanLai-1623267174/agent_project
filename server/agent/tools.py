"""Tools exposed to the LangChain agent（kb_bench 沙箱副本）。

改动 1：顶部加 sys.path 兜底，使本文件被"直接运行"时也能找到 skills 包，
解决原本的 `ModuleNotFoundError: No module named 'skills'`。

改动 2（用户隔离）：`knowledge_ingestion` 不再是模块级单例工具，改成
`make_knowledge_ingestion_tool(user_id)` 工厂 —— user_id 由调用方闭包注入，
不进入工具的 JSON Schema，避免 LLM 指定任意 user_id 往别人库里写文档。

改动 3（路径越权修复）：只闭包 user_id 还不够 —— pdf_path 仍然是工具参数，
由 LLM（也就是由提问的用户）提供。实测（verify_agent_path_escape.py）：
  用户B 只要把 pdf_path 指向【用户A 的上传目录】，就能把 A 的文件入库到 B 自己的
  知识库，随后用 /chat 读出来 = 跨用户文件读取。工具返回 success=True，无任何阻拦。
⇒ 现在加了 allowed_root 白名单：pdf_path resolve 之后必须落在该目录内，
  否则直接返回失败。默认（fail-closed）就是"该用户自己的上传目录"。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接运行本文件时（python agent/tools.py），sys.path[0] 是 agent 目录，项目根不在里面
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langchain.tools import tool

from config import user_upload_root
from skills.knowledge_ingestion import ingest_pdf_to_knowledge_base


def make_knowledge_ingestion_tool(user_id: int, allowed_root: str | Path | None = None):
    """按用户构造入库工具：把 user_id 【闭包】进工具，而不是做成工具参数。

    为什么不能把 user_id 做成 @tool 的参数：那样它就出现在工具的 JSON Schema 里，
    LLM（或任何能构造工具参数的调用方）就能指定任意 user_id
    → 等于允许把文档写进别人的知识库。用户隔离必须在工具边界之外完成。

    allowed_root：允许导入的目录白名单。默认 = 该用户自己的上传目录（fail-closed）。
    想放开（例如单机自用、允许导入任意本机路径）必须【显式】传一个更宽的根，
    这样"放宽"永远是一个看得见的决定，而不是忘传参数导致的默认行为。

    调用方（FastAPI 的 /agent 路由）负责传入鉴权后的 current_user.id。
    """
    root = Path(allowed_root).resolve() if allowed_root is not None else user_upload_root(user_id).resolve()

    @tool("knowledge_ingestion")
    def knowledge_ingestion(pdf_path: str, title: str | None = None) -> dict:
        """将一份 PDF 导入奖学金/综测知识库。

        当用户明确要求上传、导入或更新 PDF 知识库时使用。它会自动完成
        PDF 解析、Markdown 清洗、元数据提取，并写入 SQLite 与 Chroma。

        注意：只能导入该用户自己上传目录中的文件；用户自己的电脑路径无法直接读取，
        需要用户先通过上传接口把文件传上来。
        """
        try:
            resolved = Path(pdf_path).resolve()
        except (OSError, ValueError) as exc:
            return {"success": False, "error": f"路径无法解析：{pdf_path}（{exc}）"}

        # 用 resolve() 之后再比较：能同时挡住 ".." 相对穿越和符号链接穿越
        if not resolved.is_relative_to(root):
            return {
                "success": False,
                "error": (
                    f"拒绝导入：只能导入你自己上传目录里的文件（{root}）。"
                    f"请先通过上传接口上传该文件，再让我导入。"
                ),
            }
        if not resolved.is_file():
            return {"success": False, "error": f"文件不存在：{resolved}"}

        return ingest_pdf_to_knowledge_base(
            pdf_path=str(resolved), title=title, user_id=user_id
        )

    return knowledge_ingestion


# ============================ 文件搜索 / 导入（§39.63） ============================
# 为什么加这两个工具：用户不想每次手动上传，希望「说个文件名，它自己找到并入库」。
# 但「全电脑检索」会击穿多租户隔离（能读到别人的上传目录、.env、个人文档），
# 所以这里用【白名单 + 排除清单】，而不是全盘扫描：
#   · 只在显式配置的目录里搜（AGENT_SEARCH_ROOTS，默认三个常见目录）
#   · 项目目录、虚拟环境、node_modules 等一律排除（既是安全也是性能）
#   · 单次最多返回 10 条（防止把一堆同名文件全塞给模型）

# 默认搜索根：只列「常见放文件的地方」，具体机器上不存在的由 search_roots() 过滤掉。
# 为什么不写死 `C:/Users/<用户名>/Desktop`：
#   ① 会把作者路径带进发布物（导出闸「作者路径闸」会挡下，判据是真实用户名）；
#   ② 换台机器就失效 —— 别人的用户名不是这个，等于默认搜索根全废。
# 为什么仍保留 D:/Documents、D:/Downloads：Windows 上把文档放数据盘很常见，
# 本机当前的简历就在 D:\Documents 下，去掉会让 find_pdf 找不到东西（行为回归）。
_DEFAULT_SEARCH_ROOTS = (
    str(Path.home() / "Desktop"),
    str(Path.home() / "Downloads"),
    str(Path.home() / "Documents"),
    "D:/Documents",
    "D:/Downloads",
)

# 排掉：项目自身（含 .env 与用户上传目录）、依赖、缓存。宁可少搜，也不越界。
_DENY_PARTS = {
    "agent_project", "node_modules", ".venv", "venv", "__pycache__",
    ".git", "AppData", "Windows", "Program Files", "Program Files (x86)",
    "site-packages", "$Recycle.Bin", "System Volume Information",
}


def search_roots() -> list[Path]:
    """可搜索的根目录列表。

    `AGENT_SEARCH_ROOTS` 用**分号**分隔（Windows 路径里不含分号，比冒号安全）；
    未配置时用 `_DEFAULT_SEARCH_ROOTS`。存在的目录才会返回 —— 配置写错不会静默无结果，
    而是由 find_pdf 的返回值体现「0 个根目录」。
    """
    import os

    raw = os.getenv("AGENT_SEARCH_ROOTS", "").strip()
    candidates = [p.strip() for p in raw.split(";") if p.strip()] if raw else list(_DEFAULT_SEARCH_ROOTS)
    out: list[Path] = []
    for c in candidates:
        try:
            p = Path(c).resolve()
        except (OSError, ValueError):
            continue
        if p.is_dir():
            out.append(p)
    return out


def _allowed_ingest_roots(user_id: int) -> list[Path]:
    """可导入的根目录 = 搜索白名单 + 该用户自己的上传目录。"""
    roots = search_roots()
    up = user_upload_root(user_id).resolve()
    if all(up != r for r in roots):
        roots.append(up)
    return roots


def _denied(path: Path) -> bool:
    """路径里出现任何一个排除片段就拒绝（大小写不敏感）。"""
    parts = {p.lower() for p in path.parts}
    return any(d.lower() in parts for d in _DENY_PARTS)


def _find_existing(user_id: int, filename: str) -> list[dict]:
    """查该用户名下同名的已完成文档。

    为什么需要：实测同一份 `简历.pdf` 被连续入库两次（doc 14 / 15，内容相同）
    ⇒ 检索会捞到重复切片、稀释结果。用户选择「给自由度」（方案 C）：
    重复时**不自动入库**，把已存在的选项交回给用户决定。
    """
    try:
        from database import KnowledgeDocument, Session, engine
    except Exception:            # 数据库不可用时不要因此挡住入库（宁可少一层保护）
        return []
    session = Session(engine)
    try:
        rows = (
            session.query(KnowledgeDocument)
            .filter(
                KnowledgeDocument.user_id == user_id,
                KnowledgeDocument.filename == filename,
                KnowledgeDocument.status == "completed",
            )
            .all()
        )
        return [
            {
                "document_id": r.id,
                "title": r.title,
                "chunks": len(r.chunks) if getattr(r, "chunks", None) is not None else None,
            }
            for r in rows
        ]
    except Exception:
        return []
    finally:
        session.close()


def _user_documents(user_id: int) -> list[dict]:
    """列出该用户已完成的文档（供 search_knowledge 把文件名解析成 document_id）。"""
    try:
        from database import KnowledgeDocument, Session, engine
    except Exception:
        return []
    session = Session(engine)
    try:
        rows = (session.query(KnowledgeDocument)
                .filter(KnowledgeDocument.user_id == user_id,
                        KnowledgeDocument.status == "completed")
                .all())
        return [{"document_id": r.id, "filename": r.filename or "", "title": r.title or ""}
                for r in rows]
    except Exception:
        return []
    finally:
        session.close()


def make_file_tools(user_id: int, max_calls: int | None = None):
    """构造两个工具的列表：[find_pdf, ingest_pdf]。

    user_id 同样【闭包】进工具而不是做成参数（与 make_knowledge_ingestion_tool 同一原则）。

    max_calls：本次任务的工具调用预算（None = 不限）。为什么放在工具层而不是只在上层计数：
      · 同步入口 `/agent` 走的是 agent.invoke()，中途没法插手中断 —— 只有在工具里拦住才真正有效；
      · 超预算时返回**结构化拒绝**（而不是抛异常），让模型看到后自己收尾作答，同时把
        used/max 报给前端，用户能看出「它没做完是因为预算用完了」，而不是以为它偷懒。
    """
    budget = {"used": 0, "max": max_calls}

    def _spend(name: str) -> dict | None:
        """消费一次预算；超限则返回拒绝体（调用方直接 return 它）。"""
        if budget["max"] is None:
            budget["used"] += 1
            return None
        if budget["used"] >= budget["max"]:
            return {
                "success": False,
                "budget_exhausted": True,
                "used": budget["used"],
                "max": budget["max"],
                "error": "本次任务工具调用已达上限 %d 次（用于控制成本），请立刻基于已有信息作答，"
                         "并明确告诉用户哪些部分没做完。" % budget["max"],
            }
        budget["used"] += 1
        return None


    @tool("find_pdf")
    def find_pdf(name_pattern: str) -> dict:
        """在用户白名单目录里按文件名查找 PDF，返回候选列表（最多 10 条）。

        适用：用户说「把简历导入」「找一下综测办法」这类**只给简称**的指令。
        不适用：需要检索知识库内容（那是别的工具）。

        返回 {"found": n, "candidates": [{path, filename, size_kb, mtime}], "searched_roots": [...]}。
        · found == 0 ⇒ 直接把 searched_roots 告诉用户，说明这些目录里没找到；
        · found == 1 ⇒ 可以直接调用 ingest_pdf(path=那一条)；
        · found >  1 ⇒ **必须**把候选列给用户选，不要自己挑。
        """
        import os

        deny = _spend("find_pdf")
        if deny:
            return deny

        roots = search_roots()
        if not roots:
            return {"found": 0, "candidates": [], "searched_roots": [],
                    "error": "没有可搜索的目录（AGENT_SEARCH_ROOTS 未配置或都不存在）"}

        # 归一化：去扩展名、去空格、小写 —— 让「简历」能命中「简历_2024.pdf」
        needle = name_pattern.strip().lower()
        for ext in (".pdf",):
            if needle.endswith(ext):
                needle = needle[: -len(ext)]
        needle = needle.replace(" ", "")

        hits: list[dict] = []
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
                dp = Path(dirpath)
                if _denied(dp):
                    dirnames[:] = []          # 不进入被排除的目录（安全 + 性能）
                    continue
                # 就地剪枝：再往下也不需要
                dirnames[:] = [d for d in dirnames if not _denied(dp / d)]
                for fn in filenames:
                    if not fn.lower().endswith(".pdf"):
                        continue
                    if needle not in fn.lower().replace(" ", ""):
                        continue
                    try:
                        full = dp / fn
                        st = full.stat()
                    except OSError:
                        continue
                    hits.append({
                        "path": str(full),
                        "filename": fn,
                        "size_kb": round(st.st_size / 1024),
                        "mtime": __import__("datetime").datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
                    if len(hits) >= 10:
                        break
                if len(hits) >= 10:
                    break
            if len(hits) >= 10:
                break

        hits.sort(key=lambda h: h["mtime"], reverse=True)
        return {"found": len(hits), "candidates": hits,
                "searched_roots": [str(r) for r in roots]}

    @tool("search_knowledge")
    def search_knowledge(query: str, filename: str | None = None, k: int = 5) -> dict:
        """在【该用户自己的】知识库里检索（向量 + BM25 + 本地重排），返回可直接引用的原文与来源。

        什么时候用：要给用户一个**有依据**的答案（条款、分数、条件、经历等）。
        什么时候不用：用户只是让你导入文件（那是 find_pdf + ingest_pdf）。

        filename：给了就**只在这一份文档里检索**（定向检索，chat 页做不到）。
        返回 {"success", "count", "context", "sources", "candidates"}：
        · context 是带【n】编号的原文，回答时按编号引用；
        · count == 0 ⇒ 如实说「没检索到相关内容」，**不要编造**；
        · 用户点名的文件不存在 ⇒ 返回 available_filenames 让用户确认，不要换文件凑答案。
        """
        deny = _spend("search_knowledge")
        if deny:
            return deny

        try:
            from langchain_project import reranked
        except Exception as exc:
            return {"success": False, "error": "检索模块不可用：%s: %s" % (type(exc).__name__, exc)}

        only = None
        if filename:
            want = filename.lower().replace(".pdf", "").strip()
            docs = _user_documents(user_id)
            only = [d["document_id"] for d in docs
                    if want and want in d["filename"].lower().replace(".pdf", "")]
            if not only:
                return {
                    "success": False,
                    "count": 0,
                    "error": "知识库里没有叫「%s」的已完成文档。" % filename,
                    "available_filenames": [d["filename"] for d in docs],
                }

        trace: dict = {}
        try:
            context = reranked(query, user_id, top_n=max(1, int(k)), trace=trace,
                               only_doc_ids=only)
        except Exception as exc:
            return {"success": False, "error": "检索失败：%s: %s" % (type(exc).__name__, exc)}

        sources = trace.get("sources") or []
        return {
            "success": True,
            "count": len(sources),
            "context": context,
            "sources": sources,
            "candidates": trace.get("candidates"),
            "scope": ("仅「%s」" % filename) if filename else "全部文档",
            "note": "" if sources else "检索到 0 条：如实告知用户没有相关资料，不要编造。",
        }

    @tool("ingest_pdf")
    def ingest_pdf(path: str, title: str | None = None, force: bool = False) -> dict:
        """把一份 PDF 解析并写入知识库（清洗 → 切片 → 元数据 → SQLite + Chroma）。

        只接受落在白名单目录或该用户上传目录内的 .pdf；其余一律拒绝。
        若 find_pdf 返回多条候选，必须先让用户确认是哪一条再调用本工具。

        重复保护（方案 C，用户选的「给自由度」）：默认 force=false，若该用户已有
        **同名且已完成**的文档，本工具**不会**入库，而是返回 exists=true + 已有清单，
        由用户决定：再导一份（force=true）或先删旧的（在「我的文件」里删）。
        """
        deny = _spend("ingest_pdf")
        if deny:
            return deny

        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError) as exc:
            return {"success": False, "error": f"路径无法解析：{path}（{exc}）"}

        if resolved.suffix.lower() != ".pdf":
            return {"success": False, "error": f"只支持 PDF：{resolved.name}"}
        if _denied(resolved):
            return {"success": False, "error": f"该路径在排除范围内，不可导入：{resolved}"}
        roots = _allowed_ingest_roots(user_id)
        if not any(resolved.is_relative_to(r) for r in roots):
            return {
                "success": False,
                "error": "拒绝导入：只能导入白名单目录或你自己上传目录里的文件。"
                         "可用目录：" + "；".join(str(r) for r in roots),
            }
        if not resolved.is_file():
            return {"success": False, "error": f"文件不存在：{resolved}"}

        if not force:
            existing = _find_existing(user_id, resolved.name)
            if existing:
                return {
                    "success": False,
                    "exists": True,
                    "filename": resolved.name,
                    "existing": existing,
                    "hint": "同名文档已存在，未重复入库。要再导一份请带 force=true；"
                            "想替换请先在「我的文件」里删掉旧的那份。",
                }

        return ingest_pdf_to_knowledge_base(
            pdf_path=str(resolved), title=title, user_id=user_id,
            allow_duplicate=bool(force),
        )

    return [find_pdf, search_knowledge, ingest_pdf]