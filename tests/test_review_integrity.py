"""A review that did not read the changes must fail - never approve, never merge.

The incident behind these tests: the reviewer could not fetch the diff, yet wrote

    **Verdict:** ⚠️ Approve with comments
    **Impact score:** 1/10 - no code was reviewed, so this score is a placeholder ...

and PRISM wrote that into the pull request and merged it. Everything here is about
PRISM refusing to act on a "review" like that: deterministically, in three layers
(the report is checked for admissions of not having reviewed; a review that cannot
possibly see the code is not started; the agent is given an honest way to say so),
and about the failure being a plain, retryable error.
"""
import importlib
import io
import queue
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import cli_jobs as CJ  # noqa: E402
import config as C  # noqa: E402
import docreview as D  # noqa: E402
import jobs as J  # noqa: E402
import orchestrator as o  # noqa: E402
import prism_cli as P  # noqa: E402
from test_docreview import make_remote_and_clone  # noqa: E402

ROOT = HERE.parent

# The report from the incident, verbatim.
INCIDENT = """**Verdict:** ⚠️ Approve with comments
**Impact score:** 1/10 — no code was reviewed, so this score is a placeholder and not a risk assessment.

### Findings (most severe first)
- **[Medium] Review coverage — `n/a`** I could not read the diff, so this PR has no code findings and no verdict based on its contents. Treat the verdict above as a placeholder and run the review again before merging.

### Summary
The review did not complete. No PRISM-prepared block was supplied, and my `git fetch` timed out after 120s. The local `origin/develop...origin/ai_native/LLPMS-2891_PR_void` diff, stat and commit list then returned empty output. I could not get the diff by any other route, so I have not checked correctness, security, tests, infra or impact. The PR's status and any merge conflicts are also unchecked.
Coverage: 0 changed files reviewed. The only thing I learned is that the local ref `origin/ai_native/LLPMS-2891_PR_void` exists at 64e53eaa1. Please supply a PRISM-prepared diff block or restore shell access, then re-run.

Do you want me to update the PR description with this report? I'd advise against it, because the report contains no code findings.
"""

GOOD = """**Verdict:** ⚠️ Approve with comments
**Impact score:** 3/10 — small, isolated

### Findings (most severe first)
- **[Medium] Failure handling — `svc/pay.py:41`** The retry swallows the timeout. Add a bounded retry.

### Summary
Adds a retry around the payment call. Coverage: svc/pay.py, tests/test_pay.py and the migration; all 3 changed files reviewed.
"""


class Detection(unittest.TestCase):
    def test_the_incident_report_is_recognised_and_can_never_merge(self):
        r = o.parse_review_output(INCIDENT)
        # the verdict line alone WOULD have allowed a merge - that is the whole bug
        self.assertEqual(r.verdict_key, "approve-with-comments")
        self.assertTrue(r.incomplete_reason)
        self.assertFalse(r.merge_allowed())

    def test_each_way_of_saying_nothing_was_read_is_caught_on_its_own(self):
        for sentence in (
                "I could not read the diff.",
                "Unable to fetch the changes, so I stopped.",
                "I failed to get the diff.",
                "I couldn't access the pull request's changes.",
                "No code was reviewed.",
                "No changes were examined.",
                "This score is a placeholder.",
                "The verdict above is a placeholder.",
                "This is not a risk assessment.",
                "There is no verdict based on its contents.",
                "I have not checked correctness, security or tests.",
                "I have not reviewed any of the code.",
                "- **[Medium] Review coverage — `n/a`** nothing",
                "Coverage: 0 changed files reviewed.",
                "Coverage: 0 of 14 files reviewed.",
                "0 changed files were reviewed",
                "The review did not complete.",
        ):
            with self.subTest(sentence):
                r = o.parse_review_output("**Verdict:** ✅ Approve\n**Impact score:** 2/10 — x\n"
                                          + sentence + "\n")
                self.assertTrue(r.incomplete_reason, sentence)
                self.assertFalse(r.merge_allowed())

    def test_an_agent_that_declares_itself_incomplete_is_honoured_not_read_as_a_block(self):
        for verdict in ("❓ Review incomplete", "⛔ Review incomplete", "Incomplete",
                        "Not reviewed", "Could not review"):
            with self.subTest(verdict):
                r = o.parse_review_output(f"**Verdict:** {verdict}\n**Impact score:** n/a\n")
                self.assertEqual(r.verdict_key, "incomplete")
                self.assertTrue(r.incomplete_reason)
                self.assertFalse(r.merge_allowed())
        self.assertEqual(o.normalise_verdict("⛔ Block"), "block")            # unchanged
        self.assertEqual(o.normalise_verdict("✅ Approve"), "approve")
        self.assertEqual(o.normalise_verdict("⚠️ Approve with comments"), "approve-with-comments")
        self.assertEqual(o.normalise_verdict("🔴 Request changes"), "request-changes")

    def test_real_reviews_are_never_flagged(self):
        for name, text in {
            "a clean approval with no coverage line":
                "**Verdict:** ✅ Approve\n**Impact score:** 2/10 — small\n",
            "an ordinary review with findings and coverage": GOOD,
            "a finding that mentions a placeholder in the code":
                "**Verdict:** ⚠️ Approve with comments\n**Impact score:** 3/10 — x\n"
                "- **[Low] Config — `a.yml:3`** A placeholder password is committed.\n",
            "coverage of ten files (starts with 1, not 0)":
                "**Verdict:** ✅ Approve\n**Impact score:** 2/10 — x\n### Summary\n"
                "Coverage: 10 changed files reviewed.\n",
            "a file the reviewer could not read, not the diff":
                "**Verdict:** ✅ Approve\n**Impact score:** 2/10 — x\n### Summary\n"
                "I could not read the changes to logo.png (binary). Coverage: 4 of 5 files.\n",
            "files it did not reach":
                "**Verdict:** ⚠️ Approve with comments\n**Impact score:** 4/10 — x\n### Summary\n"
                "Coverage: auth and billing; I did not get to the docs folder.\n",
            "a request for changes":
                "**Verdict:** 🔴 Request changes\n**Impact score:** 7/10 — x\n"
                "- **[High] x — `a:1`** boom\n",
        }.items():
            with self.subTest(name):
                r = o.parse_review_output(text)
                self.assertEqual(r.incomplete_reason, "", name)
        self.assertTrue(o.parse_review_output(GOOD).merge_allowed())

    def test_a_cut_short_wrap_up_with_findings_is_kept_as_a_partial_review(self):
        """Only the whole-review phrase is relaxed for a wrap-up; it still never merges."""
        wrap = ("**Verdict:** ⚠️ Approve with comments\n**Impact score:** 4/10 — partial\n"
                "- **[Medium] x — `a.py:2`** a finding\n### Summary\nThe review did not complete: "
                "I reached a.py only.\n" + o.CUT_SHORT_MARKER + ": stalled]\n")
        r = o.parse_review_output(wrap)
        self.assertEqual(r.incomplete_reason, "")
        self.assertEqual(len(r.findings), 1)
        self.assertFalse(o.parse_review_output(
            wrap.replace("a.py only.", "nothing at all. No code was reviewed.")
        ).incomplete_reason == "")

    def test_ansi_styling_cannot_hide_it(self):
        for text in ("\x1b[1mNo code was\x1b[0m reviewed", "\x1b[1mI could not read the diff\x1b[0m"):
            r = o.parse_review_output("**Verdict:** ✅ Approve\n" + text + "\n")
            self.assertTrue(r.incomplete_reason, text)


class Harness(unittest.TestCase):
    """The real _run_pipeline with only the agent, AWS and git writes replaced."""

    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clone = self._tmp.name
        # Both stores a finished review writes to, kept out of the real ones: the
        # reviewed-commits record and the review-history ledger (which lives under
        # the OS temp folder unless told otherwise).
        o._REVIEWED_STORE_PATH = Path(self.clone) / "reviewed_commits.json"
        hist = Path(self.clone) / "history"
        hist.mkdir()
        patcher = mock.patch.dict("os.environ", {"PRISM_HISTORY_DIR": str(hist)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.merged, self.described, self.posted, self.progress, self.log = [], [], [], [], []
        o.ensure_bundled_agents = lambda *a, **k: ["pr-reviewer"]
        o.resolve_repo = lambda r, p, l: (r, self.clone)
        o.check_ff_mergeable = lambda *a, **k: (True, "ok")
        o.try_fast_forward_merge = lambda *a, **k: self.merged.append(1) or {}
        o.update_description_direct = lambda *a, **k: self.described.append(a) or ""
        o.run_agent = lambda *a, **k: ("", {"decision": "go", "reason": "ok"})
        o.get_pr = lambda *a, **k: {
            "status": "OPEN", "repositoryName": "repo", "destinationReference": "main",
            "sourceReference": "feat", "description": "", "sourceCommit": "a" * 40}
        o.get_pr_info = lambda *a, **k: {}
        o.notifier.post_summary = lambda *a, **k: self.posted.append(1)

    def run_review(self, report, **kw):
        o.run_opencode_review = lambda *a, **k: (report, "ses_x")
        return o.full_pipeline(
            self.clone, "repo", "7", local_repo=self.clone, webhook_url="https://x.invalid/h",
            emit=lambda m, *a, **k: self.log.append(m),
            progress=lambda s, st: self.progress.append((s, st)), **kw)


class PipelineRefusesAnUnreadReview(Harness):
    def test_the_incident_report_fails_the_run_and_changes_nothing(self):
        with self.assertRaises(o.ReviewIncomplete) as cm:
            self.run_review(INCIDENT)
        self.assertIn("did not examine", str(cm.exception))
        self.assertIn("Retry the job", str(cm.exception))
        self.assertEqual(self.described, [], "the PR description was written")
        self.assertEqual(self.merged, [], "the PR was merged")
        self.assertEqual(self.posted, [], "a verdict was posted to chat")
        self.assertIsNone(o._locally_confirmed_reviewed_commit("repo", "7"))
        self.assertEqual(o._load_history(), [])

    def test_no_verdict_is_shown_and_the_review_stage_is_marked_failed(self):
        with self.assertRaises(o.ReviewIncomplete):
            self.run_review(INCIDENT)
        self.assertFalse(any("◆ Verdict" in m for m in self.log), "a verdict was displayed")
        self.assertIn(("review", "error"), self.progress)
        self.assertNotIn(("review", "done"), self.progress)

    def test_it_fails_whatever_the_options_are(self):
        for kw in ({}, {"do_merge": False}, {"do_update_desc": False}, {"dry_run": True},
                   {"do_sync": False}):
            with self.subTest(kw), self.assertRaises(o.ReviewIncomplete):
                self.run_review(INCIDENT, **kw)
        self.assertEqual(self.merged + self.described, [])

    def test_an_agent_declared_incomplete_review_fails_the_same_way(self):
        with self.assertRaises(o.ReviewIncomplete):
            self.run_review("**Verdict:** ❓ Review incomplete\n**Impact score:** n/a — not reviewed\n"
                            "### Summary\nThe fetch timed out. Coverage: 0 changed files reviewed.\n")
        self.assertEqual(self.merged + self.described, [])

    def test_a_cut_short_review_that_read_nothing_also_fails(self):
        with self.assertRaises(o.ReviewIncomplete):
            self.run_review("**Verdict:** ✅ Approve\n**Impact score:** 1/10 — x\n"
                            "No code was reviewed.\n" + o.CUT_SHORT_MARKER + ": stalled]\n")
        self.assertEqual(self.merged + self.described, [])

    def test_a_real_review_is_completely_unchanged(self):
        res = self.run_review(GOOD)
        self.assertTrue(res["merged"])
        self.assertEqual(len(self.described), 1)
        self.assertTrue(any("◆ Verdict" in m for m in self.log))
        self.assertEqual(o._locally_confirmed_reviewed_commit("repo", "7"), "a" * 40)
        self.assertEqual(self.posted, [1])

    def test_an_unread_review_never_slips_through_the_late_merge_gates_either(self):
        """Defence in depth: even if the early check were bypassed, merge_allowed()
        itself says no."""
        r = o.parse_review_output(INCIDENT)
        self.assertFalse(r.merge_allowed())
        import codegen_bridge as cb
        decision, _reason = cb.outcome_from_summary({"merged": False, "stopped": "x"}, r)
        self.assertNotEqual(decision, getattr(cb, "DECISION_APPROVED", "approved"))


class FailedJobCanBeRetried(Harness):
    def make_manager(self):
        q = queue.Queue()

        def runner(**kw):
            return o.full_pipeline(
                kw["project_dir"], kw["repo_name"], kw["pr_id"], local_repo=kw["local_repo"],
                emit=kw["emit"], progress=kw["progress"], control=kw["control"], ask=kw["ask"],
                get_note=kw["get_note"], do_review=True, do_update_desc=True, do_merge=True,
                do_sync=True)
        return q, J.JobManager(q, runner=runner)

    def events(self, q):
        out = []
        while True:
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                return out

    def test_the_job_ends_as_an_error_the_user_can_retry(self):
        o.run_opencode_review = lambda *a, **k: (INCIDENT, "ses_x")
        q, m = self.make_manager()
        job = m.create(J.JobSpec(project_dir=self.clone, repo_name="repo", pr_id="7",
                                 local_repo=self.clone))
        m._work(job)
        kinds = [k for _id, k, _p in self.events(q)]
        self.assertIn("error", kinds)
        self.assertNotIn("done", kinds, "the job would have shown as Finished")
        job.status = J.ERROR                      # what the UI does on the "error" event
        self.assertTrue(job.is_terminal)
        again = m.retry(job.id)                   # the Retry button
        self.assertIs(again, job)
        self.assertEqual(job.status, J.QUEUED)
        self.assertEqual(self.merged + self.described, [])

    def test_the_error_message_says_what_happened_and_what_to_do(self):
        o.run_opencode_review = lambda *a, **k: (INCIDENT, "ses_x")
        q, m = self.make_manager()
        job = m.create(J.JobSpec(project_dir=self.clone, repo_name="repo", pr_id="7",
                                 local_repo=self.clone))
        m._work(job)
        (message,) = [p for _id, k, p in self.events(q) if k == "error"]
        for needle in ("did not examine", "no verdict", "Nothing was written",
                       "nothing was merged", "Retry the job"):
            self.assertIn(needle, message)


class CliFailsToo(Harness):
    def test_prism_run_exits_1_and_records_an_error(self):
        import os
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = (C.CONFIG_DIR, C.CONFIG_PATH)
        C.CONFIG_DIR = Path(tmp.name) / ".prism"
        C.CONFIG_PATH = C.CONFIG_DIR / "config.json"
        self.addCleanup(lambda: (setattr(C, "CONFIG_DIR", saved[0]),
                                 setattr(C, "CONFIG_PATH", saved[1])))
        o.run_opencode_review = lambda *a, **k: (INCIDENT, "ses_x")
        spec = J.JobSpec(project_dir=self.clone, repo_name="repo", pr_id="7",
                         local_repo=self.clone, origin="cli")
        out = io.StringIO()
        runner = lambda **kw: o.full_pipeline(  # noqa: E731
            kw.pop("project_dir"), kw.pop("repo_name"), kw.pop("pr_id"), **kw)
        with mock.patch.object(sys, "stdout", out):
            code = P.execute(spec, "ab12cd", runner=runner, interactive=False)
        rec = CJ.load("ab12cd")
        self.assertEqual(code, 1)
        self.assertEqual(rec["status"], "error")
        self.assertIn("did not examine", rec["error"])
        self.assertFalse(rec["merged"])
        self.assertEqual(self.merged + self.described, [])


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout.strip()


class ReviewStartsOnlyWhenTheCodeIsHere(unittest.TestCase):
    """Layer two: a failed fetch is no longer 'the reviewer will fetch it itself'."""

    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.origin, self.clone = make_remote_and_clone(base)
        _git(self.clone, "checkout", "-q", "main")
        _git(self.clone, "checkout", "-q", "-b", "topic")
        (self.clone / "a.txt").write_text("a\n", encoding="utf-8")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "feat work")
        _git(self.clone, "push", "-q", "origin", "topic")
        _git(self.clone, "fetch", "-q", "origin")
        self.tip = _git(self.clone, "rev-parse", "origin/topic")
        self.log = []

    def check(self, refs_ready, meta, local=None):
        return o._ensure_reviewable_refs(local if local is not None else str(self.clone), meta,
                                         "topic", "main", refs_ready,
                                         emit=lambda m, *a, **k: self.log.append(m))

    def test_a_successful_fetch_is_left_alone(self):
        self.assertTrue(self.check(True, {"sourceCommit": "0" * 40}))

    def test_a_failed_fetch_with_the_commit_already_here_reviews_from_the_clone(self):
        self.assertTrue(self.check(False, {"sourceCommit": self.tip}))
        self.assertTrue(any("already has" in m for m in self.log))

    def test_a_failed_fetch_without_the_commit_stops_before_any_model_run(self):
        with self.assertRaises(o.ReviewIncomplete) as cm:
            self.check(False, {"sourceCommit": "1" * 40})
        self.assertIn("nothing to review", str(cm.exception))
        self.assertIn("retry", str(cm.exception).lower())

    def test_a_stale_tracking_ref_is_not_mistaken_for_the_latest_code(self):
        # someone else pushes to the PR branch; this clone has not fetched it
        other = Path(self.tmp.name) / "other"
        _git(self.tmp.name, "clone", "-q", str(self.origin), str(other))
        _git(other, "config", "user.email", "o@x.com")
        _git(other, "config", "user.name", "O")
        _git(other, "checkout", "-q", "topic")
        (other / "b.txt").write_text("b\n", encoding="utf-8")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "newer")
        _git(other, "push", "-q", "origin", "topic")
        newest = _git(other, "rev-parse", "HEAD")
        self.assertNotEqual(newest, self.tip)
        with self.assertRaises(o.ReviewIncomplete):
            self.check(False, {"sourceCommit": newest})

    def test_nothing_to_verify_against_leaves_the_old_behaviour(self):
        self.assertFalse(self.check(False, {}))                      # PR lookup gave no commit
        self.assertFalse(self.check(False, {"sourceCommit": self.tip}, local=""))   # API mode
        self.assertFalse(o._ensure_reviewable_refs(str(self.clone), {"sourceCommit": self.tip},
                                                   "", "main", False))

    def test_the_review_does_not_start_when_it_cannot_see_the_code(self):
        started = []
        o.ensure_bundled_agents = lambda *a, **k: None
        o.require_engine = lambda: "opencode"
        o.engine_supports_json = lambda exe: False
        o._fetch_pr_refs = lambda *a, **k: False                      # the incident's timeout
        o.get_pr = lambda *a, **k: {"sourceReference": "topic", "destinationReference": "main",
                                    "sourceCommit": "2" * 40}
        o._run_stream_resilient = lambda *a, **k: started.append(1) or (0, GOOD, "s")
        with self.assertRaises(o.ReviewIncomplete):
            o.run_opencode_review("7", "repo", str(self.clone), str(self.clone),
                                  emit=lambda *a, **k: None)
        self.assertEqual(started, [], "a model run was spent on a review that cannot see the code")


class DocumentReviewsFollowTheSameRule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin, self.clone = make_remote_and_clone(self.tmp.name)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        saved = (o.ensure_bundled_agents, D._run_agent, o._continue_turn)
        o.ensure_bundled_agents = lambda *a, **k: ["doc-reviewer"]
        self.addCleanup(lambda: (setattr(o, "ensure_bundled_agents", saved[0]),
                                 setattr(D, "_run_agent", saved[1]),
                                 setattr(o, "_continue_turn", saved[2])))
        self.nudges = []
        o._continue_turn = lambda *a, **k: self.nudges.append(1) or (0, "x", "s")
        self.log = []

    def run_job(self, agent_output, **kw):
        D._run_agent = lambda *a, **k: (0, agent_output, "sess")
        t = D.DocTarget("repo", "clone", str(self.clone), "feat/x", ("docs/spec.md",))
        return D._pipeline(project_dir=str(self.project), targets=[t],
                           emit=lambda m, *a, **k: self.log.append(m), **kw)

    def remote(self):
        return _git(self.origin, "show", "feat/x:docs/spec.md")

    def history(self):
        return _git(self.origin, "log", "--format=%s", "feat/x")

    def test_an_incomplete_document_review_fails_and_writes_nothing(self):
        before = self.history()
        with self.assertRaises(RuntimeError) as cm:
            self.run_job("**Verdict:** ❓ Review incomplete\n**Impact score:** n/a\n"
                         "Summary: could not read the requirements. Coverage: 0 files reviewed.\n")
        self.assertIn("No document could be reviewed", str(cm.exception))
        self.assertEqual(self.history(), before)
        self.assertNotIn("PRISM review comments", self.remote())
        self.assertEqual(self.nudges, [], "it must not be pressed to 'send the report again'")

    def test_an_approval_that_admits_it_read_nothing_is_not_committed(self):
        before = self.history()
        with self.assertRaises(RuntimeError):
            self.run_job("**Verdict:** ✅ Approve\n**Impact score:** 1/10 — placeholder\n\n"
                         "<<<PRISM-REVIEW>>>\n### Summary\nI could not read the document, so "
                         "there are no comments.\n<<<END-PRISM-REVIEW>>>\n")
        self.assertEqual(self.history(), before)
        self.assertTrue(any("did not examine" in m for m in self.log))

    def test_a_normal_document_review_is_unaffected(self):
        from test_docreview import REPORT
        res = self.run_job(REPORT.format(n=1, m=2))
        self.assertTrue(res["pushed"])


class AgentsAreToldHowToSayIt(unittest.TestCase):
    def test_both_reviewers_have_an_honest_way_out_and_the_contract_says_why(self):
        for name in ("pr-reviewer", "doc-reviewer"):
            text = (ROOT / "agents" / f"{name}.md").read_text(encoding="utf-8")
            self.assertIn("Review incomplete", text, name)
            self.assertIn("placeholder", text, name)
        contract = (ROOT / "agents" / "_shared-contract.md").read_text(encoding="utf-8")
        self.assertIn("never invents a verdict", contract)


if __name__ == "__main__":
    unittest.main(verbosity=2)
