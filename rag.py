import os
from langchain_chroma import Chroma
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
import json
from langchain_core.documents import Document


# json_path = (
#     r"C:\Users\laixiaohan\PycharmProjects\PythonProject\rag_knowledge_base.json"
# )
#
# with open(json_path, "r", encoding="utf-8") as f:
#     raw_chunks = json.load(f)
#
# documents = []
#
# # 2. 遍历 JSON，转化为 LangChain Document 对象
# for chunk in raw_chunks:
#     # 提取标题层级，拼接成上下文文本（防止模型失去上下文感知）
#     section_breadcrumb = " -> ".join(chunk.get("section_path", []))
#
#     # 最终注入向量模型的文本：包含层级 + 正文
#     page_content = f"文档路径: {section_breadcrumb}\n正文内容:\n{chunk.get('content', '')}"
#
#     # 构建向量库 Metadata（方便过滤与后续引用）
#     metadata = {
#         "chunk_id": chunk.get("chunk_id"),
#         "doc_title": chunk.get("doc_title"),
#         "section_path": " -> ".join(chunk.get("section_path", [])),
#         "module": chunk.get("metadata", {}).get("module", ""),
#         "sub_module": chunk.get("metadata", {}).get("sub_module", ""),
#         "score_type": chunk.get("metadata", {}).get("score_type", ""),
#         # 可以存更多你需要的检索过滤字段...
#     }
#
#     doc = Document(page_content=page_content, metadata=metadata)
#     documents.append(doc)
#
# embeddings = DashScopeEmbeddings(
#     model="qwen3.7-text-embedding",
#     dashscope_api_key=os.getenv("QW_API_KEY"),
# )
#
# vectorstore = Chroma.from_documents(
#     documents=documents,
#     embedding=embeddings,
#     persist_directory="./.chroma_db",
#     collection_name="scholarship_rules"
# )
#
# print(f"成功导入 {len(documents)} 个 Chunk 到向量库中！")

# embeddings = DashScopeEmbeddings(
#     model="qwen3.7-text-embedding",
#     dashscope_api_key=os.getenv("QW_API_KEY"),
# )
# vectorstore = Chroma(
#     persist_directory="./.chroma_db",
#     embedding_function =embeddings,
#     collection_name="scholarship_rules"
# )
# data = vectorstore.get()
#
#
# docs = data["documents"]
# ids = data["ids"]
# for num,i in enumerate(docs,start=1):
#     print(f"---------------------------{num}-----------------------------------")
#     print(i)
#     print(f"---------------------------{num}-----------------------------------")
# print(len(docs))

# data = vectorstore.get()
# docs = data["documents"]
# ids = data["ids"]
# seen = {}
# same_ids = []
# for i, doc in enumerate(docs):
#     if doc in seen:
#         same_ids.append(ids[i])
#     else:
#         seen[doc] = i
# print(len(same_ids))

# import shutil
#
# shutil.rmtree("./.chroma_db")
#
# print("旧数据库删除完成")