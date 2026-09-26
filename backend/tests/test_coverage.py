"""Contract tests for ``backend/chat/coverage.py`` -- the retrieval-coverage readout.

Same ground rules as its sibling ``test_citations.py``: stdlib ``unittest`` only,
no install, no network, no database, no third-party package. That is not an
aspiration here -- ``ModuleHygieneTests`` in ``test_citations.py`` fails the whole
suite if this file ever imports anything outside the allow-list.

What is being pinned is deliberately narrow. This readout is a *lexical fact*
about the retrieved pages, and the whole reason it is allowed on screen is that
it makes no claim it cannot back up. So the tests assert three things above all:

1. the tokeniser survives real arXiv/ML prose (LaTeX, math, hyphenation,
   ``et al.``, figure numbers, camelCase);
2. it stays silent rather than producing a flattering number when there is too
   little to measure;
3. ``summarise_coverage`` can never emit a percentage, a confidence, a
   probability or a score -- the vocabulary of the ungrounded claims this feature
   exists to replace.

Run it with::

    python3 -m unittest discover -s backend/tests -t backend -v
"""

from __future__ import annotations

import os
import sys
import unittest

try:
    from chat.coverage import (
        MAX_REPORTED,
        MIN_TERMS,
        STOPWORDS,
        coverage_report,
        key_terms,
        summarise_coverage,
    )
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from chat.coverage import (
        MAX_REPORTED,
        MIN_TERMS,
        STOPWORDS,
        coverage_report,
        key_terms,
        summarise_coverage,
    )


#: Terms the readout is worthless without. If any of these is stop-worded out,
#: every real ML question silently loses its most distinctive words and the
#: coverage collapses toward 100%.
MUST_KEEP = (
    "attention", "transformer", "encoder", "decoder", "embedding", "embeddings",
    "learning", "training", "neural", "network", "gradient", "token", "tokens",
    "sequence", "data", "loss", "layer", "function", "value", "result",
    "results", "performance", "accuracy", "task", "problem", "state", "space",
    "softmax", "lora", "rlhf",
)

#: Scholarly apparatus that must not survive into the denominator. "et al." is
#: the one that actually bites: it is in almost every question and in almost no
#: passage's distinctive vocabulary.
MUST_DROP = ("et", "al", "eg", "ie", "vs", "cf", "fig", "figure", "table", "eq", "ref")


class StopWordPolicyTests(unittest.TestCase):
    """The stop-word list is a product decision, so it is pinned like one."""

    def test_ml_terms_are_not_stopwords(self):
        for term in MUST_KEEP:
            with self.subTest(term=term):
                self.assertNotIn(term, STOPWORDS)

    def test_scholarly_apparatus_is_stopworded(self):
        for term in MUST_DROP:
            with self.subTest(term=term):
                self.assertIn(term, STOPWORDS)

    def test_generic_research_nouns_are_stopworded(self):
        # These appear in essentially every paper, so they are trivially
        # "covered" and would inflate the readout with noise.
        for term in ("model", "models", "method", "methods", "paper", "papers",
                     "work", "works", "study", "approach", "sota", "art"):
            with self.subTest(term=term):
                self.assertIn(term, STOPWORDS)

    def test_closed_class_english_is_stopworded(self):
        for term in ("the", "a", "an", "of", "in", "is", "does", "how", "what",
                     "between", "however", "although", "because"):
            with self.subTest(term=term):
                self.assertIn(term, STOPWORDS)


class KeyTermsTokenisationTests(unittest.TestCase):
    """The tokeniser is judged against the prose it will actually see."""

    def test_none_and_empty_give_no_terms(self):
        self.assertEqual(key_terms(None), [])
        self.assertEqual(key_terms(""), [])

    def test_non_string_input_does_not_raise(self):
        for value in (123, [], {}, object()):
            with self.subTest(value=type(value).__name__):
                self.assertEqual(key_terms(value), [])

    def test_et_al_and_parenthesised_year(self):
        # "et"/"al" dropped, "2017" kept -- a year is one of the most
        # distinctive things a question can carry.
        self.assertEqual(
            key_terms("Vaswani et al. (2017) positional encoding"),
            ["vaswani", "2017", "positional", "encoding"],
        )

    def test_latex_commands_are_stripped(self):
        self.assertEqual(
            key_terms("\\textbf{Attention} positional"), ["attention", "positional"]
        )

    def test_math_is_stripped(self):
        # \\alpha, \\beta and the $...$ delimiters all vanish; only the real word
        # survives. A command name is never something the user asked about.
        self.assertEqual(key_terms(r"$\alpha$ and $\beta$ attention"), ["attention"])

    def test_latex_ref_command_leaves_nothing(self):
        # \ref{fig:1} -> "fig 1" -> "fig" is apparatus and "1" is a bare
        # integer, so the whole citation collapses to nothing.
        self.assertEqual(key_terms(r"ref{fig:1}"), [])

    def test_line_break_hyphenation_is_healed(self):
        # PyMuPDF emits "classifi-\ncation". A hyphen before a newline is a line
        # break, never part of the word, so it must be removed BEFORE the
        # separator split or "classification" becomes two nonsense terms.
        self.assertIn("classification", key_terms("classifi-\ncation helps"))

    def test_hyphenated_compounds_are_not_welded_into_one_token(self):
        # Regression: without a guard on the continuation, a compound broken
        # across a line was welded into one nonsense token and then reported as
        # missing from a passage that plainly contained it. The continuation
        # must be a whole word. ("of", "the" and "art" are stopwords, so only
        # the two content words are expected to survive.)
        self.assertEqual(
            key_terms("state-\nof-the-art attention"), ["state", "attention"]
        )

    def test_figures_and_bare_integers_are_dropped_but_real_numbers_survive(self):
        # "Figure 3" contributes nothing; 1000 and 2017 do.
        self.assertEqual(
            key_terms("Compare 1000 tokens against 95 accuracy in Figure 3"),
            ["compare", "1000", "tokens", "accuracy"],
        )

    def test_camel_case_is_not_split(self):
        # Splitting "LayerNorm" would yield two terms the paper never uses and
        # inflate the denominator with terms that can never be found.
        self.assertEqual(key_terms("LayerNorm and RMSNorm"), ["layernorm", "rmsnorm"])

    def test_ligatures_split_and_understate_coverage(self):
        # Documented limitation. U+FB01 is not in [a-z0-9] so it acts as a
        # separator. `unicodedata` is deliberately NOT imported (it is outside
        # the suite's allow-list) so this under-count is accepted: the error
        # direction is understating coverage, which is the safe one.
        self.assertEqual(key_terms("classiﬁer"), ["classi"])

    def test_duplicates_collapse_and_first_appearance_order_is_kept(self):
        # Note "nns"/"nn" are NOT usable here: a bare two-character alphabetic
        # token is dropped as noise, so a real term is used instead.
        self.assertEqual(
            key_terms("kernel kerneling kerneling attention kernel"),
            ["kernel", "kerneling", "attention"],
        )

    def test_two_calls_return_equal_results(self):
        text = "transformer attention encoder decoder"
        self.assertEqual(key_terms(text), key_terms(text))

    def test_input_is_not_mutated(self):
        text = "Attention positional encoding"
        before = str(text)
        key_terms(text)
        self.assertEqual(text, before)


class CoverageReportMatchingTests(unittest.TestCase):
    """A term counts as present when a passage really contains it."""

    QUESTION = "how does positional encoding work in the transformer"

    def test_terms_present_in_the_passage_are_covered(self):
        report = coverage_report(
            self.QUESTION, ["Positional encodings are used throughout the Transformer."]
        )
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["covered"], 3)
        self.assertEqual(report["missing"], [])

    def test_absence_is_reported_per_term_not_as_a_judgement(self):
        report = coverage_report(self.QUESTION, ["Attention is all you need"])
        self.assertEqual(report["covered"], 0)
        self.assertEqual(
            report["missing"], ["positional", "encoding", "transformer"]
        )

    def test_exact_passage_tokens_count(self):
        report = coverage_report(
            "which encoder layer and attention head", ["The encoder layer uses attention heads."]
        )
        self.assertEqual(report["covered"], 4)
        self.assertEqual(report["missing"], [])

    def test_morphological_variants_count(self):
        # "encoders" for "encoder" is the single most common false negative in
        # ML prose, so the passage side accepts a short set of suffixes.
        report = coverage_report(
            "which encoder layer and attention head",
            ["Encoders and attention layers use many heads."],
        )
        self.assertEqual(report["covered"], 4)
        self.assertEqual(report["missing"], [])

    def test_a_partially_covered_question_lists_only_what_was_missing(self):
        report = coverage_report(
            "which encoder layer and attention head", ["The encoder layer is deep."]
        )
        self.assertEqual(report["terms"], ["encoder", "layer"])
        self.assertEqual(report["missing"], ["attention", "head"])

    def test_derivational_suffixes_do_not_match_short_terms(self):
        # Regression, and the reason the derivational tier is gated on length.
        # Each passage below contains a COMPARATIVE or a different word class,
        # not a form of the question's term. Reporting the term as found marks
        # it as present when the evidence does not contain it -- the one
        # direction this readout must not err in.
        cases = [
            ("low", "how does low rank adaptation work", "A lower bound on the estimator."),
            ("high", "how does high resolution imaging work", "A higher resolution image."),
            ("form", "how do partial observability and form work", "The formal model."),
        ]
        for term, question, passage in cases:
            with self.subTest(term=term):
                report = coverage_report(question, [passage])
                self.assertIn(term, report["missing"])
                self.assertNotIn(term, report["terms"])

    def test_long_terms_still_match_their_derivations(self):
        # The gate must not over-correct: "attention" is long enough to have a
        # derivational suffix, and "attentional" is a real word in the papers.
        report = coverage_report(
            "does attention need positional encoding",
            ["Positional encoding gives an attentional bias."],
        )
        self.assertEqual(report["covered"], 3)
        self.assertEqual(report["missing"], [])

    def test_ion_suffix_is_not_used(self):
        # cat + ion = "cation" would be a real false positive, so "ion"/"ions"
        # are deliberately excluded from the variant list.
        report = coverage_report("dog cat bird fish", ["cation definition"])
        self.assertEqual(report["missing"], ["dog", "cat", "bird", "fish"])

    def test_a_longer_word_does_not_satisfy_a_shorter_term(self):
        # Three terms, so the report is not suppressed before the assertion.
        report = coverage_report("cat dog bird", ["classification of images"])
        self.assertEqual(report["missing"], ["cat", "dog", "bird"])

    def test_unrelated_words_do_not_cover_each_other(self):
        report = coverage_report(self.QUESTION, ["positivity and encoding was nice"])
        self.assertEqual(report["missing"], ["positional", "transformer"])

    def test_coverage_spans_all_passages(self):
        report = coverage_report(
            self.QUESTION, ["Nothing useful here at all", "The transformer is core."]
        )
        self.assertEqual(report["terms"], ["transformer"])
        self.assertEqual(report["missing"], ["positional", "encoding"])

    def test_none_and_non_string_passages_are_skipped_not_raised_on(self):
        report = coverage_report(self.QUESTION, [None, 42, "positional encoding"])
        self.assertEqual(report["covered"], 2)
        self.assertEqual(report["missing"], ["transformer"])

    def test_counts_stay_true_while_the_named_lists_are_capped(self):
        terms = "alpha bravo charlie delta echo foxtrot golf hotel india juliet " \
                "kilo lima mike november oscar papa"
        report = coverage_report(terms, ["alpha bravo charlie"])
        self.assertEqual(report["total"], 16)
        self.assertEqual(report["covered"], 3)
        self.assertEqual(len(report["terms"]), 3)
        # 13 were missing but only MAX_REPORTED names ship. The counts must stay
        # true regardless, so the two numbers can never contradict each other,
        # and the truncation must be declared rather than implied.
        self.assertEqual(report["missing"].__len__(), MAX_REPORTED)
        self.assertTrue(report["truncated"])

    def test_a_short_report_is_not_marked_truncated(self):
        report = coverage_report(self.QUESTION, ["transformer"])
        self.assertFalse(report["truncated"])

    def test_report_has_exactly_the_five_documented_keys(self):
        report = coverage_report(self.QUESTION, ["transformer"])
        self.assertEqual(
            sorted(report), ["covered", "missing", "terms", "total", "truncated"]
        )


class CoverageReportSuppressionTests(unittest.TestCase):
    """A flattering number beats no number in usefulness, never in honesty."""

    def test_question_with_no_key_terms_is_suppressed(self):
        self.assertIsNone(coverage_report("what is the a of", ["anything"]))

    def test_one_term_is_too_few_to_mean_anything(self):
        self.assertIsNone(coverage_report("transformer", ["transformer"]))

    def test_two_terms_is_still_too_few(self):
        # "covered 2 of 2" reads like a perfect score and means nothing, so the
        # readout is suppressed below MIN_TERMS.
        self.assertIsNone(coverage_report("transformer attention", ["transformer"]))
        self.assertEqual(MIN_TERMS, 3)

    def test_exactly_min_terms_produces_a_report(self):
        report = coverage_report("transformer attention encoder", ["transformer"])
        self.assertIsNotNone(report)
        self.assertEqual(report["total"], 3)

    def test_no_passages_is_suppressed(self):
        self.assertIsNone(coverage_report("transformer attention encoder", []))
        self.assertIsNone(coverage_report("transformer attention encoder", None))

    def test_passages_with_no_usable_text_are_suppressed(self):
        self.assertIsNone(coverage_report("transformer attention encoder", [None, 42]))


class CoverageReportPurityTests(unittest.TestCase):
    def test_inputs_are_not_mutated(self):
        question = "transformer attention encoder"
        passages = ["transformer", "attention"]
        question_before, passages_before = str(question), list(passages)
        coverage_report(question, passages)
        self.assertEqual(question, question_before)
        self.assertEqual(passages, passages_before)

    def test_two_calls_return_deeply_equal_reports(self):
        first = coverage_report("transformer attention encoder", ["transformer"])
        second = coverage_report("transformer attention encoder", ["transformer"])
        self.assertEqual(first, second)
        self.assertIsNot(first, second)

    def test_nothing_is_aliased_into_the_caller_s_text(self):
        passages = ["transformer attention encoder"]
        report = coverage_report("transformer attention encoder", passages)
        self.assertIsNot(report["terms"], passages)


class SummariseCoverageTests(unittest.TestCase):
    """The wording is the contract: a fact about pages, never a verdict."""

    REPORT = {
        "total": 7,
        "covered": 5,
        "terms": ["positional", "encoding"],
        "missing": ["decoder", "tokenizer"],
    }

    def test_none_gives_no_line(self):
        self.assertEqual(summarise_coverage(None), "")

    def test_line_reports_the_true_counts(self):
        line = summarise_coverage(self.REPORT)
        self.assertIn("covered 5 of 7", line)
        self.assertIn("positional, encoding", line)

    def test_missing_terms_are_named_not_just_counted(self):
        # "covered 5 of 7" with no way to see the two would be an unbacked
        # assertion -- the same shape as the ungrounded "58% match" this readout
        # replaces. The names must ship.
        self.assertIn("decoder, tokenizer", summarise_coverage(self.REPORT))

    def test_wording_is_scoped_to_the_retrieved_pages(self):
        # Never "not in the paper": with top-5 of 512-word chunks a distinctive
        # term can legitimately live on page 40 of a 60-page paper.
        line = summarise_coverage(self.REPORT)
        self.assertIn("not found in the retrieved pages", line)
        self.assertNotIn("not in the paper", line)

    def test_the_fixed_wording_states_no_percentage_and_no_confidence(self):
        # Asserted on the fixed part of the line only. Term names are echoed
        # verbatim from the user's own question, so a question literally
        # containing the word "confidence" would otherwise fail this test --
        # which would be the test being wrong, not the wording.
        line = summarise_coverage(self.REPORT)
        fixed = line.split(":")[0]
        for forbidden in ("%", "confidence", "probability", "score", "accurate"):
            with self.subTest(word=forbidden):
                self.assertNotIn(forbidden, fixed.lower())

    def test_truncation_is_disclosed(self):
        terms = "alpha bravo charlie delta echo foxtrot golf hotel india juliet " \
                "kilo lima mike november oscar papa"
        line = summarise_coverage(coverage_report(terms, ["alpha bravo charlie"]))
        # 13 missing, 12 named: without this the enumeration reads as complete.
        self.assertIn("not found in the retrieved pages", line)
        self.assertIn("and 1 more", line)

    def test_no_clause_is_appended_when_nothing_is_missing(self):
        line = summarise_coverage(
            {"total": 3, "covered": 3, "terms": ["a", "b", "c"], "missing": []}
        )
        self.assertNotIn("not found", line)

    def test_empty_term_list_does_not_leave_a_dangling_colon(self):
        line = summarise_coverage(
            {"total": 4, "covered": 0, "terms": [], "missing": ["x", "y", "z", "w"]}
        )
        self.assertIn("covered 0 of 4 key terms", line)
        self.assertNotIn("key terms: ", line)

    def test_an_empty_report_does_not_raise_and_says_nothing(self):
        # An empty dict is "no measurement", which is the same thing as None.
        self.assertEqual(summarise_coverage({}), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
