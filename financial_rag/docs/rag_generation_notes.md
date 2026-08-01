# RAG 生成鏈路：已知限制與除錯記錄

記錄 2026-08-01 除錯過程中發現的問題、根因與修正，供之後遇到類似「明明檢索到資料，LLM 卻回答查無資料/答錯」的情況時參考排查方向。

## 問題 1：US 10-K 權益變動表欄位對不齊（結構性 bug）

**症狀**：問「美光 FY2025 年報股東權益表的資本公積與保留盈餘」，LLM 回答查無資料，即使檢索確實抓到對的 chunk。

**根因**：`src/parser/annual_report_parser_us10k.py` 的 `_table_html_to_rows()` 原本逐格 `get_text()`，沒有展開 HTML 表格的 `colspan`/`rowspan`。美光 10-K 的權益變動表用多層表頭（如「Common Stock」用 `colspan="6"` 橫跨兩個子欄「Number of Shares」/「Amount」，其餘欄位用 `colspan="3" rowspan="2"` 跨兩列表頭只寫一次），展開前表頭列的儲存格數（6、2）跟資料列（8）對不上，餵給 LLM 的 markdown table 因此欄位錯位，模型判斷「表格沒有這個欄位」。

**修正**：
- `_table_html_to_rows()` 改成先依 `colspan`/`rowspan` 展開成完整網格（rowspan 沿用的儲存格會複製到每一列該欄位置），再用 `_collapse_spanned_cells()` 把「展開出來的重複副本」收斂回一格——用「是否為同一個原始 `<td>`」（`id(cell)`）判斷要不要收斂，而非文字是否相同，避免誤把兩個剛好數值相同的不同欄位（如兩期都是 `$0`）誤併成一欄。
- `_clean_row_cells()` 改成永遠保留第 0 欄（列標籤欄），不參與「丟棄空字串」的判斷：表頭列在這一欄本來就是空的，資料列則是實際列名，兩者性質不同不能套同一條規則。
- `_trim_equity_statement_rows()` 原本寫死取 `rows[:2]`（假設固定兩層表頭），但美光 FY2021/FY2022 的財報多了「Noncontrolling Interests in Subsidiaries」一層分組，變成三層表頭，寫死索引會抓到還沒展開完的中間層。改用結構規律：每一層表頭的第 0 欄都是空字串，只有資料列的第 0 欄是實際列名——往下找到最後一個「第 0 欄是空字串」的列，就是最深、欄數跟資料列對得上的完整表頭，不受表頭層數多寡影響。

**驗證**：重新產生並 ingest 全部 5 個財年（FY2021-FY2025）的 US_MU 10-K chunk，確認每年的表頭/資料欄數都一致，FY2025 的資本公積/保留盈餘數字（$13,339M / $48,583M）跟人工核對的 golden set 完全吻合。

## 問題 2：表格單位說明被丟棄

**症狀**：修正問題 1 後，LLM 能正確讀出數字，但回答漏掉單位（如把 $13,339M 說成「13,339」，容易被誤解成 13,339 美元）。

**根因**：10-K 原始 HTML 裡，「(In millions, except per share amounts)」這句單位說明跟報表標題不同 `<span>`（字級/字重不同），出現在標題與 `<table>` 之間，但 parser 原本只抓 `<table>` 本身，這句話整個被丟掉。表格儲存格本身只有裸數字（`$13,339`），沒有任何單位標示。

**修正**：新增 `_extract_units_caption()`，用正則 `\(in\s+millions[^)]*\)` 從標題與表格之間的文字抓出單位說明，`build_us10k_chunks()` 把它併進表格的 markdown caption（如 `**Consolidated Statements of Changes in Equity (In millions, except per share amounts)**`）。同時在 `src/rag/generator.py` 的 `SYSTEM_PROMPT` 加一條明講規則，作為雙重保險：即使某份文件的單位說明因為格式差異沒抓到，也提醒模型別漏掉表格裡看到的其他單位線索。

## 問題 3：LLM 生成結果不穩定（同一 context 有時答對有時答錯）

**症狀**：同樣的檢索結果、同樣的問題，重跑三次，答案有時正確、有時說查無資料。

**根因**：`ollama.chat()` 原本沒有指定 `temperature`，用模型預設取樣參數，量化 8B 模型讀表格數字做跨欄比較時本來就不穩，加上隨機取樣會放大這個不穩定性。

**修正**：`generate_answer()` 呼叫 `ollama.chat()` 時加 `options={"temperature": 0}`（greedy decoding）。這**不會**提升模型讀表格的正確率上限，只是讓同樣輸入不再隨機翻車——如果 greedy decoding 下仍然穩定答錯，代表是模型能力問題，需要換更大的模型或改善 prompt/context 呈現方式，而非取樣參數能解決。

## 問題 4：中文財會術語對不到英文表格欄名

**症狀**：問題用中文財會慣用語（「資本公積」「保留盈餘」），表格欄位是英文（"Additional Capital"、"Retained Earnings"），即使數字位置完全正確，模型也常判斷「查無資料」。實測换成英文問法（"Additional Capital 與 Retained Earnings"）就能答對，證實卡點在中英對應這一步，不是檢索或資料本身的問題。

**修正**：`src/rag/glossary_matcher.py`（LLM 抓詞 + 對 `glossary` collection 語意檢索找官方中英對照）原本只在生成**之後**執行，純供人工核對用（見 `scripts/test_rag_chain.py` 的「專業術語比對」區塊），沒有真正餵進 LLM 看到的 context。現在改成生成**之前**先跑，比對結果透過 `generate_answer(question, context, glossary_matches=...)` 的新參數餵進 prompt（`_format_glossary_hint()` 組成的提示區塊），明講「僅供參考、不是文件內容」避免模型誤把提示當成財報數字來源。

⚠️ **已知殘留雜訊**：glossary 語意檢索不保證每次都命中最精確的詞條（如 "Additional Capital" 有時比對到「額外對價」而非「資本公積」，因為 IFRS 詞彙表可能沒有逐字對應的詞條），比對結果本質上是「提示」而非保證正確的翻譯，不能完全取代模型自己的判斷。

## 問題 5：括號負數與加總運算

**現況**：財報表格慣例用括號 `( )` 表示負數（如 `$( 7,852 )` 代表負 7,852），**已驗證**模型在現有 `SYSTEM_PROMPT`（含明講括號代表負數的規則）下能正確判斷正負號並在回答中明確講出「是負數」。

⚠️ **已知限制**：要求模型「把這些數字加總或比較」時，實測會拒答或給出不合理的推論（如「這些數字已經是負值了所以無法再做加總」）。這是量化 8B 模型多步驟數學運算能力本身的限制，不是「有沒有注意到負號」的問題，加 SYSTEM_PROMPT 規則對此幫助有限——如果之後有明確的加總/比較類問題需求，可能需要考慮把運算邏輯移出 LLM（如程式先算好再餵給 LLM 覆述），而非單靠 prompt engineering。

## 相關檔案

- `src/parser/annual_report_parser_us10k.py`：colspan/rowspan 展開、單位說明擷取、表頭列數自適應
- `src/rag/generator.py`：`SYSTEM_PROMPT`、`temperature=0`、glossary hint 注入
- `src/rag/glossary_matcher.py`：中英術語比對（現在生成前後都會呼叫，見 `scripts/test_rag_chain.py`）
- `scripts/test_rag_chain.py`：golden set 驗證腳本，比對順序已調整為「先比對術語→餵給生成→印出結果」
