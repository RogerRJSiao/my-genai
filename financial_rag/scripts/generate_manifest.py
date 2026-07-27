"""掃描 data/raw/ 產生 data/manifest.json，欄位定義見 docs/manifest_schema.md。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
MANIFEST_PATH = ROOT / "data" / "manifest.json"

COMPANY_INFO = {
    "US_MU": {
        "market": "US",
        "ticker": "MU",
        "company_name": "Micron Technology, Inc.",
        "company_name_zh": "美光科技",
        "accounting_standard": "US GAAP",
    },
    "TW_2408": {
        "market": "TW",
        "ticker": "2408",
        "company_name": "Nanya Technology Corporation",
        "company_name_zh": "南亞科技",
        "accounting_standard": "T-IFRS",
    },
    "TW_2344": {
        "market": "TW",
        "ticker": "2344",
        "company_name": "Winbond Electronics Corporation",
        "company_name_zh": "華邦電子",
        "accounting_standard": "T-IFRS",
    },
}

SOURCE_URLS = {
    ("annual_report", "US_MU"): "https://investors.micron.com/sec-filings",
    ("annual_report", "TW_2408"): "https://mops.twse.com.tw/mops/#/web/t57sb01_q1",
    ("annual_report", "TW_2344"): "https://mops.twse.com.tw/mops/#/web/t57sb01_q1",
    ("quarterly_earningcall", "US_MU"): "https://investors.micron.com/events-and-presentations",
    ("quarterly_earningcall", "TW_2408"): "https://finmoconf.diveinvest.net",
    ("quarterly_earningcall", "TW_2344"): "https://finmoconf.diveinvest.net",
}

GLOSSARY_SOURCE_URL = "https://www.ardf.org.tw/tifrs2.html"

DATE_RE = re.compile(r"(\d{8})")
FY_RE = re.compile(r"^FY(\d{4})(Q[1-4])?$")


def parse_glossary(path):
    rel = path.relative_to(RAW_DIR)
    processed_rel = rel.with_suffix(".json")
    return {
        "id": path.stem,
        "raw_path": (Path("data/raw") / rel).as_posix(),
        "parsed_path": (Path("data/processed/parsed") / processed_rel).as_posix(),
        "chunks_path": (Path("data/processed/chunks") / processed_rel).as_posix(),
        "collection": "glossary",
        "market": None,
        "ticker": None,
        "company_name": None,
        "company_name_zh": None,
        "doc_category": "glossary",
        "doc_type": "tifrs-glossary",
        "file_format": path.suffix.lstrip(".").lower(),
        "language": "zh-en",
        "accounting_standard": "T-IFRS",
        "fiscal_year": None,
        "fiscal_period": None,
        "fiscal_period_end": None,
        "event_date": None,
        "source_url": GLOSSARY_SOURCE_URL,
        "retrieved_date": None,
        "ingestion_status": "pending",
        "chunk_count": None,
        "ingested_at": None,
    }


def parse_company_doc(path, doc_category):
    rel = path.relative_to(RAW_DIR)
    processed_rel = rel.with_suffix(".json")

    stem = path.stem
    parts = stem.split("_")
    company_key = f"{parts[0]}_{parts[1]}"
    info = COMPANY_INFO[company_key]

    date_match = DATE_RE.search(stem)
    date_str = date_match.group(1)
    iso_date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"

    # parts[2:] holds doc_type tokens, optionally an FY{year} token, then the date token.
    middle_parts = "_".join(parts[2:]).split(date_str)[0].rstrip("_").split("_")
    fy_match = FY_RE.match(middle_parts[-1]) if middle_parts else None
    if fy_match:
        doc_type = "_".join(middle_parts[:-1])
        fy_year, fy_quarter = fy_match.groups()
        filename_fiscal_year = f"FY{fy_year}"
        filename_fiscal_period = f"FY{fy_year}{fy_quarter}" if fy_quarter else None
    else:
        doc_type = "_".join(middle_parts)
        filename_fiscal_year = None
        filename_fiscal_period = None

    fiscal_year = None
    fiscal_period = None
    fiscal_period_end = None
    event_date = None

    if doc_category == "annual_report":
        fiscal_period_end = iso_date
        fiscal_year = filename_fiscal_year or (
            f"FY{date_str[0:4]}" if info["market"] == "US" else date_str[0:4]
        )
    elif doc_category == "quarterly_earningcall":
        event_date = iso_date  # 檔名日期是法說會實際召開日，非季度結算日
        fiscal_year = filename_fiscal_year
        fiscal_period = filename_fiscal_period

    return {
        "id": stem,
        "raw_path": (Path("data/raw") / rel).as_posix(),
        "parsed_path": (Path("data/processed/parsed") / processed_rel).as_posix(),
        "chunks_path": (Path("data/processed/chunks") / processed_rel).as_posix(),
        "collection": doc_category,
        "market": info["market"],
        "ticker": info["ticker"],
        "company_name": info["company_name"],
        "company_name_zh": info["company_name_zh"],
        "doc_category": doc_category,
        "doc_type": doc_type,
        "file_format": path.suffix.lstrip(".").lower(),
        "language": "en",
        "accounting_standard": info["accounting_standard"],
        "fiscal_year": fiscal_year,
        "fiscal_period": fiscal_period,
        "fiscal_period_end": fiscal_period_end,
        "event_date": event_date,
        "source_url": SOURCE_URLS.get((doc_category, company_key)),
        "retrieved_date": None,
        "ingestion_status": "pending",
        "chunk_count": None,
        "ingested_at": None,
    }


def main():
    documents = []

    for path in sorted((RAW_DIR / "annual_report").rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            documents.append(parse_company_doc(path, "annual_report"))

    for path in sorted((RAW_DIR / "quarterly_earningcall").rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            documents.append(parse_company_doc(path, "quarterly_earningcall"))

    for path in sorted((RAW_DIR / "glossary").rglob("*")):
        if path.is_file() and path.name != ".gitkeep":
            documents.append(parse_glossary(path))

    manifest = {
        "schema_version": "1.0",
        "documents": documents,
    }

    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {len(documents)} records to {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
