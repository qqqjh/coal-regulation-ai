from typing import TypedDict, List, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain.agents import Tool, AgentExecutor, create_react_agent
from langchain_core.prompts import PromptTemplate
from app.core.config import settings
from app.services.vector_store import vector_store_service
import json
import re
from difflib import SequenceMatcher
import logging

logger = logging.getLogger(__name__)

# 定义审查状态
class DocumentReviewState(TypedDict):
    """文档审查状态"""
    original_content: str  # 原始文档内容
    paragraphs: List[str]  # 分段后的内容

    # 各智能体的检测结果
    typo_corrections: List[Dict]  # 错别字修正
    fluency_improvements: List[Dict]  # 语义通顺性改进
    duplicate_detections: List[Dict]  # 重复内容检测
    compliance_issues: List[Dict]  # 合规性问题

    # 最终结果
    all_changes: List[Dict]  # 所有修改建议
    revised_content: str  # 修改后的内容

    # 进度跟踪
    current_agent: str
    progress: int
    errors: List[str]

    # 知识库ID
    kb_id: int


class DocumentReviewService:
    """文档审查修正服务 - 4个专门智能体"""

    def __init__(self):
        self.llm = ChatOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL,
            model="gpt-4o",
            temperature=0.1,  # 稍微增加温度以避免过于保守
            seed=666  # 明确指定随机种子
        )

        # 构建工作流图
        self.workflow = self._build_workflow()

    def _extract_json_from_text(self, text: str) -> List[Dict]:
        """从文本中提取JSON数组"""
        try:
            # 尝试直接解析
            return json.loads(text)
        except:
            pass

        # 尝试提取JSON数组 (使用正则)
        json_pattern = r'\[[\s\S]*\]'
        matches = re.findall(json_pattern, text)

        for match in matches:
            try:
                parsed = json.loads(match)
                if isinstance(parsed, list):
                    logger.info(f"成功从文本中提取JSON数组，包含 {len(parsed)} 项")
                    return parsed
            except:
                continue

        # 尝试提取单个JSON对象并转为数组
        obj_pattern = r'\{[\s\S]*?\}'
        matches = re.findall(obj_pattern, text)

        results = []
        for match in matches:
            try:
                parsed = json.loads(match)
                if isinstance(parsed, dict):
                    results.append(parsed)
            except:
                continue

        if results:
            logger.info(f"从文本中提取 {len(results)} 个JSON对象")
            return results

        logger.warning(f"无法从以下文本中提取JSON:\n{text[:500]}")
        return []

    def _build_workflow(self) -> StateGraph:
        """构建LangGraph工作流"""
        workflow = StateGraph(DocumentReviewState)

        # 添加节点（4个智能体）
        workflow.add_node("typo_detection", self.agent_1_typo_detection)
        workflow.add_node("fluency_analysis", self.agent_2_fluency_analysis)
        workflow.add_node("duplicate_detection", self.agent_3_duplicate_detection)
        workflow.add_node("compliance_verification", self.agent_4_compliance_verification)
        workflow.add_node("merge_results", self.merge_all_results)

        # 设置入口点
        workflow.set_entry_point("typo_detection")

        # 添加边（并行执行后合并）
        workflow.add_edge("typo_detection", "fluency_analysis")
        workflow.add_edge("fluency_analysis", "duplicate_detection")
        workflow.add_edge("duplicate_detection", "compliance_verification")
        workflow.add_edge("compliance_verification", "merge_results")
        workflow.add_edge("merge_results", END)

        return workflow.compile()

    def _create_rag_tool(self, kb_id: int = 3):
        """创建RAG检索工具"""
        def search_knowledge(query: str) -> str:
            """从知识库检索相关规程"""
            try:
                docs = vector_store_service.search(query, kb_id=kb_id, k=3)
                if not docs:
                    return "未找到相关规程"

                results = []
                for i, doc in enumerate(docs, 1):
                    filename = doc.metadata.get("filename", "unknown")
                    page = doc.metadata.get("page", 0)
                    content = doc.page_content[:200]
                    results.append(f"[{i}] {filename} 第{page}页:\n{content}")

                return "\n\n".join(results)
            except Exception as e:
                return f"检索失败: {str(e)}"

        return Tool(
            name="search_regulations",
            func=search_knowledge,
            description="从煤矿安全规程知识库中检索相关条款。输入：查询关键词，输出：相关规程内容"
        )

    def _create_text_analysis_tool(self):
        """创建文本分析工具"""
        def analyze_text(text: str) -> str:
            """分析文本的基本统计信息"""
            words = len(text)
            sentences = len(re.split(r'[。！？]', text))
            return f"字数: {words}, 句子数: {sentences}"

        return Tool(
            name="analyze_text",
            func=analyze_text,
            description="分析文本的基本统计信息。输入：文本内容，输出：字数和句子数"
        )

    async def agent_1_typo_detection(self, state: DocumentReviewState) -> DocumentReviewState:
        """智能体1：错别字检测智能体（RAG增强）"""
        state["current_agent"] = "错别字检测智能体"
        state["progress"] = 25

        try:
            content_preview = state["original_content"][:2000]
            kb_id = state.get("kb_id", 3)  # 从状态中获取知识库ID

            # 先从文档中提取关键词进行RAG检索
            rag_context = ""
            try:
                # 检索相关规程获取标准术语
                docs = vector_store_service.search(content_preview[:200], kb_id=kb_id, k=3)
                if docs:
                    rag_context = "\n\n".join([f"【参考规程{i+1}】\n{doc.page_content[:300]}" for i, doc in enumerate(docs)])
            except Exception as e:
                logger.warning(f"RAG检索失败: {e}")

            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是专业的错别字检测智能体。

**你的身份和职责：**
你是煤炭行业规程审查专家，专门负责检测文档中的错别字、拼写错误、标点符号错误。

**重要规则：**
1. 你必须对照【参考规程】中的标准术语来判断是否有错别字
2. 如果参考规程中有标准术语，必须以规程为准，不可使用自己的知识臆测
3. 对于专业术语，必须严格对照参考规程，确保术语准确性
4. 如果参考规程中没有相关内容，则基于常识判断明显的错别字
5. 只输出纯JSON数组，不要任何解释

**检测重点：**
1. 同音字错误（如：根据→根剧、综合→综和）
2. 形近字错误（如：安装→女装）
3. 专业术语错误（对照参考规程中的标准术语）
4. 标点符号错误

**【参考规程】（标准术语）：**
{rag_context}

**输出格式：**
如果没有发现错别字，返回：[]
如果发现错别字，返回：
[{{"paragraph_index": 0, "original": "错误文本", "corrected": "修正文本", "position": "具体位置", "reason": "错误原因（如：同音字错误、与规程标准术语不符）"}}]"""),
                ("user", "请检测以下文档的错别字：\n\n{content}")
            ])

            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": content_preview,
                "rag_context": rag_context if rag_context else "未检索到相关规程"
            })

            logger.info(f"错别字检测输出: {result.content[:500]}")

            corrections = self._extract_json_from_text(result.content)
            logger.info(f"找到 {len(corrections)} 个错别字")

            state["typo_corrections"] = corrections

        except Exception as e:
            logger.error(f"错别字检测失败: {str(e)}")
            state["errors"].append(f"错别字检测失败: {str(e)}")
            state["typo_corrections"] = []

        return state

    async def agent_2_fluency_analysis(self, state: DocumentReviewState) -> DocumentReviewState:
        """智能体2：语义通顺性分析智能体（RAG增强）"""
        state["current_agent"] = "语义通顺性分析智能体"
        state["progress"] = 50

        try:
            content_preview = state["original_content"][:2000]
            
            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是语义通顺性分析智能体。

**你的身份和职责：**
你是煤炭行业规程审查专家，专门负责检测文档中的语法错误、表达不清、语句不通顺的问题。

**重要规则：**
1. 你必须对照【参考规程】中的标准表述来判断文档表述是否规范
2. 如果参考规程中有标准表述，必须以规程为准，不可使用自己的知识臆测
3. 特别注意重复啰嗦的表述，必须指出并改进
4. 不要进行过度的缩略修改（例如，不要将介词"以及"改为"及"，不要将"应该"改为"应"）
5. 如果参考规程中没有相关内容，则基于语言规范判断
6. 只输出纯JSON数组，不要任何解释

**检测重点：**
1. 语法错误（主谓不一致、成分残缺等）
2. 表达不清（歧义、指代不明等）
3. 语句不通顺（逻辑混乱、衔接不当、重复啰嗦等）
4. 与规程标准表述不一致的地方

**【参考规程】（标准表述）：**
本规程中的“必须”“严禁”“应当”“可以”等说明如下：表示很严格，非这样做不可的，正面词一般用“必须”，反面词用“严禁”；表示严格，在正常情况下均应当这样做的，正面词一般用“应当”，反面词一般用“不应”或者“不得”；表示允许选择，在一定条件下可以这样做的，采用“可以”。
附录 主要名词解释
煤矿企业：从事煤炭生产和煤矿建设具有法人资格的企业，是煤矿的上级公司。
煤矿：直接从事煤炭生产和煤矿建设的业务单元。
下料孔：在煤矿生产或者建设期间，从地面施工的与井下巷道相连接且内衬耐磨管材的钻孔，通常用于输送砂石等松散材料。
全断面巷道掘进机：采用刀盘一次性全断面破岩掘进和同步支护的专用机械设备，简称 TBM（Tunnel Boring Machine）。
煤矿建设项目：新建、改建、扩建煤矿工程项目的统称。
开采深度：主井井口标高与开采的采煤工作面最低标高之间的差。
极薄煤层：不考虑倾角因素下，厚度 0.8m 以下的煤层。
综合机械化单元密实充填采煤工艺：一种置换充填采煤法，充填开采单元采用“U”型布置，按照设计尺寸将待回收煤炭资源划分为若干标准块段，其主要作业流程包括：首先采用综合机械化设备回收每个支巷的煤炭资源（掘进支巷）；煤炭资源回收完成后，立即采用构筑物对支巷两端头出口进行封闭，便于充填和封堵漏风（隔离支巷）；隔离完成并具备相应条件后，开始对其内部泵入充填料浆，进行密实充填（充填支巷）。一个充填开采单元除进风巷和回风巷外，包括掘进支巷、隔离支巷和充填支巷。
人工假顶：在厚煤层分层开采时，在顶板上铺设某些材料（如竹笆、金属网等），以形成下一层分层开采时的顶板。
沿空留巷：采用一定的技术手段将上一区段的巷道重新支护留给下一个区段使用，其做法是沿着采空区边缘施工人工构筑物隔离采空区，并对顶板进行支护，将原巷道原位保留下来。
沿空掘巷：完全沿采空区边缘或者小煤柱掘进，把巷道布置在位于靠煤柱一侧的低应力场，便于巷道维护，减少变形量。
倾斜巷道（斜巷）：井工开采时，整体倾角超过8°的巷道。
独立通风（并联通风）：井下用风地点的回风直接进入工作面回风巷、采（盘）区回风巷或者总回风巷，不再进入其他用风地点的通风方式。
分区通风：每个生产水平、每个生产采（盘）区的回风直接进入总回风巷或者回风井的通风方式。
专用回风巷：在采（盘）区巷道中，主要用于采（盘）区回风，不得用作常设行人、行车通道的巷道。
进风巷：进风风流所经过的巷道。
总进风巷：服务于全矿井或者矿井一个水平或者矿井一翼或者多个采（盘）区的进风巷道。
采（盘）区进风巷：服务于1个采（盘）区进风用的巷道。
工作面进风巷：服务于1个工作面进风用的巷道。
回风巷：回风风流所经过的巷道。
总回风巷：服务于全矿井或者矿井一个水平或者矿井一翼或者多个采（盘）区的回风巷道。
采（盘）区回风巷：服务于1个采（盘）区回风用的巷道。
工作面回风巷：服务于1个工作面回风用的巷道。
一风吹：在巷道排放瓦斯或者恢复通风过程中，没有采取可靠的控制风量排放瓦斯的措施，造成排出瓦斯与全风压风流混合处的甲烷或者二氧化碳浓度超过规定值的排放方法。
井巷揭煤：立井、斜井、平硐、石门自底（顶）板岩柱穿过煤层进入顶（底）板的全部作业过程。
煤与瓦斯突出：在地应力和瓦斯（二氧化碳）的共同作用下，破碎的煤、岩和瓦斯（二氧化碳）由煤体或者岩体内突然向采掘空间抛出的异常动力现象。
突出预兆：煤与瓦斯突出发生前出现的异常现象。分为有声突出预兆（如劈裂声、闷雷声、煤炮声等）和无声突出预兆（如顶板压力增大、煤层层理紊乱、煤壁被挤出、煤壁温度明显降低、煤壁挂汗、喷孔、顶钻、卡钻、瓦斯涌出忽大忽小等）两类。
采动应力叠加区域：煤矿井下受两个以上采、掘工作面影响而形成的合成应力影响区域，其影响因素主要包括主应力角度、断层间距大小、煤柱的稳定性等。
水力挤出（挤压）：在采掘工作面施工孔深一般不大于15m的钻孔并封孔，向孔内注入高压水使煤体挤压开裂并向外移动，以释放瓦斯、卸除应力为目的的局部防突措施。
应力集中区：应力在一定范围内明显增高的区域。
冲击地压预卸压：经评价具有冲击地压危险的区域，在监测未达到预警临界值前实施的预防性卸压措施。
“掏根”式开采：违反采矿设计提高露天煤矿边帮最终边坡角，不留保安平盘或者提高单台阶坡面角进行井段开采的采煤方法。
重点边坡：上下有重要建（构）筑物及人员、设备的边坡。
危险边坡：滑坡危险性鉴定中稳定系数不满足安全储备系数的边坡。
复合边坡：由外排土场边坡和采场边坡、内排土场边坡和采场边坡、内排土场边坡和外排土场边坡以及采场边坡组成的边坡。
外委剥离工程承包单位：由煤矿企业或者煤矿委托开展露天煤矿坑下土岩等剥离物装、运输、排弃的单位。
本质安全型：电气设备的一种防爆型式，将设备内部和暴露于爆炸性环境的连接导线可能产生的电火花或者热效应能量限制在不能产生点燃的水平。

**输出格式：**
如果没有发现问题，返回：[]
如果发现问题，返回：
[{{"paragraph_index": 0, "original": "原文", "improved": "改进后", "position": "具体位置", "issue": "问题描述（如：表述重复啰嗦、与规程标准表述不符）"}}]"""),
                ("user", "请分析以下文档的语义通顺性：\n\n{content}")
            ])

            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": content_preview
            })

            logger.info(f"语义分析输出: {result.content[:500]}")

            improvements = self._extract_json_from_text(result.content)
            logger.info(f"找到 {len(improvements)} 个问题")

            state["fluency_improvements"] = improvements

        except Exception as e:
            logger.error(f"语义分析失败: {str(e)}")
            state["errors"].append(f"语义分析失败: {str(e)}")
            state["fluency_improvements"] = []

        return state

    async def agent_3_duplicate_detection(self, state: DocumentReviewState) -> DocumentReviewState:
        """智能体3：内容重复检测智能体（RAG增强）"""
        state["current_agent"] = "内容重复检测智能体"
        state["progress"] = 75

        try:
            content_preview = state["original_content"][:2000]
            # kb_id = state.get("kb_id", 3)  # 从状态中获取知识库ID

            # RAG检索已注释
            # rag_context = ""
            # try:
            #     docs = vector_store_service.search(content_preview[:200], kb_id=kb_id, k=3)
            #     if docs:
            #         rag_context = "\n\n".join([f"【参考规程{i+1}】\n{doc.page_content[:300]}" for i, doc in enumerate(docs)])
            # except Exception as e:
            #     logger.warning(f"RAG检索失败: {e}")

            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是内容重复检测智能体。

**你的身份和职责：**
你是煤炭行业规程审查专家，专门负责检测文档中的重复内容、冗余表述。

**重要规则：**
1. 基于煤炭行业安全常识判断重复是否是必要的安全强调
2. 特别注意语义相同但表述不同的重复内容
3. 只输出纯JSON数组，不要任何解释

**检测重点：**
1. 完全重复的段落或句子
2. 语义重复但表述略有不同的内容（如："井下无人作业时，可以不实行矿领导带班下井"和"当井下没有任何人员进行作业活动时，则可以不再安排矿领导带班下井"）
3. 同一段落内的重复啰嗦表述

**输出格式：**
如果没有发现问题，返回：[]
如果发现问题，返回：
[{{"paragraph_index_1": 0, "paragraph_index_2": 1, "content_1": "第一处内容", "content_2": "第二处内容", "similarity": 0.9, "suggestion": "建议删除或合并", "type": "duplicate"}}]"""),
                ("user", "请检测以下文档的重复内容：\n\n{content}")
            ])

            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": content_preview
            })

            logger.info(f"重复检测输出: {result.content[:500]}")

            duplicates = self._extract_json_from_text(result.content)
            logger.info(f"找到 {len(duplicates)} 个重复问题")

            state["duplicate_detections"] = duplicates

        except Exception as e:
            logger.error(f"重复检测失败: {str(e)}")
            state["errors"].append(f"重复检测失败: {str(e)}")
            state["duplicate_detections"] = []

        return state

    async def agent_4_compliance_verification(self, state: DocumentReviewState) -> DocumentReviewState:
        """智能体4：技术标准合规性验证智能体（RAG增强）"""
        state["current_agent"] = "技术标准合规性验证智能体"
        state["progress"] = 90

        try:
            content_preview = state["original_content"][:2000]
            kb_id = state.get("kb_id", 3)  # 从状态中获取知识库ID

            # RAG检索相关规程条款
            rag_context = ""
            try:
                docs = vector_store_service.search(content_preview[:200], kb_id=kb_id, k=5)
                if docs:
                    rag_context = "\n\n".join([f"【参考规程{i+1}】\n{doc.page_content[:400]}" for i, doc in enumerate(docs)])
            except Exception as e:
                logger.warning(f"RAG检索失败: {e}")

            prompt = ChatPromptTemplate.from_messages([
                ("system", """你是技术标准合规性验证智能体。

**你的身份和职责：**
你是煤炭行业规程审查专家，专门负责验证文档内容是否符合技术标准和规程要求。

**重要规则：**
1. 你必须严格对照【参考规程】中的标准条款来验证文档内容
2. 如果文档中的技术参数、数值、表述与参考规程不一致，必须指出
3. 特别注意数值、单位、条件等细节差异（如："大于5MPa" vs "大于或等于5MPa"）
4. 禁止使用自己的知识补充或修改参考规程中的标准
5. 只输出纯JSON数组，不要任何解释

**验证重点：**
1. 技术参数是否符合规程标准（如：瓦斯压力、煤层厚度等数值）
2. 安全标准是否符合规程要求
3. 操作规范是否符合规程规定
4. 专业术语使用是否准确

**【参考规程】（标准条款）：**
{rag_context}

**输出格式：**
如果没有发现问题，返回：[]
如果发现问题，返回：
[{{"paragraph_index": 0, "issue": "问题描述", "original": "文档原文", "suggestion": "修正建议（基于参考规程）", "reference": "参考规程条款", "severity": "严重程度（严重/一般）"}}]"""),
                ("user", "请验证以下文档的合规性：\n\n{content}")
            ])

            chain = prompt | self.llm
            result = await chain.ainvoke({
                "content": content_preview,
                "rag_context": rag_context if rag_context else "未检索到相关规程"
            })

            logger.info(f"合规性验证输出: {result.content[:500]}")

            issues = self._extract_json_from_text(result.content)
            logger.info(f"找到 {len(issues)} 个合规性问题")

            state["compliance_issues"] = issues

        except Exception as e:
            logger.error(f"合规性验证失败: {str(e)}")
            state["errors"].append(f"合规性验证失败: {str(e)}")
            state["compliance_issues"] = []

        return state

    def merge_all_results(self, state: DocumentReviewState) -> DocumentReviewState:
        """合并所有智能体的检测结果"""
        state["current_agent"] = "合并结果"
        state["progress"] = 100

        # 合并所有修改建议
        all_changes = []

        # 添加错别字修正
        for item in state["typo_corrections"]:
            all_changes.append({
                "id": len(all_changes) + 1,
                "type": "typo",
                "category": "错别字",
                **item
            })

        # 添加语义改进
        for item in state["fluency_improvements"]:
            all_changes.append({
                "id": len(all_changes) + 1,
                "type": "fluency",
                "category": "语义通顺性",
                **item
            })

        # 添加重复检测
        for item in state["duplicate_detections"]:
            all_changes.append({
                "id": len(all_changes) + 1,
                "type": "duplicate",
                "category": "内容重复",
                **item
            })

        # 添加合规性问题
        for item in state["compliance_issues"]:
            all_changes.append({
                "id": len(all_changes) + 1,
                "type": "compliance",
                "category": "技术标准合规性",
                **item
            })

        state["all_changes"] = all_changes

        # 生成修改后的内容（简化版，实际需要更复杂的逻辑）
        state["revised_content"] = state["original_content"]

        return state

    async def review_document(self, document_content: str, kb_id: int = 3) -> Dict[str, Any]:
        """执行完整的文档审查流程"""

        # 初始化状态
        initial_state: DocumentReviewState = {
            "original_content": document_content,
            "paragraphs": [p.strip() for p in document_content.split('\n') if p.strip()],
            "typo_corrections": [],
            "fluency_improvements": [],
            "duplicate_detections": [],
            "compliance_issues": [],
            "all_changes": [],
            "revised_content": "",
            "current_agent": "",
            "progress": 0,
            "errors": [],
            "kb_id": kb_id  # 保存知识库ID到状态中
        }

        # 执行工作流
        final_state = await self.workflow.ainvoke(initial_state)

        return {
            "original_content": final_state["original_content"],
            "revised_content": final_state["revised_content"],
            "all_changes": final_state["all_changes"],
            "total_changes": len(final_state["all_changes"]),
            "typo_count": len(final_state["typo_corrections"]),
            "fluency_count": len(final_state["fluency_improvements"]),
            "duplicate_count": len(final_state["duplicate_detections"]),
            "compliance_count": len(final_state["compliance_issues"]),
            "errors": final_state["errors"]
        }


# 创建全局实例
document_review_service = DocumentReviewService()
