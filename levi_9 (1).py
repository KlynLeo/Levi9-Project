# -*- coding: utf-8 -*-
"""Levi_9.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1UGuGdSZAGOoCsnCgZx_eYEBUo8OQ3-J6
"""

!pip install datasets bs4 langchain sentence-transformers faiss-cpu transformers accelerate huggingface_hub -q
!pip install -U langchain-community gradio -q

from google.colab import files
import json
uploaded = files.upload()

with open("intents.json", "r") as f:
    data = json.load(f)

# Convert JSON -> DataFrame
import pandas as pd
from bs4 import BeautifulSoup
import re

records = []
for intent in data["intents"]:
    tag = intent["tag"]
    for pattern in intent["patterns"]:
        for response in intent["responses"]:
            records.append({
                "tag": tag,
                "pattern": pattern,
                "response": response
            })

df = pd.DataFrame(records)

def clean_text(text):
    text = BeautifulSoup(str(text), "html.parser").get_text()
    text = re.sub(r"\s+", " ", text).strip()
    return text

df["text"] = df.apply(lambda row: f"Q: {row['pattern']}\nA: {row['response']}", axis=1)
df["text"] = df["text"].apply(clean_text)
df = df[["pattern", "response", "text"]]
df.head()

# FAISS VectorStore + HuggingFace Embeddings
from langchain.vectorstores import FAISS
from langchain.embeddings import HuggingFaceEmbeddings
from langchain.docstore.document import Document

embedding_model = HuggingFaceEmbeddings(model_name="BAAI/bge-base-en-v1.5")

docs = [Document(page_content=row["text"]) for _, row in df.iterrows()]
vectorstore = FAISS.from_documents(docs, embedding_model)

retriever = vectorstore.as_retriever(search_kwargs={"k": 5})

#  MISTRAL LLM Setup
from huggingface_hub import login
from transformers import  AutoTokenizer, AutoModelForCausalLM, pipeline
from langchain.llms import HuggingFacePipeline

login(SECRET_KEY) #insert your hugging face api key to connect to the model

model_id = "mistralai/Mistral-7B-Instruct-v0.1"
tokenizer = AutoTokenizer.from_pretrained(model_id)
model = AutoModelForCausalLM.from_pretrained(model_id, device_map="auto", torch_dtype="auto")

mistral_pipeline = pipeline(
    "text-generation",
    model=model,
    tokenizer=tokenizer,
    max_new_tokens=512,
    do_sample=True,
    temperature=0.7,
    top_p=0.9,
    repetition_penalty=1.1
)

llm = HuggingFacePipeline(pipeline=mistral_pipeline)

# Prompt + rag_chain
from langchain.prompts import PromptTemplate
from langchain.chains import RetrievalQA

prompt_template = PromptTemplate(
    input_variables=["context", "question"],
    template="<s>[INST] Using the following context, answer the question as completely and accurately as possible.\n\nContext:\n{context}\n\nQuestion:\n{question} [/INST]"
)

rag_chain = RetrievalQA.from_chain_type(
    llm=llm,
    retriever=retriever,
    chain_type="stuff",
    chain_type_kwargs={"prompt": prompt_template}
)

def format_prompt_mistral(context, question):
    return f"<s>[INST] Using the following context, answer the question as completely and accurately as possible.\n\nContext:\n{context}\n\nQuestion:\n{question} [/INST]"

# Rerank + token truncation + QA funcție finală
import re
from sklearn.metrics.pairwise import cosine_similarity

# def rerank(query, docs, embedding_model, top_k):
#     query_emb = embedding_model.embed_query(query)
#     doc_embs = [embedding_model.embed_query(doc.page_content) for doc in docs]
#     scores = cosine_similarity([query_emb], doc_embs)[0]
#     ranked_docs = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
#     return [doc for doc, _ in ranked_docs[:top_k]]

def rerank(query, docs, embedding_model, top_k):
    query_with_prefix = f"Represent this sentence for searching relevant passages: {query}"
    query_emb = embedding_model.embed_query(query_with_prefix)
    doc_embs = [embedding_model.embed_query(doc.page_content) for doc in docs]
    scores = cosine_similarity([query_emb], doc_embs)[0]
    ranked_docs = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    return [doc for doc, _ in ranked_docs[:top_k]]

def truncate_to_token_limit(text, tokenizer, max_tokens=512):
    tokens = tokenizer.encode(text, truncation=True, max_length=max_tokens)
    return tokenizer.decode(tokens, skip_special_tokens=True)

def clean_mistral_output(raw_output: str):
    """Strip out everything before [/INST] and return just the answer."""
    if '[/INST]' in raw_output:
        return raw_output.split('[/INST]')[-1].strip()
    return raw_output.strip()

def clean_cutoff_output(text: str) -> str:
    sentences = re.split(r'(?<=[.!?])\s+', text)
    if len(sentences) > 1:
        return " ".join(sentences[:-1])
    return text.strip()


def generate_answer(query: str, similarity_threshold=0.60, top_k=4, max_tokens=512):
    try:
        # Step 1: Retrieve
        raw_context = retriever.get_relevant_documents(query)

        # Step 2: Rerank + threshold
        query_emb = embedding_model.embed_query(query)
        doc_texts = [doc.page_content for doc in raw_context]
        doc_embs = embedding_model.embed_documents(doc_texts)
        scores = cosine_similarity([query_emb], doc_embs)[0]
        ranked_docs = sorted(zip(raw_context, scores), key=lambda x: x[1], reverse=True)
        filtered_docs = [(doc, score) for doc, score in ranked_docs if score >= similarity_threshold]
        reranked_context = [doc for doc, _ in filtered_docs[:top_k]]

        if not reranked_context:
            return "I do not know, I do not have enough data."

        # Step 3: Prompt
        context = "\n".join([doc.page_content for doc in reranked_context])
        context = truncate_to_token_limit(context, tokenizer, max_tokens=max_tokens)
        prompt = format_prompt_mistral(context, query)

        # Step 4: Generate
        inputs = tokenizer(prompt, return_tensors="pt").to(model.device)
        outputs = model.generate(
            **inputs,
            max_new_tokens=256,
            eos_token_id=tokenizer.eos_token_id,
            do_sample=False,
            temperature=0.7,
            top_p=0.9,
            repetition_penalty=1.1
        )
        decoded = tokenizer.decode(outputs[0], skip_special_tokens=True)

        # Step 5: Clean output
        output = clean_mistral_output(decoded)
        return clean_cutoff_output(output)

    except Exception as e:
        return f"⚠️ Error: {str(e)}"

def run_rag(query, retriever, embedding_model, tokenizer, llm_pipeline, top_k=4, max_tokens=512, similarity_threshold=0.60):
    print("\n" + "=" * 60)
    print(f"📌 QUESTION: {query}")

    # 1. Retrieve documents
    raw_context = retriever.get_relevant_documents(query)
    print(f"\n🔍 Retrieved {len(raw_context)} raw documents.")

    # 2. Rerank + apply score threshold
    query_emb = embedding_model.embed_query(query)
    doc_texts = [doc.page_content for doc in raw_context]
    doc_embs = embedding_model.embed_documents(doc_texts)
    scores = cosine_similarity([query_emb], doc_embs)[0]

    # Step 3: Filter by threshold
    ranked_docs = sorted(zip(raw_context, scores), key=lambda x: x[1], reverse=True)
    filtered_docs = [(doc, score) for doc, score in ranked_docs if score >= similarity_threshold]
    reranked_context = [doc for doc, _ in filtered_docs[:top_k]]

    if reranked_context:
        print(f"\n📊 Similarity scores (Filtered Top {len(reranked_context)}):")
        for i, (doc, score) in enumerate(filtered_docs[:top_k]):
            print(f"\n➡️ Document {i+1} - Score: {score:.4f}")
            print(doc.page_content[:500] + ("..." if len(doc.page_content) > 500 else ""))
    else:
        print("\n⚠️ No documents passed the similarity threshold.")
        return "I do not know, I do not have enough data."

    # 4. Pregătim contextul pentru prompt
    context = "\n".join([doc.page_content for doc in reranked_context])
    context = truncate_to_token_limit(context, tokenizer, max_tokens=max_tokens)

    prompt = format_prompt_mistral(context, query)

    response = llm_pipeline(prompt)[0]["generated_text"]

    print("\n🧠 GENERATED ANSWER:\n")
    print(response)
    print("=" * 60)

    print("\n⭐⭐⭐⭐⭐\n")
    return response

query = "what is the difference between big data and databases?"

response = run_rag(
    query=query,
    retriever=retriever,
    embedding_model=embedding_model,
    tokenizer=tokenizer,
    llm_pipeline=mistral_pipeline,
    top_k=4,  # vezi toate cele 4 contexte
    max_tokens=512
)

questions = [
    "What is the difference between machine learning and deep learning?",
    "How is supervised learning different from unsupervised learning?",
    "What are the differences between SQL and NoSQL databases?",
    "How does cloud computing differ from traditional on-premise infrastructure?"
]

for query in questions:
    response = run_rag(
        query=query,
        retriever=retriever,
        embedding_model=embedding_model,
        tokenizer=tokenizer,
        llm_pipeline=mistral_pipeline,
        top_k=4,
        max_tokens=512
    )

query = "where is america?"

response = run_rag(
    query=query,
    retriever=retriever,
    embedding_model=embedding_model,
    tokenizer=tokenizer,
    llm_pipeline=mistral_pipeline,
    top_k=4,  # vezi toate cele 4 contexte
    max_tokens=512
)

# Gradio UI
import gradio as gr

demo = gr.Interface(
    fn=generate_answer,
    inputs=gr.Textbox(label="Pune o întrebare (ex: what is the difference between big data and databases?)"),
    outputs=gr.Textbox(label="Răspuns generat"),
    title="Computer Science Q&A (RAG Chatbot + Mistral)",
    description="Întreabă despre orice concept CS – chatbotul îți răspunde folosind vectori + Mistral-7B."
)


demo.launch()

