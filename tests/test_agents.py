"""Multi-agent contracts, conflict handling, and the safety gates.

The design rests on one claim: an agent can *decide*, but only PRISM can act,
and PRISM's gates are code that no prompt can reach. The SafetyGates tests are
the ones that prove it — they feed a "go" from pr-merger into situations where
merging is forbidden and assert nothing merges.
"""
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import orchestrator as o  # noqa: E402

REPORT = ("## PR #7\n**Verdict:** OK Approve\n"
          "**Impact score:** 2/10 - isolated\n"
          "- **[Low] style — `x.py:1`** nit\n")


def block(decision, **kw):
    lines = "\n".join(f"{k}: {v}" for k, v in (("decision", decision), *kw.items()))
    return f"Some prose from the agent.\n\n```prism\n{lines}\n```\n"


class DecisionContract(unittest.TestCase):
    def test_parses_keys(self):
        d = o.parse_decision(block("sync-then-merge", reason="behind", behind="3"))
        self.assertEqual(d["decision"], "sync-then-merge")
        self.assertEqual(d["behind"], "3")

    def test_missing_block_returns_empty_not_a_guess(self):
        self.assertEqual(o.parse_decision("I think you should merge it."), {})

    def test_last_block_wins(self):
        """An agent file shows an example block; only the real answer counts."""
        text = block("manual", reason="example") + block("merge", reason="real")
        self.assertEqual(o.parse_decision(text)["decision"], "merge")

    def test_survives_ansi_and_junk_lines(self):
        d = o.parse_decision("\x1b[32m" + block("go", reason="fine") + "\x1b[0m")
        self.assertEqual(d["decision"], "go")


class AgentBundle(unittest.TestCase):
    def test_every_agent_declares_mode_and_denies_writes(self):
        for path in sorted((ROOT / "agents").glob("*.md")):
            if path.name.startswith("_"):
                continue
            head = path.read_text(encoding="utf-8").split("---")[1]
            with self.subTest(agent=path.stem):
                self.assertIn("mode:", head)
                self.assertIn("edit: deny", head, "agents must never edit files")
                self.assertIn("task: deny", head, "agents must not spawn subagents")

    def test_no_agent_may_merge_or_push(self):
        """The permission layer backs up PRISM's gates."""
        for path in sorted((ROOT / "agents").glob("*.md")):
            if path.name.startswith("_"):
                continue
            head = path.read_text(encoding="utf-8").split("---")[1]
            if "bash: deny" in head:
                continue          # no shell at all
            with self.subTest(agent=path.stem):
                for forbidden in ('"git push*": deny', '"aws codecommit merge*": deny'):
                    self.assertIn(forbidden, head)

    def test_installs_every_agent(self):
        with tempfile.TemporaryDirectory() as d:
            names = o.ensure_bundled_agents(d, emit=lambda *a, **k: None)
            installed = {p.stem for p in Path(d, ".opencode", "agents").glob("*.md")}
            self.assertIn("pr-reviewer", installed)
            self.assertGreaterEqual(len(installed), 6)
            self.assertNotIn("_shared-contract", installed,
                             "reference docs must not install as agents")
            self.assertEqual(set(names), installed)


class Conflicts(unittest.TestCase):
    SAMPLE = ("head\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\n"
              "mid\n<<<<<<< HEAD\nours2\n=======\ntheirs2\n>>>>>>> origin/main\ntail\n")

    def test_splits_into_hunks(self):
        segs = o.parse_conflicts(self.SAMPLE)
        hunks = [s for s in segs if isinstance(s, tuple)]
        self.assertEqual(len(hunks), 2)
        self.assertEqual(hunks[0], ("ours\n", "theirs\n"))

    def test_applies_each_choice_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "x.py")
            o._write_worktree(f, self.SAMPLE)
            o.apply_conflict_choices(d, {"x.py#0": o.KEEP_CURRENT,
                                         "x.py#1": o.TAKE_INCOMING})
            self.assertEqual(o._read_worktree(f), "head\nours\nmid\ntheirs2\ntail\n")

    def test_refuses_to_guess_a_missing_choice(self):
        with tempfile.TemporaryDirectory() as d:
            o._write_worktree(Path(d, "x.py"), self.SAMPLE)
            with self.assertRaises(RuntimeError):
                o.apply_conflict_choices(d, {"x.py#0": o.KEEP_CURRENT})

    def test_abort_makes_no_choices(self):
        asked = []
        conflict = o.SyncConflict(
            [{"file": "x.py", "binary": False, "hunks": [("a\n", "b\n")]}],
            "main", "feat")
        def ask(q, choices=None):
            asked.append(q)
            return o.ABORT
        o.run_agent = lambda *a, **k: ("", {})
        self.assertIsNone(o.resolve_conflicts_with_user(
            conflict, "/tmp", ask, emit=lambda *a, **k: None))
        self.assertEqual(len(asked), 1)

    def test_binary_conflicts_are_refused_not_guessed(self):
        conflict = o.SyncConflict(
            [{"file": "logo.png", "binary": True, "hunks": []}], "main", "feat")
        self.assertIsNone(o.resolve_conflicts_with_user(
            conflict, "/tmp", lambda *a, **k: o.KEEP_CURRENT,
            emit=lambda *a, **k: None))


class SafetyGates(unittest.TestCase):
    """A 'go' from an agent must never be sufficient on its own."""

    def setUp(self):
        importlib.reload(o)
        # These monkeypatches must not survive this test: reload() only
        # resets *before* each SafetyGates test runs, so without this any
        # later test file in the same `unittest discover` process would
        # inherit whichever lambda the last SafetyGates test here installed.
        self.addCleanup(importlib.reload, o)
        self.merged = []
        # A directory that exists on every platform: the pipeline checks the
        # clone path before anything else, and Windows has no /tmp.
        self._tmp = tempfile.TemporaryDirectory()
        self.clone = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        o.ensure_bundled_agents = lambda *a, **k: ["pr-reviewer"]
        o.resolve_repo = lambda r, p, l: (r, self.clone)
        o.run_opencode_review = lambda *a, **k: (REPORT, "ses_x")
        o.run_agent = lambda *a, **k: ("", {"decision": "go", "reason": "looks fine"})
        o.check_ff_mergeable = lambda *a, **k: (True, "ok")
        o.try_fast_forward_merge = lambda *a, **k: self.merged.append(1) or {}
        o.update_description_direct = lambda *a, **k: ""

    def _run(self, report=REPORT, status="OPEN", **kw):
        o.run_opencode_review = lambda *a, **k: (report, "ses_x")
        o.get_pr = lambda *a, **k: {"status": status, "repositoryName": "repo",
                                    "destinationReference": "main",
                                    "sourceReference": "feat", "description": ""}
        return o.full_pipeline(self.clone, "repo", "7", local_repo=self.clone,
                               emit=lambda *a, **k: None, **kw)

    def test_go_does_not_merge_a_request_changes_verdict(self):
        res = self._run(report="**Verdict:** NO Request changes\n")
        self.assertEqual(self.merged, [], "agent 'go' merged a rejected PR")
        self.assertEqual(res["stopped"], "verdict-blocks-merge")

    def test_go_does_not_merge_a_closed_pr(self):
        res = self._run(status="CLOSED")
        self.assertEqual(self.merged, [], "agent 'go' merged a closed PR")
        self.assertTrue(res["stopped"].startswith("status-"))

    def test_go_does_not_merge_in_dry_run(self):
        res = self._run(dry_run=True)
        self.assertEqual(self.merged, [], "agent 'go' merged during a dry run")
        self.assertEqual(res["stopped"], "dry-run")

    def test_go_does_not_merge_when_auto_merge_is_off(self):
        res = self._run(do_merge=False)
        self.assertEqual(self.merged, [])
        self.assertEqual(res["stopped"], "merge-disabled")

    def test_no_go_holds_even_though_prism_would_allow_it(self):
        o.run_agent = lambda *a, **k: ("", {"decision": "no-go", "reason": "changed"})
        res = self._run()
        self.assertEqual(self.merged, [])
        self.assertEqual(res["stopped"], "merger-no-go")

    def test_clean_fast_forward_still_merges(self):
        res = self._run()
        self.assertEqual(len(self.merged), 1)
        self.assertTrue(res["merged"])

    def test_skipping_review_still_merges_a_clean_fast_forward(self):
        """The whole point of do_review=False: sync-and-merge with no
        reviewer call, treated as an approval rather than a missing verdict."""
        res = self._run(do_review=False)
        self.assertEqual(len(self.merged), 1)
        self.assertTrue(res["merged"])
        self.assertEqual(res["review"].verdict_key, "skipped")

    def test_skipping_review_forces_the_description_update_off_too(self):
        """There is nothing to describe without a review having run."""
        calls = []
        o.update_description_direct = lambda *a, **k: calls.append(1) or ""
        self._run(do_review=False, do_update_desc=True)
        self.assertEqual(calls, [], "description update ran with no review to draw from")

    def test_no_go_still_holds_even_with_review_skipped(self):
        """pr-merger's veto is independent of whether a review happened."""
        o.run_agent = lambda *a, **k: ("", {"decision": "no-go", "reason": "changed"})
        res = self._run(do_review=False)
        self.assertEqual(self.merged, [])
        self.assertEqual(res["stopped"], "merger-no-go")


class ChatNotification(unittest.TestCase):
    """The webhook post is a courtesy layered on top of the pipeline in
    full_pipeline() itself — these prove it never changes what the pipeline
    does, only reports on it, and stays silent with nothing configured."""

    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self.merged = []
        self._tmp = tempfile.TemporaryDirectory()
        self.clone = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        o.ensure_bundled_agents = lambda *a, **k: ["pr-reviewer"]
        o.resolve_repo = lambda r, p, l: (r, self.clone)
        o.run_opencode_review = lambda *a, **k: (REPORT, "ses_x")
        o.run_agent = lambda *a, **k: ("", {"decision": "go", "reason": "looks fine"})
        o.check_ff_mergeable = lambda *a, **k: (True, "ok")
        o.try_fast_forward_merge = lambda *a, **k: self.merged.append(1) or {}
        o.update_description_direct = lambda *a, **k: ""
        o.get_pr = lambda *a, **k: {"status": "OPEN", "repositoryName": "repo",
                                    "destinationReference": "main",
                                    "sourceReference": "feat", "description": ""}
        # notifier is a *different* module: reloading `o` re-imports it (a
        # no-op against an already-loaded module) but never re-executes it,
        # so this monkeypatch would otherwise leak into every test file that
        # runs after this one in the same `unittest discover` process.
        self._orig_post_summary = o.notifier.post_summary
        self.addCleanup(setattr, o.notifier, "post_summary", self._orig_post_summary)
        self.posted = []
        o.notifier.post_summary = lambda url, **kw: self.posted.append((url, kw)) or True

    def _run(self, **kw):
        return o.full_pipeline(self.clone, "repo", "7", local_repo=self.clone,
                               emit=lambda *a, **k: None, **kw)

    def test_no_webhook_configured_posts_nothing(self):
        self._run()
        self.assertEqual(self.posted, [])

    def test_a_merge_posts_the_verdict_and_merged_true(self):
        self._run(webhook_url="https://example.invalid/hook")
        self.assertEqual(len(self.posted), 1)
        url, kw = self.posted[0]
        self.assertEqual(url, "https://example.invalid/hook")
        self.assertTrue(kw["merged"])
        self.assertIn("Approve", kw["verdict_raw"])

    def test_a_blocked_merge_posts_merged_false_with_a_reason(self):
        o.run_agent = lambda *a, **k: ("", {"decision": "no-go", "reason": "changed"})
        self._run(webhook_url="https://example.invalid/hook")
        self.assertEqual(len(self.posted), 1)
        _url, kw = self.posted[0]
        self.assertFalse(kw["merged"])
        self.assertEqual(kw["reason"], "merge check said no")

    def test_review_skipped_is_reported_as_such_not_as_a_verdict(self):
        self._run(webhook_url="https://example.invalid/hook", do_review=False)
        _url, kw = self.posted[0]
        self.assertFalse(kw["do_review"])
        self.assertTrue(kw["merged"])

    def test_a_hard_failure_still_posts_before_reraising(self):
        def boom(*a, **k):
            raise RuntimeError("kaboom")
        o.ensure_bundled_agents = boom
        with self.assertRaises(RuntimeError):
            self._run(webhook_url="https://example.invalid/hook")
        self.assertEqual(len(self.posted), 1)
        _url, kw = self.posted[0]
        self.assertFalse(kw["merged"])
        self.assertIn("kaboom", kw["reason"])

    def test_a_user_initiated_cancel_posts_nothing(self):
        def cancelled(*a, **k):
            raise o.Cancelled("stopped")
        o.ensure_bundled_agents = cancelled
        with self.assertRaises(o.Cancelled):
            self._run(webhook_url="https://example.invalid/hook")
        self.assertEqual(self.posted, [], "the user's own action is not a notable outcome")


class LocaleIndependence(unittest.TestCase):
    """Windows decodes with cp1252 unless told otherwise.

    Every agent prompt carries the verdict emoji, the engine streams them back,
    and aws returns PR titles in any language - all of which raise
    UnicodeDecodeError under a locale codec. Rewriting a conflicted file would
    be worse than a crash: it would re-encode the user's own source and flip
    its line endings. PYTHONWARNDEFAULTENCODING flags every text I/O that
    forgot to say `encoding=`, so this fails on Linux for a bug only Windows
    hits.
    """

    SCRIPT = r"""
import tempfile, pathlib, sys
sys.path.insert(0, %r)
import orchestrator as o
with tempfile.TemporaryDirectory() as d:
    o.ensure_bundled_agents(d, emit=lambda *a, **k: None)
    o.ensure_bundled_agents(d, emit=lambda *a, **k: None)   # second pass compares
    f = pathlib.Path(d, "x.py")
    o._write_worktree(f, "a\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> b\nz\n")
    o.apply_conflict_choices(d, {"x.py#0": o.KEEP_CURRENT})
"""

    @unittest.skipIf(sys.version_info < (3, 10),
                     "EncodingWarning needs 3.10+; CI runs 3.12 everywhere")
    def test_no_text_io_relies_on_the_locale_encoding(self):
        env = dict(os.environ, PYTHONWARNDEFAULTENCODING="1")
        proc = subprocess.run(
            [sys.executable, "-W", "error::EncodingWarning", "-c",
             self.SCRIPT % str(ROOT)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, env=env)
        self.assertEqual(proc.returncode, 0,
                         "text I/O without an explicit encoding:\n" + proc.stderr)

    def test_the_agents_really_do_carry_non_ascii(self):
        """Guards the premise: if this ever fails, the test above proves less."""
        blobs = [p.read_bytes() for p in (ROOT / "agents").glob("*.md")]
        self.assertTrue(any(b.decode("utf-8") != b.decode("ascii", "replace")
                            for b in blobs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
