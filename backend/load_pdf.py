"""
加载煤矿安全规程PDF到向量数据库
"""
import asyncio
import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from app.services.vector_store import vector_store_service
from app.core.config import settings

async def load_pdf():
    # PDF文件路径
    pdf_path = Path(__file__).parent.parent / "煤矿安全规程.pdf"

    if not pdf_path.exists():
        print(f"错误: 找不到文件 {pdf_path}")
        return

    print(f"开始加载文件: {pdf_path}")
    print(f"文件大小: {pdf_path.stat().st_size / 1024 / 1024:.2f} MB")

    # 创建一个模拟的文件对象
    class FileWrapper:
        def __init__(self, path):
            self.file = open(path, 'rb')
            self.filename = path.name

    file_obj = FileWrapper(pdf_path)

    try:
        result = await vector_store_service.process_file(file_obj, file_obj.filename)
        print(f"处理结果: {result}")

        if result == "Success":
            print("[SUCCESS] PDF文件已成功加载到向量数据库！")
            print(f"向量数据库位置: {settings.CHROMA_DB_DIR}")

            # 测试检索
            print("\n测试检索功能...")
            test_query = "煤矿安全"
            results = vector_store_service.search(test_query, k=3)
            print(f"检索关键词: {test_query}")
            print(f"找到 {len(results)} 个相关文档片段")

            if results:
                print("\n第一个片段预览:")
                print(results[0].page_content[:200] + "...")
        else:
            print(f"[ERROR] 处理失败: {result}")

    except Exception as e:
        print(f"[ERROR] 发生错误: {str(e)}")
        import traceback
        traceback.print_exc()

    finally:
        file_obj.file.close()

if __name__ == "__main__":
    print("=" * 60)
    print("煤矿安全规程PDF加载工具")
    print("=" * 60)
    asyncio.run(load_pdf())
