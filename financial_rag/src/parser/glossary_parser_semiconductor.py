"""解析半導體術語中英對照表（semiconductor_glossary_latest.md），輸出逐詞條的 JSON。

來源是手寫的 Markdown，用「## 」分節（如「一、基礎概念」），每節底下一個
3 欄表格：英文 / 中文 / 說明。跟 glossary_parser_tifrs.py（處理 tifrs PDF）不同，
這裡不需要 pdfplumber，直接逐行解析 Markdown 表格語法即可。

用法：
    python -m src.parser.glossary_parser_semiconductor <md 路徑> -o <輸出 JSON 路徑>
"""
import argparse
import json
import re
from pathlib import Path

_SECTION_RE = re.compile(r"^##\s+(.+)$")
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")
_HEADER_TERMS = {"英文", "中文", "說明"}


def parse_semiconductor_glossary_md(md_path):
    """回傳詞條 list：
    [{"item": int, "category": str, "term_en": str, "term_zh": str, "description": str}, ...]
    """
    terms = []
    category = None
    item = 0
    for line in Path(md_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()

        section_match = _SECTION_RE.match(line)
        if section_match:
            category = section_match.group(1).strip()
            continue

        row_match = _TABLE_ROW_RE.match(line)
        if not row_match:
            continue
        cells = [c.strip() for c in row_match.group(1).split("|")]
        if len(cells) != 3:
            continue
        term_en, term_zh, description = cells
        if term_en in _HEADER_TERMS or set(term_en) <= {"-", ":"}:
            continue  # 表頭列或分隔列（|---|---|---|）

        item += 1
        terms.append(
            {
                "item": item,
                "category": category,
                "term_en": term_en,
                "term_zh": term_zh,
                "description": description,
            }
        )
    return terms


def build_semiconductor_chunks(doc_id, terms):
    """把詞條轉成 chunker.py 輸出格式相容的 chunk list，可直接被 ingest_data.py 讀取。"""
    chunks = []
    for term in terms:
        document = (
            f"English: {term['term_en']}\nChinese: {term['term_zh']}\n"
            f"Description: {term['description']}"
        )
        chunks.append(
            {
                "id": f"{doc_id}_item{term['item']}",
                "document": document,
                "metadata": {
                    "source_id": doc_id,
                    "item": term["item"],
                    "category": term["category"] or "",
                    "term_en": term["term_en"],
                    "term_zh": term["term_zh"],
                    "description": term["description"],
                    "doc_category": "glossary",
                },
            }
        )
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("md_path", help="semiconductor_glossary_latest.md 路徑")
    parser.add_argument("-o", "--output", required=True, help="輸出詞條 JSON 路徑")
    args = parser.parse_args()

    terms = parse_semiconductor_glossary_md(args.md_path)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Parsed {len(terms)} terms -> {output_path}")


if __name__ == "__main__":
    main()
