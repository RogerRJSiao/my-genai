"""解析中英會計用語對照表 PDF（tifrs_glossary_latest.pdf），輸出逐詞條的 JSON。

這份 PDF 是乾淨的 3 欄表格（Item / Term in English / Term in Chinese），
每頁一個表、沒有圖表、沒有複雜版面，不適合套用 page_filter.py 那套為財報頁面
（圖表＋表格＋敘述文字混雜）設計的三層抽取邏輯——直接用 pdfplumber 逐頁抓表格
即可，每一列輸出一筆詞條，供 chunker 以「一詞條一 chunk」的方式向量化，
避免現行「一頁一 chunk」的作法把每頁約 30 個詞條混在一起、稀釋語意檢索精準度。

用法：
    python -m src.parser.glossary_parser_tifrs <PDF 路徑> -o <輸出 JSON 路徑>
"""
import argparse
import json
from pathlib import Path

import pdfplumber

_HEADER_ROW = ("Item", "Term in English", "Term in Chinese")


def parse_glossary_pdf(pdf_path):
    """回傳詞條 list：[{"item": int, "term_en": str, "term_zh": str}, ...]"""
    terms = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.find_tables():
                for row in table.extract():
                    item, term_en, term_zh = (row + [None, None, None])[:3]
                    if not item or not term_en or not term_zh:
                        continue
                    if tuple(row[:3]) == _HEADER_ROW:
                        continue
                    if not item.strip().isdigit():
                        continue
                    terms.append(
                        {
                            "item": int(item.strip()),
                            # 表格儲存格換行時 pdfplumber 會保留 \n，這裡併回一行避免
                            # 詞條中間斷字（也讓 embedding/精確比對不用另外處理換行）。
                            # 英文用空白接回單字之間的斷行；中文本身字與字之間沒有空白，
                            # 直接接回避免斷行處殘留多餘空格。
                            "term_en": " ".join(term_en.split()),
                            "term_zh": "".join(term_zh.split()),
                        }
                    )
    return terms


def build_glossary_chunks(doc_id, terms):
    """把詞條轉成 chunker.py 輸出格式相容的 chunk list，可直接被 ingest_data.py 讀取。"""
    chunks = []
    for term in terms:
        document = f"English: {term['term_en']}\nChinese: {term['term_zh']}"
        chunks.append(
            {
                "id": f"{doc_id}_item{term['item']}",
                "document": document,
                "metadata": {
                    "source_id": doc_id,
                    "item": term["item"],
                    "term_en": term["term_en"],
                    "term_zh": term["term_zh"],
                    "doc_category": "glossary",
                },
            }
        )
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf_path", help="tifrs_glossary_latest.pdf 路徑")
    parser.add_argument("-o", "--output", required=True, help="輸出詞條 JSON 路徑")
    args = parser.parse_args()

    terms = parse_glossary_pdf(args.pdf_path)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(terms, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Parsed {len(terms)} terms -> {output_path}")


if __name__ == "__main__":
    main()
