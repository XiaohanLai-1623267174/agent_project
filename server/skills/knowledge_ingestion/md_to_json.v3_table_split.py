"""Turn cleaned Markdown into metadata-enriched knowledge chunks."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, BeforeValidator, Field, ValidationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")  # 显式指向项目根 .env，不受 cwd 影响

_EMPTY_PLACEHOLDERS = {"", "无", "None", "null", "-", "—", "N/A"}


def _normalize_empty(value: Any) -> Any:
    """LLM 常把缺失字段输出为 '' 或占位文本；归一为 None 再校验。"""
    if isinstance(value, str) and value.strip() in _EMPTY_PLACEHOLDERS:
        return None
    return value


def _normalize_list(value: Any) -> Any:
    """LLM 对列表字段常输出 ''（应为 []）或单个值字符串；宽容归一。"""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped in _EMPTY_PLACEHOLDERS:
            return []
        return [stripped]
    return value


OptionalFloat = Annotated[float | None, BeforeValidator(_normalize_empty)]
OptionalStr = Annotated[str | None, BeforeValidator(_normalize_empty)]
StringList = Annotated[list[str], BeforeValidator(_normalize_list)]

# 枚举约束：把「怎么拆 / 拆多细」钉死，消除模型自由发挥导致的漂移与耗时波动
RuleType = Literal["基础考核", "加分项", "扣分项", "一票否决项"]
ScoreType = Literal["基本分", "加分项", "扣分项", "通用说明"]
OptionalScoreType = Annotated[ScoreType | None, BeforeValidator(_normalize_empty)]


# 1. 结构化评分规则模型（应对规则 11 和条件约束）
class ScoreRuleDetail(BaseModel):
    rule_type: RuleType = Field(description="规则类型，只能是以下之一：基础考核、加分项、扣分项、一票否决项")
    condition: str = Field(description="触发该规则的前提条件或适用情形（需忠实保留未/不/不得等否定词）")
    score_change: OptionalFloat = Field(default=None, description="具体的加减分数值（如 +2.0, -5.0），无则为 None")
    max_score_limit: OptionalFloat = Field(default=None, description="该项规则的最高封顶或最深扣分上限")


# 2. 增强版的 Chunk 元数据模型
class ChunkMetadata(BaseModel):
    module: OptionalStr = Field(default=None, description="所属一级业务模块（如：干部考核、薪酬管理）")
    sub_module: OptionalStr = Field(default=None, description="所属二级业务模块/章节标题")
    target_audience: StringList = Field(default_factory=list, description="条款适用的对象群体（如：中层干部、普通员工）")
    score_type: OptionalScoreType = Field(default=None, description="条款类别，只能是以下之一：基本分、加分项、扣分项、通用说明")
    base_score: OptionalFloat = Field(default=None, description="该条款的基础分或基准满分，无则为空")
    score_rules: Annotated[list[ScoreRuleDetail], BeforeValidator(_normalize_list)] = Field(
        default_factory=list, description="拆解出的详细评分规则列表"
    )
    keywords: StringList = Field(default_factory=list, description="3 至 5 个高频检索关键词或业务别名")


_STRUCTURED_LLM = None


def _structured_llm():
    """模块级懒加载单例：每次管线运行只建一次客户端与结构化 runnable。"""
    global _STRUCTURED_LLM
    if _STRUCTURED_LLM is None:
        llm = ChatOpenAI(
            model="qwen3.8-flash",
            api_key=os.getenv("QW_API_KEY"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            temperature=0.0,
            timeout=(10, 300),  # (连接超时, 读取超时)
            max_retries=2,
            extra_body={"enable_thinking": False},  # 关闭 qwen3.8 默认思考模式
        )
        # 显式 function_calling：qwen3.8 官方确认支持 tools/tool_choice；
        # 1.3.5 默认 json_schema 会发 DashScope 未验证支持的 response_format。
        structured = llm.with_structured_output(ChunkMetadata, method="function_calling")
        # 模型偶尔产出不符合 schema 的值：校验失败时整个调用自动重试一次
        _STRUCTURED_LLM = structured.with_retry(
            stop_after_attempt=2,
            retry_if_exception_type=(ValidationError,),
        )
    return _STRUCTURED_LLM


def split_markdown_by_headings(markdown: str) -> list[dict[str, str]]:
    """Split on heading boundaries while retaining the title path for every chunk."""
    title = "未命名文档"
    headings: dict[int, str] = {}
    content: list[str] = []
    chunks: list[dict[str, str]] = []

    def flush() -> None:
        text = "\n".join(content).strip()
        if not text:
            return
        path = [title] + [headings[level] for level in sorted(headings)]
        chunks.append({"doc_title": title, "section_path": path, "content": text})

    for line in markdown.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            marker, separator, heading = stripped.partition(" ")
            level = len(marker)
            if separator and 1 <= level <= 6:
                flush()
                content = []
                if level == 1:
                    title = heading.strip() or title
                    headings.clear()
                else:
                    headings[level] = heading.strip()
                    for stale_level in [key for key in headings if key > level]:
                        del headings[stale_level]
                continue
        content.append(line)
    flush()
    return [chunk for chunk in chunks if len(chunk["content"]) > 10]


def _looks_like_table(paragraph: str) -> bool:
    """判断长段落是否是 Markdown 表格（首行以 | 开头 + 含分隔行 :---）。"""
    lines = paragraph.split("\n")
    if not lines or not lines[0].lstrip().startswith("|"):
        return False
    return any(l.strip().startswith("|") and "---" in l for l in lines)


def _cell(line: str, idx: int) -> str:
    """取 Markdown 表格某行的第 idx 列内容（去首尾空白）。"""
    parts = line.split("|")
    return parts[idx].strip() if len(parts) > idx else ""


def _fill_primary(line: str, primary: str) -> str:
    """把二级指标行第一列（空）回填上一级指标名，使子表自包含。"""
    parts = line.split("|")
    if len(parts) > 1 and parts[1].strip() == "":
        parts[1] = f" {primary} "
    return "|".join(parts)


def _split_group_by_secondary(header: list[str], rows: list[str], max_chars: int) -> list[str]:
    """一级指标块仍超长时，按二级指标行继续切分（每块仍带表头）。"""
    primary = _cell(rows[0], 1) if rows else ""
    blocks: list[str] = []
    current: list[str] = []
    for r in rows:
        if not _cell(r, 1) and _cell(r, 2):  # 纯二级指标行：回填一级指标名
            r = _fill_primary(r, primary)
        if current and len("\n".join(header + current + [r])) > max_chars:
            blocks.append("\n".join(header + current))
            current = [r]
        else:
            current.append(r)
    if current:
        blocks.append("\n".join(header + current))
    return blocks


def _split_markdown_table(paragraph: str, max_chars: int) -> list[str]:
    """把超长 Markdown 表格按一级指标切分，每块保留表头、二级指标行回填一级指标名。"""
    lines = paragraph.split("\n")
    header = lines[:2]
    body = lines[2:]

    groups: list[list[str]] = []  # 每个元素 = 一个一级指标下的所有行
    current: list[str] = []
    for line in body:
        if _cell(line, 1):  # 一级指标行（第一列非空）
            if current:
                groups.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        groups.append(current)

    result: list[str] = []
    for rows in groups:
        block_lines = header + rows
        block = "\n".join(block_lines)
        if len(block) <= max_chars:
            result.append(block)
        else:
            result.extend(_split_group_by_secondary(header, rows, max_chars))
    return result


def _split_large_chunk(chunk: dict[str, str], max_chars: int = 4_000) -> list[dict[str, str]]:
    """Avoid sending an oversized heading section to structured-output extraction."""
    content = chunk["content"]
    if len(content) <= max_chars:
        return [chunk]

    pieces: list[dict[str, str]] = []
    current_paragraphs: list[str] = []
    current_length = 0

    def flush_piece() -> None:
        nonlocal current_length
        pieces.append({**chunk, "content": "\n\n".join(current_paragraphs)})
        current_paragraphs.clear()
        current_length = 0

    for paragraph in content.split("\n\n"):
        if len(paragraph) > max_chars:
            # 单段落超过上限：先冲刷已积累内容
            if current_paragraphs:
                flush_piece()
            # 超长表格：按一级/二级指标切分，避免 4000 字符硬切把单元格切断、丢规则
            if _looks_like_table(paragraph):
                for sub in _split_markdown_table(paragraph, max_chars):
                    pieces.append({**chunk, "content": sub})
            else:
                for start in range(0, len(paragraph), max_chars):
                    pieces.append({**chunk, "content": paragraph[start:start + max_chars]})
            continue
        if current_length + len(paragraph) > max_chars and current_paragraphs:
            flush_piece()
        current_paragraphs.append(paragraph)
        current_length += len(paragraph) + 2

    if current_paragraphs:
        flush_piece()
    return pieces


def _extract_metadata(chunk: dict[str, Any], structured_llm: Any) -> ChunkMetadata:
    path = " -> ".join(chunk["section_path"])
    prompt = f"""你是专业的规章制度与考核细则知识库解析专家。请严格基于输入的文本段落提取结构化元数据。

【提取要求】
1. 忠实于原文：切勿推测、新增或改写任何考核条件与分值。
2. 逻辑精准：若条款包含“未”、“不得”、“无”等否定词或条件，提取 condition 时必须完整保留否定含义，严禁颠倒逻辑。
3. 规则拆解：若包含多条矩阵加减分规则或表格细则，请逐条拆解为 score_rules 结构。
4. rule_type 只能取：基础考核、加分项、扣分项、一票否决项；score_type 只能取：基本分、加分项、扣分项、通用说明。不得自创其他类别。
5. 纯程序性描述（如评审流程、报送备案、转专业归属等）不要拆成 score_rules，score_type 标为“通用说明”即可。

文档路径：{path}
正文内容：
{chunk['content']}"""
    return structured_llm.invoke(prompt)


def markdown_to_chunks(markdown: str, max_workers: int = 8) -> list[dict[str, Any]]:
    """Create serialisable chunks suitable for both SQLite and Chroma."""
    source_chunks = [
        piece
        for chunk in split_markdown_by_headings(markdown)
        for piece in _split_large_chunk(chunk)
    ]
    if not source_chunks:
        raise ValueError("Markdown 中没有可入库的有效内容")

    structured = _structured_llm()
    t0 = time.perf_counter()
    # executor.map 天然保序；单块失败只重试单块；8 并发仍在 qwen3.8-flash 限流阈值内
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        metas = list(executor.map(lambda c: _extract_metadata(c, structured), source_chunks))
    print(
        f"[3/4] 元数据提取 {len(source_chunks)} 块完成，耗时 {time.perf_counter() - t0:.1f}s",
        flush=True,
    )

    result: list[dict[str, Any]] = []
    for index, (chunk, metadata) in enumerate(zip(source_chunks, metas), start=1):
        result.append(
            {
                "chunk_id": f"chunk_{index:03d}",
                "doc_title": chunk["doc_title"],
                "section_path": chunk["section_path"],
                "content": chunk["content"],
                "metadata": metadata.model_dump(),
            }
        )
    return result
