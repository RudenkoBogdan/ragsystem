"""Bounded list endpoints (SEC-6): the policy, and the wiring that applies it.

Audit finding 17 was that `list_papers`, `list_sessions` and `get_messages`
all ended in `.all()` with no limit, no offset and no ordering guarantee, so
one request could return an entire library.

**Why half of this file is text assertions.** The routers cannot be
imported here: `backend/chat/router.py` and `backend/papers/router.py` both
import `fastapi` and `sqlalchemy`, and neither is installed. `compileall`
proves they parse; it does not prove they *bound* anything. So the wiring is
pinned the only way it honestly can be here -- by reading the source text of
the three handler functions with `ast` and asserting on what they contain.
That is a weaker guarantee than executing the handler, and it is stated as
such rather than dressed up: a reader should assume a maintainer with a
working environment still wants to exercise these endpoints against a real
database.

The other half of the file is executable, because `resolve_page` is pure and
importable: the bound itself, the cap, and the last-page ordering idiom are
all real assertions about real code.

Run with::

    python3 -m unittest discover -s backend/tests -t backend -v
"""

from __future__ import annotations

import ast
import os
import re
import sys
import unittest

try:
    from security.policy import PolicyError, resolve_page
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from security.policy import PolicyError, resolve_page


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPERS_ROUTER = os.path.join(BACKEND_DIR, "papers", "router.py")
CHAT_ROUTER = os.path.join(BACKEND_DIR, "chat", "router.py")

#: The cap every list endpoint uses, as a literal in the source. The default
#: and the maximum are the same number on purpose: an absent limit must not
#: mean "more than a present one can ask for".
PAGE_CAP = 200

LIST_HANDLERS = {
    "list_papers": PAPERS_ROUTER,
    "list_sessions": CHAT_ROUTER,
    "get_messages": CHAT_ROUTER,
}


def handler_node(path, name):
    """The top-level FunctionDef called `name` in `path`, or None."""
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def handler_source(path, name):
    """The source text of one top-level function, decorators included.

    Locating by function rather than scanning the whole file is the point:
    an unbounded load legitimately remains in `send_message`'s history
    query, and a file-wide grep would either miss real regressions or fail
    for the wrong reason. Returns "" if the function is missing or nested,
    which the assertions below then fail on loudly.

    Known brittleness, stated rather than hidden: this is raw text, so a
    *comment* mentioning a forbidden string would fail the check that looks
    for it. `test_no_handler_calls_all` therefore asserts the same property
    structurally over the AST, where a comment cannot reach it.
    """
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    lines = source.splitlines()
    node = handler_node(path, name)
    if node is None:
        return ""
    # A decorated function's `lineno` is the `def` line, so the decorators
    # have to be added back to see the route declaration.
    start = node.lineno - 1
    for decorator in node.decorator_list:
        start = min(start, decorator.lineno - 1)
    return "\n".join(lines[start:getattr(node, "end_lineno", None)])


def file_source(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class ResolvePageTests(unittest.TestCase):
    """The bound itself.

    Overlaps `test_policy.ResolvePageTests`, which covers the same function
    from the policy module's side. Repeated on purpose: this is the contract
    the *routers* depend on, and a change that broke the routers' assumption
    should be caught by a test that knows what the routers assume.
    """

    def call(self, limit, offset=0):
        return resolve_page(limit, offset, max_limit=PAGE_CAP, default_limit=PAGE_CAP)

    def test_an_explicit_limit_in_range_passes_through(self):
        self.assertEqual(self.call(1), (1, 0))
        self.assertEqual(self.call(50, 25), (50, 25))
        self.assertEqual(self.call(PAGE_CAP, 10_000), (PAGE_CAP, 10_000))

    def test_a_limit_of_zero_is_refused(self):
        # Zero is not "no limit", it is an empty response, and a caller that
        # means "all" should be told no rather than given nothing.
        with self.assertRaises(PolicyError):
            self.call(0)

    def test_a_limit_over_the_cap_is_refused(self):
        with self.assertRaises(PolicyError):
            self.call(PAGE_CAP + 1)
        with self.assertRaises(PolicyError):
            self.call(100_000)

    def test_a_negative_offset_is_refused(self):
        with self.assertRaises(PolicyError):
            self.call(10, -1)

    def test_a_missing_limit_falls_back_to_the_default(self):
        self.assertEqual(self.call(None), (PAGE_CAP, 0))

    def test_the_refusal_message_names_the_bound(self):
        with self.assertRaises(PolicyError) as caught:
            self.call(0)
        self.assertIn(str(PAGE_CAP), str(caught.exception))


class BoundedByDefaultTests(unittest.TestCase):
    """The default IS the cap.

    This is the property that makes the bound a bound rather than a
    suggestion: a client that sends nothing gets the maximum, so there is no
    value of "no limit" that means the whole table.
    """

    def test_a_no_argument_call_yields_the_cap(self):
        limit, offset = resolve_page(
            None, 0, max_limit=PAGE_CAP, default_limit=PAGE_CAP
        )
        self.assertEqual(limit, PAGE_CAP)
        self.assertEqual(offset, 0)

    def test_asking_for_the_cap_explicitly_is_the_same_ask(self):
        implicit, _ = resolve_page(None, 0, max_limit=PAGE_CAP, default_limit=PAGE_CAP)
        explicit, _ = resolve_page(PAGE_CAP, 0, max_limit=PAGE_CAP, default_limit=PAGE_CAP)
        self.assertEqual(implicit, explicit)

    def test_every_list_handler_declares_the_same_cap(self):
        for name, path in sorted(LIST_HANDLERS.items()):
            with self.subTest(handler=name):
                source = handler_source(path, name)
                self.assertIn("default=%d" % PAGE_CAP, source)
                self.assertIn("le=%d" % PAGE_CAP, source)
                self.assertIn("ge=0", source)
                self.assertIn("max_limit=%d" % PAGE_CAP, source)
                self.assertIn("default_limit=%d" % PAGE_CAP, source)

    def test_the_response_shape_is_unchanged(self):
        # SEC-6 bounds the rows; it does not wrap them. A client that reads a
        # bare array must keep reading a bare array.
        for name, path in sorted(LIST_HANDLERS.items()):
            with self.subTest(handler=name):
                source = handler_source(path, name)
                decorator_line = source.splitlines()[0]
                self.assertIn("response_model=list[", decorator_line)


class RouterWiringTests(unittest.TestCase):
    """The three handlers are bounded, ordered and no longer call `.all()`."""

    def test_no_list_handler_uses_all(self):
        for name, path in sorted(LIST_HANDLERS.items()):
            with self.subTest(handler=name):
                source = handler_source(path, name)
                self.assertNotIn(
                    ".all()",
                    source,
                    "%s still loads the whole table; SEC-6 requires a bounded "
                    "query" % name,
                )

    def test_no_handler_calls_all(self):
        # The structural version of the check above: no `.all()` attribute
        # call anywhere in the handler's body. This one cannot be fooled by
        # a comment, a docstring, or a differently-spaced `.all ()`.
        for name, path in sorted(LIST_HANDLERS.items()):
            with self.subTest(handler=name):
                node = handler_node(path, name)
                self.assertIsNotNone(node, "%s must remain a top-level handler" % name)
                called = sorted(
                    {
                        child.attr
                        for child in ast.walk(node)
                        if isinstance(child, ast.Attribute) and child.attr == "all"
                    }
                )
                self.assertEqual(
                    called, [], "%s must not call .all()" % name
                )

    def test_no_handler_executes_an_unbounded_query(self):
        # A `Query` with no `.limit()` anywhere would still return the whole
        # table, whether or not anything calls `.all()` on it.
        for name, path in sorted(LIST_HANDLERS.items()):
            with self.subTest(handler=name):
                source = handler_source(path, name)
                self.assertEqual(
                    len(re.findall(r"\.limit\(", source)),
                    len(re.findall(r"\.offset\(", source)),
                    "%s must pair every .limit() with an .offset()" % name,
                )

    def test_every_list_handler_mentions_limit_and_offset(self):
        for name, path in sorted(LIST_HANDLERS.items()):
            with self.subTest(handler=name):
                source = handler_source(path, name)
                self.assertIn("limit: int = Query", source)
                self.assertIn("offset: int = Query", source)
                self.assertIn(".limit(", source)
                self.assertIn(".offset(", source)
                self.assertIn("resolve_page(", source)

    def test_get_messages_takes_an_order(self):
        source = handler_source(CHAT_ROUTER, "get_messages")
        self.assertIn("order: str = Query", source)
        self.assertIn("^(asc|desc)$", source)
        self.assertIn('order == "desc"', source)

    def test_list_papers_is_ordered_by_a_stable_key(self):
        # Without a total order, `offset` paging can repeat or skip a row.
        source = handler_source(PAPERS_ROUTER, "list_papers")
        self.assertIn("order_by(models.Paper.id)", source)

    def test_list_sessions_keeps_updated_at_and_adds_a_tiebreaker(self):
        source = handler_source(CHAT_ROUTER, "list_sessions")
        self.assertIn("updated_at.desc()", source)
        self.assertIn("models.ChatSession.id.desc()", source)

    def test_get_messages_keeps_the_created_at_id_ordering(self):
        source = handler_source(CHAT_ROUTER, "get_messages")
        self.assertIn("models.Message.created_at", source)
        self.assertIn("models.Message.id", source)

    def test_the_stream_error_no_longer_carries_str_exc(self):
        # The regression this pins: the `done` event used to serialise
        # `str(exc)`, which is how a provider's (or an internal service's)
        # response body reached the client.
        source = file_source(CHAT_ROUTER)
        self.assertNotIn('"error": str(exc)', source)
        self.assertIn('"error": public_error_message(exc)', source)

    def test_the_ingest_failure_no_longer_carries_the_exception_text(self):
        # Same class of leak on the papers side: `detail=f"Ingestion failed:
        # {e}"` handed the user whatever arXiv's client or PyMuPDF raised.
        source = file_source(PAPERS_ROUTER)
        self.assertNotIn("Ingestion failed: {e}", source)
        self.assertIn(
            "Ingestion failed: the paper could not be retrieved from arXiv.", source
        )
        self.assertIn("log.exception(\"papers.ingest_failed", source)

    def test_no_route_decorator_or_response_model_was_rewritten(self):
        # The charter's "preserve the public API" rule, stated as a test of
        # the paths themselves.
        papers = file_source(PAPERS_ROUTER)
        chat = file_source(CHAT_ROUTER)
        for path in ('@router.get("", response_model=list[PaperResponse])',
                     '@router.delete("/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)'):
            self.assertIn(path, papers)
        for path in ('@router.get("/sessions", response_model=list[SessionResponse])',
                     '@router.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])',
                     '@router.post("/sessions/{session_id}/messages")'):
            self.assertIn(path, chat)
        self.assertIn("StreamingResponse(generate(), media_type=\"text/event-stream\")", chat)

    def test_the_sse_framing_and_http_status_are_untouched(self):
        # The terminal `done` is the only way the client ever leaves
        # streaming state, so neither the frame nor the 200 may change, and
        # the failed-request caveat must still be persisted.
        chat = file_source(CHAT_ROUTER)
        self.assertIn('"type": "done"', chat)
        self.assertIn('+ "\\n\\n"', chat)
        self.assertEqual(
            len(re.findall(r"stream_failed = True", chat)),
            1,
            "stream_failed must still be set in exactly one place",
        )
        self.assertIn("if stream_failed:", chat)
        self.assertIn("elif final_abstained and not assistant_content:", chat)


class LastPageOrderingTests(unittest.TestCase):
    """The `order="desc"` idiom, as a pure list operation.

    `LIMIT/OFFSET` count from the start of the ordering, so "the newest 20
    messages of a long session" is unreachable by ordering ascending and
    slicing -- the database has to be asked descending, and the result has
    to be put back in chronological order for the client. The router does
    exactly this; here it is checked without a database.
    """

    @staticmethod
    def last_page(all_rows, limit, offset, order):
        """The router's algorithm, on a plain list."""
        if order == "desc":
            newest_first = list(reversed(all_rows))[offset:offset + limit]
            return list(reversed(newest_first))
        return all_rows[offset:offset + limit]

    def setUp(self):
        self.rows = [(index, "message %d" % index) for index in range(1, 11)]

    def test_ascending_returns_the_oldest_first_page(self):
        self.assertEqual(
            self.last_page(self.rows, 3, 0, "asc"),
            [(1, "message 1"), (2, "message 2"), (3, "message 3")],
        )

    def test_descending_returns_the_newest_page_in_chronological_order(self):
        # The actual bug this parameter exists to fix: ascending + limit
        # returned the OLDEST 20 of the session, not the newest.
        self.assertEqual(
            self.last_page(self.rows, 3, 0, "desc"),
            [(8, "message 8"), (9, "message 9"), (10, "message 10")],
        )

    def test_descending_still_paginates(self):
        self.assertEqual(
            self.last_page(self.rows, 3, 3, "desc"),
            [(5, "message 5"), (6, "message 6"), (7, "message 7")],
        )

    def test_descending_beyond_the_end_is_empty_not_an_error(self):
        self.assertEqual(self.last_page(self.rows, 5, 100, "desc"), [])

    def test_ascending_offset_walks_forward_through_the_list(self):
        self.assertEqual(
            self.last_page(self.rows, 3, 3, "asc"),
            [(4, "message 4"), (5, "message 5"), (6, "message 6")],
        )

    def test_an_offset_past_the_end_is_empty_in_both_orders(self):
        for order in ("asc", "desc"):
            with self.subTest(order=order):
                self.assertEqual(self.last_page(self.rows, 3, 100, order), [])

    def test_a_limit_of_one_is_the_extreme_page(self):
        self.assertEqual(
            self.last_page(self.rows, 1, 0, "desc"), [(10, "message 10")]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
