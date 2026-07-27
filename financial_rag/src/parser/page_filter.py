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
# "01 Financial Results"（數字＋可選句點＋空白＋標題）。
_OUTLINE_ITEM_RE = re.compile(r"^\d{2}\.?\s+\S.*$")


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


def make_agenda_detector():
    """章節頁格式：先出現 Agenda 頁列出各章節（如 "01 Financial Results"），
    之後每個章節開始前有一頁只有該標題（如華邦電 TW_2344）。"""
    known_titles = {}

    def detector(lines):
        if not lines:
            return None

        # 嘗試把這頁當作 Agenda／Contents 頁，收集章節標題，該頁本身視為目錄頁丟棄。
        items = [
            m.group(1).strip()
            for line in lines
            if (m := _AGENDA_ITEM_RE.match(line.strip()))
        ]
        if len(items) >= 2:
            for item in items:
                known_titles[item.lower()] = item
            return ("outline_page", None)

        # 章節分隔頁：去除純數字的頁碼行後，剩餘內容（可能跨行，如
        # "Business Recap\n& Outlook"）合併比對已收集到的標題。
        content_lines = [line for line in lines if not line.strip().isdigit()]
        candidate = " ".join(content_lines).strip().lower()
        if candidate in known_titles:
            return ("section_title", known_titles[candidate])
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
            raw_text = page.extract_text() or ""

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
                if is_pure_heading_page(lines):
                    # 整頁內容都被標題吸光（純章節標題／講者介紹頁，無正文），
                    # 比照 divider 模式排除，不當作內容頁保留。
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
            if len(content_text) < MIN_TEXT_LENGTH:
                discarded_pages.append({"page": i, "reason": "too_short"})
                continue

            section = (
                extract_inline_heading(lines) if mode == "inline_heading" else current_section
            )

            valid_pages.append(
                {
                    "page": i,
                    "section": section,
                    "text": content_text,
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
