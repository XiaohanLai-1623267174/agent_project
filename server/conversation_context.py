"""[零 API] 对话上下文：把"给模型看的上文"收敛到唯一一处。

## 为什么要有这个模块

三个已实测的结论（见 SESSION_HANDOFF §35 各节）：

| 结论 | 实测数据 |
|---|---|
| **classify 必须看到上文** | 多轮追问误判 **4/7 → 0/7**；总体漏判 20.7% → 3.4% |
| **rewrite 必须看到上文** | 指代型追问改写正确率 **19% → 86%** |
| **助手回答必须完整给** | 答案消融 59% → **85%**（★ 不要截断到 150 字） |
| 3 轮已饱和 | `D3`(82%) 与 `DALL`(86%) 实质相同；1 轮不够是任务相关的 |

## 三个硬约束（多租户，第 4 个串档入口）

1. **必须校验会话归属**：`Conversation.user_id == 调用方 user_id`，
   否则用户 A 传用户 B 的 `conv_id` 就能把 B 的对话内容喂进 A 的模型 —— **串档**。
   本模块的 `load()` 在取消息**之前**先做归属校验（`_load_raw` 用 JOIN 一次做完）。
2. **绝不做进程级全局**：本对象**必须显式构造再传**，不提供任何"当前会话"单例
   （§35.13.2 前提①：AgentState 迁移时也必须保持"每请求一份"）。
3. **不可变**：`frozen=True` —— 保住"传值"优点，避免下游函数悄悄改掉别人的上下文。

## 为什么参数叫 `context`/`conv` 而不是 `history`

它装的不只是"历史提问"，**还含助手回答**（实测必须带）。
叫 `history` 会让人以为只有用户说过的话，从而写出只遍历 user 消息的 bug。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from database import Conversation, Message, new_session


# ---------------- 供模型渲染的默认参数（★ 有实测依据，见模块 docstring）----------------
DEFAULT_MAX_TURNS = 3          # D3≈DALL，3 轮已饱和
DEFAULT_MAX_ASST_CHARS = 2000  # 实测"不截断"最好；给个大上限只为防极端长答
DEFAULT_MAX_USER_CHARS = 500   # 用户提问本来很短，截断极少触发
DEFAULT_MAX_MSGS = 24          # 最多取最近 24 条消息用于配对（12 轮），防大会话拖慢


@dataclass(frozen=True)
class Turn:
    """一轮对话：用户提问 + 助手回答（可能为空串）。"""

    user: str
    assistant: str = ""

    def render(self, max_asst_chars: int = DEFAULT_MAX_ASST_CHARS) -> str:
        a = _clip(self.assistant, max_asst_chars)
        if not a:
            return "用户: %s" % _clip(self.user, DEFAULT_MAX_USER_CHARS)
        return "用户: %s\n助手: %s" % (_clip(self.user, DEFAULT_MAX_USER_CHARS), a)


def _clip(text: str, limit: int) -> str:
    """截断到 limit 字符并加省略号。limit<=0 表示不截断。"""
    text = (text or "").strip()
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "…"


@dataclass(frozen=True)
class ConversationContext:
    """一次请求的对话上下文。**必须显式构造再传，禁止做成全局单例。**

    `user_id` 跟数据一起走（而不是与 turns 分开传），这样：
      · 下游无法"拿到 turns 但忘了 user_id"；
      · 迁移到 AgentState 时就是 `state["conv"]`，函数体不用改。

    ⚠️ `turns` 是 list（`frozen=True` 只冻结属性重新绑定，不冻结元素）。
       调用方**不得原地修改** —— 约定"只读"。之所以不用 tuple：
       构造处（本模块）与消费处都按序列用，用 list 与既有实验装置一致。
    """

    user_id: int
    turns: list[Turn] = field(default_factory=list)

    # ---------------- 构造 ----------------
    @classmethod
    def empty(cls, user_id: int) -> "ConversationContext":
        """无上文的上下文（新会话 / 未提供 conv_id / 不具备归属权时用）。"""
        return cls(user_id=user_id, turns=[])

    @classmethod
    def load(cls, conv_id: int | None, user_id: int, *,
             max_turns: int = DEFAULT_MAX_TURNS,
             max_asst_chars: int = DEFAULT_MAX_ASST_CHARS,
             max_msgs: int = DEFAULT_MAX_MSGS,
             session_factory: Any = None) -> "ConversationContext":
        """从数据库读出该会话最近 max_turns 轮上下文。

        **归属校验是第一步，且失败时返回空上下文（不抛异常）** —— 理由：
        这是"给模型看的辅助信息"，不是"用户请求的资源"。传错/伪造 conv_id
        应退化为"没有上文"（回答质量下降），而不是 500 或泄漏别人的对话。

        返回的 turns 按时间**由旧到新**排列（模型的阅读顺序）。

        `session_factory` 仅供测试注入（默认用 database.new_session）。

        ⚠️ 默认值必须是【工厂】而不是 `Session` 类 —— 见 database.new_session()
        的 docstring：传类进去会 UnboundExecutionError，而测试永远走注入路径，
        所以这个默认值曾经在生产路径上必崩（§36 复现）。
        """
        if conv_id is None:
            return cls.empty(user_id)

        rows = _load_raw(conv_id, user_id, max_msgs,
                         session_factory or new_session)
        if rows is None:
            # 会话不存在，或不属于该 user —— 两者对外表现一致（不区分，避免枚举探测）
            return cls.empty(user_id)

        return cls(user_id=user_id,
                   turns=_pair_turns(rows, max_turns, max_asst_chars))

    # ---------------- 消费 ----------------
    @property
    def is_empty(self) -> bool:
        return not self.turns

    def render(self, max_asst_chars: int = DEFAULT_MAX_ASST_CHARS) -> str:
        """渲染成给模型看的一整块文本。

        空上下文返回**空串**（不是"（无）"之类占位符）——
        这样调用方可直接 `if hist:` 判断该不该往 prompt 里塞 【上文对话】 段，
        避免给模型一段"上文是空的"的噪声。
        """
        if not self.turns:
            return ""
        return "\n".join(t.render(max_asst_chars) for t in self.turns)

    def __len__(self) -> int:
        return len(self.turns)


# ---------------- 内部 ----------------
def _load_raw(conv_id: int, user_id: int, max_msgs: int,
              session_factory: Any) -> list[tuple[str, str]] | None:
    """★ 一次 JOIN 同时完成【归属校验】与【取消息】。

    做成一条查询是刻意的：如果先 `s.get(Conversation, conv_id)` 再单独取消息，
    中间就有"检查过归属后又按 conv_id 取消息"的窗口，容易在后续重构里退化成串档。
    这里 messages 表本身不带 user_id，**唯一防线就是这个 JOIN 条件**。

    返回 [(role, content), ...] 按时间由旧到新；返回 None 表示无权/不存在。
    """
    stmt = (
        select(Message.role, Message.content)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.id == conv_id, Conversation.user_id == user_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(max_msgs)
    )
    with session_factory() as s:
        rows = s.execute(stmt).all()
    if not rows:
        return None
    return [(r[0], r[1] or "") for r in reversed(rows)]   # 翻回由旧到新


def _pair_turns(rows: list[tuple[str, str]], max_turns: int,
                max_asst_chars: int) -> list[Turn]:
    """把消息流配成 (用户, 助手) 轮，只留最近 max_turns 轮。

    配对规则：
      · 以 user 消息为轮起点；其后的第一条 assistant 消息归它。
      · 没有 assistant 的轮（如用户刚发完、助手还没答）保留，assistant=""
        —— 因为 classify/rewrite 关心的是"用户在追问什么"，最后一条用户提问本身就是线索。
      · assistant 消息出现在任何 user 之前（数据异常）时忽略。
    """
    turns: list[Turn] = []
    cur: Turn | None = None
    for role, content in rows:
        if role == "user":
            if cur is not None:
                turns.append(cur)
            cur = Turn(user=content, assistant="")
        elif role == "assistant":
            if cur is not None and not cur.assistant:
                cur = Turn(user=cur.user, assistant=content)
            # 若该轮已有回答，忽略多余的 assistant（数据异常）
    if cur is not None:
        turns.append(cur)

    if max_turns > 0:
        turns = turns[-max_turns:]

    return [Turn(user=t.user, assistant=_clip(t.assistant, max_asst_chars))
            for t in turns]


def load_context(conv_id: int | None, user_id: int, **kw: Any) -> ConversationContext:
    """便捷函数：`ConversationContext.load` 的薄包装（供 fastapi 层直接调用）。"""
    return ConversationContext.load(conv_id, user_id, **kw)