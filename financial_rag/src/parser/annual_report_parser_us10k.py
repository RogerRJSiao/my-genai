"""從美股 10-K HTML 年報中，只抽取四大財務報表（不匯入其餘 MD&A/風險因素等敘述內容）。

美光的 10-K 來源是 investors.micron.com 的頁面，內嵌完整的 inline-XBRL filing
（`<ix:nonFraction>`／`<ix:nonNumeric>` 標籤包住每個數字/日期），檔案前段還有
一大段網站版型（nav/CSS/腳本），不是乾淨的 SEC EDGAR 原始檔。四大報表
（資產負債表／損益表／現金流量表／權益變動表）在文件中各自對應「目錄超連結」
與「報表本體標題」兩處以上重複出現同樣的標題文字，只有報表本體標題後面
緊接著（約數百字元內）就是 `<table>`，用這個位置關係篩掉目錄連結與其他
段落裡提及報表名稱的雜訊。

用法：
    python -m src.parser.annual_report_parser_us10k <html 路徑> -o <輸出 JSON 路徑>
"""
import argparse
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from src.parser.chunker import DOC_LEVEL_FIELDS, to_scalar_metadata  # noqa: E402
from src.parser.page_filter import table_rows_to_markdown  # noqa: E402

# 报表標題文字在不同財年/發行人間措辭可能略有差異（例如美光用「Changes in Equity」
# 而非「Stockholders' Equity」），用彈性一點的 pattern 涵蓋常見寫法。
STATEMENTS = [
    {
        "key": "balance_sheet",
        "pattern": re.compile(r"Consolidated Balance Sheets?"),
        "label_en": "Consolidated Balance Sheets",
        "label_zh": "資產負債表",
    },
    {
        "key": "income_statement",
        "pattern": re.compile(r"Consolidated Statements? of Operations"),
        "label_en": "Consolidated Statements of Operations",
        "label_zh": "綜合損益表",
    },
    {
        "key": "cash_flow_statement",
        "pattern": re.compile(r"Consolidated Statements? of Cash Flows"),
        "label_en": "Consolidated Statements of Cash Flows",
        "label_zh": "現金流量表",
    },
    {
        "key": "equity_statement",
        "pattern": re.compile(
            r"Consolidated Statements? of (?:Changes in Equity|Stockholders.{0,3}Equity)"
        ),
        "label_en": "Consolidated Statements of Changes in Equity",
        "label_zh": "權益變動表",
    },
]

# 報表本體標題後面緊接著就是 <table>；目錄超連結／後段敘述文字提及報表名稱時，
# 附近數千字元內通常不會有 <table>，用這個窗口大小區分「真正的報表標題」。
_TABLE_PROXIMITY_WINDOW = 1000


def _find_real_heading_offset(html_text, pattern):
    """回傳報表本體標題在 html_text 裡的位置；找不到（含只找到目錄連結）回傳 None。"""
    for match in pattern.finditer(html_text):
        window = html_text[match.end() : match.end() + _TABLE_PROXIMITY_WINDOW]
        if "<table" in window:
            return match.start()
    return None


def _extract_balanced_table_html(html_text, from_offset):
    """從 from_offset 開始找第一個 <table>，並配對 <table>/</table> 巢狀層數，
    回傳整個表格（含巢狀子表格）的 HTML 子字串。"""
    start = html_text.index("<table", from_offset)
    depth = 0
    pos = start
    tag_re = re.compile(r"<table\b|</table>", re.IGNORECASE)
    for m in tag_re.finditer(html_text, start):
        if m.group(0).lower().startswith("<table"):
            depth += 1
        else:
            depth -= 1
        pos = m.end()
        if depth == 0:
            break
    return html_text[start:pos]


def _table_html_to_rows(table_html):
    """把表格 HTML 轉成 list of list 字串（供 table_rows_to_markdown 使用）。

    只取最外層 <table> 的列，忽略巢狀子表格自己的列（避免重複計入子表格內容）。
    """
    soup = BeautifulSoup(table_html, "html.parser")
    outer_table = soup.find("table")
    if outer_table is None:
        return []

    rows = []
    for tr in outer_table.find_all("tr"):
        # 略過屬於巢狀子表格的 <tr>（其最近的 table 祖先不是 outer_table）。
        if tr.find_parent("table") is not outer_table:
            continue
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        rows.append(cells)
    return rows


def parse_us10k_statements(html_path):
    """回傳 list of {"key", "label_en", "label_zh", "rows"}，找不到的報表不會出現在結果裡。"""
    html_text = Path(html_path).read_text(encoding="utf-8", errors="ignore")

    statements = []
    for spec in STATEMENTS:
        offset = _find_real_heading_offset(html_text, spec["pattern"])
        if offset is None:
            continue
        table_html = _extract_balanced_table_html(html_text, offset)
        rows = _table_html_to_rows(table_html)
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            continue
        statements.append(
            {
                "key": spec["key"],
                "label_en": spec["label_en"],
                "label_zh": spec["label_zh"],
                "rows": rows,
            }
        )
    return statements


def build_us10k_chunks(doc_id, statements, manifest_entry):
    """把每個報表轉成一個 chunk（一報表一 chunk），格式相容 ingest_data.py。

    manifest_entry 是 data/manifest.json 裡該文件的紀錄，比照 chunker.py 把
    DOC_LEVEL_FIELDS（ticker/company_name/fiscal_year 等）展平進 metadata，
    否則 retriever.py 的 retrieve() 用 where={"ticker": ...} 過濾時會查不到
    這批 chunk（詞彙表沒有 ticker 概念不需要這步，但年報財報有）。
    """
    base_metadata = {
        field: to_scalar_metadata(manifest_entry.get(field)) for field in DOC_LEVEL_FIELDS
    }

    chunks = []
    for stmt in statements:
        document = table_rows_to_markdown(stmt["rows"], caption=stmt["label_en"])
        if not document:
            continue
        chunks.append(
            {
                "id": f"{doc_id}_{stmt['key']}",
                "document": document,
                "metadata": {
                    **base_metadata,
                    "source_id": doc_id,
                    "statement": stmt["key"],
                    "statement_label_en": stmt["label_en"],
                    "statement_label_zh": stmt["label_zh"],
                },
            }
        )
    return chunks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("html_path", help="US_MU_10K_*.html 路徑")
    parser.add_argument("-o", "--output", required=True, help="輸出報表 JSON 路徑")
    args = parser.parse_args()

    statements = parse_us10k_statements(args.html_path)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(statements, ensure_ascii=False, indent=2), encoding="utf-8")
    found = [s["key"] for s in statements]
    print(f"Parsed {len(statements)}/4 statements ({found}) -> {output_path}")


if __name__ == "__main__":
    main()
