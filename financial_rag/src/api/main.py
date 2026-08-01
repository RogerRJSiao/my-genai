"""把 RAG 鏈路（retriever + generator）封裝成 HTTP 服務。

原本只能透過 scripts/test_rag_chain.py 逐題手動跑；這支是同一套檢索/生成邏輯
（src/rag/）的服務化入口，供之後的前端或其他系統呼叫，行為與 test_rag_chain.py
的流程一致：解析公司代碼 -> 檢索 -> 組 glossary 提示 -> 生成回答 -> 附上引用來源。

用法：
    conda activate financial_rag
    uvicorn src.api.main:app --reload --port 8000

啟動後可用瀏覽器開 http://127.0.0.1:8000/docs 直接測試（Swagger UI），
或參考 scripts/test_api.py 的自動化測試方式。使用者輸入介面則是
http://127.0.0.1:8000/ 的靜態頁面（static/index.html，純 HTML+JS 呼叫
下面的 /query，不需要額外的前端服務或套件）。
"""
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from src.database.chroma_client import COLLECTIONS, get_client, get_collection  # noqa: E402
from src.rag.generator import generate_answer  # noqa: E402
from src.rag.glossary_matcher import extract_terms, match_terms  # noqa: E402
from src.rag.query_resolver import resolve_tickers  # noqa: E402
from src.rag.retriever import build_context, format_sources, retrieve  # noqa: E402

DEFAULT_COLLECTION = "quarterly_earningcall"

app = FastAPI(title="Financial RAG API")

# ChromaDB client 與 collection 物件在 process 生命週期內只建立一次，比照
# test_rag_chain.py 的快取方式，避免每個請求都重新連線/查詢 collection 中繼資料。
_client = None
_collections = {}


def _get_client():
    global _client
    if _client is None:
        _client = get_client()
    return _client


def _get_collection(name):
    if name not in _collections:
        _collections[name] = get_collection(_get_client(), name)
    return _collections[name]


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, description="繁體中文或英文問題")
    tickers: Optional[list[str]] = Field(
        None, description="指定要查詢的公司代碼；留空則從問題文字自動解析"
    )
    collections: Optional[list[str]] = Field(
        None,
        description=f"要檢索的 collection，可指定多個；留空則預設只查 {DEFAULT_COLLECTION}",
    )


class GlossaryMatch(BaseModel):
    query_term: str
    term_en: Optional[str]
    term_zh: Optional[str]
    distance: float


class QueryResponse(BaseModel):
    question: str
    tickers: list[str]
    answer: str
    sources: str
    glossary_matches: list[GlossaryMatch]


@app.get("/health")
def health():
    """健康檢查：確認 ChromaDB 連線正常、三個 collection 都存在。"""
    client = _get_client()
    try:
        names = [_get_collection(name).name for name in COLLECTIONS]
    except Exception as exc:  # noqa: BLE001 - 健康檢查要回報任何連線失敗原因
        raise HTTPException(status_code=503, detail=f"ChromaDB 連線失敗：{exc}") from exc
    return {"status": "ok", "collections": names}


@app.post("/query", response_model=QueryResponse)
def query(request: QueryRequest):
    """跑一次完整的 RAG 流程：解析公司代碼 -> 檢索 -> glossary 比對 -> 生成回答。

    流程與 scripts/test_rag_chain.py 的迴圈本體一致，差別只在這裡是單次請求
    而非批次跑 golden set。
    """
    collection_names = request.collections or [DEFAULT_COLLECTION]
    unknown = [name for name in collection_names if name not in COLLECTIONS]
    if unknown:
        # get_collection() 對不存在的 collection 名稱會丟 ValueError，讓它
        # 一路變成沒有訊息的通用 500 對呼叫端沒有意義（例如 Swagger UI 的
        # 「Try it out」預設會把 schema example 值 "string" 填進陣列欄位，
        # 送出後才發現是無效輸入）——在這裡先擋掉，回傳明確的 400 訊息。
        raise HTTPException(
            status_code=400,
            detail=f"未知的 collection：{unknown}，應為 {COLLECTIONS} 其中之一",
        )
    case_collections = [_get_collection(name) for name in collection_names]

    tickers = resolve_tickers(request.question, request.tickers)

    hits = retrieve(case_collections, request.question, tickers)
    context = build_context(hits)

    matches = []
    if context.strip():
        glossary_collection = _get_collection("glossary")
        candidate_terms = extract_terms(f"{request.question}\n{context}")
        if candidate_terms:
            matches = match_terms(candidate_terms, glossary_collection)

    answer = generate_answer(request.question, context, glossary_matches=matches)
    sources = format_sources(hits)

    return QueryResponse(
        question=request.question,
        tickers=tickers,
        answer=answer,
        sources=sources,
        glossary_matches=matches,
    )


# 靜態頁面掛在最後：StaticFiles 掛在 "/" 會接住所有沒被前面路由（/health、
# /query、/docs）匹配到的路徑，html=True 讓 "/" 自動回傳 static/index.html。
# 掛載順序必須在其他路由之後，不然會反過來蓋掉 /health、/query。
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
