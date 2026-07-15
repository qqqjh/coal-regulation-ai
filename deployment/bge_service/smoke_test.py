import json
import time

import requests


BASE_URL = "http://127.0.0.1:8001"
DOCUMENTS = [
    {
        "id": "ventilation",
        "text": "掘进工作面必须采用局部通风机通风。局部通风机停止运转时，应立即停止工作、撤出人员，并切断电源。",
        "metadata": {"source": "煤矿安全规程测试条款", "category": "通风"},
    },
    {
        "id": "gas",
        "text": "矿井必须建立瓦斯检查制度，并按照规定检查作业地点的甲烷浓度。",
        "metadata": {"source": "煤矿安全规程测试条款", "category": "瓦斯"},
    },
    {
        "id": "hoist",
        "text": "提升装置必须装设可靠的制动装置和过卷保护装置。",
        "metadata": {"source": "煤矿安全规程测试条款", "category": "提升"},
    },
    {
        "id": "electrical",
        "text": "严禁在井下使用未经检验合格的非防爆电气设备。",
        "metadata": {"source": "煤矿安全规程测试条款", "category": "机电"},
    },
    {
        "id": "risk",
        "text": "煤矿企业必须建立安全风险分级管控和事故隐患排查治理制度。",
        "metadata": {"source": "煤矿安全规程测试条款", "category": "管理"},
    },
]


def request(method: str, path: str, payload: dict) -> dict:
    started = time.perf_counter()
    response = requests.request(method, f"{BASE_URL}{path}", json=payload, timeout=180)
    response.raise_for_status()
    result = response.json()
    result["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    return result


index_result = request("PUT", "/v1/indexes/smoke-test", {"documents": DOCUMENTS})
search_result = request(
    "POST",
    "/v1/search",
    {
        "index": "smoke-test",
        "query": "掘进巷道的局部通风机停止运转后应当怎么办？",
        "recall_k": 5,
        "top_k": 3,
    },
)
print(json.dumps({"index": index_result, "search": search_result}, ensure_ascii=False, indent=2))

if search_result["results"][0]["document"]["id"] != "ventilation":
    raise SystemExit("smoke test failed: expected ventilation document at rank 1")
