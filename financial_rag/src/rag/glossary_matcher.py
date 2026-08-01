"""從問答文字裡抓出財務/會計專業術語，再拿去已 ingest 的 glossary collection 比對官方譯名。

跟 glossary_lookup.py（純字典精確比對，適合「已知一個詞要查譯名」）不同，
這裡要解決的是反向問題：一段自由文字（問題／LLM 回答）裡「有哪些詞算專業
術語」本身就需要語言理解，用固定規則字串比對很容易漏掉／誤判，所以先讓 LLM
抓出候選術語，再用現有的 bge-m3 embedding 對 ChromaDB 的 glossary collection
做語意檢索，找出每個候選詞最接近的官方中英對照條目。
"""
import re

import ollama

from src.rag.generator import LLM_MODEL

# LLM 不一定會照指示「一行一個術語」：實測發現它有時把所有詞用逗號擠在同一行，
# 有時在找不到術語時輸出一整句解釋、或在術語清單前加一句「以下是找到的術語：」
# 這類開場白（而非指示要求的「只輸出術語本身」）。這裡用程式層面的規則過濾，
# 而非只信任 prompt 指令：
# - 逗號、頓號、分號都當作額外分隔符，避免整行被當成一個術語
# - 排除太長（不像是術語，像是完整句子）、帶有句尾標點/冒號的片段
# - 排除含有「以下」「找到」「術語」這類開場白/後設敘述關鍵字的片段
_SPLIT_PATTERN = re.compile(r"[,，、;；]")
_MAX_TERM_LENGTH = 30
_SENTENCE_PUNCTUATION = "。？！.?!：:"
_META_MARKERS = ("以下", "找到", "沒有", "根據", "問答內容", "術語")

TERM_EXTRACTION_PROMPT = (
    "你是財務會計術語擷取工具。請從下面的問答內容中，找出所有屬於財務、會計、"
    "IFRS/US GAAP 準則相關的專業術語（中文或英文皆可，可以是單詞或片語）。"
    "只能輸出術語本身，每個術語各佔一行，不要輸出編號、項目符號、說明或其他文字。"
    "如果找不到任何專業術語，什麼都不要輸出。"
)


def extract_terms(text):
    """呼叫 LLM 從 text 中擷取候選專業術語，回傳去重後的字串清單（可能為空）。"""
    response = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": TERM_EXTRACTION_PROMPT},
            {"role": "user", "content": text},
        ],
        options={"temperature": 0},
        keep_alive="10s",
    )
    raw_lines = response["message"]["content"].splitlines()
    terms = []
    seen = set()
    for line in raw_lines:
        for piece in _SPLIT_PATTERN.split(line):
            term = piece.strip(" \t-•*")
            if not term or term in seen:
                continue
            if len(term) > _MAX_TERM_LENGTH:
                continue
            if any(p in term for p in _SENTENCE_PUNCTUATION):
                continue
            if any(marker in term for marker in _META_MARKERS):
                continue
            seen.add(term)
            terms.append(term)
    return terms


# 語意檢索一定會回傳「最接近」的結果，就算候選詞其實不是真正的專業術語、或
# 詞彙表根本沒收錄同類詞彙（例如目前這份 glossary 只有 IFRS 會計用語，不含
# 半導體技術詞彙），也會硬湊出一個 distance 很大、完全不相關的「比對結果」。
# 用門檻值過濾掉這種雜訊；未來若擴充其他專業詞彙表（例如產業技術詞彙），
# 只要用同一套 ingest 流程存進 ChromaDB 的 collection，呼叫方換一個
# glossary_collection 傳進來即可比對，不需要更動這支函式。
MAX_MATCH_DISTANCE = 0.5


def match_terms(terms, glossary_collection, top_k=1, max_distance=MAX_MATCH_DISTANCE):
    """對每個候選術語在 glossary collection 做語意檢索，回傳可信的官方對照條目。

    回傳 list of {"query_term", "term_en", "term_zh", "distance"}，依 terms
    原本的順序排列；distance 超過 max_distance 視為沒有可信對應詞條，不列入
    結果（而不是硬塞一個語意上不相關的最近鄰）。
    """
    matches = []
    for term in terms:
        res = glossary_collection.query(
            query_texts=[term],
            n_results=top_k,
            include=["metadatas", "distances"],
        )
        for meta, dist in zip(res["metadatas"][0], res["distances"][0]):
            if dist > max_distance:
                continue
            matches.append(
                {
                    "query_term": term,
                    "term_en": meta.get("term_en"),
                    "term_zh": meta.get("term_zh"),
                    "distance": dist,
                }
            )
    return matches
