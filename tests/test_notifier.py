"""The Google Chat webhook notification — a courtesy, not part of the
review, so every test here is really about one invariant: nothing it does
can ever affect the pipeline that produced the summary.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import notifier as n  # noqa: E402


class CardShape(unittest.TestCase):
    """Brief on purpose: one header, at most two widget rows."""

    def test_merged_verdict_and_impact(self):
        card = n.build_card("repo", "12", do_review=True, verdict_raw="Approve",
                            verdict_key="approve", impact_score="3", merged=True)
        self.assertEqual(card["header"]["subtitle"], "✅ Merged")
        widgets = card["sections"][0]["widgets"]
        self.assertEqual(len(widgets), 2, "verdict + impact, nothing else")
        self.assertIn("Approve", widgets[0]["decoratedText"]["text"])
        self.assertIn("✅", widgets[0]["decoratedText"]["text"])
        self.assertEqual(widgets[1]["decoratedText"]["text"], "3/10")

    def test_not_merged_carries_a_reason(self):
        card = n.build_card("repo", "12", do_review=True, verdict_raw="Request changes",
                            verdict_key="request-changes", merged=False,
                            reason="verdict blocks merge")
        self.assertEqual(card["header"]["subtitle"],
                         "⛔ Not merged — verdict blocks merge")

    def test_review_skipped_by_configuration_has_no_verdict_widget(self):
        """Verdict/impact are meaningless with no review having run —
        showing them would misrepresent a deliberate bypass as a result."""
        card = n.build_card("repo", "12", do_review=False, merged=True)
        widgets = card["sections"][0]["widgets"]
        self.assertEqual(len(widgets), 1)
        self.assertEqual(widgets[0]["decoratedText"]["text"], "Skipped by configuration")

    def test_review_ran_but_verdict_unparsed_is_distinct_from_skipped(self):
        """A real gap in what the reviewer returned must not be reported as
        the same thing as the user's own choice to skip review."""
        card = n.build_card("repo", "12", do_review=True, verdict_raw="", merged=False)
        widgets = card["sections"][0]["widgets"]
        self.assertEqual(widgets[0]["decoratedText"]["text"], "No verdict parsed")

    def test_no_impact_widget_when_impact_is_missing(self):
        card = n.build_card("repo", "12", do_review=True, verdict_raw="Approve",
                            verdict_key="approve", impact_score=None, merged=True)
        self.assertEqual(len(card["sections"][0]["widgets"]), 1)

    def test_title_names_the_repo_and_pr(self):
        card = n.build_card("my-repo", "42", do_review=True, merged=True)
        self.assertIn("my-repo", card["header"]["title"])
        self.assertIn("42", card["header"]["title"])


class PostSummary(unittest.TestCase):
    def test_no_webhook_configured_makes_no_network_call(self):
        calls = []
        orig = n.urllib.request.urlopen
        n.urllib.request.urlopen = lambda *a, **k: calls.append(1)
        try:
            result = n.post_summary(None, repo_name="r", pr_id="1",
                                    do_review=True, merged=True)
        finally:
            n.urllib.request.urlopen = orig
        self.assertFalse(result)
        self.assertEqual(calls, [], "no webhook configured must never touch the network")

    def test_posts_the_card_as_json_to_the_configured_url(self):
        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None, context=None):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["content_type"] = req.get_header("Content-type")
            return FakeResponse()

        orig = n.urllib.request.urlopen
        n.urllib.request.urlopen = fake_urlopen
        try:
            ok = n.post_summary("https://chat.googleapis.com/v1/spaces/x/messages",
                                repo_name="repo", pr_id="9", do_review=True,
                                verdict_raw="Approve", verdict_key="approve",
                                merged=True)
        finally:
            n.urllib.request.urlopen = orig
        self.assertTrue(ok)
        self.assertEqual(captured["url"],
                         "https://chat.googleapis.com/v1/spaces/x/messages")
        self.assertIn("cardsV2", captured["body"])
        self.assertEqual(captured["content_type"], "application/json; charset=UTF-8")

    def test_a_network_failure_is_swallowed_not_raised(self):
        def fake_urlopen(*a, **k):
            raise OSError("connection refused")
        orig = n.urllib.request.urlopen
        n.urllib.request.urlopen = fake_urlopen
        try:
            ok = n.post_summary("https://example.invalid/hook", repo_name="r",
                                pr_id="1", do_review=True, merged=True)
        finally:
            n.urllib.request.urlopen = orig
        self.assertFalse(ok, "a failed post reports False, never raises")

    def test_an_http_error_is_also_swallowed(self):
        import urllib.error
        def fake_urlopen(*a, **k):
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        orig = n.urllib.request.urlopen
        n.urllib.request.urlopen = fake_urlopen
        try:
            ok = n.post_summary("https://example.invalid/hook", repo_name="r",
                                pr_id="1", do_review=True, merged=True)
        finally:
            n.urllib.request.urlopen = orig
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
