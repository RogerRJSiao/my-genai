# 📈 英文財報分析 RAG 專案：本地開發與配置指南

本專案旨在建立一個以檢索增強生成（RAG, Retrieval-Augmented Generation）為核心的 AI 系統，能精準讀取並分析英文財務報告，並以流暢的繁體中文回答相關問題。

## 🛠️ 1. 系統硬體與模型配置 (Environment & Models)

- **硬體資源**：Nvidia GPU (12 GB VRAM)
- **模型儲存路徑**：`D:\ollama_models`（透過 Windows 環境變數 `OLLAMA_MODELS` 指定）

| 模型架構類型 | 模型名稱 (Ollama Tag) | 尺寸/量化版本 | 說明/用途 |
| --- | --- | --- | --- |
| LLM (文字生成) | [`cwchang/llama-3-taiwan-8b-instruct:q4_k_m`](https://ollama.com/jcai/llama-3-taiwan-8b-instruct) | ~4.9 GB (4-bit) | 具備台灣在地化語言能力的 LLM，負責將檢索到的財報內容彙總並用繁體中文回答。 |
| Embedding (向量化) | [`bge-m3:latest`](https://ollama.com/library/bge-m3) | ~1.2 GB (567M) | 強大的跨語言語意模型（生成 1024 維度向量），負責將英文財報段落與中文提問進行語意對齊。 |

> 💡 **VRAM 載入注意事項**：Ollama 預設會在閒置 5 分鐘後自動將模型從 VRAM 釋放（卸載至 0.0 GB），發送新請求時會自動重新載入，屬正常的省電與資源釋放機制。若希望模型永久常駐顯存，可在呼叫時帶入 `keep_alive="-1"`。

## 🐍 2. 開發環境建置 (Anaconda / Environment Setup)

為避免套件版本衝突並利於未來移轉，本專案採用 Anaconda 進行獨立環境管理。

```bash
# 1. 建立指定 Python 3.11 的獨立環境
conda create -n financial_rag python=3.11 -y

# 2. 啟用環境
conda activate financial_rag

# 3. 安裝專案核心套件
pip install ollama
```

## 🧪 3. Python API 基礎測試 (Step 2 驗證腳本)

驗證腳本位於 [tests/test_ollama.py](tests/test_ollama.py)，用於驗證 LLM 生成與 Embedding 向量化功能。

執行方式：

```bash
conda activate financial_rag
python tests/test_ollama.py
```

## 🚀 4. 未來部署與架構規劃 (Deployment Roadmap)

```
[開發階段]  Anaconda (financial_rag) + Windows Ollama (GPU 直通)
      │
      ▼
[部署階段]  Docker Container + Nvidia Container Toolkit (實現開發即部署)
```

- **開發階段（當前）**：使用 Anaconda 虛擬環境開發，專案完成後執行 `pip freeze > requirements.txt` 匯出依賴。
- **部署階段（未來）**：採用 Docker + Docker Compose 架構，將 Python 後端、向量資料庫（如 ChromaDB / Qdrant）與 Ollama 容器化，可快速部署至任何 Linux / 雲端伺服器。

## 📂 5. 資料來源 (Data Sources)

對應 `data/raw/` 目錄結構，整理實際資料的來源連結：

### `data/raw/10K/`（美國股市財報）& `data/raw/TW_AIA` (台灣股市財報)

| 公司 | 說明 | 會計結算日 | 來源連結 |
| --- | --- | --- | --- |
| 美光 (Micron) | SEC 10-K 檔案 (HTML) | 2021-09-02 起算 5 年 | https://investors.micron.com/sec-filings |
| 南亞科 | 公開資訊觀測站 查核報告 (PDF) | 2021-12-31 起算 5 年 | https://mops.twse.com.tw/mops/#/web/t57sb01_q1 <br>(查股票代碼 2408，英文版財報) |
| 華邦電 | 公開資訊觀測站 查核報告 (PDF) | 2021-12-31 起算 5 年 | https://mops.twse.com.tw/mops/#/web/t57sb01_q1 <br>(查股票代碼 2344，英文版財報) |

### `data/raw/investor/`（法說會相關文件）

| 公司 | 說明 | 會計年度 | 來源連結 |
| --- | --- | --- | --- |
| 美光 (Micron) | 法說會相關文件 (PDF) | FY2025Q2 - FY2026Q3  | https://investors.micron.com/events-and-presentations |
| 南亞科 | 法說會相關文件 (PDF) | FY2025Q1 - FY2026Q1 | https://finmoconf.diveinvest.net <br>(查股票代碼 2408，英文簡報) |
| 華邦電 | 法說會相關文件 (PDF) | FY2025Q1 - FY2026Q1 | https://finmoconf.diveinvest.net <br>(查股票代碼 2344，英文簡報) |

### `data/raw/glossary/`（會計用語對照表）

| 文件說明 | 來源連結 |
| --- | --- |
| 財團法人會計研究發展基金會<br>重要會計用語中英對照 | https://www.ardf.org.tw/tifrs2.html |

<details>
<summary>📁 原始資料檔名規範與資料夾結構建議 (Raw Data Naming & Folder Conventions)</summary>

為了讓 `parser/` 與 `ingest_data.py` 之後能直接從路徑/檔名解析出 metadata（公司、文件類型、日期），新增檔案時請依下列規則命名與存放。

### 檔名格式

統一格式：`{market}_{ticker}_{doc_type}_[{FY年}[Q{季}]_]{YYYYMMDD}[_補充說明].{ext}`

| 欄位 | 說明 | 範例 |
| --- | --- | --- |
| `market` | 發行股票市場 | `US`、`TW` |
| `ticker` | 股票代碼（美股用代號，台股用 4 碼數字） | `MU`、`2408`、`2344` |
| `doc_type` | 文件類型（同一份文件若拆成多種形式，直接各自獨立成一個 doc_type，不共用同一個再靠補充說明區分） | `10K`、`AIA`、`investor-conference`、`earning-deck`、`prepared-remarks` |
| `FY年[Q季]` | 選用，標示財年／財季。`annual_report` 類用 `FY{年}`；`quarterly_earningcall` 類用 `FY{年}Q{季}`，且財季歸屬**需人工核對**（法說會日期與其歸屬財季常跨年，如 12 月法說會可能報告的是下一財年 Q1），核對細節見 [docs/manifest_schema.md](docs/manifest_schema.md) | `FY2021`、`FY2026Q3` |
| `YYYYMMDD` | 財報結算日或法說會**實際召開日期**（ISO 格式，不用季度字串） | `20250331` |
| `_補充說明` | 選用，區分同一份文件的不同版本（如修訂版） | `_v2` |

範例：
- `US_MU_10K_FY2021_20210902.html`
- `TW_2408_AIA_FY2021_20211231.pdf`
- `TW_2344_investor-conference_FY2025Q2_20250806.pdf`
- `US_MU_earning-deck_FY2026Q3_20260624.pdf`
- `US_MU_prepared-remarks_FY2026Q3_20260624.pdf`

> ⚠️ 避免使用空白、公司全名、中譯英名（如 `SEC Filing_Micron Technology_...`、`Nanya_...Investor Conference.pdf`）作為檔名，這類命名難以用固定規則解析，且空白檔名在腳本／跨平台處理時容易出錯。

### 資料夾結構建議

在「文件類型」之下，再依「市場前綴 + 公司代碼」分層，避免多家公司檔案混放在同一層：

```
data/raw/
├── annual_report/
│   ├── US_10K/
│   │   └── US_MU/
│   └── TW_AIA/
│       ├── TW_2408/
│       └── TW_2344/
├── quarterly_earningcall/
│   ├── US_earning_call/
│   │   └── US_MU/
│   └── TW_investor_conference/
│       ├── TW_2408/
│       └── TW_2344/
└── glossary/

data/processed/    # 依 pipeline 階段分兩層，各自鏡射 data/raw/ 結構
├── parsed/        # page_filter.py 輸出：過濾過場頁/免責聲明頁後的乾淨文字 + 章節 metadata
│   ├── annual_report/...
│   ├── quarterly_earningcall/...
│   └── glossary/...
└── chunks/        # chunker.py 輸出：合併 manifest metadata 後、可直接餵給 ChromaDB 的 chunk
    ├── annual_report/...
    ├── quarterly_earningcall/...
    └── glossary/...
```

`parsed/` 與 `chunks/` 是兩個獨立的 pipeline 階段產物，各自資料夾結構完全鏡射 `data/raw/`，不與原始檔案混放（詳見 [docs/manifest_schema.md](docs/manifest_schema.md) 的「Pipeline 階段與資料夾」章節）。

`glossary/` 為單一參考文件，不需依公司分層。`data/processed/` 各子資料夾結構與 `data/raw/` 一致，方便 `chunker.py` 輸出對應到同一個檢索模組（collection）。

### 機器可讀的來源清單 (manifest)

本 README 的表格是「給人看」的說明，機器可讀版本為 `data/manifest.json`，由 `scripts/generate_manifest.py` 掃描 `data/raw/` 自動產生，供 `ingest_data.py` 直接讀取寫入向量資料庫的 metadata。欄位規格與待補項目詳見 [docs/manifest_schema.md](docs/manifest_schema.md)。

```bash
python scripts/generate_manifest.py
```

</details>

## 📌 當前進度摘要

- [x] Step 1：Ollama 安裝、模型下載（llama-3-taiwan & bge-m3）與 D 槽路徑修正。
- [x] Step 2：Anaconda 獨立環境建置與 `test_ollama.py` 測試腳本準備。
- [ ] Step 3：（下一階段）讀取英文 PDF 財報、切塊（Chunking）並存入向量資料庫。
- [ ] Step 4：（下一階段）檢索鏈路串接（RAG Chain），實現英文檢索與繁體中文回答。
