"""调试RAG chain的原始输出"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
from langchain_openai import ChatOpenAI
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from app.core.config import settings
from app.services.vector_store import vector_store_service

async def test_raw_rag():
    llm = ChatOpenAI(
        api_key=settings.OPENAI_API_KEY,
        base_url=settings.OPENAI_BASE_URL,
        model="gpt-3.5-turbo",
        temperature=0.7
    )

    retriever = vector_store_service.vector_store.as_retriever(
        search_kwargs={"k": 6}
    )

    system_prompt = (
        "你是一个煤炭行业规程专家助手。\n\n"
        "**重要规则：**\n"
        "1. 你必须严格基于以下【参考内容】中的原文来回答问题\n"
        "2. 如果问题涉及具体数字、条款、规定，必须直接引用原文，不可臆测或修改\n"
        "3. 如果参考内容中没有明确答案，必须明确告知用户\n"
        "4. 回答时请标注引用的条款编号（如有）\n"
        "5. 禁止使用自己的知识补充或修改参考内容中的数据\n\n"
        "【参考内容】：\n{context}\n\n"
        "请基于以上参考内容回答问题。"
    )

    prompt = ChatPromptTemplate.from_messages([
        ("system", system_prompt),
        ("placeholder", "{chat_history}"),
        ("human", "{input}"),
    ])

    question_answer_chain = create_stuff_documents_chain(llm, prompt)
    rag_chain = create_retrieval_chain(retriever, question_answer_chain)

    question = "极薄煤层是不考虑倾角因素下，厚度多少米以下的煤层"

    print(f"问题: {question}\n")
    print("=" * 80)
    print("RAG Chain 原始输出:\n")

    chunk_num = 0
    async for chunk in rag_chain.astream({"input": question, "chat_history": []}):
        chunk_num += 1
        print(f"\n{'='*80}")
        print(f"Chunk #{chunk_num}")
        print(f"Type: {type(chunk)}")
        print(f"Is dict: {isinstance(chunk, dict)}")
        if isinstance(chunk, dict):
            print(f"Keys: {chunk.keys()}")
            for key, value in chunk.items():
                if key == "answer":
                    print(f"\n  answer: {repr(value)}")
                    print(f"  answer length: {len(value) if isinstance(value, str) else 'N/A'}")
                elif key == "context":
                    print(f"\n  context: <{len(value)} documents>")
                else:
                    print(f"\n  {key}: {repr(value)[:100]}...")
        else:
            print(f"Content: {repr(chunk)}")

if __name__ == "__main__":
    asyncio.run(test_raw_rag())
