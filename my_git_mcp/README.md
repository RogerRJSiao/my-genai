# GitHub / GitLab 內容讀取工具 (my_git_mcp)


本專案同時提供兩種使用方式，讀取 GitHub / GitLab 內容：
1. 自建 MCP server (`server.py`，提供 MCP Client，在對話中直接讀取網頁/原始檔) 
2. 獨立 Flask 網頁 (`app.py`，貼上 issue/PR/MR 連結，可顯示結構化摘要)

## 比較 server.py 與 app.py 的差異

| 比較項目 | 對話助手模式 (`server.py`) | 網頁查詢模式 (`app.py`) |
|---|---|---|
| 使用時機 | 在與 Claude 對話中，直接讀某個網頁/原始檔內容 | 開啟瀏覽器，查詢某一個的 issue/PR/MR 的結構化摘要 |
| 支援連結 | 任意 github、gitlab 網頁或原始檔 | 僅 GitHub issue/PR、GitLab issue/MR 連結 |
| 資料來源 | 直接抓網頁 HTML，用 BeautifulSoup 轉純文字 | 呼叫官方 API（GitHub REST API / GitLab API v4），取得結構化 JSON |
| 執行方式 | 由 MCP client 啟動成背景進程 (background process) | CMD 手動執行 `python app.py`，並在瀏覽器打開 `http://127.0.0.1:5000` |
| client 綁定 | 需在 `claude_desktop_config.json` 設定 `command`/`args`，由 client 當子進程啟動、透過 stdio 用 JSON-RPC 溝通，對話中呼叫 `fetch_page` | 不需要設定 client，可獨立執行、用瀏覽器打開即可 |
| 通訊協定 | 透過 stdio (標準輸入輸出) 傳輸 JSON-RPC 訊息，不監聽任何 port | 監聽 port (預設 5000)，用 HTTP 存取請求 |
| 部署與測試 | [前往部署與測試-server.py](#deploy-server) | [前往部署與測試-app.py](#deploy-app) |


## 安裝 (server.py / app.py 共用)

建議在專案根目錄建立虛擬環境後，再安裝依賴套件：

```bash
python -m venv my_mcp_web
my_mcp_web\Scripts\activate   # Windows
# source my_mcp_web/bin/activate  # macOS / Linux

pip install -r requirements.txt
```

<a id="deploy-server"></a>
## 🚀 部署與測試-server.py

### MCP Client 要設定 `claude_desktop_config.json`，範例：

```json
{
  "mcpServers": {
    "my-mcp-web": {
      "command": "python",
      "args": ["D:/your-project-dir/my_git_mcp/server.py"]
    }
  }
}
```

### 執行 (用 FastMCP 框架啟動)

```bash
my_mcp_web\Scripts\activate   # Windows，若尚未啟用虛擬環境
# source my_mcp_web/bin/activate  # macOS / Linux

python server.py
```

此為本機手動測試用；正式使用時由 MCP client（Claude Desktop / Claude Code）依上方設定自動啟動成背景進程，不需手動執行。

<a id="deploy-app"></a>
## 🚀 部署與測試-app.py

### 設定環境變數

`app.py` 貼 GitHub issue/PR 或 GitLab issue/MR (含 `work_items`) 連結即可顯示摘要，執行前需要設定：

```bash
set GITHUB_TOKEN=ghp_xxxxxxxx                 # Windows cmd，GitHub API 用
set GITLAB_TOKENS={"gitlab.example.com":"xxx","gitlabce.example.com":"yyy"}
```

```powershell
$env:GITHUB_TOKEN = "ghp_xxxxxxxx"             # PowerShell
$env:GITLAB_TOKENS = '{"gitlab.example.com":"xxx","gitlabce.example.com":"yyy"}'
```

- `GITLAB_TOKENS`：JSON 字串，key 為 GitLab 主機名稱、value 為該主機的 Personal Access Token，支援同時串接多個自架 GitLab (公開的 `gitlab.com` 可不設定 token)。

### 執行

```bash
my_mcp_web\Scripts\activate   # Windows，若尚未啟用虛擬環境
# source my_mcp_web/bin/activate  # macOS / Linux

python app.py
```

預設在 http://127.0.0.1:5000 執行。

### 實作畫面

啟動 `app.py` 後的終端機輸出：

![app.py 啟動畫面](images/gitlab_測試00.png)

貼上 GitLab Issue 連結，顯示結構化摘要：

![GitLab Issue 摘要](images/gitlab_測試01.png)

貼上 GitLab Merge Request 連結，顯示結構化摘要：

![GitLab MR 摘要](images/gitlab_測試02.png)

## 資安風險控管

- **環境變數讀取 token**：`GITHUB_TOKEN`、`GITLAB_TOKENS` 一律透過環境變數讀取，不在程式碼或設定檔中寫死，避免 token 隨程式碼外洩或被提交進版控。
- **`.gitignore` 排除虛擬環境與機密檔**：專案根目錄的 `.gitignore` 已排除 `my_mcp_web/`（虛擬環境，含完整第三方套件）以及 `.env`、`.env.*` 等機密設定檔，避免 `git add` 時誤將環境、憑證資料提交進版控。
- **GitLab 主機白名單**：`app.py` 只允許 `gitlab.com` 與 `GITLAB_TOKENS` 中已設定 token 的自架主機，貼上其他未知主機的連結會直接被拒絕，避免被誘導對任意 (含內網) 主機發出請求 (SSRF)。

## 未來可能方向

1. **issue 彙整分析**：目前 `app.py` 只負責抓取並顯示結構化資料，之後可考慮加入一個彙整步驟，把抓到的 issue/PR/MR 資料整理成摘要或趨勢報告。
2. **啟用本機 LLM**：若有本機 Ollama，可把抓到的 GitLab/GitHub 內容組成 prompt，透過 HTTP 呼叫本機 API（如 `http://localhost:11434/api/chat`）做彙整分析，資料不需送到外部服務，適合處理較敏感的內部資料；缺點是彙整品質通常不如 Claude 等雲端模型，需自行評估取捨。
3. **其他 MCP client 串接**：`server.py` 是標準 MCP server (透過 stdio + JSON-RPC)，理論上 Codex CLI、Cursor 等支援 MCP 的 client 都能註冊使用，只是各自設定檔格式不同。
