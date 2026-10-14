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

import html as html_mod
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
MAX_ITEMS = 5                    # 进 prompt / 给前端的条数上限
MAX_BLOCK_CHARS = 2000           # 网络块进 prompt 的硬上限（token 成本闸）
HTTP_TIMEOUT = (4.0, 8.0)        # (连接, 读取) —— 单源最坏 8 秒
CACHE_TTL_SEC = 300.0            # 成功结论缓存 5 分钟
CACHE_TTL_FAIL_SEC = 60.0        # 失败结论只缓存 1 分钟
RATE_WINDOW_SEC = 60.0
RATE_MAX_CALLS = 12              # 每分钟最多 12 次真实外呼

PROVIDER_LABELS = {"sogou": "搜狗", "so360": "360搜索", "tavily": "Tavily", "serper": "Serper"}

# 降级原因 → 给用户看的人话（进 /health/degradations 的 legend，见 fastapi_project.py）
REASON_TEXT = {
    "no_results": "搜索引擎没有返回结果",
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
_REASON_PRIORITY = ("rate_limited", "http_error", "timeout", "irrelevant",
                    "parse_empty", "no_results", "empty_query")


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
    """provider 顺序：显式指定 > 已注册的 keyed provider > 默认链。"""
    if providers:
        return list(providers)
    return list(_EXTRA.keys()) + list(DEFAULT_PROVIDERS)


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
        try:
            if name in _EXTRA:
                items = list(_EXTRA[name](q, top_n) or [])
            else:
                html = (fetcher(name, q) if fetcher else _http_fetch(name, q))
                out.http_calls += 1
                _STATS["http_calls"] += 1
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
            per_provider.append((name, "parse_empty"))
            _note("parse_empty", name)
            continue

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
    _cache_put(key, out)                                  # 失败也只缓存 1 分钟（见 _cache_get）
    _finish(out, started, trace)
    return out


def _finish(out: WebOutcome, started: float, trace) -> None:
    out.elapsed_ms = int((time.monotonic() - started) * 1000)
    if isinstance(trace, dict):
        # ★ 不改 attempted：那是"有没有检索知识库"的信号（§39.29）。网络搜索写在独立键里。
        trace["web"] = out.to_status()
    if out.reason:
        print("[联网搜索] 未得到可用结果：%s（%s）" % (out.reason, REASON_TEXT.get(out.reason, "")))
    else:
        print("[联网搜索] %s 命中 %d 条（%d ms）" % (out.provider, len(out.items), out.elapsed_ms))


def render_web_block(outcome) -> str:
    """把结论渲染成给回答模型的文本块。

    ★ 失败/空结果也**必须给一段话**：诚实规则要求"没搜到就说没搜到"，
      而不是让模型自己编一个"网络来源"（那比不搜更糟）。
    ★ 使用规则与块**同进同出**：没开联网搜索时返回 ""，prompt 与改造前**逐字相同**
      （这条性质由装置钉着，不是靠自觉 —— 沿用 §39.35/§39.36 的"机械插槽"手法）。
    """
    if outcome is None or not getattr(outcome, "attempted", False):
        return ""
    head = ("【网络搜索结果（★ 来自公开网页，**不是**学校文件）】\n"
            "使用规则（必须遵守）：\n"
            "  · 下面的内容**不是**本校/教育部的正式文件，**绝不能**当作奖学金/综测规则的依据；\n"
            "  · **不要**给它标【1】【2】这类编号 —— 编号只属于学校文件（本段网页内容一律不编号）；\n"
            "  · 需要提及时写成「网站名 + 标题」的自然表述，并说明「以下来自网络、请以学校正式文件为准」；\n"
            "  · 网络结果与学校文件冲突时，**一律以学校文件为准**，并指出冲突；\n"
            "  · 网络结果只是搜索摘要，可能不完整或过时。\n")
    if not outcome.items:
        text = head + ("本次联网搜索**没有找到可用结果**（原因：%s）。"
                       "回答时如实说明「没搜到」，**不要编造网络来源**。\n"
                       % REASON_TEXT.get(outcome.reason or "", outcome.reason or "未知"))
        return text

    lines = [head]
    for i, it in enumerate(outcome.items, 1):
        lines.append("%d. %s（来源站点：%s）\n   链接：%s\n   摘要：%s"
                     % (i, it.title, it.site or "未知", it.url, it.snippet or "（无摘要）"))
    text = "\n".join(lines) + "\n"
    if len(text) > MAX_BLOCK_CHARS:                       # ★ token 成本闸
        text = text[:MAX_BLOCK_CHARS].rstrip() + "\n（网络结果已截断）\n"
    return text