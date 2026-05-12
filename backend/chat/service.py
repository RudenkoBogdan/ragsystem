from __future__ import annotations
import json
import aiohttp
from typing import AsyncGenerator, Optional
from vector.chroma import get_user_collection, embed
from config import settings


def retrieve_context(user_id: int, query: str, paper_ids: Optional[list[int]] = None) -> list[dict]:
    collection = get_user_collection(user_id)
    query_embedding = embed([query])[0]

    where = None
    if paper_ids:
        where = {"paper_id": {"$in": [str(pid) for pid in paper_ids]}}

    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=settings.rag_top_k,
        where=where,
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    for doc, meta in zip(results["documents"][0], results["metadatas"][0]):
        chunks.append({"text": doc, "title": meta["title"], "arxiv_id": meta["arxiv_id"], "page": meta["page"]})
    return chunks


def build_system_prompt(chunks: list[dict]) -> str:
    if not chunks:
        return (
            "You are a research assistant. No relevant papers were found in the library. "
            "Tell the user to add papers first, then answer based on your general knowledge if helpful."
        )

    context_parts = []
    for i, chunk in enumerate(chunks, 1):
        context_parts.append(
            f"[{i}] Source: \"{chunk['title']}\", page {chunk['page']}\n{chunk['text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    return f"""You are a research assistant helping with scientific papers.
Use ONLY the provided context to answer the user's question. Cite sources by their number [1], [2], etc.
If the context doesn't contain enough information, say so clearly.

Context:
{context}"""


async def stream_rag_response(
    user_id: int,
    question: str,
    history: list[dict],
    paper_ids: Optional[list[int]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    chunks = retrieve_context(user_id, question, paper_ids)
    system = build_system_prompt(chunks)

    messages = [*history, {"role": "user", "content": question}]

    sources = [
        {"title": c["title"], "arxiv_id": c["arxiv_id"], "page": c["page"]}
        for c in chunks
    ]
    # Deduplicate sources
    seen = set()
    unique_sources = []
    for s in sources:
        key = (s["arxiv_id"], s["page"])
        if key not in seen:
            seen.add(key)
            unique_sources.append(s)

    # Use provided API key or fall back to settings
    effective_api_key = api_key or settings.anthropic_api_key
    effective_model = model or settings.claude_model

    headers = {
        "Authorization": f"Bearer {effective_api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://ragsystem.local",
        "X-Title": "RAG Research Assistant",
    }

    payload = {
        "model": effective_model,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": 2048,
        "stream": True,
        "temperature": 0.7,
    }

    url = "https://openrouter.ai/api/v1/chat/completions"

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"OpenRouter API error {response.status}: {error_text}")

            async for line in response.content:
                line = line.decode("utf-8").strip()
                if not line or not line.startswith("data: "):
                    continue

                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    data = json.loads(data_str)
                    if "choices" in data and data["choices"]:
                        delta = data["choices"][0].get("delta", {})
                        if "content" in delta and delta["content"]:
                            yield f"data: {json.dumps({'type': 'token', 'content': delta['content']})}\n\n"
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

    yield f"data: {json.dumps({'type': 'done', 'sources': unique_sources})}\n\n"
