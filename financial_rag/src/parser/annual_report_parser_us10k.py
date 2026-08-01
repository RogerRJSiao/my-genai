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


def _clean_row_cells(cells):
    """把「$」符號跟空白佔位格併回實際數值，讓每列的儲存格數與期間數對得上。

    EDGAR 這類表格排版習慣把金額拆成三個 <td>：「$」、數字本體、空白佔位格
    （用來對齊縮排），逐一保留反而會讓每一列的儲存格數量長短不一，跟表頭的
    期間欄位對不齊——實測發現這會讓 LLM 把「這一欄的金額」誤讀成「下一欄的
    科目」（例如把 Common Stock 的金額誤認成 Additional Capital）。把「$」
    併到緊接著的下一格、丟掉純空白格，才能讓資料格數確實對應期間數。

    第 0 格（列標籤欄，如「Balance as of ...」）一律保留、不參與丟棄空格的
    判斷：多層表頭的表頭列在這一欄本來就是空的（見 _table_html_to_rows），
    若比照後面欄位一律丟棄空字串，會把表頭列的標籤欄整個吃掉，跟資料列的
    標籤欄位對不上，錯位一格。
    """
    if not cells:
        return cells
    cleaned = [cells[0]]
    i = 1
    while i < len(cells):
        cell = cells[i]
        if cell == "$" and i + 1 < len(cells):
            cleaned.append(f"${cells[i + 1]}")
            i += 2
        elif cell == "":
            i += 1
        else:
            cleaned.append(cell)
            i += 1
    return cleaned


def _collapse_spanned_cells(cells):
    """相鄰儲存格若來自同一個原始 <td>（colspan 展開或 rowspan 沿用而重複的
    副本），只留一份；靠「是否為同一個原始 <td>」判斷而非文字是否相同，避免
    誤把兩個剛好數值相同的不同欄位（如兩期剛好都是 "$0"）誤併成一欄。"""
    collapsed = []
    prev_key = object()
    for text, key in cells:
        if key is not None and key == prev_key:
            continue
        collapsed.append(text)
        prev_key = key
    return collapsed


def _table_html_to_rows(table_html):
    """把表格 HTML 轉成 list of list 字串（供 table_rows_to_markdown 使用）。

    只取最外層 <table> 的列，忽略巢狀子表格自己的列（避免重複計入子表格內容）。

    美光 10-K 的權益變動表表頭用 colspan/rowspan 做多層表頭（如「Common Stock」
    橫跨兩個子欄「Number of Shares」/「Amount」，其餘欄位則用 rowspan=2
    跨兩列表頭只寫一次）。原本逐格取 get_text() 不展開 colspan/rowspan，
    會讓表頭列的儲存格數遠少於資料列（實測：表頭兩列分別只有 6、2 格，
    資料列卻有 8 格），欄位對不齊到連 LLM 都判斷「查無資料」。這裡改成先
    依 colspan/rowspan 展開成完整網格（跨列的儲存格複製到每一列該欄位置），
    再用 _collapse_spanned_cells 把「展開出來的重複副本」收斂回一格，
    表頭列展開＋收斂後就能跟資料列的欄數對上。
    """
    soup = BeautifulSoup(table_html, "html.parser")
    outer_table = soup.find("table")
    if outer_table is None:
        return []

    grid_rows = []
    pending = {}  # col_index -> [text, group_key, 剩餘 rowspan 列數]
    for tr in outer_table.find_all("tr"):
        # 略過屬於巢狀子表格的 <tr>（其最近的 table 祖先不是 outer_table）。
        if tr.find_parent("table") is not outer_table:
            continue

        row = {}
        col = 0
        for cell in tr.find_all(["td", "th"]):
            while col in pending:
                text, group_key, remaining = pending[col]
                row[col] = (text, group_key)
                remaining -= 1
                if remaining > 0:
                    pending[col] = [text, group_key, remaining]
                else:
                    del pending[col]
                col += 1
            colspan = int(cell.get("colspan") or 1)
            rowspan = int(cell.get("rowspan") or 1)
            text = cell.get_text(" ", strip=True)
            group_key = id(cell)
            for _ in range(colspan):
                row[col] = (text, group_key)
                if rowspan > 1:
                    pending[col] = [text, group_key, rowspan - 1]
                col += 1
        # tr 自己宣告的儲存格處理完後，欄位往右可能還有上面幾列 rowspan 沿用
        # 下來、但這一列沒有自己宣告新內容的欄位，一併補上。
        while col in pending:
            text, group_key, remaining = pending[col]
            row[col] = (text, group_key)
            remaining -= 1
            if remaining > 0:
                pending[col] = [text, group_key, remaining]
            else:
                del pending[col]
            col += 1

        if row:
            width = max(row) + 1
            grid_rows.append([row.get(i, ("", None)) for i in range(width)])

    ncols = max((len(r) for r in grid_rows), default=0)
    grid_rows = [r + [("", None)] * (ncols - len(r)) for r in grid_rows]

    return [_clean_row_cells(_collapse_spanned_cells(row)) for row in grid_rows]


# 報表標題後面緊接著一句單位說明（如 "(In millions, except per share amounts)"），
# 跟標題本身分屬不同 <span>（字級/字重不同），但表格本體完全不會重複這個資訊——
# 儲存格只有裸數字（如 "$13,339"），沒有任何單位標示。原本只抓 <table> 本身，
# 這句話整個被丟掉，LLM 看不到「這是百萬美元」，實測發現生成回答時會直接照抄
# 裸數字、漏掉單位（如把 $13,339M 說成 13,339，讓人誤以為是 13,339 美元）。
# 用括號比對抓出這句話，不管它在原始 HTML 裡實際用什麼標籤包裝。
_UNITS_CAPTION_RE = re.compile(r"\(in\s+millions[^)]*\)", re.IGNORECASE)


def _extract_units_caption(html_text, heading_offset, table_start):
    between = html_text[heading_offset:table_start]
    text = BeautifulSoup(between, "html.parser").get_text(" ", strip=True)
    match = _UNITS_CAPTION_RE.search(text)
    return match.group(0) if match else None


def parse_us10k_statements(html_path):
    """回傳 list of {"key", "label_en", "label_zh", "units_caption", "rows"}，
    找不到的報表不會出現在結果裡。units_caption 找不到則為 None。"""
    html_text = Path(html_path).read_text(encoding="utf-8", errors="ignore")

    statements = []
    for spec in STATEMENTS:
        offset = _find_real_heading_offset(html_text, spec["pattern"])
        if offset is None:
            continue
        table_html = _extract_balanced_table_html(html_text, offset)
        table_start = html_text.index("<table", offset)
        units_caption = _extract_units_caption(html_text, offset, table_start)
        rows = _table_html_to_rows(table_html)
        rows = [row for row in rows if any(cell for cell in row)]
        if not rows:
            continue
        statements.append(
            {
                "key": spec["key"],
                "label_en": spec["label_en"],
                "label_zh": spec["label_zh"],
                "units_caption": units_caption,
                "rows": rows,
            }
        )
    return statements


# 權益變動表（equity_statement）跟其他三張報表結構不同：每一列是「某個時間點的
# 權益變化」（期初餘額、淨利、股利、庫藏股...一路滾到期末餘額），不是每欄一個
# 期間。完整表格可能有 20-30 列橫跨 3 個財年，餵給 LLM 時容易在一長串列表裡
# 漏抓最後一列（實測驗證過：問「最新年度」的資本公積/保留盈餘，LLM 常常找不到
# 埋在第 26 列的答案）。只保留表頭列＋最後一列（最新一期的期末餘額），把
# LLM 需要在長表格裡定位資訊的負擔去掉。
#
# 其餘三張報表（資產負債表／損益表／現金流量表）是「每列一個科目、每欄一個期間」
# 的結構，理論上也能只留當期欄位，但實測發現同一張表不同列的儲存格數量並不規則
# （部分列多了「$」符號儲存格、部分列尾端多出空白佔位格），單純依欄位數量猜當期
# 資料在哪風險很高，裁錯就會把錯誤數字放進 context，比維持現狀更糟，故先不處理。
_EQUITY_STATEMENT_KEY = "equity_statement"


def _trim_equity_statement_rows(rows):
    """只保留完整表頭列（欄位標籤跟資料列欄數對得上）與最後一列（最新一期的期末餘額）。

    表頭層數不固定：多數年度是兩層（如「Common Stock」再分「Number of
    Shares」/「Amount」兩個子欄，rowspan 沿用的欄位標籤已由 _table_html_to_rows
    展開填進第二層），但併入非控制權益（Noncontrolling Interests in
    Subsidiaries）的年度（如 FY2021 10-K，多了「Micron Shareholders」這層
    分組）會變成三層表頭——原本寫死取 rows[1] 在這種年度會抓到還沒展開完的
    中間層，跟資料列欄數對不上（實測：9 欄表頭 vs 10 欄資料）。改用結構規律：
    每一層表頭的第 0 欄（列標籤欄）都是空字串，只有資料列的第 0 欄是實際列名
    （如「Balance at ...」），往下找到最後一個「第 0 欄是空字串」的列，就是
    最深、欄數跟資料列對得上的完整表頭，不受表頭層數多寡影響。
    """
    if len(rows) <= 3:
        return rows
    header_idx = 0
    for i, row in enumerate(rows):
        if row and row[0] == "":
            header_idx = i
        else:
            break
    return [rows[header_idx], rows[-1]]


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
        rows = stmt["rows"]
        if stmt["key"] == _EQUITY_STATEMENT_KEY:
            rows = _trim_equity_statement_rows(rows)
        caption = stmt["label_en"]
        if stmt.get("units_caption"):
            caption = f"{caption} {stmt['units_caption']}"
        document = table_rows_to_markdown(rows, caption=caption)
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
