import requests
import json

# 测试检索接口
url = "http://localhost:8000/api/rag/retrieve"
data = {
    "query": "煤矿瓦斯检测有哪些要求",
    "kb_id": 3,
    "top_k": 5,
    "similarity_threshold": 0.7
}

print("测试检索接口...")
print(f"请求数据: {json.dumps(data, ensure_ascii=False, indent=2)}")

try:
    response = requests.post(url, json=data)
    print(f"\n状态码: {response.status_code}")
    print(f"响应: {json.dumps(response.json(), ensure_ascii=False, indent=2)}")
except Exception as e:
    print(f"错误: {e}")
    print(f"响应文本: {response.text}")
