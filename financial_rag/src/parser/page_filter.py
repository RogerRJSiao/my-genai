"""用 pdfplumber 逐頁篩選 PDF 內容，算出丟給 Marker 轉換前的 valid_pages 清單。

規則：
1. 頁面文字長度 < MIN_TEXT_LENGTH：過場頁，丟棄。
2. 頁面文字包含 "Safe Harbor"：免責聲明頁，丟棄。
3. 章節頁（如 "01.\nQ2'26 Revenue & Results"）：不保留為內容頁，但標題轉為
   後續頁面的 section metadata，直到下一個章節頁出現為止。
4. 每頁含 "All Rights Reserved." 的頁尾行一律移除，不保留在輸出文字中。

未來規劃：valid_pages 對應的原始頁碼會再丟給 Marker 做高品質 Markdown 轉換，
本模組只負責先算出 valid_pages 清單，尚未串接 Marker。

每個 valid page 預留 "charts" 欄位（目前恆為空陣列），供之後 Vision model／Marker
讀出圖表資料時填入，欄位形狀約定為：
{"chart_id": str, "chart_type": str, "chart_title": str, "summary": str}
"""

import argparse
import json
import re
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"

MIN_TEXT_LENGTH = 20
FOOTER_RE = re.compile(r".*All Rights Reserved\..*")
SECTION_TITLE_RE = re.compile(r"^\d{2}\.$")  # 如 "01."、"02."
# PDF 斷行可能把 "Safe Harbor" 拆成兩行（"Safe\nHarbor"），故容許任意空白字元
SAFE_HARBOR_RE = re.compile(r"safe\s+harbor", re.IGNORECASE)


def strip_footer_lines(text):
    return [
        line
        for line in text.splitlines()
        if line.strip() and not FOOTER_RE.match(line.strip())
    ]


def is_section_title_page(lines):
    return bool(lines) and bool(SECTION_TITLE_RE.match(lines[0].strip()))


def filter_pages(raw_path):
    valid_pages = []
    discarded_pages = []
    current_section = None
    total_pages = 0

    with pdfplumber.open(raw_path) as pdf:
        total_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            raw_text = page.extract_text() or ""

            if len(raw_text) < MIN_TEXT_LENGTH:
                discarded_pages.append({"page": i, "reason": "too_short"})
                continue

            if SAFE_HARBOR_RE.search(raw_text):
                discarded_pages.append({"page": i, "reason": "safe_harbor_disclaimer"})
                continue

            lines = strip_footer_lines(raw_text)

            if is_section_title_page(lines):
                current_section = " ".join(lines[1:]).strip()
                discarded_pages.append(
                    {"page": i, "reason": "section_title", "section": current_section}
                )
                continue

            valid_pages.append(
                {
                    "page": i,
                    "section": current_section,
                    "text": "\n".join(lines),
                    "charts": [],  # 預留給 Vision model／Marker 之後填入圖表資料
                }
            )

    return {
        "total_pages": total_pages,
        "valid_pages": valid_pages,
        "discarded_pages": discarded_pages,
    }


def find_manifest_entry(raw_path):
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    raw_rel = Path(raw_path).resolve().relative_to(ROOT).as_posix()
    for entry in manifest["documents"]:
        if entry["raw_path"] == raw_rel:
            return entry
    raise ValueError(f"data/manifest.json 找不到對應紀錄: {raw_rel}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_path", help="要篩選的 PDF 檔案路徑（data/raw/ 底下）")
    args = parser.parse_args()

    entry = find_manifest_entry(args.raw_path)
    result = filter_pages(args.raw_path)
    output = {"id": entry["id"], "raw_path": entry["raw_path"], **result}

    output_path = ROOT / entry["parsed_path"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"total={result['total_pages']} valid={len(result['valid_pages'])} "
        f"discarded={len(result['discarded_pages'])} -> {output_path.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
