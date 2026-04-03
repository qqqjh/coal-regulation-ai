"""测试特定问题的RAG回复"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import asyncio
from app.services.chat_service import chat_service

async def test_question():
    question = "极薄煤层是不考虑倾角因素下，厚度多少米以下的煤层"

    messages = [
        {"role": "user", "content": question}
    ]

    print(f"问题: {question}\n")
    print("=" * 60)
    print("AI回复:")
    print("-" * 60)

    full_response = ""
    chunk_count = 0
    try:
        async for chunk in chat_service.chat_stream(messages, use_rag=True):
            chunk_count += 1
            print(f"\n[Chunk {chunk_count}]: {repr(chunk)}")
            print(chunk, end='', flush=True)
            full_response += chunk
    except Exception as e:
        print(f"\n\n错误: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 60)
    print(f"\n总共收到 {chunk_count} 个chunk")
    print(f"完整回复长度: {len(full_response)} 字符")
    print(f"回复内容: {repr(full_response)}")

if __name__ == "__main__":
    asyncio.run(test_question())
