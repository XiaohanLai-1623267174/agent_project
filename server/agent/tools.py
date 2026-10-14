"""Tools exposed to the LangChain agent（kb_bench 沙箱副本）。

改动 1：顶部加 sys.path 兜底，使本文件被"直接运行"时也能找到 skills 包，
解决原本的 `ModuleNotFoundError: No module named 'skills'`。

改动 2（用户隔离）：`knowledge_ingestion` 不再是模块级单例工具，改成
`make_knowledge_ingestion_tool(user_id)` 工厂 —— user_id 由调用方闭包注入，
不进入工具的 JSON Schema，避免 LLM 指定任意 user_id 往别人库里写文档。

改动 3（路径越权修复）：只闭包 user_id 还不够 —— pdf_path 仍然是工具参数，
由 LLM（也就是由提问的用户）提供。实测（verify_agent_path_escape.py）：
  用户B 只要把 pdf_path 指向【用户A 的上传目录】，就能把 A 的文件入库到 B 自己的
  知识库，随后用 /chat 读出来 = 跨用户文件读取。工具返回 success=True，无任何阻拦。
⇒ 现在加了 allowed_root 白名单：pdf_path resolve 之后必须落在该目录内，
  否则直接返回失败。默认（fail-closed）就是"该用户自己的上传目录"。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 直接运行本文件时（python agent/tools.py），sys.path[0] 是 agent 目录，项目根不在里面
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langchain.tools import tool

from config import user_upload_root
from skills.knowledge_ingestion import ingest_pdf_to_knowledge_base


def make_knowledge_ingestion_tool(user_id: int, allowed_root: str | Path | None = None):
    """按用户构造入库工具：把 user_id 【闭包】进工具，而不是做成工具参数。

    为什么不能把 user_id 做成 @tool 的参数：那样它就出现在工具的 JSON Schema 里，
    LLM（或任何能构造工具参数的调用方）就能指定任意 user_id
    → 等于允许把文档写进别人的知识库。用户隔离必须在工具边界之外完成。

    allowed_root：允许导入的目录白名单。默认 = 该用户自己的上传目录（fail-closed）。
    想放开（例如单机自用、允许导入任意本机路径）必须【显式】传一个更宽的根，
    这样"放宽"永远是一个看得见的决定，而不是忘传参数导致的默认行为。

    调用方（FastAPI 的 /agent 路由）负责传入鉴权后的 current_user.id。
    """
    root = Path(allowed_root).resolve() if allowed_root is not None else user_upload_root(user_id).resolve()

    @tool("knowledge_ingestion")
    def knowledge_ingestion(pdf_path: str, title: str | None = None) -> dict:
        """将一份 PDF 导入奖学金/综测知识库。

        当用户明确要求上传、导入或更新 PDF 知识库时使用。它会自动完成
        PDF 解析、Markdown 清洗、元数据提取，并写入 SQLite 与 Chroma。

        注意：只能导入该用户自己上传目录中的文件；用户自己的电脑路径无法直接读取，
        需要用户先通过上传接口把文件传上来。
        """
        try:
            resolved = Path(pdf_path).resolve()
        except (OSError, ValueError) as exc:
            return {"success": False, "error": f"路径无法解析：{pdf_path}（{exc}）"}

        # 用 resolve() 之后再比较：能同时挡住 ".." 相对穿越和符号链接穿越
        if not resolved.is_relative_to(root):
            return {
                "success": False,
                "error": (
                    f"拒绝导入：只能导入你自己上传目录里的文件（{root}）。"
                    f"请先通过上传接口上传该文件，再让我导入。"
                ),
            }
        if not resolved.is_file():
            return {"success": False, "error": f"文件不存在：{resolved}"}

        return ingest_pdf_to_knowledge_base(
            pdf_path=str(resolved), title=title, user_id=user_id
        )

    return knowledge_ingestion