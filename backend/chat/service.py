from __future__ import annotations
import asyncio
import functools
import json
import os
import aiohttp
from typing import AsyncGenerator, Optional
from vector.chroma import get_user_collection, embed
from config import settings
from security.logging_setup import get_logger
from security.netguard import (
    NetGuardError,
    UpstreamProviderError,
    check_resolved_target,
    normalise_base_url,
    parse_endpoint,
    redact_body,
    redact_url,
    resolve_addresses,
)
from .citations import (
    group_citations,
    extract_citations,
    build_chunks,
    build_sources,
    abstention_message,
)
from .coverage import coverage_report, summarise_coverage


IN_DOCKER = os.path.exists("/.dockerenv")

log = get_logger("chat.service")


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


def _default_base_url(provider: Optional[str]) -> str:
    """The operator-configured base URL for `provider`.

    Single source of truth for "where do we go when the user did not say",
    called both by `_resolve_endpoint` and by `_guarded_endpoint` so the
    request path and the settings path can never disagree about the default
    and one of them quietly skips a check the other applies.
    """
    if _resolve_provider(provider) == "ollama":
        return settings.ollama_base_url
    return settings.openrouter_base_url


def _resolve_base_url(base_url: str) -> str:
    """Validate a base URL and normalise it for use.

    When running inside Docker, localhost refers to the container itself and
    is rewritten to host.docker.internal -- but only in the *host*, which is
    what the whole-URL `re.sub` this replaced failed to do (it also rewrote
    the word "localhost" inside a path or a query).  Trailing slashes are
    stripped, as before, so `f"{base}/chat/completions"` is unchanged.

    Raises `NetGuardError` for anything the guard refuses; the caller must
    not fall back to the raw value.
    """
    return normalise_base_url(base_url, in_docker=IN_DOCKER)


def _resolve_endpoint(provider: str, base_url: Optional[str], api_key: Optional[str], model: Optional[str]):
    """Return (url, headers, model) for the selected provider.

    Both OpenRouter and Ollama expose an OpenAI-compatible
    /chat/completions endpoint, so only the base URL and auth differ.
    """
    provider = _resolve_provider(provider)

    if provider == "ollama":
        effective_base = base_url or _default_base_url(provider)
        effective_model = model or settings.ollama_model
        headers = {"Content-Type": "application/json"}
        # Ollama ignores the key but some setups put it behind a proxy that needs one
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:  # openrouter (default)
        effective_base = base_url or _default_base_url(provider)
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


async def _guarded_endpoint(
    provider: Optional[str],
    base_url: Optional[str],
    api_key: Optional[str],
    model: Optional[str],
    *,
    resolver=None,
):
    """Resolve, validate and address-check the provider endpoint. (SEC-1)

    The single choke point for every outbound LLM request.  Audit finding 12
    was that a registered user could put any URL in `base_url` and the
    server would connect to it, so this is where the decision is made --
    once, for the settings path and the request path alike.

    `origin` is the security-relevant distinction:

    * `"settings"` -- the URL came from operator configuration. It is still
      parsed and validated (a typo in `.env` is worth catching), but the
      resolved-address policy is not applied, because a private-LAN Ollama
      is a legitimate self-hosted setup and no user can reach this branch.
    * `"request"` -- the URL came out of a request body. It is resolved and
      every address it resolves to must be permitted, with
      `LLM_ALLOW_PRIVATE_HOSTS=false` meaning "public only".  That is what
      removes the authenticated read-oracle against the cloud metadata
      endpoint, Redis, and anything else on the LAN.

    The resolution runs in a thread executor: `getaddrinfo` is a blocking
    call and this runs on the event loop of a streaming endpoint.

    `resolver` is injectable so the guard's decision logic is testable
    without a network; production always leaves it None.
    """
    origin = "request" if base_url else "settings"
    try:
        log.info(
            "llm.endpoint_resolve provider=%s origin=%s",
            _resolve_provider(provider),
            origin,
        )
        endpoint = parse_endpoint(
            base_url or _default_base_url(provider), in_docker=IN_DOCKER
        )
        if origin == "request":
            loop = asyncio.get_running_loop()
            addresses = await loop.run_in_executor(
                None,
                functools.partial(
                    resolve_addresses, endpoint.host, endpoint.port, resolver=resolver
                ),
            )
            check_resolved_target(
                endpoint.base_url,
                addresses,
                allow_private=settings.llm_allow_private_hosts,
                origin=origin,
            )
    except NetGuardError as exc:
        log.warning(
            "llm.base_url.rejected origin=%s code=%s url=%s",
            origin,
            exc.code,
            redact_url(base_url or ""),
        )
        raise
    except Exception as exc:
        # Fail closed.  A guard that raised something unexpected and let the
        # request continue has failed at the only job it has, so an
        # unexpected error inside the guard becomes a refusal.
        log.exception("llm.base_url.guard_error origin=%s", origin)
        raise NetGuardError(
            "malformed_url", "the outbound URL guard failed to evaluate the base_url"
        ) from exc

    return _resolve_endpoint(provider, endpoint.base_url, api_key, model)


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

    # All three result lists are treated the same defensive way. `distances` was
    # already guarded; `documents` and `metadatas` were not, and a `where` filter
    # that matches nothing is exactly the case that makes a version return a
    # missing or short list. Indexing those two directly raised TypeError inside
    # the stream generator, which meant no `done` event ever arrived and the
    # client sat there with a dead composer.
    documents = (results.get("documents") or [[]])[0] or []
    metadatas = (results.get("metadatas") or [[]])[0] or []
    distances = (results.get("distances") or [[]])[0] or []

    return build_chunks(documents, metadatas, distances)


def build_system_prompt(chunks: list[dict]) -> tuple[str, list[dict]]:
    """Return (system_prompt, groups).

    `groups` is the output of group_citations(chunks) — one entry per distinct
    (arxiv_id, page) — and the [n] markers written into the prompt are exactly
    `group["label"]`. The caller reuses those same labels for the sources list,
    so the numbering the model sees and the numbering the client renders can
    no longer disagree.

    Empty-library branch: the prompt text is unchanged and the second element
    is [] (there is nothing to cite).
    """
    if not chunks:
        # Defence in depth. The prompt is unreachable whenever retrieval is empty
        # (stream_rag_response returns before calling the LLM), but if that ever
        # changes, this branch must still not invite an unsourced answer.
        return (
            "You are a research assistant. No relevant papers were found in the library. "
            "Tell the user plainly that their library does not contain the answer and "
            "suggest adding a relevant paper. Do not answer from memory.",
            [],
        )

    groups = group_citations(chunks)

    context_parts = []
    for group in groups:
        context_parts.append(
            f"[{group['label']}] Source: \"{group['title']}\", page {group['page']}\n{group['text']}"
        )
    context = "\n\n---\n\n".join(context_parts)

    system_prompt = f"""You are a research assistant helping with scientific papers.
Use ONLY the provided context to answer the user's question. Cite sources by their number [1], [2], etc.
If the context doesn't contain enough information, say so clearly.

Context:
{context}"""

    return system_prompt, groups


async def stream_rag_response(
    user_id: int,
    question: str,
    history: list[dict],
    paper_ids: Optional[list[int]] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    # Last, and keyword-only in practice: `paper_ids` is the 4th positional, so a
    # new parameter inserted anywhere earlier would silently re-order call sites.
    scope_titles: Optional[list[str]] = None,
) -> AsyncGenerator[str, None]:
    # What was actually searched, echoed back so the client can say so. Recorded
    # before retrieval so the abstention path below can report it too.
    scope = {
        "applied": bool(paper_ids),
        "paper_ids": list(paper_ids) if paper_ids else [],
        "paper_titles": list(scope_titles) if scope_titles else [],
    }

    chunks = retrieve_context(user_id, question, paper_ids)

    # Honest abstention, decided here rather than in the prompt. No passage came
    # back, so there is nothing to ground an answer in: the app says so in its own
    # words and never opens an LLM connection. That makes the refusal
    # deterministic, free, and impossible for the model to talk its way around.
    if not chunks:
        message = abstention_message(scope)
        yield f"data: {json.dumps({'type': 'token', 'content': message})}\n\n"
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "done",
                    "sources": [],
                    "cited": [],
                    "unresolved": [],
                    "scope": scope,
                    "coverage": None,
                    "coverage_line": None,
                    "abstained": True,
                }
            )
            + "\n\n"
        )
        return

    system, groups = build_system_prompt(chunks)

    messages = [*history, {"role": "user", "content": question}]

    # Sources are built from the same groups (and therefore the same labels)
    # that were numbered into the prompt, so the inline [n] markers the model
    # emits always point at the right page of the right paper.
    sources = build_sources(groups)

    # Which of the question's distinctive terms the retrieved pages actually
    # contain. Measured against the merged group text, i.e. exactly what the
    # model was shown. A lexical fact about the evidence, not a score, and never
    # a verdict about the answer -- so it is reported alongside the answer rather
    # than fed back into the prompt.
    coverage = coverage_report(question, [g["text"] for g in groups])
    # The line the user actually reads is rendered HERE, not re-derived in the
    # client, so the wording covered by the test suite is the wording shipped.
    coverage_line = summarise_coverage(coverage)

    if scope["applied"] and scope["paper_titles"]:
        system = (
            f"{system}\n\nThe user has restricted this question to these papers: "
            f"{', '.join(scope['paper_titles'])}. If the provided context does not "
            f"answer it, say so plainly instead of guessing."
        )

    # Resolve endpoint, auth and model based on the selected provider, with
    # the outbound URL checked first (SEC-1).  Deliberately after the
    # abstention return above: an empty library must not open a connection
    # to anything, and must not resolve a hostname either.
    url, headers, effective_model = await _guarded_endpoint(
        provider, base_url, api_key, model
    )

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
    # Visible text, collected alongside the SSE tokens so the finished answer
    # can be checked for real vs. fabricated citations once it is complete.
    answer_parts = []

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                # The old line interpolated the provider's response body into
                # the exception message, and the router put str(exc) into the
                # `done` event -- which is how the SSRF became a *read*
                # oracle.  The body is now redacted for the log only, and
                # `public_error_message` is what reaches the client.
                raise UpstreamProviderError(
                    response.status, redact_url(url), redact_body(error_text)
                )

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

    # Which labels did the answer actually use, and which did it invent?
    answer = "".join(answer_parts)
    cited, unresolved = extract_citations(answer, [g["label"] for g in groups])

    yield (
        "data: "
        + json.dumps(
            {
                "type": "done",
                "sources": sources,
                "cited": cited,
                "unresolved": unresolved,
                "scope": scope,
                "coverage": coverage,
                "coverage_line": coverage_line,
                "abstained": False,
            }
        )
        + "\n\n"
    )
