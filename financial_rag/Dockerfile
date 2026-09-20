# 定義 api service 的映像檔要如何組裝：基底環境、安裝套件、複製程式碼、啟動指令
# 建置後的映像檔由 docker-compose.yml 的 api service（build: .）啟動變成真正的容器

# 1. 從官方 Python 3.11 精簡版映像檔開始，作為建置的基底環境
FROM python:3.11-slim

# 2. 容器裡切到 /app 目錄，之後的指令都在這裡執行
WORKDIR /app

# 3. 只先複製 requirements.txt，4. 再安裝套件
#    分開兩步是為了讓 build cache 沿用：requirements.txt 沒變時，重新 build 可跳過重裝套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 把原始碼複製進映像檔
#    來源是本機的 financial_rag/src/ 與 financial_rag/scripts/
#    目的地是容器映像檔裡的 /app/src/ 與 /app/scripts/
COPY src/ src/
COPY scripts/ scripts/

# 6. 標記這個容器會用 8000 port
#    此處僅文件記錄性質，不會真正實際建立 port
#    建立是依據 docker-compose.yml 的 ports: 設定
EXPOSE 8000

# 7. 容器啟動時要執行的指令：啟動 FastAPI 服務
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
