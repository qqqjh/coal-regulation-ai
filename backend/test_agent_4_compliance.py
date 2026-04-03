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
        content = "第四条 采掘工作面的瓦斯浓度不得超过1.5%，当瓦斯浓度达到1.5%时必须停止作业。\n（注：规程可能规定是1.0%）"
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

    print("正在运行 智能体4：合规性验证...")
    try:
        new_state = await document_review_service.agent_4_compliance_verification(state)
        
        print(f"\n=== 检测结果 (找到 {len(new_state['compliance_issues'])} 处) ===")
        for i, item in enumerate(new_state["compliance_issues"], 1):
            print(f"\n[{i}]")
            print(f"问题: {item.get('issue')}")
            print(f"原文: {item.get('original')}")
            print(f"建议: {item.get('suggestion')}")
            print(f"参考: {item.get('reference')}")
            print(f"严重程度: {item.get('severity')}")

    except Exception as e:
        print(f"运行出错: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
