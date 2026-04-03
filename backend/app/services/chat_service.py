from langchain_openai import ChatOpenAI
from langchain.chains import create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.messages import HumanMessage, AIMessage
from app.core.config import settings
from app.services.vector_store import vector_store_service
from typing import List, Generator

class ChatService:
    def __init__(self):
        # 不再在初始化时创建固定的LLM实例
        pass

    def _get_llm(self, model: str = "gpt-3.5-turbo"):
        """根据模型名称创建LLM实例"""
        return ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model=model,
            temperature=0.7
        )

    async def chat_stream(self, messages: List[dict], use_rag: bool = True, kb_id: int = None, model: str = "gpt-3.5-turbo"):
        # 转换现有消息历史
        history = []
        # 保留最近几轮对话以适应上下文窗口，这里简单处理
        for msg in messages[:-1]:
            if msg['role'] == 'user':
                history.append(HumanMessage(content=msg['content']))
            elif msg['role'] == 'assistant':
                history.append(AIMessage(content=msg['content']))

        last_user_input = messages[-1]['content']

        # 创建对应模型的LLM实例
        llm = self._get_llm(model)

        if use_rag:
            # 如果没有指定知识库ID，需要提示用户
            if kb_id is None:
                yield "错误：使用RAG模式时必须指定知识库ID"
                return

            # 获取指定知识库的collection并创建retriever
            vector_store = vector_store_service._get_collection(kb_id)
            retriever = vector_store.as_retriever(
                search_kwargs={"k": 6}  # 增加检索数量
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

            # 流式输出RAG结果
            # 注意：LangChain的RAG chain每个chunk的answer字段只包含单个字符或增量内容
            async for chunk in rag_chain.astream({"input": last_user_input, "chat_history": history}):
                # LangChain的astream会返回不同的chunk类型
                if isinstance(chunk, dict) and "answer" in chunk:
                    # answer字段包含增量内容（通常是单个字符）
                    delta = chunk["answer"]
                    if isinstance(delta, str) and delta:
                        yield delta
                    
        else:
            # 普通对话模式
            prompt = ChatPromptTemplate.from_messages([
                ("system", "你是一个有用的助手。"),
                ("placeholder", "{chat_history}"),
                ("human", "{input}"),
            ])
            chain = prompt | llm
            async for chunk in chain.astream({"input": last_user_input, "chat_history": history}):
                yield chunk.content

chat_service = ChatService()
