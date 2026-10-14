"""模型角色的【唯一构造点】—— 角色→后端映射、429 重试退避、JSON 容错解析、降级计数。

为什么要有这个文件（沿用 §19 / §21.3 的手法）：
  §19 那个真 bug 的根因是"同一个目录在两个模块里有两种写法" —— 路径靠"记得写一样"维持，
  结构上必然出错。模型名/base_url/key 现在是同一个问题：原先硬编码在 6 处以上。
  把"角色 → 后端"集中到这里，换成"写法不一致在结构上无法发生"。

本文件同时承载三件【必须集中】的事（每条都有实测依据）：

1) 角色映射：classify / rewrite / answer / direct 各自一个后端，可逐角色替换。
2) 429 重试退避：实测魔搭免费额度是【按模型限流】的（不只是按额度 403）——
   classify 顺序连打实测 3/20 触发 429；带重试后退避 1s/2s 全部恢复。
   生产 classify 原先没有 try/except，429 会直接冒到 SSE 变成"发生错误:…"。
3) JSON 容错解析 + 可见降级：原实现只 replace("'''json","")/replace("'''","")，
   剥不掉 ``` 反引号围栏 → json.loads 抛异常 → rewrite() 静默降级成单查询（无任何报错）。
   实测 Qwen 在关思考模式下出现过 1/26 次围栏 —— 靠"换模型躲开"躲不掉（静默失效），
   靠"剥围栏"是确定性修复。
"""

from __future__ import annotations

import json
import os
import re
import time

from langchain_openai import ChatOpenAI

from config import ROLE_MODELS
from key_pool import KeyPool, mask

# ★ key 池注册表：同级 const 不是"按角色"，而是"按环境变量组" ——
#   因为 config.py 里四个角色都指向 MS_API_KEY，共享同一个池。
#   这个选择很重要：同一个 key 在不同角色间是【同一份额度】，
#   若按角色各建一个池，classify 耗尽 key1 之后 rewrite 还会去白撞 key1。
_POOL_CACHE: dict[tuple[str, ...], KeyPool] = {}
# 模型对象 → 它的池（用 id() 做键：ChatOpenAI 是 pydantic 模型，不接受任意属性写入）
_MODEL_POOL: dict[int, KeyPool] = {}


def key_env_names(base_env: str, *, max_keys: int = 8) -> tuple[str, ...]:
    """把一个 `MS_API_KEY` 展开成候选池：MS_API_KEY, MS_API_KEY_2, MS_API_KEY_3, …

    为什么不新增配置项：`ROLE_MODELS` 只需写主变量名，**加一个备用 key 就少一处要改的配置**；
    而"配了但为空的备用 key"会被池自动跳过，不会误判。
    """
    return (base_env,) + tuple("%s_%d" % (base_env, i) for i in range(2, max_keys + 1))


def pool_for(base_env: str) -> KeyPool:
    names = key_env_names(base_env)
    if names not in _POOL_CACHE:
        _POOL_CACHE[names] = KeyPool(names, label=base_env)
    return _POOL_CACHE[names]


def get_pool(model) -> KeyPool | None:
    """取某个模型对象所属的 key 池（供 invoke_with_retry 轮换用）。"""
    return _MODEL_POOL.get(id(model))


def _pool_of_chain(chain) -> KeyPool | None:
    """从 RunnableChain 里找出挂有池的那个 LLM。

    classify 走的是 `classify_chain = classify_prompt | classify_model | StrOutputParser()`，
    换 key 必须作用在【链里的模型】上，而不是链本身。逐个成员查注册表即可。
    """
    if chain is None:
        return None
    for part in getattr(chain, "steps", None) or []:
        p = _MODEL_POOL.get(id(part))
        if p is not None:
            return p
    for attr in ("first", "last", "bound", "model"):
        p = _MODEL_POOL.get(id(getattr(chain, attr, None)))
        if p is not None:
            return p
    return None


def pool_status(role: str | None = None) -> str:
    """可观测：打印池状态（不泄漏完整 key）。role 为空则打印所有池。"""
    if role and role in ROLE_MODELS:
        return pool_for(ROLE_MODELS[role]["api_key_env"]).describe()
    return " ; ".join(p.describe() for p in _POOL_CACHE.values()) or "（尚无池）"


# 降级计数：把"静默降级"变成"可观测降级"（§23.1 / §29.6 的教训）
_DEGRADED: dict[str, int] = {}


def note_degradation(role: str, reason: str, detail: str = "") -> None:
    key = "%s:%s" % (role, reason)
    _DEGRADED[key] = _DEGRADED.get(key, 0) + 1
    print("[降级] role=%s reason=%s 累计=%d %s" % (role, reason, _DEGRADED[key], detail))


def degraded_counts() -> dict[str, int]:
    return dict(_DEGRADED)


def make_model(role: str, **kwargs):
    """按角色造模型。返回 (model, cfg)；缺少对应 key 时【立即报错】，不静默退化成别的后端。

    若配置了 `MS_API_KEY_2` / `MS_API_KEY_3` …，会自动组成 key 池，
    池会注册到本模块，供 `invoke_with_retry` 在额度耗尽时自动轮换
    （生产接线方式：把 `with_retry(lambda: chain.invoke(x))` 换成 `invoke_with_retry`）。
    """
    if role not in ROLE_MODELS:
        raise KeyError("未定义的角色 %r；已定义: %s" % (role, sorted(ROLE_MODELS)))
    cfg = ROLE_MODELS[role]
    pool = pool_for(cfg["api_key_env"])
    if len(pool) == 0:
        raise RuntimeError(
            "角色 %s 需要环境变量 %s（及可选的 %s），但它们都为空（拒绝静默换后端）"
            % (role, cfg["api_key_env"], "/".join(pool.alt_env_names)))
    env_name, key = pool.current()
    model = ChatOpenAI(model=cfg["model"], api_key=key, base_url=cfg["base_url"],
                       extra_body=cfg.get("extra_body"), **kwargs)
    _MODEL_POOL[id(model)] = pool
    return model, dict(cfg)


_RETRIABLE = ("429", "RateLimit", "500", "502", "503", "504", "Timeout", "timeout",
              "APIConnectionError", "InternalServerError")

# ⚠️ 永久性失败：重试【不可能】成功，且必须优先于 _RETRIABLE 判断。
#
# 为什么必须加这个（§35.7 实测）：额度耗尽返回的是
#     RateLimitError: Error code: 429 - {'error': {'message': 'insufficient balance'}}
# 文本里同时含 "RateLimit" 和 "429"，于是被 _RETRIABLE 判为"可重试" ——
# 一次实验白重试 123 次，并把"额度已耗尽"误读成"只是限流"，多跑了一整批作废数据。
#
# 注意：不 import mskeys 的同类常量 —— 那会让生产库 llm_roles 反向依赖开发用的
# key 池模块（并触发它的 .env 加载副作用）。此处独立列出，由
# test_permanent_failure.py 交叉校验两份清单一致。
_PERMANENT = (
    # 额度 / 计费（换 key 或充值才有用，同 key 重试无意义）
    "insufficient balance",
    "Free quota exhausted",
    "free tier only",
    "quota exceeded",
    "exceeded your current quota",
    "余额不足",
    "arrearage",
    "欠费",
    # 鉴权（key 本身无效）
    "invalid api key",
    "InvalidApiKey",
    "AuthenticationError",
    "Incorrect API key",
    "unauthorized",
)


def is_permanent_error(exc_or_text) -> bool:
    """判断是否为【重试无意义】的永久性失败（额度耗尽 / 鉴权失败）。"""
    text = exc_or_text if isinstance(exc_or_text, str) else \
        "%s %s" % (type(exc_or_text).__name__, exc_or_text)
    return any(tok in text for tok in _PERMANENT)


def with_retry(call, *, role: str, max_attempts: int = 3, base_wait: float = 1.0,
               on_permanent=None, note_permanent: bool = True):
    """429 / 5xx / 连接类错误自动重试退避。返回 (结果, 实际尝试次数)。

    永久性失败（额度耗尽 / 鉴权）：默认【立即放弃】，不消耗重试次数 —— 见 _PERMANENT 注释。
    但若传入 `on_permanent` 且它返回 True，则【视为可重试】——这是 key 轮换的挂载点：
    调用方用它在换到备用 key 之后重试同一请求（换 key 后原错误确实可能消失）。
    """
    last = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call(), attempt
        except Exception as exc:                       # noqa: BLE001 —— 需要按内容判断是否可重试
            last = exc
            text = "%s %s" % (type(exc).__name__, exc)

            # ★ 先判永久性失败：这类错误重试必然再失败，且会被 429/RateLimit 文本误判为可重试
            if is_permanent_error(text):
                if note_permanent:
                    note_degradation(role, "permanent_fail",
                                     "第%d次即永久性失败(%s)，不重试：%s"
                                     % (attempt, type(exc).__name__, text[:90]))
                # 挂载点：调用方可能已经/正要换 key —— 换 key 后重试是有意义的
                if on_permanent is not None and on_permanent(exc) and attempt < max_attempts:
                    continue
                break

            if not any(tok in text for tok in _RETRIABLE) or attempt == max_attempts:
                break
            wait = base_wait * (2 ** (attempt - 1))
            note_degradation(role, "retry", "第%d次失败(%s)，%.1fs 后重试"
                             % (attempt, type(exc).__name__, wait))
            time.sleep(wait)
    raise last


def _swap_key(model, key: str) -> None:
    """把新 key 真正写进发请求的客户端。

    ⚠️ 只改 `model.openai_api_key` 在 langchain_openai 下【无效】：构造时 key 已被
    固化到底层 `openai.OpenAI` 实例（`root_client.api_key`）。实测见 API_KEYS.md §3.5：
    属性变了但请求仍用旧 key，表现为"换了 key 还报额度耗尽"。
    """
    try:
        from pydantic import SecretStr
        model.openai_api_key = SecretStr(key)          # 属性同步（保持一致性）
    except Exception:                                  # noqa: BLE001
        pass
    for attr in ("root_client", "root_async_client"):
        client = getattr(model, attr, None)
        if client is not None and hasattr(client, "api_key"):
            client.api_key = key                       # ★ 真正生效的那一行


def invoke_with_retry(model_or_chain, prompt, *, role: str, max_attempts: int = 3):
    """invoke + 重试 + **额度耗尽自动换 key**。也接受 RunnableChain（如 classify_chain）。

    这是生产侧的唯一推荐入口。相比 `with_retry(lambda: model.invoke(x))`，
    它额外做到：命中永久性失败（额度耗尽/key 无效）时
    ① 把当前 key 标记耗尽；② 换到池中下一个 key 并重写底层客户端；③ 重试同一请求；
    ④ 池也用完时抛 `AllKeysExhausted` —— 此时才该告知用户。

    ★ 池是按"模型对象"注册的。传 Chain 时，池要从其内部的模型取 ——
      所以优先用 `model_or_chain` 自身的 id 查表，查不到再尝试其 `.first`/`.steps` 里的模型。
    """
    pool = get_pool(model_or_chain)
    if pool is None:
        pool = _pool_of_chain(model_or_chain)
    # ★ 换 key 必须作用在【真正持有客户端的那个对象】上：
    #   直接传模型时就是它自己；传 Chain 时是链里的模型（如 classify_chain）。
    target = model_or_chain if get_pool(model_or_chain) is not None else None
    if target is None:
        for part in getattr(model_or_chain, "steps", None) or []:
            if get_pool(part) is not None:
                target = part
                break
    swaps = {"n": 0}

    def on_permanent(exc) -> bool:
        nonlocal pool
        if pool is None or target is None:
            return False
        # ★ 协议（被 test_key_rotation 连抓四次才理清）：
        #   只有【一个】换 key 入口 —— `exhaust_current()`。
        #   它同时完成"标记当前 key 已死"与"把 _active 前移到下一个活的"，
        #   两者不可分离：分开会让池里最后一个 key 永远不被标记，
        #   all_exhausted() 恒假、AllKeysExhausted 永不抛。
        #   ⚠️ 也不要用 can_rotate() 做"还能不能继续"的判断 ——
        #   它的语义是"除当前之外还有备用 key 吗"，与"换完还有得用吗"不是一回事。
        env_name, key = pool.exhaust_current()
        if key is None:
            if pool.all_exhausted():
                note_degradation(role, "all_keys_exhausted",
                                 "池内 key 全部耗尽：%s" % pool.describe())
            return False
        swaps["n"] += 1
        _swap_key(target, key)
        note_degradation(role, "key_rotated",
                         "第%d次换 key → %s（%s）" % (swaps["n"], env_name, mask(key)))
        return True

    try:
        if pool is not None:
            cur = pool.current()
            note_degradation(role, "key_used",
                             "本次使用 %s（%s）" % (cur[0], mask(cur[1] or "")))
        return with_retry(lambda: model_or_chain.invoke(prompt), role=role,
                          max_attempts=max_attempts, on_permanent=on_permanent,
                          note_permanent=False)
    except Exception as exc:                           # noqa: BLE001
        if pool is not None and pool.all_exhausted():
            note_degradation(role, "permanent_fail",
                             "永久性失败且池已耗尽：%s" % "%s %s" % (type(exc).__name__, str(exc)[:70]))
            raise AllKeysExhausted(
                "角色 %s：key 池全部耗尽（%s）。请告知用户——需要新 key 或等额度过期恢复。"
                % (role, pool.describe())) from exc
        note_degradation(role, "permanent_fail",
                         "永久性失败(%s)：%s" % (type(exc).__name__, str(exc)[:90]))
        raise


class AllKeysExhausted(RuntimeError):
    """池内所有 key 都不可用 —— 只有这时才应该让用户知道。"""


_FENCE_HEAD = re.compile(r"^\s*```[A-Za-z0-9_+-]*\s*")
_FENCE_TAIL = re.compile(r"\s*```\s*$")


def loads_json_list(text) -> list[str] | None:
    """把模型输出解析成字符串列表，【容错】剥掉 ``` 与 ''' 围栏。

    只做"剥壳"，不做意义修补：解析不出来就返回 None，由调用方记一次可见降级。
    """
    if not isinstance(text, str) or not text.strip():
        return None
    s = text.strip().replace("'''json", "").replace("'''", "")
    s = _FENCE_HEAD.sub("", s)
    s = _FENCE_TAIL.sub("", s).strip()
    value = None
    try:
        value = json.loads(s)
    except Exception:                                  # noqa: BLE001
        match = re.search(r"\[.*\]", s, re.S)          # 兜底：从夹杂文字里抠出数组
        if match:
            try:
                value = json.loads(match.group(0))
            except Exception:                          # noqa: BLE001
                return None
    if not isinstance(value, list):
        return None
    items = [x for x in value if isinstance(x, str) and x.strip()]
    return items or None
