"""Best-effort contract test for the *real* ``build_system_prompt``.

``backend/chat/service.py`` imports ``aiohttp`` (and ``vector.chroma``,
``config``) at module scope, and none of those are installed here, so the
module cannot be imported.  Rather than skip the most important integration
point, this file lifts **only** the ``build_system_prompt`` ``FunctionDef`` out
of the source with the stdlib ``ast`` module, ``compile()``s that single
function, and ``exec()``s it into a namespace pre-seeded with the single global
it needs (``group_citations``).  Nothing is stubbed except the module-level
imports, and the function body under test is the production source, verbatim.

If the loader ever stops working (the function is renamed, moved, or starts
using more module globals) the tests ``skip`` with a reason instead of failing
for an unrelated reason -- a fragile loader is worse than no loader.

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
    from chat.citations import group_citations
except ImportError:  # pragma: no cover - direct execution fallback
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from chat.citations import group_citations


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICE_PATH = os.path.join(BACKEND_DIR, "chat", "service.py")
BLOCK_HEADER = re.compile(r'(?m)^\[(\d+)\] Source: "([^"]*)", page (.*)$')


def _module_level_names(tree):
    """Every name bound at the top level of a module: imports, defs, constants."""
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _free_names(func):
    """Names ``func`` reads that it does not itself bind (params, locals, comps)."""
    bound = set()
    args = func.args
    for arg in list(getattr(args, "posonlyargs", [])) + list(args.args) + list(args.kwonlyargs):
        bound.add(arg.arg)
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.comprehension):
            for inner in ast.walk(node.target):
                if isinstance(inner, ast.Name):
                    bound.add(inner.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
    loads = {
        node.id
        for node in ast.walk(func)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }
    return loads - bound


def load_build_system_prompt():
    """Return the real ``build_system_prompt``, or ``None`` if it cannot be lifted.

    Only the stdlib is involved: read source -> parse -> pick one FunctionDef
    -> compile that node alone -> exec with ``group_citations`` pre-bound.
    """
    with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=SERVICE_PATH)
    target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_system_prompt":
            target = node
            break
    if target is None:
        return None
    module = ast.Module(body=[target], type_ignores=[])
    namespace = {"group_citations": group_citations, "__name__": "lifted_service"}
    exec(compile(module, SERVICE_PATH, "exec"), namespace)  # noqa: S102
    return namespace.get("build_system_prompt")


BUILD_SYSTEM_PROMPT = load_build_system_prompt()


def chunk(arxiv_id, page, text, title=None, score=None):
    return {
        "text": text,
        "title": title if title is not None else ("Paper %s" % arxiv_id),
        "arxiv_id": arxiv_id,
        "page": page,
        "score": score,
    }


DUPLICATE_PAGE_CHUNKS = [
    chunk("A", 3, "passage A p3", title="Paper A", score=0.91),
    chunk("A", 4, "passage A p4 first", title="Paper A", score=0.80),
    chunk("A", 4, "passage A p4 second", title="Paper A", score=0.55),
    chunk("B", 1, "passage B p1", title="Paper B", score=0.42),
    chunk("C", 2, "passage C p2", title="Paper C", score=0.10),
]


@unittest.skipIf(BUILD_SYSTEM_PROMPT is None, "build_system_prompt could not be lifted")
class BuildSystemPromptMappingTests(unittest.TestCase):
    """The ``[1]..[N]`` blocks in the prompt are exactly the returned labels."""

    CASES = {
        "empty": [],
        "single": [chunk("A", 1, "only passage")],
        "all_unique": [chunk("A", 1, "a1"), chunk("B", 7, "b7"), chunk("C", 2, "c2")],
        "duplicate_page": DUPLICATE_PAGE_CHUNKS,
        "all_same_page": [chunk("A", 1, "a"), chunk("A", 1, "b"), chunk("A", 1, "c")],
    }

    def test_empty_library_branch_returns_prompt_and_no_groups(self):
        prompt, groups = BUILD_SYSTEM_PROMPT([])
        self.assertEqual(groups, [])
        self.assertIn("No relevant papers", prompt)
        self.assertNotIn("] Source:", prompt)

    def test_prompt_blocks_map_one_to_one_onto_returned_labels(self):
        for name, chunks in self.CASES.items():
            if not chunks:
                continue
            with self.subTest(case=name):
                prompt, groups = BUILD_SYSTEM_PROMPT(chunks)
                headers = BLOCK_HEADER.findall(prompt)
                self.assertEqual(len(headers), len(groups))
                self.assertEqual(
                    [int(number) for number, _t, _p in headers],
                    [g["label"] for g in groups],
                )
                for index, (number, title, page) in enumerate(headers):
                    self.assertEqual(int(number), groups[index]["label"])
                    self.assertEqual(int(number), index + 1)
                    self.assertEqual(title, groups[index]["title"])
                    self.assertEqual(page, str(groups[index]["page"]))

    def test_duplicate_page_produces_four_blocks_not_five(self):
        prompt, groups = BUILD_SYSTEM_PROMPT(DUPLICATE_PAGE_CHUNKS)
        self.assertEqual([g["label"] for g in groups], [1, 2, 3, 4])
        self.assertEqual([int(n) for n, _t, _p in BLOCK_HEADER.findall(prompt)], [1, 2, 3, 4])
        self.assertEqual(prompt.count("] Source:"), 4)

    def test_merged_passages_both_appear_under_their_single_label(self):
        prompt, groups = BUILD_SYSTEM_PROMPT(DUPLICATE_PAGE_CHUNKS)
        block_two = prompt.split("\n\n---\n\n")[1]
        self.assertTrue(block_two.startswith('[2] Source: "Paper A", page 4'))
        self.assertIn("passage A p4 first", block_two)
        self.assertIn("passage A p4 second", block_two)
        self.assertEqual(groups[1]["chunk_count"], 2)

    def test_returned_groups_are_the_group_citations_output(self):
        chunks = list(DUPLICATE_PAGE_CHUNKS)
        _prompt, groups = BUILD_SYSTEM_PROMPT(chunks)
        self.assertEqual(groups, group_citations(chunks))

    def test_prompt_keeps_its_instructions_around_the_context(self):
        prompt, _groups = BUILD_SYSTEM_PROMPT(DUPLICATE_PAGE_CHUNKS)
        self.assertIn("Use ONLY the provided context", prompt)
        self.assertIn("Context:", prompt)

    def test_non_empty_branch_never_permits_general_knowledge(self):
        prompt, _groups = BUILD_SYSTEM_PROMPT(DUPLICATE_PAGE_CHUNKS)
        self.assertNotIn("general knowledge", prompt)


class LiftedFunctionStaysSelfContainedTests(unittest.TestCase):
    """Guard the loader itself, which is the one way this suite can go quietly blind.

    ``BuildSystemPromptMappingTests`` is skipped -- not failed -- if
    ``build_system_prompt`` is renamed, moved, nested or made async. A skip is
    easy to mistake for a pass, and the summary line is the only thing that
    reveals it. Two failure modes are covered here:

    * a *renamed or nested* function, which must fail loudly here rather than
      turning six tests into a skip;
    * a *new module-level global* referenced inside the function, which would
      raise ``NameError`` at call time in the lifted namespace -- a loud failure
      in the other direction, but one that reads like an unrelated error.
    """

    ALLOWED_GLOBALS = frozenset(["group_citations"])

    def setUp(self):
        with open(SERVICE_PATH, "r", encoding="utf-8") as handle:
            self.tree = ast.parse(handle.read(), filename=SERVICE_PATH)
        self.target = None
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "build_system_prompt":
                self.target = node
                break

    def test_the_function_is_still_liftable(self):
        self.assertIsNotNone(
            self.target,
            "build_system_prompt must remain a top-level def in service.py, or the "
            "prompt-mapping tests silently skip and stop testing anything",
        )

    def test_it_is_not_async(self):
        # ast.AsyncFunctionDef is not a subclass of ast.FunctionDef, so an async
        # def would vanish from the loader with no error at all.
        self.assertFalse(
            isinstance(self.target, ast.AsyncFunctionDef),
            "build_system_prompt must stay synchronous for the AST loader",
        )

    def test_it_references_no_module_global_except_group_citations(self):
        if self.target is None:
            self.fail("build_system_prompt is missing; see test_the_function_is_still_liftable")
        module_globals = _module_level_names(self.tree)
        leaked = _free_names(self.target) & module_globals
        self.assertEqual(
            leaked,
            set(self.ALLOWED_GLOBALS),
            "build_system_prompt is lifted into a namespace seeded only with "
            "group_citations, so it must not reference any other module-level name",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
