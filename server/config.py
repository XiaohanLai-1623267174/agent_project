"""项目路径的【唯一来源】。

为什么要有这个文件 —— §19 那个 bug 的根因就是"同一个目录在两个模块里有两种写法"：
  · langchain_project.py 用相对路径 "./.chroma_db"
  · json_to_db.py        用绝对路径 str(PROJECT_ROOT/".chroma_db")
chromadb 以【路径字符串】为键复用/新建进程内 System ⇒ 同一磁盘目录被当成两套索引状态，
表现为"入库后立刻检索"抛 chromadb.errors.InternalError: Error finding id。

把路径常量集中到这里，是为了让"写法不一致"在结构上无法再发生 ——
而不是靠"记得两个文件要写一样"。
"""

from __future__ import annotations

from pathlib import Path

# config.py 就在项目根，所以 parent 即项目根
PROJECT_ROOT = Path(__file__).resolve().parent

# 向量库：所有用到 Chroma 的模块都必须从这里取，不要再各写各的
CHROMA_DIRECTORY = PROJECT_ROOT / ".chroma_db"
COLLECTION_NAME = "scholarship_rules"

# 清洗后的 Markdown / chunks 缓存
KNOWLEDGE_MARKDOWN_DIR = PROJECT_ROOT / "knowledge_markdown"

# 上传暂存区：/documents 把用户上传的 PDF 写到这里，agent 也只能从这里读
UPLOAD_ROOT = PROJECT_ROOT / "uploads"


def user_upload_root(user_id: int) -> Path:
    """某个用户专属的上传目录。

    它同时充当 agent 入库工具的【允许根目录】：
    工具只接受 resolve 后位于该目录内的 pdf_path，
    否则一个用户就能让 agent 去读服务器上任意路径（包括别人的上传文件）。
    """
    return UPLOAD_ROOT / str(user_id)

# ============ 模型角色配置（唯一来源，§19/§21.3 手法）============
# 换后端 = 改这里一行（或用 .env 覆写），业务代码不动。
# 实测依据（本会话 100+ 次调用）：
#   · enable_thinking=False 让"改写"从 18.0s → 1.4s（13.1×），JSON 可靠性不变
#   · classify 用轻量 Qwen3.5-35B-A3B：实测判对 20/20（= 强模型同分），不重试时 ≈400ms
#   · 魔搭免费额度【按模型限流】：顺序连打会 429 → llm_roles.with_retry 必须开
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODELSCOPE_BASE_URL = "https://api-inference.modelscope.cn/v1"

ROLE_MODELS = {
    "classify": {
        "base_url": MODELSCOPE_BASE_URL, "api_key_env": "MS_API_KEY",
        "model": "Qwen/Qwen3.5-35B-A3B",          # 轻量：35B MoE / 3B 激活
        "extra_body": {"enable_thinking": False},
    },
    # ---- 会话标题（方案 B，第十九次会话新增）----------------------------
    # 复用 classify 同款轻量模型：3B 激活、实测不重试约 400ms、输出只有一句话。
    # 它是【首轮问答之后】的后台任务，不占回答的感知延迟；失败直接退回截断标题。
    "title": {
        "base_url": MODELSCOPE_BASE_URL, "api_key_env": "MS_API_KEY",
        "model": "Qwen/Qwen3.5-35B-A3B",
        "extra_body": {"enable_thinking": False},
    },
    "rewrite": {
        "base_url": MODELSCOPE_BASE_URL, "api_key_env": "MS_API_KEY",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "extra_body": {"enable_thinking": False},
    },
    "answer": {
        "base_url": MODELSCOPE_BASE_URL, "api_key_env": "MS_API_KEY",
        "model": "deepseek-ai/DeepSeek-V4.1-Flash-1017",
        "extra_body": {"enable_thinking": False},  # 待验证项：回答质量是否需要保留思考（§29.5-1）
    },
    "direct": {
        "base_url": MODELSCOPE_BASE_URL, "api_key_env": "MS_API_KEY",
        "model": "Qwen/Qwen3.8-Flash-Next",        # 非奖学金问题的直答（≤200 字）
        "extra_body": {"enable_thinking": False},
    },
    # ---- 入库管线两环（第十次会话新增）------------------------------------
    # 背景：这两环原先【硬编码】百炼 `qwen3.8-flash` + QW_API_KEY，不在角色表里，
    #   所以第六次会话"chat 迁魔搭"时从没触达它们（交接文档 §30.6 的角色表里
    #   也根本没有这两行）。而 `qwen3.8-flash` 免费额度已耗尽（§30 已记载 403），
    #   于是第一章那些改造全都白做：POST /documents 必 400。
    # 实测（0 计费 token —— 403 是被拒绝的请求）：真上传 1868B fixture →
    #   HTTP 400 detail=PermissionDeniedError 403 AllocationQuota.FreeTierOnly
    # 而 /chat 一直正常（检索只需 embedding + rerank，这两个仍走百炼且活着）
    #   ⇒ 造成"聊天好好的、就是传不了文件"这个很难归因的组合。
    "clean": {
        "base_url": MODELSCOPE_BASE_URL, "api_key_env": "MS_API_KEY",
        "model": "Qwen/Qwen3.8-Flash-Next",        # PDF→Markdown 清洗（长输出）
        "extra_body": {"enable_thinking": False},
    },
    "metadata": {
        "base_url": MODELSCOPE_BASE_URL, "api_key_env": "MS_API_KEY",
        "model": "Qwen/Qwen3.8-Flash-Next",        # chunk 元数据（function_calling）
        "extra_body": {"enable_thinking": False},
    },
    # embedding / rerank 【仍然不动】：仍走百炼。这不是遗漏，是实测结论。
    #   · embedding 换模型 = 全库必须重新嵌入（Chroma 不会报错，只会返回垃圾）
    #   · rerank 魔搭没有对应端点（/v1/rerank → 404，已实测）
    #   · 更根本的：魔搭 /v1/models 列 35 个模型，含 rerank / embedding 的【0 个】
    #   ⇒ 这两环【迁不过去】，而 clean/metadata 只是"当时没纳入范围"，性质不同。
}
