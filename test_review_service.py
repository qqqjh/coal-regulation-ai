"""
直接测试document_review_service
"""
import asyncio
import sys
sys.path.insert(0, 'backend')

from app.services.document_review_service import document_review_service

async def test_review():
    content = """第一章 总则
第一条 为了加强煤矿安全生产管理，保障职工生命安全,根剧《煤矿安全规程》制定本制度。
第二条 煤矿企业必须坚持"安全第一、预防为主、综和治理"的方针。

第二章 瓦斯管理
第三条 矿井必须建立瓦斯检测制度。瓦斯检测应当每4小时进行一次检测。
第四条 采掘工作面的瓦斯浓度不得超过1.5%，当瓦斯浓度达到1.5%时必须停止作业。

第三章 通风系统
第五条 矿井必须建立完善的通风系统，主要通风机应当安装在井下。
第六条 通风系统应当定期检查维护，防爆门每年检查维修1次。

第四章 安全责任
第七条 企业必须建立健全安全生产责任制。企业必须建立健全安全生产责任制。
第八条 矿长是安全生产的第一责任人，对安全生产工作全面负责，必须确保安全。"""

    print("Starting review...")
    try:
        result = await document_review_service.review_document(content)
        print(f"\n=== Review Result ===")
        print(f"Total changes: {result['total_changes']}")
        print(f"Typo count: {result['typo_count']}")
        print(f"Fluency count: {result['fluency_count']}")
        print(f"Duplicate count: {result['duplicate_count']}")
        print(f"Compliance count: {result['compliance_count']}")
        print(f"\nErrors: {result['errors']}")

        print(f"\n=== All Changes ===")
        for i, change in enumerate(result['all_changes'][:5], 1):
            print(f"\n{i}. ID: {change.get('id')}, Type: {change.get('type')}")
            print(f"   Category: {change.get('category')}")
            if 'original' in change:
                print(f"   Original: {change['original'][:100]}")
            if 'corrected' in change:
                print(f"   Corrected: {change['corrected'][:100]}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_review())
