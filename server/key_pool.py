"""[零 API] 模型后端的 API key 池 —— 生产用，供 llm_roles 轮换。

与 kb_bench/mskeys.py 的区别（为什么要有两个）：
    mskeys.py   = 【实验装置】用的魔搭专用池，import 时 load_dotenv(BENCH/".env")
    key_pool.py = 【生产代码】用的通用池，不碰 .env、不做任何 io
  让 llm_roles（生产库）反向 import mskeys 会带来两个问题：
    ① 生产库依赖仅存在于开发目录的 kb_bench/.env；
    ② 触发 import 期副作用。
  所以这里只接收"变量名列表 + 取环境变量的函数"，由调用方注入。

额度口径（2026-10-04 用户澄清，★ 重要）：
    魔搭的额度单位是【魔粒】，每个 key 一共 250 魔粒。
    不同模型每次调用消耗不同魔粒数（如 0.5 / 1 / 2）
    ⇒ 【不存在】"每天多少次"这个量。任何"按调用次数估算剩余额度"都是错的。
    能确定的只有：某个 key 报 `insufficient balance` 时，它就没魔粒了。

★ 状态机设计（被 test_key_rotation 连抓四次后收敛，改之前务必读完）
    本类**显式跟踪 `_active`** —— "正在被使用的那个 key 的变量名"。
    这是关键：若把 `current()` 朴素地定义为"第一个未耗尽的 key"，就无法区分
    "哪个 key 正在被使用"，于是：
        exhaust_current()   标记 K1 死
        current()           返回"第一个未耗尽" = K2（因为 K1 刚死）
        rotate(排除 cur)     再去找非 K2 的 → 找到已死的 K1 → None
    ⇒ 换 key 永远失败（"K2 明明可用，却说不该轮换"）。
    引入 `_active` 后语义才闭合：

        current()           = _active 对应的 key（若它已死则自动前移）
        exhaust_current()   = 把 _active 标记为死，再把 _active 前移到下一个活的
                              ★ 返回新 key；返回 (None, None) 表示池已空
        can_rotate()        = 纯谓词："当前之外还有活的吗"
        rotate()            = 兼容别名，等价于 exhaust_current()

    唯一推荐用法：
        name, key = pool.current()           # 开始时取
        ... 调用失败且判定为额度耗尽 ...
        name, key = pool.exhaust_current()   # 换下一个；(None, None) ⇒ 池空
"""
from __future__ import annotations

import os
import time
from typing import Callable


def _ttl_seconds() -> float:
    """额度耗尽标记的有效期（秒）。默认 1800。

    为什么需要它（§39.60.6 方案①）：`_exhausted` 只增不减，池把所有 key 标死后
    **永不自愈** —— 魔搭免费额度按日重置，但进程不感知日期，跨天也必须重启才恢复。
    给标记加个时间窗，过期自动复活：限流是分钟级、日额度是日级，30 分钟是一个折中。
    若上游其实仍不可用，复活后会再撞一次 429 并被重新标记（调用方重试次数有上限）。
    """
    try:
        return float(os.getenv("KEY_EXHAUSTED_TTL_S", "1800"))
    except ValueError:
        return 1800.0


def mask(key: str) -> str:
    """只显示头尾，避免日志泄漏完整 key。"""
    if not key:
        return "<空>"
    if len(key) <= 10:
        return key[:3] + "***"
    return "%s…%s" % (key[:6], key[-4:])


class KeyPool:
    """一个后端的一组 API key，支持"耗尽即换下一个"。

    设计要点：
      · **耗尽状态是【实例级】的**，不是进程级全局 —— 避免把某个实验的耗尽量
        误传染给后续实验（不同 provider 的池互不影响）。
      · 顺序由 env_names 决定；已标记耗尽的会被跳过。
      · `_active` 是"正在用的那一个"，不是"第一个可用的" —— 见模块 docstring。
    """

    def __init__(self, env_names: list[str], *, read: Callable[[str], str | None] = None,
                 label: str = "model"):
        self.label = label
        self.env_names = list(env_names)
        self._read = read or (lambda n: os.getenv(n))
        self._exhausted: set[str] = set()
        self._exhausted_at: dict[str, float] = {}   # key -> 标记时刻（用于 TTL 复活）
        self._active: str | None = None          # ★ 正在使用的变量名
        self._pairs: list[tuple[str, str]] = []
        self.reload()

    # ---------------- 池内容 ----------------
    def reload(self) -> None:
        self._pairs = [(n, (self._read(n) or "").strip()) for n in self.env_names]
        self._pairs = [(n, k) for n, k in self._pairs if k]
        self._active = self._pairs[0][0] if self._pairs else None

    def __len__(self) -> int:
        return len(self._pairs)

    @property
    def alt_env_names(self) -> list[str]:
        """除第一个之外的名字（用于"还配了哪些备用 key"的提示）。"""
        return self.env_names[1:]

    # ---------------- 内部 ----------------
    def _key_of(self, env_name: str | None) -> str | None:
        for n, k in self._pairs:
            if n == env_name:
                return k
        return None

    def _first_alive_after(self, env_name: str | None) -> str | None:
        """env_name 之后（按池顺序）第一个未耗尽的变量名；没有则 None。"""
        start = 0
        if env_name is not None:
            for i, (n, _) in enumerate(self._pairs):
                if n == env_name:
                    start = i + 1
                    break
        for n, k in self._pairs[start:]:
            if k not in self._exhausted:
                return n
        return None

    def _first_alive(self) -> str | None:
        for n, k in self._pairs:
            if k not in self._exhausted:
                return n
        return None

    # ---------------- 当前 key ----------------
    def current(self) -> tuple[str | None, str | None]:
        """返回当前应使用的 (env_name, key)；池空或全耗尽则 (None, None)。

        纯查询语义（只在 _active 已失效时才前移），不改变"哪个 key 已死"。
        """
        self._prune_exhausted()
        if self._active is None:
            self._active = self._first_alive()
        if self._active is not None and self._key_of(self._active) in self._exhausted:
            self._active = self._first_alive()
        if self._active is None:
            return None, None
        return self._active, self._key_of(self._active)

    def name_of(self, key: str | None) -> str:
        for n, k in self._pairs:
            if k == key:
                return n
        return "<未知>"

    # ---------------- 耗尽 / 轮换 ----------------
    def mark_exhausted(self, key: str | None) -> None:
        if key:
            self._exhausted.add(key)
            self._exhausted_at[key] = time.time()

    def _prune_exhausted(self) -> None:
        """把超过 TTL 的耗尽量标记清掉（方案①：让池能自愈）。"""
        ttl = _ttl_seconds()
        if ttl <= 0 or not self._exhausted_at:
            return
        now = time.time()
        stale = [k for k, ts in self._exhausted_at.items() if now - ts >= ttl]
        for k in stale:
            self._exhausted.discard(k)
            self._exhausted_at.pop(k, None)

    def exhausted_names(self) -> list[str]:
        return [self.name_of(k) for k in self._exhausted]

    def exhaust_current(self) -> tuple[str | None, str | None]:
        """把**当前正在用的** key 标记为耗尽，并把 _active 前移到下一个活的。

        返回新的 (env_name, key)；池已空时返回 (None, None)。
        ★ 这是唯一的"换 key"入口 —— 它同时完成"标记"与"前进"，两者不可分离
          （分开会让最后一个 key 永远不被标记，all_exhausted() 恒假）。
        """
        name, key = self.current()
        if key is None:
            return None, None
        self.mark_exhausted(key)
        nxt = self._first_alive_after(name)
        if nxt is None:
            nxt = self._first_alive()
        self._active = nxt
        return self.current()

    def rotate(self) -> tuple[str, str] | None:
        """兼容别名：等价于 `exhaust_current()`，返回新 key 或 None。"""
        name, key = self.exhaust_current()
        return (name, key) if key is not None else None

    def can_rotate(self) -> bool:
        """纯谓词：**除当前 key 之外**还有可用的吗（不改变任何状态）。

        ⚠️ 不要用它判断"换 key 之后还能不能继续" —— 那是 `exhaust_current()`
           返回非 None 的语义。本方法只回答"是否还有备用 key 存在"。
        """
        _, cur_key = self.current()
        return any(k not in self._exhausted and k != cur_key for _, k in self._pairs)

    def mark_all_exhausted(self) -> None:
        """整体停用这个池（池内所有 key 都不可用）。"""
        for _, key in self._pairs:
            self.mark_exhausted(key)
        self._active = None

    # ---------------- 可观测 ----------------
    def describe(self) -> str:
        if not self._pairs:
            return "%s: 池为空（%s 都未设置）" % (self.label, "/".join(self.env_names))
        parts = []
        for n, k in self._pairs:
            if k in self._exhausted:
                state = "🔴已耗尽"
            elif n == self._active:
                state = "★使用中"
            else:
                state = "✅备用"
            parts.append("%s=%s %s" % (n, mask(k), state))
        return "%s: %s" % (self.label, " | ".join(parts))

    def all_exhausted(self) -> bool:
        """池内是否已无可用 key。

        判据统一为"取不到当前 key 且池非空" —— 避免"所有 key 被标记"与
        "索引走完"出现语义分叉（`_dbg_keypool.py` 曾抓出该分叉）。
        """
        if not self._pairs:
            return False          # 池本来就空 = 配置缺失，不是"额度耗尽"，要区分
        name, _ = self.current()
        return name is None