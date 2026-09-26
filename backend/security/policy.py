"""Pure, stdlib-only policy checks for the security-hardening series.

This module is deliberately dependency-free.  It is imported by
``backend/main.py`` *before* the database engine, the routers and the
application object are built, because two of its checks are boot gates:

* ``jwt_secret_problem`` refuses to let the process start with the shipped
  placeholder ``JWT_SECRET`` (the fix for audit finding 13), and
* ``parse_cors_origins`` refuses a ``CORS_ALLOW_ORIGINS`` value that would
  re-open the ``allow_origins=["*"]`` hole the middleware used to have.

A boot gate that imports ``pydantic`` or ``config`` would be a boot gate that
can itself fail in a way nobody can diagnose, and one that could not be
exercised by the repository's stdlib-only test suite.  So: ``re`` and
``typing`` and nothing else.  ``ModuleHygieneTests`` in
``backend/tests/test_citations.py`` enforces that by AST, not by review.

None of the messages produced here ever contain the value being checked.
They name the *variable* and the *rule*, because these strings end up in
startup logs and in API error bodies.

Run the tests with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# --- SEC-2: JWT secret ------------------------------------------------------

#: HS256 keys shorter than this are not worth signing anything with.
MIN_JWT_SECRET_LENGTH = 32

#: Values that are publicly readable in this repository (the ``config.py``
#: default and the ``.env.example`` sample) plus the usual stand-ins.  These
#: are rejected even when long enough to satisfy the length rule, so that a
#: deployment cannot "upgrade" the default into a longer placeholder.
WEAK_JWT_SECRETS = frozenset(
    {
        "change-me-in-production",
        "change-me-to-a-random-secret-string",
        "changeme",
        "secret",
        "password",
        "jwt-secret",
        "",
    }
)

# --- SEC-3: credential length policy ----------------------------------------

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
MIN_USERNAME_LENGTH = 3
MAX_USERNAME_LENGTH = 32

# --- CORS -------------------------------------------------------------------

#: A comma-separated allow-list is a configuration mistake waiting to happen
#: long before it is a security problem; cap it so a typo cannot explode into
#: thousands of origins.
MAX_CORS_ORIGINS = 20

#: ``\s`` rather than ``str.isspace()`` so the rule and the test can state the
#: same thing literally.
_WHITESPACE_RE = re.compile(r"\s")


class PolicyError(ValueError):
    """A configuration or request value that the policy refuses.

    A ``ValueError`` subclass so that a caller which already handles
    ``ValueError`` (pydantic validators, for example) keeps working, while
    ``main.py`` can still catch exactly this and turn it into a clean
    ``SystemExit`` instead of a traceback.
    """


def jwt_secret_problem(secret, *, allow_insecure: bool = False) -> Optional[str]:
    """Describe why ``secret`` is unusable as an HS256 key, or return ``None``.

    Returns a human-readable reason *never* containing the secret itself.

    ``allow_insecure=True`` short-circuits every check and returns ``None``:
    it is the documented local-development escape hatch, and it is
    deliberately a keyword-only flag so that a call site which enables it is
    visible in a diff.

    The placeholder check runs *before* the length check on purpose.  The
    shipped default is 23 characters long, so checking length first would
    report the more actionable problem -- "this is the placeholder that ships
    in the public repository" -- as merely "too short", and the operator would
    go looking for a length knob that does not exist.
    """
    if allow_insecure:
        return None

    if secret is None:
        # Tolerated rather than raised: a missing value is a *configuration*
        # problem, and the caller is a boot path that must be able to print
        # one clear line instead of a TypeError traceback.
        return (
            "is not set. Set JWT_SECRET to at least %d random characters"
            % MIN_JWT_SECRET_LENGTH
        )

    if not isinstance(secret, str):
        return "is not a string."

    # Normalised only for the placeholder comparison, so that " SECRET " is
    # still recognised as the stand-in it is.
    if secret.strip().lower() in WEAK_JWT_SECRETS:
        return (
            "is still a known placeholder value. Generate a random one with "
            "`python3 -c \"import secrets;print(secrets.token_urlsafe(48))\"`"
        )

    if len(secret) < MIN_JWT_SECRET_LENGTH:
        return (
            "is shorter than %d characters (%d given). Generate one with "
            "`python3 -c \"import secrets;print(secrets.token_urlsafe(48))\"`"
            % (MIN_JWT_SECRET_LENGTH, len(secret))
        )

    return None


# --- SEC-3: credential policy -----------------------------------------------


def username_problem(username) -> Optional[str]:
    """Return why ``username`` is unacceptable, or ``None``.

    Length (3..32 inclusive) and "contains no whitespace" are the only rules.
    There is deliberately no character-class rule: usernames are not secrets,
    an over-tight pattern produces lock-out support tickets rather than
    security, and a display name that rejects a unicode letter or an underscore
    is a worse product than one that accepts it.
    """
    if username is None:
        username = ""
    if not isinstance(username, str):
        return "Username must be text."

    if len(username) < MIN_USERNAME_LENGTH:
        return "Username must be at least %d characters." % MIN_USERNAME_LENGTH
    if len(username) > MAX_USERNAME_LENGTH:
        return "Username must be at most %d characters." % MAX_USERNAME_LENGTH
    if _WHITESPACE_RE.search(username):
        return "Username must not contain whitespace."

    return None


def password_problem(password) -> Optional[str]:
    """Return why ``password`` is unacceptable, or ``None``.

    **Length is the only rule.**  Twelve spaces is a valid password here, and
    ``test_policy.py`` asserts that deliberately.  A passphrase is not a
    dictionary word, and no composition rule ("one upper, one digit, one
    symbol") reliably improves entropy -- it reliably produces `Password1!`,
    which is in every cracking dictionary ever published.  A length floor plus
    a hashing function that is deliberately slow is the part that matters;
    the floor is here to reject the empty and one-character passwords the
    register endpoint used to accept, not to adjudicate taste.

    The one non-length rule is emptiness, and even that is deliberately
    narrow: a value that is empty, or that is nothing *but* whitespace and
    too short to be worth anything anyway, is rejected up front.  Whitespace
    that clears the length floor is left alone -- see above.
    """
    if password is None:
        return "Password must not be empty."
    if not isinstance(password, str):
        return "Password must be text."

    if password == "":
        return "Password must not be empty."

    if not password.strip() and len(password) < MIN_PASSWORD_LENGTH:
        # Whitespace-only, and shorter than the floor it can never reach.
        return "Password must not be empty."

    if len(password) < MIN_PASSWORD_LENGTH:
        return "Password must be at least %d characters." % MIN_PASSWORD_LENGTH
    if len(password) > MAX_PASSWORD_LENGTH:
        return "Password must be at most %d characters." % MAX_PASSWORD_LENGTH

    return None


def password_too_long(password) -> bool:
    """Whether ``password`` exceeds :data:`MAX_PASSWORD_LENGTH`.

    Split out from :func:`password_problem` because argon2 has its own,
    different, hard limit and a caller needs to be able to ask the question
    without also getting a message.  Tolerant of ``None`` (and of any other
    non-string) so a request body that is missing the field cannot turn a
    length check into an exception.
    """
    if not isinstance(password, str):
        return False
    return len(password) > MAX_PASSWORD_LENGTH


# --- CORS -------------------------------------------------------------------


def parse_cors_origins(raw: str, *, allow_credentials: bool) -> List[str]:
    """Parse a comma-separated origin allow-list.

    Entries are stripped and empty ones dropped, so a trailing comma or a
    blank line in ``.env`` is not a startup failure.

    Raises :class:`PolicyError` when the list is longer than
    :data:`MAX_CORS_ORIGINS`, and when it contains ``"*"`` while
    ``allow_credentials`` is true -- that combination is exactly the
    "wildcard origin with cookies" state, which is either silently rejected by
    the browser or, worse, honoured by a proxy in front of us.

    Empty or blank input returns ``[]``, which ``main.py`` reads as "install
    no CORS middleware at all": a same-origin deployment needs none, and
    installing a middleware that allows nothing only produces confusing
    preflight failures in the log.
    """
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        raise PolicyError("CORS_ALLOW_ORIGINS must be a comma-separated string.")

    origins = [part.strip() for part in raw.split(",")]
    origins = [origin for origin in origins if origin]

    if len(origins) > MAX_CORS_ORIGINS:
        raise PolicyError(
            "CORS_ALLOW_ORIGINS lists %d origins; at most %d are allowed."
            % (len(origins), MAX_CORS_ORIGINS)
        )

    if allow_credentials and "*" in origins:
        raise PolicyError(
            'CORS_ALLOW_ORIGINS contains "*" while CORS_ALLOW_CREDENTIALS is '
            "true. Either list the exact browser origins, or set "
            "CORS_ALLOW_CREDENTIALS=false."
        )

    return origins


# --- SEC-6: bounded list endpoints ------------------------------------------


def resolve_page(
    limit: Optional[int],
    offset: int,
    *,
    max_limit: int,
    default_limit: int,
) -> Tuple[int, int]:
    """Validate a ``(limit, offset)`` query pair and return it normalised.

    ``limit=None`` means "the caller did not ask", so the endpoint's own
    default applies -- an absent limit is not an error, an out-of-range one
    is, because the whole point of the bound is that a client cannot ask for
    the entire table.

    Raises :class:`PolicyError` for a limit outside ``1..max_limit`` and for a
    negative offset.
    """
    effective_limit = default_limit if limit is None else limit
    effective_offset = 0 if offset is None else offset

    if effective_limit < 1 or effective_limit > max_limit:
        raise PolicyError(
            "limit must be between 1 and %d (got %r)." % (max_limit, effective_limit)
        )
    if effective_offset < 0:
        raise PolicyError("offset must be 0 or greater (got %r)." % (effective_offset,))

    return (effective_limit, effective_offset)
