"""用 pdfplumber 逐頁篩選 PDF 內容，算出丟給 Marker 轉換前的 valid_pages 清單。

規則：
1. 頁面文字包含 "Safe Harbor"：免責聲明頁，丟棄。
2. 章節資訊：不同公司的簡報格式不同，依 (ticker, doc_type) 分派策略（見
   SECTION_STRATEGIES）：
   - "divider" 模式：章節頁本身不保留為內容頁，標題轉為後續頁面的 section
     metadata，直到下一個章節頁出現為止（如南亞科／華邦電的法說會簡報）。
   - "inline_heading" 模式：每一頁保留為內容頁，直接用該頁自己的大標題
     （去除結尾括號文字，如 "Industry Trends (1 of 3)" -> "Industry Trends"）
     當作該頁自己的 section（如美光的法說會簡報，一頁一個獨立標題）。
   找不到對應設定時退回預設的 "01." 樣式 divider 偵測。
3. 頁面文字長度 < MIN_TEXT_LENGTH 且非章節頁：過場頁，丟棄。
4. 頁尾行一律移除，不保留在輸出文字中。頁尾格式依 ticker 分派（見
   FOOTER_PATTERNS），預設為含 "All Rights Reserved." 的行；美光的簡報頁尾是
   單純日期（如 "June 24, 2026"），部分頁面因為舊版模板文字疊加，日期行被
   擷取成亂碼（字元重複，如 "JJuunnee 2244,, 22002266"；或新舊日期字母交錯，
   如 "March 18, 2026" 疊在 "June 24, 2026" 上），一併視為頁尾移除
   （見 strip_footer_lines 的分層判斷邏輯）。

未來規劃：valid_pages 對應的原始頁碼會再丟給 Marker 做高品質 Markdown 轉換，
本模組只負責先算出 valid_pages 清單，尚未串接 Marker。

5. 頁面內容擷取分三層，依序進行（見 extract_page_text_and_tables）：
   a. 先用 find_tables() 找出候選表格，用 _is_plausible_table 篩掉版面誤判：
      bbox 面積佔頁面 90% 以上的裝飾邊框，或欄數/列數過少、儲存格大半是空的
      假表格——這種通常是長條圖的柱子邊框/座標軸格線視覺上剛好對齊成網格，
      被 find_tables() 誤判成表格。真正保留下來的表格會同時找出正上方的標題行
      與正下方的附註行（_find_table_caption_and_footnote，依垂直距離與水平
      範圍重疊判斷），一併併入表格的 Markdown 內容。
   b. 再框出「圖表區域」（find_chart_regions）：以非裝飾性的內容圖片
      （page.images，排除貼齊頁緣、佔滿整頁寬/高的版面裝飾滿版圖/色條）以及
      上一步被排除的假表格 bbox（純向量繪圖的長條圖/折線圖沒有點陣圖片，
      找不到內容圖片可用時，這些假表格 bbox 剛好就是圖表本身的範圍）為核心，
      合併鄰近的矩形/線條（圖表外框、座標軸），得到每個圖表的完整涵蓋範圍。
      這個範圍內的文字（圖表標題、座標軸刻度、圖例百分比等）一律不讀進本模組的
      輸出，保留原始頁面，日後交給 Vision model 整張圖表一起判讀（見 charts 欄位）。
   c. 排除以上兩層範圍（含表格的標題/附註行）後，剩餘的文字物件才組成 raw_text，
      交給後續章節/頁尾偵測。

每個 valid page 的 "charts" 欄位是偵測到的圖表區域清單（無圖表則為空陣列），
目前只記錄位置，尚未讀出圖表實際內容，欄位形狀為：
{"chart_id": str, "bbox": [x0, top, x1, bottom], "status": "pending_vision"}
之後接上 Vision model／Marker 時，可依 bbox 從原始頁面裁切圖片餵給模型，再補上
chart_type／chart_title／summary 等內容。
"""

import argparse
import json
import re
from pathlib import Path

import pdfplumber

ROOT = Path(__file__).resolve().parent.parent.parent
MANIFEST_PATH = ROOT / "data" / "manifest.json"

MIN_TEXT_LENGTH = 20
# PDF 斷行可能把 "Safe Harbor" 拆成兩行（"Safe\nHarbor"），故容許任意空白字元
SAFE_HARBOR_RE = re.compile(r"safe\s+harbor", re.IGNORECASE)

DEFAULT_FOOTER_RE = re.compile(r".*All Rights Reserved\..*")
# 美光簡報頁尾是純日期，如 "June 24, 2026" 或 "June 24, 2026 33"（含頁碼）
DATE_FOOTER_RE = re.compile(r"^[A-Za-z]+ \d{1,2}, \d{4}(\s+\d+)?$")

# ticker -> 頁尾行判斷規則；找不到時退回 DEFAULT_FOOTER_RE。
FOOTER_PATTERNS = {
    "MU": DATE_FOOTER_RE,  # 美光 Micron
}


def get_footer_re(ticker):
    return FOOTER_PATTERNS.get(ticker, DEFAULT_FOOTER_RE)


def _collapse_repeated_chars(s):
    """把連續重複字元收斂成一個，如 'JJuunnee' -> 'June'。"""
    out = []
    for ch in s:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


# 美光頁尾日期偶爾殘留舊版模板文字疊加，導致文字擷取變成新舊日期字母交錯
# （如 "March 18, 2026" 疊在 "June 24, 2026" 上）。這類亂碼無法用固定 pattern
# 比對，改用啟發式判斷：整行字元都落在 "June ... 2026" 與 "March ... 2026"
# 兩者字母集合內、夠短、且含數字，視為殘留頁尾雜訊。
_DATE_GARBLE_ALPHABET = set("junemarch")
_MAX_DATE_GARBLE_LENGTH = 50


def _looks_like_garbled_date_footer(stripped):
    letters = {c.lower() for c in stripped if c.isalpha()}
    return (
        0 < len(stripped) <= _MAX_DATE_GARBLE_LENGTH
        and letters
        and letters <= _DATE_GARBLE_ALPHABET
        and any(c.isdigit() for c in stripped)
    )


def strip_footer_lines(text, footer_re=DEFAULT_FOOTER_RE):
    lines = [line for line in text.splitlines() if line.strip()]
    cleaned = []
    for idx, line in enumerate(lines):
        stripped = line.strip()
        is_last_line = idx == len(lines) - 1

        if footer_re.match(stripped):
            continue
        if footer_re is DATE_FOOTER_RE and footer_re.match(
            _collapse_repeated_chars(stripped)
        ):
            continue
        if (
            footer_re is DATE_FOOTER_RE
            and is_last_line
            and _looks_like_garbled_date_footer(stripped)
        ):
            continue

        cleaned.append(line)

    # 部分文件把頁碼獨立成一行、跟版權頁尾行分開（版權行已被上面規則移除），
    # 殘留在最後一行的純數字頁碼一併去除。
    if cleaned and cleaned[-1].strip().isdigit():
        cleaned.pop()

    return cleaned


# ---------------------------------------------------------------------------
# 章節頁偵測器：不同公司的簡報格式不同，各自回傳一個 detector(lines) -> result
# 的函式。lines 是該頁已去除頁尾行的文字行。result 為 None 代表「不是章節頁」
# （該頁仍照一般規則處理）；否則回傳 (reason, title) tuple：
#   ("section_title", title)：章節分隔頁，丟棄，並把 title 設為後續頁面的
#                              section metadata。
#   ("outline_page", None)：Contents／Agenda 目錄頁，丟棄，但不更動目前的
#                            section（本身不屬於任何章節）。
# ---------------------------------------------------------------------------

_NUMERIC_DOT_RE = re.compile(r"^\d{2}\.$")  # 如 "01."、"02."（Nanya 章節頁）
# 目錄／Agenda 頁常見的條列格式，如 "01. Q2'26 Revenue & Results" 或
# "01 Financial Results"（數字＋可選句點＋空白＋標題）。編號位數不同季度可能
# 不一致（如 "1." 或 "01."），故容許 1～2 位數字。
_OUTLINE_ITEM_RE = re.compile(r"^\d{1,2}\.?\s+\S.*$")


def _count_outline_items(lines):
    return sum(1 for line in lines if _OUTLINE_ITEM_RE.match(line.strip()))


def make_numeric_dot_detector():
    """章節頁格式："01.\nTitle"（如南亞科 TW_2408）；另偵測 Contents 目錄頁。"""

    def detector(lines):
        if lines and _NUMERIC_DOT_RE.match(lines[0].strip()):
            return ("section_title", " ".join(lines[1:]).strip())
        if _count_outline_items(lines) >= 2:
            return ("outline_page", None)
        return None

    return detector


_AGENDA_ITEM_RE = re.compile(r"^\d{2}\s+(.+)$")  # 如 "01 Financial Results"
_AND_WORD_RE = re.compile(r"\band\b")


def _normalize_ampersand(s):
    """章節分隔頁有時把 Agenda 頁的 "&" 拼成 "and"（如 "Excellence & Forward"
    vs "Excellence and Forward"），統一轉成 "&" 再比對。"""
    return _AND_WORD_RE.sub("&", s)


def _match_known_title_prefix(candidate, known_titles):
    """candidate 是否以某個已收集標題開頭（含字界檢查，避免誤判半個字）。

    部分季度的章節分隔頁會在標題後面多加副標題（如 "Excellence & Forward
    - Secure Flash Business Update"），故用前綴比對而非要求完全相同。
    """
    for title_key, title in known_titles.items():
        if not candidate.startswith(title_key):
            continue
        remainder = candidate[len(title_key):]
        if remainder == "" or not remainder[0].isalnum():
            return title
    return None


def make_agenda_detector():
    """章節頁格式：先出現 Agenda 頁列出各章節（如 "01 Financial Results"），
    之後每個章節開始前有一頁只有該標題（如華邦電 TW_2344）。"""
    known_titles = {}

    def detector(lines):
        if not lines:
            return None

        # 嘗試把這頁當作 Agenda／Contents 頁：真正的目錄頁編號必為連續遞增的
        # "01, 02, 03..."，藉此排除圖表數據頁裡剛好也有「兩位數字開頭」的
        # 內容行（如調查分數 "54 Strong sales service"）造成的誤判。
        matches = [
            (m.group(0)[:2], m.group(1).strip())
            for line in lines
            if (m := _AGENDA_ITEM_RE.match(line.strip()))
        ]
        numbers = [num for num, _ in matches]
        expected = [f"{n:02d}" for n in range(1, len(numbers) + 1)]
        if len(matches) >= 2 and numbers == expected:
            for _, item in matches:
                known_titles[_normalize_ampersand(item.lower())] = item
            return ("outline_page", None)

        # 章節分隔頁：去除純數字的頁碼行後，剩餘內容（可能跨行，如
        # "Business Recap\n& Outlook"）合併比對已收集到的標題（允許標題後方
        # 多接副標題，並容忍 "&"/"and" 拼法不一致）。
        content_lines = [line for line in lines if not line.strip().isdigit()]
        candidate = _normalize_ampersand(" ".join(content_lines).strip().lower())
        matched_title = _match_known_title_prefix(candidate, known_titles)
        if matched_title is not None:
            return ("section_title", matched_title)
        return None

    return detector


# 每頁自帶大標題的簡報（如美光），標題結尾常見 "(1 of 3)" 這類頁碼提示，取
# section 時要去掉。
_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")
_BULLET_PREFIX = "•"  # "•"，內文條列符號，出現即代表標題已結束
_MAX_HEADING_LINES = 2  # 標題最多只取前兩行，不取到第三行文字


def _collect_heading_lines(lines):
    """回傳組成標題的原始行清單（尚未去除結尾括號、尚未合併）。

    標題可能跨行（如 "Business Model Transformation and\nStrategic Customer
    Agreements (1 of 3)"），從第一行開始合併：第一行必收，之後每行只在「不是
    條列符號開頭」且「去除結尾括號文字後不含數字」時才視為標題延續（財務數據
    表格頁的第二行通常直接是數字，藉此避免把整個表格內容都當成標題），直到不
    符合條件、遇到條列符號或達到安全行數上限為止。
    """
    if not lines:
        return []

    heading_lines = [lines[0].strip()]
    for line in lines[1:_MAX_HEADING_LINES]:
        stripped = line.strip()
        if stripped.startswith(_BULLET_PREFIX):
            break
        candidate = _TRAILING_PAREN_RE.sub("", stripped).strip()
        if any(c.isdigit() for c in candidate):
            break
        heading_lines.append(stripped)

    return heading_lines


def extract_inline_heading(lines):
    """回傳該頁自己的大標題（去除結尾括號文字），取不到則 None。"""
    heading_lines = _collect_heading_lines(lines)
    heading = _TRAILING_PAREN_RE.sub("", " ".join(heading_lines)).strip()
    return heading or None


def is_pure_heading_page(lines):
    """該頁內容是否整頁都被標題吸光、沒有剩餘正文（純標題／講者介紹頁）。"""
    heading_lines = _collect_heading_lines(lines)
    return bool(heading_lines) and len(heading_lines) == len(lines)


# (ticker, doc_type) -> ("divider", detector_factory) 或 ("inline_heading", None)；
# 找不到對應設定時退回 ("divider", make_numeric_dot_detector)。
SECTION_STRATEGIES = {
    ("2408", "investor-conference"): ("divider", make_numeric_dot_detector),  # 南亞科
    ("2344", "investor-conference"): ("divider", make_agenda_detector),  # 華邦電
    ("MU", "earning-deck"): ("inline_heading", None),  # 美光：每頁自帶大標題
}


def get_section_strategy(ticker, doc_type):
    return SECTION_STRATEGIES.get(
        (ticker, doc_type), ("divider", make_numeric_dot_detector)
    )


# ---------------------------------------------------------------------------
# 表格分離：先框出表格範圍，正文擷取時排除該範圍內的文字物件，避免表格儲存格
# 內容被拆成一行行文字插進正文；表格本身另外轉成 Markdown table。
#
# find_tables() 有兩種常見誤判，都需要排除（見 _is_plausible_table）：
# 1. 投影片版面裝飾用的邊框/分隔線：整頁的邊框被當成僅 1~2 列的「表格」，bbox
#    幾乎等於整頁範圍，一旦排除該範圍會把整頁正文都吃掉。真正的財務表格 bbox
#    通常明顯小於整頁（有標題/邊界留白），故排除 bbox 面積佔比過高的偵測結果。
# 2. 純向量繪圖的長條圖/折線圖：柱子邊框、座標軸格線視覺上剛好對齊成網格，被
#    誤判成表格，但實際只有 1 欄（單一長條的堆疊標籤）、只有 1 列，或大半儲存
#    格是空的——跟真正的財報表格（至少「標題列＋資料列」「項目欄＋數值欄」、
#    儲存格大多有內容）明顯不同，故也排除掉。這類被排除的假表格 bbox 不會就此
#    丟棄，而是轉交給 find_chart_regions 當作圖表核心之一（見該函式說明）。
# ---------------------------------------------------------------------------

_MAX_TABLE_AREA_RATIO = 0.9
_MIN_TABLE_ROWS = 2  # 至少要有標題列＋一列資料，只有 1 列的多半是圖表殘留的座標軸標籤
_MIN_TABLE_COLS = 2  # 至少要有「項目欄＋數值欄」，只有 1 欄的多半是長條圖單一長條的堆疊標籤
_MIN_TABLE_FILL_RATIO = 0.5  # 儲存格非空比例；圖表視覺對齊誤判成的假表格通常大半儲存格是空的


def _is_plausible_table(rows, bbox, page_area):
    """排除兩種常見的 find_tables() 誤判：
    1. 版面裝飾用滿版矩形被當成表格：bbox 面積佔頁面比例過高（既有規則）。
    2. 圖表本身（長條圖的柱子邊框、座標軸格線）視覺上剛好對齊成網格，被誤判成
       表格：真正的財報表格通常至少「標題列＋資料列」「項目欄＋數值欄」都有，
       且儲存格大多有內容；長條圖殘留的假表格常常只有 1 欄（單一長條的堆疊
       百分比標籤）、只有 1 列，或大半儲存格是空的。三個條件任一不滿足就排除。

    rows 是呼叫方已經呼叫過 table.extract() 的結果（而非在此重新呼叫），
    避免每個表格候選都重複解析一次儲存格內容。
    """
    x0, top, x1, bottom = bbox
    area_ratio = ((x1 - x0) * (bottom - top)) / page_area
    if area_ratio > _MAX_TABLE_AREA_RATIO:
        return False

    if len(rows) < _MIN_TABLE_ROWS or not rows or len(rows[0]) < _MIN_TABLE_COLS:
        return False

    total_cells = sum(len(row) for row in rows)
    filled_cells = sum(1 for row in rows for cell in row if cell and cell.strip())
    if total_cells == 0 or filled_cells / total_cells < _MIN_TABLE_FILL_RATIO:
        return False

    return True


def _point_in_bbox(x, y, bbox):
    x0, top, x1, bottom = bbox
    return x0 <= x <= x1 and top <= y <= bottom


def _make_exclude_regions_filter(exclude_bboxes):
    def keep(obj):
        cx = (obj["x0"] + obj["x1"]) / 2
        cy = (obj["top"] + obj["bottom"]) / 2
        return not any(_point_in_bbox(cx, cy, bbox) for bbox in exclude_bboxes)

    return keep


def _format_table_cell(cell):
    return (cell or "").strip().replace("\n", " ").replace("|", "\\|")


def table_rows_to_markdown(rows, caption=None, footnote=None):
    """把 pdfplumber Table.extract() 回傳的列資料轉成 Markdown table 字串，
    caption（表格上方標題）與 footnote（表格下方附註）若有找到，一併併入。"""
    rows = [row for row in rows if row and any(cell for cell in row)]
    if not rows:
        return ""

    header = [_format_table_cell(c) for c in rows[0]]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * len(header)) + " |",
    ]
    for row in rows[1:]:
        lines.append("| " + " | ".join(_format_table_cell(c) for c in row) + " |")

    md = "\n".join(lines)
    if caption:
        md = f"**{caption}**\n\n{md}"
    if footnote:
        md = f"{md}\n\n_{footnote}_"
    return md


# ---------------------------------------------------------------------------
# 表格標題／附註：財報表格常見「表格正上方一行標題＋正下方一行附註」的排版
# （如 "Revenue by technology" 標題 + 表格 + "Percentages of total revenue may
# not total 100% due to rounding." 附註）。用 extract_text_lines() 取得含座標
# 的行資料，抓表格上下緊鄰、水平範圍有重疊、且不屬於其他表格/圖表區域的行。
# ---------------------------------------------------------------------------

_CAPTION_MAX_GAP = 70  # 表格上方標題與表格框的最大垂直距離（pt），投影片標題與表格間常留白
_FOOTNOTE_MAX_GAP = 160  # 表格下方附註與表格框的最大垂直距離（pt）：財報投影片的附註常
# 固定貼在整頁底部、跟頁尾文字之間才隔一小段，不管表格本身多高，故容許較大間距；
# 用 _find_page_footer_line 排除頁尾行本身，避免真的抓到頁碼/版權宣告當成附註。
_CAPTION_X_MARGIN = 20  # 標題/附註允許稍微超出表格左右邊界的容許值（pt）


def _line_bbox(line):
    return (line["x0"], line["top"], line["x1"], line["bottom"])


def _line_overlaps_x_range(line, x0, x1, margin=_CAPTION_X_MARGIN):
    return line["x1"] >= x0 - margin and line["x0"] <= x1 + margin


def _line_in_any_bbox(line, bboxes):
    cx = (line["x0"] + line["x1"]) / 2
    cy = (line["top"] + line["bottom"]) / 2
    return any(_point_in_bbox(cx, cy, bbox) for bbox in bboxes)


_MAX_CAPTION_WIDTH_RATIO = 1.5  # 候選行寬度超過表格自身寬度此倍數，視為跨欄位誤併，不採用


def _find_page_footer_line(lines):
    """整頁最下緣的一行幾乎都是頁尾（頁碼/版權宣告/日期），用來排除在附註候選之外，
    避免 _FOOTNOTE_MAX_GAP 開得較大時，在沒有真附註的頁面誤抓頁尾當附註。"""
    return max(lines, key=lambda l: l["bottom"]) if lines else None


def _is_plausible_caption_width(line, table_width):
    """多欄位並排的複雜版面（如財測 guidance 頁）常把不同欄位的文字併成同一行
    （extract_text_lines 依 y 座標分行、不分欄），導致行寬遠超過任一表格本身寬度。
    這種行拿來當表格標題/附註幾乎必錯，用寬度比例排除。"""
    return (line["x1"] - line["x0"]) <= _MAX_CAPTION_WIDTH_RATIO * table_width


def _find_table_caption_and_footnote(table_bbox, lines, other_region_bboxes, footer_line=None):
    """回傳 (caption_line, footnote_line)，找不到則為 None；other_region_bboxes
    是「除了這個表格本身以外」的表格/圖表區域，避免誤抓成鄰近表格的內容；
    footer_line 是整頁頁尾行（見 _find_page_footer_line），排除在附註候選之外。"""
    x0, top, x1, bottom = table_bbox
    table_width = x1 - x0

    above = [
        line
        for line in lines
        if line["bottom"] <= top
        and top - line["bottom"] <= _CAPTION_MAX_GAP
        and _line_overlaps_x_range(line, x0, x1)
        and not _line_in_any_bbox(line, other_region_bboxes)
        and _is_plausible_caption_width(line, table_width)
    ]
    caption = max(above, key=lambda l: l["bottom"]) if above else None

    below = [
        line
        for line in lines
        if line["top"] >= bottom
        and line["top"] - bottom <= _FOOTNOTE_MAX_GAP
        and _line_overlaps_x_range(line, x0, x1)
        and _is_plausible_caption_width(line, table_width)
        and not _line_in_any_bbox(line, other_region_bboxes)
        and line is not footer_line
    ]
    footnote = min(below, key=lambda l: l["top"]) if below else None

    return caption, footnote


# ---------------------------------------------------------------------------
# 圖表區域偵測：財報簡報裡的圖表大致分兩種畫法，各有不同的偵測核心來源：
# 1. 圖表主體用點陣圖片繪製（每個扇形/長條各是一張小圖），座標軸刻度/圖例文字
#    則是疊在圖片上的真實文字。圖片本身 extract_text() 不會讀到，但疊加的文字
#    會，導致圖表的軸標籤/圖例百分比混進正文、且彼此順序打散無法還原對應關係
#    （見人工測試案例：華邦電法說會簡報裡一頁 2 個圓餅圖＋4 個長條圖，標題/
#    百分比/座標軸刻度全部抓到但順序全亂）。→ 用內容圖片（排除貼齊頁緣、佔滿
#    整頁寬或高的版面裝飾用滿版圖/色條/側邊條）當核心。
# 2. 圖表主體是純向量繪圖（長條=矩形、折線=曲線），完全沒有點陣圖片。這種會被
#    find_tables() 誤判成表格（見 _is_plausible_table 排除的假表格），排除掉的
#    假表格 bbox 剛好就是圖表本身的範圍，直接拿來當核心（見人工測試案例：華邦電
#    「Revenue by Products - Consolidated」整頁堆疊長條圖+折線圖，find_tables()
#    誤判出 11 個假表格，其中最大一個涵蓋了整個圖表區域）。
#
# 兩種核心來源合併後，再合併鄰近的矩形/線條（圖表外框、座標軸），框出圖表的
# 完整涵蓋範圍，之後這個範圍內的文字一律不讀進正文，整塊留給未來的 Vision
# model 判讀。
# ---------------------------------------------------------------------------

_MIN_CONTENT_IMAGE_DIM = 20  # 小於此尺寸視為項目符號／小圖示，不當作圖表核心
_DECORATIVE_EDGE_TOLERANCE = 2  # 判斷圖片是否貼齊頁緣的容許誤差（pt）
_DECORATIVE_SIZE_RATIO = 0.85  # 圖片寬或高達頁面對應邊長此比例以上，視為版面裝飾
_CHART_CLUSTER_GAP = 10  # 內容圖片彼此合併為同一圖表的最大間距（pt）
_CHART_SHAPE_MERGE_GAP = 15  # 併入鄰近矩形/線條（圖表外框、座標軸）的最大間距（pt）


def _touches_page_edge(bbox, page_bbox, tolerance=_DECORATIVE_EDGE_TOLERANCE):
    x0, top, x1, bottom = bbox
    px0, ptop, px1, pbottom = page_bbox
    return (
        x0 <= px0 + tolerance
        or top <= ptop + tolerance
        or x1 >= px1 - tolerance
        or bottom >= pbottom - tolerance
    )


def _is_decorative_image(img, page_bbox):
    """版面裝飾用的滿版背景圖/側邊色條/頁首頁尾色塊：貼齊頁面邊緣、且寬或高
    幾乎佔滿整頁；或小於 _MIN_CONTENT_IMAGE_DIM 的項目符號／小圖示。"""
    if img["width"] < _MIN_CONTENT_IMAGE_DIM or img["height"] < _MIN_CONTENT_IMAGE_DIM:
        return True

    bbox = (img["x0"], img["top"], img["x1"], img["bottom"])
    if not _touches_page_edge(bbox, page_bbox):
        return False

    page_width = page_bbox[2] - page_bbox[0]
    page_height = page_bbox[3] - page_bbox[1]
    return (
        img["width"] >= _DECORATIVE_SIZE_RATIO * page_width
        or img["height"] >= _DECORATIVE_SIZE_RATIO * page_height
    )


def _bbox_union(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _bbox_close(a, b, tolerance):
    ax0, atop, ax1, abottom = a
    bx0, btop, bx1, bbottom = b
    return not (
        ax1 + tolerance < bx0
        or bx1 + tolerance < ax0
        or abottom + tolerance < btop
        or bbottom + tolerance < atop
    )


def _cluster_bboxes(bboxes, tolerance):
    """把彼此重疊或間距在 tolerance 內的 bbox 合併成同一個 bbox。"""
    clusters = list(bboxes)
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                if _bbox_close(clusters[i], clusters[j], tolerance):
                    clusters[i] = _bbox_union(clusters[i], clusters[j])
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break
    return clusters


def find_chart_regions(page, implausible_table_bboxes=()):
    """回傳圖表區域 bbox 清單（可能為空）：以非裝飾性內容圖片、以及 find_tables()
    誤判但被 _is_plausible_table 排除的假表格 bbox 為核心，合併鄰近矩形/線條，
    框出每個圖表的完整範圍。

    純向量繪圖的長條圖/折線圖（長條=矩形、折線=曲線，沒有點陣圖片）常被
    find_tables() 誤判成表格（柱子邊框/座標軸格線視覺上剛好對成網格），
    這類假表格被 _is_plausible_table 排除後，它的 bbox 剛好就是圖表本身的
    範圍，直接拿來當圖表核心，不用另外重新判斷哪些矩形/曲線屬於圖表。"""
    content_image_bboxes = [
        (img["x0"], img["top"], img["x1"], img["bottom"])
        for img in page.images
        if not _is_decorative_image(img, page.bbox)
    ]
    seed_bboxes = content_image_bboxes + list(implausible_table_bboxes)
    if not seed_bboxes:
        return []

    regions = _cluster_bboxes(seed_bboxes, tolerance=_CHART_CLUSTER_GAP)

    # 版面裝飾用的滿版背景矩形（如整頁底色）面積接近整頁，若不排除，合併時會把
    # 整頁都吃進圖表區域（原理同 _is_plausible_table 排除的假表格）。
    page_area = (page.bbox[2] - page.bbox[0]) * (page.bbox[3] - page.bbox[1])
    nearby_shapes = [
        (s["x0"], s["top"], s["x1"], s["bottom"])
        for s in list(page.rects) + list(page.lines)
        if ((s["x1"] - s["x0"]) * (s["bottom"] - s["top"])) / page_area
        <= _MAX_TABLE_AREA_RATIO
    ]
    for shape in nearby_shapes:
        for i, region in enumerate(regions):
            if _bbox_close(region, shape, tolerance=_CHART_SHAPE_MERGE_GAP):
                regions[i] = _bbox_union(region, shape)
                break

    return _remove_contained_bboxes(regions)


def _remove_contained_bboxes(bboxes):
    """併入矩形/線條後，部分內容圖片的 cluster 可能整個落在另一個 cluster
    範圍內（同一張圖表被記成兩個重疊區域），只保留沒有被其他 bbox 完全包住的。"""

    def is_contained(inner, outer):
        ix0, itop, ix1, ibottom = inner
        ox0, otop, ox1, obottom = outer
        return ox0 <= ix0 and otop <= itop and ix1 <= ox1 and ibottom <= obottom

    return [
        b
        for i, b in enumerate(bboxes)
        if not any(i != j and is_contained(b, other) for j, other in enumerate(bboxes))
    ]


def extract_page_text_and_tables(page):
    """回傳 (raw_text, tables_markdown, chart_regions)：raw_text 已排除圖表區域
    與表格區域（含表格上方標題／下方附註）內的文字物件。"""
    page_area = page.bbox[2] * page.bbox[3]
    all_candidates = [(t, t.extract()) for t in page.find_tables()]
    plausible_flags = [_is_plausible_table(rows, t.bbox, page_area) for t, rows in all_candidates]
    plausible = [c for c, ok in zip(all_candidates, plausible_flags) if ok]
    implausible_bboxes = [
        t.bbox for (t, _), ok in zip(all_candidates, plausible_flags) if not ok
    ]
    table_bboxes = [t.bbox for t, _ in plausible]
    chart_regions = find_chart_regions(page, implausible_table_bboxes=implausible_bboxes)

    lines = page.extract_text_lines() if table_bboxes else []
    footer_line = _find_page_footer_line(lines) if table_bboxes else None
    tables_markdown = []
    caption_footnote_bboxes = []
    for i, (table, rows) in enumerate(plausible):
        other_bboxes = table_bboxes[:i] + table_bboxes[i + 1 :] + chart_regions
        caption, footnote = _find_table_caption_and_footnote(
            table.bbox, lines, other_bboxes, footer_line=footer_line
        )
        if caption is not None:
            caption_footnote_bboxes.append(_line_bbox(caption))
        if footnote is not None:
            caption_footnote_bboxes.append(_line_bbox(footnote))

        md = table_rows_to_markdown(
            rows,
            caption=caption["text"] if caption else None,
            footnote=footnote["text"] if footnote else None,
        )
        if md:
            tables_markdown.append(md)

    exclude_bboxes = table_bboxes + chart_regions + caption_footnote_bboxes
    if exclude_bboxes:
        text_page = page.filter(_make_exclude_regions_filter(exclude_bboxes))
        raw_text = text_page.extract_text() or ""
    else:
        raw_text = page.extract_text() or ""

    return raw_text, tables_markdown, chart_regions


def filter_pages(raw_path, ticker=None, doc_type=None):
    mode, detector_factory = get_section_strategy(ticker, doc_type)
    section_detector = detector_factory() if mode == "divider" else None
    footer_re = get_footer_re(ticker)

    valid_pages = []
    discarded_pages = []
    current_section = None
    total_pages = 0

    with pdfplumber.open(raw_path) as pdf:
        total_pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            raw_text, tables_markdown, chart_regions = extract_page_text_and_tables(page)

            if SAFE_HARBOR_RE.search(raw_text):
                discarded_pages.append({"page": i, "reason": "safe_harbor_disclaimer"})
                continue

            lines = strip_footer_lines(raw_text, footer_re=footer_re)

            if mode == "divider":
                if i == 0:
                    # 封面頁（公司名／季度／日期），無分析性內容，一律丟棄。
                    discarded_pages.append({"page": i, "reason": "cover_page"})
                    continue

                result = section_detector(lines)
                if result is not None:
                    reason, title = result
                    entry = {"page": i, "reason": reason}
                    if title is not None:
                        current_section = title
                        entry["section"] = title
                    discarded_pages.append(entry)
                    continue
            elif mode == "inline_heading":
                if i == 0:
                    # 封面頁（如 "Financial results FQ3 2026"），無分析性內容，一律丟棄。
                    discarded_pages.append({"page": i, "reason": "cover_page"})
                    continue
                if i == total_pages - 1:
                    # 最後一頁通常是版權聲明，不留。
                    discarded_pages.append({"page": i, "reason": "copyright_page"})
                    continue
                if is_pure_heading_page(lines) and not tables_markdown and not chart_regions:
                    # 整頁內容都被標題吸光（純章節標題／講者介紹頁，無正文），
                    # 比照 divider 模式排除，不當作內容頁保留。表格/圖表內容已被
                    # 排除在 lines 之外，若該頁還有表格或圖表，代表其實有實質內容，
                    # 只是正文剛好只剩標題，不能當成純標題頁丟棄（否則整頁表格資料
                    # 都會遺失）。
                    current_section = extract_inline_heading(lines)
                    discarded_pages.append(
                        {
                            "page": i,
                            "reason": "section_title",
                            "section": current_section,
                        }
                    )
                    continue

            content_text = "\n".join(lines)
            # 表格與圖表內容已排除在 content_text 之外（見
            # extract_page_text_and_tables），純表格頁/純圖表頁的正文可能極短
            # 甚至為空，過短判斷須把表格與圖表一併算入，否則會被誤判成過場頁丟棄。
            has_content = (
                len(content_text) >= MIN_TEXT_LENGTH or tables_markdown or chart_regions
            )
            if not has_content:
                discarded_pages.append({"page": i, "reason": "too_short"})
                continue

            section = (
                extract_inline_heading(lines) if mode == "inline_heading" else current_section
            )

            charts = [
                {
                    "chart_id": f"p{i}_c{idx}",
                    "bbox": [round(v, 1) for v in bbox],
                    "status": "pending_vision",
                }
                for idx, bbox in enumerate(chart_regions)
            ]

            valid_pages.append(
                {
                    "page": i,
                    "section": section,
                    "text": content_text,
                    "tables": tables_markdown,
                    "charts": charts,  # 目前只記錄位置，尚未讀出圖表實際內容
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
    # 解析命令列參數：只需要 PDF 路徑。
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_path", help="要篩選的 PDF 檔案路徑（data/raw/ 底下）")
    args = parser.parse_args()

    # 查 manifest 取得該文件的 ticker/doc_type，據此篩選頁面。
    entry = find_manifest_entry(args.raw_path)
    result = filter_pages(
        args.raw_path, ticker=entry.get("ticker"), doc_type=entry.get("doc_type")
    )
    output = {"id": entry["id"], "raw_path": entry["raw_path"], **result}

    # 寫入 manifest 指定的 parsed_path。
    output_path = ROOT / entry["parsed_path"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 印出摘要方便快速健檢：總頁數/留下頁數/丟棄頁數是否合理、輸出寫到哪裡。
    print(
        f"total={result['total_pages']} valid={len(result['valid_pages'])} "
        f"discarded={len(result['discarded_pages'])} -> {output_path.relative_to(ROOT)}"
    )


if __name__ == "__main__":
    main()
