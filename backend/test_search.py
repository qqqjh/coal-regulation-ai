"""测试向量检索"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from app.services.vector_store import vector_store_service

# 搜索相关内容
query = "开采有瓦斯喷出 突出危险 煤层垂距 串联通风"
results = vector_store_service.search(query, k=5)

print(f"搜索关键词: {query}")
print(f"找到 {len(results)} 个结果\n")

for i, doc in enumerate(results, 1):
    print(f"========== 结果 {i} ==========")
    print(f"来源: {doc.metadata.get('source', 'unknown')}")

    # 查找是否包含数字
    content = doc.page_content
    if '10' in content or '十' in content or '20' in content or '二十' in content:
        print(f"包含数字: ", end='')
        if '10' in content:
            print("10 ", end='')
        if '十' in content:
            print("十 ", end='')
        if '20' in content:
            print("20 ", end='')
        if '二十' in content:
            print("二十", end='')
        print()

    # 打印部分内容
    print(f"内容长度: {len(content)} 字符")
    print(f"内容预览:\n{content[:500]}\n")
