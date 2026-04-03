import chromadb

# 连接到ChromaDB
client = chromadb.PersistentClient(path="backend/data/chroma_db")

# 获取所有collection
collections = client.list_collections()

print(f"ChromaDB中的collection数量: {len(collections)}")
print("\nCollection列表:")
for col in collections:
    print(f"  - {col.name} (count: {col.count()})")
