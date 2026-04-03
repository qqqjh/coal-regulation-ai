import chromadb
from pathlib import Path

# 连接到ChromaDB
chroma_path = Path("data/chroma_db")
client = chromadb.PersistentClient(path=str(chroma_path))

# 删除旧的documents collection
try:
    client.delete_collection(name="documents")
    print("已删除旧的 'documents' collection")
except Exception as e:
    print(f"删除失败: {e}")

# 列出剩余的collections
collections = client.list_collections()
print(f"\n剩余 {len(collections)} 个collection:")
for col in collections:
    print(f"  - {col.name}: {col.count()}个文档")
