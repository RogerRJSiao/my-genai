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
    "如果原始表格中的金額或個數有標明單位（如「百萬」「千股」等），"
    "回答時務必把單位一併加上去，不可只照抄原本數字。"
    "財報表格慣例會用括號「( )」表示負數，如「$( 7,852 )」代表負7,852，"
    "回答時務必判斷正負號並正確表達，不可誤植成正數；"
    "若題目要用這些數字做加總或比較，務必把小括號內的數字當負數計算。"
)


NO_CONTEXT_ANSWER = "查無相關資料，系統中沒有找到與此問題相關的財報段落，無法回答。"


def _format_glossary_hint(glossary_matches):
    """把 glossary_matcher 比對到的中英對照組成一段提示文字，附加在問題後面。

    實測發現：財報表格欄位常是英文（如 "Additional Capital"、"Retained
    Earnings"），問題卻用中文財會慣用語（如「資本公積」「保留盈餘」）——即使
    context 裡數字位置完全正確，模型也常常做不出這一步中英對應，判斷成「查無
    資料」。glossary collection 已經有這些詞的官方中英對照，比對結果原本只在
    生成後印出來供人工核對（見 test_rag_chain.py），這裡改成生成前就餵給
    LLM，讓它不用自己硬猜這個對應關係。比對結果本來就可能有語意相近但不完全
    準確的雜訊（如 "Additional Capital" 有時比對到「額外對價」而非「資本
    公積」），所以明講「僅供參考」，不能讓 LLM 誤把提示當成文件本身的內容。
    """
    if not glossary_matches:
        return ""
    lines = [
        f"- {m['query_term']} = {m['term_en']} / {m['term_zh']}" for m in glossary_matches
    ]
    return (
        "\n\n可能相關的財會術語中英對照（僅供參考，不是文件內容，"
        "不可當作財報數字來源）：\n" + "\n".join(lines)
    )


def generate_answer(question, context, glossary_matches=None):
    # context 為空代表檢索完全沒有結果：與其信任 LLM 會依 prompt 指示誠實說查無
    # 資料（實測發現它常常改用訓練時的記憶硬答，例如編造 SK海力士的舊財報數字），
    # 不如在有把握判斷「絕對沒有依據」的這個情況下，直接攔截不呼叫 LLM。
    if not context.strip():
        return NO_CONTEXT_ANSWER

    glossary_hint = _format_glossary_hint(glossary_matches)
    response = ollama.chat(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"財報段落：\n{context}\n\n問題：{question}{glossary_hint}",
            },
        ],
        keep_alive="10s",
        # 財報數字比對需要跨欄位讀表，實測發現預設取樣溫度會讓同一組 context
        # 有時答對有時答錯（見 2026-08-01 除錯）；設 0 用 greedy decoding 讓
        # 輸出穩定，不會消除模型讀表能力的極限，但至少同樣輸入不會隨機翻車。
        options={"temperature": 0},
    )
    return response["message"]["content"]
