from langchain_community.embeddings import DashScopeEmbeddings
from langchain_openai import ChatOpenAI
from langchain_chroma import Chroma
from langchain_core.output_parsers import StrOutputParser
import os
from langchain_core.runnables import RunnablePassthrough, RunnableLambda, RunnableBranch
from langchain_core.prompts import ChatPromptTemplate
import jieba
from langchain_community.retrievers import BM25Retriever
from langchain_community.document_compressors import DashScopeRerank
from dashscope import TextReRank
import json
from langchain_core.documents import Document
# os.environ["LANGSMITH_TRACING"] = "true"
# os.environ["LANGSMITH_API_KEY"] = "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"
# os.environ["LANGSMITH_PROJECT"] = "langchain_lianxi"

embeddings = DashScopeEmbeddings(
    model="qwen3.7-text-embedding",
    dashscope_api_key=os.getenv("QW_API_KEY"),
)

vectorstore = Chroma(
    persist_directory="./.chroma_db",
    embedding_function=embeddings,
    collection_name="scholarship_rules"
)

vector_retriever = vectorstore.as_retriever(
    search_type="mmr",
    search_kwargs={
        "k": 10,
        "fetch_k": 15
    }
)

raw_data = vectorstore.get()
all_langchain_docs = [
    Document(page_content=doc, metadata=meta)
    for doc, meta in zip(raw_data["documents"], raw_data["metadatas"])
]

bm25_retriever = BM25Retriever.from_documents(
    documents=all_langchain_docs,
    preprocess_func=lambda x: jieba.lcut(x)
)

bm25_retriever.k = 10

model = ChatOpenAI(
    model="qwen3.8-max",
    api_key=os.getenv("QW_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
)

model1 = ChatOpenAI(
    model="qwen3.8-flash",
    api_key=os.getenv("QW_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"
)
# ============ 新增：问题分类 ============

classify_prompt = ChatPromptTemplate.from_template(
    """你是一个问题分类助手。请判断以下用户问题是否与「奖学金」或「综测」（综合素质测评）等学校问题相关。

分类规则：
- 如果问题涉及奖学金申请、评定、发放、条件、综测加分、综测计算、学校等相关内容，返回 "YES"
- 如果问题完全不涉及奖学金或综测或学校，返回 "NO"
- 只返回 YES 或 NO，不要包含任何其他内容

【用户问题】: {question}

【分类结果】:"""
)

classify_chain = classify_prompt | model1 | StrOutputParser()


def classify(question: str) -> bool:
    """返回 True 表示与奖学金/综测相关"""
    result = classify_chain.invoke(question)
    return "YES" in result.strip().upper()


# ============ 原有的 RAG 逻辑 ============

def rewrite(query):
    prompt = f"""你是一个搜索引擎优化专家。请将用户的提问先抽取问题最关键的词组，并改写为 5 个近义或同义的检索关键词，以便在知识库中精准匹配。
【改写策略】：
1. 如果问题只有一个条件就直接简写问题，并提取关键词，删去非关键成分(人物代称等)
2. 如果问题有多个条件，那就有几个条件就拆分成几个原始问题，然后再进行关键词改写
3. 改写关键字首先考虑同义或近义词，并且不要出现两个近义词在同一个句子里
4. 改写后的问题不可超过原问题长度，可大胆删减不重要的字

【用户提问】: {query}
【输出要求】: 
必须严格输出为一个 JSON 格式的字符串数组（List），不要包含任何额外的解释或 Markdown 标签。
例如：["改写关键词1", "改写关键词2", "改写关键词3", "改写关键词4", "改写关键词5"]

【输出 JSON】:"""
    try:
        response = model.invoke(prompt)
        content = response.content.strip().replace("'''json","").replace("'''","")
        queries = json.loads(content)
        if "综测" not in query:
            queries.append(query)
        print(queries)
        return queries
    except Exception as e:
        print(e)
        return [query]


# RAG 模式的 prompt（带参考资料）
rag_template = """你是学校奖学金知识库助手。
回答要求：
1. 优先回答用户真正的问题。
2. 参考资料只是辅助，不要直接复述资料。
3. 如果资料无法回答，请明确说明。
4. 不要把参考资料中的其他主题强行套入用户问题。
5. 如果用户的问题不是关于奖学金或者综测方面的，可以忽略参考资料，专心回答用户问题，字数限制在100字以内。
【参考资料】
{context}
【用户问题】
{question}
【你的回答】："""

rag_prompt = ChatPromptTemplate.from_template(rag_template)


def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)


def reranked(original_query, top_n=5):
    queries = rewrite(original_query)
    all_docs_dict = {}
    for query in queries:
        vector_docs = vector_retriever.invoke(query)
        bm25_docs = bm25_retriever.invoke(query)

        for doc in bm25_docs + vector_docs:
            all_docs_dict[doc.page_content] = doc

    all_docs_list = list(all_docs_dict.values())
    if not all_docs_list:
        return "未检索到资料"
    try:
        rerank_docs = TextReRank.call(
            model="qwen3-rerank",
            api_key=os.getenv("QW_API_KEY"),
            top_n=top_n,
            query=original_query,
            documents=[doc.page_content for doc in all_docs_list]
        )

        results = sorted(rerank_docs.output.results,
                         key=lambda doc: doc.relevance_score,
                         reverse=True)

        docs_list = [all_docs_list[r.index] for r in results[:top_n]]
        for i in docs_list:
            print(i)
        return format_docs(docs_list)
    except Exception as e:
        print(e)
        return format_docs(all_docs_list[:top_n])


# RAG 链
rag_chain = (
    {"context": lambda x: reranked(x), "question": RunnablePassthrough()}
    | rag_prompt
    | model
    | StrOutputParser()
)

# 非 RAG 的普通对话链（无参考资料）
direct_template = """你是一个ai助手，你的回答必须以用户的问题为核心，回答字数控制在200字以内。
【用户问题】
{question}
【你的回答】："""

direct_prompt = ChatPromptTemplate.from_template(direct_template)
direct_chain = direct_prompt | model1 | StrOutputParser()


# ============ 核心：根据分类结果路由 ============

def route(question: str):
    if classify(question):
        print("[路由] 问题与奖学金/综测相关，使用 RAG 检索回答")
        return rag_chain
    else:
        print("[路由] 问题与奖学金/综测无关，直接回答")
        return direct_chain


if __name__ == "__main__":
    while True:
        user_input = input("用户：")
        if not user_input:
            break
        else:
            try:
                selected_chain = route(user_input)
                for chunk in selected_chain.stream(user_input):
                    print(chunk, end="", flush=True)
                print()
            except Exception as e:
                print(e)

