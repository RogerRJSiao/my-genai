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
from src.rag.glossary_matcher import extract_terms, match_terms  # noqa: E402
from src.rag.query_resolver import resolve_tickers  # noqa: E402
from src.rag.retriever import build_context, format_sources, retrieve  # noqa: E402

# 人工準備的測試題，expected 只是提示，需自行對照原始 PDF 核對正確性
# tickers 可放多家公司代碼：每家公司各自檢索 top_k，保證每家都有結果進 context，
# 不會因為某家公司段落語意相似度較高，把另一家的段落全部擠掉。留空則由
# query_resolver 從問題文字自動解析。
# collections 預設 ["quarterly_earningcall"]，個別題目可用 "collection"（單一）
# 或 "collections"（list）指定其他 collection；填多個 collection 時每個都各自
# 檢索、結果合併進 context，供需要跨 collection 才能回答的問題使用（例如同時
# 對照法說會展望與年報揭露）。glossary collection 已 ingest IFRS/US GAAP
# 與半導體詞彙表，供 glossary_matcher 比對用，golden set 目前沒有直接對它
# 出題。若要測試「查無資料」的情境，改用下面涵蓋範圍之外的公司/財季/報表
# 組合（如 annual_report 目前只有美光，南亞科/華邦電就還沒 ingest）。
#
# 目前資料庫實際涵蓋範圍（截至 2026-08-01，供設計邊界測試題參考）：
#   quarterly_earningcall：
#     MU   FY2025Q1 (2024-12-18) ~ FY2026Q3 (2026-06-24)
#     2408 FY2025Q1 (2025-04-10) ~ FY2026Q2 (2026-07-10)
#     2344 FY2025Q1 (2025-05-07) ~ FY2026Q1 (2026-05-05)
#   annual_report：
#     MU   FY2021 ~ FY2025（10-K，四大報表：資產負債表/損益表/現金流量表/權益變動表）
#     2408／2344 尚未 ingest，問到這兩家的 annual_report 應誠實回答查無資料
GOLDEN_SET = [
    {
        "category": "一般問題（單一公司細節）",
        "question": "美光FY2026Q3的DRAM營收相較上一季、前一同期成長多少？",
        "tickers": [],
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
        "category": "一般問題（年報股東權益表）",
        "question": "美光FY2025年報的股東權益表中，資本公積與保留盈餘分別是多少？",
        "tickers": ["MU"],
        "collections": ["annual_report"],
        "expected": "資本公積(Additional Capital) $13,339M、保留盈餘(Retained Earnings) $48,583M"
        "（見 US_MU_10K_FY2025 Consolidated Statements of Changes in Equity，Balance as of August 28, 2025）",
    },
    {
        "category": "資料庫未涵蓋的公司（annual_report，僅美光已 ingest）",
        "question": "南亞科與華邦電在最新年度財報的股東權益表中，資本公積與保留盈餘分別是多少？",
        "tickers": ["2408", "2344"],
        "collections": ["annual_report"],
        "expected": "annual_report collection 目前只有美光（MU）5個財年的資料，"
        "南亞科/華邦電尚未 ingest，應誠實回答查無資料，不可誤用美光的數字回答",
    },
    {
        "category": "跨 collection 比較（法說會 vs 年報，目前僅美光兩邊都有資料）",
        "question": "美光FY2026Q3法說會提到的展望，與其最新年報揭露的風險因子是否一致？",
        "tickers": ["MU"],
        "collections": ["quarterly_earningcall", "annual_report"],
        "expected": "context 應同時包含 quarterly_earningcall 與 annual_report 兩邊的段落，"
        "而非只查到其中一個 collection 就作答",
    },
]


def main():
    # 依序跑 GOLDEN_SET 每一題的檢索+生成，印出結果供人工核對正確性。
    client = get_client()
    collections = {}

    for i, case in enumerate(GOLDEN_SET, 1):
        #--檢索模組--#
        # 依題目指定的 collection（可單一可多個）取用，並快取已取得的 collection
        # 物件避免重複呼叫。"collection"（單數字串）與 "collections"（list）
        # 都支援，沒填則預設只查 quarterly_earningcall。
        collection_names = case.get("collections") or [case.get("collection", "quarterly_earningcall")]
        for name in collection_names:
            if name not in collections:
                collections[name] = get_collection(client, name)
        case_collections = [collections[name] for name in collection_names]

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
        # collections 可傳 list，跨 collection 查詢時每筆 hit 各自標註來源 collection。
        hits = retrieve(case_collections, case["question"], tickers)
        print(f"檢索結果（collections={collection_names}）：")
        if not hits:
            print("  （無結果）")
        for id_, doc, meta, dist, hit_collection_name in hits:
            print(
                f"  - [{hit_collection_name}/{meta.get('ticker')}] {id_}"
                f" | distance={dist:.4f} | section={meta.get('section')}"
            )

        context = build_context(hits)

        #--3.專業術語比對（src/rag/glossary_matcher.py）：
        # 從問題與「實際檢索到的財報原文」（不是 LLM 回答，避免 LLM 改寫用詞或
        # 把查無資料的固定回覆句誤判成術語）擷取候選術語，再用已 ingest 的
        # glossary collection 做語意檢索，找出對應的官方中英譯名。
        # context 是空的代表沒有真正的術語來源可以擷取；比對不到可信結果的
        # 候選詞也不用顯示——沒東西可看就不印這個區塊。
        # 提前到生成之前做（原本只在生成後印出來供人工核對），是因為實測發現
        # 財報表格欄位是英文、問題卻用中文財會慣用語時（如「資本公積」對應
        # 表格的 "Additional Capital"），就算數字位置完全正確，LLM 也常常
        # 做不出這一步中英對應而誤判「查無資料」；比對結果現在會一併餵給
        # generate_answer 當提示，不只是印出來而已。
        matches = []
        if context.strip():
            if "glossary" not in collections:
                collections["glossary"] = get_collection(client, "glossary")
            candidate_terms = extract_terms(f"{case['question']}\n{context}")
            if candidate_terms:
                matches = match_terms(candidate_terms, collections["glossary"])

        #--生成模組--#（src/rag/generator.py）
        # 把檢索到的段落與術語比對提示餵給 LLM，產生繁體中文回答。
        answer = generate_answer(case["question"], context, glossary_matches=matches)

        print("-" * 70)
        print("LLM 回答：")
        print(answer)
        # 引用來源由 retriever 依實際檢索結果組出，不假手 LLM 覆述。
        sources = format_sources(hits)
        if sources:
            print()
            print(sources)

        if matches:
            print()
            # glossary collection 目前收錄 IFRS/US GAAP 財務會計詞彙（tifrs_glossary_latest）
            # 與半導體產業技術詞彙（semiconductor_glossary_latest）；仍不含公司內部業務單位
            # 代號（如 CDBU/MCBU 這類 Micron 自訂縮寫），比對結果請以此為前提解讀。
            print("專業術語比對：")
            for m in matches:
                print(
                    f"  - {m['query_term']} -> {m['term_en']} / {m['term_zh']}"
                    f" (distance={m['distance']:.4f})"
                )
        print()


if __name__ == "__main__":
    main()
