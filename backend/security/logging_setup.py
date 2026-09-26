"""Structured-enough logging for the application, with no project imports.

Audit finding 16 was that the backend had *no* logging at all: no startup
line, no auth-failure line, no ingest line, and therefore no way to
investigate findings 5-15 after the fact.  This is the smallest thing that
fixes that.

Two constraints shaped the API, and both are load-bearing:

1. **It must not import anything from this project.**  ``main.py`` calls
   :func:`configure_logging` as its very first statement, before the settings
   object, the database engine and the routers exist.  A module that did
   ``from config import settings`` would drag ``pydantic`` and
   ``pydantic_settings`` into the boot path -- and if *those* were the thing
   that failed, the logger that was supposed to tell you would be the thing
   that took the process down.  So the level is a parameter, and the caller
   reads it out of the settings it already has.
   ``ModuleHygieneTests`` in ``backend/tests/test_citations.py`` and
   ``LoggingSetupIsImportableTests`` in ``backend/tests/test_logging.py``
   enforce both halves of this by AST.

2. **It must never raise.**  Logging is diagnostics; a diagnostic that can
   crash the application is a worse outage than the one it was reporting.

Everything the application logs lives under the single ``ragapp`` logger
namespace, with one handler on ``ragapp`` itself and ``propagate = False`` so
records go to stderr exactly once instead of doubling up through the root
handler that ``uvicorn`` installs.

Run the tests with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible.
"""

from __future__ import annotations

import logging
import sys

#: The one format string. ``asctime`` so a line can be correlated with
#: anything else in the container log; ``name`` so it is obvious which
#: subsystem emitted it; no ``exc_info`` handling here because
#: ``logger.exception(...)`` already does the right thing at the call site.
DEFAULT_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: Everything this application logs is under this name, so the whole app can
#: be silenced (or raised) with a single call and cannot collide with a
#: library logger of the same name.
ROOT_LOGGER_NAME = "ragapp"

#: Attribute stamped on the handler we install, so a second call recognises
#: its own handler instead of counting ``logger.handlers``.
_HANDLER_MARK = "_ragapp_handler"

#: Idempotence flag.  ``main.py`` is imported once per process, but
#: ``--reload`` and the test suite both re-enter, and a duplicated handler
#: means every line is logged twice.
_CONFIGURED = False


def _resolve_level(level) -> int:
    """Turn a level name into a ``logging`` level integer.

    ``getattr(logging, "BANANA", logging.INFO)`` handles the misspelling, but
    not the case where the name resolves to something that is not a level at
    all (``logging.NOTSET`` is fine, ``logging.getLogger`` is not), so the
    type is checked as well.  A bad ``LOG_LEVEL`` in ``.env`` therefore costs
    you verbosity, not a boot failure.

    Note there is no integer shortcut: a non-string argument goes through
    ``str()`` exactly like a string one, so ``configure_logging(7)`` means
    "the level called 7", which does not exist, and falls back to INFO.
    """
    try:
        candidate = getattr(logging, str(level).strip().upper(), logging.INFO)
    except Exception:  # pragma: no cover - str() of a hostile object
        return logging.INFO
    if isinstance(candidate, int):
        return candidate
    return logging.INFO


def configure_logging(level: str = "INFO") -> None:
    """Install the single ``ragapp`` handler. Safe to call more than once.

    Idempotent: the second call is a no-op, so a reloader restart or a test
    that calls it directly cannot end up with two handlers and every message
    doubled.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    try:
        logger = logging.getLogger(ROOT_LOGGER_NAME)

        installed = False
        for handler in logger.handlers:
            if getattr(handler, _HANDLER_MARK, False):
                installed = True
                break
        if not installed:
            # stderr, not stdout: under docker-compose or any supervisor that
            # pipes stdout, stdout is where application data belongs and a log
            # line is not application data.
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(DEFAULT_FORMAT))
            setattr(handler, _HANDLER_MARK, True)
            logger.addHandler(handler)

        logger.setLevel(_resolve_level(level))
        # Do not also walk up into the root logger: uvicorn installs its own
        # handler there, and propagation would print every line twice.
        logger.propagate = False

        _CONFIGURED = True
    except Exception:  # pragma: no cover - diagnostics must not break boot
        # Swallow on purpose. See constraint (2) in the module docstring.
        pass


def get_logger(name: str) -> logging.Logger:
    """Return the application logger for ``name``, namespaced under ``ragapp``.

    ``get_logger("chat.service")`` -> ``ragapp.chat.service``, which is what
    makes ``LOG_LEVEL=DEBUG`` (or a targeted ``logging.getLogger(
    "ragapp.auth").setLevel(DEBUG)``) possible without a handler per module.
    """
    return logging.getLogger(ROOT_LOGGER_NAME + "." + str(name))
