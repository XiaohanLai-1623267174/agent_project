"""Persistence layer for the knowledge-ingestion skill."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_core.documents import Document

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:  # 单文件直跑（PyCharm run config，cwd 是包目录）时兜底
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")  # 显式指向项目根 .env，不受 cwd 影响

from config import CHROMA_DIRECTORY, COLLECTION_NAME  # 向量库路径的唯一来源，见 config.py
from database import KnowledgeChunk, KnowledgeDocument, Session, engine, init_db


EMBED_BATCH_SIZE = 20  # qwen3.7-text-embedding 单次 API 硬上限 20 条，超过会 400

_VECTOR_STORE: Chroma | None = None


def _vector_store() -> Chroma:
    """模块级单例：每次管线运行只建一次 PersistentClient 与 embedding 客户端。"""
    global _VECTOR_STORE
    if _VECTOR_STORE is None:
        _VECTOR_STORE = Chroma(
            persist_directory=str(CHROMA_DIRECTORY),
            embedding_function=DashScopeEmbeddings(
                model="qwen3.7-text-embedding",
                dashscope_api_key=os.getenv("QW_API_KEY"),
            ),
            collection_name=COLLECTION_NAME,
        )
    return _VECTOR_STORE


def _count_vectors(document_id: int) -> int:
    """该文档当前在 Chroma 里的向量条数（只用于回报与断言，不参与业务判断）。"""
    raw = _vector_store().get(where={"document_id": document_id})
    return len(raw.get("ids") or [])


def _delete_vectors(document_id: int) -> None:
    """删掉该文档在 Chroma 里的全部向量。

    ★ 全项目【唯一】一处按 document_id 删向量的地方：入库重跑的清理与
      delete_document 都调它，避免两处 where 条件各自演化而留下残留向量。
    """
    _vector_store().delete(where={"document_id": document_id})


def _invalidate_bm25_after_change(user_id: int, *, was_shared: bool) -> str | None:
    """文档集合变化后让 BM25 缓存失效；返回错误字符串（无错则 None）。

    ★ was_shared=True 必须【全量】失效：公共库文档在每个用户的语料里，
      只失效属主缓存的话，其他人仍会用旧语料召回这份已删除的文档。
    失败只回报警告、不抛异常 —— 缓存失效不该把一次已成功的删除变成失败。
    """
    try:
        from langchain_project import invalidate_all_bm25, invalidate_bm25
    except Exception as exc:  # noqa: BLE001
        return f"导入失效函数失败（{type(exc).__name__}: {exc}）"
    try:
        if was_shared:
            invalidate_all_bm25()
        else:
            invalidate_bm25(user_id)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _json(value: object) -> str:
    """安全地将对象/Pydantic模型转为 JSON 字符串。"""
    if isinstance(value, list):
        value = [v.model_dump() if hasattr(v, "model_dump") else v for v in value]
    elif hasattr(value, "model_dump"):
        value = value.model_dump()
    return json.dumps(value if value is not None else [], ensure_ascii=False)


def save_chunks(
    *, pdf_path: str | Path, title: str, chunks: list[dict[str, Any]],
    user_id: int, db: Session | None = None, dedupe: bool = True
) -> tuple[int, int]:
    """Store one ingested document in SQL and its searchable representations in Chroma.

    user_id 必填：文档归属是它之后能被检索到的前提
    （langchain_project.user_doc_ids 只返回该用户名下的 document_id）。
    """
    if not chunks:
        raise ValueError("没有可写入的知识块")

    init_db()
    owns_session = db is None
    session = db if db is not None else Session(engine)

    document: KnowledgeDocument | None = None
    try:
        # 幂等：同一 PDF 重跑时先清掉旧记录与旧向量，避免重复入库（配合断点缓存重跑）
        # ⚠️ 必须带 user_id 条件：否则别的用户上传了相同 file_path 的 PDF 时，
        #    这一次入库会把【别人的】文档连同向量一起删掉（跨用户破坏）。
        resolved_path = str(Path(pdf_path).resolve())
        # dedupe=True（默认）：同一路径重跑先清旧行，保证幂等（断点缓存重跑不会堆重复）。
        # dedupe=False：显式允许「同一路径并存多份」——用户选了「给自由度」，
        #   所以必须有一个真能留下两份同路径文档的入口，否则 force=true 也只是白叫。
        if dedupe:
            old_docs = session.query(KnowledgeDocument).filter(
                KnowledgeDocument.file_path == resolved_path,
                KnowledgeDocument.user_id == user_id,
            ).all()
            for old in old_docs:
                _delete_vectors(old.id)   # 与 delete_document 共用同一处 Chroma 删除逻辑
                session.delete(old)       # cascade 删旧 chunks
            session.commit()

        document = KnowledgeDocument(
            user_id=user_id,
            filename=Path(pdf_path).name,
            file_path=resolved_path,
            title=title,
            status="processing",
        )
        session.add(document)
        session.flush()

        records: list[KnowledgeChunk] = []
        vector_documents: list[Document] = []
        vector_ids: list[str] = []

        for chunk in chunks:
            meta = chunk["metadata"]
            section_path = chunk["section_path"]
            breadcrumb = " -> ".join(section_path)

            # 1. 构建 SQL 记录
            records.append(
                KnowledgeChunk(
                    document_id=document.id,
                    chunk_id=chunk["chunk_id"],
                    content=chunk["content"],
                    module=meta.get("module"),
                    sub_module=meta.get("sub_module"),
                    score_type=meta.get("score_type"),
                    base_score=meta.get("base_score"),
                    section_path=_json(section_path),
                    score_rules=_json(meta.get("score_rules", [])),
                    keywords=_json(meta.get("keywords", [])),
                )
            )

            # 2. 格式化提取用于 Chroma 过滤的轻量级字符串
            target_audience = meta.get("target_audience", [])
            keywords = meta.get("keywords", [])
            audience_str = "、".join(target_audience) if isinstance(target_audience, list) else str(target_audience or "")
            keywords_str = "、".join(keywords) if isinstance(keywords, list) else str(keywords or "")

            # 3. 优化嵌入文本 (把路径与适用人群显式作为向量上下文)
            page_content = (
                f"【文档路径】: {breadcrumb}\n"
                f"【适用对象】: {audience_str if audience_str else '通用'}\n"
                f"【规则类别】: {meta.get('score_type') or '普通条款'}\n"
                f"【正文内容】:\n{chunk['content']}"
            )

            # 4. 构建 Chroma Document
            vector_documents.append(
                Document(
                    page_content=page_content,
                    metadata={
                        "document_id": document.id,
                        "chunk_id": chunk["chunk_id"],
                        "doc_title": chunk["doc_title"],
                        "section_path": breadcrumb,
                        "module": meta.get("module") or "",
                        "sub_module": meta.get("sub_module") or "",
                        "score_type": meta.get("score_type") or "",
                        "target_audience": audience_str,
                        "keywords": keywords_str,
                    },
                )
            )
            vector_ids.append(f"{document.id}:{chunk['chunk_id']}")

        session.add_all(records)
        session.commit()

        # 写入向量数据库（按 embedding API 硬上限分批，避免整批超 20 条被 400）
        vector_store = _vector_store()
        for start in range(0, len(vector_documents), EMBED_BATCH_SIZE):
            vector_store.add_documents(
                vector_documents[start:start + EMBED_BATCH_SIZE],
                ids=vector_ids[start:start + EMBED_BATCH_SIZE],
            )

        document.status = "completed"
        session.commit()

        # 入库成功 → 必须让该用户的 BM25 缓存失效，否则新文档在 BM25 这一路召回不到。
        # 用 try/except 包住：缓存失效失败不该把一次已经成功的入库变成失败。
        # 注：langchain_project 现在不再在 import 期建全局 BM25，所以这里的延迟导入很便宜。
        try:
            from langchain_project import invalidate_bm25
            invalidate_bm25(user_id)
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] BM25 缓存失效失败（{type(exc).__name__}: {exc}）"
                  f"，新文档可能 BM25 召回不到", flush=True)

        return document.id, len(records)

    except Exception:
        session.rollback()
        if document is not None and document.id is not None:
            failed = session.get(KnowledgeDocument, document.id)
            if failed is not None:
                failed.status = "failed"
                session.commit()
        raise
    finally:
        if owns_session:
            session.close()


def delete_document(document_id: int, *, db: Session | None = None) -> dict[str, Any]:
    """删除一份文档的【全部可检索表示】：Chroma 向量 + SQL 行(级联 chunks) + BM25 缓存。

    ★ 为什么必须收敛成一个函数：这三处漏掉任何一处都会留下"幽灵数据"——
      文档列表里已经没有了，检索却还能命中它，用户会看到来自"不存在的文档"的答案。
      这是最难排查的一类 bug，所以删除路径刻意只有一个入口。

    返回实际删除条数供调用方回报用户；document_id 不存在时 deleted=False。
    归属校验【不】在这里做：本函数是持久层，由调用方（API）先验明归属再调。
    """
    init_db()
    owns_session = db is None
    session = db if db is not None else Session(engine)
    try:
        document = session.get(KnowledgeDocument, document_id)
        if document is None:
            return {"deleted": False, "document_id": document_id, "filename": None,
                    "chunks_deleted": 0, "vectors_deleted": 0, "vectors_left": 0,
                    "bm25_warning": None}

        filename = document.filename
        owner_id = document.user_id
        was_shared = bool(document.is_shared)

        chunks_deleted = session.query(KnowledgeChunk).filter(
            KnowledgeChunk.document_id == document_id).count()
        try:
            vectors_deleted = _count_vectors(document_id)
        except Exception:  # noqa: BLE001 — 数不出来不该阻断删除
            vectors_deleted = -1

        # ① 必须先删向量、再删 SQL 行：SQL 行是定位残留向量的唯一线索，
        #    一旦先删了它，就再也无法按 document_id 清理 Chroma 了。
        #    反过来的风险（向量删了、commit 失败）只是"文档还在但检不到"，
        #    用户看得见、重试即可（_delete_vectors 幂等）——两害相权取此。
        _delete_vectors(document_id)

        # ② 删 SQL 行（relationship 配了 cascade="all, delete-orphan"，chunks 自动删）
        session.delete(document)
        session.commit()

        # ③ 让 BM25 缓存失效（公共库文档影响所有用户，见 helper 的注释）
        bm25_warning = _invalidate_bm25_after_change(owner_id, was_shared=was_shared)

        # ④ 完整性断言：残留向量虽然会被 user_doc_ids 那道过滤挡住、召回不到，
        #    但仍属"幽灵数据"，必须如实回报，而不是假装删干净了。
        try:
            vectors_left = _count_vectors(document_id)
        except Exception:  # noqa: BLE001
            vectors_left = -1

        return {"deleted": True, "document_id": document_id, "filename": filename,
                "chunks_deleted": chunks_deleted, "vectors_deleted": vectors_deleted,
                "vectors_left": vectors_left, "bm25_warning": bm25_warning}
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
