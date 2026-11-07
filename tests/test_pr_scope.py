"""Reviewing a pull request whose branches show no difference.

The failure behind these tests (a real one): a review of an already-merged PR. The
destination branch already held the source branch, so comparing them was empty; PRISM
did not notice, handed the reviewer an empty diff, the reviewer's `git merge-base`
check was refused by a permission rule that was meant for `git merge`, and the job
ended as an error blaming the network. Now PRISM works out what the PR changed itself
(through the commits CodeCommit recorded when the branches say nothing), says plainly
when there is really nothing to review, and the reviewer may run `git merge-base`.
"""
import fnmatch
import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import orchestrator as o  # noqa: E402

ROOT = HERE.parent
QUIET = lambda *a, **k: None  # noqa: E731


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


class Repo(unittest.TestCase):
    """origin + a clone with `main` and a PR branch `topic` that adds work."""

    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin, self.clone = base / "origin.git", base / "clone"
        git(base, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(base, "clone", "-q", str(self.origin), str(self.clone))
        git(self.clone, "config", "user.email", "t@x.com")
        git(self.clone, "config", "user.name", "T")
        (self.clone / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", "base")
        git(self.clone, "push", "-q", "origin", "HEAD:refs/heads/main")
        self.main_before = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "checkout", "-q", "-b", "topic")
        (self.clone / "feature.py").write_text("def pay():\n    return 1\n", encoding="utf-8")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-qm", "add the feature")
        git(self.clone, "push", "-q", "origin", "topic")
        self.topic = git(self.clone, "rev-parse", "HEAD")
        git(self.clone, "fetch", "-q", "origin")

    def merge_it(self, fast_forward=True):
        """What CodeCommit's fast-forward merge does to the destination branch."""
        git(self.clone, "push", "-q", "origin", f"{self.topic}:refs/heads/main")
        git(self.clone, "fetch", "-q", "origin")

    def meta(self, **kw):
        base = {"status": "OPEN", "sourceCommit": self.topic, "destinationCommit": self.main_before,
                "mergeBase": self.main_before, "isMerged": False, "title": "t"}
        base.update(kw)
        return base

    def scope(self, meta):
        return o._pr_change_scope(str(self.clone), meta, "topic", "main", True)


class FindingTheChanges(Repo):
    def test_an_open_pr_is_read_the_way_it_always_was(self):
        s = self.scope(self.meta())
        self.assertEqual(s["kind"], "branch")
        self.assertEqual(s["diff_spec"], "origin/main...origin/topic")
        self.assertFalse(s["merged"])

    def test_a_merged_pr_is_found_through_its_recorded_commits(self):
        self.merge_it()
        # the situation from the incident: the two branches are now identical ...
        self.assertEqual(git(self.clone, "rev-parse", "origin/main"),
                         git(self.clone, "rev-parse", "origin/topic"))
        s = self.scope(self.meta(status="CLOSED", isMerged=True))
        # ... yet the PR's changes are still there to review
        self.assertEqual(s["kind"], "recorded")
        self.assertTrue(s["merged"])
        self.assertEqual(s["diff_spec"], f"{self.main_before}..{self.topic}")
        self.assertIn("already merged", s["label"])

    def test_the_destination_commit_alone_is_enough(self):
        self.merge_it()
        s = self.scope(self.meta(status="CLOSED", mergeBase=""))
        self.assertEqual((s["kind"], s["diff_spec"]),
                         ("recorded", f"{self.main_before}...{self.topic}"))

    def test_a_merged_pr_whose_record_shows_nothing_is_verifiably_empty(self):
        self.merge_it()
        s = self.scope(self.meta(status="CLOSED", isMerged=True, mergeBase=self.topic,
                                 destinationCommit=self.topic))
        self.assertTrue(s.get("empty"))
        self.assertTrue(s["merged"])

    def test_an_open_pr_with_no_commits_of_its_own_is_empty(self):
        git(self.clone, "push", "-q", "origin", f"{self.main_before}:refs/heads/topic", "--force")
        git(self.clone, "fetch", "-q", "origin")
        s = o._pr_change_scope(str(self.clone), {"status": "OPEN", "sourceCommit": ""}, "topic",
                               "main", True)
        self.assertTrue(s.get("empty"))
        self.assertFalse(s["merged"])

    def test_it_says_nothing_it_cannot_know(self):
        self.assertIsNone(o._pr_change_scope(str(self.clone), self.meta(), "topic", "main", False))
        self.assertIsNone(o._pr_change_scope("", self.meta(), "topic", "main", True))
        self.assertIsNone(o._pr_change_scope(str(self.clone), self.meta(), "", "main", True))
        # git cannot answer when the refs are not here: unknown, never "empty"
        self.assertIsNone(o._pr_change_scope(str(self.clone), {}, "no-such", "also-missing", True))

    def test_messages_say_what_actually_happened(self):
        merged = o._nothing_to_review_message(self.meta(status="CLOSED", isMerged=True),
                                              "topic", "main")
        self.assertIn("already merged", merged)
        self.assertIn("'main' already contains 'topic'", merged)
        self.assertNotIn("network", merged.lower())
        empty = o._nothing_to_review_message({"status": "OPEN"}, "topic", "main")
        self.assertIn("no commits", empty)
        self.assertNotIn("already merged", empty)
        closed = o._nothing_to_review_message({"status": "CLOSED"}, "topic", "main")
        self.assertIn("already closed", closed)

    def test_get_pr_exposes_what_codecommit_recorded(self):
        raw = {"pullRequest": {"pullRequestStatus": "CLOSED", "title": "t", "pullRequestTargets": [{
            "repositoryName": "r", "sourceReference": "refs/heads/topic",
            "destinationReference": "refs/heads/main", "sourceCommit": "a" * 40,
            "destinationCommit": "b" * 40, "mergeBase": "c" * 40,
            "mergeMetadata": {"isMerged": True}}]}}
        with mock.patch.object(o, "aws_cli", return_value=raw):
            pr = o.get_pr("7")
        self.assertEqual((pr["mergeBase"], pr["isMerged"], pr["status"]),
                         ("c" * 40, True, "CLOSED"))
        with mock.patch.object(o, "aws_cli", return_value={"pullRequest": {"pullRequestTargets": [{}]}}):
            pr = o.get_pr("7")
        self.assertEqual((pr["mergeBase"], pr["isMerged"]), ("", False))     # older shape: harmless


class ReviewUsesIt(Repo):
    """run_opencode_review with real git; only the model and AWS are stubbed."""

    def setUp(self):
        super().setUp()
        self.started, self.prompts = [], []
        o.ensure_bundled_agents = lambda *a, **k: None
        o.require_engine = lambda: "opencode"
        o.engine_supports_json = lambda exe: False
        o._fetch_pr_refs = lambda *a, **k: True
        o._incremental_context = lambda *a, **k: None
        o._review_history_context = lambda *a, **k: ""

        def stream(cmd, cwd, emit, control=None, json_mode=False, meter=None):
            self.started.append(1)
            self.prompts.append(cmd[-1])
            return 0, "**Verdict:** ✅ Approve\n**Impact score:** 2/10 — small\n", "sid"
        o._run_stream_resilient = stream
        self.log = []

    def review(self, meta):
        o.get_pr = lambda *a, **k: dict(meta, sourceReference="topic", destinationReference="main")
        return o.run_opencode_review("7", "repo", str(self.clone), str(self.clone),
                                     emit=lambda m, *a, **k: self.log.append(m))

    def test_nothing_to_review_is_decided_before_any_model_run(self):
        self.merge_it()
        with self.assertRaises(o.NothingToReview) as cm:
            self.review(self.meta(status="CLOSED", isMerged=True, mergeBase=self.topic,
                                  destinationCommit=self.topic))
        self.assertIn("already merged", str(cm.exception))
        self.assertEqual(self.started, [], "a model run was spent on an empty diff")

    def test_a_merged_pr_is_reviewed_from_its_recorded_changes(self):
        self.merge_it()
        self.review(self.meta(status="CLOSED", isMerged=True))
        (prompt,) = self.prompts
        self.assertIn("feature.py", prompt)                         # the diff stat is in the brief
        self.assertIn("def pay()", prompt)                          # and the diff itself
        self.assertIn("CodeCommit recorded", prompt)
        self.assertTrue(any("already merged" in m for m in self.log))

    def test_an_open_pr_gets_exactly_the_brief_it_always_did(self):
        self.review(self.meta())
        (prompt,) = self.prompts
        self.assertIn("origin/main...origin/topic", prompt)
        self.assertNotIn("recorded", prompt)

    def test_an_incremental_review_keeps_its_own_range(self):
        o._incremental_context = lambda *a, **k: {
            "commit": self.main_before, "current_commit": self.topic, "verdict": "approve",
            "block": "- earlier finding"}
        self.merge_it()                       # even with the branches identical
        self.review(self.meta(status="CLOSED", isMerged=True))
        self.assertEqual(len(self.started), 1)
        self.assertIn("INCREMENTAL", self.prompts[0])


class PipelineOutcome(unittest.TestCase):
    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clone = self._tmp.name
        o._REVIEWED_STORE_PATH = Path(self.clone) / "reviewed_commits.json"
        hist = Path(self.clone) / "history"
        hist.mkdir()
        patcher = mock.patch.dict("os.environ", {"PRISM_HISTORY_DIR": str(hist)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.merged, self.described, self.posted, self.log, self.progress = [], [], [], [], []
        o.ensure_bundled_agents = lambda *a, **k: ["pr-reviewer"]
        o.resolve_repo = lambda r, p, l: (r, self.clone)
        o.try_fast_forward_merge = lambda *a, **k: self.merged.append(1) or {}
        o.update_description_direct = lambda *a, **k: self.described.append(a) or ""
        o.check_ff_mergeable = lambda *a, **k: (True, "ok")
        o.get_pr = lambda *a, **k: {"status": "CLOSED", "repositoryName": "repo",
                                    "destinationReference": "main", "sourceReference": "topic",
                                    "description": "", "sourceCommit": "a" * 40}
        o.get_pr_info = lambda *a, **k: {}
        o.notifier.post_summary = lambda *a, **k: self.posted.append(1)

    def run_pipeline(self, **kw):
        def nothing(*a, **k):
            raise o.NothingToReview("this pull request is already merged: 'main' already "
                                    "contains 'topic'")
        o.run_opencode_review = nothing
        return o.full_pipeline(self.clone, "repo", "7", local_repo=self.clone,
                               webhook_url="https://x.invalid/h",
                               emit=lambda m, *a, **k: self.log.append(m),
                               progress=lambda s, st: self.progress.append((s, st)), **kw)

    def test_it_stops_cleanly_and_touches_nothing(self):
        res = self.run_pipeline()                        # no exception: not an error to retry
        self.assertEqual(res["stopped"], "no-changes")
        self.assertIs(res["merged"], False)
        self.assertEqual(self.merged + self.described + self.posted, [])
        self.assertIsNone(o._locally_confirmed_reviewed_commit("repo", "7"))
        self.assertEqual(o._load_history(), [])

    def test_it_says_why_in_plain_words_and_shows_no_verdict(self):
        self.run_pipeline()
        text = "\n".join(self.log)
        self.assertIn("Nothing to review", text)
        self.assertIn("already merged", text)
        self.assertNotIn("◆ Verdict", text)
        self.assertNotIn("network", text.lower())
        self.assertEqual(o._stopped_reason("no-changes"), "nothing to review")

    def test_every_stage_is_marked_skipped_and_it_holds_with_any_options(self):
        for kw in ({}, {"do_merge": False}, {"dry_run": True}, {"do_update_desc": False}):
            with self.subTest(kw):
                self.progress.clear()
                self.assertEqual(self.run_pipeline(**kw)["stopped"], "no-changes")
                self.assertIn(("review", "skipped"), self.progress)
        self.assertEqual(self.merged + self.described, [])

    def test_codegen_hands_it_to_a_person(self):
        import codegen_bridge as cb
        res = self.run_pipeline()
        decision, _ = cb.outcome_from_summary(res, res["review"])
        self.assertEqual(decision, cb.DECISION_NEEDS_HUMAN)

    def test_an_incomplete_review_now_carries_the_reviewers_own_explanation(self):
        o.run_opencode_review = lambda *a, **k: (
            "**Verdict:** ❓ Review incomplete\n**Impact score:** n/a\n### Summary\n"
            "The diff came back empty and a command was refused.\nCoverage: 0 changed files "
            "reviewed.\n", "s")
        with self.assertRaises(o.ReviewIncomplete) as cm:
            o.full_pipeline(self.clone, "repo", "7", local_repo=self.clone, emit=QUIET)
        self.assertIn("The reviewer said:", str(cm.exception))
        self.assertIn("a command was refused", str(cm.exception))


class ReviewerMayRunMergeBase(unittest.TestCase):
    """`git merge*` is denied so the reviewer cannot merge - but that pattern also matches
    `git merge-base`, which only reads and is how a review checks what a PR changed."""

    @staticmethod
    def shell_rules(path):
        rules, in_bash = [], False
        for line in (ROOT / "agents" / path).read_text(encoding="utf-8").split("---")[1].splitlines():
            if line.startswith("  bash:"):
                in_bash = True
                continue
            if in_bash:
                stripped = line.strip()
                if not line.startswith("    ") and stripped:
                    break
                if stripped.startswith('"') and ":" in stripped:
                    pattern, _, effect = stripped.rpartition(":")
                    rules.append((pattern.strip().strip('"'), effect.strip()))
        return rules

    @staticmethod
    def verdict(rules, command):
        result = None
        for pattern, effect in rules:                    # the LAST matching rule wins
            if fnmatch.fnmatchcase(command, pattern):
                result = effect
        return result

    def test_read_only_git_is_allowed_and_everything_that_writes_is_still_denied(self):
        for agent in ("pr-reviewer.md", "doc-reviewer.md"):
            rules = self.shell_rules(agent)
            for command in ("git merge-base origin/main origin/topic",
                            "git merge-base --is-ancestor abc123 def456",
                            "git diff --stat origin/main...origin/topic", "git log --oneline -5",
                            "git rev-parse HEAD", "git show abc123:path/file.py", "git fetch origin main"):
                with self.subTest(agent=agent, command=command):
                    self.assertEqual(self.verdict(rules, command), "allow")
            for command in ("git merge origin/topic", "git merge --no-ff topic",
                            "git push origin topic", "git commit -m x", "git checkout main",
                            "git rebase main", "pytest -q", "npm install", "pip install x",
                            "aws codecommit merge-pull-request-by-fast-forward --x",
                            "aws codecommit update-pull-request-description --x"):
                with self.subTest(agent=agent, command=command):
                    self.assertEqual(self.verdict(rules, command), "deny")

    @unittest.skipUnless(shutil.which("opencode"), "needs the real engine")
    def test_the_real_engine_loads_the_allow_after_the_deny(self):
        with tempfile.TemporaryDirectory() as d:
            agents = Path(d) / ".opencode" / "agents"
            agents.mkdir(parents=True)
            for name in ("pr-reviewer.md", "doc-reviewer.md"):
                shutil.copyfile(str(ROOT / "agents" / name), str(agents / name))
            out = subprocess.run(["opencode", "debug", "agents"], cwd=d, capture_output=True,
                                 text=True, encoding="utf-8", timeout=120).stdout
        data = json.loads(out[out.index("["):])
        for name in ("pr-reviewer", "doc-reviewer"):
            rules = next(a for a in data if a["name"] == name)["permissions"]
            order = [(r["resource"], r["effect"]) for r in rules if r["action"] == "shell"]
            self.assertLess(order.index(("git merge*", "deny")),
                            order.index(("git merge-base*", "allow")), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
