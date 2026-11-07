"""Unit tests for the access policy, without a server."""

import unittest

from umbrous.api.security import AccessPolicy, evaluate


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.p = AccessPolicy(8417, clock=self.clock)
        self.ok = {"Host": "127.0.0.1:8417", "X-Umbrous-Token": self.p.session_token}

    def test_launch_code_expires(self):
        self.clock.t += 121
        self.assertIsNone(self.p.claim(self.p.launch_code))

    def test_launch_code_before_expiry(self):
        self.clock.t += 119
        self.assertEqual(self.p.claim(self.p.launch_code), self.p.session_token)

    def test_secrets_are_long_and_distinct(self):
        self.assertGreaterEqual(len(self.p.session_token), 43)
        self.assertNotEqual(self.p.session_token, self.p.launch_code)
        self.assertNotEqual(self.p.session_token, AccessPolicy(8417).session_token)

    def test_host_header_is_case_insensitive_but_exact(self):
        self.assertTrue(self.p.check_host("LOCALHOST:8417"))
        self.assertFalse(self.p.check_host("localhost.evil.example:8417"))
        self.assertFalse(self.p.check_host(None))

    def test_same_origin_fetch_metadata_passes(self):
        h = dict(self.ok, **{"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1:8417"})
        self.assertTrue(evaluate(self.p, "GET", "/api/health", h).ok)

    def test_same_site_is_not_same_origin(self):
        h = dict(self.ok, **{"Sec-Fetch-Site": "same-site"})
        self.assertEqual(evaluate(self.p, "GET", "/api/health", h).reason, "origin_not_allowed")

    def test_https_variant_of_own_origin_is_refused(self):
        h = dict(self.ok, Origin="https://127.0.0.1:8417")
        self.assertFalse(evaluate(self.p, "GET", "/api/health", h).ok)

    def test_order_host_checked_before_credential(self):
        d = evaluate(self.p, "GET", "/api/health", {"Host": "evil.example:8417"})
        self.assertEqual((d.status, d.reason), (403, "host_not_allowed"))


if __name__ == "__main__":
    unittest.main()
