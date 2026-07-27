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
GOLDEN_SET = [
    {
        "question": "請比較美光FY2026Q2、南亞科FY2026Q1、華邦電FY2026Q1法說會提到的獲利與稅務？",
        "tickers": ["MU", "2408", "2344"],
        "expected": "資料中心營收超過 250 億美元，年化超過千億美元（見 US_MU_earning-deck_FY2026Q3 Overview 段落）",
    },
        {
        "question": "能否拿美光的FY2026Q2比較南亞科FY2026Q1資料？",
        "tickers": ["MU", "2408"],
        "expected": "",
    },
    # {
    #     "question": "南亞科FY2025Q1的營收狀況如何？",
    #     "tickers": ["2408"],
    #     "expected": "需對照 TW_2408_investor-conference_FY2025Q1 的 Financial Results 段落",
    # },
    # {
    #     "question": "華邦電最近一季法說會提到哪些重點？",
    #     "tickers": ["2344"],
    #     "expected": "需對照最新一期 TW_2344_investor-conference 的內容",
    # },
]


def retrieve(collection, question, tickers, top_k=TOP_K):
    """每家公司各自查詢 top_k 筆再合併，避免多公司比較時被單一公司的段落洗掉。"""
    hits = []
    for ticker in tickers:
        res = collection.query(
            query_texts=[question],
            n_results=top_k,
            where={"ticker": ticker},
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
    collection = get_collection(client, "quarterly_earningcall")

    for i, case in enumerate(GOLDEN_SET, 1):
        print("=" * 70)
        print(f"[{i}] 問題：{case['question']}")
        print(f"    提示（需人工核對）：{case['expected']}")
        print("-" * 70)

        hits = retrieve(collection, case["question"], case["tickers"])
        print("檢索結果：")
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
