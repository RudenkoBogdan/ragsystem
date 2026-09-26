"""Outbound-request guard for the user-supplied LLM ``base_url`` (SEC-1).

Audit finding 12 was that any registered account could choose where the
server's own HTTP client connects: ``base_url`` came straight out of the
request body, was only ``localhost``-rewritten inside Docker, and was then
used verbatim for an authenticated ``POST`` to
``{base_url}/chat/completions``.  On a non-200 the response body was
interpolated into the exception message, which the router put straight into
the ``done`` event -- so the same feature was a *read* oracle against
``169.254.169.254`` (cloud instance metadata), Redis, or anything else
listening on the LAN.

This module is the half of the fix that can be proved tonight: a pure,
stdlib-only parser, address classifier and redactor.  It has **no** project
imports at all -- not ``config``, not ``fastapi``, not ``aiohttp`` -- so
``backend/tests/test_netguard.py`` can exercise every rule on a bare
Python with nothing installed, and so a guard that decides where a request
goes is not itself standing on the import graph it is protecting.  The
asynchronous half (resolving the host off the event loop) lives in
``chat/service.py::_guarded_endpoint``, which is the single choke point.

Three rules shape every function here:

1. **Never fail open.**  Anything this module cannot understand is
   rejected (:class:`NetGuardError`), not passed through.  The caller turns
   an unexpected exception inside the guard into a rejection too, because a
   guard that raises an unexpected error and lets the request continue has
   failed at the only job it has.

2. **Never leak.**  ``NetGuardError.client_message()`` and
   ``UpstreamProviderError.client_message()`` are looked up by code in a
   fixed table and never interpolate anything.  The ``reason`` string, the
   resolved address and the provider's response body are for the operator's
   log, reached through :func:`redact_url` / :func:`redact_body` and
   :func:`public_error_message` respectively -- never through ``str(exc)``.

3. **Never lend a server-side secret to a caller-chosen host.**  The
   provider URL is user-supplied; the API key is not.  A rule about *where*
   the request may go says nothing about *what it carries*, so the guard now
   also answers "may this request carry the operator's credential?", in
   :func:`may_use_operator_credential`.  An unknown origin is treated as
   untrusted, in keeping with rule 1.

The address vocabulary is exactly nine categories, listed in
:data:`ALWAYS_BLOCKED` and produced by :func:`classify_address`:
``public``, ``private``, ``loopback``, ``link_local``, ``multicast``,
``unspecified``, ``reserved``, ``carrier_grade_nat`` and ``invalid``.

Run the tests with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible (``typing.List`` and friends, not PEP 585 builtins).
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import List, NamedTuple, Sequence
from urllib.parse import urlsplit, urlunsplit

# --- constants ---------------------------------------------------------------

#: Where a container reaches the host machine. Replaces a whole-URL
#: ``re.sub`` that used to rewrite the substring "localhost" *anywhere* in
#: the string, including in a path or a query (see :func:`parse_endpoint`).
DOCKER_HOSTNAME = "host.docker.internal"

#: Only these two. Rejects ``file:``, ``gopher:``, ``ftp:`` and -- because a
#: bare ``localhost:11434`` parses as scheme ``localhost`` -- the schemeless
#: form people actually type.
ALLOWED_SCHEMES = ("http", "https")

DEFAULT_PORTS = {"http": 80, "https": 443}

#: A base URL is operator- or user-supplied configuration, never a document.
#: The cap exists so a hostile value cannot make the parser do real work.
MAX_URL_LENGTH = 2048

#: Rewritten to :data:`DOCKER_HOSTNAME` when running inside a container.
DOCKER_REWRITE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

#: RFC 3986 characters, and nothing else. This single pattern is what kills a
#: space, a CR/LF header injection, a NUL byte, a backslash, and every
#: non-ASCII codepoint (including a full-width colon homoglyph) in one test.
SAFE_URL_CHARS = re.compile(r"\A[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+\Z")

#: How much of a provider's error body may reach the log.
BODY_LOG_LIMIT = 200

#: RFC 6598 carrier-grade NAT, ``100.64.0.0/10``.  Not an RFC 1918 range and
#: *not* in the standard library's private list, so ``ipaddress`` reports a
#: CGNAT address as public -- but it is a shared space owned by the mobile
#: network operator and it fronts the subscriber network, so a name that
#: resolves into it is reaching into carrier infrastructure, not the Internet.
CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")

#: Categories that stay forbidden *even* when ``LLM_ALLOW_PRIVATE_HOSTS`` is
#: on. The flag exists to let a developer point the app at a private LAN
#: Ollama; it is not a switch that re-opens the cloud metadata endpoint.
ALWAYS_BLOCKED = frozenset(
    {
        "link_local",
        "multicast",
        "unspecified",
        "reserved",
        "invalid",
        "carrier_grade_nat",
    }
)

#: Address categories a *request-supplied* URL may resolve to, with the
#: default (flag off) and permissive (flag on) sets. ``carrier_grade_nat`` is
#: deliberately in neither: :data:`ALWAYS_BLOCKED` covers it, so listing it
#: here as well would suggest the permissive flag can reach it.
_ALLOWED_CATEGORIES = frozenset({"public"})
_ALLOWED_CATEGORIES_PRIVATE_OK = frozenset({"public", "private", "loopback"})

#: The provider this module assumes when it is not told, mirroring
#: ``chat/service.py::_resolve_provider``. Repeated here rather than imported
#: because a guard that has to import the thing it guards is no longer a
#: standalone check.
DEFAULT_PROVIDER = "openrouter"

#: Providers whose branch in ``_resolve_endpoint`` falls back to a *server-side*
#: key when the request did not supply one. Only the openrouter branch does:
#: the ollama branch attaches a bearer header solely when the request itself
#: provided ``api_key``, so nothing server-side is ever at stake there. See
#: :func:`may_use_operator_credential`, whose truth table tests pin that
#: asymmetry rather than leaving it to a reading of another file.
OPERATOR_CREDENTIAL_PROVIDERS = frozenset({DEFAULT_PROVIDER})

# Log-only redaction patterns. Applied after the printable-ASCII pass, so a
# body cannot smuggle a newline through them either.
_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_API_KEY_RE = re.compile(r"sk-[A-Za-z0-9_\-]{8,}")
_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+")


# --- errors ------------------------------------------------------------------


class NetGuardError(ValueError):
    """An outbound destination the guard refuses.

    A ``ValueError`` subclass so a caller that already handles bad input
    keeps working.  The split between the two attributes is deliberate:

    * ``.code`` -- a stable machine-readable token used to look up
      :meth:`client_message` and to make log lines greppable.
    * ``.reason`` -- operator-facing detail, *log only*.  It can contain a
      resolved address or a scheme; it must never be sent to a client.
    """

    #: The complete set of codes. ``unknown`` is the fallback for anything
    #: a future change invents, so a new code degrades to a generic refusal
    #: rather than to a ``KeyError`` or, worse, to a leaked reason string.
    _CLIENT_MESSAGES = {
        "bad_scheme": (
            "The language model URL must start with http:// or https://."
        ),
        "userinfo_not_allowed": (
            "The language model URL must not contain a username or password."
        ),
        "bad_port": (
            "The language model URL has an invalid port number."
        ),
        "malformed_url": (
            "The language model URL is not a valid address."
        ),
        # Named verbatim because it is the exact variable an operator has to
        # set, and a refusal that does not name its own escape hatch is a
        # support ticket.
        "blocked_address": (
            "The language model URL resolves to a network address this server "
            "is not allowed to reach. Set LLM_ALLOW_PRIVATE_HOSTS=true on the "
            "server to allow a private or local address."
        ),
        "dns_failure": (
            "The language model host could not be resolved."
        ),
        "dns_empty": (
            "The language model host resolved to no usable address."
        ),
        "unknown": (
            "The answer could not be generated."
        ),
    }

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason

    def client_message(self) -> str:
        """A fixed sentence for the API response. Never contains ``reason``."""
        return self._CLIENT_MESSAGES.get(self.code, self._CLIENT_MESSAGES["unknown"])


class UpstreamProviderError(RuntimeError):
    """The LLM provider answered with a non-200.

    ``str(exc)`` is a fixed sentence, so the historical
    ``RuntimeError(f"LLM API error {status} ({url}): {body}")`` -- which made
    an internal service's response body readable by any account that could
    reach it -- cannot be reintroduced by forgetting to redact at a call
    site.  ``.status``, ``.safe_url`` and ``.safe_body`` are for the log.
    """

    def __init__(self, status: int, safe_url: str, safe_body: str) -> None:
        super().__init__(
            "The language model provider returned an error (%s)." % status
        )
        self.status = status
        self.safe_url = safe_url
        self.safe_body = safe_body

    def client_message(self) -> str:
        """A fixed sentence carrying only the integer status."""
        return "The language model provider returned an error (%s)." % self.status


def public_error_message(exc: BaseException) -> str:
    """The single sink for anything an exception wants to tell a client.

    Every error path funnels through here, so the set of strings that can
    reach a user is exactly the set defined above -- and an exception type
    nobody thought about degrades to a generic sentence instead of leaking
    its ``str()``.
    """
    if isinstance(exc, NetGuardError):
        return exc.client_message()
    if isinstance(exc, UpstreamProviderError):
        return exc.client_message()
    return "The answer could not be generated."


# --- URL parsing -------------------------------------------------------------


class Endpoint(NamedTuple):
    """A validated outbound base URL."""

    scheme: str
    host: str
    port: int
    path: str
    base_url: str


def parse_endpoint(base_url, *, in_docker: bool = False) -> Endpoint:
    """Validate a base URL and return its :class:`Endpoint`.

    The checks run in this exact order, and the order is the security
    property: a cheap shape check (``malformed_url``) happens before the
    ``userinfo`` check, which happens before anything looks at the host, and
    nothing downstream is reached with a part that was not validated.

    Behaviour changes against the old ``_resolve_base_url``, both intended:

    * **Query and fragment are dropped.**  ``f"{base}/chat/completions"`` on
      a URL that had a query used to produce a request path with ``?`` in the
      middle of it.  The path is all the provider needs.
    * **The Docker rewrite is host-scoped.**  The old whole-URL
      ``re.sub(r"(localhost|127\\.0\\.0\\.1)", ...)`` also rewrote the word
      "localhost" inside a path or a query, producing a different host
      string at a place the caller never meant to change.
    """
    # 1. Not a string, or nothing left after stripping.
    if not isinstance(base_url, str) or not base_url.strip():
        raise NetGuardError("malformed_url", "base_url must be a non-empty string")

    # 2. Length cap, before any parsing work is done with it.
    if len(base_url) > MAX_URL_LENGTH:
        raise NetGuardError(
            "malformed_url",
            "base_url is %d characters, over the %d limit"
            % (len(base_url), MAX_URL_LENGTH),
        )

    # 3. Character allow-list. One regex for spaces, CR, LF, TAB, NUL,
    #    backslash and every non-ASCII codepoint.
    if not SAFE_URL_CHARS.match(base_url):
        raise NetGuardError(
            "malformed_url",
            "base_url contains characters that are not allowed in a URL",
        )

    try:
        parts = urlsplit(base_url)
    except ValueError as exc:  # pragma: no cover - urlsplit rarely raises here
        raise NetGuardError(
            "malformed_url", "base_url could not be split: %s" % (exc,)
        ) from exc

    # 4. Scheme, case-folded. Runs after the character allow-list so a value
    #    that is not even URL-shaped never reaches scheme matching.
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise NetGuardError(
            "bad_scheme",
            "scheme %r is not one of %s" % (scheme, ", ".join(ALLOWED_SCHEMES)),
        )

    netloc = parts.netloc
    # 5. A URL with no authority has no host to check, so it is malformed
    #    rather than merely unroutable.
    if not netloc:
        raise NetGuardError("malformed_url", "base_url has no host part")

    # 6. `http://user:pass@169.254.169.254/` -- checked BEFORE the Docker
    #    rewrite, so the rewrite can never be used to launder a credential
    #    out of the authority and still reach the guard.
    if "@" in netloc:
        raise NetGuardError(
            "userinfo_not_allowed",
            "base_url must not carry a user:password@ section",
        )

    # 7. Host, lowercased and without a trailing root dot.
    try:
        host = (parts.hostname or "").lower()
    except ValueError as exc:  # pragma: no cover - defensive
        raise NetGuardError(
            "malformed_url", "base_url has an unreadable host: %s" % (exc,)
        ) from exc
    host = host.rstrip(".")
    if not host:
        raise NetGuardError("malformed_url", "base_url has an empty host")

    # 8. Host-scoped Docker rewrite. Strictly better than the old whole-URL
    #    substitution: only the thing that is actually a host changes.
    if in_docker and host in DOCKER_REWRITE_HOSTS:
        host = DOCKER_HOSTNAME

    # 9. Port. `parts.port` raises ValueError for out-of-range and
    #    non-numeric ports; 0 is caught by the range test.
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise NetGuardError(
            "bad_port", "base_url has an invalid port: %s" % (exc,)
        ) from exc
    if explicit_port is None:
        port = DEFAULT_PORTS[scheme]
    elif 1 <= explicit_port <= 65535:
        port = explicit_port
    else:
        raise NetGuardError(
            "bad_port", "port %d is outside 1..65535" % explicit_port
        )

    # 10. Path, with the trailing slashes the old `rstrip("/")` removed, so
    #     f"{base_url}/chat/completions" is byte-identical to before.
    path = (parts.path or "").rstrip("/")

    # 11. Rebuild the netloc from the *validated, possibly rewritten* host so
    #     the returned base_url is what the checks above described. Query and
    #     fragment are dropped.
    netloc_out = host
    if ":" in host:  # IPv6 literal
        netloc_out = "[%s]" % host
    if explicit_port is not None:
        netloc_out = "%s:%d" % (netloc_out, port)

    return Endpoint(
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        base_url=urlunsplit((scheme, netloc_out, path, "", "")),
    )


def normalise_base_url(base_url, *, in_docker: bool = False) -> str:
    """Thin wrapper over :func:`parse_endpoint` returning just the base URL."""
    return parse_endpoint(base_url, in_docker=in_docker).base_url


# --- address policy ----------------------------------------------------------


def classify_address(ip: str) -> str:
    """Classify ``ip`` into exactly one category, or ``"invalid"``.

    Never raises: an unparseable address is a *rejection*, not an
    exception, because the caller is holding the output of a resolver and
    must treat anything it does not recognise as hostile by default.

    Order matters.  ``169.254.169.254`` is both private and link-local in
    Python's vocabulary, and reporting it as "private" would hide the fact
    that it is the cloud metadata endpoint, so link-local is tested first.
    Loopback likewise precedes private.  ``reserved`` is last of the
    structural tests because in the standard library almost every reserved
    range is *also* private, so testing it earlier would make it
    unreachable.
    """
    try:
        address = ipaddress.ip_address(str(ip))
    except (ValueError, TypeError):
        return "invalid"

    # An IPv4-mapped IPv6 address (`::ffff:169.254.169.254`) is how a
    # resolver may hand back a link-local target over an IPv6 socket.
    # Classify what it actually points at, so "never permitted" categories
    # stay unreachable through the mapped spelling.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped

    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link_local"
    if address.is_multicast:
        return "multicast"
    if address.is_private:
        return "private"
    if address.is_reserved:
        return "reserved"
    return "public"


def check_resolved_target(
    base_url: str,
    resolved: Sequence[str],
    *,
    allow_private: bool,
    origin: str,
) -> str:
    """Apply the address policy to an already-resolved host, or refuse.

    ``origin`` is the security-relevant switch:

    * ``"settings"`` -- the URL came from operator configuration, not from a
      request. It is returned unchanged and the address policy is **not**
      applied, because a self-hosted private Ollama is a legitimate setup
      and no user can choose this value.
    * ``"request"`` -- the URL came out of a request body. Every returned
      address must be allowed; one bad address poisons the batch, because a
      hostname with both a public and a loopback A record is precisely the
      shape a rebinding attack uses, and a check that passed on "the first
      address" would pass on it.

    The "one bad address poisons the batch" rule is deliberately stricter
    than picking a single address: it removes the choice of *which* address
    the connection would use from anyone but the resolver.
    """
    if origin == "settings":
        return base_url

    if not resolved:
        raise NetGuardError(
            "dns_empty", "the host resolved to no addresses at all"
        )

    allowed = (
        _ALLOWED_CATEGORIES_PRIVATE_OK if allow_private else _ALLOWED_CATEGORIES
    )

    for address in resolved:
        category = classify_address(address)
        if category in ALWAYS_BLOCKED or category not in allowed:
            raise NetGuardError(
                "blocked_address",
                "the host resolved to %s, which is classified %s and is not "
                "permitted for a request-supplied base_url (allowed: %s, "
                "LLM_ALLOW_PRIVATE_HOSTS=%s)"
                % (
                    address,
                    category,
                    ", ".join(sorted(allowed)),
                    "true" if allow_private else "false",
                ),
            )

    return base_url


def resolve_addresses(host: str, port: int, *, resolver=None) -> List[str]:
    """Resolve ``host`` to a list of address strings, or raise.

    ``resolver`` is injectable so the test suite can pass a lambda and never
    touch the network.  The default is :func:`socket.getaddrinfo`, which is
    called the way ``aiohttp`` calls it -- ``SOCK_STREAM`` only, no family
    hint -- so the guard sees the same candidate set the client would.

    Never returns ``[]``: an empty answer is a :class:`NetGuardError`, so a
    caller cannot accidentally treat "resolved nothing" as "resolved
    something fine".
    """
    fn = resolver or socket.getaddrinfo
    try:
        infos = fn(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, socket.herror, OSError) as exc:
        # gaierror and herror are OSError subclasses; the explicit list is
        # documentation, not narrowing.
        raise NetGuardError(
            "dns_failure",
            "the host could not be resolved: %s: %s" % (type(exc).__name__, exc),
        ) from exc

    addresses: List[str] = []
    for info in infos or []:
        try:
            sockaddr = info[4]
            raw = str(sockaddr[0])
        except (IndexError, KeyError, TypeError):
            # A resolver entry shaped unlike getaddrinfo's is skipped rather
            # than trusted; if that leaves nothing, dns_empty fires below.
            continue
        # An IPv6 link-local answer carries a zone index (`fe80::1%en0`).
        addresses.append(raw.split("%", 1)[0])

    if not addresses:
        raise NetGuardError(
            "dns_empty", "the host resolved to no usable addresses"
        )

    return addresses


# --- redaction (log only) ----------------------------------------------------


def redact_url(url) -> str:
    """``scheme://host[:port]/path`` with userinfo, query and fragment removed.

    Never raises. A URL that cannot be reduced to that shape becomes
    ``"<unparseable url>"`` rather than a partially-redacted original: the
    failure mode of a redactor that passes its input through is leaking
    exactly what it was written to remove.
    """
    try:
        if not isinstance(url, str) or not url.strip():
            return "<unparseable url>"
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
        if not host:
            return "<unparseable url>"
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = "[%s]" % host if ":" in host else host
        if port is not None:
            netloc = "%s:%d" % (netloc, port)
        return urlunsplit((parts.scheme, netloc, parts.path or "", "", ""))
    except Exception:
        return "<unparseable url>"


def redact_body(text, limit: int = BODY_LOG_LIMIT) -> str:
    """A single-line, length-capped, credential-scrubbed excerpt of a body.

    **Log only.**  Never raises, and never returns the input unchanged:

    * non-printable and non-ASCII characters become spaces, which is what
      removes CR and LF -- an upstream response body must not be able to
      forge log lines, which is the classic log-injection primitive;
    * runs of whitespace collapse to one space;
    * ``Bearer <token>``, ``sk-...`` and ``key=value`` / ``token: value`` /
      ``secret=`` / ``password=`` shapes are replaced;
    * the result is truncated to ``limit`` characters with a trailing
      ``"..."`` so one long provider body cannot flood the log.
    """
    if text is None:
        return ""
    try:
        raw = text if isinstance(text, str) else str(text)
        printable = "".join(
            ch if "\x20" <= ch <= "\x7e" else " " for ch in raw
        )
        collapsed = " ".join(printable.split())
        scrubbed = _BEARER_RE.sub("Bearer [redacted]", collapsed)
        scrubbed = _API_KEY_RE.sub("[redacted]", scrubbed)
        scrubbed = _SECRET_RE.sub(r"\1=[redacted]", scrubbed)
        if limit is None or limit < 0:
            return scrubbed
        if len(scrubbed) > limit:
            return scrubbed[:limit] + "..."
        return scrubbed
    except Exception:
        return ""
