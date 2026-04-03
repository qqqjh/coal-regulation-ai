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
        content = "第七条 企业必须建立健全安全生产责任制。企业必须建立健全安全生产责任制。\n第八条 矿长是安全生产的第一责任人，对安全生产工作全面负责，必须确保安全。"
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

    print("正在运行 智能体3：内容重复检测...")
    try:
        new_state = await document_review_service.agent_3_duplicate_detection(state)
        
        print(f"\n=== 检测结果 (找到 {len(new_state['duplicate_detections'])} 处) ===")
        for i, item in enumerate(new_state["duplicate_detections"], 1):
            print(f"\n[{i}]")
            print(f"内容1: {item.get('content_1')}")
            print(f"内容2: {item.get('content_2')}")
            print(f"相似度: {item.get('similarity')}")
            print(f"建议: {item.get('suggestion')}")

    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
