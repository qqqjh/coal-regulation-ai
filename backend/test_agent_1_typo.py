import asyncio
import os
import sys
import docx2txt

# 确保能导入 app 模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from app.services.document_review_service import document_review_service, DocumentReviewState

async def main():
    # 读取 ../待议.docx
    docx_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "待议.docx")
    if not os.path.exists(docx_path):
        print(f"未找到文件: {docx_path}")
        # 如果文件不存在，使用测试文本
        content = "第一章 总则\n第一条 为了加强煤矿安全生产管理，保障职工生命安全,根剧《煤矿安全规程》制定本制度。\n第二条 煤矿企业必须坚持\"安全第一、预防为主、综和治理\"的方针。"
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

    print("正在运行 智能体1：错别字检测...")
    try:
        new_state = await document_review_service.agent_1_typo_detection(state)
        
        print(f"\n=== 检测结果 (找到 {len(new_state['typo_corrections'])} 处) ===")
        for i, item in enumerate(new_state["typo_corrections"], 1):
            print(f"\n[{i}]")
            print(f"原文: {item.get('original')}")
            print(f"修正: {item.get('corrected')}")
            print(f"位置: {item.get('position')}")
            print(f"原因: {item.get('reason')}")
            
    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
