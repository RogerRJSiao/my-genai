"""讀取 data/processed/chunks/**/*.json，寫入 ChromaDB 對應 collection，
並回填 data/manifest.json 的 ingestion_status/chunk_count/ingested_at。

用法：
    python scripts/ingest_data.py                                   # 處理全部尚未 ingest 的 chunk 檔案
    python scripts/ingest_data.py "data/processed/chunks/.../x.json" # 只處理指定檔案（略過 ingestion_status 檢查）
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.database.chroma_client import get_client, upsert_chunks  # noqa: E402

MANIFEST_PATH = ROOT / "data" / "manifest.json"


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def save_manifest(manifest):
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def find_manifest_entry(manifest, chunks_path):
    rel_path = Path(chunks_path).resolve().relative_to(ROOT).as_posix()
    for entry in manifest["documents"]:
        if entry["chunks_path"] == rel_path:
            return entry
    raise ValueError(f"manifest.json 找不到對應項目: {rel_path}")


def ingest_file(client, entry):
    chunks_path = ROOT / entry["chunks_path"]
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    count = upsert_chunks(client, entry["collection"], chunks)
    entry["ingestion_status"] = "ingested"
    entry["chunk_count"] = count
    entry["ingested_at"] = datetime.now(timezone.utc).isoformat()
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "chunks_path",
        nargs="*",
        help="要寫入的 chunk 檔案路徑；留空則處理 manifest 中 ingestion_status 不是 ingested 的全部項目",
    )
    args = parser.parse_args()

    manifest = load_manifest()
    client = get_client()

    if args.chunks_path:
        entries = [find_manifest_entry(manifest, p) for p in args.chunks_path]
    else:
        entries = [e for e in manifest["documents"] if e["ingestion_status"] != "ingested"]

    if not entries:
        print("沒有待處理的項目（全部 ingestion_status 已是 ingested）。")
        return

    for entry in entries:
        chunks_path = ROOT / entry["chunks_path"]
        if not chunks_path.exists():
            print(f"{entry['chunks_path']} -> 尚未產生 chunk 檔案，略過")
            continue
        count = ingest_file(client, entry)
        print(f"{entry['chunks_path']} -> {count} chunks ingested")

    save_manifest(manifest)


if __name__ == "__main__":
    main()
