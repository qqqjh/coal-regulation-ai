import chromadb
from pathlib import Path

# 连接到ChromaDB
chroma_path = Path("data/chroma_db")
client = chromadb.PersistentClient(path=str(chroma_path))

# 列出所有collection
collections = client.list_collections()

print(f"找到 {len(collections)} 个collection:")
for col in collections:
    print(f"\n集合名称: {col.name}")
    print(f"集合ID: {col.id}")

    # 获取collection中的文档数量
    count = col.count()
    print(f"文档数量: {count}")

    # 获取一些样本数据
    if count > 0:
        results = col.get(limit=3)
        if results and results.get('metadatas'):
            print("样本元数据:")
            for meta in results['metadatas'][:3]:
                print(f"  - {meta}")
