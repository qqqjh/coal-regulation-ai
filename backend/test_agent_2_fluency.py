import asyncio
import os
import sys
import docx2txt
import logging

# 配置日志输出到控制台
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# 确保能导入 app 模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.document_review_service import document_review_service, DocumentReviewState

async def main():
    # 读取 ../待议.docx
    docx_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "待议.docx")
    if not os.path.exists(docx_path):
        print(f"未找到文件: {docx_path}")
        content = "第五条 矿井必须建立完善的通风系统，主要通风机应当安装在井下。\n第六条 通风系统应当定期检查维护，防爆门每年检查维修1次。"
        print("使用的是默认测试文本。")
    else:
        print(f"正在读取文件: {docx_path}")
        content = docx2txt.process(docx_path)
        print(f"读取成功，共 {len(content)} 个字符")

    # 初始化状态
    state: DocumentReviewState = {
        "original_content": content,
        "paragraphs": [p.strip() for p in content.split('\n') if p.strip()],
        "typo_corrections": [],
        "fluency_improvements": [],
        "duplicate_detections": [],
        "compliance_issues": [],
        "all_changes": [],
        "revised_content": "",
        "current_agent": "",
        "progress": 0,
        "errors": [],
        "kb_id": 3
    }

    print("正在运行 智能体2：语义通顺性分析...")
    try:
        new_state = await document_review_service.agent_2_fluency_analysis(state)
        
        print(f"\n=== 检测结果 (找到 {len(new_state['fluency_improvements'])} 处) ===")
        for i, item in enumerate(new_state["fluency_improvements"], 1):
            print(f"\n[{i}]")
            print(f"原文: {item.get('original')}")
            print(f"改进: {item.get('improved')}")
            print(f"位置: {item.get('position')}")
            print(f"问题: {item.get('issue')}")

    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
