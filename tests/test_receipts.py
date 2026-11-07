"""Receipt format and redaction rules (M0 defines them; M3 is the first to store receipts)."""

import json
import unittest
from datetime import datetime, timedelta, timezone

from umbrous.receipts import EvidenceRef, Receipt, ReceiptError, Redactor, new_action_id, new_task_id
from umbrous.receipts.model import new_id


def fixture_secrets():
    """Fake secrets in real shapes. Built by concatenation so secret scanners
    don't mistake this file for a leak."""
    return {
        "anthropic_key": "sk-" + "ant-" + "api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
        "openai_key": "sk-" + "proj-" + "Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2",
        "github_token": "gh" + "p_" + "0123456789abcdefghijABCDEFGHIJklmnop",
        "aws_access_key": "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "slack_token": "xo" + "xb-" + "1234567890-abcdefghij",
        "jwt": "ey" + "JhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVl",
        "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow\nAAAA\n-----END RSA PRIVATE KEY-----",
    }


def make_receipt(**overrides):
    t0 = datetime(2026, 11, 7, 1, 0, tzinfo=timezone.utc)
    values = dict(task_id=new_task_id(), action_id=new_action_id(), action="fs.read",
                  scope="project:demo resource:docs/a.md", decision="allowed",
                  started_at=t0, ended_at=t0 + timedelta(milliseconds=42),
                  evidence=(EvidenceRef("file", "docs/a.md", sha256="a" * 64, size_bytes=120, lines=(1, 40)),))
    values.update(overrides)
    return Receipt(**values)


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.r = Redactor()

    def test_fixture_secrets_are_redacted(self):
        for kind, secret in fixture_secrets().items():
            with self.subTest(kind=kind):
                out = self.r.redact(f"before {secret} after")
                self.assertNotIn(secret, out)
                self.assertIn("[REDACTED:", out)
                self.assertTrue(out.startswith("before ") and out.endswith(" after"))

    def test_assignments_keep_the_name(self):
        out = self.r.redact('DB_PASSWORD="hunter2hunter2" user=fred')
        self.assertEqual(out, 'DB_PASSWORD="[REDACTED:assignment]" user=fred')

    def test_short_and_quoted_assignment_values(self):
        cases = {
            "password=abc": "password=[REDACTED:assignment]",
            'password="two secret words" user=x': 'password="[REDACTED:assignment]" user=x',
            "passphrase: 'correct horse battery staple'": "passphrase: '[REDACTED:assignment]'",
            'API_KEY: "never closed and has spaces': 'API_KEY: "[REDACTED:assignment]',
            "token=x": "token=[REDACTED:assignment]",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.r.redact(text), expected)

    def test_count_like_names_are_not_secrets(self):
        text = "max_tokens=512 tokens: 40 token_count=7"
        self.assertEqual(self.r.redact(text), text)

    def test_url_credentials(self):
        out = self.r.redact("clone https://fred:s3cretpass@example.com/repo.git")
        self.assertEqual(out, "clone https://[REDACTED:url_credentials]example.com/repo.git")

    def test_registered_runtime_secret_is_redacted_whatever_its_shape(self):
        self.r.register("plain-looking-value-123")
        self.assertEqual(self.r.redact("x plain-looking-value-123 y"), "x [REDACTED:runtime_secret] y")

    def test_short_values_cannot_be_registered(self):
        with self.assertRaises(ValueError):
            self.r.register("abc")

    def test_sensitive_keys_in_structures(self):
        out = self.r.redact_obj({"headers": {"Authorization": "anything", "Accept": "json"},
                                 "items": [{"github_token": "whatever"}], "n": 3})
        self.assertEqual(out["headers"]["Authorization"], "[REDACTED:field]")
        self.assertEqual(out["headers"]["Accept"], "json")
        self.assertEqual(out["items"][0]["github_token"], "[REDACTED:field]")
        self.assertEqual(out["n"], 3)

    def test_ordinary_text_is_untouched(self):
        text = "Read docs/a.md (120 bytes), tokens used: 512, see https://example.com/path"
        self.assertEqual(self.r.redact(text), text)


class ReceiptTests(unittest.TestCase):
    def test_record_shape(self):
        rec = make_receipt().to_record(Redactor())
        self.assertEqual(rec["format_version"], 1)
        self.assertEqual(rec["decision"], "allowed")
        self.assertEqual(rec["duration_ms"], 42)
        self.assertEqual(rec["started_at"], "2026-11-07T01:00:00.000Z")
        self.assertEqual(rec["evidence"][0]["lines"], [1, 40])
        json.dumps(rec)  # plain JSON values only

    def test_fixture_secret_is_redacted_before_storage(self):
        secret = fixture_secrets()["github_token"]
        r = make_receipt(decision="denied", reason=f"command contained {secret}",
                         scope=f"project:demo env:{secret}",
                         evidence=(EvidenceRef("url", f"https://x:{secret}@example.com/"),))
        stored = json.dumps(r.to_record(Redactor()))
        self.assertNotIn(secret, stored)
        self.assertIn("[REDACTED:", stored)

    def test_content_sized_fields_are_rejected(self):
        with self.assertRaises(ReceiptError):
            make_receipt(reason="x" * 281)
        with self.assertRaises(ReceiptError):
            make_receipt(scope="line one\nline two")
        with self.assertRaises(ReceiptError):
            EvidenceRef("file", "a" * 513)

    def test_invalid_values_are_rejected(self):
        bad = [dict(decision="maybe"), dict(action="Read File"), dict(task_id="tsk_123"),
               dict(action_id=new_task_id()), dict(ended_at=datetime(2026, 1, 1, tzinfo=timezone.utc))]
        for b in bad:
            with self.subTest(b=b), self.assertRaises(ReceiptError):
                make_receipt(**b)
        with self.assertRaises(ReceiptError):
            EvidenceRef("file", "a", sha256="not-a-hash")
        with self.assertRaises(ReceiptError):
            EvidenceRef("file", "a", lines=(5, 2))

    def test_every_decision_is_representable(self):
        for d in ("allowed", "denied", "failed", "uncertain"):
            self.assertEqual(make_receipt(decision=d).to_record(Redactor())["decision"], d)

    def test_ids_sort_by_time(self):
        a, b = new_id("tsk"), new_id("tsk")
        self.assertLessEqual(a[:16], b[:16])
        with self.assertRaises(ReceiptError):
            new_id("xyz")


if __name__ == "__main__":
    unittest.main()
