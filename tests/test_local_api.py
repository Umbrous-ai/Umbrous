"""M0 acceptance: the local API refuses everything that is not this page, on this machine."""

import json
import socket
import time
import unittest

from umbrous.api.security import CLAIM_PATH
from tests.helpers import RunningRuntime


def non_loopback_ipv4_addresses():
    """This machine's own non-loopback IPv4 addresses (e.g. its Wi-Fi address)."""
    found = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # TEST-NET address; UDP connect sends nothing, just picks a route
        found.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in found if not a.startswith("127.") and a != "0.0.0.0")


class LocalApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rt = RunningRuntime().__enter__()
        cls.token = cls.rt.policy.session_token

    @classmethod
    def tearDownClass(cls):
        cls.rt.__exit__(None, None, None)

    # --- the happy path -------------------------------------------------
    def test_page_is_served_with_security_headers(self):
        status, headers, body = self.rt.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="runtime"', body)
        self.assertIn(b"/runtime-bridge.js", body)
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("'sha256-", csp)
        self.assertNotIn("'unsafe-inline'", csp.split("script-src", 1)[1].split(";", 1)[0])
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["x-content-type-options"], "nosniff")

    def test_health_with_credential(self):
        status, body = self.rt.json("GET", "/api/health", token=self.token, headers={"Origin": self.rt.origin})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["milestone"], "M3")
        self.assertEqual(body["schema_version"], 4)
        self.assertEqual(body["bind"], self.rt.host)
        self.assertGreaterEqual(body["launches"], 1)

    def test_localhost_name_is_also_accepted(self):
        status, _ = self.rt.json("GET", "/api/health", token=self.token, host=f"localhost:{self.rt.port}")
        self.assertEqual(status, 200)

    # --- session credential ----------------------------------------------
    def test_health_without_credential_is_refused(self):
        status, body = self.rt.json("GET", "/api/health")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "missing_or_wrong_session_credential")

    def test_wrong_credential_is_refused(self):
        status, _ = self.rt.json("GET", "/api/health", token=self.token[:-1] + ("A" if self.token[-1] != "A" else "B"))
        self.assertEqual(status, 401)

    def test_unknown_api_path_needs_credential_before_revealing_404(self):
        self.assertEqual(self.rt.request("GET", "/api/nope")[0], 401)
        self.assertEqual(self.rt.request("GET", "/api/nope", token=self.token)[0], 404)

    # --- Origin ----------------------------------------------------------
    def test_foreign_origin_is_refused_even_with_credential(self):
        for origin in ("https://evil.example", "http://127.0.0.1:1", "null", f"http://127.0.0.1:{self.rt.port}.evil.example"):
            with self.subTest(origin=origin):
                status, body = self.rt.json("GET", "/api/health", token=self.token, headers={"Origin": origin})
                self.assertEqual(status, 403)
                self.assertEqual(body["error"], "origin_not_allowed")

    def test_cross_site_fetch_metadata_is_refused(self):
        status, _ = self.rt.json("GET", "/api/health", token=self.token, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_cors_preflight_is_refused_without_cors_headers(self):
        status, headers, _ = self.rt.request("OPTIONS", "/api/health", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-umbrous-token"})
        self.assertEqual(status, 403)
        self.assertNotIn("access-control-allow-origin", headers)

    # --- Host (DNS rebinding) ----------------------------------------------
    def test_unexpected_host_is_refused_even_with_credential(self):
        for host in (f"evil.example:{self.rt.port}", "127.0.0.1", f"127.0.0.1:{self.rt.port + 1}", f"0.0.0.0:{self.rt.port}", ""):
            with self.subTest(host=host):
                status, body = self.rt.json("GET", "/api/health", token=self.token, host=host)
                self.assertEqual(status, 403)
                self.assertEqual(body["error"], "host_not_allowed")

    def test_rebinding_attack_cannot_read_page_either(self):
        self.assertEqual(self.rt.request("GET", "/", host=f"evil.example:{self.rt.port}")[0], 403)

    # --- misc hardening ---------------------------------------------------
    def test_path_tricks_do_not_serve_files(self):
        for path in ("/../../etc/passwd", "/prototypes/umbrous-harness.html", "/%2e%2e/README.md", "/umbrous/config.py"):
            with self.subTest(path=path):
                self.assertEqual(self.rt.request("GET", path)[0], 404)

    def test_oversized_body_is_refused(self):
        status, _, _ = self.rt.request("POST", CLAIM_PATH, body=b"x" * 300_000)
        self.assertEqual(status, 413)

    def test_write_methods_on_page_are_refused(self):
        self.assertEqual(self.rt.request("POST", "/", body=b"")[0], 405)

    # --- loopback only ------------------------------------------------------
    def test_socket_is_bound_to_loopback_only(self):
        self.assertEqual(self.rt.server.server_address[0], "127.0.0.1")

    def test_not_reachable_on_non_loopback_addresses(self):
        addresses = non_loopback_ipv4_addresses()
        if not addresses:
            self.skipTest("this machine has no non-loopback IPv4 address to try")
        for addr in addresses:
            with self.subTest(addr=addr):
                with self.assertRaises(OSError):  # ConnectionRefusedError (or timeout) — never a response
                    socket.create_connection((addr, self.rt.port), timeout=2).close()


class LogTruncationTests(unittest.TestCase):
    def test_long_path_is_redacted_before_it_is_truncated(self):
        # Codex review repro: "/" + 180 filler + "/" + token used to leave a token prefix in the log.
        with RunningRuntime() as rt:
            token = rt.policy.session_token
            rt.request("GET", "/" + "a" * 180 + "/" + token)
            for _ in range(100):
                logfile = (rt.config.log_dir / "runtime.log").read_text()
                if '"http.request"' in logfile:
                    break
                time.sleep(0.02)
            for n in (8, 12, 18):
                self.assertNotIn(token[:n], logfile)
            self.assertIn("/[REDACTED:", logfile)


class LaunchCodeTests(unittest.TestCase):
    def test_launch_code_works_once(self):
        with RunningRuntime() as rt:
            code = rt.policy.launch_code
            status, body = rt.json("POST", CLAIM_PATH, headers={"X-Umbrous-Launch": code, "Origin": rt.origin}, body=b"")
            self.assertEqual(status, 200)
            self.assertEqual(body["token"], rt.policy.session_token)
            status, body = rt.json("POST", CLAIM_PATH, headers={"X-Umbrous-Launch": code}, body=b"")
            self.assertEqual(status, 401)
            self.assertNotIn("token", body)

    def test_wrong_launch_code_does_not_burn_the_real_one(self):
        with RunningRuntime() as rt:
            self.assertEqual(rt.request("POST", CLAIM_PATH, headers={"X-Umbrous-Launch": "guess"}, body=b"")[0], 401)
            self.assertEqual(rt.request("POST", CLAIM_PATH, headers={"X-Umbrous-Launch": rt.policy.launch_code}, body=b"")[0], 200)

    def test_claim_from_foreign_origin_is_refused(self):
        with RunningRuntime() as rt:
            status, _ = rt.json("POST", CLAIM_PATH, headers={"X-Umbrous-Launch": rt.policy.launch_code,
                                                            "Origin": "https://evil.example"}, body=b"")
            self.assertEqual(status, 403)
            self.assertTrue(rt.policy.launch_code_available())

    def test_each_launch_gets_a_new_credential(self):
        with RunningRuntime() as a, RunningRuntime() as b:
            self.assertNotEqual(a.policy.session_token, b.policy.session_token)
            self.assertEqual(b.request("GET", "/api/health", token=a.policy.session_token, host=b.host)[0], 401)


class LoggingTests(unittest.TestCase):
    def test_logs_are_json_and_never_contain_credentials(self):
        with RunningRuntime() as rt:
            rt.request("GET", "/api/health", token=rt.policy.session_token)
            rt.request("POST", CLAIM_PATH, headers={"X-Umbrous-Launch": rt.policy.launch_code}, body=b"")
            rt.request("GET", "/api/health?token=" + rt.policy.session_token)
            logfile = ""
            for _ in range(100):  # the access line is written just after the response is sent
                logfile = (rt.config.log_dir / "runtime.log").read_text()
                if logfile.count('"http.request"') >= 3:
                    break
                time.sleep(0.02)
            for text in (logfile, rt.log_stream.getvalue()):
                self.assertNotIn(rt.policy.session_token, text)
                self.assertNotIn(rt.policy.launch_code, text)
            lines = [json.loads(l) for l in logfile.splitlines()]
            requests = [l for l in lines if l["event"] == "http.request"]
            self.assertEqual([r["status"] for r in requests], [200, 200, 401])
            self.assertTrue(all("path" in r and "ms" in r for r in requests))
            self.assertEqual((rt.config.log_dir / "runtime.log").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
