"""RAG 鏈路人工驗證腳本：中文問題 -> bge-m3 檢索英文 chunk -> llama-3-taiwan 生成繁中回答。

用途是拿一組人工準備、已知答案的 golden set 問題，把檢索到的段落與最終回答都印出來，
方便人工核對兩件事：(1) 有沒有檢索到正確段落 (2) LLM 有沒有依原文回答而非幻覺。
尚未做自動化評分，golden set 的 expected 欄位只是提示，需要人眼比對。

檢索/生成邏輯已搬到 src/rag/（retriever.py、generator.py、query_resolver.py），
供未來的 API 服務共用；本腳本只保留 golden set 資料與跑測試的迴圈。

用法：
    conda activate financial_rag
    python scripts/test_rag_chain.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.database.chroma_client import get_client, get_collection  # noqa: E402
from src.rag.generator import generate_answer  # noqa: E402
from src.rag.query_resolver import resolve_tickers  # noqa: E402
from src.rag.retriever import build_context, retrieve  # noqa: E402

# 人工準備的測試題，expected 只是提示，需自行對照原始 PDF 核對正確性
# tickers 可放多家公司代碼：每家公司各自檢索 top_k，保證每家都有結果進 context，
# 不會因為某家公司段落語意相似度較高，把另一家的段落全部擠掉。留空則由
# query_resolver 從問題文字自動解析。
# collection 預設 "quarterly_earningcall"，個別題目可指定其他 collection（目前
# annual_report/glossary 都還沒 ingest 任何資料，可用來測試「查無資料」的情境）。
#
# 目前資料庫實際涵蓋範圍（截至 2026-08-01，供設計邊界測試題參考）：
#   MU   FY2025Q1 (2024-12-18) ~ FY2026Q3 (2026-06-24)
#   2408 FY2025Q1 (2025-04-10) ~ FY2026Q2 (2026-07-10)
#   2344 FY2025Q1 (2025-05-07) ~ FY2026Q1 (2026-05-05)
GOLDEN_SET = [
    {
        "category": "一般問題（單一公司細節）",
        "question": "美光FY2026Q3的DRAM營收相較上一季、前一同期成長多少？",
        "tickers": [],
        "expected": "DRAM $31,328M vs 上季 $18,768M，QoQ +67%（見 US_MU_earning-deck_FY2026Q3 Revenue by technology）",
    },
    # {
    #     "category": "一般問題（多公司比較）",
    #     "question": "請比較美光FY2026Q2、南亞科FY2026Q1、華邦電FY2026Q1法說會提到的獲利與稅務？",
    #     "tickers": ["MU", "2408", "2344"],
    #     "expected": "需分別對照三家公司對應財季的財務段落",
    # },
    # {
    #     "category": "財季對齊比較（跨市場季度偏移）",
    #     "question": "能否拿美光的FY2026Q2比較南亞科FY2026Q1資料？",
    #     "tickers": ["MU", "2408"],
    #     "expected": "系統提示應能理解台股/美股財季命名偏移一季的對應關係",
    # },
    # {
    #     "category": "時間模糊問題（已知弱點：語意檢索不理解「最近」）",
    #     "question": "華邦電最近一季法說會提到哪些重點？",
    #     "tickers": ["2344"],
    #     "expected": "應對應最新一期 FY2026Q1（2026-05-05）的內容，而非語意相似度最高、但較舊的季度",
    # },
    # {
    #     "category": "超出資料時間範圍（不存在的未來財季）",
    #     "question": "美光FY2027Q1的營收預測是多少？",
    #     "tickers": ["MU"],
    #     "expected": "資料庫目前只到 FY2026Q3，應誠實回答查無資料，不可用其他財季數字硬湊或幻覺",
    # },
    {
        "category": "資料庫未涵蓋的公司",
        "question": "SK海力士（SK Hynix）最新一季的財報表現如何？",
        "tickers": ["000660"],
        "expected": "資料庫完全沒有這家公司的資料，應回答查無資料，不可誤用其他三家公司的數字回答",
    },
    # {
    #     "category": "超出文件類型範圍（annual_report 尚未 ingest）",
    #     "question": "華邦電年度財報中會計師查核意見為何？",
    #     "tickers": ["2344"],
    #     "collection": "annual_report",
    #     "expected": "annual_report collection 目前是空的（尚未跑過 page_filter/chunker/ingest），應回答查無資料",
    # },
]


def main():
    # 依序跑 GOLDEN_SET 每一題的檢索+生成，印出結果供人工核對正確性。
    client = get_client()
    collections = {}

    for i, case in enumerate(GOLDEN_SET, 1):
        #--檢索模組--#
        # 依題目指定的 collection 取用，並快取已取得的 collection 物件避免重複呼叫。
        collection_name = case.get("collection", "quarterly_earningcall")
        if collection_name not in collections:
            collections[collection_name] = get_collection(client, collection_name)
        collection = collections[collection_name]

        #--1.公司名稱解析（src/rag/query_resolver.py）：
        # golden set 有填 tickers 就直接用，沒填則從問題文字自動解析公司名稱。
        tickers = resolve_tickers(case["question"], case.get("tickers"))

        print("=" * 70)
        print(f"[{i}] 類別：{case.get('category', '(未分類)')}")
        print(f"    問題：{case['question']}")
        print(f"    解析出的 tickers：{tickers}")
        print(f"    提示（需人工核對）：{case['expected']}")
        print("-" * 70)

        #--2.語意檢索與組裝 context（src/rag/retriever.py）：
        # retrieve() 內部依序做「最近/最新」關鍵字偵測、財季字樣偵測、實際語意查詢。
        hits = retrieve(collection, case["question"], tickers)
        print(f"檢索結果（collection={collection_name}）：")
        if not hits:
            print("  （無結果）")
        for id_, doc, meta, dist in hits:
            print(f"  - [{meta.get('ticker')}] {id_} | distance={dist:.4f} | section={meta.get('section')}")

        context = build_context(hits)

        #--生成模組--#（src/rag/generator.py）
        # 把檢索到的段落餵給 LLM，產生繁體中文回答。
        answer = generate_answer(case["question"], context)

        print("-" * 70)
        print("LLM 回答：")
        print(answer)
        print()


if __name__ == "__main__":
    main()
