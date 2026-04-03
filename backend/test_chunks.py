"""测试文档分块获取"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from app.services.vector_store import vector_store_service

# 获取文档分块
filename = "煤矿安全规程.pdf"
chunks = vector_store_service.get_document_chunks(filename, limit=5)

print(f"文档: {filename}")
print(f"找到 {len(chunks)} 个分块\n")

for chunk in chunks:
    print(f"========== 分块 {chunk['id']} ==========")
    print(f"页码: {chunk.get('page', 'N/A')}")
    print(f"内容预览: {chunk['content'][:200]}...")
    print()
