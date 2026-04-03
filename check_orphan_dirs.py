import chromadb
import os

# 连接到ChromaDB
client = chromadb.PersistentClient(path="backend/data/chroma_db")

# 获取所有collection及其UUID
collections = client.list_collections()

print("活跃的collection及其UUID:")
active_uuids = set()
for col in collections:
    print(f"  - {col.name}: {col.id}")
    active_uuids.add(str(col.id))

print(f"\n活跃UUID数量: {len(active_uuids)}")

# 检查目录中的所有UUID文件夹
chroma_dir = "backend/data/chroma_db"
all_dirs = [d for d in os.listdir(chroma_dir)
            if os.path.isdir(os.path.join(chroma_dir, d)) and d not in ['.', '..']]

print(f"\n目录中的UUID文件夹数量: {len(all_dirs)}")
print("\n所有UUID文件夹:")
for d in all_dirs:
    status = "ACTIVE" if d in active_uuids else "ORPHAN"
    dir_path = os.path.join(chroma_dir, d)
    size = sum(os.path.getsize(os.path.join(dir_path, f))
               for f in os.listdir(dir_path)
               if os.path.isfile(os.path.join(dir_path, f)))
    print(f"  [{status}] {d} (size: {size/1024:.1f}KB)")
