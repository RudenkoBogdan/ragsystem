from __future__ import annotations
import json
import os
import re
import aiohttp
from typing import AsyncGenerator, Optional
from vector.chroma import get_user_collection, embed
from config import settings
from .citations import group_citations, extract_citations


IN_DOCKER = os.path.exists("/.dockerenv")


class ThinkFilter:
    """Strips a leading <think>...</think> reasoning block from a token stream.

    Reasoning models (e.g. Qwen3) emit their chain-of-thought wrapped in
    <think> tags before the actual answer. For RAG we don't want to show it,
    so we buffer the leading tokens until we can decide, then pass the rest
    through unchanged."""

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self.buf = ""
        self.done = False
        self.emitted = False

    def _emit(self, text: str) -> str:
        # Trim leading whitespace until the first real character of the answer.
        if not self.emitted:
            text = text.lstrip()
            if text:
                self.emitted = True
        return text

    def feed(self, text: str) -> str:
        if self.done:
            return self._emit(text)
        self.buf += text
        s = self.buf.lstrip()
        # Buffer is still a prefix of "<think>" (incl. empty/whitespace) — wait.
        if self.OPEN.startswith(s):
            return ""
        if s.startswith(self.OPEN):
            idx = self.buf.find(self.CLOSE)
            if idx == -1:
                return ""  # still inside the think block, keep buffering
            out = self.buf[idx + len(self.CLOSE):]
            self.buf = ""
            self.done = True
            return self._emit(out)
        # Definitely no think block — flush everything.
        out = self.buf
        self.buf = ""
        self.done = True
        return self._emit(out)

    def flush(self) -> str:
        if self.done:
            return ""
        out = self.buf
        self.buf = ""
        self.done = True
        return self._emit(out)


def _resolve_provider(provider: Optional[str]) -> str:
    return (provider or settings.llm_provider or "openrouter").lower()


def _resolve_base_url(base_url: str) -> str:
    """When running inside Docker, localhost refers to the container itself.
    Rewrite it to host.docker.internal so the backend can reach a service
    (e.g. Ollama) running on the host machine."""
    if IN_DOCKER:
        base_url = re.sub(r"(localhost|127\.0\.0\.1)", "host.docker.internal", base_url)
    return base_url.rstrip("/")


def _resolve_endpoint(provider: str, base_url: Optional[str], api_key: Optional[str], model: Optional[str]):
    """Return (url, headers, model) for the selected provider.

    Both OpenRouter and Ollama expose an OpenAI-compatible
    /chat/completions endpoint, so only the base URL and auth differ.
    """
    provider = _resolve_provider(provider)

    if provider == "ollama":
        effective_base = base_url or settings.ollama_base_url
        effective_model = model or settings.ollama_model
        headers = {"Content-Type": "application/json"}
        # Ollama ignores the key but some setups put it behind a proxy that needs one
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:  # openrouter (default)
        effective_base = base_url or settings.openrouter_base_url
        effective_model = model or settings.claude_model
        effective_key = api_key or settings.anthropic_api_key
        headers = {
            "Authorization": f"Bearer {effective_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://ragsystem.local",
            "X-Title": "RAG Research Assistant",
        }

    url = f"{_resolve_base_url(effective_base)}/chat/completions"
    return url, headers, effective_model


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

    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    distances = (results.get("distances") or [[]])[0] or []

    chunks = []
    for i, (doc, meta) in enumerate(zip(documents, metadatas)):
        score = None
        if i < len(distances) and distances[i] is not None:
            try:
                score = round(1.0 - float(distances[i]), 4)
            except (TypeError, ValueError):
                score = None
        chunks.append({
            "text": doc,
            "title": meta["title"],
            "arxiv_id": meta["arxiv_id"],
            "page": meta["page"],
            "score": score,
        })
    return chunks


def build_system_prompt(chunks: list[dict]) -> tuple[str, list[dict]]:
    if not chunks:
        return (
            "You are a research assistant. No relevant papers were found in the library. "
            "Tell the user to add papers first, then answer based on your general knowledge if helpful.",
            [],
        )

    groups = group_citations(chunks)
    context_parts = []
    for group in groups:
        context_parts.append(
            f"[{group['label']}] Source: \"{group['title']}\", page {group['page']}\n{group['text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    prompt = f"""You are a research assistant helping with scientific papers.
Use ONLY the provided context to answer the user's question. Cite sources by their number [1], [2], etc.
If the context doesn't contain enough information, say so clearly.

Context:
{context}"""
    return prompt, groups


async def stream_rag_response(
    user_id: int,
    question: str,
    history: list[dict],
    paper_ids: Optional[list[int]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    chunks = retrieve_context(user_id, question, paper_ids)
    system, groups = build_system_prompt(chunks)

    messages = [*history, {"role": "user", "content": question}]

    sources = [
        {
            "label": group["label"],
            "title": group["title"],
            "arxiv_id": group["arxiv_id"],
            "page": group["page"],
            "snippet": group["text"][:400],
            "score": group["score"],
            "chunk_count": group["chunk_count"],
        }
        for group in groups
    ]

    # Resolve endpoint, auth and model based on the selected provider
    url, headers, effective_model = _resolve_endpoint(provider, base_url, api_key, model)

    # For local reasoning models (Qwen3 via Ollama) disable chain-of-thought:
    # RAG answers don't need it and it slows generation considerably.
    is_ollama = _resolve_provider(provider) == "ollama"
    if is_ollama:
        system = f"{system}\n\n/no_think"

    payload = {
        "model": effective_model,
        "messages": [{"role": "system", "content": system}, *messages],
        "max_tokens": 2048,
        "stream": True,
        "temperature": 0.7,
    }

    think_filter = ThinkFilter()
    answer_parts = []

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise RuntimeError(f"LLM API error {response.status} ({url}): {error_text}")

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
                            visible = think_filter.feed(delta["content"])
                            if visible:
                                answer_parts.append(visible)
                                yield f"data: {json.dumps({'type': 'token', 'content': visible})}\n\n"
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

    # Flush any buffered content (e.g. a response with no think block at all)
    tail = think_filter.flush()
    if tail:
        answer_parts.append(tail)
        yield f"data: {json.dumps({'type': 'token', 'content': tail})}\n\n"

    cited, unresolved = extract_citations(
        "".join(answer_parts), [group["label"] for group in groups]
    )
    yield f"data: {json.dumps({'type': 'done', 'sources': sources, 'cited': cited, 'unresolved': unresolved})}\n\n"
