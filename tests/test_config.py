"""Persistent app-level settings — currently just the Google Chat webhook.

Points CONFIG_DIR/CONFIG_PATH at a temp directory for every test so this
never touches the real ~/.prism on the machine running the suite.
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config as C  # noqa: E402


class WebhookPersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.orig_dir, self.orig_path = C.CONFIG_DIR, C.CONFIG_PATH
        C.CONFIG_DIR = Path(self._tmp.name) / ".prism"
        C.CONFIG_PATH = C.CONFIG_DIR / "config.json"
        self.addCleanup(setattr, C, "CONFIG_DIR", self.orig_dir)
        self.addCleanup(setattr, C, "CONFIG_PATH", self.orig_path)

    def test_nothing_saved_yet_reads_as_empty(self):
        self.assertEqual(C.get_webhook_url(), "")

    def test_round_trips_through_a_real_file(self):
        C.set_webhook_url("https://chat.googleapis.com/v1/spaces/x/messages")
        self.assertEqual(C.get_webhook_url(),
                         "https://chat.googleapis.com/v1/spaces/x/messages")
        # A second, independent read must see what the first call wrote.
        self.assertTrue(C.CONFIG_PATH.is_file())

    def test_setting_it_empty_clears_it(self):
        C.set_webhook_url("https://example.invalid/hook")
        C.set_webhook_url("")
        self.assertEqual(C.get_webhook_url(), "")

    def test_surrounding_whitespace_is_trimmed(self):
        C.set_webhook_url("  https://example.invalid/hook  \n")
        self.assertEqual(C.get_webhook_url(), "https://example.invalid/hook")

    def test_a_corrupt_config_file_reads_as_empty_not_a_crash(self):
        C.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        C.CONFIG_PATH.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(C.get_webhook_url(), "")

    def test_saving_preserves_other_keys(self):
        C.save({"some_other_setting": "keep-me"})
        C.set_webhook_url("https://example.invalid/hook")
        self.assertEqual(C.load()["some_other_setting"], "keep-me")


if __name__ == "__main__":
    unittest.main(verbosity=2)
