"""Executable proof of the SEC-1 outbound guard: `security/netguard.py`.

Audit finding 12 was an authenticated SSRF read-oracle: a registered user
chose the `base_url`, the server connected to it with its own HTTP client
and handed the response body back in the error message. The guard is pure
and stdlib-only precisely so that *this* file can prove it works on a bare
Python with nothing installed -- and, in the one place that could bite, so
that **no test in this suite ever opens a socket**.

That last point is why `resolve_addresses()` takes an injectable
`resolver=`. Every DNS test in `ResolveAddressesTests` passes a lambda, so
the suite is deterministic and offline. A test that quietly depends on a
real resolver is a test that fails in CI and passes on a laptop.

Run with::

    python3 -m unittest discover -s backend/tests -t backend -v
"""

from __future__ import annotations

import os
import socket
import sys
import unittest

try:
    from security.netguard import (
        ALWAYS_BLOCKED,
        DOCKER_HOSTNAME,
        MAX_URL_LENGTH,
        NetGuardError,
        UpstreamProviderError,
        check_resolved_target,
        classify_address,
        normalise_base_url,
        parse_endpoint,
        public_error_message,
        redact_body,
        redact_url,
        resolve_addresses,
    )
except ImportError:  # pragma: no cover - exercised only by direct execution
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    from security.netguard import (
        ALWAYS_BLOCKED,
        DOCKER_HOSTNAME,
        MAX_URL_LENGTH,
        NetGuardError,
        UpstreamProviderError,
        check_resolved_target,
        classify_address,
        normalise_base_url,
        parse_endpoint,
        public_error_message,
        redact_body,
        redact_url,
        resolve_addresses,
    )


def fake_resolver(addresses):
    """A `socket.getaddrinfo`-shaped callable that resolves to `addresses`.

    Entry shape is `(family, socktype, proto, canonname, sockaddr)`, and the
    guard reads the address out of `entry[4][0]` exactly as it would from the
    real thing.
    """
    def _resolver(host, port, type=None, *args, **kwargs):  # noqa: A002
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
            for address in addresses
        ]

    return _resolver


class AddressClassificationTests(unittest.TestCase):
    """Every address category the policy can be handed."""

    def test_the_cloud_metadata_endpoint_is_link_local_not_private(self):
        # It is *both* in the standard library's vocabulary. Reporting it as
        # "private" would hide the one address this guard exists to stop.
        self.assertEqual(classify_address("169.254.169.254"), "link_local")

    def test_the_whole_of_127_0_0_0_8_is_loopback(self):
        for address in ("127.0.0.1", "127.0.0.53", "127.1.2.3", "127.255.255.254"):
            with self.subTest(address=address):
                self.assertEqual(classify_address(address), "loopback")

    def test_ipv6_loopback_and_link_local(self):
        self.assertEqual(classify_address("::1"), "loopback")
        self.assertEqual(classify_address("fe80::1"), "link_local")

    def test_rfc1918_ranges_are_private(self):
        for address in ("10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.1"):
            with self.subTest(address=address):
                self.assertEqual(classify_address(address), "private")

    def test_unspecified_is_its_own_category(self):
        self.assertEqual(classify_address("0.0.0.0"), "unspecified")
        self.assertEqual(classify_address("::"), "unspecified")

    def test_multicast(self):
        self.assertEqual(classify_address("224.0.0.1"), "multicast")
        self.assertEqual(classify_address("ff02::1"), "multicast")

    def test_reserved(self):
        # `::2` sits in ::/8, which the standard library reports as reserved
        # and *not* private -- the only reachable way to reach the `reserved`
        # branch, since almost every reserved range is also private.
        self.assertEqual(classify_address("::2"), "reserved")

    def test_public_ipv4_and_ipv6(self):
        self.assertEqual(classify_address("8.8.8.8"), "public")
        self.assertEqual(classify_address("2606:4700:4700::1111"), "public")

    def test_garbage_is_invalid_and_never_raises(self):
        for value in ("not-an-ip", "", "999.999.999.999", "example.com", None, 42):
            with self.subTest(value=value):
                self.assertEqual(classify_address(value), "invalid")

    def test_an_ipv4_mapped_address_is_judged_by_what_it_points_at(self):
        # A resolver may answer over IPv6 with `::ffff:169.254.169.254`.
        # Classifying that as "private" would let the permissive flag reach
        # the metadata endpoint through the mapped spelling.
        self.assertEqual(classify_address("::ffff:169.254.169.254"), "link_local")
        self.assertEqual(classify_address("::ffff:127.0.0.1"), "loopback")

    def test_every_returned_value_is_one_of_the_eight_known_categories(self):
        known = {
            "public", "private", "loopback", "link_local",
            "multicast", "unspecified", "reserved", "invalid",
        }
        for value in ("8.8.8.8", "10.0.0.1", "::1", "nope", "240.0.0.1", ""):
            with self.subTest(value=value):
                self.assertIn(classify_address(value), known)


class ParseEndpointTests(unittest.TestCase):
    """The URL half of the guard, in the order the checks actually run."""

    def assertRejected(self, code, url, in_docker=False):
        with self.assertRaises(NetGuardError) as caught:
            parse_endpoint(url, in_docker=in_docker)
        self.assertEqual(
            caught.exception.code,
            code,
            "%r should be refused as %r, got %r"
            % (url, code, caught.exception.code),
        )

    def test_a_normal_public_url_round_trips(self):
        endpoint = parse_endpoint("https://openrouter.ai/api/v1")
        self.assertEqual(endpoint.scheme, "https")
        self.assertEqual(endpoint.host, "openrouter.ai")
        self.assertEqual(endpoint.port, 443)
        self.assertEqual(endpoint.path, "/api/v1")
        self.assertEqual(endpoint.base_url, "https://openrouter.ai/api/v1")

    def test_the_default_port_follows_the_scheme(self):
        self.assertEqual(parse_endpoint("http://example.com").port, 80)
        self.assertEqual(parse_endpoint("https://example.com").port, 443)
        self.assertEqual(parse_endpoint("http://example.com:8080").port, 8080)

    def test_only_http_and_https_are_accepted(self):
        self.assertRejected("bad_scheme", "file:///etc/passwd")
        self.assertRejected("bad_scheme", "gopher://10.0.0.5:70/")
        # Schemeless is refused as a bad scheme, which is the right code:
        # `urlsplit` reads "localhost" as the scheme of "localhost:11434".
        self.assertRejected("bad_scheme", "localhost:11434")
        self.assertRejected("bad_scheme", "ftp://example.com")

    def test_the_scheme_is_case_folded(self):
        endpoint = parse_endpoint("HTTPS://Example.COM/api")
        self.assertEqual(endpoint.scheme, "https")
        self.assertEqual(endpoint.host, "example.com")
        self.assertEqual(endpoint.base_url, "https://example.com/api")

    def test_a_trailing_root_dot_is_stripped_from_the_host(self):
        endpoint = parse_endpoint("https://example.com./api")
        self.assertEqual(endpoint.host, "example.com")

    def test_userinfo_is_refused_before_anything_else_looks_at_it(self):
        # The exact payload from audit finding 12, aimed at the cloud
        # metadata service.
        self.assertRejected(
            "userinfo_not_allowed", "http://user:pass@169.254.169.254/"
        )
        self.assertRejected("userinfo_not_allowed", "http://a@b@example.com/")

    def test_ports_outside_the_legal_range_are_refused(self):
        self.assertRejected("bad_port", "http://example.com:0/")
        self.assertRejected("bad_port", "http://example.com:99999/")
        self.assertRejected("bad_port", "http://example.com:notaport/")

    def test_malformed_values(self):
        self.assertRejected("malformed_url", "")
        self.assertRejected("malformed_url", "   ")
        self.assertRejected("malformed_url", None)
        self.assertRejected("malformed_url", 12345)
        self.assertRejected("malformed_url", "x" * (MAX_URL_LENGTH + 1))
        self.assertRejected("malformed_url", "http://exa mple.com/")

    def test_a_crlf_injected_url_is_refused(self):
        # Header injection into the outbound request, or a log line forged
        # from a URL. One character allow-list stops both.
        self.assertRejected(
            "malformed_url", "http://example.com/\r\nX-Evil: 1"
        )
        self.assertRejected("malformed_url", "http://exa\tmple.com/")
        self.assertRejected("malformed_url", "http://example.com/\x00")

    def test_a_length_exactly_at_the_cap_is_accepted(self):
        # The cap is `>`, not `>=`: an over-long value is refused, a
        # boundary value is not, and the test says which.
        host = "http://example.com/" + ("a" * (MAX_URL_LENGTH - len("http://example.com/")))
        self.assertEqual(len(host), MAX_URL_LENGTH)
        self.assertEqual(parse_endpoint(host).host, "example.com")

    def test_trailing_slashes_are_stripped_so_the_completion_url_is_unchanged(self):
        # The caller does f"{base_url}/chat/completions", so the old
        # `rstrip("/")` behaviour is load-bearing for a byte-identical URL.
        self.assertEqual(
            parse_endpoint("http://localhost:11434/v1/").base_url,
            "http://localhost:11434/v1",
        )
        self.assertEqual(
            parse_endpoint("http://localhost:11434/v1").base_url
            + "/chat/completions",
            "http://localhost:11434/v1/chat/completions",
        )
        self.assertEqual(
            parse_endpoint("http://example.com///").base_url, "http://example.com"
        )

    def test_query_and_fragment_are_dropped(self):
        # Documented behaviour change: a base URL is not a document, and
        # `f"{base}/chat/completions"` on a URL with a query used to build a
        # path with a `?` in the middle of it.
        endpoint = parse_endpoint("http://example.com/v1?api_key=sk-secret#frag")
        self.assertEqual(endpoint.base_url, "http://example.com/v1")
        self.assertNotIn("sk-secret", endpoint.base_url)
        self.assertNotIn("frag", endpoint.base_url)

    def test_ipv6_literals_keep_their_brackets_in_the_rebuilt_url(self):
        endpoint = parse_endpoint("http://[::1]:11434/v1")
        self.assertEqual(endpoint.host, "::1")
        self.assertEqual(endpoint.base_url, "http://[::1]:11434/v1")

    def test_normalise_base_url_is_the_thin_wrapper(self):
        self.assertEqual(
            normalise_base_url("https://example.com/api/"),
            parse_endpoint("https://example.com/api/").base_url,
        )
        self.assertEqual(normalise_base_url("https://example.com"), "https://example.com")


class DockerRewriteTests(unittest.TestCase):
    """`localhost` inside a container, rewritten in the host only."""

    def test_loopback_hosts_become_the_docker_gateway(self):
        for url in ("http://localhost:11434/v1", "http://127.0.0.1:11434/v1"):
            with self.subTest(url=url):
                endpoint = parse_endpoint(url, in_docker=True)
                self.assertEqual(endpoint.host, DOCKER_HOSTNAME)
                self.assertEqual(endpoint.port, 11434)
                self.assertEqual(
                    endpoint.base_url, "http://%s:11434/v1" % DOCKER_HOSTNAME
                )

    def test_ipv6_loopback_is_rewritten_too(self):
        endpoint = parse_endpoint("http://[::1]:11434/v1", in_docker=True)
        self.assertEqual(endpoint.host, DOCKER_HOSTNAME)

    def test_outside_docker_nothing_is_rewritten(self):
        endpoint = parse_endpoint("http://localhost:11434/v1")
        self.assertEqual(endpoint.host, "localhost")
        self.assertEqual(endpoint.base_url, "http://localhost:11434/v1")

    def test_a_real_host_is_untouched(self):
        endpoint = parse_endpoint("https://openrouter.ai/api/v1", in_docker=True)
        self.assertEqual(endpoint.host, "openrouter.ai")

    def test_the_rewrite_is_host_scoped_not_substring_scoped(self):
        # The old code was `re.sub(r"(localhost|127\\.0\\.0\\.1)", ...)` over
        # the WHOLE url, so `?model=localhost` or a path segment containing
        # the word would be rewritten too -- changing a string the caller
        # never meant to change. Only the host changes now.
        endpoint = parse_endpoint(
            "https://api.example.com/v1?model=localhost", in_docker=True
        )
        self.assertEqual(endpoint.host, "api.example.com")
        # And the query is dropped rather than rewritten-and-kept.
        self.assertEqual(endpoint.base_url, "https://api.example.com/v1")
        self.assertNotIn("host.docker.internal", endpoint.base_url)

    def test_userinfo_is_refused_before_the_rewrite_can_touch_it(self):
        # Order matters: if the rewrite ran first, `user@localhost` would
        # become `user@host.docker.internal` and the credential check would
        # be laundering a userinfo section through the guard.
        with self.assertRaises(NetGuardError) as caught:
            parse_endpoint("http://user:pass@localhost:11434/v1", in_docker=True)
        self.assertEqual(caught.exception.code, "userinfo_not_allowed")


class CheckResolvedTargetTests(unittest.TestCase):
    """The address policy, applied to a host that has already resolved."""

    def test_a_public_address_passes_by_default(self):
        self.assertEqual(
            check_resolved_target(
                "https://example.com/api",
                ["93.184.216.34"],
                allow_private=False,
                origin="request",
            ),
            "https://example.com/api",
        )

    def test_an_internal_address_is_refused_by_default(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "169.254.169.254", "0.0.0.0"):
            with self.subTest(address=address):
                with self.assertRaises(NetGuardError) as caught:
                    check_resolved_target(
                        "http://evil.example/",
                        [address],
                        allow_private=False,
                        origin="request",
                    )
                self.assertEqual(caught.exception.code, "blocked_address")

    def test_the_flag_re_permits_loopback_and_rfc1918(self):
        for address in ("127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.4.4"):
            with self.subTest(address=address):
                self.assertEqual(
                    check_resolved_target(
                        "http://ollama.lan:11434/v1",
                        [address],
                        allow_private=True,
                        origin="request",
                    ),
                    "http://ollama.lan:11434/v1",
                )

    def test_the_flag_does_not_re_permit_the_always_blocked_categories(self):
        # This is the whole reason the flag is not a switch: the metadata
        # endpoint, multicast, unspecified and garbage stay refused.
        for address in ("169.254.169.254", "fe80::1", "224.0.0.1", "0.0.0.0", "::", "nonsense"):
            with self.subTest(address=address):
                with self.assertRaises(NetGuardError) as caught:
                    check_resolved_target(
                        "http://evil.example/",
                        [address],
                        allow_private=True,
                        origin="request",
                    )
                self.assertEqual(caught.exception.code, "blocked_address")

    def test_always_blocked_is_exactly_the_sticky_set(self):
        for category in ALWAYS_BLOCKED:
            with self.subTest(category=category):
                self.assertNotIn(category, {"public", "private", "loopback"})

    def test_one_bad_address_poisons_the_whole_batch(self):
        # A host with both a public and a loopback A record is exactly what a
        # rebinding attack looks like. "The first address was fine" is not a
        # check, because nobody here gets to choose which one is used.
        with self.assertRaises(NetGuardError) as caught:
            check_resolved_target(
                "https://rebind.example/",
                ["93.184.216.34", "127.0.0.1", "10.0.0.1"],
                allow_private=False,
                origin="request",
            )
        self.assertEqual(caught.exception.code, "blocked_address")

    def test_the_flag_does_not_make_a_mixed_batch_acceptable(self):
        with self.assertRaises(NetGuardError) as caught:
            check_resolved_target(
                "http://ollama.lan/",
                ["10.0.0.5", "169.254.169.254"],
                allow_private=True,
                origin="request",
            )
        self.assertEqual(caught.exception.code, "blocked_address")

    def test_an_empty_answer_is_dns_empty_not_a_pass(self):
        for empty in ([], (), None):
            with self.subTest(empty=empty):
                with self.assertRaises(NetGuardError) as caught:
                    check_resolved_target(
                        "https://example.com/",
                        empty,
                        allow_private=True,
                        origin="request",
                    )
                self.assertEqual(caught.exception.code, "dns_empty")

    def test_the_settings_origin_skips_the_policy_entirely(self):
        # Operator configuration is not attacker-controlled: a private
        # Ollama on the LAN is the documented setup, and no user can reach
        # this branch.
        for address in ("127.0.0.1", "10.1.2.3", "169.254.169.254"):
            with self.subTest(address=address):
                self.assertEqual(
                    check_resolved_target(
                        "http://localhost:11434/v1",
                        [address],
                        allow_private=False,
                        origin="settings",
                    ),
                    "http://localhost:11434/v1",
                )

    def test_the_settings_origin_does_not_even_need_an_answer(self):
        self.assertEqual(
            check_resolved_target(
                "http://localhost:11434/v1", [], allow_private=False, origin="settings"
            ),
            "http://localhost:11434/v1",
        )


class ResolveAddressesTests(unittest.TestCase):
    """Resolution. Every test injects a fake resolver: no socket, ever."""

    def test_a_resolved_host_returns_its_addresses(self):
        self.assertEqual(
            resolve_addresses("example.com", 443, resolver=fake_resolver(["93.184.216.34"])),
            ["93.184.216.34"],
        )

    def test_every_returned_address_is_retained(self):
        addresses = ["93.184.216.34", "93.184.216.35", "93.184.216.36"]
        self.assertEqual(
            resolve_addresses("example.com", 443, resolver=fake_resolver(addresses)),
            addresses,
        )

    def test_the_resolver_is_called_the_way_aiohttp_calls_it(self):
        seen = {}

        def recording(host, port, type=None):  # noqa: A002
            seen["host"] = host
            seen["port"] = port
            seen["type"] = type
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", port))]

        resolve_addresses("example.com", 443, resolver=recording)
        self.assertEqual(seen["host"], "example.com")
        self.assertEqual(seen["port"], 443)
        self.assertEqual(seen["type"], socket.SOCK_STREAM)

    def test_a_link_local_zone_index_is_stripped(self):
        # `fe80::1%en0` is not a valid argument to ip_address, and leaving it
        # would turn a blocked category into "invalid" by accident.
        entries = [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%en0", 11434, 0, 0))
        ]
        self.assertEqual(
            resolve_addresses("fe80", 11434, resolver=lambda h, p, type=None: entries),
            ["fe80::1"],
        )

    def test_a_dns_failure_is_a_dns_failure_not_an_empty_list(self):
        def failing(host, port, type=None):  # noqa: A002
            raise socket.gaierror(-2, "Name or service not known")

        with self.assertRaises(NetGuardError) as caught:
            resolve_addresses("nope.invalid", 443, resolver=failing)
        self.assertEqual(caught.exception.code, "dns_failure")

    def test_any_os_error_is_also_a_dns_failure(self):
        def failing(host, port, type=None):  # noqa: A002
            raise OSError("resolver exploded")

        with self.assertRaises(NetGuardError) as caught:
            resolve_addresses("nope.invalid", 443, resolver=failing)
        self.assertEqual(caught.exception.code, "dns_failure")

    def test_an_empty_answer_is_dns_empty(self):
        for empty in ([], None, ()):
            with self.subTest(empty=empty):
                with self.assertRaises(NetGuardError) as caught:
                    resolve_addresses("example.com", 443, resolver=lambda h, p, type=None: empty)
                self.assertEqual(caught.exception.code, "dns_empty")

    def test_resolve_never_returns_an_empty_list(self):
        # The property, stated directly, because every caller treats `[]` as
        # "allowed" and only a non-empty list is safe to hand to the policy.
        def weird(host, port, type=None):  # noqa: A002
            return [object()]

        try:
            result = resolve_addresses("example.com", 443, resolver=weird)
        except NetGuardError:
            return
        self.assertTrue(result, "resolve_addresses must never hand back []")


class ClientMessageTests(unittest.TestCase):
    """What a refusal is allowed to say."""

    def test_a_blocked_address_names_the_override_flag_verbatim(self):
        message = NetGuardError(
            "blocked_address", "resolved to 169.254.169.254 (link_local)"
        ).client_message()
        self.assertIn("LLM_ALLOW_PRIVATE_HOSTS", message)

    def test_no_client_message_ever_contains_its_reason(self):
        secret = "169.254.169.254 internal-metadata token sk-abcdefgh12345"
        for code in (
            "bad_scheme", "userinfo_not_allowed", "bad_port", "malformed_url",
            "blocked_address", "dns_failure", "dns_empty", "unknown",
        ):
            with self.subTest(code=code):
                message = NetGuardError(code, secret).client_message()
                self.assertNotIn(secret, message)
                self.assertNotIn("169.254.169.254", message)
                self.assertNotIn("sk-", message)

    def test_an_unknown_code_degrades_to_a_generic_refusal(self):
        # A future code must not become a KeyError, and must not become a
        # leaked reason string either.
        error = NetGuardError("a_code_nobody_wrote", "sensitive internal detail")
        self.assertEqual(error.client_message(), "The answer could not be generated.")

    def test_str_of_the_error_is_the_reason_and_that_is_the_only_place(self):
        # Documenting the split: `.reason` is for the log, and `str(exc)` is
        # NOT what any handler should send -- `public_error_message` is.
        error = NetGuardError("blocked_address", "internal detail")
        self.assertEqual(str(error), "internal detail")
        self.assertNotIn("internal detail", error.client_message())

    def test_an_upstream_error_never_puts_the_body_in_str(self):
        error = UpstreamProviderError(
            503, "https://example.com/api", "token=sk-abcdefgh12345 upstream said no"
        )
        self.assertNotIn("sk-abcdefgh12345", str(error))
        self.assertNotIn("upstream said no", str(error))
        self.assertIn("503", str(error))

    def test_an_upstream_client_message_carries_only_the_status(self):
        error = UpstreamProviderError(429, "https://example.com/api", "rate limited")
        self.assertEqual(
            error.client_message(),
            "The language model provider returned an error (429).",
        )
        self.assertNotIn("rate limited", error.client_message())
        self.assertNotIn("example.com", error.client_message())

    def test_the_upstream_attributes_are_still_available_for_the_log(self):
        error = UpstreamProviderError(500, "https://example.com", "boom")
        self.assertEqual(error.status, 500)
        self.assertEqual(error.safe_url, "https://example.com")
        self.assertEqual(error.safe_body, "boom")


class RedactionTests(unittest.TestCase):
    """Log-only redaction. Both helpers must never raise."""

    def test_redact_url_keeps_the_addressable_part(self):
        self.assertEqual(
            redact_url("https://openrouter.ai/api/v1/chat/completions"),
            "https://openrouter.ai/api/v1/chat/completions",
        )

    def test_redact_url_strips_userinfo_query_and_fragment(self):
        self.assertEqual(
            redact_url("http://user:secret@example.com:8080/v1?api_key=sk-abcdefgh#frag"),
            "http://example.com:8080/v1",
        )

    def test_redact_url_never_raises(self):
        for value in (None, "", "   ", "not a url", 12345, "http://", "://x", b"bytes"):
            with self.subTest(value=value):
                result = redact_url(value)
                self.assertIsInstance(result, str)

    def test_an_unparseable_url_never_echoes_its_input(self):
        # The failure mode of a redactor that passes its input through is
        # leaking exactly what it was written to remove.
        self.assertEqual(redact_url("not a url"), "<unparseable url>")
        self.assertNotIn("secret", redact_url("http://user:secret@"))

    def test_redact_body_keeps_only_printable_ascii(self):
        result = redact_body("line one\r\nline two\x00\x07 end")
        self.assertNotIn("\r", result)
        self.assertNotIn("\n", result)
        self.assertNotIn("\x00", result)
        self.assertIn("line one", result)

    def test_redact_body_cannot_forge_a_log_line(self):
        forged = "harmless\n2026-01-01 INFO ragapp.auth login accepted for admin"
        result = redact_body(forged)
        self.assertNotIn("\n", result)
        self.assertEqual(len(result.splitlines()), 1)

    def test_redact_body_scrubs_bearer_tokens(self):
        result = redact_body("Authorization: Bearer abcdef1234567890")
        self.assertNotIn("abcdef1234567890", result)
        self.assertIn("[redacted]", result)

    def test_redact_body_scrubs_openai_style_keys(self):
        self.assertNotIn("sk-abcdefgh12345", redact_body("key sk-abcdefgh12345 here"))

    def test_redact_body_scrubs_named_secrets(self):
        for text in (
            "api_key=supersecret",
            "api-key: supersecret",
            "token = supersecret",
            "secret=supersecret",
            "password=supersecret",
        ):
            with self.subTest(text=text):
                self.assertNotIn("supersecret", redact_body(text))

    def test_redact_body_truncates_with_an_ellipsis(self):
        result = redact_body("x" * 5000, limit=50)
        self.assertEqual(result, "x" * 50 + "...")

    def test_redact_body_of_none_is_empty(self):
        self.assertEqual(redact_body(None), "")

    def test_redact_body_never_raises(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no str for you")

            def __len__(self):
                return 10

        for value in (Hostile(), 12345, object()):
            with self.subTest(value=type(value).__name__):
                self.assertIsInstance(redact_body(value), str)


class PublicErrorMessageTests(unittest.TestCase):
    """The one sink every error path is supposed to go through."""

    def test_a_guard_refusal_surfaces_its_client_message(self):
        error = NetGuardError("blocked_address", "internal: 169.254.169.254")
        self.assertEqual(public_error_message(error), error.client_message())
        self.assertIn("LLM_ALLOW_PRIVATE_HOSTS", public_error_message(error))

    def test_an_upstream_failure_surfaces_only_the_status(self):
        error = UpstreamProviderError(500, "https://example.com", "internal body")
        self.assertEqual(
            public_error_message(error),
            "The language model provider returned an error (500).",
        )

    def test_anything_else_degrades_to_the_fixed_fallback(self):
        # The regression this pins: the old code raised
        # `RuntimeError(f"LLM API error {status} ({url}): {error_text}")` and
        # the router put `str(exc)` straight into the `done` event.
        error = RuntimeError("password=supersecret at /app/secret")
        message = public_error_message(error)
        self.assertEqual(message, "The answer could not be generated.")
        self.assertNotIn("supersecret", message)
        self.assertNotIn("/app/secret", message)

    def test_the_sink_leaks_nothing_from_any_known_error(self):
        cases = [
            NetGuardError("blocked_address", "169.254.169.254"),
            UpstreamProviderError(401, "https://internal.lan/x", "Bearer sk-abcdefgh12345"),
            ValueError("host 10.0.0.1 rejected"),
            KeyError("api_key"),
            Exception(),
        ]
        for error in cases:
            with self.subTest(error=type(error).__name__):
                message = public_error_message(error)
                for leak in ("169.254.169.254", "sk-", "api_key", "10.0.0.1", "internal.lan"):
                    self.assertNotIn(leak, message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
