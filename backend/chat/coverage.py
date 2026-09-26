"""Retrieval coverage: which of your question's terms the pages actually contain.

This module is deliberately **pure and stdlib-only** -- no third-party imports,
no I/O, no globals mutated -- exactly like its sibling :mod:`chat.citations`, so
it is importable by a bare ``python3`` with nothing installed and is the natural
unit under test.

Why it exists
-------------
Top-5 cosine retrieval over a 40-paper library is a lottery: nothing on screen
tells you whether the passages the model answered from even *mention* the things
you asked about. The relevance bar shows a similarity number, but a similarity
number cannot answer the only question a reader actually has -- *"did the thing I
typed show up in the evidence?"*

``coverage_report`` answers exactly that, and only that. It compares the
question's **distinctive terms** against the **text the model was actually
shown** (the merged group text, not the raw chunks) and reports which terms are
present and which are not. It is a lexical fact, not a probability:

* it never emits a percentage, a score, a confidence, or a probability;
* it never claims the answer is right or wrong;
* ``missing`` is shipped, not just the count, because "covered 5 of 7" with no
  way to see the two is an unbacked assertion in the same shape as the very
  ungrounded "58% match" it is meant to replace.

The one thing it is NOT
-----------------------
It is **not** retrieval quality, answer correctness, or confidence. It reads
*low* on a correct answer whose vocabulary differs from the paper's, and *high*
on a wrong answer that merely echoes the question. With ``rag_top_k=5`` of
512-word chunks (~2 500 words) a distinctive term living on page 40 is
legitimately absent. That is why the summary says "not found in the retrieved
pages" and never "not in the paper".
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Set

__all__ = [
    "STOPWORDS",
    "MIN_TERMS",
    "MAX_REPORTED",
    "key_terms",
    "coverage_report",
    "summarise_coverage",
]


#: Below this many distinctive terms the readout is suppressed entirely. Aggressive
#: stop-wording leaves a two-word question with two surviving terms, and
#: "covered 2 of 2" is a perfect score that means nothing.
MIN_TERMS = 3

#: How many term names are listed in the report. ``total`` and ``covered`` stay
#: uncapped so the numbers are always the true counts.
MAX_REPORTED = 12


# Closed-class English, scholarly apparatus, and the generic research nouns that
# appear in essentially every ML paper. The last group matters most: "model",
# "method", "approach" and "paper" would otherwise land in nearly every question
# and be trivially "covered", inflating the readout with noise.
#
# Explicitly NOT here, and pinned by tests: attention, transformer, encoder,
# decoder, embedding, learning, training, neural, network, gradient, token,
# sequence, data, loss, layer, function, value, result, performance, accuracy,
# task, problem, state, space, softmax, lora, rlhf. Removing any of those would
# make the readout meaningless.
STOPWORDS = frozenset(
    [
        # closed-class English
        "a", "about", "above", "after", "again", "against", "all", "already", "also",
        "although", "am", "among", "an", "and", "another", "any", "are", "as", "at",
        "because", "been", "before", "being", "below", "between", "both", "but", "by",
        "can", "could", "did", "do", "does", "doing", "done", "down", "during", "each",
        "either", "else", "end", "even", "ever", "every", "few", "first", "for", "from",
        "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself",
        "him", "himself", "his", "how", "however", "i", "if", "in", "into", "is", "it",
        "its", "itself", "just", "least", "less", "let", "like", "made", "make", "many",
        "may", "me", "might", "mine", "more", "most", "much", "must", "my", "myself",
        "neither", "never", "next", "no", "nor", "not", "now", "of", "off", "on", "once",
        "one", "only", "onto", "or", "other", "others", "ought", "our", "ours", "ourselves",
        "out", "over", "own", "per", "perhaps", "rather", "same", "shall", "she", "should",
        "since", "so", "some", "still", "such", "than", "that", "the", "their", "theirs",
        "them", "themselves", "then", "there", "these", "they", "this", "those", "though",
        "through", "thus", "till", "to", "together", "too", "under", "until", "up", "upon",
        "us", "very", "via", "was", "we", "were", "what", "when", "where", "whether",
        "which", "while", "who", "whom", "why", "will", "with", "within", "without",
        "would", "yet", "you", "your", "yours",
        # scholarly apparatus -- "et al." is the one that actually bites
        "al", "cf", "eg", "et", "etc", "ie", "vs",
        "able", "according", "based", "describe", "described", "describes", "given",
        "letting", "need", "needs", "present", "presented", "presents", "propose",
        "proposed", "proposes", "show", "showed", "shown", "shows", "use", "used",
        "uses", "using", "want", "wants",
        # section / apparatus labels printed in running text
        "appendix", "arxiv", "chapter", "cite", "citation", "conference", "equation",
        "eq", "eqn", "fig", "figure", "journal", "page", "pages", "pp", "preprint",
        "proceedings", "ref", "refs", "reference", "references", "sec", "section",
        "subsection", "table", "tab", "trans", "vol",
        # generic research nouns -- present in nearly every paper
        "approach", "approaches", "art", "method", "methods", "model", "models",
        "paper", "papers", "sota", "study", "studies", "work", "works",
    ]
)


# A morphological variant of a question term that counts as "present" in a
# passage. Applied to the PASSAGE side only, so it can recognise a variant
# ("encoders" for "encoder") but never invents a term out of nothing.
#
# The two tiers exist because a single permissive list produces *false*
# positives, which is the one direction this readout is not allowed to err in.
# It starts from "can recognise a variant" and has to be earned back:
#
#   * "s", "es", "ed", "ing" are safe at any length -- "layers" for "layer",
#     "trained" for "train" are ordinary inflections.
#   * "er", "ers", "al" are derivational, and on a short term they cross
#     derivational classes rather than inflecting it. Measured: "low" was
#     reported as covered by a passage reading "a LOWER bound"; "high" by
#     "a HIGHER resolution"; "form" by "the FORMAL model". Those are the
#     opposite of a hit -- they mark a term as found when the evidence does
#     not contain it -- so they are gated on a term long enough to have a
#     derivational suffix at all (>= 6 characters: "attention" -> "attentional"
#     survives, "low" -> "lower" does not).
#
# "ion"/"ions" is excluded outright: "cat" + "ion" = "cation" is a real false
# positive. Whole-token-only matching under-reports "tokenizer"/"tokenize"; that
# residual is accepted, and it errs towards *understating* coverage.
_INFLECTIONAL_SUFFIXES = ("s", "es", "ed", "ing")
_DERIVATIONAL_SUFFIXES = ("er", "ers", "al")
_MIN_TERM_FOR_DERIVATION = 6

# LaTeX command names, e.g. "\alpha", "\textbf", "\cite". PyMuPDF emits raw
# LaTeX and a command name is never a term the user asked about.
_LATEX_COMMAND = re.compile(r"\\[a-zA-Z]+")
# Math delimiters: $x_i$, $\sim$, \(...\), \[...\]
_MATH_DELIMS = re.compile(r"[${}]")
# A remaining backslash is a spacing macro such as "\\" (line break).
_BACKSLASH = re.compile(r"\\")
# A hyphen immediately before a newline is a line-break artefact, not part of the
# word. Must run BEFORE lowercasing so the [a-z] lookahead still works.
#
# The negative lookahead on the continuation is load-bearing: without it, a real
# compound that happens to break across a line is welded into one nonsense token.
# "state-\nof-the-art" became "stateof" and then reported "state" as missing from
# a passage that plainly contained it. The continuation must be a complete word,
# i.e. not followed by another hyphen or letter.
_HYPHEN_BREAK = re.compile(r"-\s*\n\s*([a-z]+)(?![-\w])")
# Everything that is not a lowercase ASCII letter or a digit is a separator.
# One rule handles punctuation, hyphens, underscores, soft hyphens, accents and
# ligatures. camelCase is deliberately NOT split: "LayerNorm" stays one token,
# because splitting it yields two terms the paper never uses.
_SEPARATORS = re.compile(r"[^a-z0-9]+")


def _tokens(text) -> List[str]:
    """Clean a chunk of scientific prose down to a list of raw tokens.

    Order is load-bearing: LaTeX and math are stripped before de-hyphenation,
    which must see the original line breaks, and de-hyphenation must happen
    before lowercasing so ``classifi-\\ncation`` becomes ``classification`` rather
    than two nonsense terms.

    Never raises: a non-string in, an empty list out.
    """
    if not isinstance(text, str) or not text:
        return []
    text = _LATEX_COMMAND.sub(" ", text)
    text = _MATH_DELIMS.sub(" ", text)
    text = _BACKSLASH.sub(" ", text)
    text = _HYPHEN_BREAK.sub(r"\1", text)
    text = text.lower()
    text = _SEPARATORS.sub(" ", text)
    return text.split()


def key_terms(text: Optional[str]) -> List[str]:
    """The distinctive terms of ``text``, in first-appearance order.

    Stopwords are removed, and so are two classes of term that carry no signal:

    * alphabetic tokens of one or two characters -- ``et``, ``al``, ``of`` and
      friends, plus bare math variables like ``n``, ``k``, ``x``;
    * bare integers shorter than four digits -- ``Figure 2`` contributes nothing,
      while ``2017`` and ``1000`` are the most distinctive thing a question can
      carry and are kept.

    Pure and deterministic. ``None`` and ``""`` both give ``[]``.
    """
    seen: Set[str] = set()
    terms: List[str] = []
    for token in _tokens(text):
        if token.isdigit():
            if len(token) < 4:
                continue
        elif len(token) <= 2:
            continue
        if token in STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _is_present(term: str, vocabulary: Set[str]) -> bool:
    """Is ``term`` (or a recognised morphological variant of it) in ``vocabulary``?"""
    if term in vocabulary:
        return True
    for suffix in _INFLECTIONAL_SUFFIXES:
        if term + suffix in vocabulary:
            return True
    if len(term) >= _MIN_TERM_FOR_DERIVATION:
        for suffix in _DERIVATIONAL_SUFFIXES:
            if term + suffix in vocabulary:
                return True
    return False


def coverage_report(
    question: Optional[str], passages: Optional[Iterable[Optional[str]]]
) -> Optional[dict]:
    """Which of the question's key terms appear in ``passages``.

    Parameters
    ----------
    question:
        The user's question, verbatim. ``None``/``""`` is safe.
    passages:
        The text the model was actually shown -- in this codebase the merged
        ``text`` of each citation group, *not* the raw retrieved chunks, because
        only the merged text reached the prompt. Non-string and ``None``
        elements are skipped rather than raising.

    Returns
    -------
    dict or None
        ``None`` when the readout would be misleading rather than informative:
        fewer than :data:`MIN_TERMS` distinctive terms in the question, or no
        usable passage text. Otherwise a dict with exactly these keys:

        ``total``    int, the true number of key terms (never capped).
        ``covered``  int, how many of them appear in the passages (never capped).
        ``terms``    the covered term names, question order, capped at
                     :data:`MAX_REPORTED`.
        ``missing``  the terms that did not appear, question order, capped at
                     :data:`MAX_REPORTED`.

    Pure and deterministic; mutates nothing it is given.
    """
    terms = key_terms(question)
    if len(terms) < MIN_TERMS:
        return None
    if not passages:
        return None

    vocabulary: Set[str] = set()
    for passage in passages:
        vocabulary.update(_tokens(passage))
    if not vocabulary:
        return None

    covered: List[str] = []
    missing: List[str] = []
    for term in terms:
        if _is_present(term, vocabulary):
            covered.append(term)
        else:
            missing.append(term)

    return {
        "total": len(terms),
        "covered": len(covered),
        "terms": covered[:MAX_REPORTED],
        "missing": missing[:MAX_REPORTED],
        # True when names were dropped for length. The UI must say so, because
        # an enumeration of 12 of 13 missing terms reads as complete otherwise.
        "truncated": len(covered) > MAX_REPORTED or len(missing) > MAX_REPORTED,
    }


def summarise_coverage(report: Optional[dict]) -> str:
    """One human-readable line for ``report``; ``""`` when there is nothing to say.

    The wording is part of the contract, and this is the function that PRODUCES
    the line the user reads -- it is shipped on the ``done`` event rather than
    re-derived in the client, so these tests guard the actual string.

    It states a lexical fact about the retrieved pages and never a judgement
    about the answer: no percentage, no "confidence", no "probability", no
    "score", and never "not in the paper".
    """
    if not report:
        return ""

    total = report.get("total") or 0
    covered = report.get("covered") or 0
    line = "covered %d of %d key terms" % (covered, total)

    terms = report.get("terms") or []
    if terms:
        line += ": " + ", ".join(str(term) for term in terms)
        if covered > len(terms):
            line += " and %d more" % (covered - len(terms))

    missing = report.get("missing") or []
    if missing:
        line += " - not found in the retrieved pages: " + ", ".join(
            str(term) for term in missing
        )
        named = total - covered - len(missing)
        if named > 0:
            line += " and %d more" % named
    return line
