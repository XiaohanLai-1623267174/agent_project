"""联网搜索（无 key 默认可用）—— provider 抽象 + 两道闸 + 降级计价 + 成本闸。

为什么"无 key 默认"是硬要求
---------------------------
本项目 `.env` 里只有 LLM 的 key（QW / SENSENOVA / MS），**没有任何搜索 key**。
用户点「联网搜索」绝不能因为"没配 key"而什么都不发生 —— 那正是本项目最想消灭的
"**点了没反应**"（§39.29 记的就是这类静默失效）。所以默认链必须**零配置可用**，
keyed provider 只是"将来有 key 就优先"的可插拔项（见 `register_provider()`）。

选型：全部依据实测（`kb_bench/_probe_websearch.py`，2026-10-13 五轮探针）
--------------------------------------------------------------------
| 候选 | 实测结果 | 结论 |
|---|---|---|
| DuckDuckGo（html / api） | 本机**连接超时** | ✗ 不可用 |
| **Bing**（HTML 与 RSS） | HTTP 200、结构**完美**（9 条 `b_algo`、标题摘要齐全），**但内容与查询无关**：问「中国海洋大学 奖学金 评定办法」回来的是「中华人民共和国_百度百科」，乱码串回来的是哔哩哔哩；**相关性 0/9** | ✗ **判为诱饵页，绝不采用** |
| `www.baidu` | 1488 B 反爬页（"百度安全验证"） | ✗ |
| `m.baidu` | 200、结果相关；但单条结果内层是 React + `data-log` JSON、链接需解包 | ✗ 结构太脆，留证不用 |
| **`sogou`** | 7 块中 5 条三件套齐全，站点命中**本校**（`xsgz/chem/econ.ouc.edu.cn`） | ✅ **首选** |
| **`so360`** | 6 块中 2 条有 `data-mdurl` + 摘要，其余是自家 AI/文库推荐位 | ✅ 次选（**必须要求 `data-mdurl`**） |

★ Bing 那一行是本模块存在的理由：它的返回**看起来完全正常**，只有"结果里有没有查询词"
  这一条能识破它。**这类源比"失败"危险得多** —— 失败会如实显示"没搜到"，
  而它会把垃圾注入回答、还挂着"网页来源"的招牌。所以 `search()` 里有两道闸：

  ① **三件套闸**：标题 + 真链接（http/https）+ 摘要缺一即丢 ⇒ 挡掉推荐位/提示框/广告位；
  ② **相关性闸**：结果文本必须命中查询词 ≥ `_min_hits()` 个 ⇒ 挡掉"结构完美但全无关"。

成本闸（§39.36.7-3 约定的"成本闸"）
----------------------------------
· 每问最多外呼 `len(providers)` 次（默认 2 次），单次 `HTTP_TIMEOUT=(4,8)` 秒 ⇒ 最坏 ~16 秒；
· 同一问句 **5 分钟内复用缓存**（成功）；失败结论只缓存 60 秒（避免把一次抖动锁 5 分钟）；
· 进程级**限速** `RATE_MAX_CALLS/分钟` ⇒ 防止误用/连环点击把引擎惹毛（触发即降级记账）；
· 进 prompt 的网络块**硬截断** `MAX_BLOCK_CHARS`（token 成本可控）；
· 不做任何页面正文抓取（只吃搜索结果摘要）⇒ 没有"抓网页"那类不可控延迟与解析风险。

诚实边界（★ 不夸大，写在这里防止后来者误读）
------------------------------------------
· "相关性闸"只证明"结果文本里出现了查询词"，**不等于**内容正确或权威；
· 摘要可能过时/被截断，本模块**不抓正文**，因此不保证拿到完整规则；
· 无 key 的 HTML 源**随时可能改版或被反爬**：改版后表现为 `parse_empty`
  （降级记账里可见），**不会**静默给出垃圾（两道闸兜底）。
"""
from __future__ import annotations

import datetime
import hashlib
import html as html_mod
import os
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

# ============================================================================
#  参数（全部是"成本闸"的一部分，改这里就是改闸门）
# ============================================================================
DEFAULT_PROVIDERS = ("sogou", "so360")
MAX_ITEMS = 20                   # 进 prompt / 给前端的条数上限（2026-10-16 用户：越多越好 ⇒ 5→20）
# ★ 2026-10-18：5000 → **5800**，依据是**真实 20 条结果的渲染长度实测**（花 1 credit 的 `_probe_tavily_20_items.py`，
#   证据原件 `_probe_tavily_20_items.json`）：同一条问句「帮我搜索一下最近一周的ai大事」真实返回 20 条，
#   纯块头 480 + 条目行 4992 = **5472 字符** ⇒ 闸 5000 时只进 **17** 条（用户当场看到「为什么不是 20」）。
#   5800 ≈ 5472 + 6% 余量。
#   ⚠️ 这**不是**「永远装得下 20 条」的保证：单条最长实测 **571 字符**（超长 URL + 长标题），
#      换一批 URL 更长的结果仍可能裁掉一两条 —— 那时**明细日志会如实说明**
#      （「预算前 N 条 -> 进 prompt M 条」），不会静默变少（§39.47 的教训）。
MAX_BLOCK_CHARS = 5800           # 网络块进 prompt 的硬上限（token 成本闸）★ 2026-10-16→18：2000→4400→5000→5800
HTTP_TIMEOUT = (4.0, 8.0)        # (连接, 读取) —— 单源最坏 8 秒
CACHE_TTL_SEC = 300.0            # 成功结论缓存 5 分钟
# ★ ②（2026-10-17 用户批准）：失败结论的缓存从 **60 秒压到 5 秒**。
#   动机（真实故障链）：用户在 60 秒内重问同一个问题，会**直接重放上一次的"没搜到"**——
#   日志实证：第 2 次请求连 Tavily 都没调（没有 `[联网搜索·tavily]` 行）就直接回「未得到可用结果」。
#   「失败」不该和「成功」享受同等时长的记忆。保留几秒只是为了挡住**手抖连点**（避免为同一次
#   误点付两份钱）—— 那点收益不值得让用户在一分钟内反复看到同一个失败。
CACHE_TTL_FAIL_SEC = 5.0         # 失败结论只缓存 5 秒（原 60 秒，见上）
RATE_WINDOW_SEC = 60.0
RATE_MAX_CALLS = 12              # 每分钟最多 12 次真实外呼

PROVIDER_LABELS = {"sogou": "搜狗", "so360": "360搜索", "tavily": "Tavily", "serper": "Serper"}

# 降级原因 → 给用户看的人话（进 /health/degradations 的 legend，见 fastapi_project.py）
REASON_TEXT = {
    "no_results": "首选源没有返回任何结果（可能是问句里的时间范围过窄，或该源此刻没有相关索引）",
    "irrelevant": "搜索结果与问题不相关（已丢弃，避免注入无关内容）",
    "parse_empty": "结果页结构变了，解析不出结果",
    "timeout": "搜索引擎响应超时",
    "http_error": "搜索引擎请求失败",
    "rate_limited": "触发本机限速（每分钟外呼上限）",
    "empty_query": "查询为空",
}

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ============================================================================
#  数据形状
# ============================================================================
@dataclass(frozen=True)
class WebItem:
    """一条网络结果。`url` **必须**是 http/https（解析层已过滤）——前端要把它渲染成可点链接。"""
    title: str
    url: str
    snippet: str
    site: str
    provider: str

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet,
                "site": self.site, "provider": self.provider,
                "provider_label": PROVIDER_LABELS.get(self.provider, self.provider)}


# 逐源原因都在时，最终该报哪一个？★ 这是被自己的装置抓出来的一个真 bug：
#   `search()` 原来用一个 `last_reason` 逐源覆盖 ⇒ 首选源明明"搜到了但全不相关"（irrelevant，
#   信息量大：说明这个源在骗人），却被次选源的 `parse_empty`（只是它结构没解析出来）盖掉，
#   用户最终看到的是"结果页结构变了"，与事实不符。
#   现在：逐源原因全部记进 `tried_reasons`，最终 `reason` 按**信息量**取（越靠前越具体）。
#   ★ 2026-10-17 修正（④ 的后半）：上一次会话只改了 `REASON_TEXT` 与 keyed 源的原因码，**漏了这张表**
#     ⇒ 记录写着"已落地"，代码里 `parse_empty` 仍排在 `no_results` 之前 ⇒ 用户读到的依然是误导文案。
#     为什么必须让 `no_results` 先胜出：真实故障的形状是——**主源是 keyed 的 Tavily（有凭据、花了钱、
#     用户最信它）返回 0 条**，而两个 HTML 源只是「页面结构没解析出来」。若让 `parse_empty` 胜出，
#     用户（和我自己，本轮就被带偏过一次）会去查解析器，而事实是主源压根没返回东西。
#     逐源真相并不因此丢失：全部仍在 `tried_reasons` 里，并由失败路径的明细日志打出来（③）。
_REASON_PRIORITY = ("rate_limited", "http_error", "timeout", "irrelevant",
                    "no_results", "parse_empty", "empty_query")


def _pick_reason(per_provider: list) -> str:
    """从逐源原因里挑最终原因：优先级越靠前越"具体且可行动"。"""
    got = [r for _, r in per_provider if r]
    if not got:
        return "no_results"
    for cand in _REASON_PRIORITY:
        if cand in got:
            return cand
    return got[-1]


@dataclass
class WebOutcome:
    """一次联网搜索的完整结论。`attempted` 是**给前端看的**：用户没开联网搜索时是 False。"""
    attempted: bool = False
    items: list = field(default_factory=list)
    provider: str = ""
    reason: str | None = None
    elapsed_ms: int = 0
    http_calls: int = 0
    cached: bool = False
    providers_tried: list = field(default_factory=list)
    tried_reasons: list = field(default_factory=list)   # [(源名, 该源的原因), ...] —— 诊断用
    query: str = ""
    date_range: str = ""                                # 实际生效的时间区间（空 = 没加过滤）
    # ★ 诊断用，但**必须声明成字段** —— 不能事后 `out.items_kept = ...` 动态加：
    #   缓存命中走的是 `WebOutcome(**{**cached.__dict__})`，多一个未声明的键就抛 TypeError，
    #   而它会被 `_web_block` 的 except 兜成「http_error ⇒ 没找到可用结果」——
    #   表现为「同一个问题第二次问反而搜不到了」（2026-10-16 实测踩到，装置 ⑥d 抓住）。
    items_kept: int = 0                                 # 预算裁剪**前**的条数（见 _finish 明细行）
    # ★ ① 的诚实开关（2026-10-17）：为「带区间 0 条 ⇒ 去掉区间重搜成功」这一种情形单独留一个声明位。
    #   为什么要声明成字段而不是动态挂：同上 —— 缓存命中走 `WebOutcome(**cached.__dict__)`。
    #   为什么非要有它：`date_range` 一旦非空，`_block_head()` 就会对模型声称
    #   「已按此区间过滤；区间外的结果不会出现在这里」，而放宽那一遍**一个日期参数都没发** ⇒ 那是谎报，
    #   模型会据此把不限时间的旧内容当「最近一周」讲（比原来的「没搜到」更坏）。
    dates_relaxed: bool = False                         # True = 这份结果来自「放宽时间范围」的那一遍

    @property
    def ok(self) -> bool:
        return bool(self.items)

    def to_status(self) -> dict:
        """随 SSE 的 sources 帧推给前端（字段名与既有帧风格一致：小驼峰+下划线混用已被前端按名取）。"""
        return {
            "attempted": self.attempted,
            "provider": self.provider,
            "provider_label": PROVIDER_LABELS.get(self.provider, self.provider),
            "count": len(self.items),
            "reason": self.reason,
            "reason_text": REASON_TEXT.get(self.reason or "", self.reason or ""),
            "elapsed_ms": self.elapsed_ms,
            "cached": self.cached,
            "providers_tried": list(self.providers_tried),
            "tried_reasons": [{"provider": p, "reason": r} for p, r in self.tried_reasons],
            "date_range": self.date_range,
            "items": [it.to_dict() for it in self.items],
        }


# ============================================================================
#  HTML 工具（★ 不新增依赖：本环境没有 bs4/lxml，探针已确认）
# ============================================================================
_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_WS_RE = re.compile(r"[\s\u200b\u00a0]+")
# ★ 只有【块级/换行】标签才换成空格，其余标签**直接删掉**。
#   为什么：搜索引擎用 `<em><!--red_beg-->奖学金<!--red_end--></em>` 做关键词高亮，
#   若把标签一律替换成空格，摘要里就会出现「本科学生 奖学金 评审办法」这种**假空格**
#   （第一次真跑就看到了）—— 直接进 prompt，会被模型当成原文的一部分。
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|li|ul|ol|br|tr|td|h[1-6]|section|article)\b[^>]*>", re.I)


def _text(raw: str) -> str:
    """去注释 → 块级标签换空格、行内标签直接删（消掉 `<em>` 高亮造成的假空格）→ 反转义 → 折叠空白。"""
    s = _COMMENT_RE.sub("", raw or "")
    s = _BLOCK_TAG_RE.sub(" ", s)
    s = _TAG_RE.sub("", s)
    return _WS_RE.sub(" ", html_mod.unescape(s)).strip()


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:                                     # noqa: BLE001
        return ""


def _safe_url(raw: str) -> str:
    """只放行 http/https —— ★ 安全闸：`javascript:` / `data:` 一律丢弃（防注入到前端的 href）。"""
    u = html_mod.unescape((raw or "").strip())
    if not u.lower().startswith(("http://", "https://")):
        return ""
    return u


# ============================================================================
#  解析器（结构照实测写；每一条都要求"三件套齐全"）
# ============================================================================
def parse_sogou(html: str, top_n: int = MAX_ITEMS) -> list:
    """搜狗：`<div class="vrwrap">` 块，标题在 `<h3>` 里，**真链接在 `data-url`**（`href` 是 /link? 跳转）。

    ★ 必须用 `data-url`：`href="/link?url=…"` 只是搜狗的跳转串，直接给前端等于"点开是跳转页"。
    ★ 块里混着"大家还在搜"这类提示框（无 `<h3>` 或无 `data-url`）⇒ 拿不到三件套就丢。
    """
    out = []
    blocks = re.findall(r'<div class="vrwrap[^"]*"[\s\S]*?(?=<div class="vrwrap|</body>)', html or "")
    for block in blocks:
        h3 = re.search(r"<h3[^>]*>([\s\S]*?)</h3>", block)
        if not h3:
            continue
        title = _text(h3.group(1))
        mu = re.search(r'data-url="([^"]+)"', block)
        url = _safe_url(mu.group(1)) if mu else ""
        if not title or not url:
            continue
        snippet = ""
        for cls in ("star-wiki", "text-layout", "fz-mid", "space-txt"):
            sm = re.search(r'class="[^"]*%s[^"]*"[^>]*>([\s\S]*?)</(?:p|div|span)>' % cls, block)
            if sm:
                cand = _text(sm.group(1))
                if len(cand) > len(snippet):
                    snippet = cand
        out.append(WebItem(title=title, url=url, snippet=snippet,
                           site=_host(url), provider="sogou"))
        if len(out) >= top_n:
            break
    return out


def parse_so360(html: str, top_n: int = MAX_ITEMS) -> list:
    """360搜索：`<li class="res-list">` 块，**真链接在 `data-mdurl`**，摘要在 `class="res-list-summary"`。

    ★ 必须要求 `data-mdurl`：不要求的话，`wenku.so.com` / `ai.so.com` 这类**自家推荐位**
      也会被当成结果（实测 6 块里只有 2 块是真结果）。
    ★ 属性顺序不保证（`href` 与 `data-mdurl` 谁先谁后都可能）⇒ 先取 `<h3>` 再从块里找 `data-mdurl`，
      不做"两个属性相邻"的假设（那是最容易随改版失效的写法）。
    """
    out = []
    blocks = re.findall(r'<li class="res-list"[\s\S]*?(?=<li class=|</ol>)', html or "")
    for block in blocks:
        h3 = re.search(r"<h3[^>]*>([\s\S]*?)</h3>", block)
        if not h3:
            continue
        title = _text(h3.group(1))
        mu = re.search(r'data-mdurl="([^"]+)"', block)
        url = _safe_url(mu.group(1)) if mu else ""
        if not title or not url:
            continue
        sm = re.search(r'class="[^"]*res-list-summary[^"]*"[^>]*>([\s\S]*?)</span>', block)
        snippet = _text(sm.group(1)) if sm else ""
        if not snippet:
            # ★ 兜底：不是每条都带 res-list-summary（实测 3 条里有 1 条带）。
            #   没有摘要的条目**仍然有用**（标题 + 真链接），但摘要尽量补一个，
            #   否则进 prompt 的条目几乎等于只有标题。
            for pat in (r'class="[^"]*res-desc[^"]*"[^>]*>([\s\S]*?)</(?:div|p|span)>',
                        r"<p[^>]*>([\s\S]*?)</p>"):
                m2 = re.search(pat, block)
                if m2 and _text(m2.group(1)):
                    snippet = _text(m2.group(1))
                    break
        out.append(WebItem(title=title, url=url, snippet=snippet,
                           site=_host(url), provider="so360"))
        if len(out) >= top_n:
            break
    return out


# ============================================================================
#  provider 抽象（keyed provider 将来插在这里）
# ============================================================================
# name -> (解析器, URL, 查询参数名)
_PROVIDERS = {
    "sogou": (parse_sogou, "https://www.sogou.com/web", "query"),
    "so360": (parse_so360, "https://www.so.com/s", "q"),
}
_EXTRA = {}          # register_provider() 注册进来的（keyed）provider：name -> callable(query, top_n)


def register_provider(name: str, fn) -> None:
    """注册一个"自带凭据"的 provider（如 Tavily/Serper）。

    fn(query, top_n) -> list[WebItem]。★ 现在**没有**任何搜索 key，所以一个都没注册 ——
    这不是"忘了"，而是刻意的：注册了却调用失败，用户看到的就是"点了没反应"。
    将来有 key 时在 `order()` 里把它排在前面即可，路由/前端/装置都不用改。
    """
    _EXTRA[name] = fn


def order(providers=None) -> list:
    """provider 顺序：显式指定 > 已注册的 keyed provider > 默认链。

    ★ 2026-10-16 起：**只要注册了 keyed provider（默认即 Tavily）就只用它**，
      不再回落到搜狗/360 —— 用户明确要求「网页搜索都用 tavily」。两个理由：
        · 混源的**成色**不可预测：HTML 源会给出"结构完美但全无关"的诱饵页（本模块存在的理由）；
        · 混源的**成本**不可预测：Tavily 是按次计费的，回落会让"这次花了没有"说不清。
      Tavily 失败时**不静默换源**，而是如实报"没搜到"+ 原因（降级可见，§39.29）。
      想恢复回落行为：设 `WEBSEARCH_KEYED_ONLY=0`。
    ★ **没有 key 时行为与改造前逐字相同**（默认链 sogou → so360）——
      "无 key 也能用"是本模块的硬契约，不能因为接了 Tavily 就破坏。
    """
    if providers:
        return list(providers)
    autoregister_tavily()
    if _EXTRA and _keyed_only():
        return list(_EXTRA.keys())
    return list(_EXTRA.keys()) + list(DEFAULT_PROVIDERS)


def _keyed_only() -> bool:
    """有 keyed provider 时是否**只用**它（默认是）。

    ★ 惰性读 env：本模块**不在 import 期读任何 env** —— 这是 tier-2 迁移审计的既有结论
      （`audit_env_migration.py` 把"import 期读 env 的模块"算出来，`websearch` 不在其中），
      在这里加一句模块级 `os.getenv` 会把那条结论打破，所以一律放进函数里现读。
    """
    v = (os.getenv("WEBSEARCH_KEYED_ONLY", "1") or "").strip().lower()
    return v not in ("0", "false", "off", "no")


# ============================================================================
#  相关性闸（★ 本模块的核心防线：识破"结构完美但全无关"的诱饵页）
# ============================================================================
_STOPWORDS = {"的", "了", "和", "与", "及", "或", "是", "在", "有", "我", "你", "他", "她", "它",
              "们", "这", "那", "什么", "怎么", "如何", "请问", "一下", "哪些", "多少", "关于",
              "以及", "可以", "是否", "不会", "没有", "介绍", "说明"}


def query_terms(query: str) -> list:
    """把查询切成"用于判断相关性"的词表。

    ★ 用 jieba（本项目已依赖，BM25 在用）；**jieba 缺失时退化**为按空白/标点切，
      绝不因为分词库出问题就整条联网搜索失效。
    ★ 整串查询也算一个词：命中长串显然更相关（排序时按命中数排）。
    """
    q = (query or "").strip()
    toks = []
    try:
        import jieba                                     # noqa: PLC0415
        toks = list(jieba.lcut(q))
    except Exception:                                    # noqa: BLE001
        toks = re.split(r"[\s,，、。？?！!；;：:／/（）()\[\]【】]+", q)
    terms = []
    for t in toks:
        t = (t or "").strip()
        if len(t) < 2 or t in _STOPWORDS or t.isdigit():
            continue
        terms.append(t)
    if len(q) >= 2:
        terms.append(q)
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _min_hits(n_terms: int) -> int:
    """需要命中几个词才算相关。

    ★ 这是**对着 Bing 的失败调出来的**：诱饵结果里有"中国"（命中 1 个词）却与问题无关，
      所以"命中 ≥1 个"不够；只要查询切出 ≥2 个词，就要求**≥2 个**。
      （Bing 诱饵页那 9 条全是 1 个命中 ⇒ 会被整体丢掉。）
    """
    return 1 if n_terms <= 1 else 2


def relevance_filter(query: str, items: list) -> tuple:
    """按相关性过滤 + 排序。返回 `(kept_items, terms, need_hits)`。

    ★ 刻意**返回被丢弃的条数**给调用方记账：如果一次搜索"抓到了 6 条、全被丢弃"，
      那说明源在用诱饵页骗人（或关键词闸太严），这是**必须能在降级记账里看见**的事实。
    """
    terms = query_terms(query)
    need = _min_hits(len(terms))
    scored = []
    for it in items:
        hay = "%s %s %s" % (it.title, it.snippet, it.site)
        hits = sum(1 for t in terms if t in hay)
        if hits >= need:
            scored.append((hits, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored], terms, need


# ============================================================================
#  成本闸：缓存 + 限速（都是进程级的，够用且好观测）
# ============================================================================
_LOCK = threading.Lock()
_CACHE: dict = {}                                        # key -> (ts, WebOutcome)
_CALLS: deque = deque()                                  # 真实外呼时间戳
_STATS = Counter()


def _cache_get(key: str):
    with _LOCK:
        row = _CACHE.get(key)
        if not row:
            return None
        ts, out = row
        ttl = CACHE_TTL_SEC if out.items else CACHE_TTL_FAIL_SEC
        if time.monotonic() - ts > ttl:
            _CACHE.pop(key, None)
            return None
        _STATS["cache_hit"] += 1
        return out


def _cache_put(key: str, out: WebOutcome) -> None:
    with _LOCK:
        _CACHE[key] = (time.monotonic(), out)
        if len(_CACHE) > 200:                            # 粗暴但有界的清理
            for k in list(_CACHE)[:50]:
                _CACHE.pop(k, None)


def _rate_ok(now: float) -> bool:
    with _LOCK:
        while _CALLS and now - _CALLS[0] > RATE_WINDOW_SEC:
            _CALLS.popleft()
        if len(_CALLS) >= RATE_MAX_CALLS:
            _STATS["rate_limited"] += 1
            return False
        _CALLS.append(now)
        return True


def _bump_calls(out) -> None:
    """记一次**真实外呼**（`out.http_calls` 与进程账本）。

    ★ 必须在**发起调用之前**记，不能等拿到结果再补记 —— 超时/5xx 也真的把请求发出去了，
      少记就等于成本闸说谎。2026-10-17 实测的形状：① 的放宽重搜「第一遍返 0 条 + 第二遍抛异常」
      时走 except 分支，两次外呼**一次都没记**（账本 0，实际 2）⇒ 这是被记录成"已落地"却没写的一段。
    ★ 同时这是**限速之外**的独立账本：限速看 `_CALLS`，成本看这里，两者都要准。
    """
    out.http_calls += 1
    _STATS["http_calls"] += 1


def stats() -> dict:
    """给诊断用（限速/缓存/各原因计数）——也是"降级不是静默的"的一部分。"""
    with _LOCK:
        return dict(_STATS)


def _http_fetch(name: str, query: str) -> str:
    _, url, param = _PROVIDERS[name]
    r = requests.get(url, params={param: query, "ie": "utf-8"},
                     headers={"User-Agent": UA_PC, "Accept-Language": "zh-CN,zh;q=0.9"},
                     timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.text


def _note(reason: str, detail: str = "") -> None:
    """降级记账：走**同一套** `llm_roles.note_degradation` ⇒ 会出现在 `/health/degradations`
    的 by_reason 里（与 LLM 降级同一个出口，不另造一个"看不见的地方"）。"""
    try:
        from llm_roles import note_degradation            # noqa: PLC0415
        note_degradation("websearch", reason, detail)
    except Exception:                                     # noqa: BLE001
        pass


# ============================================================================
#  Tavily（带 key 的 provider）—— 2026-10-16 用户要求「网页搜索都用 tavily」
# ============================================================================
# 选型依据（全部实测，见 SESSION_HANDOFF §39.44.3）：
#   · `search_depth=basic` = **1 credit/次**（advanced 是 2）⇒ 默认取便宜的；
#   · `topic=general` 出**中文**结果，但响应里**没有发布时间字段**（0/5）；
#     `topic=news` 有发布时间（5/5）却**只出英文新闻**（中文话题也一样）⇒ 默认 general；
#   · 两把 key 同参下结果**逐条相同**（6/6 URL 一致）⇒ 语义等价，只用来兜底额度/鉴权失败；
#   · 响应**没有 credits/usage 字段** ⇒ 消耗只能自己记账（本模块记 `_STATS["credits"]`）。
TAVILY_URL = "https://api.tavily.com/search"
TAVILY_KEY_ENVS = ("TAVILY_API_KEY", "TAVILY_API_KEY_2")
TAVILY_DEPTH = "basic"
TAVILY_TOPIC = "general"
_TAVILY_CREDIT = {"basic": 1, "advanced": 2}
# ★ 每条摘要的字符上限。为什么**必须**有它（2026-10-16 实测，见 §39.47）：
#   Tavily 的 `content` 远比 HTML 源的 snippet 长 —— 同一次查询里 74~1418 字符（中位约 970）；
#   不限长时 **5 条正文合计 8664 字符** ⇒ 块在 MAX_BLOCK_CHARS 处被硬切，
#   造成"前端显示 5 条、进 prompt 只剩约 3 条，第 3 条还是半截"——**条数被静默吃掉**。
#   截到 160 后每条约 241 字符（实测）⇒ 5 条共 1588 字符，**5 条全部完整进 prompt**。
TAVILY_SNIPPET_CHARS = 160
# ★ 两档摘要（2026-10-16 用户实测反馈后定稿）：
#   ① 前 `TAVILY_SNIPPET_ITEMS` 条给 `TAVILY_SNIPPET_CHARS`（深读：回答主要靠这几条）；
#   ② 其余条目给 `TAVILY_SNIPPET_CHARS_TAIL`（速览）。
#   ★ 为什么**所有**条目都必须有摘要：曾经让后 14 条只留「标题 + 站点 + 链接」，
#     用户实测的回答就变成"罗列站点名"、不再像总结 ✗ —— 模型手里没有正文，只能报出处。
#     代价参考：20 条只带标题+链接本来就要 ~3190 字符，摘要每多 1 字符就多 1 字符 prompt ⇒
#     两档法既让 20 条都有正文，又只多付约 +3170 token/问（实测推算 4769 字符）。
TAVILY_SNIPPET_ITEMS = 6
TAVILY_SNIPPET_CHARS_TAIL = 60
# 语义检索的 provider：见 search() 里"不套词面闸"的理由
_SEMANTIC_PROVIDERS = {"tavily"}
# 支持"日期过滤"的源（只有它才会在状态帧里显示时间区间；否则就是**谎报**"已按时间过滤"）
_DATE_CAPABLE_PROVIDERS = {"tavily"}
_AUTOREGISTERED = {"done": False}


def _tavily_cfg() -> tuple:
    """(depth, topic)：默认 basic/general，可用 env 覆盖（惰性读，理由同 `_keyed_only()`）。"""
    d = (os.getenv("TAVILY_DEPTH") or TAVILY_DEPTH).strip() or TAVILY_DEPTH
    t = (os.getenv("TAVILY_TOPIC") or TAVILY_TOPIC).strip() or TAVILY_TOPIC
    return d, t


def _tavily_keys() -> list:
    """按顺序取可用的 key：[(环境变量名, 值), ...]（空列表 = 没配）。"""
    out = []
    for n in TAVILY_KEY_ENVS:
        v = (os.getenv(n) or "").strip()
        if v:
            out.append((n, v))
    return out


def _key_fp(v: str) -> str:
    """key 指纹（8 位）——写日志/降级记账用：看得出换了哪把，又不泄露密钥（E3）。"""
    return hashlib.sha256(v.encode()).hexdigest()[:8]


def _tavily_http(payload: dict, headers: dict, timeout):
    """★ 本 provider 的**唯一注入点**（与 HTML 源的 `_http_fetch` 对齐）。

    装置把它换成"读夹具的假函数"⇒ 适配器/映射/记账/闸/渲染**全是真代码**，零真网络零费用。
    """
    r = requests.post(TAVILY_URL, json=payload, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _clip_snippet(text: str, limit: int = TAVILY_SNIPPET_CHARS) -> str:
    """摘要截到 limit（加省略号）。理由见 `TAVILY_SNIPPET_CHARS`：
    不截断会挤爆块闸，把"条数"悄悄吃掉（降级成 3 条半）。"""
    t = (text or "").strip()
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


# ★ 同站点最多留几条（A 项，2026-10-16 用户要求放宽）
#   原来"同站点只留第一条"偏狠：实测一次查询里 `youtube.com` 给了 3 条、`instagram.com` 2 条，
#   它们可能确实是**不同事件**，全砍掉等于白丢来源（那次 19 条被砍成 16 条）。
#   放宽到 2 条：既挡住"同一批学院通知刷屏"，又不至于把不同事件一起杀掉。
TAVILY_MAX_PER_SITE = 2


def _tavily_items(data, top_n: int) -> list:
    """响应 → `list[WebItem]`（形状与 HTML 源完全一致，前端/装置都不用区分来源）。

    ★ 两层预算（见 `TAVILY_SNIPPET_ITEMS` / `TAVILY_SNIPPET_CHARS_TAIL`）：前几条深读、其余速览，
      但**每条都有正文** —— 只给标题会让回答退化成"罗列站点名"（用户实测 ✗）。
    ★ 按**站点**限量：同一站点最多 `TAVILY_MAX_PER_SITE` 条（重复项只会挤掉别的来源），
      限量后**可能不足 top_n 条** —— 那也如实显示（宁可少几条，也不用重复项凑数）。
    """
    out, seen = [], {}                                   # site -> 已收条数（见 TAVILY_MAX_PER_SITE）
    for r in (data or {}).get("results") or []:
        url = (r.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue                                     # 与 HTML 源同一条硬要求：必须是真链接
        title = (r.get("title") or "").strip()
        if not title:
            continue
        site = urlparse(url).netloc or "未知"
        if seen.get(site, 0) >= TAVILY_MAX_PER_SITE:
            continue                                     # 同站点已达上限（理由见 docstring）
        seen[site] = seen.get(site, 0) + 1
        limit = (TAVILY_SNIPPET_CHARS if len(out) < TAVILY_SNIPPET_ITEMS
                 else TAVILY_SNIPPET_CHARS_TAIL)
        out.append(WebItem(title=title, url=url,
                           snippet=(_clip_snippet(_WS_RE.sub(" ", (r.get("content") or "").strip()), limit)
                                    if limit > 0 else ""),
                           site=site, provider="tavily"))
    return out[:top_n]


# ---- 问句里的时间词 ⇒ 绝对日期区间（★ 只在问句**自己**表达明确时间跨度时才过滤）----
# 为什么必须"有条件才加"：日期过滤会把「没更新但权威」的原文页挡掉（评审办法正文常年不改版）
#   ⇒ 用户问「评审办法怎么规定」时若默认加日期，就会**搜不到权威原文**，那正是本知识库的主用途。
# 为什么本机算日期就够：相对时间 ⇒ 绝对区间是**纯算术**，**不需要 LLM**（0 次调用，C2）。
# 为什么「最新/最近」这类**模糊词不触发硬过滤**：它们几乎出现在每个"查最新规定"的问句里，
#   据此把范围压到 30 天等于把权威原文页挡在门外 ⇒ 模糊词只走 **prompt 口径**（见 render_web_block）。
_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_SPAN_PREFIXED_RE = re.compile(r"(?:最近|近|过去的?|前)\s*(\d+|[一两二三四五六七八九十两]+)\s*(天|日|周|个月|月)")
_SPAN_SUFFIXED_RE = re.compile(r"(\d+|[一两二三四五六七八九十两]+)\s*(天|日|周|个月|月)\s*(?:内|以来|之内)")


def _today(now=None):
    """今天。★ **可注入**：装置必须能固定「今天」，否则断言会随真实日期漂移 ⇒ 不确定。**

    （踩过的教训：断言里出现"明天/上个月"这类相对量，等于把测试绑在日历上。）
    """
    return now or datetime.date.today()


def detect_date_range(query: str, now=None) -> tuple:
    """问句 → `(start, end)`（**闭区间、含今天**）；问句没有明确时间跨度 ⇒ `()`。

    ★ `()` 的含义是「**不加任何过滤**」—— 这是默认路径，行为与改造前**逐字一致**。
    明确跨度 = 今天/昨天/前天、最近 N 天|周|个月、这一周/这一月、半年、今年、去年、上个月。
    """
    q = query or ""
    t = _today(now)
    for rx in (_SPAN_PREFIXED_RE, _SPAN_SUFFIXED_RE):
        m = rx.search(q)
        if m:
            raw, unit = m.group(1), m.group(2)
            n = int(raw) if raw.isdigit() else _CN_NUM.get(raw, 0)
            days = n if unit in ("天", "日") else (n * 7 if unit == "周" else n * 30)
            if days >= 1:
                return (t - datetime.timedelta(days=days - 1), t)   # 含今天
    if re.search(r"今天|今日|当天", q):
        return (t, t)
    if re.search(r"昨天|昨日", q):
        return (t - datetime.timedelta(days=1), t - datetime.timedelta(days=1))
    if re.search(r"前天", q):
        return (t - datetime.timedelta(days=2), t - datetime.timedelta(days=2))
    if re.search(r"上一?周|上个?周", q):                    # 上周 = 上周一 ~ 上周日（整周）
        monday = t - datetime.timedelta(days=t.weekday() + 7)
        return (monday, monday + datetime.timedelta(days=6))
    if re.search(r"这一?周|本周|近一周|过去一周", q):
        return (t - datetime.timedelta(days=6), t)
    if re.search(r"上个月|上月", q):
        first = t.replace(day=1) - datetime.timedelta(days=1)        # 上月最后一天
        return (first.replace(day=1), first)
    if re.search(r"这一?个月|本月|近一个月|过去一个月", q):
        return (t - datetime.timedelta(days=29), t)
    if re.search(r"半年", q):
        return (t - datetime.timedelta(days=179), t)
    if re.search(r"今年|本年度", q):
        return (datetime.date(t.year, 1, 1), t)
    if re.search(r"去年|上一年", q):
        last = t.year - 1
        return (datetime.date(last, 1, 1), datetime.date(last, 12, 31))
    return ()


def date_params(query: str, now=None) -> dict:
    """问句 → Tavily 的日期参数；**无明确时间跨度 ⇒ `{}`**（= 请求与改造前逐字相同）。"""
    rng = detect_date_range(query, now)
    if not rng:
        return {}
    return {"start_date": rng[0].isoformat(), "end_date": rng[1].isoformat()}


def date_range_label(query: str, now=None) -> str:
    """给 prompt / 状态帧看的人话区间（空串 = 没加过滤）。"""
    rng = detect_date_range(query, now)
    return "%s ~ %s" % (rng[0].isoformat(), rng[1].isoformat()) if rng else ""


def tavily_search(query: str, top_n: int = MAX_ITEMS, *, keys=None, http=None, timeout=None,
                  dates=None) -> list:
    """Tavily 适配器 → `list[WebItem]`。**失败抛异常**，由 `search()` 统一归类成 reason。

    · 两把 key **依次试**：鉴权/额度类状态（401/403/429/432）**与** 5xx、以及网络层瞬时故障
      （超时 / 连接重置 / DNS，即 `requests.RequestException`）**都**换下一把再试一次（B，2026-10-16 起）。
      ★ 这段原写作「其余错误（超时、5xx）换 key 也没用」—— 那是**改造前**的行为，已过期 ✗；
        真实行为以本函数下面的 `except` 分支为准（`HTTPError` 是 `RequestException` 的子类 ⇒
        两个 except 的**顺序不能反**）。注释与代码不一致会把下一个读代码的人误导（本轮就被旧注释骗过）。
    · 每次成功调用记 `_STATS["credits"] += 1`（basic）与"哪把 key"的计数
      —— 响应里没有用量字段，这是唯一的账本。
    """
    ks = _tavily_keys() if keys is None else list(keys)
    if not ks:
        raise RuntimeError("no_tavily_key")
    do = http or _tavily_http
    depth, topic = _tavily_cfg()
    # ★ dates 三重语义：None = 按问句自动判定（生产路径）；{} = 强制不加（对照实验用）；
    #   非空 dict = 按给定区间（对照实验用）。**默认 None ⇒ 行为与改造前一致。**
    dp = date_params(query) if dates is None else dict(dates or {})
    last_exc = None
    for env_name, key in ks:
        payload = {
            "query": query,
            "max_results": max(1, min(int(top_n or MAX_ITEMS), 20)),
            "search_depth": depth,
            "topic": topic,
            "include_answer": False,
            "include_raw_content": False,
        }
        payload.update(dp)                               # start_date/end_date（仅当问句有明确跨度）
        headers = {"Authorization": "Bearer %s" % key, "Content-Type": "application/json"}
        try:
            data = do(payload, headers, timeout or HTTP_TIMEOUT)
        except requests.HTTPError as e:                   # noqa: PERF203
            code = getattr(getattr(e, "response", None), "status_code", None)
            if code in (401, 403, 429, 432) or (code is not None and 500 <= code < 600):
                last_exc = e
                _note("http_error", "tavily %s %s key=%s" % (code, env_name, _key_fp(key)))
                continue                                  # 换下一把
            raise
        except requests.RequestException as e:
            # ★ B（2026-10-16 用户要求）：网络层瞬时故障（超时 / 连接重置 / DNS）**也换下一把再试一次**。
            #   以前只在鉴权/额度类错误上换 key ⇒ 一次抖动就直接变成「没搜到」✗（用户真实遇到）。
            #   代价：首把失败时多 1 次外呼（≤2 credits）—— 换"抖动不再等于失败"。
            #   ★ 这句必须放在 `except requests.HTTPError` **之后**：HTTPError 也是 RequestException
            #     的子类，顺序反了会把"该 raise 的 4xx"也吞进来换 key。
            last_exc = e
            _note("http_error", "tavily 瞬时失败 %s key=%s" % (type(e).__name__, _key_fp(key)))
            continue
        # ★ 诊断明细（2026-10-16，用户问「为什么前端显示 15 条而不是 20」）：
        #   条数会在**三处**缩水 —— ① Tavily 实际返回数 ② 按站点去重 ③ 条数预算 ⇒
        #   必须分开打印才回答得了"到底哪种"。原来只写「命中 N 条」，查不出来（日志块缓冲还看不到）。
        _raw_n = len((data or {}).get("results") or [])
        items = _tavily_items(data, top_n or MAX_ITEMS)
        # ★ 记账**必须在日志之前**：日志万一出问题，账本也不能漏记（3.72 秒那次的实测就是教训）。
        _STATS["credits"] += _TAVILY_CREDIT.get(depth, 1)
        _STATS["tavily_key_" + _key_fp(key)] += 1
        # ★ 分隔符只用 ASCII（`->`）：GBK 控制台编不了 `⇒`，而"日志炸了"会伪装成"搜索失败"✗
        _log("[联网搜索·tavily] 请求 %d 条 -> 接口返回 %d 条 -> 去重/过滤后 %d 条"
             % (max(1, min(int(top_n or MAX_ITEMS), 20)), _raw_n, len(items)))
        return items
    raise last_exc if last_exc is not None else RuntimeError("no_tavily_key")


def autoregister_tavily(env=None) -> bool:
    """有 Tavily key 就注册（惰性、幂等）；返回最终是否注册成功。

    ★ 刻意**不在 import 期**做这件事：本模块可能被 import 在任何 env 加载之前
      （tier-2 的教训），而且"注册了却没有 key"正是本模块最怕的"点了没反应"。
    """
    if _AUTOREGISTERED["done"]:
        return "tavily" in _EXTRA
    _AUTOREGISTERED["done"] = True
    src = os.environ if env is None else env
    if (src.get("TAVILY_API_KEY") or "").strip() or (src.get("TAVILY_API_KEY_2") or "").strip():
        # ★ ①′（2026-10-17）：这里**必须**用具名函数，不能再用 `lambda` ——
        #   `search()` 靠 `getattr(fn, "accepts_dates", False)` 决定要不要走「带区间 0 条 ⇒ 去掉区间重搜」
        #   那两遍流程，而 **lambda 挂不上属性** ⇒ 判定恒假 ⇒ ① 会**静默失效**（不报错、不生效，
        #   这比报错更坏：记录里写着"已落地"，行为上一次都没发生过）。M14 变异专治这一条。
        def _tp(q, n, dates=None):
            return tavily_search(q, n, dates=dates)
        _tp.accepts_dates = True                         # 能力声明：本适配器真能按给定区间过滤（也能强制不带）
        register_provider("tavily", _tp)
        return True
    return False


# ============================================================================
#  主入口
# ============================================================================
def search(query: str, top_n: int = MAX_ITEMS, providers=None, trace=None,
           fetcher=None) -> WebOutcome:
    """联网搜索。**绝不抛异常**：任何失败都变成"带 reason 的空结论"（降级可见、不静默）。

    `fetcher(name, query) -> html`：注入点。装置/反向对照用它做**真解析 + 假网络**，
    所以本模块的两道闸、限速、缓存都能零 API 地被验证。

    `trace`：传入即把 `trace["web"] = outcome.to_status()` 写好（/chat 的来源帧直接带上）。
    """
    started = time.monotonic()
    q = (query or "").strip()
    out = WebOutcome(attempted=True, query=q, providers_tried=[])
    if not q:
        out.reason = "empty_query"
        _finish(out, started, trace)
        return out

    key = "%s|%d" % (q, top_n)
    cached = _cache_get(key)
    if cached is not None:
        # ★ 缓存命中也要写 trace（前端/装置看到的是同一份形状），但标记 cached=True
        hit = WebOutcome(**{**cached.__dict__})
        hit.cached = True
        hit.elapsed_ms = int((time.monotonic() - started) * 1000)
        _finish(hit, started, trace)
        return hit

    per_provider = []                                    # [(源名, 该源的原因)] —— 逐源都记，不互相覆盖
    for name in order(providers):
        out.providers_tried.append(name)
        if name not in _PROVIDERS and name not in _EXTRA:
            continue
        if not _rate_ok(time.monotonic()):
            per_provider.append((name, "rate_limited"))
            _note("rate_limited", "%d/%d per %.0fs" % (len(_CALLS), RATE_MAX_CALLS, RATE_WINDOW_SEC))
            break
        dates_dropped = False                            # ① 是否走"去掉时间范围"的第二遍（诚实呈现要用）
        try:
            if name in _EXTRA:
                fn = _EXTRA[name]
                # ★ ①（2026-10-17 用户批准）：带时间范围却 **0 条** ⇒ 去掉时间范围**放宽重搜一次**。
                #   动机（实测，不是猜的）：同一条问句"带区间 0 条 / 不带区间 19 条"在生产日志与探针里
                #   各出现过一次；而 2026-10-17 的 A/B 用**生产参数**没能复现那个 0 条（同 query+params
                #   这次给 20 条；★ 注意**采样只有 1 次**，按 B6 不能据此说「稳定」⇒ 只能说"未复现"）
                #   ⇒ 更可能是**偶发**（真因仍未测出，"未复现"不等于"已找到原因"）。偶发不能变成
                #   用户看到的「没搜到」✗
                #   ⇒ 多花 ≤1 credit 把它兜住。只在"带区间且第一遍 0 条"时发生 ⇒ 正常路径一分不多花。
                if getattr(fn, "accepts_dates", False) and date_params(q):
                    _bump_calls(out)                                  # 第一遍：按问句带时间范围
                    items = list(fn(q, top_n, dates=None) or [])
                    if not items:
                        # ★ 第二次外呼也要过限速闸（`_rate_ok` 是"每次调用记一次"）——
                        #   漏了它，限速账本会少算一次（成本闸就形同虚设）。
                        if _rate_ok(time.monotonic()):
                            _bump_calls(out)                          # 第二遍：强制不带日期
                            relaxed = list(fn(q, top_n, dates={}) or [])
                            # ★ 日志写在**两遍都有结果之后**：原来那句「再试一次」打在第二遍之前 ⇒
                            #   第二遍若抛异常，日志会显得"放宽过"，而事实是没拿到东西（日志也会说谎）。
                            _log("[联网搜索] 首选源带时间范围 0 条 -> 去掉时间范围重搜：返回 %d 条"
                                 "（多 1 次外呼，见 ① / 2026-10-17）" % len(relaxed))
                            if relaxed:
                                items, dates_dropped = relaxed, True
                else:
                    _bump_calls(out)
                    items = list(fn(q, top_n) or [])
                # ★ keyed 源同样是**一次真实外呼**：实测踩到 Tavily 调用成功后 `http_calls` 还是 0
                #   ⇒ 界面与诊断会以为「没花钱」。记账已由 `_bump_calls()` 在调用前完成。
            else:
                _bump_calls(out)                                      # HTML 源：抓页面 = 一次真实外呼
                html = (fetcher(name, q) if fetcher else _http_fetch(name, q))
                items = _PROVIDERS[name][0](html, top_n)
        except requests.Timeout:
            per_provider.append((name, "timeout"))
            _note("timeout", name)
            continue
        except Exception as e:                            # noqa: BLE001
            per_provider.append((name, "http_error"))
            _note("http_error", "%s: %s" % (name, type(e).__name__))
            continue

        if not items:
            # ★ ④（2026-10-17 用户批准）：**区分两种「0 条」** ——
            #   keyed 语义源（Tavily）是「它真的没返回结果」；HTML 源是「抓到页面但解析不出」。
            #   以前两种都记成 `parse_empty` ⇒ 用户看到「结果页结构变了，解析不出结果」，
            #   而事实是主源返回 0 条 ✗（这就是本次的真实误导）。原因码必须如实。
            r0 = "no_results" if name in _SEMANTIC_PROVIDERS else "parse_empty"
            per_provider.append((name, r0))
            _note(r0, name)
            continue

        if name in _SEMANTIC_PROVIDERS:
            # ★ keyed 语义检索**不套词面闸**：那道闸是为识破 HTML 诱饵页("结构完美但全无关")而生；
            #   对语义检索会**误杀**——Tavily 按语义匹配，摘要里未必字面出现查询词。
            #   代价：这一路少了一道防线 ⇒ 由"带凭据的 API + 只吃摘要 + 块内使用规则"兜。
            kept, terms, need = list(items), query_terms(q), 0
        else:
            kept, terms, need = relevance_filter(q, items)
        if not kept:
            # ★ 抓到结果但全不相关 = 本模块最想抓住的那种源（Bing 诱饵页）。
            per_provider.append((name, "irrelevant"))
            _note("irrelevant", "%s: 抓到 %d 条，命中< %d 词 ⇒ 全丢弃"
                  % (name, len(items), need))
            continue

        out.items = kept
        out.provider = name
        out.reason = None
        out.dates_relaxed = dates_dropped                 # ①：这份结果是「放宽时间范围」那一遍拿到的
        if name in _DATE_CAPABLE_PROVIDERS and not dates_dropped:
            # ★ 只有"支持日期过滤的源"才写区间：HTML 源根本没这能力，写了就是**谎报**
            #   （同一纯函数、同一入参 ⇒ 与适配器真正发出去的参数必然一致）
            # ★ `and not dates_dropped` 是 ① 的**诚实条件**（2026-10-17 修订）：放宽那一遍一个日期参数
            #   都没发出去，若仍把区间写进块头，模型会读到「已按此区间过滤；区间外的结果不会出现在
            #   这里」⇒ 它会理直气壮地把不限时间的旧内容当「最近一周」讲 ✗（比原来的「没搜到」更坏：
            #   前者用户看得出，后者看不出来）。所以宁可写「未限定 + 说明时效无法保证」（见 _block_head）。
            out.date_range = date_range_label(q)
        # ★ 条数预算：必须**在 date_range 写好之后**算 —— 块头里含"检索时间范围"那一行，
        #   先算会少算几十字符 ⇒ 可能多塞一条 ⇒ 又被安全网硬切（正是本轮要修的坑）。
        #   预算后 outcome.items 就是"进 prompt 的那一份"，前端/状态帧同源 ⇒ 如实一致。
        out.items_kept = len(kept)                       # 诊断用：预算裁剪**前**的条数（见 _finish）
        out.items = budget_fit_items(out, kept)
        out.tried_reasons = per_provider
        _STATS["ok"] += 1
        _STATS["provider_" + name] += 1
        _cache_put(key, out)
        _finish(out, started, trace)
        return out

    out.tried_reasons = per_provider
    # ★ 最终原因按【信息量优先级】挑，而不是"最后一个源说了算"：
    #   首选源"搜到了但全不相关"（irrelevant）比次选源"结构没解析出来"（parse_empty）
    #   更具体、更可行动；逐源原因仍全在 tried_reasons 里，不丢信息。
    out.reason = _pick_reason(per_provider)
    _STATS["fail"] += 1
    _cache_put(key, out)                                  # 失败也只缓存 5 秒（②，2026-10-17 由 60 秒压下）
    _finish(out, started, trace)
    return out


def _log(msg: str) -> None:
    """打日志 —— **绝不允许日志把功能搞坏**（2026-10-16 用一整轮排查换来的教训）。

    生产进程的 stdout 是 **GBK（cp936）控制台**：中文能编（显示成乱码但写得出去），
    但 `⇒`（U+21D2）这类字符**不在 GBK 里** ⇒ `print` 抛 `UnicodeEncodeError`
    ⇒ 被 `search()` 的逐源 except 兜成 `http_error` ⇒ 用户看到「搜索引擎请求失败」✗✗。
    更阴的是：所有装置与探针都带 `PYTHONIOENCODING=utf-8` ⇒ **只有生产会炸**（B3：模拟≠真实）。

    所以：能编就编，编不了就替换字符再写，实在不行就**闭嘴** —— 功能优先于日志。
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        try:
            import sys                                     # noqa: PLC0415
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:                                  # noqa: BLE001
            pass
    except Exception:                                      # noqa: BLE001
        pass


def _finish(out: WebOutcome, started: float, trace) -> None:
    out.elapsed_ms = int((time.monotonic() - started) * 1000)
    if isinstance(trace, dict):
        # ★ 不改 attempted：那是"有没有检索知识库"的信号（§39.29）。网络搜索写在独立键里。
        trace["web"] = out.to_status()
    if out.reason:
        # ★ 一律走 `_log()`：日志编码不了也**绝不能**影响功能（见 _log 的 docstring）。
        _log("[联网搜索] 未得到可用结果：%s（%s）" % (out.reason, REASON_TEXT.get(out.reason, "")))
    else:
        _log("[联网搜索] %s 命中 %d 条（%d ms）" % (out.provider, len(out.items), out.elapsed_ms))
    # ★ 明细行（2026-10-16 起；★ 2026-10-17 修订：**从成功分支里搬出来**）——
    #   原来它只写在 `else`（成功）分支里 ⇒ **恰恰故障时没有线索**：看不到"到底发了什么查询、试过哪些源、
    #   是不是压根没外呼"。本轮那次「Tavily 返回 0 条」就是被这个坑住的（③ 的由来）。
    #   分隔符只用 ASCII/GBK 安全字符（不要 `⇒`/`→`）—— 见 _log。
    _log("[联网搜索·明细] 查询=%s | 时间范围=%s | 预算前 %s 条 -> 进 prompt %d 条（闸 %d 字符）"
         "| 外呼 %d 次（%s）| %s | 试过 %s"
         % (out.query or "?", out.date_range or "未限定",
            getattr(out, "items_kept", "?"), len(out.items), MAX_BLOCK_CHARS,
            out.http_calls,
            "放宽时间范围后重搜" if getattr(out, "dates_relaxed", False) else "按原条件",
            "缓存命中" if out.cached else ("本次新查" if out.http_calls else "本次未外呼"),
            "-".join(out.providers_tried or []) or "无"))
    # ★ 逐源原因（④ 的另一半）：两种「0 条」必须都能看见。以前失败时只打一句**按优先级挑出来的**最终
    #   原因 ⇒ 逐源真相全在 `tried_reasons` 里却没人打，用户和排查者都看不到主源到底说了什么。
    if out.tried_reasons:
        _log("[联网搜索·逐源] %s"
             % " ; ".join("%s=%s" % (nm, rs) for nm, rs in out.tried_reasons))


def _block_head(outcome) -> str:
    """块头（含使用规则）。

    ★ 单独抽出来**是为了能测量它占多少字符**：`budget_fit_items()` 要用「总闸 − 块头」
      来算能装几条（2026-10-16 实测纯块头 = **331** 字符；别把 383 当块头 ——
      383 是"0 条时渲染"多出来的那句「没搜到」说明的长度）。它含"检索时间范围"那一行 ⇒
      预算**必须在 `date_range` 写好之后**算，否则少算几十字符、多塞一条、又被硬切。
    """
    head = ("【网络搜索结果（★ 来自公开网页，**不是**学校文件）】\n"
            "检索日期：%s\n" % _today().isoformat())
    if getattr(outcome, "date_range", ""):
        head += ("检索时间范围：%s（★ 问句含**明确时间跨度**，已按此区间过滤；区间外的结果不会出现在这里）\n"
                 % outcome.date_range)
    elif getattr(outcome, "dates_relaxed", False):
        # ★ ① 的**第三种情形**（2026-10-17 修订）：问句**有**明确时间跨度，但按该区间一条都没搜到
        #   ⇒ 已去掉时间范围重搜过了。这里必须如实告诉模型「没按时间过滤」✗→✓，否则它会把这些
        #   不限时间的旧内容当「最近一周」讲。**不要**与上面那句合并：那句明写"已按此区间过滤"，
        #   是一个硬承诺，两种情形必须能机械区分（装置 ⑩ 就是钉这一条）。
        head += ("检索时间范围：**未限定**（★ 问句含明确时间跨度，但按该区间**没有搜到任何结果**，"
                 "以下是**放宽时间**后的结果 ⇒ 回答里必须说明「未能限定在你要的时间范围内，时效无法保证」）\n")
    else:
        head += ("检索时间范围：未限定（★ 问句没有明确时间跨度。若用户问的是「最新/最近」动态，"
                 "请在回答里说明这一点无法保证时效）\n")
    head += ("使用规则（必须遵守）：\n"
             "  · 下面的内容**不是**本校/教育部的正式文件，**绝不能**当作奖学金/综测规则的依据；\n"
             "  · **不要**给它标【1】【2】这类编号 —— 编号只属于学校文件（本段网页内容一律不编号）；\n"
             "  · 需要提及时写成「网站名 + 标题」的自然表述，并说明「以下来自网络、请以学校正式文件为准」；\n"
             "  · 网络结果与学校文件冲突时，**一律以学校文件为准**，并指出冲突；\n"
             "  · 网络结果只是搜索摘要，可能不完整或过时。\n")
    # ★ 2026-10-16 用户实测的两条补丁（都来自真实失败，不是猜的）：
    #   ① 会话 39 msg174：模型把**当天**搜到的东西说成「未来时间段」并拒答
    #      ⇒ 把"今天就是 X、这些就是当下的真实信息"写进块里；
    #   ② 用户问「总结」时，回答变成罗列站点名+标题（因为条目只有标题）
    #      ⇒ 明确要求"写成有条理的总结"，同时把"只罗列出处"点名为反面。
    head += ("  · **今天是 %s**；下面就是**当下的真实信息** —— 不要以「超出我的知识范围」"
             "「那是未来时间」为由回避或拒答；\n" % _today().isoformat())
    head += ("  · 用户要「总结 / 梳理 / 盘点」时，请据此写成**有条理的总结**（可分类、可列要点），"
             "**不要只罗列站点名和标题**；\n")
    return head


def _render_item_line(i: int, it) -> str:
    """单条渲染 —— prompt 与「条数预算」共用**同一个格式**（分开写会把预算算错）。"""
    return ("%d. %s（来源站点：%s）\n   链接：%s\n   摘要：%s"
            % (i, it.title, it.site or "未知", it.url, it.snippet or "（无摘要）"))


def budget_fit_items(outcome, items) -> list:
    """★ 按**整条**装进 `MAX_BLOCK_CHARS`：能装几条给几条（绝不切半截）。

    为什么不是"全渲染完再切字符串"（原做法，2026-10-16 改掉）：
      硬切会让**前端显示 5 条、进 prompt 只剩约 3 条**（第 3 条还切在随机位置）——
      用户看到的与模型依据的**不一致**，丢信息还不可见。
      现在 `outcome.items` **本身就是**进 prompt 的那一份 ⇒ prompt / 状态帧 / 前端
      必然是同一份（用户 2026-10-16 要求"如实给"，这条是它的机械保证）。
    为什么至少留 1 条：宁可那一条被安全网截断，也不能让"搜到了"变成"没搜到"
      （后者会让回答模型谎报"没搜到"）。
    """
    items = list(items or [])
    if not items:
        return []
    used = len(_block_head(outcome)) + 1                 # +1 = 末尾换行
    out = []
    for i, it in enumerate(items, 1):
        ln = len(_render_item_line(i, it)) + 1           # +1 = 与下一条之间的换行
        if out and used + ln > MAX_BLOCK_CHARS:
            break
        used += ln
        out.append(it)
    return out


def render_web_block(outcome) -> str:
    """把结论渲染成给回答模型的文本块。

    ★ 失败/空结果也**必须给一段话**：诚实规则要求"没搜到就说没搜到"，
      而不是让模型自己编一个"网络来源"（那比不搜更糟）。
    ★ 使用规则与块**同进同出**：没开联网搜索时返回 ""，prompt 与改造前**逐字相同**
      （这条性质由装置钉着，不是靠自觉 —— 沿用 §39.35/§39.36 的"机械插槽"手法）。
    ★ 条数由 `budget_fit_items()` 在 search() 里先定好 ⇒ 这里的字符串硬切只是**安全网**
      （正常情况下不该触发；装置里有一条"渲染后不得出现（网络结果已截断）"钉着）。
    """
    if outcome is None or not getattr(outcome, "attempted", False):
        return ""
    head = _block_head(outcome)
    if not outcome.items:
        text = head + ("本次联网搜索**没有找到可用结果**（原因：%s）。"
                       "回答时如实说明「没搜到」，**不要编造网络来源**。\n"
                       % REASON_TEXT.get(outcome.reason or "", outcome.reason or "未知"))
        return text

    lines = [head]
    for i, it in enumerate(outcome.items, 1):
        lines.append(_render_item_line(i, it))
    text = "\n".join(lines) + "\n"
    if len(text) > MAX_BLOCK_CHARS:                       # ★ token 成本闸（`budget_fit_items` 之后不该再触发）
        text = text[:MAX_BLOCK_CHARS].rstrip() + "\n（网络结果已截断）\n"
    return text