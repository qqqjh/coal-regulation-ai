from app.services.vector_store import vector_store_service

# 测试获取所有数据
print("=== 测试向量数据库 ===")

# 尝试获取所有文档
try:
    results = vector_store_service.vector_store.get()
    print(f"\n总共有 {len(results.get('ids', []))} 个分块")

    if results.get('metadatas'):
        print("\n前5个分块的元数据:")
        for i, metadata in enumerate(results['metadatas'][:5]):
            print(f"{i+1}. {metadata}")

    # 测试按 source 查询
    if results.get('metadatas') and len(results['metadatas']) > 0:
        test_source = results['metadatas'][0].get('source', 'unknown')
        print(f"\n测试查询 source='{test_source}':")

        test_results = vector_store_service.vector_store.get(
            where={"source": test_source},
            limit=3
        )
        print(f"找到 {len(test_results.get('ids', []))} 个匹配的分块")
        if test_results.get('documents'):
            print(f"第一个分块内容: {test_results['documents'][0][:100]}...")

except Exception as e:
    print(f"错误: {e}")
    import traceback
    traceback.print_exc()
