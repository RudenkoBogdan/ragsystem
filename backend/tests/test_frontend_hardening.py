"""Regression guard for the frontend's no-raw-HTML property.

**This is a text check.**  It reads files as strings and greps them.  It does
not import, parse, type-check, build or render anything, and it never executes
a line of TypeScript.  There is no JavaScript or TypeScript test runner in this
repository -- ``frontend/node_modules`` does not exist, ``node``, ``npm``,
``npx`` and ``tsc`` are not on ``PATH``, and the project charter forbids
installing them -- so this is the only kind of check available from the Python
test suite.  Read it as a smoke alarm that fires on the two spellings of the
dangerous thing, not as proof that the frontend is safe.

What is being protected: the audit cleared the frontend of any raw-HTML
rendering path and marked that clearance a hard property
(``NIGHT_REPORT.md``, "Explicitly NOT problems").  React escapes by default,
and that default is what makes model-generated markdown and KaTeX output safe
to render.  Two changes would undo it:

* ``dangerouslySetInnerHTML`` anywhere in ``frontend/src`` -- the direct bypass;
* ``rehype-raw`` (or the camelCase ``rehypeRaw``) added to the
  ``react-markdown`` plugin list in ``components/MathContent.tsx`` -- which
  re-enables raw HTML parsing inside an otherwise-escaping renderer.

Neither exists today.  This file exists so that the next person who adds one
sees a red test instead of shipping an XSS hole on the day they are not
looking for one.

Run it with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible, stdlib only.  Nothing is imported from ``frontend/``.
"""

from __future__ import annotations

import os
import re
import unittest

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
FRONTEND_SRC = os.path.join(REPO_ROOT, "frontend", "src")
MATH_CONTENT = os.path.join(FRONTEND_SRC, "components", "MathContent.tsx")

#: File extensions a Next.js App Router project can hold source in. Anything
#: else under frontend/src (a .css file, a .json) is still read by the sweep
#: below -- the guard does not care what a file is called.
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".json", ".mdx")

#: Both the package name and the identifier the import would be renamed to.
REHYPE_RAW_RE = re.compile(r"rehype[-_]?raw", re.IGNORECASE)

#: Directories that are not source we should be scanning.
SKIP_DIRS = frozenset({"node_modules", ".next", ".git", "dist", "build", "out"})


def iter_source_files(root=FRONTEND_SRC):
    """Yield ``(absolute_path, relative_path, text)`` for every file under root.

    Read as text with ``errors="replace"`` so an unexpected binary or an
    encoding surprise fails the *assertion* below with a readable message
    rather than crashing the suite.
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if not filename.endswith(SOURCE_SUFFIXES):
                continue
            path = os.path.join(dirpath, filename)
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                yield path, os.path.relpath(path, REPO_ROOT), handle.read()


class NoRawHtmlRenderingTests(unittest.TestCase):
    """``dangerouslySetInnerHTML`` must not appear anywhere in frontend/src."""

    def _files(self):
        return list(iter_source_files())

    def test_frontend_src_actually_exists(self):
        # Without this, every assertion below would pass vacuously if the path
        # constant ever drifted.
        self.assertTrue(os.path.isdir(FRONTEND_SRC), FRONTEND_SRC)

    def test_the_sweep_actually_read_files(self):
        # A walk that matched nothing would make the whole class trivially
        # green and look like a pass.
        found = self._files()
        self.assertGreaterEqual(len(found), 10, "frontend/src looks unexpectedly empty")

    def test_no_file_uses_dangerouslysetinnerhtml(self):
        offenders = [
            relative
            for _path, relative, text in self._files()
            if "dangerouslySetInnerHTML" in text
        ]
        self.assertEqual(
            offenders,
            [],
            "raw HTML rendering must stay off; React's default escaping is the "
            "control, and rehype-raw would remove it",
        )

    def test_no_file_uses_a_cased_variant_of_it(self):
        # Belt and braces: the attribute is case-sensitive in React, so this
        # cannot break a working build, but a `dangerouslySetInnerHtml` that a
        # future React version accepts is exactly the regression this is for.
        offenders = [
            relative
            for _path, relative, text in self._files()
            if re.search(r"dangerouslysetinnerhtml", text, re.IGNORECASE)
        ]
        self.assertEqual(offenders, [])

    def test_no_file_injects_html_through_a_raw_remark_rehype_plugin(self):
        offenders = [
            relative
            for _path, relative, text in self._files()
            if REHYPE_RAW_RE.search(text)
        ]
        self.assertEqual(
            offenders,
            [],
            "react-markdown escapes HTML unless a raw-HTML plugin is added; "
            "rehype-raw is the plugin that turns it off",
        )

    def test_known_source_files_are_covered_by_the_sweep(self):
        # If the walk silently stopped descending, the assertions above would
        # keep passing while covering nothing.
        covered = {relative for _path, relative, _text in self._files()}
        for expected in (
            os.path.join("frontend", "src", "components", "MessageBubble.tsx"),
            os.path.join("frontend", "src", "components", "MathContent.tsx"),
            os.path.join("frontend", "src", "app", "chat", "page.tsx"),
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, covered)


class MathContentIsNotARawHtmlSinkTests(unittest.TestCase):
    """The one component that renders model-generated text."""

    def test_the_file_exists(self):
        self.assertTrue(os.path.isfile(MATH_CONTENT), MATH_CONTENT)

    def _text(self):
        with open(MATH_CONTENT, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_it_does_not_reference_rehype_raw(self):
        text = self._text()
        self.assertNotIn("rehype-raw", text)
        self.assertNotIn("rehypeRaw", text)

    def test_it_does_not_use_dangerouslysetinnerhtml(self):
        self.assertNotIn("dangerouslySetInnerHTML", self._text())

    def test_it_does_not_use_an_injected_html_helper(self):
        # `rehypePlugins` plus a raw plugin is the indirect route to the same
        # hole; so is reading a component's `html` prop and pushing it through
        # an innerHTML-equivalent. Neither exists, and both are worth a
        # permanent red if they ever do.
        for marker in ("rawNode", "allowDangerousHtml", ".innerHTML"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self._text())


class NoJavaScriptToolchainTests(unittest.TestCase):
    """Documented, not aspirational: what this repository can and cannot run."""

    def test_node_modules_is_absent(self):
        # The reason this whole file is a text check. If a future environment
        # does have node_modules, the real checks belong in JS and this file
        # should be a backstop rather than the only guard.
        self.assertFalse(
            os.path.isdir(os.path.join(REPO_ROOT, "frontend", "node_modules"))
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
