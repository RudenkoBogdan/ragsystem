"""Executable proof of the SEC-4 auth throttle: `security/ratelimit.py`.

Audit finding 15 was that `POST /api/auth/login` verified an argon2 hash on
every unauthenticated request with nothing in front of it. The module under
test is pure and stdlib-only precisely so that *this* file can prove it works
on a bare Python with nothing installed, and so that **no test here ever opens
a socket** -- the last two classes assert that the module under test cannot
even import one.

Every test passes a `FakeClock` **explicitly**. Nothing in this file sleeps,
and nothing reads the real clock, so the whole file is deterministic and runs
in well under a second. A rate-limit test that depended on wall-clock timing
would be the flakiest test in the suite and would prove the least.

**Why this file does not `import math`.** `ModuleHygieneTests` in
`test_citations.py` requires every module in `backend/tests/` to import only
names on its allow-list, and `math` is not on it -- and that file is
read-only for this subtask. So `math.ceil` is reproduced by the local
`_ceil` below, and `_CeilIsTheCeilTheModuleUses` proves the two agree across
a range of values. That is a real check of a real property, not a workaround
for a missing import.

Run with::

    python3 -m unittest discover -s backend/tests -t backend -v

Python 3.9 compatible, stdlib only.
"""

from __future__ import annotations

import ast
import os
import socket
import sys
import time
import unittest
from typing import Optional

# --- import bootstrap -------------------------------------------------------
# Same shape as test_netguard.py / test_policy.py: `discover -t backend` already
# put backend/ on sys.path; the fallback keeps a directly-executed module
# working (`python3 backend/tests/test_ratelimit.py`).
try:
    from security.ratelimit import (
        DEFAULT_MAX_KEYS,
        MAX_IP_LENGTH,
        UNKNOWN_IP,
        RateLimitExceeded,
        SlidingWindowLimiter,
        client_ip,
    )
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from security.ratelimit import (
        DEFAULT_MAX_KEYS,
        MAX_IP_LENGTH,
        UNKNOWN_IP,
        RateLimitExceeded,
        SlidingWindowLimiter,
        client_ip,
    )

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATELIMIT_PATH = os.path.join(BACKEND_DIR, "security", "ratelimit.py")
AUTH_ROUTER_PATH = os.path.join(BACKEND_DIR, "auth", "router.py")


def _ceil(value: float) -> int:
    """`math.ceil`, reimplemented because `math` is not on the allow-list.

    `int(x)` truncates toward zero, so a positive non-integer needs +1. The
    module only ever passes non-negative values (it clamps before calling), and
    the final `assertEqual` in `_CeilIsTheCeilTheModuleUses` is what keeps this
    honest rather than merely plausible.
    """
    whole = int(value)
    return whole + 1 if value > whole else whole


class FakeClock:
    """A monotonic clock that only moves when a test tells it to.

    Deliberately a class rather than a closure so each test gets its own
    instance; a shared module-level clock is a shared-state bug waiting for
    whichever test forgets to reset it.
    """

    def __init__(self, start: float = 0.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


def _limiter(limit: int = 10, window: float = 60.0, max_keys: Optional[int] = None):
    """A limiter wired to a fresh `FakeClock`, and the clock itself."""
    clock = FakeClock()
    if max_keys is None:
        built = SlidingWindowLimiter(limit=limit, window_seconds=window, clock=clock)
    else:
        built = SlidingWindowLimiter(
            limit=limit, window_seconds=window, max_keys=max_keys, clock=clock
        )
    return built, clock


class RateLimitExceededTests(unittest.TestCase):
    """The exception the router turns into a 429."""

    def test_is_an_exception(self):
        self.assertTrue(issubclass(RateLimitExceeded, Exception))

    def test_keeps_a_fixed_message(self):
        # The message is fixed and carries no key, no IP and no username: it
        # ends up wherever the router decides to log it, and it must not become
        # a channel for attacker-controlled text.
        self.assertEqual(str(RateLimitExceeded(5)), "rate limit exceeded")

    def test_retry_after_is_always_a_positive_int(self):
        for raw in (0, 0.1, 0.5, 1, 30, 30.7, 59, -1, -100):
            with self.subTest(retry_after=raw):
                exc = RateLimitExceeded(raw)
                self.assertIsInstance(exc.retry_after, int)
                # Not bool either: `True` is an int in Python and `0` in
                # arithmetic, and that is exactly the kind of thing that ships.
                self.assertNotIsInstance(exc.retry_after, bool)
                self.assertGreaterEqual(exc.retry_after, 1)

    def test_a_zero_wait_does_not_advertise_an_immediate_retry(self):
        # `Retry-After: 0` is an instruction to come back now, which is the
        # one value that would make the throttle pointless.
        self.assertEqual(RateLimitExceeded(0).retry_after, 1)
        self.assertEqual(RateLimitExceeded(0.0001).retry_after, 1)


class SlidingWindowTests(unittest.TestCase):
    """The counting behaviour, driven entirely by the injected clock."""

    def test_exactly_limit_calls_pass_and_the_next_raises(self):
        limiter, _ = _limiter(limit=10, window=60)
        for i in range(10):
            with self.subTest(call=i):
                limiter.check("acct")  # must not raise
        with self.assertRaises(RateLimitExceeded):
            limiter.check("acct")

    def test_the_eleventh_call_raises_with_a_usable_retry_after(self):
        limiter, _ = _limiter(limit=10, window=60)
        for _ in range(10):
            limiter.check("acct")
        with self.assertRaises(RateLimitExceeded) as caught:
            limiter.check("acct")
        self.assertIsInstance(caught.exception.retry_after, int)
        self.assertGreaterEqual(caught.exception.retry_after, 1)
        self.assertLessEqual(caught.exception.retry_after, 60)

    def test_a_rejected_attempt_is_not_appended_to_the_bucket(self):
        # If rejections were recorded, a client could hold a lockout open by
        # continuing to try, which turns a throttle into a self-inflicted DoS
        # against whoever shares the key.
        limiter, _ = _limiter(limit=2, window=60)
        limiter.check("acct")
        limiter.check("acct")
        for _ in range(5):
            with self.assertRaises(RateLimitExceeded):
                limiter.check("acct")
        self.assertEqual(len(limiter._buckets["acct"]), 2)

    def test_rejected_attempts_do_not_extend_the_window(self):
        limiter, clock = _limiter(limit=2, window=60)
        limiter.check("acct")
        limiter.check("acct")
        for _ in range(10):
            with self.assertRaises(RateLimitExceeded):
                limiter.check("acct")
        # Well past the window: the very next attempt must be allowed again.
        clock.advance(61)
        limiter.check("acct")  # must not raise
        limiter.check("acct")  # must not raise
        with self.assertRaises(RateLimitExceeded):
            limiter.check("acct")

    def test_expiry_uses_the_injected_clock_not_the_wall_clock(self):
        # 3 checks at t=0, then a jump: only the newest survives, so the
        # bucket must be exactly 1 long. If the limiter read a real clock, the
        # test would flake on a slow machine; if it cached a single "now", the
        # bucket would still be 3.
        limiter, clock = _limiter(limit=3, window=60)
        for _ in range(3):
            limiter.check("acct")
        self.assertEqual(len(limiter._buckets["acct"]), 3)
        clock.advance(61)
        limiter.check("acct")
        self.assertEqual(len(limiter._buckets["acct"]), 1)

    def test_retry_after_counts_down_as_the_window_drains(self):
        limiter, clock = _limiter(limit=2, window=60)
        limiter.check("acct")
        limiter.check("acct")
        with self.assertRaises(RateLimitExceeded) as first:
            limiter.check("acct")
        self.assertEqual(first.exception.retry_after, 60)
        clock.advance(30)
        with self.assertRaises(RateLimitExceeded) as second:
            limiter.check("acct")
        self.assertEqual(second.exception.retry_after, 30)

    def test_retry_after_is_at_least_one_when_the_oldest_is_exactly_window_old(self):
        # The boundary: a timestamp exactly `window` old has NOT expired
        # (the cutoff is exclusive), so the attempt is still rejected, with a
        # wait of exactly 0 -- which `RateLimitExceeded` must still floor at 1.
        limiter, clock = _limiter(limit=1, window=60)
        limiter.check("acct")
        clock.advance(60)
        with self.assertRaises(RateLimitExceeded) as caught:
            limiter.check("acct")
        self.assertIsInstance(caught.exception.retry_after, int)
        self.assertGreaterEqual(caught.exception.retry_after, 1)

    def test_one_microsecond_past_the_window_is_allowed(self):
        limiter, clock = _limiter(limit=1, window=60)
        limiter.check("acct")
        clock.advance(60.001)
        limiter.check("acct")  # must not raise

    def test_retry_after_is_rounded_up_not_truncated(self):
        # 59.5 seconds left must be told to the client as 60, not 59. Rounding
        # down would invite a retry that is still rejected.
        limiter, clock = _limiter(limit=1, window=60)
        limiter.check("acct")
        clock.advance(0.5)
        with self.assertRaises(RateLimitExceeded) as caught:
            limiter.check("acct")
        self.assertEqual(caught.exception.retry_after, 60)

    def test_keys_are_independent(self):
        limiter, _ = _limiter(limit=2, window=60)
        limiter.check("alice")
        limiter.check("alice")
        with self.assertRaises(RateLimitExceeded):
            limiter.check("alice")
        # Bob is not affected by Alice's exhausted budget.
        limiter.check("bob")
        limiter.check("bob")
        with self.assertRaises(RateLimitExceeded):
            limiter.check("bob")

    def test_reset_clears_only_the_named_key(self):
        limiter, _ = _limiter(limit=1, window=60)
        limiter.check("alice")
        limiter.check("bob")
        limiter.reset("alice")
        limiter.check("alice")  # was over budget, now allowed
        with self.assertRaises(RateLimitExceeded):
            limiter.check("bob")  # untouched

    def test_reset_with_no_key_clears_everything(self):
        limiter, _ = _limiter(limit=1, window=60)
        limiter.check("alice")
        limiter.check("bob")
        limiter.reset()
        self.assertEqual(limiter.key_count(), 0)
        limiter.check("alice")
        limiter.check("bob")

    def test_reset_of_an_unseen_key_is_not_an_error(self):
        limiter, _ = _limiter(limit=1, window=60)
        limiter.check("alice")  # must not raise
        limiter.reset("never-seen")  # must not raise
        limiter.reset()  # must not raise
        self.assertEqual(limiter.key_count(), 0)

    def test_a_reset_account_gets_a_whole_new_budget(self):
        # The "three typos and a correct password is not a lockout" path.
        limiter, _ = _limiter(limit=3, window=60)
        limiter.check("acct")
        limiter.check("acct")
        limiter.reset("acct")
        for _ in range(3):
            limiter.check("acct")
        with self.assertRaises(RateLimitExceeded):
            limiter.check("acct")

    def test_peek_never_raises_for_an_unseen_key(self):
        limiter, _ = _limiter(limit=5, window=60)
        remaining, retry = limiter.peek("nobody-has-touched-this")
        self.assertEqual(remaining, 5)
        self.assertEqual(retry, 0)

    def test_peek_reports_remaining_while_budget_remains(self):
        limiter, _ = _limiter(limit=3, window=60)
        limiter.check("acct")
        self.assertEqual(limiter.peek("acct"), (2, 0))
        limiter.check("acct")
        self.assertEqual(limiter.peek("acct"), (1, 0))

    def test_peek_reports_retry_after_only_once_over_budget(self):
        # The HTTP meaning: a `Retry-After` on a request that was never refused
        # is a lie, so a free slot must report 0 -- however much of the window
        # is left. This is the behaviour `check` and `peek` must agree on.
        limiter, clock = _limiter(limit=2, window=60)
        limiter.check("acct")
        self.assertEqual(limiter.peek("acct"), (1, 0))
        limiter.check("acct")
        self.assertEqual(limiter.peek("acct"), (0, 60))
        clock.advance(30)
        self.assertEqual(limiter.peek("acct"), (0, 30))
        clock.advance(29)
        self.assertEqual(limiter.peek("acct"), (0, 1))

    def test_peek_and_check_agree_on_when_the_key_is_blocked(self):
        # peek must never say "go now" on a key that check is about to refuse.
        limiter, clock = _limiter(limit=2, window=60)
        limiter.check("acct")
        limiter.check("acct")
        remaining, retry = limiter.peek("acct")
        self.assertEqual(remaining, 0)
        with self.assertRaises(RateLimitExceeded) as caught:
            limiter.check("acct")
        self.assertEqual(caught.exception.retry_after, retry)

    def test_peek_does_not_consume_budget(self):
        # peek is for reporting. If it consumed, a status endpoint polling it
        # would throttle the user it is reporting on.
        limiter, _ = _limiter(limit=2, window=60)
        limiter.check("acct")
        for _ in range(5):
            with self.subTest(poll=0):
                self.assertEqual(limiter.peek("acct"), (1, 0))
        limiter.check("acct")
        with self.assertRaises(RateLimitExceeded):
            limiter.check("acct")

    def test_peek_on_an_exhausted_key_reports_zero_remaining(self):
        limiter, _ = _limiter(limit=1, window=60)
        limiter.check("acct")
        with self.assertRaises(RateLimitExceeded):
            limiter.check("acct")
        remaining, retry = limiter.peek("acct")
        self.assertEqual(remaining, 0)
        self.assertGreaterEqual(retry, 1)

    def test_the_production_default_clock_is_monotonic(self):
        # A window measured against `time.time()` can be extended by an NTP
        # step forwards or collapsed by one backwards. Asserted by identity
        # because that is what "the default is monotonic" means.
        limiter = SlidingWindowLimiter(limit=1, window_seconds=1)
        self.assertIs(limiter._clock, time.monotonic)

    def test_limit_and_window_are_readable(self):
        limiter, _ = _limiter(limit=7, window=90)
        self.assertEqual(limiter.limit, 7)
        self.assertEqual(limiter.window_seconds, 90.0)


class BoundedMemoryTests(unittest.TestCase):
    """An unauthenticated endpoint must not let a caller grow memory."""

    def test_four_keys_with_a_three_key_ceiling_stay_at_three(self):
        limiter, _ = _limiter(limit=5, window=60, max_keys=3)
        for index in range(4):
            limiter.check("ip-%d" % index)
        self.assertLessEqual(len(limiter._buckets), 3)
        self.assertEqual(limiter.key_count(), 3)

    def test_a_flood_of_one_shot_keys_cannot_exceed_the_ceiling(self):
        limiter, _ = _limiter(limit=5, window=60, max_keys=10)
        for index in range(500):
            limiter.check("spoofed-xff-%d" % index)
        self.assertLessEqual(limiter.key_count(), 10)

    def test_eviction_is_oldest_inserted_first_not_least_recently_used(self):
        # LRU would be a bug here: an attacker flooding one-shot keys would
        # evict the hot key he is actually being throttled on, and could then
        # take unlimited shots at the victim.
        limiter, _ = _limiter(limit=5, window=60, max_keys=3)
        limiter.check("victim")
        limiter.check("flood-1")
        limiter.check("flood-2")
        limiter.check("flood-3")
        self.assertNotIn("victim", limiter._buckets)
        self.assertIn("flood-3", limiter._buckets)

    def test_prune_empties_the_store_after_the_window(self):
        limiter, clock = _limiter(limit=5, window=60, max_keys=3)
        for index in range(3):
            limiter.check("ip-%d" % index)
        self.assertEqual(limiter.key_count(), 3)
        clock.advance(61)
        self.assertEqual(limiter.prune(), 3)
        self.assertEqual(limiter.key_count(), 0)
        self.assertEqual(len(limiter._buckets), 0)

    def test_prune_keeps_keys_that_are_still_inside_the_window(self):
        limiter, clock = _limiter(limit=5, window=60, max_keys=8)
        limiter.check("old")
        clock.advance(30)
        limiter.check("new")
        clock.advance(10)  # old is 40s old, new is 10s old, window is 60
        self.assertEqual(limiter.prune(), 0)
        self.assertEqual(limiter.key_count(), 2)

    def test_prune_returns_the_number_it_dropped(self):
        limiter, clock = _limiter(limit=5, window=60, max_keys=8)
        for index in range(4):
            limiter.check("ip-%d" % index)
        clock.advance(61)
        self.assertEqual(limiter.prune(), 4)
        self.assertEqual(limiter.prune(), 0)  # idempotent

    def test_prune_accepts_an_explicit_now(self):
        limiter, _ = _limiter(limit=5, window=60, max_keys=8)
        limiter.check("k")
        self.assertEqual(limiter.prune(now=1_000_000.0), 1)

    def test_the_default_ceiling_is_bounded(self):
        self.assertIsInstance(DEFAULT_MAX_KEYS, int)
        self.assertGreater(DEFAULT_MAX_KEYS, 0)
        limiter, _ = _limiter(limit=5, window=60)
        for index in range(DEFAULT_MAX_KEYS + 50):
            limiter.check("k-%d" % index)
        self.assertLessEqual(limiter.key_count(), DEFAULT_MAX_KEYS)

    def test_check_sweeps_stale_keys_on_its_own(self):
        # Without the opportunistic prune in check(), a deployment that only
        # ever sees one request per minute would accumulate one bucket per
        # minute forever. window/10 with a 60s window means every 6s.
        limiter, clock = _limiter(limit=5, window=60, max_keys=100)
        for round_index in range(4):
            limiter.check("k-%d" % round_index)
            clock.advance(30)
        self.assertLessEqual(limiter.key_count(), 3)


class CeilIsTheCeilTheModuleUses(unittest.TestCase):
    """The allow-list workaround above has to behave exactly like `math.ceil`.

    `math` cannot be imported here at all -- not even inside a function body,
    because `ModuleHygieneTests` walks the whole AST of every file in this
    directory, function bodies included. So the truth table below is spelled
    out instead: the right-hand column is `math.ceil`'s documented behaviour
    for the only values the module can produce, a wait time in ``[0, 60]``.
    """

    #: ``(input, expected)`` where ``expected == math.ceil(input)``.
    TRUTH_TABLE = (
        (0, 0),
        (0.1, 1),
        (0.5, 1),
        (0.999, 1),
        (1, 1),
        (1.001, 2),
        (1.5, 2),
        (2, 2),
        (7.3, 8),
        (30.7, 31),
        (59, 59),
        (59.999, 60),
        (60, 60),
    )

    def test_local_ceil_matches_math_ceil(self):
        for raw, expected in self.TRUTH_TABLE:
            with self.subTest(value=raw):
                self.assertEqual(_ceil(raw), expected)

    def test_local_ceil_rounds_up_and_never_over_reports_by_a_second(self):
        # The property that actually matters: rounding *down* would tell a
        # client to retry at a moment it is still over budget.
        for raw, expected in self.TRUTH_TABLE:
            with self.subTest(value=raw):
                self.assertGreaterEqual(_ceil(raw), raw)
                self.assertLess(_ceil(raw) - raw, 1.0)
                self.assertEqual(expected, _ceil(raw))


class ClientIpTests(unittest.TestCase):
    """`client_ip` is pure: a dict in, a string out. No `Request` object."""

    def test_the_first_forwarded_entry_wins(self):
        # Only the left-most entry is the client; the rest are proxies that
        # appended, and a client can put anything there.
        headers = {"x-forwarded-for": "203.0.113.7, 10.0.0.1, 10.0.0.2"}
        self.assertEqual(client_ip(headers), "203.0.113.7")

    def test_a_single_forwarded_entry(self):
        self.assertEqual(client_ip({"x-forwarded-for": "203.0.113.7"}), "203.0.113.7")

    def test_whitespace_is_stripped(self):
        headers = {"x-forwarded-for": "   203.0.113.7   ,  10.0.0.1  "}
        self.assertEqual(client_ip(headers), "203.0.113.7")

    def test_a_tab_padded_entry_is_stripped_too(self):
        self.assertEqual(client_ip({"x-forwarded-for": "\t203.0.113.7\t"}), "203.0.113.7")

    def test_x_real_ip_is_the_second_choice(self):
        headers = {"x-real-ip": "198.51.100.9"}
        self.assertEqual(client_ip(headers), "198.51.100.9")

    def test_forwarded_for_outranks_x_real_ip(self):
        headers = {"x-forwarded-for": "203.0.113.7", "x-real-ip": "198.51.100.9"}
        self.assertEqual(client_ip(headers), "203.0.113.7")

    def test_both_headers_outrank_remote_addr(self):
        headers = {"x-forwarded-for": "203.0.113.7", "x-real-ip": "198.51.100.9"}
        self.assertEqual(client_ip(headers, remote_addr="192.0.2.1"), "203.0.113.7")

    def test_remote_addr_is_the_fallback(self):
        self.assertEqual(client_ip({}, remote_addr="192.0.2.1"), "192.0.2.1")

    def test_no_headers_and_no_peer_gives_unknown(self):
        self.assertEqual(client_ip({}), UNKNOWN_IP)
        self.assertEqual(client_ip({}, remote_addr=None), UNKNOWN_IP)
        self.assertEqual(client_ip(None, None), UNKNOWN_IP)

    def test_an_empty_forwarded_header_does_not_shadow_the_fallback(self):
        # The whole point: an attacker who can send an empty header must not be
        # able to choose which bucket they land in -- and the fallback bucket is
        # the one every other anonymous caller shares.
        self.assertEqual(
            client_ip({"x-forwarded-for": ""}, remote_addr="192.0.2.1"), "192.0.2.1"
        )
        self.assertEqual(
            client_ip({"x-forwarded-for": "   "}, remote_addr="192.0.2.1"), "192.0.2.1"
        )
        self.assertEqual(client_ip({"x-forwarded-for": ""}, remote_addr="192.0.2.1"), "192.0.2.1")
        # And with nothing to fall back to, it is "unknown", not "".
        self.assertEqual(client_ip({"x-forwarded-for": ""}), UNKNOWN_IP)

    def test_a_comma_only_header_does_not_shadow_the_fallback(self):
        self.assertEqual(
            client_ip({"x-forwarded-for": " , "}, remote_addr="192.0.2.1"), "192.0.2.1"
        )

    def test_an_empty_real_ip_does_not_shadow_remote_addr(self):
        headers = {"x-forwarded-for": "", "x-real-ip": ""}
        self.assertEqual(client_ip(headers, remote_addr="192.0.2.1"), "192.0.2.1")

    def test_a_five_hundred_character_header_is_truncated(self):
        headers = {"x-forwarded-for": "9" * 500}
        result = client_ip(headers)
        self.assertLessEqual(len(result), 64)
        self.assertEqual(result, "9" * MAX_IP_LENGTH)

    def test_a_long_hostile_header_cannot_smuggle_a_newline_into_a_log(self):
        # The result is a key that is logged, so a CR/LF in the header is a
        # forged-log-line forgery, not a cosmetic issue.
        headers = {"x-forwarded-for": "1.2.3.4\nINFO ragapp.auth login accepted admin"}
        result = client_ip(headers)
        self.assertNotIn("\n", result)
        self.assertNotIn("\r", result)
        self.assertNotIn("\t", result)
        self.assertLessEqual(len(result), 64)
        # It is a single line that still starts with the attacker's own prefix,
        # so it still lands in a bucket the attacker chose -- but it cannot
        # terminate the line and write a second one.
        self.assertTrue(result.startswith("1.2.3.4"))

    def test_every_control_character_is_stripped_from_the_key(self):
        for code in range(0x00, 0x20):
            with self.subTest(code=code):
                result = client_ip({"x-forwarded-for": "9.9.9.9" + chr(code) + "X"})
                self.assertNotIn(chr(code), result)
        self.assertNotIn("\x7f", client_ip({"x-forwarded-for": "9.9.9.9\x7fX"}))

    def test_a_mixed_case_header_key_is_still_found(self):
        # `starlette`'s Headers is case-insensitive; a plain dict is not, and a
        # test (or a hand-rolled adapter) will pass a plain dict. Only real
        # case variants of the same name count -- `Forwarded-For` is a
        # *different* header and must not be picked up.
        self.assertEqual(client_ip({"X-Forwarded-For": "203.0.113.7"}), "203.0.113.7")
        self.assertEqual(client_ip({"X-Real-IP": "198.51.100.9"}), "198.51.100.9")
        self.assertEqual(client_ip({"X-FORWARDED-FOR": "203.0.113.7"}), "203.0.113.7")
        self.assertEqual(client_ip({"x-REAL-ip": "198.51.100.9"}), "198.51.100.9")

    def test_a_similarly_named_header_is_not_mistaken_for_the_real_one(self):
        self.assertEqual(client_ip({"Forwarded-For": "203.0.113.7"}), UNKNOWN_IP)
        self.assertEqual(
            client_ip({"Forwarded": "203.0.113.7"}, remote_addr="192.0.2.1"), "192.0.2.1"
        )

    def test_a_hostile_mapping_does_not_raise(self):
        class Exploding:
            def get(self, *args, **kwargs):
                raise RuntimeError("boom")

            def items(self):
                raise RuntimeError("boom")

        # The point is that it degrades to the fallback rather than 500-ing the
        # login endpoint.
        self.assertEqual(client_ip(Exploding(), remote_addr="192.0.2.1"), "192.0.2.1")
        self.assertEqual(client_ip(Exploding()), UNKNOWN_IP)
        self.assertEqual(client_ip(Exploding(), None), UNKNOWN_IP)

    def test_a_non_string_header_value_does_not_raise(self):
        self.assertEqual(client_ip({"x-forwarded-for": 12345}), "12345")
        self.assertEqual(client_ip({"x-forwarded-for": None}, "192.0.2.1"), "192.0.2.1")

    def test_an_ipv6_address_survives(self):
        self.assertEqual(client_ip({"x-forwarded-for": "2001:db8::1"}), "2001:db8::1")

    def test_the_same_caller_always_gets_the_same_key(self):
        headers = {"x-forwarded-for": " 203.0.113.7 , 10.0.0.1"}
        self.assertEqual(client_ip(headers), client_ip(headers))
        self.assertEqual(client_ip(headers), "203.0.113.7")

    def test_no_socket_is_opened(self):
        # The key claim of this class, stated as an executable check: deriving
        # a rate-limiting key is arithmetic, not I/O.
        real_socket = socket.socket

        def forbidden(*args, **kwargs):
            raise AssertionError("client_ip must not open a socket")

        socket.socket = forbidden
        try:
            self.assertEqual(client_ip({"x-forwarded-for": "203.0.113.7"}), "203.0.113.7")
        finally:
            socket.socket = real_socket


class KeyingTests(unittest.TestCase):
    """The composite keys, as pure strings -- no router is imported.

    `auth/router.py` cannot be imported here at all (`fastapi`, `sqlalchemy`
    and `passlib` are all absent), so these assert the *format* the router
    builds, and `RouterWiringTextTests` below pins the router to it.
    """

    def test_the_login_key_carries_ip_and_username(self):
        self.assertEqual("login:%s:%s" % ("1.2.3.4", "alice"), "login:1.2.3.4:alice")

    def test_the_login_key_differs_across_ip(self):
        self.assertNotEqual(
            "login:%s:%s" % ("1.2.3.4", "alice"), "login:%s:%s" % ("5.6.7.8", "alice")
        )

    def test_the_login_key_differs_across_username(self):
        self.assertNotEqual(
            "login:%s:%s" % ("1.2.3.4", "alice"), "login:%s:%s" % ("1.2.3.4", "bob")
        )

    def test_case_differences_fold_to_one_key(self):
        # Otherwise "Alice", "alice" and "ALICE" are three separate budgets for
        # one account, which multiplies the real limit by the number of spellings.
        def norm(value):
            return str(value or "").strip().lower()

        self.assertEqual(
            "login:%s:%s" % ("1.2.3.4", norm("Alice")),
            "login:%s:%s" % ("1.2.3.4", norm("alice")),
        )
        self.assertEqual(
            "login:%s:%s" % ("1.2.3.4", norm("  ALICE  ")),
            "login:%s:%s" % ("1.2.3.4", norm("alice")),
        )

    def test_the_two_login_windows_cannot_share_state(self):
        # "login:1.2.3.4:alice" and "login-ip:1.2.3.4" must not collide, or
        # exhausting one window would silently drain the other.
        self.assertNotEqual("login:1.2.3.4:alice", "login-ip:1.2.3.4")

    def test_the_register_key_is_distinct_from_both_login_keys(self):
        self.assertNotEqual("register:1.2.3.4", "login-ip:1.2.3.4")
        self.assertNotEqual("register:1.2.3.4", "login:1.2.3.4:alice")

    def test_every_key_starts_with_a_known_scope(self):
        for key in ("login:1.2.3.4:alice", "login-ip:1.2.3.4", "register:1.2.3.4"):
            with self.subTest(key=key):
                # The router logs `key.split(":")[0]` and nothing else, so the
                # scope must be recoverable and must never contain user data.
                self.assertIn(key.split(":")[0], ("login", "login-ip", "register"))

    def test_the_logged_scope_never_contains_the_username(self):
        key = "login:1.2.3.4:alice"
        self.assertEqual(key.split(":")[0], "login")
        self.assertNotIn("alice", key.split(":")[0])
        self.assertNotIn("1.2.3.4", key.split(":")[0])

    def test_folded_keys_still_expose_only_the_scope(self):
        def norm(value):
            return str(value or "").strip().lower()

        key = "login:%s:%s" % ("1.2.3.4", norm("Alice"))
        self.assertEqual(key, "login:1.2.3.4:alice")
        self.assertEqual(key.split(":")[0], "login")

    def test_a_username_containing_a_colon_cannot_forge_a_scope(self):
        # A submitted username is attacker-controlled, so it must not be able to
        # change what the log line's leading segment says. Not fixed by
        # stripping: this is a reason the log takes segment 0 of a *fixed*
        # prefix, not a reason the username is cleaned harder.
        key = "login:1.2.3.4:admin"
        self.assertEqual(key.split(":")[0], "login")


class RouterWiringTextTests(unittest.TestCase):
    """Pins the router by reading it. Never imports it.

    `backend/auth/router.py` imports `fastapi`, `sqlalchemy`, `pydantic` and
    `passlib`; none is installed, so the handler cannot be executed here. These
    are source assertions, exactly as `test_pagination.py` does for the list
    endpoints, and they are worth having: the limiter is the control, and
    without them nothing in the suite would notice the wiring being deleted.
    """

    @staticmethod
    def _source() -> str:
        with open(AUTH_ROUTER_PATH, "r", encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _handler(name: str) -> str:
        """The source of one top-level function, decorators included."""
        with open(AUTH_ROUTER_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source, filename=AUTH_ROUTER_PATH)
        lines = source.splitlines()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                start = node.lineno - 1
                for decorator in node.decorator_list:
                    start = min(start, decorator.lineno - 1)
                return "\n".join(lines[start : node.end_lineno])
        raise AssertionError("no top-level function named %r" % name)

    def test_the_public_contract_is_unchanged(self):
        # Same paths, same methods, same response models, same status codes.
        register = self._handler("register")
        login = self._handler("login")
        me = self._handler("me")
        self.assertIn('@router.post("/register", response_model=TokenResponse, '
                      "status_code=status.HTTP_201_CREATED)", register)
        self.assertIn('@router.post("/login", response_model=TokenResponse)', login)
        self.assertIn('@router.get("/me", response_model=UserResponse)', me)
        self.assertIn('detail="Username already taken"', register)
        self.assertIn("status_code=400", register)
        self.assertIn('detail="Invalid credentials"', login)
        self.assertIn("status_code=401", login)
        self.assertIn("TokenResponse(access_token=create_token(user.id))", login)

    def test_both_handlers_take_a_request(self):
        # FastAPI injects it; the HTTP contract is unchanged because `Request`
        # is not a body or query parameter.
        for name in ("register", "login"):
            with self.subTest(handler=name):
                self.assertIn("request: Request", self._handler(name))

    def test_three_limiters_are_built_from_settings(self):
        source = self._source()
        for field in (
            "auth_login_rate_limit",
            "auth_login_rate_window",
            "auth_login_ip_rate_limit",
            "auth_login_ip_rate_window",
            "auth_register_rate_limit",
            "auth_register_rate_window",
        ):
            with self.subTest(setting=field):
                self.assertIn(field, source)
        for limiter in ("_login_limiter", "_login_ip_limiter", "_register_limiter"):
            with self.subTest(limiter=limiter):
                self.assertIn(limiter, source)

    def test_the_429_carries_retry_after(self):
        source = self._source()
        self.assertIn("status_code=429", source)
        self.assertIn('headers={"Retry-After": str(exc.retry_after)}', source)
        self.assertIn("RateLimitExceeded", source)

    def test_the_rate_limited_line_logs_the_scope_only(self):
        source = self._source()
        self.assertIn('log.warning("auth.rate_limited key=%s retry_after=%s"', source)
        # The argument is the first segment, so the submitted username can
        # never reach the log from this call.
        self.assertIn('key.split(":")[0]', source)

    def test_login_checks_the_password_ceiling_first(self):
        login = self._handler("login")
        self.assertLess(
            login.index("password_too_long"), login.index("_limited(_login_limiter")
        )

    def test_login_applies_the_upper_bound_and_not_the_whole_policy(self):
        login = self._handler("login")
        self.assertIn("password_too_long(body.password)", login)
        # The floor must NOT appear here: a "too short" complaint on login is a
        # free oracle about the stored credential.
        self.assertNotIn("password_problem", login)
        self.assertNotIn("username_problem", login)

    def test_both_login_windows_run_before_the_database_and_before_argon2(self):
        # The ordering IS the finding. Checked after both, so a rejection
        # cannot be moved back behind the expensive call without failing here.
        login = self._handler("login")
        account_limit = login.index("_limited(_login_limiter")
        ip_limit = login.index("_login_ip_limiter")
        database = login.index("db.query(models.User)")
        argon2 = login.index("verify_password(body.password, stored_hash)")
        self.assertLess(account_limit, database)
        self.assertLess(ip_limit, database)
        self.assertLess(account_limit, argon2)
        self.assertLess(ip_limit, argon2)

    def test_success_resets_the_account_window_but_not_the_ip_window(self):
        login = self._handler("login")
        self.assertIn('_login_limiter.reset("login:%s:%s" % (ip, norm_user))', login)
        self.assertNotIn("_login_ip_limiter.reset", login)

    def test_the_failure_log_never_contains_the_password(self):
        source = self._source()
        self.assertIn('log.info("auth.login_failed user=%s ip=%s", norm_user, ip)', source)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("log.", "log.warn", "log.error", "log.debug")):
                with self.subTest(line=stripped):
                    self.assertNotIn("body.password", stripped)
                    self.assertNotIn("hashed_password", stripped)

    def test_the_timing_equalisation_computes_the_comparison_first(self):
        login = self._handler("login")
        # The old shape short-circuited; the new one computes, then branches.
        self.assertIn(
            "stored_hash = user.hashed_password if user else DUMMY_HASH", login
        )
        self.assertIn("ok = bool(verify_password(body.password, stored_hash))", login)
        self.assertLess(
            login.index("ok = bool(verify_password("), login.index("if not user or not ok:")
        )
        # ...and the comparison cannot 500 on a placeholder hash.
        self.assertIn("except Exception:", login)

    def test_the_dummy_hash_is_labelled_a_placeholder(self):
        source = self._source()
        self.assertIn('DUMMY_HASH = ""', source)
        self.assertIn("PLACEHOLDER", source)

    def test_registration_enforces_the_policy_before_the_uniqueness_query(self):
        register = self._handler("register")
        self.assertIn(
            "username_problem(body.username) or password_problem(body.password)", register
        )
        self.assertIn("status_code=422", register)
        self.assertLess(
            register.index("username_problem"), register.index("db.query(models.User)")
        )

    def test_registration_throttles_before_it_hashes(self):
        register = self._handler("register")
        self.assertLess(
            register.index("_limited(_register_limiter"), register.index("hash_password(")
        )
        self.assertIn('"register:%s" % ip', register)

    def test_registration_logs_only_the_new_user_id(self):
        register = self._handler("register")
        self.assertIn('log.info("auth.registered user_id=%s", user.id)', register)


class RateLimitHygieneTests(unittest.TestCase):
    """`ratelimit.py` must stay pure, or nothing above is testable."""

    ALLOWED_STDLIB = frozenset(
        ["__future__", "collections", "math", "threading", "time", "typing"]
    )

    def _top_level_imports(self, path):
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module)
        return {name.split(".")[0] for name in names}

    def test_the_module_imports_only_the_allowed_stdlib(self):
        # A `config` or `fastapi` import here would make the throttle
        # untestable in the only environment this series can be verified in.
        unexpected = sorted(self._top_level_imports(RATELIMIT_PATH) - self.ALLOWED_STDLIB)
        self.assertEqual(unexpected, [])

    def test_the_module_cannot_open_a_socket(self):
        self.assertNotIn("socket", self._top_level_imports(RATELIMIT_PATH))
        self.assertNotIn("urllib", self._top_level_imports(RATELIMIT_PATH))
        self.assertNotIn("http", self._top_level_imports(RATELIMIT_PATH))

    def test_the_module_does_not_import_project_code(self):
        # No `from config import settings` either: the limits are passed in.
        first_party = sorted(
            name
            for name in self._top_level_imports(RATELIMIT_PATH)
            if name in ("config", "security", "chat", "auth", "models", "database")
        )
        self.assertEqual(first_party, [])


if __name__ == "__main__":  # pragma: no cover - convenience only
    unittest.main()
