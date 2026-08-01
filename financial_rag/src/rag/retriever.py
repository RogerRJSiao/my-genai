"""ChromaDB 檢索邏輯：語意查詢 + metadata 過濾（財季/最新一期），並組出 LLM context。

從 scripts/test_rag_chain.py 原樣搬移，供 API 服務與驗證腳本共用。
"""
import re

TOP_K = 3

# 語意檢索無法理解「最近/最新」等時間詞：這類問題 embedding 距離最近的段落，
# 不一定是日期最新的財季。偵測到這類關鍵字時改用 metadata 先鎖定該公司最新一期，
# 再於該期範圍內做語意排序，避免回傳語意相似但過時的資料。
_RECENCY_KEYWORDS = ("最近", "最新", "近期", "latest", "recent")

# 「最新」要看哪個日期欄位，依 doc_category 而異：quarterly_earningcall 用
# event_date（法說會召開日），annual_report 沒有 event_date，要用
# fiscal_period_end（財報結算日）。依序檢查，metadata 裡哪個欄位有值就用哪個，
# 不用另外傳 doc_category 判斷（曾經寫死只查 event_date，導致 annual_report
# 的「最新」提問完全失效、退回純語意排序抓到舊財年資料）。
_RECENCY_FIELDS = ("event_date", "fiscal_period_end")


def _is_recency_question(question):
    q = question.lower()
    return any(kw in question or kw in q for kw in _RECENCY_KEYWORDS)


def _latest_recency_field(collection, ticker):
    """回傳 (欄位名, 最新值) 用來鎖定該 ticker 最新一期；兩個候選欄位都沒有
    資料則回傳 (None, None)。日期都存成 "YYYY-MM-DD" ISO 格式字串，可直接用
    字串比較取最大值。
    """
    res = collection.get(where={"ticker": ticker}, include=["metadatas"])
    metadatas = res["metadatas"]
    for field in _RECENCY_FIELDS:
        values = [m.get(field) for m in metadatas if m.get(field)]
        if values:
            return field, max(values)
    return None, None


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
            field, latest = _latest_recency_field(collection, ticker)
            if latest:
                where = {"$and": [{"ticker": ticker}, {field: latest}]}
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


def _format_file_reference(meta):
    """組出來源檔案的引用片段：有 page 就標頁碼（財報 PDF，一頁一 chunk），
    沒有 page 但有 statement 就標報表名稱（10-K HTML，一報表一 chunk）；
    副檔名一律讀 metadata 的 file_format，不能寫死 .pdf（詞彙表/10-K 來源
    可能是 .md/.html）。"""
    filename = f"{meta.get('source_id')}.{meta.get('file_format') or 'pdf'}"
    if meta.get("page"):
        return f"{filename} 第{meta['page']}頁"
    if meta.get("statement_label_en"):
        return f"{filename} / {meta['statement_label_en']}"
    return filename


def build_context(hits):
    blocks = []
    for id_, doc, meta, dist in hits:
        header = (
            f"[來源: {meta.get('company_name_zh') or meta.get('ticker')}"
            f"（{meta.get('ticker')}） {meta.get('fiscal_period') or meta.get('fiscal_year')} / {meta.get('section') or ''}"
            f" / 檔案: {_format_file_reference(meta)}]"
        )
        blocks.append(f"{header}\n{doc}")
    return "\n\n".join(blocks)


def format_sources(hits):
    """把實際檢索到的段落組成引用來源清單（依 metadata 直接產生，不假手 LLM 覆述）。

    LLM 對「回答最後照抄引用格式」這類附加指令的遵從度不穩定，容易漏引用或
    自行編造頁碼；引用來源本來就是檢索結果的已知資訊，直接在這裡組字串
    比較可靠。呼叫方應把回傳值接在 LLM 回答後面顯示，而非要求 LLM 自己生成。
    """
    seen = set()
    lines = []
    for id_, doc, meta, dist in hits:
        # 用 chunk 自己的 id 去重（保證唯一）：原本用 (source_id, page) 組 key，
        # 但 annual_report 這類「一份文件多個 chunk、沒有 page」的來源
        # （如 10-K 的 4 張報表）共用同一個 source_id 且 page 都是空值，
        # 會被誤判成同一筆而錯誤地把其他報表的引用擠掉。
        if id_ in seen:
            continue
        seen.add(id_)
        lines.append(
            f"- {meta.get('company_name_zh') or meta.get('ticker')}"
            f"（{meta.get('ticker')}） {meta.get('fiscal_period') or meta.get('fiscal_year')} /"
            f" {meta.get('section') or ''} / {_format_file_reference(meta)}"
        )
    if not lines:
        return ""
    return "引用來源：\n" + "\n".join(lines)
