"""PDF extraction and Markdown-cleaning steps for knowledge ingestion."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pdfplumber
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:  # 单文件直跑（cwd 是本包目录）时也要能 import llm_roles
    sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")  # 显式指向项目根 .env，不受 cwd 影响

from llm_roles import invoke_with_retry, make_model  # noqa: E402 （必须在 sys.path 兜底之后）



CLEAN_PROMPT = """
你是一名专业的中文规章制度文档清洗器。

你的任务是清洗下面由 PDF 转换得到的 纯Markdown/文本，目的是恢复原始文档的结构，使其适合后续 RAG 检索。

【绝对规则】
1. 不得新增原文没有的信息。
2. 不得删除任何有效信息。
3. 不得改变任何数字、分数、百分比、日期、次数、等级、条件。
4. 不得改变“不得”“不”“未”“无”等否定含义。
5. 不得修改原文的政策规定和评分规则。
6. 不得自行解释或总结。
7. 不得把不同规则合并成一个规则。
8. 不得把一个规则拆成失去上下文的碎片。
9. 只修复 PDF 转换造成的错误换行、词语被拆开、标题层级丢失、表格阅读顺序混乱等。
10. 如果无法确定某处原始结构，不要猜测，保留原文并标记：[结构无法确定]
11.如果单元格内包含复杂的条文、矩阵加分或多条件规则，请将其单独提取并还原为标准的 Markdown 多列表格，不要用 <br> 拼接到同一个单元格内。

【结构要求】
请尽可能恢复以下 Markdown 层级：#、##、###、#### 等。
对于 PDF 转换产生的表格，如果能够根据原文明确恢复表格关系，可以转换成 Markdown 表格；如果无法确定，不要猜测。

【输出要求】
只输出清洗后的 Markdown。不要输出解释。不要输出“以下是清洗结果”等额外文字。

原始输入数据：
{raw_text}
"""


def _clean_cell(value: object | None) -> str:
    return " ".join(str(value or "").replace("\n", " ").split())


def extract_pdf_content(pdf_path: str | Path) -> str:
    """Extract page text and table rows into a loss-minimising plain-text representation."""
    parts: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            parts.append(f"\n========== 第 {page_number} 页 ==========\n")
            text = (page.extract_text() or "").strip()
            if text:
                parts.extend(("【页面正文提取】", text))

            for table_number, table in enumerate(page.extract_tables(), start=1):
                rows = [
                    [_clean_cell(cell) for cell in row]
                    for row in table
                    if row and any(_clean_cell(cell) for cell in row)
                ]
                if not rows:
                    continue
                parts.append(f"【表格 {table_number}】")
                # Preserve cells explicitly so the model does not mistake columns for paragraphs.
                parts.extend(" | ".join(row) for row in rows)
    return "\n".join(parts).strip()


def _markdown_llm():
    """PDF→Markdown 清洗模型 —— 走【角色表】，不再硬编码。

    model / base_url / api_key 三者的唯一来源是 `config.ROLE_MODELS`（角色 "clean"）。
    历史：这里原先是硬编码的百炼 `qwen3.8-flash` + `QW_API_KEY`。该模型免费额度
    耗尽（403 AllocationQuota.FreeTierOnly）后，整条上传链路就断了 —— 而 chat 侧
    早已迁魔搭，于是长期表现为"聊天一切正常、只有上传 400"这个很难归因的组合。
    改用角色后，换后端只需改 `config.py` 一行，业务代码不动（§19/§21.3 手法）。
    """
    model, _cfg = make_model("clean", temperature=0.1, timeout=(10, 300),
                             max_retries=2)
    return model


def _split_for_cleaning(raw_content: str, max_chars: int = 24_000) -> list[str]:
    """Keep long PDFs within a model request while preferring page boundaries."""
    pages = raw_content.split("========== 第 ")
    groups: list[str] = []
    current = ""
    for page in pages:
        candidate = page if not current else f"{current}\n========== 第 {page}"
        if current and len(candidate) > max_chars:
            groups.append(current)
            current = f"========== 第 {page}"
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


def pdf_to_markdown(pdf_path: str | Path) -> str:
    """Convert one PDF to cleaned Markdown; raises an error instead of silently creating bad data."""
    path = Path(pdf_path).expanduser().resolve()
    if path.suffix.lower() != ".pdf":
        raise ValueError("只支持 PDF 文件")
    if not path.is_file():
        raise FileNotFoundError(f"找不到 PDF 文件：{path}")

    t0 = time.perf_counter()
    raw_content = extract_pdf_content(path)
    if not raw_content:
        raise ValueError("PDF 中未提取到可处理的文本或表格")
    print(f"[1/4] PDF 抽取完成：{len(raw_content)} 字符，耗时 {time.perf_counter() - t0:.1f}s", flush=True)

    llm = _markdown_llm()
    cleaned_parts: list[str] = []
    parts = _split_for_cleaning(raw_content)
    for index, part in enumerate(parts, start=1):
        t = time.perf_counter()
        # 用 invoke_with_retry 而不是裸 invoke：魔搭免费额度是【按模型限流】的
        # （顺序连打会 429，§30 实测），它同时负责 429 退避与"额度耗尽时换 key"
        # （池 = MS_API_KEY / MS_API_KEY_2 / …，由 llm_roles 从环境变量自动展开）。
        result, _attempts = invoke_with_retry(
            llm, CLEAN_PROMPT.format(raw_text=part), role="clean")
        cleaned = str(result.content).strip()
        if not cleaned:
            raise RuntimeError("Markdown 清洗模型未返回内容")
        cleaned_parts.append(cleaned)
        print(
            f"[2/4] 清洗分片 {index}/{len(parts)}：输出 {len(cleaned)} 字符，"
            f"耗时 {time.perf_counter() - t:.1f}s",
            flush=True,
        )
    markdown = "\n\n".join(cleaned_parts)
    if not markdown:
        raise RuntimeError("PDF 清洗后没有可用的 Markdown 内容")
    return markdown
