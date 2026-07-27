"""把 page_filter.py 產生的 data/processed/parsed/ JSON 轉成 ChromaDB / LangChain 可直接吃的 chunk 格式，
輸出到 data/manifest.json 指定的 chunks_path（即 data/processed/chunks/ 下對應路徑）。

輸出為 JSON 陣列，每筆 {"id": ..., "document": ..., "metadata": {...}}：
- id / document / metadata 可直接拆開餵給 chromadb 的 Collection.add(ids=, documents=, metadatas=)
- 也可逐筆 langchain_core.documents.Document(page_content=item["document"], metadata=item["metadata"]) 建立 LangChain Document

metadata 只放純量值，因為 ChromaDB 限制 metadata 值只能是 str/int/float/bool：
- 來源頁面的 "section" 若為 None，改存空字串 ""
- 來源頁面的 "charts"（list）序列化成 JSON 字串存在 "charts_json"，另外用 "has_charts" 記錄是否有內容

目前為 MVP：一個 valid page = 一個 chunk，尚未做進一步的階層感知切塊
（如按段落/表格再細切），後續若頁面內容過長可在此擴充。
"""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"

# data/manifest.json 裡要展平進每個 chunk metadata 的文件層級欄位
DOC_LEVEL_FIELDS = [
    "market",
    "ticker",
    "company_name",
    "company_name_zh",
    "doc_category",
    "doc_type",
    "collection",
    "accounting_standard",
    "fiscal_year",
    "fiscal_period",
    "fiscal_period_end",
    "event_date",
    "source_url",
]


def to_scalar_metadata(value):
    """ChromaDB metadata 只吃 str/int/float/bool，None 一律轉空字串。"""
    return "" if value is None else value


def find_manifest_entry(doc_id):
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in manifest["documents"]:
        if entry["id"] == doc_id:
            return entry
    raise ValueError(f"data/manifest.json 找不到對應紀錄: {doc_id}")


def build_chunks(valid_pages_path):
    data = json.loads(Path(valid_pages_path).read_text(encoding="utf-8"))
    doc_id = data["id"]
    entry = find_manifest_entry(doc_id)

    base_metadata = {
        field: to_scalar_metadata(entry.get(field)) for field in DOC_LEVEL_FIELDS
    }

    chunks = []
    for page in data["valid_pages"]:
        text = page["text"]
        charts = page.get("charts", [])
        metadata = {
            **base_metadata,
            "source_id": doc_id,
            "page": page["page"],
            "section": to_scalar_metadata(page.get("section")),
            "has_charts": bool(charts),
            "charts_json": json.dumps(charts, ensure_ascii=False),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        chunks.append(
            {
                "id": f"{doc_id}_p{page['page']}",
                "document": text,
                "metadata": metadata,
            }
        )
    return chunks, entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "valid_pages_path", help="page_filter.py 產生的 data/processed/parsed/ 底下 JSON 路徑"
    )
    parser.add_argument(
        "-o", "--output", help="輸出路徑，預設用 manifest.json 的 chunks_path"
    )
    args = parser.parse_args()

    chunks, entry = build_chunks(args.valid_pages_path)

    output_path = Path(args.output) if args.output else ROOT / entry["chunks_path"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(chunks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Built {len(chunks)} chunks -> {output_path.resolve().relative_to(ROOT)}")


if __name__ == "__main__":
    main()
