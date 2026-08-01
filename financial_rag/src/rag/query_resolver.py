"""把問題裡出現的公司名稱解析成 ticker，讓呼叫方不用自己先查 ticker 代碼。

retrieve() 要求呼叫方明確傳入 tickers 清單，但真正的查詢服務應該讓使用者
直接打「美光」「南亞科」「華邦電」這類自然語言名稱。本模組從
data/manifest.json 動態收集每個 ticker 的公司全名（company_name/
company_name_zh），並疊加一份手動維護的常用簡稱表（manifest 裡沒有簡稱，
公司數量少，用手動維護即可，不需要接 NER/LLM）。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"

# 手動維護的常用簡稱／英文名，補足 manifest 裡的公司全名不足以匹配日常問句的部分。
# 之後新增公司時，若沒有在這裡補上對應簡稱，仍可靠 manifest 的公司全名/ticker
# 代碼比對到，只是無法辨識簡稱。
_ALIAS_OVERRIDES = {
    "MU": ["美光", "Micron"],
    "2408": ["南亞科", "Nanya"],
    "2344": ["華邦電", "Winbond"],
}


def _load_alias_table():
    """回傳 {別名: ticker}，別名包含 ticker 代碼本身、manifest 公司全名、常用簡稱。"""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    alias_to_ticker = {}
    seen_tickers = set()
    for entry in manifest["documents"]:
        ticker = entry.get("ticker")
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        alias_to_ticker[ticker] = ticker
        for field in ("company_name", "company_name_zh"):
            name = entry.get(field)
            if name:
                alias_to_ticker[name] = ticker
        for alias in _ALIAS_OVERRIDES.get(ticker, []):
            alias_to_ticker[alias] = ticker
    return alias_to_ticker


_ALIAS_TO_TICKER = _load_alias_table()


def resolve_tickers(question, explicit_tickers=None):
    """回傳問題對應的 ticker 清單。

    若呼叫方已明確指定 explicit_tickers 則原樣回傳（不做解析）；否則掃描
    question 文字比對別名表，依別名在問題中第一次出現的位置排序，回傳去重後
    的 ticker 清單。一個都比對不到則回傳空 list。
    """
    if explicit_tickers:
        return list(explicit_tickers)

    matches = []
    for alias, ticker in _ALIAS_TO_TICKER.items():
        idx = question.find(alias)
        if idx != -1:
            matches.append((idx, ticker))
    matches.sort(key=lambda pair: pair[0])

    result = []
    for _, ticker in matches:
        if ticker not in result:
            result.append(ticker)
    return result
