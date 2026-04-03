import sqlite3

# 连接到chroma.sqlite3
conn = sqlite3.connect('backend/data/chroma_db/chroma.sqlite3')
cursor = conn.cursor()

# 查询所有表
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
print("Tables in chroma.sqlite3:")
for table in tables:
    print(f"  - {table[0]}")

# 查询collections表
print("\n\nCollections:")
cursor.execute("SELECT id, name FROM collections;")
collections = cursor.fetchall()
for col in collections:
    print(f"  {col[0]} -> {col[1]}")

# 查询segments表（存储UUID映射）
print("\n\nSegments (UUID mappings):")
cursor.execute("SELECT id, collection FROM segments;")
segments = cursor.fetchall()
for seg in segments:
    print(f"  UUID: {seg[0]}")
    print(f"  Collection ID: {seg[1]}")
    print()

conn.close()
