import fitz
from docx import Document
import pandas as pd
from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), base_url=os.getenv("OPENAI_BASE_URL"))

def extract_pdf_text(pdf_path):
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text

def extract_doc_text(doc_path):
    doc = Document(doc_path)
    text = ""
    for para in doc.paragraphs:
        text += para.text + "\n"
    return text

regulation_text = extract_pdf_text(r"backend\data\uploads\煤矿安全规程.pdf")
work_text = extract_doc_text(r"new_docs\test_doc\004    S1302工作面作业规程（综采）.doc")

prompt = f"""你是煤矿安全合规检测专家。请对比《煤矿安全规程》和《S1302工作面作业规程》，找出不合规的地方。

《煤矿安全规程》内容：
{regulation_text[:8000]}

《S1302工作面作业规程》内容：
{work_text[:8000]}

请以JSON格式输出检测结果，格式如下：
[
  {{"技术要点": "规程原文片段", "待审文段描述": "作业规程中的相关描述", "不一致地方": "具体不一致内容或无", "建议修改为": "修改建议"}},
  ...
]

只输出JSON数组，不要其他内容。"""

response = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": prompt}],
    temperature=0
)

import json
results = json.loads(response.choices[0].message.content)

for i, item in enumerate(results, 1):
    item['序号'] = i

df = pd.DataFrame(results)
df = df[['序号', '技术要点', '待审文段描述', '不一致地方', '建议修改为']]
df.to_excel("合规性检测结果.xlsx", index=False, engine='openpyxl')
print("检测完成，结果已保存到: 合规性检测结果.xlsx")
