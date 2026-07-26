import ollama

# ----------------------------------------------------
# 1. 測試 LLM 生成 (Text Generation)
# ----------------------------------------------------
print("=== 測試 LLM 生成對話 ===")
llm_model = "jcai/llama-3-taiwan-8b-instruct:q4_k_m"

response = ollama.chat(
    model=llm_model,
    messages=[
        {
            "role": "user",
            "content": "請用繁體中文簡短自我介紹，並說明你擅長分析什麼類型的文件？",
        },
    ],
    keep_alive=0    #-- 保持常駐顯存用-1，維持5分鐘用"5m"
)

print("LLM 回應：")
print(response["message"]["content"])
print("\n" + "=" * 50 + "\n")

# ----------------------------------------------------
# 2. 測試 Embedding 向量生成 (Cross-lingual Embedding)
# ----------------------------------------------------
print("=== 測試 Embedding 向量生成 ===")
embed_model = "bge-m3"
sample_text = "Apple Inc. reported quarterly revenue of $89.5 billion."

embed_response = ollama.embeddings(model=embed_model, prompt=sample_text)

embedding_vector = embed_response["embedding"]
print("成功將英文財務句子轉為向量！")
print(f"向量維度大小: {len(embedding_vector)}")
print(f"向量前 5 個數值示範: {embedding_vector[:5]}")
