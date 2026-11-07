"""Static checks on the page bridge and demo: the credential must stay in memory."""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def code_without_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in js.splitlines())


class BridgeTests(unittest.TestCase):
    def test_credential_is_never_written_to_browser_storage(self):
        code = code_without_comments((REPO / "umbrous/web/runtime-bridge.js").read_text())
        for api in ("sessionStorage", "localStorage", "indexedDB", "document.cookie", "caches"):
            with self.subTest(api=api):
                self.assertNotIn(api, code)

    def test_values_are_written_as_text_not_html(self):
        code = code_without_comments((REPO / "umbrous/web/runtime-bridge.js").read_text())
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(sink, code)

    def test_demo_never_puts_the_credential_on_a_command_line(self):
        demo = (REPO / "scripts/demo-m0.sh").read_text()
        self.assertNotRegex(demo, r"X-Umbrous-Token: <")
        self.assertNotIn("sessionStorage", demo)


if __name__ == "__main__":
    unittest.main()
