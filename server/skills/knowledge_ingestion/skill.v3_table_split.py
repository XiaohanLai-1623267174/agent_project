"""Public entry point for ingesting a PDF into the scholarship knowledge base."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .json_to_db import PROJECT_ROOT, save_chunks
from .md_to_json import markdown_to_chunks
from .pdf_to_md import pdf_to_markdown


GENERATED_MARKDOWN_DIRECTORY = PROJECT_ROOT / "knowledge_markdown"
_GENERATOR_VERSION = 3  # 清洗 prompt 或 chunk 结构变更时手动 +1，使旧缓存失效


def _cache_path(source: Path) -> Path:
    return GENERATED_MARKDOWN_DIRECTORY / f"{source.stem}_chunks.json"


def _load_cached_chunks(source: Path) -> list[dict[str, object]] | None:
    """命中则返回缓存的 chunks；PDF 内容/时间戳变化、版本不符或缓存损坏时返回 None。"""
    cache_path = _cache_path(source)
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    st = source.stat()
    if (
        data.get("pdf_size_bytes") != st.st_size
        or data.get("pdf_mtime_ns") != st.st_mtime_ns
        or data.get("generator_version") != _GENERATOR_VERSION
    ):
        return None
    return data.get("chunks")


def _write_cached_chunks(
    source: Path, chunks: list[dict[str, object]], markdown_path: Path
) -> None:
    st = source.stat()
    payload = {
        "source_pdf": str(source),
        "pdf_size_bytes": st.st_size,
        "pdf_mtime_ns": st.st_mtime_ns,
        "generator_version": _GENERATOR_VERSION,
        "cleaned_markdown_path": str(markdown_path),
        "chunks": chunks,
    }
    _cache_path(source).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ingest_pdf_to_knowledge_base(pdf_path: str, title: str | None = None) -> dict[str, object]:
    """Run PDF → cleaned Markdown → metadata chunks → SQLite + Chroma.

    This is deliberately the only business entry point that the Agent should expose.
    """
    source = Path(pdf_path).expanduser().resolve()
    GENERATED_MARKDOWN_DIRECTORY.mkdir(parents=True, exist_ok=True)
    markdown_path = GENERATED_MARKDOWN_DIRECTORY / f"{source.stem}_cleaned.md"

    chunks = _load_cached_chunks(source)
    if chunks is None:
        t0 = time.perf_counter()
        markdown = pdf_to_markdown(source)
        print(f"PDF 清洗总耗时 {time.perf_counter() - t0:.1f}s", flush=True)
        markdown_path.write_text(markdown, encoding="utf-8")

        t0 = time.perf_counter()
        chunks = markdown_to_chunks(markdown)
        print(f"chunk 拆分与元数据提取总耗时 {time.perf_counter() - t0:.1f}s", flush=True)
        _write_cached_chunks(source, chunks, markdown_path)
        print(f"[4/4] chunks 缓存已写入 {_cache_path(source)}", flush=True)
    else:
        print(f"命中 chunks 缓存（{_cache_path(source)}），跳过清洗与元数据提取", flush=True)

    document_title = title or (chunks[0]["doc_title"] if chunks else source.stem)
    t0 = time.perf_counter()
    document_id, chunk_count = save_chunks(
        pdf_path=source,
        title=str(document_title),
        chunks=chunks,
    )
    print(f"入库（SQLite + Chroma）耗时 {time.perf_counter() - t0:.1f}s", flush=True)
    return {
        "success": True,
        "document_id": document_id,
        "title": document_title,
        "chunk_count": chunk_count,
        "markdown_path": str(markdown_path),
    }
