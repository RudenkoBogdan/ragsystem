"""Executable proof of the feature's headline claim: no LLM call when retrieval is empty.

``backend/chat/service.py`` imports ``aiohttp`` and ``vector.chroma`` at module
scope, none of which are installed here, so it cannot be imported -- and
``ModuleHygieneTests`` deliberately forbids any test from importing it.

The alternative is to leave the most important behaviour in the diff untested:
"if retrieval returns nothing, refuse, and never open a connection to the
provider" is a claim about control flow, and it is the claim this whole feature
rests on. So this file does what ``test_prompt_mapping.py`` already does for
``build_system_prompt``: lift the real ``stream_rag_response`` ``AsyncFunctionDef``
out of the source with ``ast``, ``compile()`` it, and ``exec()`` it into a
namespace where every collaborator is either a REAL importable function or a
sentinel that raises if it is ever touched.

The sentinels are the actual test. ``_guarded_endpoint`` (the SEC-1 outbound
URL guard that replaced the direct ``_resolve_endpoint`` call), ``_resolve_endpoint``
and ``aiohttp`` are replaced by things that fail the test if called, so "no LLM
call was made" is asserted by construction rather than by reading the source.
Adding the guard in front of the provider call made this loader's job bigger --
the lifted function now has two more module globals to resolve -- and a
``NameError`` at call time would have looked like an unrelated failure, so
``_guarded_endpoint`` is bound explicitly and a null ``log`` is bound for the
same reason.  ``build_system_prompt`` shares the namespace and must stay
liftable on its own with only ``group_citations``; its set of free names is
asserted by ``test_prompt_mapping.LiftedFunctionStaysSelfContainedTests`` and
is deliberately not widened here.

Run with::

    python3 -m unittest discover -s backend/tests -t backend -v
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import sys
import unittest
from typing import AsyncGenerator, Optional

try:
    from chat.citations import (
        abstention_message,
        build_sources,
        extract_citations,
        group_citations,
    )
    from chat.coverage import coverage_report, summarise_coverage
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from chat.citations import (
        abstention_message,
        build_sources,
        extract_citations,
        group_citations,
    )
    from chat.coverage import coverage_report, summarise_coverage


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_PATH = os.path.join(BACKEND_DIR, "chat", "service.py")


class _CalledTheModel(Exception):
    """Raised by a sentinel if the abstention path ever reaches the LLM."""


def _forbidden(name):
    def _boom(*args, **kwargs):
        raise _CalledTheModel(name)

    return _boom


def _null_logger():
    """A `ragapp`-namespaced logger that emits nothing.

    The lifted function logs through the module-level `log`. Binding the real
    `get_logger` would mean binding the lifted function to the application's
    logging configuration for a test that asserts nothing was logged, so this
    is a `NullHandler` with `propagate = False` instead.
    """
    logger = logging.getLogger("ragapp.lifted_test")
    if not any(isinstance(h, logging.NullHandler) for h in logger.handlers):
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def load_stream_rag_response(retrieve_result, calls):
    """Lift the real ``stream_rag_response`` and bind it to test doubles.

    ``calls`` is a list that records the ``(user_id, question, paper_ids)`` the
    lifted function passed to ``retrieve_context``, so the scope plumbing is
    observable too.
    """

    def fake_retrieve_context(user_id, question, paper_ids=None):
        calls.append({"user_id": user_id, "question": question, "paper_ids": paper_ids})
        return list(retrieve_result)

    with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=SERVICE_PATH)

    target = None
    prompt_fn = None
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "stream_rag_response":
            target = node
        elif isinstance(node, ast.FunctionDef) and node.name == "build_system_prompt":
            prompt_fn = node
    if target is None or prompt_fn is None:
        return None

    namespace = {
        "__name__": "lifted_service",
        # Real, importable collaborators.
        "json": json,
        "Optional": Optional,
        "AsyncGenerator": AsyncGenerator,
        "group_citations": group_citations,
        "extract_citations": extract_citations,
        "build_sources": build_sources,
        "abstention_message": abstention_message,
        "coverage_report": coverage_report,
        "summarise_coverage": summarise_coverage,
        "retrieve_context": fake_retrieve_context,
        "log": _null_logger(),
        # Sentinels: the whole point is that these are never reached.
        "_guarded_endpoint": _forbidden("_guarded_endpoint"),
        "_resolve_endpoint": _forbidden("_resolve_endpoint"),
        "aiohttp": _forbidden("aiohttp"),
    }
    exec(
        compile(ast.Module(body=[prompt_fn, target], type_ignores=[]), SERVICE_PATH, "exec"),
        namespace,
    )
    return namespace.get("stream_rag_response")


def drain(agen):
    """Collect every event an async generator yields."""

    async def collect():
        events = []
        async for chunk in agen:
            events.append(chunk)
        return events

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(collect())
    finally:
        loop.close()


def parse_events(raw_chunks):
    """Turn the SSE wire format back into dicts, asserting the framing."""
    events = []
    for chunk in raw_chunks:
        assert chunk.startswith("data: "), "every event must be a `data: ` frame"
        assert chunk.endswith("\n\n"), "every event must end with a blank line"
        assert "event:" not in chunk, "no SSE event names are used by this project"
        events.append(json.loads(chunk[6:].strip()))
    return events


@unittest.skipIf(
    load_stream_rag_response([], []) is None,
    "stream_rag_response could not be lifted from service.py",
)
class EmptyRetrievalAbstainsTests(unittest.TestCase):
    """The claim, stated as a test: nothing retrieved => no provider call."""

    def run_stream(self, retrieve_result, paper_ids=None, scope_titles=None):
        calls = []
        fn = load_stream_rag_response(retrieve_result, calls)
        agen = fn(
            1,
            "what dropout rate did they use?",
            [],
            paper_ids=paper_ids,
            scope_titles=scope_titles,
        )
        return parse_events(drain(agen)), calls

    def test_exactly_two_events_are_emitted(self):
        events, _calls = self.run_stream([])
        self.assertEqual(len(events), 2)
        self.assertEqual([e["type"] for e in events], ["token", "done"])

    def test_the_token_event_is_the_app_authored_refusal(self):
        # Authored by `abstention_message`, streamed as if it were an answer, so
        # the client needs no new branch to render it.
        events, _calls = self.run_stream([])
        self.assertEqual(events[0]["content"], abstention_message(None))

    def test_the_refusal_never_permits_an_unsourced_answer(self):
        events, _calls = self.run_stream([])
        self.assertNotIn("general knowledge", events[0]["content"])

    def test_the_done_event_declares_the_abstention(self):
        events, _calls = self.run_stream([])
        done = events[-1]
        self.assertTrue(done["abstained"])
        self.assertEqual(done["sources"], [])
        self.assertEqual(done["cited"], [])
        self.assertEqual(done["unresolved"], [])
        self.assertIsNone(done["coverage"])
        self.assertIsNone(done["coverage_line"])

    def test_no_llm_call_is_made(self):
        # The lifted function's `_guarded_endpoint`, `_resolve_endpoint` and
        # `aiohttp` all raise if reached, so simply completing proves the
        # provider was never contacted -- not even its URL was resolved.
        try:
            events, _calls = self.run_stream([])
        except _CalledTheModel as exc:  # pragma: no cover - the failure this guards
            self.fail("the abstention path must never reach the LLM, got %s" % exc)
        self.assertEqual(len(events), 2)

    def test_the_scope_is_echoed_back(self):
        events, _calls = self.run_stream([])
        self.assertEqual(
            events[-1]["scope"],
            {"applied": False, "paper_ids": [], "paper_titles": []},
        )

    def test_a_scope_that_matched_nothing_still_abstains(self):
        # The dangerous case: scoping to papers with no indexed text. It must
        # refuse, and it must not describe the search as "your library".
        events, _calls = self.run_stream([], paper_ids=[3, 7], scope_titles=["A", "B"])
        done = events[-1]
        self.assertTrue(done["abstained"])
        self.assertEqual(done["scope"]["applied"], True)
        self.assertEqual(done["scope"]["paper_ids"], [3, 7])
        self.assertIn("A", events[0]["content"])
        self.assertNotIn("general knowledge", events[0]["content"])

    def test_the_scope_reaches_retrieval_unchanged(self):
        _events, calls = self.run_stream([], paper_ids=[3, 7])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["paper_ids"], [3, 7])

    def test_an_unscoped_question_searches_the_whole_library(self):
        _events, calls = self.run_stream([])
        self.assertIsNone(calls[0]["paper_ids"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
