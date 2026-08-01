"""不透過 embedding 的中英會計用語精確比對，保證回傳的是官方譯名而非近似值。

src/rag/retriever.py 的語意檢索是「找相關」，適合模糊查詢；但翻譯校對這種
情境需要「找到官方確切用詞」，語意最近的向量結果不保證就是正確譯名（bge-m3
可能把語意相近但用詞不同的條目排在前面）。這裡直接讀取
data/processed/parsed/glossary/ 底下每一份詞彙表解析出的詞條清單做字典查詢，
不吃 ChromaDB，適合翻譯校對/術語一致性檢查這類需要 100% 準確度的場景。

新增詞彙表時（例如未來再加一份產業別詞彙表）只要把解析後的 JSON 放進同一個
目錄，這裡會自動一併載入，不需要另外修改程式。
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
GLOSSARY_DIR = ROOT / "data" / "processed" / "parsed" / "glossary"


def _load_terms():
    terms = []
    for path in sorted(GLOSSARY_DIR.glob("*.json")):
        terms.extend(json.loads(path.read_text(encoding="utf-8")))
    return terms


_TERMS = _load_terms()


def lookup_term(term):
    """回傳符合 term 的詞條清單，每筆為
    {"item": int, "term_en": str, "term_zh": str, "match": "exact" | "contains"}。

    比對邏輯：
    1. 先做大小寫不敏感的精確相等比對（英文詞查中文譯名、中文詞查英文譯名皆可，
       依 term 落在 term_en 還是 term_zh 欄位自動判斷方向）。
    2. 若無精確比對結果，退回大小寫不敏感的包含比對（例如查 "goodwill" 也能
       找到 "acquired goodwill" 這種片語條目），並標記為 "contains" 讓呼叫方
       知道這不是保證等級的官方譯名，只是相關候選。
    找不到則回傳空 list。
    """
    needle = term.strip().lower()
    if not needle:
        return []

    exact = []
    contains = []
    for entry in _TERMS:
        en_lower = entry["term_en"].lower()
        zh_lower = entry["term_zh"].lower()
        if needle == en_lower or needle == zh_lower:
            exact.append({**entry, "match": "exact"})
        elif needle in en_lower or needle in zh_lower:
            contains.append({**entry, "match": "contains"})

    return exact if exact else contains
