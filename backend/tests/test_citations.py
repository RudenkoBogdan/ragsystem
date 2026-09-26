"""Executable contract tests for ``backend/chat/citations.py``.

This is the first *executed* proof in this repository of Milestone 1's core
claim: **the citation label shown to the model is the same label the user sees
in the source list.**

The module under test is deliberately pure and stdlib-only, so it is imported
directly -- no stubbing, no AST surgery, no fixtures, no network, no database.
Everything asserted below was read off the implementation and, where the
implementation and its own docstring disagree, the docstring's contract is
asserted instead so the discrepancy shows up as a red test rather than as
silent agreement (see :class:`DocumentedRegexContractTests`).

Run it with::

    # from the repository root
    python3 -m unittest discover -s backend/tests -t backend -v

    # or from inside backend/
    cd backend && python3 -m unittest discover -s tests -t . -v

    # or a single module
    python3 backend/tests/test_citations.py

Python 3.9 compatible, stdlib only.
"""

from __future__ import annotations

import ast
import copy
import os
import re
import sys
import unittest

# --- import bootstrap -------------------------------------------------------
# `discover -s backend/tests -t backend` imports this module as `tests.*`, so the
# package __init__ has already put backend/ on sys.path.  The fallback keeps a
# directly-executed module (where the package __init__ never runs) working too.
try:
    from chat.citations import (
        CITATION_PATTERN,
        abstention_message,
        build_chunks,
        build_sources,
        extract_citations,
        group_citations,
    )
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from chat.citations import (
        CITATION_PATTERN,
        abstention_message,
        build_chunks,
        build_sources,
        extract_citations,
        group_citations,
    )


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CITATIONS_PATH = os.path.join(BACKEND_DIR, "chat", "citations.py")


# --- shared fixtures --------------------------------------------------------

def chunk(arxiv_id, page, text, title=None, score=None):
    """One retrieved chunk, shaped like ``retrieve_context`` builds it."""
    return {
        "text": text,
        "title": title if title is not None else ("Paper %s" % arxiv_id),
        "arxiv_id": arxiv_id,
        "page": page,
        "score": score,
    }


#: The canonical wrong-page case: five retrieved chunks, of which #2 and #3 come
#: from the *same* (paper A, page 4).  Old code numbered the prompt over raw
#: chunks (1..5) while de-duplicating the source list (1..4) -- so every marker
#: after the duplicate pointed at the wrong page of the wrong paper.
DUPLICATE_PAGE_CHUNKS = [
    chunk("A", 3, "passage A p3", title="Paper A", score=0.91),
    chunk("A", 4, "passage A p4 first", title="Paper A", score=0.80),
    chunk("A", 4, "passage A p4 second", title="Paper A", score=0.55),
    chunk("B", 1, "passage B p1", title="Paper B", score=0.42),
    chunk("C", 2, "passage C p2", title="Paper C", score=0.10),
]


def render_prompt_blocks(groups):
    """Build prompt context blocks exactly the way ``service.build_system_prompt``
    does (``backend/chat/service.py:174-179``): one ``[label]`` block per group,
    joined by ``"\\n\\n---\\n\\n"``.

    ``test_prompt_mapping.py`` runs the *real* function through an AST loader;
    this helper exists so the label<->block invariant can also be asserted from
    this module, which must not import ``chat.service`` (it needs ``aiohttp``).
    """
    parts = [
        '[%s] Source: "%s", page %s\n%s'
        % (group["label"], group["title"], group["page"], group["text"])
        for group in groups
    ]
    return "\n\n---\n\n".join(parts)


#: Matches the ``[n]`` header each context block starts with.
BLOCK_HEADER = re.compile(r'(?m)^\[(\d+)\] Source: "([^"]*)", page (.*)$')


class GroupCitationsEmptyInputTests(unittest.TestCase):
    """Requirement 1: empty and ``None`` inputs."""

    def test_empty_list_returns_empty_list(self):
        self.assertEqual(group_citations([]), [])

    def test_none_is_tolerated(self):
        self.assertEqual(group_citations(None), [])

    def test_any_iterable_of_chunks_is_accepted(self):
        # The signature promises ``Iterable``, and retrieve_context returns a
        # list, but a generator must not break it either.
        groups = group_citations(iter([chunk("A", 1, "x")]))
        self.assertEqual([g["label"] for g in groups], [1])


class DuplicatePageRegressionTests(unittest.TestCase):
    """Requirement 2 -- the regression that matters most.

    Two chunks from the same ``(arxiv_id, page)`` must produce ONE label, and
    must not consume a number, so the labels after the duplicate still point at
    the right page of the right paper.
    """

    def setUp(self):
        self.groups = group_citations(DUPLICATE_PAGE_CHUNKS)

    def test_five_chunks_collapse_to_four_labelled_groups(self):
        # Four distinct (paper, page) pairs -> four labels, numbered 1..4.
        # The old, buggy numbering produced five.
        self.assertEqual([g["label"] for g in self.groups], [1, 2, 3, 4])
        self.assertEqual(len(self.groups), 4)
        self.assertNotEqual([g["label"] for g in self.groups], [1, 2, 3, 4, 5])

    def test_labels_are_not_the_raw_chunk_indexes(self):
        # Explicit guard against a "one label per chunk" regression.
        self.assertLess(len(self.groups), len(DUPLICATE_PAGE_CHUNKS))

    def test_duplicate_page_chunks_merge_into_label_two(self):
        merged = self.groups[1]
        self.assertEqual(merged["label"], 2)
        self.assertEqual(merged["arxiv_id"], "A")
        self.assertEqual(merged["page"], 4)
        self.assertEqual(merged["chunk_count"], 2)

    def test_merged_text_keeps_both_passages_in_retrieval_order(self):
        merged_text = self.groups[1]["text"]
        # Nothing dropped, nothing invented, retrieval order preserved.
        self.assertEqual(merged_text, "passage A p4 first\n\npassage A p4 second")
        self.assertEqual(merged_text.count("passage A p4"), 2)
        self.assertNotIn("passage A p3", merged_text)

    def test_group_counts_sum_back_to_the_number_of_input_chunks(self):
        self.assertEqual(
            sum(g["chunk_count"] for g in self.groups), len(DUPLICATE_PAGE_CHUNKS)
        )

    def test_labels_after_the_duplicate_still_point_at_the_right_page(self):
        by_label = {g["label"]: g for g in self.groups}
        self.assertEqual((by_label[1]["arxiv_id"], by_label[1]["page"]), ("A", 3))
        self.assertEqual((by_label[2]["arxiv_id"], by_label[2]["page"]), ("A", 4))
        self.assertEqual((by_label[3]["arxiv_id"], by_label[3]["page"]), ("B", 1))
        self.assertEqual((by_label[4]["arxiv_id"], by_label[4]["page"]), ("C", 2))

    def test_five_chunks_with_two_duplicate_pairs_yield_labels_1_2_3(self):
        # Same regression, arranged so the expected labels are literally 1,2,3
        # rather than 1,2,3,4: two of the five chunks are duplicates.
        chunks = [
            chunk("A", 3, "a p3"),
            chunk("A", 4, "a p4 first"),
            chunk("A", 4, "a p4 second"),
            chunk("B", 1, "b p1 first"),
            chunk("B", 1, "b p1 second"),
        ]
        groups = group_citations(chunks)
        self.assertEqual([g["label"] for g in groups], [1, 2, 3])
        self.assertEqual([g["chunk_count"] for g in groups], [1, 2, 2])
        self.assertEqual(
            [g["text"] for g in groups],
            ["a p3", "a p4 first\n\na p4 second", "b p1 first\n\nb p1 second"],
        )

    def test_old_numbering_would_have_drifted_and_new_numbering_does_not(self):
        """The bug Milestone 1 fixed, stated as an executable fact.

        OLD: the prompt was numbered over raw chunks while the sources list was
        de-duplicated, so a marker the model emitted for the 4th chunk (B p1)
        read ``[4]`` -- but the client's 4th source entry was C p2.  Every chunk
        after the duplicate pointed at the wrong page, and the 5th chunk's marker
        ``[5]`` had no source at all.
        NEW: one numbering, shared by prompt and sources, so they cannot drift.
        """
        chunks = DUPLICATE_PAGE_CHUNKS

        # --- what the old code did ------------------------------------------
        # The prompt was numbered over raw chunks, so chunk i carried the
        # marker [i + 1] regardless of whether it duplicated an earlier page.
        old_source_position = {}  # chunk -> position in the de-duplicated list
        seen = set()
        for i, c in enumerate(chunks):
            key = (c["arxiv_id"], c["page"])
            if key in seen:
                continue
            seen.add(key)
            old_source_position[i] = len(old_source_position) + 1

        drifted = [
            i
            for i in range(len(chunks))
            if (i + 1) != old_source_position.get(i)
        ]
        # Chunks 0 and 1 line up; the duplicate (2) and everything after it
        # (3, 4) do not -- three of five citations pointed at the wrong page.
        self.assertEqual(drifted, [2, 3, 4])

        # --- what the new code does ----------------------------------------
        groups = group_citations(chunks)
        new_label_of_position = [g["label"] for g in groups]
        self.assertEqual(
            new_label_of_position, list(range(1, len(groups) + 1))
        )
        for i, c in enumerate(chunks):
            position = next(
                n
                for n, g in enumerate(groups)
                if g["arxiv_id"] == c["arxiv_id"] and g["page"] == c["page"]
            )
            # The number the model is shown equals the source-list index, always.
            self.assertEqual(position + 1, new_label_of_position[position])


class GroupCitationsLabelContractTests(unittest.TestCase):
    """Requirement 3: labels are 1-based, sequential, gap-free, first-appearance."""

    def _chunks(self):
        return [
            chunk("A", 1, "a1"),
            chunk("B", 1, "b1"),
            chunk("A", 2, "a2"),
            chunk("C", 1, "c1"),
            chunk("B", 1, "b1 again"),
            chunk("A", 1, "a1 again"),
            chunk("D", 5, "d5"),
        ]

    def test_labels_are_one_based_sequential_and_gap_free(self):
        labels = [g["label"] for g in group_citations(self._chunks())]
        self.assertEqual(labels, list(range(1, len(labels) + 1)))
        self.assertEqual(labels[0], 1)
        self.assertEqual(labels, sorted(set(labels)))

    def test_labels_follow_first_appearance_order(self):
        groups = group_citations(self._chunks())
        self.assertEqual(
            [(g["arxiv_id"], g["page"]) for g in groups],
            [("A", 1), ("B", 1), ("A", 2), ("C", 1), ("D", 5)],
        )

    def test_reappearing_page_keeps_its_original_label_and_gains_chunks(self):
        groups = group_citations(self._chunks())
        first = groups[0]
        self.assertEqual(first["label"], 1)
        self.assertEqual(first["page"], 1)
        self.assertEqual(first["chunk_count"], 2)
        self.assertEqual(first["text"], "a1\n\na1 again")

    def test_group_has_exactly_the_documented_keys(self):
        group = group_citations(self._chunks())[0]
        self.assertEqual(
            sorted(group),
            ["arxiv_id", "chunk_count", "label", "page", "score", "text", "title"],
        )

    def test_title_comes_from_the_first_chunk_of_the_group(self):
        chunks = [
            chunk("A", 1, "first", title="Original Title"),
            chunk("A", 1, "second", title="Stale Title"),
        ]
        self.assertEqual(group_citations(chunks)[0]["title"], "Original Title")

    def test_missing_title_becomes_empty_string(self):
        groups = group_citations([{"text": "x", "arxiv_id": "A", "page": 1}])
        self.assertEqual(groups[0]["title"], "")
        groups = group_citations([{"text": "x", "title": None, "arxiv_id": "A", "page": 1}])
        self.assertEqual(groups[0]["title"], "")


class GroupCitationsScoreTests(unittest.TestCase):
    """Requirement 4: the group score is the FIRST chunk's score."""

    def test_score_is_the_first_most_relevant_chunk_score(self):
        groups = group_citations(DUPLICATE_PAGE_CHUNKS)
        self.assertEqual(groups[1]["score"], 0.80)  # first of the merged pair
        self.assertEqual(groups[0]["score"], 0.91)
        self.assertEqual(groups[3]["score"], 0.10)

    def test_score_is_none_when_input_chunks_carry_no_score(self):
        groups = group_citations([chunk("A", 1, "x", score=None)])
        self.assertIsNone(groups[0]["score"])

    def test_score_is_none_when_the_key_is_absent_entirely(self):
        groups = group_citations([{"text": "x", "title": "t", "arxiv_id": "A", "page": 1}])
        self.assertIsNone(groups[0]["score"])

    def test_unscoreable_score_does_not_break_the_prompt(self):
        # _coerce_score swallows TypeError/ValueError rather than raising.
        for bad in ("n/a", object(), [1, 2]):
            groups = group_citations([chunk("A", 1, "x", score=bad)])
            self.assertIsNone(groups[0]["score"])

    def test_numeric_score_is_coerced_to_float(self):
        self.assertEqual(group_citations([chunk("A", 1, "x", score=1)])[0]["score"], 1.0)
        self.assertEqual(
            group_citations([chunk("A", 1, "x", score="0.25")])[0]["score"], 0.25
        )

    def test_merged_group_ignores_the_scores_of_later_chunks(self):
        chunks = [chunk("A", 1, "first", score=0.9), chunk("A", 1, "second", score=0.1)]
        self.assertEqual(group_citations(chunks)[0]["score"], 0.9)


class GroupCitationsPurityTests(unittest.TestCase):
    """Requirement 5: nothing passed in is mutated."""

    def test_input_list_and_dicts_are_not_mutated(self):
        chunks = [dict(c) for c in DUPLICATE_PAGE_CHUNKS]
        before = copy.deepcopy(chunks)
        group_citations(chunks)
        self.assertEqual(chunks, before)

    def test_nested_values_are_not_aliased_into_the_result(self):
        # The group's `text` is a fresh join, never a reference into a chunk.
        chunks = [chunk("A", 1, "payload")]
        groups = group_citations(chunks)
        groups[0]["text"] += " MUTATED"
        self.assertEqual(chunks[0]["text"], "payload")

    def test_repeated_calls_are_deterministic(self):
        first = group_citations(DUPLICATE_PAGE_CHUNKS)
        second = group_citations(DUPLICATE_PAGE_CHUNKS)
        self.assertEqual(first, second)

    def test_two_calls_return_distinct_objects(self):
        a = group_citations(DUPLICATE_PAGE_CHUNKS)
        b = group_citations(DUPLICATE_PAGE_CHUNKS)
        self.assertEqual(a, b)
        self.assertIsNot(a, b)
        self.assertIsNot(a[0], b[0])


class GroupCitationsToleranceTests(unittest.TestCase):
    """Requirement 6: degenerate metadata must not raise."""

    def test_none_text_does_not_raise(self):
        groups = group_citations([chunk("A", 1, None)])
        self.assertEqual(groups[0]["text"], "")
        self.assertEqual(groups[0]["chunk_count"], 1)

    def test_none_text_merges_as_an_empty_part(self):
        chunks = [chunk("A", 1, None), chunk("A", 1, "real text")]
        merged = group_citations(chunks)[0]
        self.assertEqual(merged["text"], "\n\nreal text")
        self.assertEqual(merged["chunk_count"], 2)

    def test_missing_keys_do_not_raise(self):
        groups = group_citations([{}])
        self.assertEqual(groups[0]["arxiv_id"], None)
        self.assertEqual(groups[0]["page"], None)
        self.assertEqual(groups[0]["text"], "")
        self.assertEqual(groups[0]["title"], "")

    def test_none_page_and_arxiv_id_are_preserved_as_none(self):
        groups = group_citations([{"text": "a", "page": None, "arxiv_id": None}])
        self.assertIsNone(groups[0]["page"])
        self.assertIsNone(groups[0]["arxiv_id"])

    def test_chunks_with_no_page_and_no_arxiv_id_collapse_into_one_group(self):
        # Documented consequence of keying on (arxiv_id, page): two metadata-less
        # chunks are the same "page" as far as grouping is concerned.
        chunks = [{"text": "one"}, {"text": "two"}]
        groups = group_citations(chunks)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["chunk_count"], 2)
        self.assertEqual(groups[0]["text"], "one\n\ntwo")

    def test_same_page_number_on_different_papers_is_a_separate_group(self):
        groups = group_citations([chunk("A", 1, "a1"), chunk("B", 1, "b1")])
        self.assertEqual([g["label"] for g in groups], [1, 2])

    def test_same_paper_different_pages_is_a_separate_group(self):
        groups = group_citations([chunk("A", 1, "a1"), chunk("A", 2, "a2")])
        self.assertEqual([g["label"] for g in groups], [1, 2])

    def test_page_zero_and_negative_pages_are_kept(self):
        groups = group_citations([chunk("A", 0, "a0"), chunk("A", -1, "a-1")])
        self.assertEqual([g["page"] for g in groups], [0, -1])


class ExtractCitationsTests(unittest.TestCase):
    """Requirement 7: reading the finished answer back."""

    def test_valid_markers_are_extracted_in_first_appearance_order(self):
        cited, unresolved = extract_citations("First [3], then [1], finally [2].", [1, 2, 3])
        self.assertEqual(cited, [3, 1, 2])
        self.assertEqual(unresolved, [])

    def test_repeated_markers_are_reported_once(self):
        cited, unresolved = extract_citations("[1] and [1] and [1]", [1])
        self.assertEqual(cited, [1])
        self.assertEqual(unresolved, [])

    def test_fabricated_markers_are_unresolved_sorted_ascending(self):
        cited, unresolved = extract_citations("Claim [9] and [4] and [7].", [1, 2, 3])
        self.assertEqual(cited, [])
        self.assertEqual(unresolved, [4, 7, 9])

    def test_mixed_answer_splits_across_the_two_lists(self):
        cited, unresolved = extract_citations(
            "Real [1] and [3], but [42] and [7] were invented.", [1, 2, 3]
        )
        self.assertEqual(cited, [1, 3])
        self.assertEqual(unresolved, [7, 42])

    def test_none_text_is_safe(self):
        self.assertEqual(extract_citations(None, [1, 2]), ([], []))

    def test_none_valid_labels_is_safe(self):
        self.assertEqual(extract_citations("cites [1] then [2]", None), ([], [1, 2]))

    def test_empty_valid_labels_is_safe(self):
        self.assertEqual(extract_citations("cites [1]", []), ([], [1]))

    def test_empty_answer_cites_nothing(self):
        self.assertEqual(extract_citations("no markers at all", [1, 2, 3]), ([], []))

    def test_whitespace_inside_the_brackets_is_tolerated(self):
        cited, _ = extract_citations("see [ 3 ] and [  1  ]", [1, 2, 3])
        self.assertEqual(cited, [3, 1])

    def test_non_numeric_brackets_are_ignored(self):
        for text in ("[abc]", "[]", "[ ]", "[x]", "[1.5]", "[--]", "[1,]"):
            self.assertEqual(
                extract_citations(text, [1, 2, 3]),
                ([], []),
                "%r must not be read as a citation" % text,
            )

    def test_markdown_link_is_not_misread_as_a_citation(self):
        self.assertEqual(extract_citations("[1](http://example.com/a)", [1]), ([], []))

    def test_sequence_indexing_is_not_misread_as_a_citation(self):
        self.assertEqual(extract_citations("arr[1] and a[0] and b[12]", [1, 12]), ([], []))

    def test_parenthesised_marker_is_ignored(self):
        self.assertEqual(extract_citations("([1])", [1]), ([], []))

    def test_image_syntax_prefix_is_ignored(self):
        self.assertEqual(extract_citations("![fig](fig.png)", [1, 2]), ([], []))

    def test_doubled_brackets_resolve_to_the_inner_label(self):
        # Documented consequence: [[1]] is a markdown rendering artifact, and the
        # inner [1] is the real marker -- it must NOT be reported as fabricated.
        cited, unresolved = extract_citations("Rendered as [[1]] here.", [1])
        self.assertEqual(cited, [1])
        self.assertEqual(unresolved, [])

    def test_huge_marker_is_reported_unresolved_not_silently_dropped(self):
        cited, unresolved = extract_citations("A claim [123456].", [1, 2, 3])
        self.assertEqual(cited, [])
        self.assertEqual(unresolved, [123456])

    def test_huge_marker_is_never_truncated_into_a_valid_label(self):
        # Greedy \d+ means [123456] cannot degrade into 1, 12 or 1234, any of
        # which would otherwise resolve and silently attribute the claim to the
        # wrong source.
        cited, unresolved = extract_citations("A claim [123456].", [1, 12, 1234])
        self.assertEqual(cited, [])
        self.assertEqual(unresolved, [123456])

    def test_markers_across_newlines_are_found(self):
        cited, _ = extract_citations("line one [1]\nline two [2]\n", [1, 2])
        self.assertEqual(cited, [1, 2])

    def test_valid_labels_accepts_a_set_or_a_tuple(self):
        self.assertEqual(extract_citations("[1] [2]", {1, 2})[0], [1, 2])
        self.assertEqual(extract_citations("[1] [2]", (1, 2))[0], [1, 2])

    def test_valid_labels_accepts_the_labels_of_group_citations_output(self):
        groups = group_citations(DUPLICATE_PAGE_CHUNKS)
        labels = [g["label"] for g in groups]
        cited, unresolved = extract_citations("Answer [1][2][3][4].", labels)
        self.assertEqual(cited, [1, 2, 3, 4])
        self.assertEqual(unresolved, [])


class DocumentedRegexContractTests(unittest.TestCase):
    """Asserts the contract the module *documents*, not just what it does.

    ``backend/chat/citations.py:55`` states::

        * ``arr[1]``, ``![alt][2]``  -> no match (not a citation)

    and the guard comment at ``citations.py:43`` says ``(?<!\\!)`` exists to "do
    not match the '!' image syntax".  The suite pins the *documented* behaviour
    rather than whatever the current regex happens to do, so a drift between the
    comment and the code shows up here as a failure instead of as silent
    agreement.

    These tests were written RED, and that is how a real bug was found: the
    original single-branch pattern only rejected a ``[`` whose *immediately*
    preceding character was ``!``, so a reference-style image ``![alt][2]`` --
    where the ``!`` is further away -- still matched and was reported as a
    citation of label 2.  The client would then highlight source 2 as "cited"
    although the model never cited it.  ``re`` has no variable-length
    lookbehind, so the fix consumes the whole construct with a leading
    alternation branch instead of trying to look behind at it (see
    ``citations.py:33-40`` and the pattern at ``citations.py:75-78``).

    The production code has since been fixed, so these tests are GREEN and
    **must stay green**: they are the regression guard for that fix.  A failure
    here means the reference-style-image case has regressed; it is never an
    accepted or expected failure.
    """

    def test_reference_style_image_is_not_a_citation(self):
        cited, unresolved = extract_citations("See ![alt][2] for the diagram.", [1, 2, 3])
        self.assertEqual(
            cited, [], "a reference-style image must not count as a citation"
        )
        self.assertEqual(unresolved, [])

    def test_reference_style_image_link_is_not_a_citation(self):
        # Same syntax with a definition-style target, e.g. "[2]: fig.png".
        cited, unresolved = extract_citations("![alt][2] and [2]: fig.png", [1, 2, 3])
        self.assertEqual(cited, [])
        self.assertEqual(unresolved, [])


class EndToEndInvariantTests(unittest.TestCase):
    """Requirement 8: the number in the prompt == the label on the source.

    This is the whole point of Milestone 1.  ``test_prompt_mapping.py`` runs the
    real ``build_system_prompt``; here the same invariant is asserted directly
    against ``group_citations`` output for several inputs, including the
    duplicate-page regression.
    """

    CASES = {
        "duplicate_page": DUPLICATE_PAGE_CHUNKS,
        "single_chunk": [chunk("A", 1, "only passage")],
        "all_unique": [chunk("A", 1, "a1"), chunk("B", 7, "b7"), chunk("C", 2, "c2")],
        "all_same_page": [chunk("A", 1, "a"), chunk("A", 1, "b"), chunk("A", 1, "c")],
        "empty": [],
        "page_returns_later": [
            chunk("A", 1, "a1"),
            chunk("B", 1, "b1"),
            chunk("A", 1, "a1 again"),
            chunk("A", 3, "a3"),
        ],
    }

    def test_every_prompt_block_number_equals_its_source_label(self):
        for name, chunks in self.CASES.items():
            with self.subTest(case=name):
                groups = group_citations(chunks)
                headers = BLOCK_HEADER.findall(render_prompt_blocks(groups))
                self.assertEqual(len(headers), len(groups))
                for index, (number, title, page) in enumerate(headers):
                    group = groups[index]
                    self.assertEqual(
                        int(number),
                        group["label"],
                        "block [%s] must be source label %s" % (number, group["label"]),
                    )
                    self.assertEqual(int(number), index + 1)
                    self.assertEqual(title, group["title"])
                    self.assertEqual(page, str(group["page"]))

    def test_number_of_prompt_blocks_equals_number_of_sources(self):
        for name, chunks in self.CASES.items():
            with self.subTest(case=name):
                groups = group_citations(chunks)
                blocks = render_prompt_blocks(groups)
                sources = [
                    {"label": g["label"], "page": g["page"], "arxiv_id": g["arxiv_id"]}
                    for g in groups
                ]
                self.assertEqual(
                    [int(n) for n, _t, _p in BLOCK_HEADER.findall(blocks)],
                    [s["label"] for s in sources],
                )

    def test_every_label_the_model_can_emit_resolves_to_a_source(self):
        for name, chunks in self.CASES.items():
            if not chunks:
                continue
            with self.subTest(case=name):
                groups = group_citations(chunks)
                labels = [g["label"] for g in groups]
                # The model can only ever see, and therefore cite, 1..N.
                answer = " ".join("[%d]" % n for n in range(1, len(labels) + 1))
                cited, unresolved = extract_citations(answer, labels)
                self.assertEqual(cited, labels)
                self.assertEqual(unresolved, [])

    def test_duplicate_page_case_marks_the_same_page_twice_but_lists_it_once(self):
        groups = group_citations(DUPLICATE_PAGE_CHUNKS)
        labels = [g["label"] for g in groups]
        # The merged page is reachable by exactly one label...
        self.assertEqual(labels.count(2), 1)
        # ...and both of its passages are inside that one block.
        block_two = render_prompt_blocks(groups).split("\n\n---\n\n")[1]
        self.assertIn("passage A p4 first", block_two)
        self.assertIn("passage A p4 second", block_two)
        self.assertTrue(block_two.startswith("[2] Source:"))


class ModuleHygieneTests(unittest.TestCase):
    """Requirement 3 of the validation plan: prove the suite needs no packages.

    Stated by inspection of the AST -- nothing is installed to make this pass.
    If someone later adds ``import chromadb`` to the module under test (or to a
    test), these fail immediately and loudly instead of at import time in CI.
    """

    ALLOWED_STDLIB = frozenset(
        [
            "__future__",
            "ast",
            # Added for `test_abstention.py`, which drives the real async
            # `stream_rag_response` with a throwaway event loop. It is stdlib,
            # needs no install and no network; the alternative was leaving the
            # feature's headline claim ("no LLM call when retrieval is empty")
            # with no executable coverage at all.
            "asyncio",
            # The rest were added, pre-emptively and in one go, by the
            # security-hardening series so that the modules it introduces
            # (`security/policy.py`, `security/logging_setup.py`, the
            # outbound-host guard, the auth rate limiter) can all be written
            # and tested without anyone having to come back and widen this
            # list again. They are all stdlib, so widening it costs nothing
            # in the property this class actually protects: no test may
            # require an installed third-party package to run.
            "collections",
            "copy",
            "functools",
            "ipaddress",
            "json",
            "logging",
            # `security/ratelimit.py` needs `math.ceil` for its retry-after
            # rounding and `threading.Lock` around the per-key deques. The
            # pre-emptive widening above was written before that module
            # existed, so its two names were simply not predicted; they are
            # stdlib, and adding them is what lets the hygiene case below
            # assert the *whole* module instead of a subset of it.
            "math",
            "os",
            "pathlib",
            "re",
            "socket",
            "sys",
            "threading",
            "time",
            "typing",
            "unittest",
            "urllib",
        ]
    )

    #: First-party modules of this repo that the suite is allowed to import.
    ALLOWED_FIRST_PARTY = frozenset(["chat", "security", "tests"])

    def _imported_modules(self, path):
        """Every fully-qualified module name imported by ``path`` (AST, not text)."""
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
        return names

    def _top_level_imports(self, path):
        return {name.split(".")[0] for name in self._imported_modules(path)}

    def test_citations_module_imports_only_the_allowed_stdlib(self):
        self.assertTrue(
            self._top_level_imports(CITATIONS_PATH) <= self.ALLOWED_STDLIB,
            "backend/chat/citations.py must stay stdlib-only",
        )

    def test_test_package_imports_only_stdlib_or_first_party(self):
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        allowed = self.ALLOWED_STDLIB | self.ALLOWED_FIRST_PARTY
        for name in sorted(os.listdir(tests_dir)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(tests_dir, name)
            with self.subTest(module=name):
                unexpected = sorted(self._top_level_imports(path) - allowed)
                self.assertEqual(
                    unexpected, [], "%s must not import a third-party package" % name
                )

    def test_suite_does_not_import_chat_service(self):
        # chat.service imports aiohttp at module scope and is unimportable here;
        # test_prompt_mapping.py lifts build_system_prompt out of the source
        # with ast instead of importing the module.
        tests_dir = os.path.dirname(os.path.abspath(__file__))
        for name in sorted(os.listdir(tests_dir)):
            if not name.endswith(".py"):
                continue
            path = os.path.join(tests_dir, name)
            with self.subTest(module=name):
                forbidden = sorted(
                    m
                    for m in self._imported_modules(path)
                    if m == "chat.service" or m.startswith("chat.service.")
                )
                self.assertEqual(forbidden, [], "%s must not import chat.service" % name)


class BuildChunksTests(unittest.TestCase):
    """The Chroma-result -> chunk-dict projection, which used to be untested.

    This lived inline in ``retrieve_context`` inside ``chat/service.py``, a module
    that cannot be imported here. It is the one hand-written link between the
    vector store and everything the citation layer guarantees, so it is now a
    pure function with tests.
    """

    def test_fully_populated_result(self):
        chunks = build_chunks(
            ["passage text"],
            [{"title": "Paper A", "arxiv_id": "2301.07041", "page": 3}],
            [0.29],
        )
        self.assertEqual(
            chunks,
            [
                {
                    "text": "passage text",
                    "title": "Paper A",
                    "arxiv_id": "2301.07041",
                    "page": 3,
                    "score": 0.71,
                }
            ],
        )

    def test_chunk_has_exactly_the_documented_keys(self):
        chunk = build_chunks(["t"], [{}], [0.0])[0]
        self.assertEqual(
            sorted(chunk), ["arxiv_id", "page", "score", "text", "title"]
        )

    def test_distance_is_converted_to_similarity(self):
        self.assertEqual(build_chunks(["t"], [{}], [0.0])[0]["score"], 1.0)
        self.assertEqual(build_chunks(["t"], [{}], [0.5])[0]["score"], 0.5)

    def test_numeric_string_distance_is_coerced(self):
        self.assertEqual(build_chunks(["t"], [{}], ["0.25"])[0]["score"], 0.75)

    def test_uncoercible_distance_yields_no_score(self):
        self.assertIsNone(build_chunks(["t"], [{}], ["not a number"])[0]["score"])
        self.assertIsNone(build_chunks(["t"], [{}], [object()])[0]["score"])

    def test_absent_distance_yields_no_score(self):
        self.assertIsNone(build_chunks(["t"], [{}], [None])[0]["score"])
        self.assertIsNone(build_chunks(["t"], [{}], [])[0]["score"])

    def test_missing_lists_do_not_raise(self):
        # The exact shape a Chroma version returns when a `where` filter matches
        # nothing. Indexing these directly raised TypeError mid-stream, which
        # meant no `done` event and a permanently dead composer on the client.
        self.assertEqual(build_chunks(None, None, None), [])
        self.assertEqual(build_chunks([], [], []), [])

    def test_ragged_metadata_does_not_raise(self):
        chunks = build_chunks(["a", "b"], [{"title": "T"}], [])
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[1]["title"], "")
        self.assertIsNone(chunks[1]["arxiv_id"])
        self.assertIsNone(chunks[1]["page"])
        self.assertIsNone(chunks[1]["score"])

    def test_metadata_row_that_is_not_a_dict_is_tolerated(self):
        for bad in (None, "text", 7, ["x"]):
            with self.subTest(bad=type(bad).__name__):
                self.assertEqual(build_chunks(["a"], [bad], [0.1])[0]["title"], "")

    def test_missing_title_key_becomes_empty_string(self):
        # A collection written before a metadata key existed must not 500 on the
        # very first scoped query that happens to hit it.
        self.assertEqual(build_chunks(["a"], [{"arxiv_id": "X"}], [0.1])[0]["title"], "")

    def test_inputs_are_not_mutated(self):
        documents = ["a"]
        metadatas = [{"title": "T"}]
        before = (list(documents), [dict(m) for m in metadatas])
        build_chunks(documents, metadatas, [0.1])
        self.assertEqual((documents, metadatas), before)

    def test_two_calls_return_distinct_objects(self):
        a = build_chunks(["a"], [{}], [0.1])
        b = build_chunks(["a"], [{}], [0.1])
        self.assertEqual(a, b)
        self.assertIsNot(a, b)
        self.assertIsNot(a[0], b[0])


class BuildSourcesTests(unittest.TestCase):
    """The groups -> client ``sources`` payload projection."""

    def test_source_has_exactly_the_documented_keys(self):
        source = build_sources(group_citations(DUPLICATE_PAGE_CHUNKS))[0]
        self.assertEqual(
            sorted(source),
            [
                "arxiv_id",
                "chunk_count",
                "label",
                "page",
                "score",
                "snippet",
                "title",
            ],
        )

    def test_one_source_per_group_in_group_order(self):
        groups = group_citations(DUPLICATE_PAGE_CHUNKS)
        sources = build_sources(groups)
        self.assertEqual(len(sources), len(groups))
        self.assertEqual([s["label"] for s in sources], [g["label"] for g in groups])
        self.assertEqual([s["page"] for s in sources], [g["page"] for g in groups])

    def test_labels_are_authoritative_never_recomputed(self):
        # The label the model was shown is the one the client renders. Recomputing
        # it here as `index + 1` is exactly the drift this project already fixed
        # once, so the projection copies it instead.
        groups = group_citations(DUPLICATE_PAGE_CHUNKS)
        for index, source in enumerate(build_sources(groups)):
            self.assertEqual(source["label"], groups[index]["label"])

    def test_snippet_is_the_leading_text_of_the_group(self):
        groups = group_citations(DUPLICATE_PAGE_CHUNKS)
        for group, source in zip(groups, build_sources(groups)):
            self.assertEqual(source["snippet"], group["text"][:400])

    def test_snippet_is_truncated_to_the_default_length(self):
        groups = group_citations([chunk("A", 1, "x" * 1000)])
        self.assertEqual(len(build_sources(groups)[0]["snippet"]), 400)

    def test_snippet_length_is_overridable(self):
        groups = group_citations([chunk("A", 1, "x" * 1000)])
        self.assertEqual(len(build_sources(groups, snippet_chars=50)[0]["snippet"]), 50)

    def test_empty_input_gives_no_sources(self):
        self.assertEqual(build_sources([]), [])
        self.assertEqual(build_sources(None), [])
        self.assertEqual(build_sources(group_citations([])), [])

    def test_non_dict_group_is_skipped(self):
        self.assertEqual(build_sources([None, "x", {"label": 1}]), [{"label": 1, "title": "", "arxiv_id": None, "page": None, "snippet": "", "score": None, "chunk_count": None}])

    def test_two_calls_return_deeply_equal_payloads(self):
        groups = group_citations(DUPLICATE_PAGE_CHUNKS)
        self.assertEqual(build_sources(groups), build_sources(groups))


class AbstentionMessageTests(unittest.TestCase):
    """The refusal, when retrieval found nothing. App-authored, never prompted."""

    def test_unscoped_refusal_names_the_library(self):
        message = abstention_message(None)
        self.assertIn("library", message)
        self.assertTrue(message.strip().endswith("."))

    def test_scoped_refusal_names_the_papers_searched(self):
        message = abstention_message({"paper_titles": ["Attention Is All You Need", "BERT"]})
        self.assertIn("Attention Is All You Need", message)
        self.assertIn("BERT", message)

    def test_neither_variant_permits_an_unsourced_answer(self):
        # The phrase the old empty-library prompt used to license. Asserted as a
        # literal so the regression cannot come back in a reworded form.
        for scope in (None, {}, {"paper_titles": ["A"]}, {"paper_titles": []}):
            with self.subTest(scope=scope):
                self.assertNotIn("general knowledge", abstention_message(scope))

    def test_no_variant_contains_a_citation_marker(self):
        # The refusal is the one piece of assistant text with no sources. A "[1]"
        # in it would be read by the client as a claim about a source.
        for scope in (None, {"paper_titles": ["A"]}):
            with self.subTest(scope=scope):
                self.assertNotIn("[1]", abstention_message(scope))

    def test_missing_or_empty_scope_falls_back_to_the_library_wording(self):
        for scope in ({}, {"paper_titles": []}, {"paper_titles": None}, {"applied": True}):
            with self.subTest(scope=scope):
                self.assertIn("library", abstention_message(scope))

    def test_garbage_scope_never_raises(self):
        for scope in ("not a dict", 7, [], {"paper_titles": "Attention"}, {"paper_titles": 3}):
            with self.subTest(scope=repr(scope)):
                self.assertTrue(abstention_message(scope).strip())

    def test_blank_and_non_string_titles_are_dropped(self):
        # Every requested id can be another user's or already deleted, leaving
        # nothing to name. The message must degrade, not render an empty list.
        message = abstention_message({"paper_titles": [None, 5, "   ", "Real Paper"]})
        self.assertIn("Real Paper", message)
        self.assertNotIn(", )", message)

    def test_scope_naming_nothing_does_not_leak_an_empty_artifact(self):
        message = abstention_message({"applied": True, "paper_ids": [1, 2], "paper_titles": []})
        self.assertIn("library", message)
        self.assertNotIn("()", message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
