"""
Agentic RAG 测试脚本
功能：对待议文档中的每个条目分别进行 RAG 检索
"""
import asyncio
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from app.core.config import settings
from app.services.vector_store import vector_store_service
from typing import List, Dict
import json
import time

class AgenticRAG:
    def __init__(self, kb_id: int, model: str = "gpt-3.5-turbo"):
        self.kb_id = kb_id
        self.llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model=model,
            temperature=0
        )

        # 获取知识库的 vector store
        self.vector_store = vector_store_service._get_collection(kb_id)
        self.retriever = self.vector_store.as_retriever(search_kwargs={"k": 5})

    async def extract_questions(self, document: str) -> List[Dict[str, str]]:
        """
        步骤1：从待议文档中提取独立的问题/议题
        """
        extraction_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个文档分析专家。请分析用户提供的待议文档，提取出其中的所有独立问题或议题。

要求：
1. 识别文档中的每个独立问题、议题或需要查询的条目
2. 为每个问题生成一个简洁的标题和完整的问题描述
3. 如果文档只包含一个问题，也要提取出来
4. 返回 JSON 格式，格式如下：
{{
  "questions": [
    {{
      "id": 1,
      "title": "问题标题",
      "question": "完整的问题描述"
    }}
  ]
}}

注意：只返回 JSON，不要有其他文字。"""),
            ("human", "待议文档：\n{document}")
        ])

        chain = extraction_prompt | self.llm
        response = await chain.ainvoke({"document": document})

        try:
            # 尝试解析 JSON
            content = response.content.strip()
            # 移除可能的 markdown 代码块标记
            if content.startswith("```json"):
                content = content[7:]
            if content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()

            result = json.loads(content)
            return result.get("questions", [])
        except json.JSONDecodeError as e:
            print(f"JSON 解析失败: {e}")
            print(f"原始响应: {response.content}")
            # 如果解析失败，返回原始文档作为单个问题
            return [{"id": 1, "title": "原始查询", "question": document}]

    async def retrieve_for_question(self, question: str) -> List[str]:
        """
        步骤2：对单个问题进行 RAG 检索
        """
        docs = await self.retriever.ainvoke(question)
        return [doc.page_content for doc in docs]

    async def answer_question(self, question: str, context: List[str]) -> str:
        """
        步骤3：基于检索到的上下文回答问题
        """
        answer_prompt = ChatPromptTemplate.from_messages([
            ("system", """你是一个煤炭行业规程专家助手。

**重要规则：**
1. 你必须严格基于以下【参考内容】中的原文来回答问题
2. 如果问题涉及具体数字、条款、规定，必须直接引用原文，不可臆测或修改
3. 如果参考内容中没有明确答案，必须明确告知用户
4. 回答时请标注引用的条款编号（如有）
5. 禁止使用自己的知识补充或修改参考内容中的数据

【参考内容】：
{context}

请基于以上参考内容回答问题。"""),
            ("human", "{question}")
        ])

        context_text = "\n\n---\n\n".join(context)
        chain = answer_prompt | self.llm
        response = await chain.ainvoke({
            "question": question,
            "context": context_text
        })

        return response.content

    async def process_document(self, document: str) -> Dict:
        """
        完整的 Agentic RAG 流程
        """
        print("=" * 80)
        print("开始 Agentic RAG 处理")
        print("=" * 80)

        # 步骤1：提取问题
        print("\n[步骤1] 从待议文档中提取问题...")
        questions = await self.extract_questions(document)
        print(f"提取到 {len(questions)} 个问题：")
        for q in questions:
            print(f"  - [{q['id']}] {q['title']}")

        # 步骤2-3：对每个问题分别进行 RAG 检索和回答
        results = []
        for i, q in enumerate(questions):
            if i > 0:
                print(f"\n等待 5 秒以避免速率限制...")
                time.sleep(5)

            print(f"\n[步骤2] 为问题 [{q['id']}] 检索相关文档...")
            context = await self.retrieve_for_question(q['question'])
            print(f"检索到 {len(context)} 个相关文档片段")

            print(f"\n[步骤3] 生成答案...")
            answer = await self.answer_question(q['question'], context)

            results.append({
                "id": q['id'],
                "title": q['title'],
                "question": q['question'],
                "retrieved_chunks": len(context),
                "answer": answer
            })

            print(f"\n问题 [{q['id']}] 处理完成")
            print("-" * 80)

        return {
            "total_questions": len(questions),
            "results": results
        }


async def test_single_question():
    """测试1：单个问题"""
    print("\n" + "=" * 80)
    print("测试1：单个问题")
    print("=" * 80)

    agent = AgenticRAG(kb_id=3, model="gpt-4o")

    document = "极薄煤层是不考虑倾角因素下，厚度多少米以下的煤层？"
    result = await agent.process_document(document)

    print("\n" + "=" * 80)
    print("最终结果")
    print("=" * 80)
    for r in result['results']:
        print(f"\n问题 [{r['id']}]: {r['title']}")
        print(f"原始问题: {r['question']}")
        print(f"检索片段数: {r['retrieved_chunks']}")
        print(f"答案:\n{r['answer']}")


async def test_multiple_questions():
    """测试2：包含多个问题的待议文档"""
    print("\n" + "=" * 80)
    print("测试2：包含多个问题的待议文档")
    print("=" * 80)

    agent = AgenticRAG(kb_id=3, model="gpt-4o")

    document = """
第五条　带班矿领导必须对诸如采煤作业区、掘进工作面等重点区域和关键工序，以及包括石门揭煤、探放水作业、巷道贯通施工、煤仓清理、强制放顶操作、火区密闭与启封作业、动火作业，还有国家矿山安全监察局所明确界定的其他各类危险作业，开展现场的检查和巡视工作。当井下没有任何人员进行作业活动时，则可以不再安排矿领导带班下井；也就是说，在井下无作业人员的情况下，矿领导带班下井制度可暂不执行。

第六条　带班矿领导应当对采煤、掘进等重点部位、关键环节,以及石门揭煤、探放水、巷道贯通、清理煤仓、强制放顶、火区密闭和启封、动火以及国家矿山安全监察局规定的其他危险作业等进行现场检查巡视。井下无人作业时，可以不实行矿领导带班下井。

第七条　煤矿企业、煤矿必须支持工会等组织对煤矿安全生产与职业病危害防治工作的监督活动，发挥群众的监督作用，让群众监督发挥作用。

第十三条　有突出危险煤层的新建矿井必须先抽后建，首采区内有突出危险且瓦斯压力大于5MPa的煤层，必须进行地面钻井预抽，将瓦斯压力降至4MPa以下后，方可开工建设。

第二十条　煤矿露夫转井工开采或者井工转露天开采的，应当履行设计重大变更审查程序。
"""

    result = await agent.process_document(document)

    print("\n" + "=" * 80)
    print("最终结果汇总")
    print("=" * 80)
    print(f"共处理 {result['total_questions']} 个问题\n")

    for r in result['results']:
        print(f"\n{'=' * 80}")
        print(f"问题 [{r['id']}]: {r['title']}")
        print(f"{'=' * 80}")
        print(f"原始问题: {r['question']}")
        print(f"检索片段数: {r['retrieved_chunks']}")
        print(f"\n答案:\n{r['answer']}")


async def test_comparison():
    """测试3：对比传统 RAG 和 Agentic RAG"""
    print("\n" + "=" * 80)
    print("测试3：对比传统 RAG 和 Agentic RAG")
    print("=" * 80)

    document = """
第五条　带班矿领导必须对诸如采煤作业区、掘进工作面等重点区域和关键工序，以及包括石门揭煤、探放水作业、巷道贯通施工、煤仓清理、强制放顶操作、火区密闭与启封作业、动火作业，还有国家矿山安全监察局所明确界定的其他各类危险作业，开展现场的检查和巡视工作。当井下没有任何人员进行作业活动时，则可以不再安排矿领导带班下井；也就是说，在井下无作业人员的情况下，矿领导带班下井制度可暂不执行。

第六条　带班矿领导应当对采煤、掘进等重点部位、关键环节,以及石门揭煤、探放水、巷道贯通、清理煤仓、强制放顶、火区密闭和启封、动火以及国家矿山安全监察局规定的其他危险作业等进行现场检查巡视。井下无人作业时，可以不实行矿领导带班下井。
"""

    # 传统 RAG：把整个文档作为一个查询
    print("\n[传统 RAG] 把整个文档作为一个查询...")
    agent = AgenticRAG(kb_id=3, model="gpt-4o")
    context = await agent.retrieve_for_question(document)
    print(f"检索到 {len(context)} 个文档片段")
    traditional_answer = await agent.answer_question(document, context)

    print("\n传统 RAG 答案:")
    print(traditional_answer)

    # 等待避免速率限制
    print("\n等待 5 秒以避免速率限制...")
    time.sleep(5)

    # Agentic RAG：分别处理每个问题
    print("\n" + "-" * 80)
    print("[Agentic RAG] 分别处理每个问题...")
    result = await agent.process_document(document)

    print("\nAgentic RAG 答案:")
    for r in result['results']:
        print(f"\n问题 [{r['id']}]: {r['title']}")
        print(f"答案: {r['answer']}")


async def main():
    """运行所有测试"""
    print("Agentic RAG 测试脚本")
    print("=" * 80)

    # 测试1：单个问题
    await test_single_question()

    print("\n\n等待 10 秒后开始下一个测试...")
    time.sleep(10)

    # 测试2：多个问题
    await test_multiple_questions()

    print("\n\n等待 10 秒后开始下一个测试...")
    time.sleep(10)

    # 测试3：对比
    await test_comparison()


if __name__ == "__main__":
    asyncio.run(main())
