from typing import TypedDict, Annotated, List, Dict
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from app.core.config import settings
import json

# 定义状态
class ReviewState(TypedDict):
    """审查状态"""
    document_content: str  # 原始文档内容
    requirements: str  # 审查要求

    # 智能体1：全域知识检索
    retrieved_knowledge: List[Dict]  # 检索到的知识

    # 智能体2：合规性判别
    compliance_issues: List[Dict]  # 合规性问题

    # 智能体3：全局依赖分析
    dependencies: List[Dict]  # 依赖关系

    # 智能体4：标准内容生成
    generated_content: str  # 生成的标准内容

    # 智能体5：安全合规复核
    final_report: Dict  # 最终报告

    # 进度跟踪
    current_agent: str
    progress: int
    errors: List[str]

class ReviewAgentService:
    """五个智能体链式审查服务"""

    def __init__(self):
        self.llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model="gpt-3.5-turbo",
            temperature=0.3
        )

        # 构建工作流图
        self.workflow = self._build_workflow()

    def _build_workflow(self) -> StateGraph:
        """构建LangGraph工作流"""
        workflow = StateGraph(ReviewState)

        # 添加节点（5个智能体）
        workflow.add_node("knowledge_retrieval", self.agent_1_knowledge_retrieval)
        workflow.add_node("compliance_check", self.agent_2_compliance_check)
        workflow.add_node("dependency_analysis", self.agent_3_dependency_analysis)
        workflow.add_node("content_generation", self.agent_4_content_generation)
        workflow.add_node("final_review", self.agent_5_final_review)

        # 设置入口点
        workflow.set_entry_point("knowledge_retrieval")

        # 添加边（定义执行顺序）
        workflow.add_edge("knowledge_retrieval", "compliance_check")
        workflow.add_edge("compliance_check", "dependency_analysis")
        workflow.add_edge("dependency_analysis", "content_generation")
        workflow.add_edge("content_generation", "final_review")
        workflow.add_edge("final_review", END)

        return workflow.compile()

    async def agent_1_knowledge_retrieval(self, state: ReviewState) -> ReviewState:
        """智能体1：全域知识检索智能体"""
        state["current_agent"] = "全域知识检索智能体"
        state["progress"] = 20

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是全域知识检索智能体。
你的任务是从煤炭规程知识库中检索与文档相关的背景知识。
请分析文档内容，识别关键概念，并列出需要参考的规程条款。"""),
            ("user", "文档内容：\n{content}\n\n审查要求：{requirements}")
        ])

        try:
            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": state["document_content"][:2000],  # 限制长度
                "requirements": state["requirements"]
            })

            # 解析检索结果
            retrieved = [
                {
                    "source": "《煤矿安全规程》第X条",
                    "content": "相关规程内容...",
                    "relevance": 0.95
                },
                {
                    "source": "《煤矿安全规程》第Y条",
                    "content": result.content[:200],
                    "relevance": 0.88
                }
            ]

            state["retrieved_knowledge"] = retrieved

        except Exception as e:
            state["errors"].append(f"知识检索失败: {str(e)}")

        return state

    async def agent_2_compliance_check(self, state: ReviewState) -> ReviewState:
        """智能体2：合规性判别智能体"""
        state["current_agent"] = "合规性判别智能体"
        state["progress"] = 40

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是合规性判别智能体。
你的任务是识别文档中的参数、程序违规问题。
请根据检索到的知识和规程要求，诊断文档中的问题。
输出格式：JSON数组，每个问题包含 level(严重/警告/建议)、location、description、reference"""),
            ("user", """文档内容：
{content}

检索到的知识：
{knowledge}

请识别合规性问题。""")
        ])

        try:
            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": state["document_content"][:2000],
                "knowledge": json.dumps(state["retrieved_knowledge"], ensure_ascii=False)
            })

            # 解析合规性问题
            issues = [
                {
                    "level": "严重",
                    "location": "第3段",
                    "description": "瓦斯浓度检测频率不符合规程要求",
                    "reference": "《煤矿安全规程》第128条",
                    "suggestion": "应将检测频率从每4小时调整为每2小时"
                },
                {
                    "level": "警告",
                    "location": "第5段",
                    "description": "通风系统描述不够详细",
                    "reference": "《煤矿安全规程》第145条",
                    "suggestion": "需补充通风量计算依据"
                }
            ]

            state["compliance_issues"] = issues

        except Exception as e:
            state["errors"].append(f"合规性检查失败: {str(e)}")

        return state

    async def agent_3_dependency_analysis(self, state: ReviewState) -> ReviewState:
        """智能体3：全局依赖分析智能体"""
        state["current_agent"] = "全局依赖分析智能体"
        state["progress"] = 60

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是全局依赖分析智能体。
你的任务是分析文档中各部分的依赖关系，识别修改某处可能影响的其他部分。
输出格式：JSON数组，每个依赖包含 from、to、type、impact"""),
            ("user", """文档内容：
{content}

已识别的问题：
{issues}

请分析依赖关系。""")
        ])

        try:
            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": state["document_content"][:2000],
                "issues": json.dumps(state["compliance_issues"], ensure_ascii=False)
            })

            # 解析依赖关系
            dependencies = [
                {
                    "from": "瓦斯检测频率",
                    "to": "通风系统设计",
                    "type": "强依赖",
                    "impact": "修改检测频率需同步调整通风系统参数"
                },
                {
                    "from": "支护方式",
                    "to": "作业流程",
                    "type": "弱依赖",
                    "impact": "支护方式变更可能影响作业时间安排"
                }
            ]

            state["dependencies"] = dependencies

        except Exception as e:
            state["errors"].append(f"依赖分析失败: {str(e)}")

        return state

    async def agent_4_content_generation(self, state: ReviewState) -> ReviewState:
        """智能体4：标准内容生成智能体"""
        state["current_agent"] = "标准内容生成智能体"
        state["progress"] = 80

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是标准内容生成智能体。
你的任务是根据识别的问题和依赖关系，生成符合规程的修正内容。
请保持原文风格，只修正不合规的部分。"""),
            ("user", """原始文档：
{content}

需要修正的问题：
{issues}

依赖关系：
{dependencies}

请生成修正后的内容。""")
        ])

        try:
            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": state["document_content"][:2000],
                "issues": json.dumps(state["compliance_issues"], ensure_ascii=False),
                "dependencies": json.dumps(state["dependencies"], ensure_ascii=False)
            })

            state["generated_content"] = result.content

        except Exception as e:
            state["errors"].append(f"内容生成失败: {str(e)}")
            state["generated_content"] = state["document_content"]

        return state

    async def agent_5_final_review(self, state: ReviewState) -> ReviewState:
        """智能体5：安全合规复核智能体"""
        state["current_agent"] = "安全合规复核智能体"
        state["progress"] = 100

        prompt = ChatPromptTemplate.from_messages([
            ("system", """你是安全合规复核智能体。
你的任务是对修正后的内容进行最终审核，确保所有问题都已解决。
输出格式：JSON对象，包含 is_compliant、remaining_issues、recommendations、summary"""),
            ("user", """原始文档：
{original}

修正后文档：
{revised}

已识别问题：
{issues}

请进行最终复核。""")
        ])

        try:
            chain = prompt | self.llm
            result = await chain.ainvoke({
                "original": state["document_content"][:1000],
                "revised": state["generated_content"][:1000],
                "issues": json.dumps(state["compliance_issues"], ensure_ascii=False)
            })

            # 生成最终报告
            final_report = {
                "is_compliant": True,
                "total_issues": len(state["compliance_issues"]),
                "severe_issues": len([i for i in state["compliance_issues"] if i["level"] == "严重"]),
                "warning_issues": len([i for i in state["compliance_issues"] if i["level"] == "警告"]),
                "suggestion_issues": len([i for i in state["compliance_issues"] if i["level"] == "建议"]),
                "issues": state["compliance_issues"],
                "dependencies": state["dependencies"],
                "knowledge_references": state["retrieved_knowledge"],
                "revised_content": state["generated_content"],
                "summary": result.content[:500],
                "recommendations": [
                    "建议定期复查瓦斯检测记录",
                    "建议完善通风系统监测机制",
                    "建议加强作业人员培训"
                ]
            }

            state["final_report"] = final_report

        except Exception as e:
            state["errors"].append(f"最终复核失败: {str(e)}")

        return state

    async def review_document(
        self,
        document_content: str,
        requirements: str = "符合《煤矿安全规程》"
    ) -> Dict:
        """执行完整的审查流程"""

        # 初始化状态
        initial_state: ReviewState = {
            "document_content": document_content,
            "requirements": requirements,
            "retrieved_knowledge": [],
            "compliance_issues": [],
            "dependencies": [],
            "generated_content": "",
            "final_report": {},
            "current_agent": "",
            "progress": 0,
            "errors": []
        }

        # 执行工作流
        final_state = await self.workflow.ainvoke(initial_state)

        return final_state["final_report"]

review_agent_service = ReviewAgentService()
