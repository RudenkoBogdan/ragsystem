"""Executable contract tests for ``backend/security/logging_setup.py``.

Audit finding 16 was that the backend had no logging at all.  These tests
pin the three properties that make the new logger usable rather than merely
present:

* every application logger lives under the ``ragapp`` namespace, so one
  ``LOG_LEVEL`` setting governs the whole app;
* ``configure_logging()`` is genuinely idempotent -- a second call must not
  install a second handler, or every line is logged twice;
* it is importable with no project dependencies, because ``main.py`` calls it
  before anything else exists.

The last one is checked by AST over the module's own source, not by reading
it: ``from config import settings`` would be invisible in a smoke test and
would pull ``pydantic_settings`` into the boot path.

Note on global state: ``configure_logging`` mutates the process-wide ``ragapp``
logger and a module-level flag, so every test here snapshots and restores
both.  A leaked handler would make some other module's output appear twice for
the rest of the run -- a failure that is very hard to trace back here.

Run it with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible, stdlib only.
"""

from __future__ import annotations

import ast
import logging
import os
import sys
import unittest

# --- import bootstrap -------------------------------------------------------
try:
    from security import logging_setup
    from security.logging_setup import (
        DEFAULT_FORMAT,
        ROOT_LOGGER_NAME,
        configure_logging,
        get_logger,
    )
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from security import logging_setup
    from security.logging_setup import (
        DEFAULT_FORMAT,
        ROOT_LOGGER_NAME,
        configure_logging,
        get_logger,
    )


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGGING_SETUP_PATH = os.path.join(BACKEND_DIR, "security", "logging_setup.py")


class LoggingSetupIsolationMixin(unittest.TestCase):
    """Snapshot/restore of everything ``configure_logging`` touches."""

    def setUp(self):
        self.logger = logging.getLogger(ROOT_LOGGER_NAME)
        self._before = (
            list(self.logger.handlers),
            self.logger.level,
            self.logger.propagate,
            logging_setup._CONFIGURED,
        )

    def tearDown(self):
        handlers, level, propagate, configured = self._before
        self.logger.handlers[:] = handlers
        self.logger.setLevel(level)
        self.logger.propagate = propagate
        logging_setup._CONFIGURED = configured

    def reset(self):
        """Put the module back into its pre-``configure_logging`` state."""
        self.logger.handlers[:] = []
        self.logger.setLevel(logging.NOTSET)
        self.logger.propagate = True
        logging_setup._CONFIGURED = False


class GetLoggerTests(LoggingSetupIsolationMixin):
    def test_name_is_namespaced_under_ragapp(self):
        self.assertEqual(get_logger("chat.service").name, "ragapp.chat.service")

    def test_every_logger_shares_the_ragapp_prefix(self):
        for name in ("main", "auth.router", "papers.ingest", "main"):
            with self.subTest(name=name):
                self.assertTrue(get_logger(name).name.startswith("ragapp."))

    def test_two_calls_return_the_same_object(self):
        # logging.getLogger is a registry, not a factory; if this ever stops
        # being true, per-module level overrides stop working.
        self.assertIs(get_logger("chat.service"), get_logger("chat.service"))

    def test_it_is_a_real_logger(self):
        self.assertIsInstance(get_logger("main"), logging.Logger)

    def test_the_returned_logger_is_a_child_of_the_root_logger(self):
        root = logging.getLogger(ROOT_LOGGER_NAME)
        self.assertTrue(get_logger("main").name.startswith(root.name + "."))

    def test_a_non_string_name_does_not_raise(self):
        # Cheap tolerance: a logger name is never attacker-controlled here, but
        # the helper is called from module scope in several places and a
        # TypeError in a boot path is never worth the strictness.
        self.assertTrue(get_logger(7).name.startswith("ragapp."))


class ConfigureLoggingTests(LoggingSetupIsolationMixin):
    def test_default_arguments_install_one_handler(self):
        self.reset()
        configure_logging()
        self.assertEqual(len(self.logger.handlers), 1)

    def test_the_handler_writes_to_stderr(self):
        self.reset()
        configure_logging()
        handler = self.logger.handlers[0]
        self.assertIsInstance(handler, logging.StreamHandler)
        self.assertIs(handler.stream, sys.stderr)

    def test_the_handler_uses_the_documented_format(self):
        self.reset()
        configure_logging()
        self.assertEqual(self.logger.handlers[0].formatter._fmt, DEFAULT_FORMAT)

    def test_the_format_carries_time_level_and_name(self):
        for token in ("asctime", "levelname", "name", "message"):
            with self.subTest(token=token):
                self.assertIn(token, DEFAULT_FORMAT)

    def test_calling_it_twice_does_not_raise(self):
        self.reset()
        configure_logging()
        configure_logging()  # must not raise

    def test_calling_it_twice_does_not_add_a_second_handler(self):
        # The doubling bug: a reloader restart or a direct call from a test
        # would otherwise print every line twice for the rest of the process.
        self.reset()
        configure_logging()
        configure_logging()
        configure_logging()
        self.assertEqual(len(self.logger.handlers), 1)

    def test_it_sets_the_level_and_stops_propagation(self):
        self.reset()
        configure_logging("INFO")
        self.assertEqual(self.logger.level, logging.INFO)
        self.assertFalse(self.logger.propagate)

    def test_the_level_is_case_insensitive(self):
        for spelling in ("debug", "DEBUG", "Debug", " debug "):
            with self.subTest(spelling=spelling):
                self.reset()
                configure_logging(spelling)
                self.assertEqual(self.logger.level, logging.DEBUG)

    def test_standard_level_names_are_honoured(self):
        for name, expected in (
            ("DEBUG", logging.DEBUG),
            ("INFO", logging.INFO),
            ("WARNING", logging.WARNING),
            ("ERROR", logging.ERROR),
            ("CRITICAL", logging.CRITICAL),
        ):
            with self.subTest(name=name):
                self.reset()
                configure_logging(name)
                self.assertEqual(self.logger.level, expected)

    def test_an_invalid_level_falls_back_to_info(self):
        # A typo in .env must cost verbosity, not the boot.
        for bad in ("BANANA", "", "   ", None, 7, object(), "getLogger"):
            with self.subTest(bad=repr(bad)):
                self.reset()
                configure_logging(bad)
                self.assertEqual(self.logger.level, logging.INFO)

    def test_it_never_raises(self):
        # Logging is diagnostics. A diagnostic that can take the process down
        # is a worse outage than the one it was reporting.
        for bad in (None, object(), "NOT_A_LEVEL", 3.14, []):
            with self.subTest(bad=repr(bad)):
                self.reset()
                configure_logging(bad)  # must not raise

    def test_it_uses_only_one_handler_even_if_the_logger_already_has_one(self):
        # A handler installed by something else is left alone rather than
        # cleared: this function adds, it does not take over.
        self.reset()
        self.logger.addHandler(logging.NullHandler())
        configure_logging()
        self.assertEqual(len(self.logger.handlers), 2)
        self.assertTrue(
            any(getattr(h, "_ragapp_handler", False) for h in self.logger.handlers)
        )

    def test_a_second_call_does_not_reconfigure_the_level(self):
        # Documented consequence of the idempotence flag: the first call wins.
        self.reset()
        configure_logging("DEBUG")
        configure_logging("ERROR")
        self.assertEqual(self.logger.level, logging.DEBUG)

    def test_the_installed_handler_is_marked_so_it_can_be_recognised(self):
        self.reset()
        configure_logging()
        self.assertTrue(
            any(getattr(h, "_ragapp_handler", False) for h in self.logger.handlers)
        )


class LoggingSetupIsImportableTests(unittest.TestCase):
    """The "no project imports" promise, checked by AST rather than by review.

    ``main.py`` calls ``configure_logging`` before the settings object, the
    database engine or the routers exist.  If this module imported
    ``config`` (which imports ``pydantic_settings`` and ``dotenv``), the
    logger that is supposed to explain a boot failure would itself be a
    possible cause of one.
    """

    def _imported_modules(self):
        with open(LOGGING_SETUP_PATH, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=LOGGING_SETUP_PATH)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
        return names

    def test_it_does_not_import_config(self):
        imported = self._imported_modules()
        self.assertNotIn("config", imported)
        self.assertFalse(
            [m for m in imported if m.startswith("config.")],
            "logging_setup must not import the settings module",
        )

    def test_it_imports_nothing_from_this_package_but_itself(self):
        for name in self._imported_modules():
            with self.subTest(name=name):
                self.assertFalse(
                    name.startswith(("auth.", "papers.", "chat.", "vector.")),
                    "logging_setup must not import application modules",
                )

    def test_it_only_imports_the_stdlib(self):
        allowed = {"__future__", "logging", "sys", "typing"}
        top_level = {name.split(".")[0] for name in self._imported_modules()}
        self.assertTrue(
            top_level <= allowed,
            "logging_setup must stay stdlib-only, found %r" % sorted(top_level - allowed),
        )

    def test_the_module_file_exists_where_the_test_expects_it(self):
        # Guards against the path constants silently drifting apart, which
        # would turn every assertion above into a FileNotFoundError or, worse,
        # into a test that quietly reads nothing.
        self.assertTrue(os.path.isfile(LOGGING_SETUP_PATH))


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
