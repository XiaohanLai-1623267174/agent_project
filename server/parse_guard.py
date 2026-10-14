"""pdfplumber 解析可信度自检（兜底触发器）。

阈值全部来自实测（`guard_report.md` + `probe_borderless_detector.py`）：

正控（源 PDF，已验证干净）的基线：
    无文本层页 0 ／ lines 策略表 11 ／ 退化表(cols<2) 0 ／ 稀疏表 0
    正文残留规则点 0 ／ 正文-表格 6gram 重合 0.14% ／ 跨页合并候选 4

设计原则：
  1. 便宜 —— 昂贵探针惰性求值；实测在好文档上额外成本为 0
  2. 独立 —— 不拿 pdfplumber 的输出证明 pdfplumber 自己
  3. 只对"兜底确实能修"的问题触发（表格结构类）
     标题类问题故意不触发：实测 MinerU basic 只 5 个标题、flash 只 1 个，
     而 pdfplumber v8 有 9 个 —— 升级反而更差
  4. 在已知干净文档上 0 误报（含警告）
"""

import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import pdfplumber

# ---------------- 阈值（均为实测基线） ----------------
GAP_PT = 8.0                    # 行内"列间隙"的间距
EMPTY_PAGE_MAX_CHARS = 5        # 少于这么多字符 = 无文本层
IMAGE_COVER_RATIO = 0.50        # 图像覆盖页面面积比例
DEGENERATE_MAX_COLS = 2         # cols < 此值 = 真退化表（实测正控为 0）
SPARSE_EMPTY_RATIO = 0.60       # 原始空单元格占比超过此值 = 稀疏表（实测正控为 0）
SPARSE_FRACTION = 0.30          # 稀疏表占比超过此值才告警
NGRAM = 6
NGRAM_OVERLAP_MAX = 0.05        # 实测正控 0.0014
# text 策略探针（无框线表格）的判据
PROBE_MIN_MODAL_COLS = 2        # 散文会被切成 1 列；真表格 ≥2 列
PROBE_MIN_ROWS = 3
PROBE_MIN_CONSISTENCY = 0.50


def _nospace(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _ngrams(s: str, n: int = NGRAM) -> set:
    s = _nospace(s)
    return {s[i:i + n] for i in range(max(len(s) - n + 1, 0))}


@dataclass
class Alarm:
    code: str
    severity: str          # "fatal" 应升级 ／ "warn" 只记录
    detail: str
    value: float
    threshold: float

    def __str__(self) -> str:
        return (f"[{self.severity.upper():5}] {self.code}: {self.detail} "
                f"(实测 {self.value:.3f} / 阈值 {self.threshold:.3f})")


def _multicol_ratio(page, gap: float = GAP_PT) -> float:
    """该页有多少比例的行含有多个"列间隙"（昂贵的 text 探针的廉价前置条件）。"""
    lines = page.extract_text_lines()
    if not lines:
        return 0.0
    hit = 0
    for ln in lines:
        chars = sorted(ln["chars"], key=lambda c: c["x0"])
        gaps = sum(1 for a, b in zip(chars, chars[1:]) if b["x0"] - a["x1"] > gap)
        if gaps >= 2:
            hit += 1
    return hit / len(lines)


def _text_strategy_probe(page) -> dict | None:
    """昂贵探针：text 策略找表，并用「列数一致性」判断它是不是真表格。

    实测要点：散文会被 text 策略切成 modal_cols=1 的单列表；
    真表格是 ≥2 列。所以 modal_cols 是关键判据。
    """
    best = None
    for t in page.find_tables({"vertical_strategy": "text", "horizontal_strategy": "text"}):
        rows = t.extract()
        counts = [len(r) for r in rows]
        if not counts:
            continue
        modal, freq = Counter(counts).most_common(1)[0]
        cand = {
            "rows": len(rows),
            "modal_cols": modal,
            "consistency": freq / len(counts),
        }
        if (modal >= PROBE_MIN_MODAL_COLS and len(rows) >= PROBE_MIN_ROWS
                and cand["consistency"] >= PROBE_MIN_CONSISTENCY):
            if best is None or cand["modal_cols"] > best["modal_cols"]:
                best = cand
    return best


def collect(pdf_path: Path, content_signature: re.Pattern | None = None) -> dict:
    """只测量，不判断。content_signature 是可选的领域内容特征（本项目：分值规则点）。"""
    t_start = time.perf_counter()
    d: dict = {
        "file": Path(pdf_path).name,
        "chars_per_page": [],
        "image_cover_per_page": [],
        "tables_lines_per_page": [],
        "multicol_ratio_per_page": [],
        "probe_runs": [],
        "probe_hits": [],
        "table_shapes": [],
        "degenerate_tables": 0,
        "wide_tables": 0,
        "sparse_tables": 0,
        "crosspage_merge_candidates": 0,
        "body_chars_excluded": 0,
        "signature_hits_in_body": 0,
        "table_ngram_overlap": 0.0,
        "timing": {},
    }

    t0 = time.perf_counter()
    with pdfplumber.open(pdf_path) as pdf:
        d["pages"] = len(pdf.pages)
        table_text_parts: list[str] = []
        body_parts: list[str] = []
        prev_cols: set | None = None

        for pno, page in enumerate(pdf.pages, 1):
            d["chars_per_page"].append(len(page.chars))

            page_area = max(page.width * page.height, 1.0)
            img_area = 0.0
            for im in page.images:
                img_area += max((im["x1"] - im["x0"]) * (im["bottom"] - im["top"]), 0.0)
            d["image_cover_per_page"].append(min(img_area / page_area, 1.0))

            tables = page.find_tables()
            d["tables_lines_per_page"].append(len(tables))

            cols_here = {len(t.rows[0].cells) if t.rows else 0 for t in tables}
            if prev_cols and cols_here and (prev_cols & cols_here):
                d["crosspage_merge_candidates"] += 1
            prev_cols = cols_here or prev_cols

            for t in tables:
                rows = t.extract()
                ncols = max((len(r) for r in rows), default=0)
                if ncols < DEGENERATE_MAX_COLS:
                    d["degenerate_tables"] += 1
                if ncols > 15:
                    d["wide_tables"] += 1
                cells = [c for r in rows for c in r]
                empty = sum(1 for c in cells if c is None or not str(c).strip())
                er = empty / len(cells) if cells else 1.0
                d["table_shapes"].append((len(rows), ncols, round(er, 3)))
                if er > SPARSE_EMPTY_RATIO and len(rows) >= 2 and ncols >= 2:
                    d["sparse_tables"] += 1
                table_text_parts.extend(str(c) for c in cells if c and str(c).strip())

            # ---- 无框线表格：廉价前置条件 + 惰性昂贵探针 ----
            ratio = _multicol_ratio(page)
            d["multicol_ratio_per_page"].append(round(ratio, 3))
            if len(tables) == 0 and ratio > 0:
                probe = _text_strategy_probe(page)
                d["probe_runs"].append(pno)
                if probe:
                    d["probe_hits"].append({"page": pno, **probe})

            # ---- 剔除表格区域后的正文（没找到表时，正文就是原始文本）----
            # 注意：不能只在 rects 非空时才收集，否则"一张表都没找到"的情况下
            # 正文为空，signature_in_body 信号会自行失效。
            rects = [t.bbox for t in tables]
            if rects:
                def keep(obj, _r=rects):
                    try:
                        cx = (obj["x0"] + obj["x1"]) / 2
                        cy = (obj["top"] + obj["bottom"]) / 2
                    except Exception:
                        return True
                    return not any(x0 <= cx <= x1 and top <= cy <= bottom
                                   for x0, top, x1, bottom in _r)
                body_parts.append(page.filter(keep).extract_text() or "")
            else:
                body_parts.append(page.extract_text() or "")

    d["timing"]["extract"] = round(time.perf_counter() - t0, 3)

    body_text = "\n".join(body_parts)
    d["body_chars_excluded"] = len(body_text)
    if content_signature is not None:
        d["signature_hits_in_body"] = len(content_signature.findall(body_text))
    table_text = "\n".join(table_text_parts)
    tg, bg = _ngrams(table_text), _ngrams(body_text)
    d["table_ngram_overlap"] = round(len(tg & bg) / len(tg), 4) if tg else 0.0
    d["timing"]["total"] = round(time.perf_counter() - t_start, 3)
    return d


def audit(d: dict) -> list[Alarm]:
    """纯阈值判断，~0 成本。"""
    alarms: list[Alarm] = []

    # 信号 1：无文本层（扫描件）—— 领域无关
    empty_pages = {i + 1 for i, n in enumerate(d["chars_per_page"]) if n <= EMPTY_PAGE_MAX_CHARS}
    covered = {i + 1 for i, r in enumerate(d["image_cover_per_page"]) if r >= IMAGE_COVER_RATIO}
    if empty_pages & covered:
        alarms.append(Alarm("no_text_layer", "fatal",
                            f"第 {sorted(empty_pages & covered)} 页无文本层且被图像覆盖，疑似扫描件",
                            len(empty_pages & covered), 0))

    # 信号 2：无框线表格 —— 领域无关（本次验证通过的判据）
    #   lines 策略找不到表，但 text 策略说这里有多列表结构
    if d["probe_hits"]:
        pages = [h["page"] for h in d["probe_hits"]]
        worst = max(h["modal_cols"] for h in d["probe_hits"])
        alarms.append(Alarm("borderless_table", "fatal",
                            f"第 {pages} 页 lines 策略找不到表格，但文字呈多列表结构"
                            f"（列数最高 {worst}），疑似无框线表格",
                            worst, PROBE_MIN_MODAL_COLS))

    # 信号 3：正文残留内容特征（需调用方配置签名；本项目实测正控 0 / 负控 141）
    if d["signature_hits_in_body"] > 0:
        alarms.append(Alarm("signature_in_body", "fatal",
                            f"剔除表格后正文仍残留 {d['signature_hits_in_body']} 个内容特征，"
                            f"表格区域可能漏划或划错",
                            d["signature_hits_in_body"], 0))

    # 信号 4（弱）：正文与表格文本重合 —— 实测正控 0.14%
    if d["table_ngram_overlap"] > NGRAM_OVERLAP_MAX:
        alarms.append(Alarm("body_table_overlap", "warn",
                            f"正文与表格文本 {NGRAM}-gram 重合率过高，剔除不彻底",
                            d["table_ngram_overlap"], NGRAM_OVERLAP_MAX))

    # 信号 5：退化表占比 —— 实测正控 0/11
    total_t = sum(d["tables_lines_per_page"])
    if total_t and d["degenerate_tables"] / total_t > 0.30:
        alarms.append(Alarm("degenerate_ratio", "warn",
                            f"退化表（列数<{DEGENERATE_MAX_COLS}）占比过高，表格检测质量可疑",
                            d["degenerate_tables"] / total_t, 0.30))
    if total_t and d["sparse_tables"] / total_t > SPARSE_FRACTION:
        alarms.append(Alarm("sparse_ratio", "warn",
                            f"稀疏表占比过高，合并单元格填充可能有问题",
                            d["sparse_tables"] / total_t, SPARSE_FRACTION))

    # 故意不触发：标题数量少 / 字号层级单一（实测 MinerU 更差）
    return alarms


def report(d: dict, alarms: list[Alarm]) -> str:
    L = [f"### {d['file']}", ""]
    L.append(f"- 页数 {d['pages']}｜总字符 {sum(d['chars_per_page'])}｜"
             f"lines 表 {sum(d['tables_lines_per_page'])}｜"
             f"无文本层页 {sum(1 for n in d['chars_per_page'] if n <= EMPTY_PAGE_MAX_CHARS)}")
    L.append(f"- 每页字符 {d['chars_per_page']}")
    L.append(f"- 每页表数 {d['tables_lines_per_page']}")
    L.append(f"- 每页多列行占比 {d['multicol_ratio_per_page']}")
    L.append(f"- 每页图像覆盖 {[round(x, 2) for x in d['image_cover_per_page']]}")
    L.append(f"- 昂贵探针执行页 {d['probe_runs'] or '无（前置条件全不成立 → 零额外成本）'}")
    L.append(f"- 探针命中 {d['probe_hits'] or '无'}")
    L.append(f"- 表格形状(行,列,空占比) {d['table_shapes'][:8]}"
             f"{' …' if len(d['table_shapes']) > 8 else ''}")
    L.append(f"- 退化表 {d['degenerate_tables']}｜宽表(>15列) {d['wide_tables']}｜"
             f"稀疏表 {d['sparse_tables']}｜跨页合并候选 {d['crosspage_merge_candidates']}")
    L.append(f"- 正文(剔除后) {d['body_chars_excluded']} 字符｜"
             f"内容特征残留 {d['signature_hits_in_body']}｜"
             f"与表格 {NGRAM}-gram 重合 {d['table_ngram_overlap']:.2%}")
    L.append(f"- 耗时 {d['timing']}")
    L.append("")
    L.extend(f"- {a}" for a in alarms) if alarms else L.append("- **0 告警** ✓")
    L.append("")
    return "\n".join(L)