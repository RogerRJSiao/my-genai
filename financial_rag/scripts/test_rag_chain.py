"""RAG 鏈路人工驗證腳本：中文問題 -> bge-m3 檢索英文 chunk -> llama-3-taiwan 生成繁中回答。

用途是拿一組人工準備、已知答案的 golden set 問題，把檢索到的段落與最終回答都印出來，
方便人工核對兩件事：(1) 有沒有檢索到正確段落 (2) LLM 有沒有依原文回答而非幻覺。
尚未做自動化評分，golden set 的 expected 欄位只是提示，需要人眼比對。

用法：
    conda activate financial_rag
    python scripts/test_rag_chain.py
"""
import sys
from pathlib import Path

import ollama

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.database.chroma_client import get_client, get_collection  # noqa: E402

LLM_MODEL = "jcai/llama-3-taiwan-8b-instruct:q4_k_m"
TOP_K = 3

SYSTEM_PROMPT = (
    "你是財經分析專家，只能根據下方提供的財報段落回答問題，"
    "不可使用段落以外的知識。若段落中找不到答案，請明確說明查無資料。"
    "務必使用繁體中文回答，並在回答最後標註引用來源（公司代碼、財季、章節）。"
    "如果要用季度比較時，請務必拿台灣股市的原本季度名(FY2020Q1)與美國股市的下一季度名(FY2020Q2)相互比較。"
    "美國股市用美元，台灣股市用台幣，通通都要先轉換成台幣才能比較。"
)

# 人工準備的測試題，expected 只是提示，需自行對照原始 PDF 核對正確性
# tickers 可放多家公司代碼：每家公司各自檢索 top_k，保證每家都有結果進 context，
# 不會因為某家公司段落語意相似度較高，把另一家的段落全部擠掉。
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
        "question": "美光FY2026Q3的DRAM營收相較上一季成長多少？",
        "tickers": ["MU"],
        "expected": "DRAM $31,328M vs 上季 $18,768M，QoQ +67%（見 US_MU_earning-deck_FY2026Q3 Revenue by technology）",
    },
    {
        "category": "一般問題（多公司比較）",
        "question": "請比較美光FY2026Q2、南亞科FY2026Q1、華邦電FY2026Q1法說會提到的獲利與稅務？",
        "tickers": ["MU", "2408", "2344"],
        "expected": "需分別對照三家公司對應財季的財務段落",
    },
    {
        "category": "財季對齊比較（跨市場季度偏移）",
        "question": "能否拿美光的FY2026Q2比較南亞科FY2026Q1資料？",
        "tickers": ["MU", "2408"],
        "expected": "系統提示應能理解台股/美股財季命名偏移一季的對應關係",
    },
    {
        "category": "時間模糊問題（已知弱點：語意檢索不理解「最近」）",
        "question": "華邦電最近一季法說會提到哪些重點？",
        "tickers": ["2344"],
        "expected": "應對應最新一期 FY2026Q1（2026-05-05）的內容，而非語意相似度最高、但較舊的季度",
    },
    {
        "category": "超出資料時間範圍（不存在的未來財季）",
        "question": "美光FY2027Q1的營收預測是多少？",
        "tickers": ["MU"],
        "expected": "資料庫目前只到 FY2026Q3，應誠實回答查無資料，不可用其他財季數字硬湊或幻覺",
    },
    {
        "category": "資料庫未涵蓋的公司",
        "question": "SK海力士（SK Hynix）最新一季的財報表現如何？",
        "tickers": ["000660"],
        "expected": "資料庫完全沒有這家公司的資料，應回答查無資料，不可誤用其他三家公司的數字回答",
    },
    {
        "category": "超出文件類型範圍（annual_report 尚未 ingest）",
        "question": "華邦電年度財報中會計師查核意見為何？",
        "tickers": ["2344"],
        "collection": "annual_report",
        "expected": "annual_report collection 目前是空的（尚未跑過 page_filter/chunker/ingest），應回答查無資料",
    },
]


# 語意檢索無法理解「最近/最新」等時間詞：這類問題 embedding 距離最近的段落，
# 不一定是日期最新的財季。偵測到這類關鍵字時改用 metadata（event_date）先鎖定
# 該公司最新一期，再於該期範圍內做語意排序，避免回傳語意相似但過時的資料。
_RECENCY_KEYWORDS = ("最近", "最新", "近期", "latest", "recent")


def _is_recency_question(question):
    q = question.lower()
    return any(kw in question or kw in q for kw in _RECENCY_KEYWORDS)


def _latest_event_date(collection, ticker):
    """回傳該 ticker 在此 collection 底下最新的 event_date（無資料或無日期則回傳 None）。

    event_date 存成 "YYYY-MM-DD" ISO 格式字串，可直接用字串比較取最大值。
    """
    res = collection.get(where={"ticker": ticker}, include=["metadatas"])
    dates = [m.get("event_date") for m in res["metadatas"] if m.get("event_date")]
    return max(dates) if dates else None


def retrieve(collection, question, tickers, top_k=TOP_K):
    """每家公司各自查詢 top_k 筆再合併，避免多公司比較時被單一公司的段落洗掉。"""
    recency = _is_recency_question(question)
    hits = []
    for ticker in tickers:
        where = {"ticker": ticker}
        if recency:
            latest = _latest_event_date(collection, ticker)
            if latest:
                where = {"$and": [{"ticker": ticker}, {"event_date": latest}]}
        res = collection.query(
            query_texts=[question],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        hits.extend(zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]))
    return hits


def build_context(hits):
    blocks = []
    for id_, doc, meta, dist in hits:
        header = f"[來源: {meta.get('ticker')} {meta.get('fiscal_period') or meta.get('fiscal_year')} / {meta.get('section')}]"
        blocks.append(f"{header}\n{doc}")
    return "\n\n".join(blocks)


def generate_answer(question, context):
    response = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"財報段落：\n{context}\n\n問題：{question}"},
        ],
        keep_alive="10s",
    )
    return response["message"]["content"]


def main():
    # 依序跑 GOLDEN_SET 每一題的檢索+生成，印出結果供人工核對正確性。
    client = get_client()
    collections = {}

    for i, case in enumerate(GOLDEN_SET, 1):
        collection_name = case.get("collection", "quarterly_earningcall")
        if collection_name not in collections:
            collections[collection_name] = get_collection(client, collection_name)
        collection = collections[collection_name]

        print("=" * 70)
        print(f"[{i}] 類別：{case.get('category', '(未分類)')}")
        print(f"    問題：{case['question']}")
        print(f"    提示（需人工核對）：{case['expected']}")
        print("-" * 70)

        hits = retrieve(collection, case["question"], case["tickers"])
        print(f"檢索結果（collection={collection_name}）：")
        if not hits:
            print("  （無結果）")
        for id_, doc, meta, dist in hits:
            print(f"  - [{meta.get('ticker')}] {id_} | distance={dist:.4f} | section={meta.get('section')}")

        context = build_context(hits)
        answer = generate_answer(case["question"], context)

        print("-" * 70)
        print("LLM 回答：")
        print(answer)
        print()


if __name__ == "__main__":
    main()
