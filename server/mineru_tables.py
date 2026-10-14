"""MinerU 的 HTML 表格 → Markdown 管道表归一化器。

为什么需要：MinerU 输出 HTML <table>（rowspan/colspan），而下游
`md_to_json.py` 的 `_split_markdown_table`（靠它才修回班干部规则）只认
Markdown 管道表。归一化后下游只需一条路，不用分叉。

合并单元格策略：把合并值【重复填进每个被覆盖的格子】——与 pdfplumber 路径
的 `fill_merged` 策略一致，保证每一行自包含（对 RAG 友好）。
MinerU 知道真实 span，所以展开结果比 pdfplumber 更准。

只用标准库（html.parser），不引入新依赖。
"""

import re
from html.parser import HTMLParser

_CELL_TAGS = {"td", "th"}
_SKIP_TAGS = {"script", "style"}
# ASCII 字母/数字之间的空格是有意义的（"Q1 2024"），其余位置的空格是 OCR 噪声
_ASCII_SPACE = re.compile(r"(?<=[0-9A-Za-z])[ \t]+(?=[0-9A-Za-z])")


def despace(s: str) -> str:
    """清掉 CJK 之间的空格（OCR 把竖排/换行文字拆成单字时会引入）。

    实测：MinerU 在扫描件上把竖排的「学创素质」输出成「学 创 素 质」，
    导致关键词/BM25 完全匹配不到（与 pdfplumber 路径上修过的
    「参 加考核人数」是同一类缺陷）。

    策略：只在 ASCII 字母/数字 之间保留空格，其余 whitespace 全删。
    """
    s = _ASCII_SPACE.sub("\x01", s)
    s = re.sub(r"\s+", "", s)
    return s.replace("\x01", " ")


class _TableExtractor(HTMLParser):
    """从 markdown 文本里抽出所有 <table>，每个解析成 (grid, metadata)。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, int, int]]]] = []
        self._table: list[list[tuple[str, int, int]]] | None = None
        self._row: list[tuple[str, int, int]] | None = None
        self._cell: list[str] | None = None
        self._cell_span = (1, 1)
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip += 1
            return
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in _CELL_TAGS and self._row is not None:
            d = {k.lower(): v for k, v in attrs}
            try:
                colspan = max(int(d.get("colspan", 1) or 1), 1)
            except ValueError:
                colspan = 1
            try:
                rowspan = max(int(d.get("rowspan", 1) or 1), 1)
            except ValueError:
                rowspan = 1
            self._cell = []
            self._cell_span = (colspan, rowspan)
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")
        elif tag == "img" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self._skip = max(self._skip - 1, 0)
            return
        if tag in _CELL_TAGS and self._cell is not None:
            text = despace(re.sub(r"\s+", " ", "".join(self._cell))).strip()
            assert self._row is not None
            self._row.append((text, self._cell_span[0], self._cell_span[1]))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table is not None:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data):
        if self._cell is not None and not self._skip:
            self._cell.append(data)


def _expand(grid_raw: list[list[tuple[str, int, int]]],
            repeat_rowspan: bool = True,
            repeat_colspan: bool = False) -> list[list[str]]:
    """展开 rowspan/colspan。

    合并单元格策略（实测决定）：
      rowspan（纵向合并）→ 把值重复填进被覆盖的每一行。这让每一行自包含，
        对 RAG 是必要的（例：一级指标名只写在首行，不重复就丢了）。
      colspan（横向合并）→ 只在首格放内容，其余留空。因为覆盖的格子都在
        【同一行】内，重复不带来任何信息增量，只会让 BM25 词频虚高
        （实测：重复 colspan 会让 `-1分` 出现 9→17 次，整体规则点 142→160）。
    """
    placed: dict[tuple[int, int], str] = {}
    max_col = 0
    for r, row in enumerate(grid_raw):
        c = 0
        for text, colspan, rowspan in row:
            while (r, c) in placed:
                c += 1
            for dr in range(rowspan if repeat_rowspan else 1):
                placed[(r + dr, c)] = text
                max_col = max(max_col, c + 1)
                if repeat_colspan:
                    for dc in range(1, colspan):
                        placed[(r + dr, c + dc)] = text
                        max_col = max(max_col, c + dc + 1)
                elif dr == 0:
                    for dc in range(1, colspan):
                        placed.setdefault((r, c + dc), "")
                        max_col = max(max_col, c + dc + 1)
            for dr in range(1, rowspan if repeat_rowspan else 1):
                for dc in range(1, colspan):
                    placed.setdefault((r + dr, c + dc), "")
                    max_col = max(max_col, c + dc + 1)
            c += colspan
    if not placed:
        return []
    max_row = max(r for r, _ in placed) + 1
    return [[placed.get((r, c), "") for c in range(max_col)] for r in range(max_row)]


def _to_pipe(grid: list[list[str]]) -> str:
    if not grid or not grid[0]:
        return ""
    width = max(len(r) for r in grid)
    grid = [r + [""] * (width - len(r)) for r in grid]
    esc = lambda s: s.replace("|", "\\|").strip()          # noqa: E731
    out = ["| " + " | ".join(esc(c) for c in grid[0]) + " |",
           "|" + "---|" * width]
    for row in grid[1:]:
        out.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(out)


def normalize_html_tables(markdown: str,
                          repeat_rowspan: bool = True,
                          repeat_colspan: bool = False) -> tuple[str, dict]:
    """把 markdown 里所有 <table> 换成 Markdown 管道表。

    返回 (新文本, 统计信息)
    """
    stats = {"tables": 0, "rows_in": 0, "rows_out": 0, "cells_spanned": 0,
             "empty_ratio": 0.0, "max_cols": 0,
             "repeat_rowspan": repeat_rowspan, "repeat_colspan": repeat_colspan}
    p = _TableExtractor()
    p.feed(markdown)

    grids = [_expand(g, repeat_rowspan, repeat_colspan) for g in p.tables]

    def _one(m: re.Match) -> str:
        i = stats["tables"]
        stats["tables"] += 1
        if i >= len(grids):
            return m.group(0)
        return _to_pipe(grids[i])

    new_text = re.sub(r"<table\b.*?</table>", _one, markdown, flags=re.S | re.I)

    total_cells = 0
    empty_cells = 0
    for grid, raw in zip(grids, p.tables):
        stats["rows_in"] += len(raw)
        stats["rows_out"] += len(grid)
        if grid:
            stats["max_cols"] = max(stats["max_cols"], len(grid[0]))
        for row in raw:
            for _, cs, rs in row:
                if cs > 1 or rs > 1:
                    stats["cells_spanned"] += (cs * rs - 1)
        for row in grid:
            for c in row:
                total_cells += 1
                if not c:
                    empty_cells += 1
    stats["empty_ratio"] = round(empty_cells / total_cells, 4) if total_cells else 0.0

    # 清掉残留的孤立 HTML 标签（MinerU 偶尔输出 <sup> 等）
    new_text = re.sub(r"</?(?!table|tr|td|th)[a-zA-Z][^>]{0,40}>", "", new_text)
    return new_text, stats


if __name__ == "__main__":
    import sys
    from pathlib import Path

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.stem + "_normalized.md")
    text = src.read_text(encoding="utf-8")
    out, st = normalize_html_tables(text)
    dst.write_text(out, encoding="utf-8")
    print(f"{src.name} → {dst.name}")
    print(f"  表格 {st['tables']} 张（行 {st['rows_in']} → {st['rows_out']}，"
          f"最多 {st['max_cols']} 列）")
    print(f"  展开出的合并格子 {st['cells_spanned']} 个（合并值已重复填充）")
    print(f"  残留空单元格比例 {st['empty_ratio']:.1%}")
    print(f"  剩余 HTML 标签 <table>={out.count('<table')} "
          f"<tr>={out.count('<tr')} <td>={out.count('<td')}")
    print(f"  管道表行数 {sum(1 for l in out.splitlines() if l.lstrip().startswith('|'))}")
    print(f"  字符数 {len(text)} → {len(out)}")
    (dst.parent / (dst.stem + "_stats.txt")).write_text(
        "\n".join(f"{k}: {v}" for k, v in st.items()), encoding="utf-8")