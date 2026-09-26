"""Sliding-window rate limiting for the unauthenticated auth endpoints.

Audit finding 15 was that `POST /api/auth/login` verifies an argon2 hash on
every attempt.  That hash function is deliberately slow, the endpoint is
unauthenticated, and there was no throttle at all, so the endpoint was a free
CPU-exhaustion primitive for anyone who could reach the port.  Finding 14's
sibling problem is that the same endpoint is an *online* password-guessing
oracle: 10,000 guesses cost an attacker nothing but wall-clock time.

This module is the mechanism for both.  It is deliberately pure and
stdlib-only -- no `fastapi`, no `config`, no `logging` -- for the same reason
`policy.py` and `logging_setup.py` are:

* it has to be executable by `backend/tests/test_ratelimit.py` on a bare
  Python with nothing installed, which is the only kind of proof available in
  the environment this series was written in; and
* a throttle that reached for `settings` or a logger would be a throttle that
  could fail inside the failure path.  Raising `RateLimitExceeded` is the
  whole contract; the router decides what an `HTTPException` looks like.

**The clock is injectable.**  The default is `time.monotonic`, not
`time.time`.  A sliding window measured against the wall clock can be
*extended* by an NTP step forwards or *collapsed* by a step backwards, and
either one is an availability bug an attacker does not even have to be
timing.  `time.monotonic` cannot be moved by anything outside the process.
Tests pass a `FakeClock` explicitly and never touch a real one.

**A rejected attempt does not extend the window.**  When the limit is hit the
timestamp is *not* appended, so an attacker who keeps hammering a locked-out
account does not keep his own lockout alive -- which would turn a throttle
into a self-inflicted denial of service against a legitimate user of the same
account name.

**Memory is bounded.**  This runs on an unauthenticated endpoint, so the key
space is attacker-controlled: without a ceiling, sending a million distinct
`X-Forwarded-For` values grows a million buckets and turns a DoS control into
a DoS.  `max_keys` is a hard ceiling on how many keys are retained, and the
oldest-inserted is evicted first (FIFO by insertion, *not* LRU -- evicting by
recent use would let an attacker evict a hot victim key with a flood of
one-shot keys and then get unlimited attempts against the victim).

Run the tests with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible.
"""

from __future__ import annotations

import collections
import math
import threading
import time
from typing import Callable, List, Optional, Tuple

#: Returned by :func:`client_ip` when no address can be derived at all.  It is
#: a real bucket key, and that is deliberate *and* dangerous: see the residual
#: risks in `SECURITY.md`.  Every request with no derivable IP shares it, so
#: one attacker can throttle every other anonymous caller in the deployment.
UNKNOWN_IP = "unknown"

#: An address longer than this is either a malformed header or an attempt to
#: push a large string into every log line.  Real IPv6 addresses are well under
#: 64 characters, so truncation never costs a legitimate caller.
MAX_IP_LENGTH = 64

#: How many distinct keys a single limiter will retain.  The auth limiters use
#: this default; see the module docstring for why the ceiling is mandatory.
DEFAULT_MAX_KEYS = 10_000

#: Header names, in precedence order.  `x-forwarded-for` is a list, so its
#: *first* entry is the original client and the ones after it are proxies that
#: appended.  Trusting the first entry is only sound when a trusted proxy
#: *overwrites* the header; that is a deployment requirement, documented in
#: `SECURITY.md`, not something this module can enforce.
X_FORWARDED_FOR = "x-forwarded-for"
X_REAL_IP = "x-real-ip"


class RateLimitExceeded(Exception):
    """Raised by :meth:`SlidingWindowLimiter.check` when a key is over budget.

    Carries ``retry_after`` in whole seconds so the caller can put it straight
    into a `Retry-After` response header.  It is always ``>= 1``: a `0` in
    `Retry-After` tells a client to retry immediately, and an HTTP date of
    "now" is the same instruction, so a zero would advertise that the limit is
    not real.
    """

    def __init__(self, retry_after: int):
        super().__init__("rate limit exceeded")
        try:
            seconds = int(math.ceil(float(retry_after)))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            seconds = 1
        self.retry_after = max(1, seconds)


class SlidingWindowLimiter:
    """An exact sliding-window counter, one bucket per key.

    A sliding window is used rather than a fixed one because a fixed window
    has a boundary you can walk through: a client sending 10 attempts at
    11:59 and 10 more at 12:00 gets 20 in one second.  This keeps the real
    timestamps and counts only the ones still inside the window, so
    "``limit`` attempts per ``window_seconds``" means what it says.

    Every public method takes the same lock, so the limiter is safe to share
    between the threads FastAPI runs sync handlers on.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        max_keys: int = DEFAULT_MAX_KEYS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._limit = int(limit)
        self._window = float(window_seconds)
        self._max_keys = max(1, int(max_keys))
        self._clock = clock
        # OrderedDict, not dict: insertion order is what makes the
        # `popitem(last=False)` eviction below O(1) and, more importantly,
        # makes the eviction order *deterministic* and testable.
        self._buckets: "collections.OrderedDict[str, List[float]]" = (
            collections.OrderedDict()
        )
        self._lock = threading.Lock()
        self._last_prune = self._clock()
        # Opportunistic sweep at most this often. window/10 bounds the stale
        # memory a key can pin to a tenth of a window without turning every
        # check into an O(keys) walk.
        self._prune_interval = self._window / 10.0 if self._window > 0 else 0.0

    # -- introspection --------------------------------------------------------

    @property
    def limit(self) -> int:
        """The configured per-window attempt ceiling."""
        return self._limit

    @property
    def window_seconds(self) -> float:
        """The configured window width, in seconds."""
        return self._window

    def key_count(self) -> int:
        """How many keys are currently retained (diagnostics and tests)."""
        with self._lock:
            return len(self._buckets)

    # -- the two operations the router uses ----------------------------------

    def check(self, key: str) -> None:
        """Consume one unit of ``key``'s budget, or raise :class:`RateLimitExceeded`.

        The timestamp is appended **only when the attempt is allowed**.  If it
        were appended on rejection, a client could pin its own (or a victim's)
        lockout open indefinitely by continuing to try, which is the opposite
        of what a throttle is for.

        ``retry_after`` is computed from the *oldest* retained timestamp, which
        is the one that has to expire before a slot frees up.
        """
        with self._lock:
            now = self._clock()
            self._maybe_prune(now)

            bucket = self._buckets.get(key)
            if bucket is not None:
                self._expire(bucket, now - self._window)

            if bucket is not None and len(bucket) >= self._limit:
                wait = bucket[0] + self._window - now
                raise RateLimitExceeded(wait)

            if bucket is None:
                bucket = []
                self._insert(key, bucket)
            bucket.append(now)

    def peek(self, key: str) -> Tuple[int, int]:
        """Return ``(remaining, retry_after)`` for ``key`` without consuming.

        Never raises, and an unseen key is simply a key with a full budget, so
        a caller must not read anything into the absence of an exception here.

        ``retry_after`` follows the HTTP meaning of the header rather than the
        arithmetic one: it is **0 whenever a slot is free**, because "you may
        retry at 0 seconds" is true, and it is the number of seconds until the
        oldest timestamp expires only once the key is actually over budget.
        Returning the time-to-free-a-slot unconditionally would put a "60" in
        a ``Retry-After`` header on a request that was never refused.
        """
        with self._lock:
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is None:
                return (self._limit, 0)
            self._expire(bucket, now - self._window)
            if not bucket:
                return (self._limit, 0)
            remaining = max(0, self._limit - len(bucket))
            if remaining > 0:
                return (remaining, 0)
            wait = max(0.0, bucket[0] + self._window - now)
            return (0, int(math.ceil(wait)))

    # -- maintenance ----------------------------------------------------------

    def reset(self, key: Optional[str] = None) -> None:
        """Forget ``key``, or forget everything when ``key`` is ``None``.

        Resetting a key that was never seen is not an error: a success path
        calls this on every login, and "there was no bucket" is the normal
        state for a first-time-success.
        """
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)

    def prune(self, now: Optional[float] = None) -> int:
        """Drop every key whose newest timestamp has left the window.

        Returns the number of keys dropped.  Exposed (and tested) so a
        long-lived process can be swept deliberately; :meth:`check` also
        sweeps on its own, at most once per ``window/10``.
        """
        with self._lock:
            if now is None:
                now = self._clock()
            return self._prune_locked(now)

    # -- internals (all called with the lock held) ----------------------------

    def _expire(self, bucket: List[float], cutoff: float) -> None:
        """Drop timestamps strictly older than ``cutoff``, in place.

        Written as a full filter rather than a ``while bucket[0] < cutoff``
        loop on purpose: that loop is only correct while the clock is
        non-decreasing, and an injected clock in a test (or a future
        non-monotonic source) would make it silently wrong.  ``len(bucket)`` is
        bounded by ``limit``, so the filter is cheap.
        """
        kept = [stamp for stamp in bucket if stamp >= cutoff]
        if len(kept) != len(bucket):
            del bucket[:]
            bucket.extend(kept)

    def _insert(self, key: str, bucket: List[float]) -> None:
        """Add a new key, evicting oldest-inserted while over ``max_keys``."""
        self._buckets[key] = bucket
        self._buckets.move_to_end(key)
        while len(self._buckets) > self._max_keys:
            # last=False -> FIFO by insertion, NOT LRU.  See the module
            # docstring: LRU would let an attacker evict the key he is
            # actually being throttled on.
            self._buckets.popitem(last=False)

    def _prune_locked(self, now: float) -> int:
        cutoff = now - self._window
        stale = [
            key
            for key, bucket in self._buckets.items()
            if not bucket or bucket[-1] < cutoff
        ]
        for key in stale:
            del self._buckets[key]
        self._last_prune = now
        return len(stale)

    def _maybe_prune(self, now: float) -> None:
        if now - self._last_prune >= self._prune_interval:
            self._prune_locked(now)


def _header_value(headers, name: str) -> str:
    """Read ``name`` from a header mapping, tolerating spelling and garbage.

    `starlette`'s `Headers` is case-insensitive; a plain `dict` -- which is
    what a test and a hand-rolled adapter will pass -- is not.  So the exact
    lowercase name, the canonical capitalisation and then a case-folded scan
    are all tried.

    Returns `""` rather than raising: this runs on the login path, and a header
    mapping that explodes must produce a throttled or unthrottled request,
    never a 500.

    The value is **stripped here**, not by the caller, and that is the whole
    point of the function.  ``X-Forwarded-For: " "`` is trivially sent by
    anyone; if a blank header counted as "present", the caller would stop
    before the fallbacks and the request would land in the shared
    :data:`UNKNOWN_IP` bucket -- which is everyone else's bucket, so the
    throttle would become a weapon against other users.
    """
    if headers is None:
        return ""

    def _usable(value) -> str:
        if not value:
            return ""
        try:
            return str(value).strip()
        except Exception:  # pragma: no cover - a hostile __str__
            return ""

    try:
        found = _usable(headers.get(name))
    except Exception:
        found = ""
    if found:
        return found

    canonical = "-".join(part.capitalize() for part in name.split("-"))
    try:
        found = _usable(headers.get(canonical))
    except Exception:
        found = ""
    if found:
        return found

    try:
        items = list(headers.items())
    except Exception:
        return ""
    folded = name.lower()
    for key, value in items:
        try:
            if str(key).lower() == folded:
                found = _usable(value)
                if found:
                    return found
        except Exception:  # pragma: no cover - a hostile __str__
            continue
    return ""


def _clean_candidate(raw) -> str:
    """Normalise one raw header/fallback value into a usable key segment.

    ``""`` means "not usable", which is what lets :func:`client_ip` move on to
    the next source instead of shadowing it.

    Three things happen, in this order, and each one earns its place:

    1. **Left-most list entry.** ``X-Forwarded-For`` is a list; only the first
       entry is the client.
    2. **Control characters are removed.**  This value is used as a log field.
       A header of ``"1.2.3.4\\nINFO ragapp.auth login accepted admin"`` would
       otherwise write a forged line into a shared log stream, which is a real
       injected-log-line forgery, not a cosmetic issue. Removing the character
       is safer than rejecting the header: two hostile clients may then share
       a bucket, and for a rate limiter sharing a bucket fails *closed*.
    3. **Strip, truncate to 64, strip again.**  Truncating a value that is 500
       characters long keeps both the dictionary key and the log line bounded;
       the second strip matters because the truncation can expose padding.
    """
    if not raw:
        return ""
    try:
        candidate = str(raw).split(",")[0]
        candidate = "".join(
            ch for ch in candidate if ord(ch) > 0x1F and ord(ch) != 0x7F
        )
        candidate = candidate.strip()[:MAX_IP_LENGTH].strip()
    except Exception:  # pragma: no cover - str() of a hostile object
        return ""
    return candidate


def client_ip(headers, remote_addr: Optional[str] = None) -> str:
    """Best-effort source address for rate-limiting, in one string.

    Precedence: the **first** comma-separated entry of `X-Forwarded-For`,
    then `X-Real-IP`, then ``remote_addr``, then :data:`UNKNOWN_IP`.

    The result is whitespace-stripped, truncated to :data:`MAX_IP_LENGTH`
    characters, and is :data:`UNKNOWN_IP` if nothing usable survives.  It
    **never raises**: a malformed, absent or hostile header must not turn the
    login endpoint into a 500, and a bug in an untrusted-header parser should
    not be a remotely-triggerable outage.

    Two consequences are worth stating plainly rather than hiding:

    * An *empty* header must not shadow the fallback.  `X-Forwarded-For: ` is
      trivially sent by anyone, and a `dict.get(...) or ""` that short-circuits
      on the header's presence rather than its usability would let an attacker
      pin every one of their requests into the shared :data:`UNKNOWN_IP`
      bucket -- which is the *victim* bucket, so the throttle would become a
      weapon against everyone else.
    * The result is a **key**, not a validated address.  It is not parsed as
      an IP, because the limiter only needs a stable, bounded, low-entropy
      grouping key, and a parser here would be a parser an attacker can crash
      or confuse.  Anything that reaches the log goes through the same
      normalisation, so a header cannot inject a newline into a log line.
    """
    try:
        candidate = _clean_candidate(_header_value(headers, X_FORWARDED_FOR))
        if not candidate:
            candidate = _clean_candidate(_header_value(headers, X_REAL_IP))
        if not candidate:
            candidate = _clean_candidate(remote_addr)
        return candidate or UNKNOWN_IP
    except Exception:  # pragma: no cover - the guarantee above is the point
        return UNKNOWN_IP
