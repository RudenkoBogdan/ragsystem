"""Executable contract tests for ``backend/security/policy.py``.

``policy.py`` is where the security-hardening series puts its boot gates, so
these tests are not decoration: the JWT-secret check is the only thing standing
between a forgotten ``.env`` and a deployment that signs tokens with a
constant published in this repository, and the CORS parser is what stops
``allow_origins=["*"]`` from coming back.

The module under test is pure and stdlib-only, so it is imported directly --
no install, no network, no database, no settings object.  Anything it needed
from ``pydantic`` or ``config`` would have made it untestable here, which is
precisely why it is not allowed to.

Run it with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible, stdlib only.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from typing import List, Optional

# --- import bootstrap -------------------------------------------------------
# Same shape as test_citations.py: `discover -t backend` already put backend/
# on sys.path; the fallback keeps a directly-executed module working.
try:
    from security.policy import (
        MAX_CORS_ORIGINS,
        MAX_PASSWORD_LENGTH,
        MAX_USERNAME_LENGTH,
        MIN_JWT_SECRET_LENGTH,
        MIN_PASSWORD_LENGTH,
        MIN_USERNAME_LENGTH,
        PolicyError,
        WEAK_JWT_SECRETS,
        jwt_secret_problem,
        parse_cors_origins,
        password_problem,
        password_too_long,
        resolve_page,
        username_problem,
    )
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from security.policy import (
        MAX_CORS_ORIGINS,
        MAX_PASSWORD_LENGTH,
        MAX_USERNAME_LENGTH,
        MIN_JWT_SECRET_LENGTH,
        MIN_PASSWORD_LENGTH,
        MIN_USERNAME_LENGTH,
        PolicyError,
        WEAK_JWT_SECRETS,
        jwt_secret_problem,
        parse_cors_origins,
        password_problem,
        password_too_long,
        resolve_page,
        username_problem,
    )


BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The literal that ships in ``config.py`` and the literal that ships in
#: ``.env.example``. Both are public in this repository. Read as text, never
#: interpolated into an assertion message, and never logged.
SHIPPED_DEFAULT_SECRET = "change-me-in-production"
ENV_EXAMPLE_SECRET = "change-me-to-a-random-secret-string"

#: A value that passes every rule: long, random-looking, not on the list.
GOOD_SECRET = "kQ7#vZ2!pR4m@Xw9$Lc8%tB5&nH3^jF6*yD1=gS0+"

COMMAND_RE = re.compile(r"`[^`]+`")


class PolicyErrorTests(unittest.TestCase):
    """The exception type the boot path catches."""

    def test_is_a_value_error(self):
        # main.py catches PolicyError specifically, but callers elsewhere may
        # already be handling ValueError; subclassing keeps both working.
        self.assertTrue(issubclass(PolicyError, ValueError))
        self.assertIsInstance(PolicyError("x"), ValueError)

    def test_keeps_its_message(self):
        self.assertEqual(str(PolicyError("bad origin list")), "bad origin list")


class ConstantsTests(unittest.TestCase):
    """The numbers later subtasks and operators will read off the docs."""

    def test_minimum_jwt_secret_length_is_32(self):
        self.assertEqual(MIN_JWT_SECRET_LENGTH, 32)

    def test_weak_secret_set_contains_both_shipped_placeholders(self):
        for placeholder in (SHIPPED_DEFAULT_SECRET, ENV_EXAMPLE_SECRET):
            self.assertIn(placeholder, WEAK_JWT_SECRETS)

    def test_weak_secret_set_is_a_frozenset_and_includes_the_empty_string(self):
        self.assertIsInstance(WEAK_JWT_SECRETS, frozenset)
        self.assertIn("", WEAK_JWT_SECRETS)

    def test_credential_and_cors_bounds(self):
        self.assertEqual(MIN_PASSWORD_LENGTH, 12)
        self.assertEqual(MAX_PASSWORD_LENGTH, 256)
        self.assertEqual(MIN_USERNAME_LENGTH, 3)
        self.assertEqual(MAX_USERNAME_LENGTH, 32)
        self.assertEqual(MAX_CORS_ORIGINS, 20)


class JwtSecretProblemTests(unittest.TestCase):
    """SEC-2: the boot gate."""

    def test_a_generated_secret_is_accepted(self):
        self.assertIsNone(jwt_secret_problem(GOOD_SECRET))
        self.assertGreaterEqual(len(GOOD_SECRET), MIN_JWT_SECRET_LENGTH)

    def test_exactly_the_minimum_length_is_accepted(self):
        self.assertIsNone(jwt_secret_problem("x" * MIN_JWT_SECRET_LENGTH))

    def test_one_character_short_is_rejected(self):
        problem = jwt_secret_problem("x" * (MIN_JWT_SECRET_LENGTH - 1))
        self.assertIsNotNone(problem)
        self.assertIn("shorter than 32 characters", problem)

    def test_shipped_default_is_rejected_as_a_placeholder(self):
        problem = jwt_secret_problem(SHIPPED_DEFAULT_SECRET)
        self.assertIsNotNone(problem)
        self.assertIn("placeholder", problem)

    def test_env_example_literal_is_rejected_as_a_placeholder(self):
        # The value a user copies out of .env.example and never edits is the
        # single most likely real-world deployment, so it is pinned here.
        problem = jwt_secret_problem(ENV_EXAMPLE_SECRET)
        self.assertIsNotNone(problem)
        self.assertIn("placeholder", problem)

    def test_placeholder_wins_over_the_length_message(self):
        # Both shipped placeholders are far shorter than 32 characters. If the
        # length check ran first the operator would be told to add characters
        # instead of being told they are running a public constant.
        for placeholder in (SHIPPED_DEFAULT_SECRET, "changeme", "secret"):
            with self.subTest(placeholder=placeholder):
                problem = jwt_secret_problem(placeholder)
                self.assertIn("placeholder", problem)
                self.assertNotIn("shorter than", problem)

    def test_empty_string_is_rejected_as_a_placeholder(self):
        self.assertIsNotNone(jwt_secret_problem(""))
        self.assertIn("placeholder", jwt_secret_problem(""))

    def test_placeholder_check_ignores_case_and_surrounding_space(self):
        for variant in ("SECRET", " secret ", "Change-Me-In-Production"):
            with self.subTest(variant=variant):
                self.assertIn("placeholder", jwt_secret_problem(variant))

    def test_none_is_reported_not_raised(self):
        # main.py calls this at import time with whatever Settings produced. A
        # TypeError here would be a traceback instead of a one-line refusal.
        problem = jwt_secret_problem(None)
        self.assertIsNotNone(problem)
        self.assertIn("JWT_SECRET", problem)

    def test_allow_insecure_short_circuits_every_rule(self):
        for value in (None, "", SHIPPED_DEFAULT_SECRET, ENV_EXAMPLE_SECRET, "x"):
            with self.subTest(value=repr(value)):
                self.assertIsNone(jwt_secret_problem(value, allow_insecure=True))

    def test_allow_insecure_is_keyword_only(self):
        # It has to be: a positional escape hatch is one refactor away from
        # being the default.
        with self.assertRaises(TypeError):
            jwt_secret_problem(GOOD_SECRET, True)  # noqa: FBT003

    def test_no_message_ever_contains_the_secret(self):
        # Only values that are actually rejected, because a valid one has no
        # message at all and "no message" trivially contains no secret.
        secret = "topsecretvalue"
        for value in (secret, "  " + secret, secret.upper(), secret[:20]):
            with self.subTest(value=repr(value)):
                problem = jwt_secret_problem(value)
                self.assertIsNotNone(problem)
                self.assertNotIn(secret.lower(), problem.lower())
                # It must describe the rule, never hand back a value to copy.
                self.assertNotIn("=", problem)

    def test_a_long_repeated_secret_is_accepted_not_warned_about(self):
        self.assertIsNone(jwt_secret_problem("topsecretvalue" * 4))

    def test_the_missing_secret_reason_names_the_variable(self):
        # The one case where the reason has to stand on its own, because it is
        # the only branch that cannot start with "is ..." usefully.
        self.assertIn("JWT_SECRET", jwt_secret_problem(None))

    def test_the_rejection_message_says_how_to_generate_a_replacement(self):
        problem = jwt_secret_problem(SHIPPED_DEFAULT_SECRET)
        self.assertIsNotNone(problem)
        self.assertTrue(COMMAND_RE.search(problem), "must show a generation command")


class UsernameProblemTests(unittest.TestCase):
    """SEC-3: length and whitespace only -- never a character class."""

    def test_ordinary_username_is_accepted(self):
        self.assertIsNone(username_problem("alice"))

    def test_minimum_length_boundary(self):
        self.assertIsNone(username_problem("a" * MIN_USERNAME_LENGTH))
        self.assertIsNotNone(username_problem("a" * (MIN_USERNAME_LENGTH - 1)))

    def test_maximum_length_boundary(self):
        self.assertIsNone(username_problem("a" * MAX_USERNAME_LENGTH))
        self.assertIsNotNone(username_problem("a" * (MAX_USERNAME_LENGTH + 1)))

    def test_empty_and_none_are_rejected(self):
        self.assertIsNotNone(username_problem(""))
        self.assertIsNotNone(username_problem(None))

    def test_any_whitespace_is_rejected(self):
        for bad in ("two words", " leading", "trailing ", "tab\there", "new\nline"):
            with self.subTest(bad=repr(bad)):
                problem = username_problem(bad)
                self.assertIsNotNone(problem)
                self.assertIn("whitespace", problem)

    def test_there_is_no_character_class_rule(self):
        # A display name that rejects an underscore, a dot or a non-ASCII
        # letter produces support tickets, not security. These are in bounds.
        for ok in ("user_name", "a.b", "Ünïcödé", "123", "-_-", "a" * 32):
            with self.subTest(ok=ok):
                self.assertIsNone(username_problem(ok))

    def test_messages_name_the_variable_not_the_value(self):
        problem = username_problem("a b")
        self.assertIsNotNone(problem)
        self.assertIn("Username", problem)
        self.assertNotIn("a b", problem)


class PasswordProblemTests(unittest.TestCase):
    """SEC-3: length is the only rule, and that is asserted on purpose."""

    def test_empty_is_rejected(self):
        self.assertEqual(password_problem(""), "Password must not be empty.")

    def test_none_is_rejected(self):
        self.assertEqual(password_problem(None), "Password must not be empty.")

    def test_short_whitespace_only_is_rejected_as_empty(self):
        # Too short to clear the length floor, so there is nothing to keep.
        self.assertEqual(
            password_problem("   "), "Password must not be empty."
        )

    def test_too_short_is_rejected(self):
        problem = password_problem("x" * (MIN_PASSWORD_LENGTH - 1))
        self.assertEqual(
            problem, "Password must be at least %d characters." % MIN_PASSWORD_LENGTH
        )

    def test_minimum_length_boundary(self):
        self.assertIsNone(password_problem("x" * MIN_PASSWORD_LENGTH))

    def test_maximum_length_boundary(self):
        self.assertIsNone(password_problem("x" * MAX_PASSWORD_LENGTH))
        problem = password_problem("x" * (MAX_PASSWORD_LENGTH + 1))
        self.assertEqual(
            problem, "Password must be at most %d characters." % MAX_PASSWORD_LENGTH
        )

    def test_twelve_spaces_is_a_valid_password(self):
        # DELIBERATE. Length is the only rule: twelve spaces is long enough,
        # so it is accepted. A composition or "not all whitespace" rule would
        # be a policy about the user's memory, not about the attacker's
        # dictionary, and it is not this function's job to legislate.
        self.assertEqual(" " * 12, " " * MIN_PASSWORD_LENGTH)
        self.assertIsNone(password_problem(" " * 12))

    def test_a_passphrase_of_real_words_is_accepted(self):
        self.assertIsNone(password_problem("correct horse battery"))

    def test_a_single_repeated_character_is_accepted(self):
        # Same argument as the spaces above, and the same reason it is pinned.
        self.assertIsNone(password_problem("x" * 12))

    def test_surrounding_whitespace_does_not_invalidate_a_long_password(self):
        self.assertIsNone(password_problem("  " + "x" * 12 + "  "))

    def test_message_never_echoes_the_password(self):
        # Only rejected values: a valid one has no message, which is the whole
        # point of the length-is-the-only-rule decision above.
        for value in ("hunter2hunt", "p" * 300, " " * 4):
            with self.subTest(value=repr(value)):
                problem = password_problem(value)
                self.assertIsNotNone(problem)
                self.assertNotIn(value, problem)
                self.assertIn("Password", problem)


class PasswordTooLongTests(unittest.TestCase):
    """The boolean form, separate from the message."""

    def test_over_the_limit(self):
        self.assertTrue(password_too_long("x" * (MAX_PASSWORD_LENGTH + 1)))

    def test_at_the_limit_is_not_too_long(self):
        self.assertFalse(password_too_long("x" * MAX_PASSWORD_LENGTH))

    def test_none_is_tolerated(self):
        self.assertFalse(password_too_long(None))

    def test_non_strings_are_tolerated(self):
        for value in (7, [], {}, object()):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(password_too_long(value))

    def test_agrees_with_password_problem(self):
        for length in (0, 1, 11, 12, 255, 256, 257, 1000):
            with self.subTest(length=length):
                value = "x" * length
                self.assertEqual(
                    password_too_long(value),
                    password_problem(value) is not None
                    and "at most" in (password_problem(value) or ""),
                )


class ParseCorsOriginsTests(unittest.TestCase):
    """The allow-list that replaced ``allow_origins=["*"]``."""

    def test_blank_input_is_no_origins(self):
        # main.py reads [] as "install no middleware", which is the right
        # behaviour for a same-origin deployment.
        for raw in ("", "   ", ",", " , , "):
            with self.subTest(raw=repr(raw)):
                self.assertEqual(parse_cors_origins(raw, allow_credentials=False), [])

    def test_whitespace_around_entries_is_stripped_and_empties_dropped(self):
        self.assertEqual(
            parse_cors_origins(
                "http://localhost:3000, http://x ,", allow_credentials=False
            ),
            ["http://localhost:3000", "http://x"],
        )

    def test_a_single_origin(self):
        self.assertEqual(
            parse_cors_origins("https://app.example.com", allow_credentials=False),
            ["https://app.example.com"],
        )

    def test_wildcard_without_credentials_is_permitted(self):
        # It is the deployment's call; the point of the check is that the
        # operator has to make it consciously.
        self.assertEqual(parse_cors_origins("*", allow_credentials=False), ["*"])

    def test_wildcard_with_credentials_is_refused(self):
        # The exact state the old middleware was hard-coded to.
        with self.assertRaises(PolicyError) as caught:
            parse_cors_origins("*", allow_credentials=True)
        self.assertIn("CORS_ALLOW_ORIGINS", str(caught.exception))
        self.assertIn("CORS_ALLOW_CREDENTIALS", str(caught.exception))

    def test_wildcard_among_other_origins_with_credentials_is_refused(self):
        with self.assertRaises(PolicyError):
            parse_cors_origins(
                "https://app.example.com, *, http://localhost:3000",
                allow_credentials=True,
            )

    def test_at_the_cap_is_accepted(self):
        raw = ",".join("https://o%d.example.com" % n for n in range(MAX_CORS_ORIGINS))
        self.assertEqual(
            len(parse_cors_origins(raw, allow_credentials=False)), MAX_CORS_ORIGINS
        )

    def test_over_the_cap_is_refused(self):
        raw = ",".join("https://o%d.example.com" % n for n in range(MAX_CORS_ORIGINS + 1))
        with self.assertRaises(PolicyError) as caught:
            parse_cors_origins(raw, allow_credentials=False)
        message = str(caught.exception)
        self.assertIn(str(MAX_CORS_ORIGINS), message)

    def test_the_cap_counts_entries_not_commas(self):
        # 21 commas but 20 real origins is fine; a trailing comma is not a
        # reason to refuse to start.
        raw = ",".join("https://o%d.example.com" % n for n in range(MAX_CORS_ORIGINS)) + ","
        self.assertEqual(
            len(parse_cors_origins(raw, allow_credentials=False)), MAX_CORS_ORIGINS
        )

    def test_allow_credentials_is_keyword_only(self):
        with self.assertRaises(TypeError):
            parse_cors_origins("https://a.example.com", True)  # noqa: FBT003

    def test_none_is_treated_as_blank(self):
        self.assertEqual(parse_cors_origins(None, allow_credentials=False), [])

    def test_the_input_string_is_not_mutated(self):
        raw = "https://a.example.com, https://b.example.com"
        parse_cors_origins(raw, allow_credentials=True)
        self.assertEqual(raw, "https://a.example.com, https://b.example.com")

    def test_duplicate_origins_are_preserved_verbatim(self):
        # Not de-duplicated on purpose: parse_cors_origins is a parser, and
        # silently rewriting an operator's configuration is how a typo becomes
        # invisible. Order and count are exactly what they wrote.
        self.assertEqual(
            parse_cors_origins(
                "https://a.example.com,https://a.example.com",
                allow_credentials=False,
            ),
            ["https://a.example.com", "https://a.example.com"],
        )


class ResolvePageTests(unittest.TestCase):
    """SEC-6: the (limit, offset) validator the list endpoints will use."""

    def test_absent_limit_falls_back_to_the_endpoint_default(self):
        self.assertEqual(resolve_page(None, 0, max_limit=200, default_limit=50), (50, 0))

    def test_a_supplied_limit_is_honoured(self):
        self.assertEqual(resolve_page(10, 5, max_limit=200, default_limit=50), (10, 5))

    def test_both_bounds_are_inclusive(self):
        self.assertEqual(resolve_page(1, 0, max_limit=200, default_limit=50), (1, 0))
        self.assertEqual(
            resolve_page(200, 0, max_limit=200, default_limit=50), (200, 0)
        )

    def test_limit_of_zero_is_refused(self):
        # "return nothing" must be expressed as an empty list client-side, not
        # as a magic limit value the endpoint has to special-case.
        with self.assertRaises(PolicyError):
            resolve_page(0, 0, max_limit=200, default_limit=50)

    def test_negative_and_over_maximum_limits_are_refused(self):
        for bad in (-1, -200, 201, 10_000):
            with self.subTest(limit=bad):
                with self.assertRaises(PolicyError):
                    resolve_page(bad, 0, max_limit=200, default_limit=50)

    def test_the_refusal_names_both_the_variable_and_the_bound(self):
        with self.assertRaises(PolicyError) as caught:
            resolve_page(201, 0, max_limit=200, default_limit=50)
        message = str(caught.exception)
        self.assertIn("limit", message)
        self.assertIn("200", message)

    def test_negative_offset_is_refused(self):
        with self.assertRaises(PolicyError) as caught:
            resolve_page(10, -1, max_limit=200, default_limit=50)
        self.assertIn("offset", str(caught.exception))

    def test_offset_zero_and_large_offsets_are_accepted(self):
        self.assertEqual(resolve_page(10, 0, max_limit=200, default_limit=50)[1], 0)
        self.assertEqual(
            resolve_page(10, 10_000, max_limit=200, default_limit=50)[1], 10_000
        )

    def test_absent_offset_is_treated_as_zero(self):
        self.assertEqual(
            resolve_page(None, None, max_limit=200, default_limit=50), (50, 0)
        )

    def test_returns_a_two_tuple(self):
        result: tuple = resolve_page(10, 5, max_limit=200, default_limit=50)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_bounds_are_keyword_only(self):
        with self.assertRaises(TypeError):
            resolve_page(10, 0, 200, 50)  # noqa: FBT003


class NeverLeaksTheSecretTests(unittest.TestCase):
    """A cross-cutting check, run against every reason string the module can
    produce, so a future edit that adds ``% secret`` to a format string fails
    here rather than in a startup log on someone's server."""

    REASONS: List[Optional[str]] = [
        jwt_secret_problem(None),
        jwt_secret_problem(""),
        jwt_secret_problem(SHIPPED_DEFAULT_SECRET),
        jwt_secret_problem(ENV_EXAMPLE_SECRET),
        jwt_secret_problem("short"),
        username_problem("bad name"),
        username_problem(""),
        password_problem(""),
        password_problem("short"),
        password_problem("x" * 300),
    ]

    def test_every_reason_is_a_non_empty_string(self):
        for reason in self.REASONS:
            with self.subTest(reason=reason):
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip())

    def test_no_reason_contains_a_submitted_value(self):
        needles = (
            SHIPPED_DEFAULT_SECRET,
            ENV_EXAMPLE_SECRET,
            "bad name",
            "hunter2hunt",
        )
        for reason in self.REASONS:
            for needle in needles:
                with self.subTest(reason=reason, needle=needle):
                    self.assertNotIn(needle, reason or "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
