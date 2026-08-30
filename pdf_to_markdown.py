import pdfplumber
import os
from langchain_openai import ChatOpenAI

# ==========================================
# 1. LLM 配置 (请替换为你使用的大模型配置)
# ==========================================
client = ChatOpenAI(
    model="qwen3.8-max",
    api_key=os.getenv("QW_API_KEY"),
    base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    temperature=0.1
)

CLEAN_PROMPT = """
你是一名专业的中文规章制度文档清洗器。

你的任务是清洗下面由 PDF 转换得到的 纯Markdown/文本，目的是恢复原始文档的结构，使其适合后续 RAG 检索。

【绝对规则】
1. 不得新增原文没有的信息。
2. 不得删除任何有效信息。
3. 不得改变任何数字、分数、百分比、日期、次数、等级、条件。
4. 不得改变“不得”“不”“未”“无”等否定含义。
5. 不得修改原文的政策规定和评分规则。
6. 不得自行解释或总结。
7. 不得把不同规则合并成一个规则。
8. 不得把一个规则拆成失去上下文的碎片。
9. 只修复 PDF 转换造成的错误换行、词语被拆开、标题层级丢失、表格阅读顺序混乱等。
10. 如果无法确定某处原始结构，不要猜测，保留原文并标记：[结构无法确定]
11.如果单元格内包含复杂的干部考核或矩阵加分规则，请将其单独提取并还原为标准的 4-5 列 Markdown 表格，不要用 <br> 拼接到同一个单元格内。

【结构要求】
请尽可能恢复以下 Markdown 层级：#、##、###、#### 等。
对于 PDF 转换产生的表格，如果能够根据原文明确恢复表格关系，可以转换成 Markdown 表格；如果无法确定，不要猜测。

【输出要求】
只输出清洗后的 Markdown。不要输出解释。不要输出“以下是清洗结果”等额外文字。

原始输入数据：
{raw_text}
"""

pdf_path = r"D:\Documents\Desktop\9.18★ 《海洋信息工程学院学生专业奖学金评定和综合素质量化测评办法》的通知_20250918173325.pdf"


# ==========================================
# 2. PDF 提取函数 (保留你原有的逻辑)
# ==========================================
def clean_text(text):
    if text is None: return ""
    text = str(text).replace("\n", " ")
    return " ".join(text.split()).strip()


def clean_cell(cell):
    if cell is None: return ""
    return clean_text(cell)


def is_fragmented_table(table):
    if not table: return False
    row_lengths = [len(row) for row in table]
    max_cols = max(row_lengths)
    return max_cols >= 8


def repair_fragmented_table(table):
    repaired_rows = []
    for row in table:
        row = ["" if cell is None else str(cell) for cell in row]
        text = " ".join("".join(row).split())
        if text: repaired_rows.append(text)
    return repaired_rows


def extract_pdf(pdf_path):
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            print(f"正在提取第 {page_num} 页...")
            text = page.extract_text() or ""
            text = text.strip()

            raw_tables = page.extract_tables()
            tables = []
            for table_index, table in enumerate(raw_tables):
                cleaned_table = []
                for row in table:
                    if not row: continue
                    cleaned_row = [clean_cell(cell) for cell in row]
                    if any(cleaned_row):
                        cleaned_table.append(cleaned_row)
                if cleaned_table:
                    tables.append({"table_index": table_index + 1, "rows": cleaned_table})

            pages.append({"page": page_num, "text": text, "tables": tables})
    return pages


# ==========================================
# 3. LLM 转换函数
# ==========================================
def call_llm_for_markdown(raw_content):
    """调用 LLM 将拼接好的原始文本转换为最终的 Markdown"""
    print("正在调用 LLM 进行深度清洗与 Markdown 重构...")

    prompt = CLEAN_PROMPT.replace("{raw_text}", raw_content)

    try:
        response = client.invoke(prompt)

        return response.content.strip()
    except Exception as e:
        print(f"LLM 调用失败: {e}")
        return ""


# ==========================================
# 4. 主流程编排
# ==========================================
if __name__ == "__main__":
    # 1. 提取 PDF
    extracted_pages = extract_pdf(pdf_path)

    # 2. 将提取出的正文和表格拼接成一个长文本，供 LLM 理解
    raw_document_builder = []

    for page in extracted_pages:
        raw_document_builder.append(f"\n========== 第 {page['page']} 页 ==========\n")

        # 写入正文
        if page["text"]:
            raw_document_builder.append("【页面正文提取】")
            raw_document_builder.append(page["text"])

        # 写入表格结构数据
        for table in page["tables"]:
            raw_document_builder.append(f"\n--- 表格 {table['table_index']} ---")
            rows = table["rows"]

            if is_fragmented_table(rows):
                raw_document_builder.append("检测到碎片化表格：")
                repaired = repair_fragmented_table(rows)
                for row in repaired:
                    raw_document_builder.append(row)
            else:
                raw_document_builder.append("正常表格：")
                raw_document_builder.append(str(rows))

    raw_content = "\n".join(raw_document_builder)

    # 3. 发送给 LLM
    final_markdown = call_llm_for_markdown(raw_content)

    #4. 保存结果
    output_path = pdf_path.replace(".pdf", "_cleaned.md")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final_markdown)

    print(f"\n处理完成！最终 Markdown 已保存至：{output_path}")
