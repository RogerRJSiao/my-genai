# 📈 英文財報分析 RAG 專案：本地開發與配置指南

本專案旨在建立一個以檢索增強生成（RAG, Retrieval-Augmented Generation）為核心的 AI 系統，能精準讀取並分析英文財務報告，並以流暢的繁體中文回答相關問題。

## 🛠️ 1. 系統硬體與模型配置 (Environment & Models)

- **硬體資源**：Nvidia GPU (12 GB VRAM)
- **模型儲存路徑**：`D:\ollama_models`（透過 Windows 環境變數 `OLLAMA_MODELS` 指定）

| 模型架構類型 | 模型名稱 (Ollama Tag) | 尺寸/量化版本 | 說明/用途 |
| --- | --- | --- | --- |
| LLM (文字生成) | `cwchang/llama-3-taiwan-8b-instruct:q4_k_m` | ~4.9 GB (4-bit) | 具備台灣在地化語言能力的 LLM，負責將檢索到的財報內容彙總並用繁體中文回答。 |
| Embedding (向量化) | `bge-m3:latest` | ~1.2 GB (567M) | 強大的跨語言語意模型（生成 1024 維度向量），負責將英文財報段落與中文提問進行語意對齊。 |

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

## 📌 當前進度摘要

- [x] Step 1：Ollama 安裝、模型下載（llama-3-taiwan & bge-m3）與 D 槽路徑修正。
- [x] Step 2：Anaconda 獨立環境建置與 `test_ollama.py` 測試腳本準備。
- [ ] Step 3：（下一階段）讀取英文 PDF 財報、切塊（Chunking）並存入向量資料庫。
- [ ] Step 4：（下一階段）檢索鏈路串接（RAG Chain），實現英文檢索與繁體中文回答。
