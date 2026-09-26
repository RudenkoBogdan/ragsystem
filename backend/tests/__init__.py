"""First executable test suite for this repository (stdlib ``unittest`` only).

Why this bootstrap exists
-------------------------
``backend/`` uses **flat, top-level imports** (``from chat.citations import
...``, ``from config import settings``) and is expected to run with
``cwd=backend``.  Python does not put a package's parent directory on
``sys.path`` by itself, so an absolute import like ``chat.citations`` only
resolves when ``backend/`` happens to be on the path.

``unittest discover`` normally handles this for us when it is pointed at the
right top-level directory::

    # from the repository root
    python3 -m unittest discover -s backend/tests -t backend -v

but the suite is also expected to work from inside ``backend/`` and when a
single module is executed directly.  ``ensure_backend_on_path()`` therefore
computes the directory from ``__file__`` (not from ``os.getcwd()``) and makes
it importable in every one of those cases, without touching ``PYTHONPATH`` or
installing anything.

Constraints honoured here
-------------------------
* No third-party imports.  Not ``fastapi``, ``sqlalchemy``, ``pydantic``,
  ``aiohttp``, ``chromadb``, ``sentence_transformers``, ``arxiv`` or ``fitz`` --
  none of those are installed in this environment, and importing one would make
  the suite unrunnable.  Only the stdlib is used.
* Python 3.9 compatible syntax (the local interpreter is 3.9.6; the project
  targets 3.11).
* Nothing is written to disk, no network, no user data.
"""

from __future__ import annotations

import os
import sys

#: Absolute path of ``backend/`` -- derived from this file, never from the cwd.
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Absolute path of this test package.
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def ensure_backend_on_path() -> str:
    """Make ``backend/`` importable so the project's flat imports resolve.

    Idempotent, so it is safe to call from every test module (which is what
    makes a module runnable on its own, e.g. ``python3 backend/tests/
    test_citations.py``, where this package's ``__init__`` is never executed).

    Returns ``BACKEND_DIR``.
    """
    if BACKEND_DIR not in sys.path:
        sys.path.insert(0, BACKEND_DIR)
    return BACKEND_DIR


# Applied on import, so simply importing ``tests`` is enough.
ensure_backend_on_path()
