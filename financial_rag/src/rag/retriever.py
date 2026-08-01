"""ChromaDB 檢索邏輯：語意查詢 + metadata 過濾（財季/最新一期），並組出 LLM context。

從 scripts/test_rag_chain.py 原樣搬移，供 API 服務與驗證腳本共用。
"""
import re

TOP_K = 3

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


# 問題若明確點名財季（如「FY2026Q2」），檢索時應直接鎖定該財季而非交給語意相似度
# 排序：多公司比較/跨市場財季對齊的問題常常混雜 2-3 個財季字樣，光靠語意排序會抓到
# 語意相近但財季不對的段落（例如問 FY2026Q2 卻抓到 FY2025Q3、FY2026Q1 的段落），
# 讓 LLM 因為 context 財季對不上而保守拒答。
_FISCAL_PERIOD_PATTERN = re.compile(r"FY\d{4}Q[1-4]")


def _mentioned_fiscal_periods(question):
    return _FISCAL_PERIOD_PATTERN.findall(question)


def retrieve(collection, question, tickers, top_k=TOP_K):
    """每家公司各自查詢 top_k 筆再合併，避免多公司比較時被單一公司的段落洗掉。"""
    recency = _is_recency_question(question)
    periods = _mentioned_fiscal_periods(question)
    hits = []
    for ticker in tickers:
        where = {"ticker": ticker}
        if recency:
            latest = _latest_event_date(collection, ticker)
            if latest:
                where = {"$and": [{"ticker": ticker}, {"event_date": latest}]}
        elif periods:
            period_filter = (
                {"fiscal_period": periods[0]}
                if len(periods) == 1
                else {"fiscal_period": {"$in": periods}}
            )
            where = {"$and": [{"ticker": ticker}, period_filter]}
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
        header = (
            f"[來源: {meta.get('company_name_zh') or meta.get('ticker')}"
            f"（{meta.get('ticker')}） {meta.get('fiscal_period') or meta.get('fiscal_year')} / {meta.get('section')}]"
        )
        blocks.append(f"{header}\n{doc}")
    return "\n\n".join(blocks)
