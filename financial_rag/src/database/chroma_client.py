"""封裝 ChromaDB PersistentClient 與 collection 存取，供 scripts/ingest_data.py 使用。

Embedding 固定使用 README §1 指定的 Ollama bge-m3（跨語言語意模型，1024 維），
而非 Chroma 預設的英文 all-MiniLM-L6-v2，確保英文財報段落與繁中提問能語意對齊。

DB_PATH 底下的落地檔案：
- chroma.sqlite3：中控 metadata（collections 清單、每個 chunk 的 document/metadata、全文檢索輔助表）
- 以 collection UUID 命名的資料夾：該 collection 的 HNSW 向量索引（hnswlib），
  data_level0.bin 存實際向量，header.bin/length.bin 記錄維度與筆數，link_lists.bin 是上層圖連結。
簡言之：sqlite3 存「是什麼內容」，UUID 資料夾存「向量與相似度索引」。
"""
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import OllamaEmbeddingFunction

ROOT = Path(__file__).resolve().parent.parent.parent
DB_PATH = ROOT / "data" / "chroma_db"

COLLECTIONS = ("annual_report", "quarterly_earningcall", "glossary")
OLLAMA_URL = "http://localhost:11434"
EMBEDDING_MODEL = "bge-m3:latest"


def get_embedding_function():
    return OllamaEmbeddingFunction(url=OLLAMA_URL, model_name=EMBEDDING_MODEL)


def get_client():
    DB_PATH.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(path=str(DB_PATH))


def get_collection(client, name):
    if name not in COLLECTIONS:
        raise ValueError(f"未知的 collection: {name}（應為 {COLLECTIONS} 其中之一）")
    return client.get_or_create_collection(name=name, embedding_function=get_embedding_function())


_UPSERT_BATCH_SIZE = 100


def upsert_chunks(client, collection_name, chunks):
    """chunks 為 chunker.py 輸出的 list，每筆含 id/document/metadata。

    用 upsert 而非 add，同一份文件重新處理後再次寫入時會覆蓋舊 chunk 而非報錯或重複。
    分批寫入（每批 _UPSERT_BATCH_SIZE 筆）：曾經一次把 1949 筆詞條全部丟給 Ollama
    的 /embed 端點，導致其背後的模型 runner 掛掉、後續請求全部連線被拒，分批後
    每次請求量小很多，不會再觸發這個問題。
    """
    collection = get_collection(client, collection_name)
    for i in range(0, len(chunks), _UPSERT_BATCH_SIZE):
        batch = chunks[i : i + _UPSERT_BATCH_SIZE]
        collection.upsert(
            ids=[c["id"] for c in batch],
            documents=[c["document"] for c in batch],
            metadatas=[c["metadata"] for c in batch],
        )
    return len(chunks)
