"""Citation grouping and verification for RAG answers.

This module is deliberately **pure and stdlib-only**: no third-party imports,
no I/O, no globals mutated. It is therefore importable by a bare ``python3``
with nothing installed, which makes it the natural unit under test for the
"verifiable citations" contract.

Why it exists
-------------
Retrieval returns *chunks*, but the answer must cite *pages*. Two chunks can
come from the same ``(arxiv_id, page)`` pair, and the old code numbered the
prompt over raw chunks while de-duplicating the source list. Every citation
after the first duplicate therefore pointed at the wrong page of the wrong
paper. ``group_citations`` collapses chunks into page-level citation groups
**once**, and that single numbering is used for the prompt, the sources list
and the client. ``extract_citations`` then reads the finished answer back and
reports which labels were really cited and which were invented.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

__all__ = [
    "group_citations",
    "extract_citations",
    "build_chunks",
    "build_sources",
    "abstention_message",
    "CITATION_PATTERN",
    "SNIPPET_CHARS",
]

#: How many characters of retrieved text ride along in the sources payload.
SNIPPET_CHARS = 400


# An inline citation marker: [1], [2], [12], tolerating inner whitespace
# such as "[ 3 ]".
#
# The pattern is an alternation of two branches, and the order matters.
#
# Branch 1 -- a reference-style IMAGE, `![alt][2]`.  It is matched purely so
# that it is CONSUMED: `finditer` scans left to right and never overlaps, so
# swallowing the whole construct is what stops the trailing `[2]` from then
# being re-matched as a citation.  This branch captures nothing, so a match
# from it has `group(1) is None` and `extract_citations` skips it.  A
# fixed-length lookbehind cannot express this (the alt text is variable
# length) and `re` has no variable-length lookbehind, so consuming the
# construct is the correct mechanism rather than a workaround.
#
# Branch 2 -- a real citation, with these guards:
#   (?<!\!)   do not match the "!" of an inline image, e.g. `![fig](f.png)`
#   (?<!\w)   do not match array/sequence indexing such as `arr[1]`
#   (?<!\()   do not match a parenthesised citation such as `([1])`
#   (?!\()    do not match markdown link syntax such as `[1](http://...)`
#   (?!\s*:)  do not match a reference-style LINK DEFINITION such as
#             `[2]: fig.png`, which declares a target rather than citing it
#   \d+       greedy over the whole digit run, so `[123456]` can never be
#             truncated into a partial (and wrong) label such as 1234
#
# Consequences of these guards, which are part of the tested contract:
#   * `[abc]`, `[ ]`, `[x]`      -> no match (non-numeric / checkbox syntax)
#   * `[1](http://example)`      -> no match (markdown link)
#   * `arr[1]`, `![alt][2]`      -> no match (not a citation)
#   * `[2]: fig.png`             -> no match (link definition, not a citation)
#   * `[123456]`                 -> matches 123456 in full, so it is reported
#                                   as unresolved (a fabricated label) rather
#                                   than silently ignored
#   * `[[1]]`                    -> the INNER `[1]` is treated as the real
#                                   marker and resolves to label 1. Doubled
#                                   brackets are a common rendering artifact
#                                   of markdown; they are not a distinct
#                                   label, so they must not be reported as
#                                   fabricated.  Note this is why branch 2
#                                   may NOT simply forbid a preceding `]`:
#                                   adjacent real markers such as `[1][2]`
#                                   are legal and must all be reported.
#
# Known, deliberate ambiguity: `(?!\s*:)` rejects a genuine citation that is
# immediately followed by a colon, as in "According to [1]: the method ...".
# Telling that apart from a link-reference definition would need whole-line
# link-definition parsing, which is out of scope here; erring towards "not a
# citation" can only under-report, never mis-attribute a claim to a source.
CITATION_PATTERN = re.compile(
    r"!\[[^\]\n]*\]\[\s*\d+\s*\]"
    r"|(?<![!\w(])\[\s*(\d+)\s*\](?!\()(?!\s*:)"
)


def _coerce_score(value) -> Optional[float]:
    """Best-effort conversion of a raw chunk score to float.

    Returns ``None`` when the score is absent, ``None`` already, or not a
    number — an unscoreable chunk must never break the prompt.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def group_citations(chunks: Iterable[dict]) -> List[dict]:
    """Collapse retrieved chunks into page-level citation groups.

    Parameters
    ----------
    chunks:
        Raw retrieved chunk dicts, in retrieval (most-relevant-first) order.
        Each is expected to carry ``text``, ``title``, ``arxiv_id``, ``page``
        and (optionally) ``score``.

    Returns
    -------
    list[dict]
        One group per distinct ``(arxiv_id, page)``, in order of first
        appearance. Each group has exactly these keys:

        ``label``       int, 1-based, sequential, no gaps. This is the number
                        shown to the model *and* the number carried in the
                        sources list, so the two can never drift apart.
        ``title``       str, from the first chunk of the group.
        ``arxiv_id``    str, from the first chunk of the group.
        ``page``        int, from the first chunk of the group.
        ``text``        str, every passage in the group in retrieval order,
                        joined by a blank line ("\\n\\n"). Concatenation is
                        lossless: no character of any passage is dropped.
        ``chunk_count`` int, how many chunks were merged into this group.
        ``score``       float, the score of the FIRST chunk in the group (the
                        most relevant passage), or ``None`` when the input
                        carried no usable score.

    Pure and deterministic: no argument is mutated, and the same input always
    produces the same output. ``group_citations([]) == []``.
    """
    groups: List[dict] = []
    # key -> position in `groups`; keeps first-appearance order and dedup.
    positions = {}
    # key -> list of passage texts, kept apart so the group dicts stay clean.
    parts: dict = {}

    for chunk in chunks or []:
        key = (chunk.get("arxiv_id"), chunk.get("page"))
        text = chunk.get("text") or ""
        if key in positions:
            parts[key].append(text)
            groups[positions[key]]["chunk_count"] += 1
            continue
        positions[key] = len(groups)
        parts[key] = [text]
        groups.append(
            {
                "label": len(groups) + 1,
                "title": chunk.get("title") or "",
                "arxiv_id": chunk.get("arxiv_id"),
                "page": chunk.get("page"),
                "text": "",  # filled in below, once the group is complete
                "chunk_count": 1,
                "score": _coerce_score(chunk.get("score")),
            }
        )

    for group in groups:
        group["text"] = "\n\n".join(parts[(group["arxiv_id"], group["page"])])

    return groups


def build_chunks(
    documents: Optional[Iterable[Optional[str]]],
    metadatas: Optional[Iterable[Optional[dict]]],
    distances: Optional[Iterable[Optional[float]]],
) -> List[dict]:
    """Project one Chroma query result into the chunk dicts retrieval consumes.

    This is the ``retrieve_context`` -> ``group_citations`` link, lifted out of
    the streaming service so it is testable here rather than only in a module
    that needs ``aiohttp`` to import.

    Every input is treated as untrusted, because they are: a ``where`` filter
    that matches nothing can yield a missing/short list depending on the
    chromadb version, and a collection written by an older ingest schema can be
    missing metadata keys. Ragged lists, ``None`` elements and absent keys all
    degrade instead of raising, because the alternative is a 500 in the middle
    of a stream -- which strands the client's composer permanently.

    ``score`` is the cosine similarity ``1.0 - distance`` rounded to 4 places,
    or ``None`` when the distance is absent or not a number.
    """
    documents = list(documents or [])
    metadatas = list(metadatas or [])
    distances = list(distances or [])

    chunks: List[dict] = []
    for index, document in enumerate(documents):
        meta = metadatas[index] if index < len(metadatas) else None
        if not isinstance(meta, dict):
            meta = {}
        distance = distances[index] if index < len(distances) else None
        score: Optional[float] = None
        if distance is not None:
            try:
                score = round(1.0 - float(distance), 4)
            except (TypeError, ValueError):
                score = None
        chunks.append(
            {
                "text": document,
                "title": meta.get("title") or "",
                "arxiv_id": meta.get("arxiv_id"),
                "page": meta.get("page"),
                "score": score,
            }
        )
    return chunks


def build_sources(groups: Optional[Iterable[dict]], snippet_chars: int = SNIPPET_CHARS) -> List[dict]:
    """Project citation groups into the ``sources`` list sent to the client.

    One source per group, in group order, carrying exactly the keys the client
    renders: the authoritative ``label`` (never the array position), the paper
    identity, a ``snippet`` of the retrieved text, the similarity and how many
    passages from that page were merged.

    Pure and deterministic. ``group_citations([])`` gives ``[]``.
    """
    sources: List[dict] = []
    for group in groups or []:
        if not isinstance(group, dict):
            continue
        text = group.get("text") or ""
        sources.append(
            {
                "label": group.get("label"),
                "title": group.get("title") or "",
                "arxiv_id": group.get("arxiv_id"),
                "page": group.get("page"),
                "snippet": text[:snippet_chars],
                "score": group.get("score"),
                "chunk_count": group.get("chunk_count"),
            }
        )
    return sources


def abstention_message(scope: Optional[dict] = None) -> str:
    """The app-authored sentence shown when retrieval found nothing at all.

    Deliberately not written by the model. When no passage came back there is no
    evidence to ground an answer in, and the previous behaviour -- instructing
    the model to "answer based on your general knowledge if helpful" -- turned
    that exact moment into a confident, source-free answer. This function is the
    refusal, and it is deterministic, free, and un-gameable.

    Never raises on a missing or partial ``scope``. Never contains the phrase
    "general knowledge" and never contains a ``[n]`` citation marker, so it can
    never be mistaken for a sourced claim.
    """
    titles: List[str] = []
    if isinstance(scope, dict):
        raw = scope.get("paper_titles") or []
        if isinstance(raw, (list, tuple)):
            titles = [t.strip() for t in raw if isinstance(t, str) and t.strip()]

    if titles:
        return (
            "I searched only the paper(s) you scoped this question to (%s) and found "
            "nothing relevant, so I am not going to answer from memory. Widen the scope "
            "to your whole library, or add the paper that would contain the answer."
            % ", ".join(titles)
        )

    # A scope whose ids no longer resolve -- every paper deleted, or ids belonging
    # to another user. Retrieval was still restricted, so claiming "in your
    # library" would misdescribe what was searched.
    if isinstance(scope, dict) and scope.get("applied"):
        count = len(scope.get("paper_ids") or [])
        if count == 1:
            return (
                "I searched only the one paper you scoped this question to and found "
                "nothing relevant, so I am not going to answer from memory. Widen the "
                "scope to your whole library, or add the paper that would contain the "
                "answer."
            )
        return (
            "I searched only the %d papers you scoped this question to and found nothing "
            "relevant, so I am not going to answer from memory. Widen the scope to your "
            "whole library, or add the paper that would contain the answer." % count
        )

    return (
        "I could not find anything relevant to that in your library, so I am not going "
        "to answer from memory. Add a paper that covers it, or rephrase the question."
    )


def extract_citations(text: Optional[str], valid_labels: Optional[Iterable[int]]) -> Tuple[List[int], List[int]]:
    """Scan a finished answer for inline citation markers.

    Parameters
    ----------
    text:
        The answer body, as finally emitted to the client (i.e. after any
        reasoning block has been stripped). ``None`` is treated as ``""``.
    valid_labels:
        The labels that actually exist, i.e.
        ``[g["label"] for g in groups]``. May be any iterable of ints or a set;
        ``None`` is treated as "no valid labels".

    Returns
    -------
    (cited, unresolved): tuple[list[int], list[int]]
        ``cited``      distinct labels that were cited AND exist, in order of
                       first appearance in the answer (so the UI can highlight
                       them in reading order).
        ``unresolved`` distinct labels that were cited but do NOT exist,
                       sorted ascending. A model claiming "[9]" when only five
                       sources exist is fabricating, and surfacing that is the
                       point of this feature.

    Pure and deterministic. Never raises on odd input.
    """
    valid = set(valid_labels) if valid_labels is not None else set()

    cited: List[int] = []
    seen: set = set()
    unresolved: set = set()

    for match in CITATION_PATTERN.finditer(text or ""):
        digits = match.group(1)
        if digits is None:
            # Branch 1 of CITATION_PATTERN matched a reference-style image
            # (`![alt][2]`). It was consumed so its label cannot be mistaken
            # for a citation, and it deliberately captures nothing.
            continue
        label = int(digits)
        if label in valid:
            if label not in seen:
                seen.add(label)
                cited.append(label)
        else:
            unresolved.add(label)

    return cited, sorted(unresolved)
