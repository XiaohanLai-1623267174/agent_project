"""Factory for the agent that can invoke the knowledge-ingestion skill（kb_bench 沙箱副本）。

改动 1：修复直接运行时的两种 ImportError
  - `from .tools import ...` 相对导入 → 加 try/except 回退到绝对导入
  - 项目根不在 sys.path → 顶部兜底插入
并新增 __main__ 入口，用于亲眼验证"LLM 决定并调用 skill"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 直接运行本文件时（python agent/agent.py），补上项目根到 sys.path。
# ⚠️ 必须保证项目根排在 sys.path[0]（脚本自身目录）之前：否则 `import agent.tools`
#    会先撞上本文件（kb_bench/agent/agent.py），把它当成顶层模块 `agent` → 自引用递归
#    → ModuleNotFoundError: 'agent' is not a package。
#    旧写法 `if root not in sys.path: insert(0, root)` 在 root 已由 PYTHONPATH 提供时
#    会跳过 insert，正好踩中这个坑（已实测复现）。
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if str(_PROJECT_ROOT) in sys.path:
    sys.path.remove(str(_PROJECT_ROOT))
sys.path.insert(0, str(_PROJECT_ROOT))
# 同时把脚本目录从搜索路径里剔除（只剔除其作为"顶层目录"的身份，不影响本文件已完成的加载）
sys.path[:] = [p for p in sys.path if p and str(Path(p).resolve()) != _SCRIPT_DIR]

from langchain.agents import create_agent

# 统一底座（§39.53.1）：模型 / 后端 / key 池全部从 llm_roles 来，不再硬编码任何后端。
import env_bootstrap          # noqa: E402  import 即加载 .env：本文件要能作为脚本直跑
from llm_roles import make_model

try:
    from .tools import make_file_tools   # 作为包导入（uvicorn 从项目根启动）
except ImportError:                      # 作为脚本直接运行（模块名为 __main__，无父包）
    from agent.tools import make_file_tools


def _max_tool_calls() -> int:
    """单次任务的工具调用预算（`AGENT_MAX_TOOL_CALLS`，默认 8）。

    为什么要有上限：agent 是 ReAct 循环，模型理论上可以无限调用工具；
    这个项目定位是「受预算约束、可自证」，没有上限就既不是「受约束」，也可能真的烧额度。
    默认 8：实测「搜索 → 入库 → 作答」只需 2~3 次，8 次足够正常任务，又能挡住死循环。
    """
    raw = os.getenv("AGENT_MAX_TOOL_CALLS", "").strip()
    try:
        n = int(raw) if raw else 8
    except ValueError:
        n = 8
    return max(1, n)


def _default_pdf() -> str:
    """kb_bench 根目录下第一份 PDF（避免硬编码含特殊字符的文件名）。"""
    pdfs = sorted(_PROJECT_ROOT.glob("*.pdf"))
    return str(pdfs[0]) if pdfs else ""


def create_knowledge_agent(
    user_id: int,
    enable_thinking: bool = False,
    allowed_root: str | Path | None = None,
):
    """构造知识库 agent：能【自己找文件】再【导入】，不用用户粘贴绝对路径。

    历史演进（保留以示取舍，§39.63）：
      · 最初只有 knowledge_ingestion(pdf_path) ⇒ 用户必须提供服务器绝对路径 ✗
      · 现在拆成 find_pdf(名字) + ingest_pdf(路径)：
        用户说「把简历导入」⇒ agent 先搜 ⇒ 唯一匹配就导、多条候选就反问 ✓

    安全边界（多用户前提）：搜索只在 AGENT_SEARCH_ROOTS 白名单内，
    并排除项目目录 / 虚拟环境 / node_modules；导入同样只接受白名单或本人上传目录内的 PDF。
    user_id 闭包进工具、不进 Schema —— 模型无法指定别人的库。

    enable_thinking 现在真的传进 extra_body（§39.62）；但实测该模型不返回
    reasoning_content，所以打开也不会多出「思考内容」—— 只是把开关接线做对。
    """
    model, _cfg = make_model(
        "answer", temperature=0,
        extra_body={"enable_thinking": bool(enable_thinking)},
    )
    return create_agent(
        model=model,
        tools=make_file_tools(user_id, max_calls=_max_tool_calls()),
        system_prompt="""你是学校知识库助手，能【检索知识库回答问题】，也能【把电脑里的 PDF 找出来并入库】。

能力边界（重要，别混用）：
· 用户问「条款写了什么 / 分数多少 / 有什么经历 / 两个文件有什么差异」⇒ 用 **search_knowledge**；
· 用户说「把 XX 导入 / 加到库里」⇒ 用 **find_pdf** 找到文件，再 **ingest_pdf** 入库；
· 用户点名某个文件（如「简历里」「按简历回答」）⇒ search_knowledge 带 **filename** 参数，
  这是定向检索：只在那一份文档里找，比全库检索更准。

工作方式：
1. 用户提到某个文件时（可能只说简称，如「简历」「综测办法」），**先调用 find_pdf 搜索**。
   不要向用户索要完整路径，也不要自己编造路径。
2. 拿到 find_pdf 的结果后按规则处理：
   · found == 1 ⇒ 直接 ingest_pdf 导入那一条，然后报告【真实返回】的 document_id 与切片数；
   · found >  1 ⇒ 把候选（文件名 + 大小 + 修改时间）**列给用户选**，不要自己挑；
   · found == 0 ⇒ 明确说「在这些目录里没找到」并列出 searched_roots，
     请用户确认文件名或把文件放进去；不要反复换关键词试探超过两次。
3. 入库结果必须**照抄工具返回值**（document_id / chunk_count / 失败原因），
   不要美化、不要编造成功。工具返回 success=false 时把 error 原文转述给用户。
3b. 若 ingest_pdf 返回 **exists=true**（同名文档已存在，工具刻意不重复入库）：
   把 existing 里的每一份列出来（document_id + 标题 + 切片数），然后用一句话让用户选：
   · 想再导一份 ⇒ 说「再导一次」，你再带 force=true 调用；
   · 想替换 ⇒ 提示用户先在左侧「我的文件」里删掉旧的那份，再重新导入。
   **不要**自己替用户决定，也不要因为被拦就改口说「已成功导入」。
4. 若工具返回「拒绝导入：只能导入白名单目录…」，直接告知用户可用目录，
   不要尝试别的路径绕过。
5. 回答知识库内容时必须**基于 search_knowledge 返回的 context**，并按其中【n】编号引用来源。
   context 为空或 count == 0 时，明确说「库里没有检索到相关内容」，**绝不编造**条款、分数或经历。
6. 与知识库无关的普通问答（闲聊、通用常识），不要调用工具，直接回答。""",
    )


if __name__ == "__main__":
    # --check：只构建 agent（会构造工具，不触发真实入库），用于快速验证接线是否打通
    # 用法：python -m agent.agent <user_id> [--check]
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not argv:
        print("用法: python -m agent.agent <user_id> [--check]")
        raise SystemExit(2)
    user_id = int(argv[0])

    if "--check" in sys.argv:
        agent = create_knowledge_agent(user_id)
        print("agent built OK ->", type(agent).__name__)
        tools = make_file_tools(user_id)
        for t in tools:
            print("tool:", t.name, "args ->", sorted(t.args.keys()))
        sys.exit(0)

    pdf = _default_pdf()
    query = f"请把这份 PDF 导入知识库：{pdf}"
    print(f"[查询] {query}\n", flush=True)

    # 注意：allowed_root 形参已不再参与工具构造（工具自己按白名单 + 本人上传目录校验），
    # 保留它只为兼容既有调用方。脚本直跑时同样走白名单逻辑。
    agent = create_knowledge_agent(user_id)
    result = agent.invoke({"messages": [{"role": "user", "content": query}]})

    print("=" * 70)
    for msg in result["messages"]:
        name = type(msg).__name__
        content = getattr(msg, "content", "")
        tool_calls = getattr(msg, "tool_calls", None)
        print(f"[{name}]")
        if tool_calls:
            for tc in tool_calls:
                print("   tool_calls ->", tc.get("name"), tc.get("args"))
        if content:
            print("   content:", str(content)[:600])