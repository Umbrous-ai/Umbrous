"""Provider controls: preference safety and the browser request contract."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama
from tests.helpers import RunningRuntime
from tests.test_browser_m1 import node_env

SCRIPTS = Path(__file__).parent / 'browser'


class ProviderControlsTests(unittest.TestCase):
    def test_preferences_and_login_urls(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is not installed')
        result = subprocess.run([node, str(SCRIPTS / 'model_controls_test.js')], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_controls_in_the_browser(self):
        found = node_env()
        if not found:
            self.skipTest('Node.js with Playwright is not installed')
        node, env = found
        fake = FakeOllama().start()
        try:
            with RunningRuntime(ollama_url=fake.url) as rt:
                result = subprocess.run([node, str(SCRIPTS / 'provider_controls_check.js'),
                                         f'{rt.origin}/#launch={rt.policy.launch_code}'],
                                        env=env, capture_output=True, text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        finally:
            fake.stop()
