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
from langchain_openai import ChatOpenAI

try:
    from .tools import make_knowledge_ingestion_tool   # 作为包导入（uvicorn 从项目根启动）
except ImportError:                                    # 作为脚本直接运行（模块名为 __main__，无父包）
    from agent.tools import make_knowledge_ingestion_tool


def _default_pdf() -> str:
    """kb_bench 根目录下第一份 PDF（避免硬编码含特殊字符的文件名）。"""
    pdfs = sorted(_PROJECT_ROOT.glob("*.pdf"))
    return str(pdfs[0]) if pdfs else ""


def create_knowledge_agent(
    user_id: int,
    enable_thinking: bool = False,
    allowed_root: str | Path | None = None,
):
    """Create an Agent with only the safe, high-level PDF ingestion capability.

    user_id 必填：入库工具的归属由它决定（闭包注入，不进工具 Schema）。
    allowed_root：入库工具的可读目录白名单（默认 = 该用户自己的上传目录，
    fail-closed）。见 tools.py 改动 3：不限制 pdf_path 会导致跨用户文件读取。
    """
    model = ChatOpenAI(
        model="qwen3.8-max",
        api_key=os.getenv("QW_API_KEY"),
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        temperature=0,
        # 与 skill 里同样的教训：qwen3.8 默认思考模式会拖慢/干扰工具调用
        extra_body={"enable_thinking": enable_thinking},
    )
    return create_agent(
        model=model,
        tools=[make_knowledge_ingestion_tool(user_id, allowed_root=allowed_root)],
        system_prompt="""你是学校知识库管理助手。
只有当用户明确要求将一个本地 PDF 导入、更新到奖学金或综测知识库时，
才调用 knowledge_ingestion；必须使用用户提供的真实 PDF 路径。
不要自行编造路径，也不要把普通问答当成导入任务。
如果工具返回"拒绝导入：只能导入你自己上传目录里的文件"，
就告诉用户先把该文件通过上传接口上传，不要反复重试或换路径试探。""",
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
        tool = make_knowledge_ingestion_tool(user_id)
        print("tool schema args ->", sorted(tool.args.keys()))
        sys.exit(0)

    pdf = _default_pdf()
    query = f"请把这份 PDF 导入知识库：{pdf}"
    print(f"[查询] {query}\n", flush=True)

    # 标准运维用法：在项目根直接跑脚本，导入的是本机项目目录里的 PDF。
    # 这里【显式】把允许根放宽到项目根 —— 正因为是显式写出来的，
    # 才不会变成"默认就允许读任意路径"（见 tools.py 改动 3）。
    agent = create_knowledge_agent(user_id, allowed_root=_PROJECT_ROOT)
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