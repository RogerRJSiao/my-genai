"""FastAPI 服務的自動化驗證腳本：用 TestClient 直接呼叫 app，不需另外啟動 uvicorn。

驗證兩件事：(1) /health 回報 ChromaDB 連線與三個 collection 都正常
(2) /query 對 golden set 其中一題能算出正確答案（跟 test_rag_chain.py 的
golden set 是同一題，方便交叉核對 API 層跟直接呼叫 src/rag/ 的結果一致）。

用法：
    conda activate financial_rag
    python scripts/test_api.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from src.api.main import app  # noqa: E402

client = TestClient(app)


def main():
    print("=== /health ===")
    resp = client.get("/health")
    print(resp.status_code, resp.json())
    assert resp.status_code == 200
    assert set(resp.json()["collections"]) == {"annual_report", "quarterly_earningcall", "glossary"}

    print()
    print("=== /query ===")
    resp = client.post(
        "/query",
        json={
            "question": "美光FY2025年報的股東權益表中，資本公積與保留盈餘分別是多少？",
            "tickers": ["MU"],
            "collections": ["annual_report"],
        },
    )
    print(resp.status_code)
    body = resp.json()
    print("答案：", body["answer"])
    print("來源：", body["sources"])
    assert resp.status_code == 200
    assert "13,339" in body["answer"]
    assert "48,583" in body["answer"]

    print()
    print("全部通過。")


if __name__ == "__main__":
    main()
