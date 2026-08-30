import json
from typing import List, Dict, Any
from pydantic import BaseModel, Field
from langchain_openai import ChatOpenAI
import os
# 1. 初始化 ChatOpenAI (兼容 DashScope / DeepSeek / OpenAI 等)
llm = ChatOpenAI(
    model="qwen3.8-flash",
    api_key=os.getenv("QW_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    temperature=0.0,
    timeout=120,
    max_retries=3
)

md_path = r"D:\Documents\Desktop\9.18★ 《海洋信息工程学院学生专业奖学金评定和综合素质量化测评办法》的通知_20250918173325_cleaned.md"
# 2. 定义 Pydantic 数据结构
class ChunkMetadata(BaseModel):
    module: str = Field(description="所属一级模块名称，如：思想道德素质、学创素质、奖学金评审条件等")
    sub_module: str = Field(description="所属二级子项名称，如：遵纪守法、学科创新、基本条件等")
    score_type: str = Field(description="分值类型，仅限选择：'基本分'、'加分项'、'扣分项'、'规则说明'")
    base_score: float = Field(default=0.0, description="若文中提及基本分或满分，提取数值，无则填0")
    score_rules: List[str] = Field(description="该条款包含的具体加扣分规则简短摘要列表")
    keywords: List[str] = Field(description="用于增强检索的业务关键词/别名，3-5个")


# 核心修改点：绑定 Pydantic 结构生成结构化 LLM Runner
structured_llm = llm.with_structured_output(ChunkMetadata)


# 3. Markdown 规则树预解析器 (保持不变)
def split_markdown_by_headings(md_text: str) -> List[Dict[str, Any]]:
    lines = md_text.split('\n')
    chunks = []
    current_doc = "通用文档"
    h2_title = ""
    h3_title = ""
    current_content = []

    for line in lines:
        if line.startswith('# '):
            current_doc = line.replace('# ', '').strip()
        elif line.startswith('## '):
            if current_content and (h2_title or h3_title):
                chunks.append({
                    "doc_title": current_doc,
                    "h2": h2_title,
                    "h3": h3_title,
                    "content": "\n".join(current_content).strip()
                })
                current_content = []
            h2_title = line.replace('## ', '').strip()
            h3_title = ""
        elif line.startswith('### '):
            if current_content and (h2_title or h3_title):
                chunks.append({
                    "doc_title": current_doc,
                    "h2": h2_title,
                    "h3": h3_title,
                    "content": "\n".join(current_content).strip()
                })
                current_content = []
            h3_title = line.replace('### ', '').strip()
        else:
            if line.strip():
                current_content.append(line)

    if current_content:
        chunks.append({
            "doc_title": current_doc,
            "h2": h2_title,
            "h3": h3_title,
            "content": "\n".join(current_content).strip()
        })

    return [c for c in chunks if len(c["content"]) > 10]


# 4. 调用 structured_llm 提取元数据 (简化)
def extract_metadata_with_llm(chunk: Dict[str, Any]) -> ChunkMetadata:
    prompt = f"""你是一个 RAG 规章制度解析专家。请分析以下规章制度文本，提取结构化的元数据。

【上下文信息】
所属文档: {chunk['doc_title']}
一级目录: {chunk['h2']}
二级目录: {chunk['h3']}

【待分析文本内容】
{chunk['content']}
"""
    # 直接 invoke，返回的就是实例化好的 ChunkMetadata 对象
    return structured_llm.invoke(prompt)


# 5. 主 Pipeline
def process_markdown_to_rag_json(md_content: str, output_path: str):
    raw_chunks = split_markdown_by_headings(md_content)
    rag_json_list = []

    print(f"检测到 {len(raw_chunks)} 个逻辑切块，开始解析...")

    for idx, chunk in enumerate(raw_chunks):
        try:
            # 调用 LangChain 抽取元数据
            meta: ChunkMetadata = extract_metadata_with_llm(chunk)

            path = [chunk['doc_title']]
            if chunk['h2']: path.append(chunk['h2'])
            if chunk['h3']: path.append(chunk['h3'])

            rag_node = {
                "chunk_id": f"chunk_{idx + 1:03d}",
                "doc_title": chunk['doc_title'],
                "section_path": path,
                "content": chunk['content'],
                "metadata": meta.model_dump()  # pydantic v2 转 dict 方法
            }
            rag_json_list.append(rag_node)
            print(f"成功处理 [{idx + 1}/{len(raw_chunks)}]: {' -> '.join(path)}")
        except Exception as e:
            print(f"处理切块 {idx} 失败: {e}")

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(rag_json_list, f, ensure_ascii=False, indent=2)
    print(f"处理完成！最终生成 JSON 存至 {output_path}")


if __name__ == "__main__":
    with open(md_path, "r", encoding="utf-8") as f:
        md_text = f.read()
    process_markdown_to_rag_json(md_text, "rag_knowledge_base.json")