"""用檢索到的財報段落餵給 Taiwan-Llama-3，生成繁體中文回答。

從 scripts/test_rag_chain.py 原樣搬移，供 API 服務與驗證腳本共用。
"""
import ollama

LLM_MODEL = "jcai/llama-3-taiwan-8b-instruct:q4_k_m"

SYSTEM_PROMPT = (
    "你是財經分析專家，只能根據下方提供的財報段落回答問題，"
    "不可使用段落以外的知識。若段落中找不到答案，請明確說明查無資料。"
    "務必使用繁體中文回答。引用來源會由系統另外附加在回答後面，"
    "你不需要自己在回答中標註公司代碼、檔案名稱或頁數。"
    "如果要用季度比較時，請務必拿台灣股市的原本季度名(FY2020Q1)與美國股市的下一季度名(FY2020Q2)相互比較。"
    "美國股市用美元，台灣股市用台幣，需要換算成同一幣別才能比較；"
    "但若段落中沒有提供匯率，絕對不可自行編造或引用記憶中的匯率數字換算，"
    "應直接列出雙方原始幣別金額，並明確說明「查無匯率資料，無法換算成同一幣別比較」。"
)


NO_CONTEXT_ANSWER = "查無相關資料，系統中沒有找到與此問題相關的財報段落，無法回答。"


def generate_answer(question, context):
    # context 為空代表檢索完全沒有結果：與其信任 LLM 會依 prompt 指示誠實說查無
    # 資料（實測發現它常常改用訓練時的記憶硬答，例如編造 SK海力士的舊財報數字），
    # 不如在有把握判斷「絕對沒有依據」的這個情況下，直接攔截不呼叫 LLM。
    if not context.strip():
        return NO_CONTEXT_ANSWER

    response = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"財報段落：\n{context}\n\n問題：{question}"},
        ],
        keep_alive="10s",
    )
    return response["message"]["content"]
