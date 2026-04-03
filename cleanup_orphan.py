import chromadb
import os
import shutil
import sqlite3

# 1. 连接到ChromaDB获取所有活跃的segments
conn = sqlite3.connect('backend/data/chroma_db/chroma.sqlite3')
cursor = conn.cursor()

# 获取所有活跃的segment UUIDs
cursor.execute("SELECT id FROM segments;")
active_segments = set(row[0] for row in cursor.fetchall())
conn.close()

print(f"活跃的segment数量: {len(active_segments)}")
print("活跃的segments:")
for seg in active_segments:
    print(f"  - {seg}")

# 2. 扫描目录中的所有UUID文件夹
chroma_dir = "backend/data/chroma_db"
all_dirs = [d for d in os.listdir(chroma_dir)
            if os.path.isdir(os.path.join(chroma_dir, d)) and d not in ['.', '..']]

print(f"\n目录中的UUID文件夹数量: {len(all_dirs)}")

# 3. 找出孤立的目录
orphan_dirs = []
for d in all_dirs:
    if d not in active_segments:
        dir_path = os.path.join(chroma_dir, d)
        size = sum(os.path.getsize(os.path.join(dir_path, f))
                   for f in os.listdir(dir_path)
                   if os.path.isfile(os.path.join(dir_path, f)))
        orphan_dirs.append((d, size))
        print(f"\n找到孤立目录: {d}")
        print(f"  大小: {size/1024:.1f}KB")

# 4. 删除孤立的目录
if orphan_dirs:
    print(f"\n准备删除 {len(orphan_dirs)} 个孤立目录...")
    for d, size in orphan_dirs:
        dir_path = os.path.join(chroma_dir, d)
        try:
            shutil.rmtree(dir_path)
            print(f"[OK] Deleted: {d} ({size/1024:.1f}KB)")
        except Exception as e:
            print(f"[FAIL] Delete failed {d}: {str(e)}")

    print(f"\n清理完成！释放了 {sum(s for _, s in orphan_dirs)/1024:.1f}KB 空间")
else:
    print("\n没有发现孤立目录")
