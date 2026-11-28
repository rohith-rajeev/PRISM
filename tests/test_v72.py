"""v7.2: job IDs (on every card, in every list, searchable, kept by a retry), the lists
split by kind, the document form starting empty, the file pickers, a retry as another
reviewer role, human approval of high-impact merges, and the security standards.

Everything runs offline: canned reviewers, stubbed CodeCommit, real local git where a
branch is written, and the desktop app only where there is a display.
"""
import io
import json
import os
import queue
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import chat as CH  # noqa: E402
import cli_jobs as CJ  # noqa: E402
import config as C  # noqa: E402
import docreview as D  # noqa: E402
import folderpicker as FP  # noqa: E402
import jobid  # noqa: E402
import jobs as J  # noqa: E402
import notifier as N  # noqa: E402
import prism_cli as P  # noqa: E402
from test_cli import Isolated  # noqa: E402
from test_v65 import card_texts  # noqa: E402
from test_v70 import PR_REPORT, DocBase, PrBase, _display  # noqa: E402

JOB_ID = r"^[0-9a-f]{10}$"


def pr_spec(pr="7", repo="acme-be", **kw):
    return J.JobSpec(project_dir="/tmp/p", repo_name=repo, pr_id=pr, **kw)


def doc_spec(persona="ba", files=("docs/a.md",)):
    return D.DocJobSpec(project_dir="/tmp/p", targets=(
        D.DocTarget("repo", "r", "/tmp/p", "main", tuple(files)),), persona=persona,
        owner="Priya")


def finish(job, status=J.DONE):
    job.status = status


# ============================================================= job IDs =========
class JobIds(unittest.TestCase):
    def test_a_job_id_is_ten_hex_characters_and_never_repeats(self):
        ids = [jobid.new() for _ in range(5000)]
        self.assertEqual(len(set(ids)), 5000)
        for r in ids[:50]:
            self.assertRegex(r, JOB_ID)
            self.assertTrue(jobid.valid(r))

    def test_only_a_well_formed_id_is_valid(self):
        for bad in ("", None, "abc", "0123456789a", "ABCDEF0123", "ghijklmnop", 12345,
                    "01234-6789"):
            self.assertFalse(jobid.valid(bad), bad)

    def test_what_a_person_pastes_is_cleaned_up(self):
        for text in ("3f9c2a71be", " Job ID 3f9c2a71be ", "job id: 3F9C2A71BE",
                     "PRISM-3f9c2a71be", "prism-3f9c2a71be", "Job #3f9c2a71be",
                     "Job-ID 3f9c2a71be"):
            self.assertEqual(jobid.normalise_query(text), "3f9c2a71be", text)
        # A name that merely starts with "job" or "prism-" is left alone.
        self.assertEqual(jobid.normalise_query("jobber-api"), "jobber-api")
        self.assertEqual(jobid.normalise_query("prism-api"), "prism-api")

    def test_matching_by_the_id_part_of_it_or_the_name(self):
        ids = ["3f9c2a71be"]
        self.assertTrue(jobid.matches("", ids, "x"))
        self.assertTrue(jobid.matches("3f9c", ids, "x"))
        self.assertTrue(jobid.matches("Job ID 3f9c2a71be", ids, "x"))
        self.assertTrue(jobid.matches("ACME", ids, "acme-be #7"))
        self.assertFalse(jobid.matches("3f9c2a71bf", ids, "acme-be #7"))


class JobIdOnJobs(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.m = J.JobManager(queue.Queue(), runner=lambda **k: self.seen.append(k) or {})

    def test_every_job_has_its_own_job_id_from_the_moment_it_exists(self):
        a, b = self.m.create(pr_spec("1")), self.m.create(pr_spec("2"))
        self.assertRegex(a.job_id, JOB_ID)
        self.assertNotEqual(a.job_id, b.job_id)

    def test_the_run_gets_the_jobs_id(self):
        job = self.m.create(pr_spec())
        self.m._work(job)
        self.assertEqual(self.seen[0]["job_id"], job.job_id)

    def test_a_retry_keeps_the_job_id(self):
        job = self.m.create(pr_spec())
        first = job.job_id
        finish(job)
        self.m.retry(job.id, model="m/other")
        self.assertEqual(job.job_id, first)
        self.m._work(job)
        self.assertEqual(self.seen[-1]["job_id"], first)
        docs = self.m.create(doc_spec())                # as another reviewer role too
        first = docs.job_id
        for persona in (None, "qa"):
            finish(docs)
            self.m.retry(docs.id, persona=persona)
            self.assertEqual(docs.job_id, first)

    def test_searching_the_list(self):
        a, b = self.m.create(pr_spec("1", repo="alpha")), self.m.create(pr_spec("2", repo="beta"))
        jobs = list(self.m.jobs.values())
        self.assertEqual(J.find_jobs(jobs, a.job_id), [a])
        self.assertEqual(J.find_jobs(jobs, "Job ID " + b.job_id), [b])
        self.assertEqual(J.find_jobs(jobs, ""), [a, b])
        self.assertEqual(J.find_jobs(jobs, "beta"), [b])
        self.assertEqual(J.find_jobs(jobs, "zzzzzzzzzz"), [])

    def test_the_job_id_is_on_the_job_page_and_in_the_chat_details(self):
        job = self.m.create(pr_spec())
        self.assertIn(f"job ID {job.job_id}", job.summary_line())
        docs = self.m.create(doc_spec())
        self.assertIn(f"job ID {docs.job_id}", docs.summary_line())
        details = job.chat_details()
        self.assertEqual(details["job_id"], job.job_id)
        self.assertIn(f"Job ID: {job.job_id}", CH.snapshot(details))


# ================================================== retry as another role ======
class RetryAsAnotherRole(unittest.TestCase):
    def setUp(self):
        self.m = J.JobManager(queue.Queue(), runner=lambda **k: {})

    def test_a_document_review_reruns_as_the_role_picked(self):
        job = self.m.create(doc_spec("ba"))
        finish(job)
        self.m.retry(job.id, persona="architect")
        self.assertEqual(job.spec.persona, "architect")
        self.assertIn("Architect", job.label)
        self.assertEqual(job.spec.owner, "Priya")              # nothing else changes
        self.assertEqual(job.spec.pipeline_kwargs()["persona"], "architect")
        finish(job)
        self.m.retry(job.id, persona="QA", model="m/x")           # both at once
        self.assertEqual((job.spec.persona, job.spec.model), ("qa", "m/x"))
        finish(job)
        self.m.retry(job.id)                                     # None keeps the role
        self.assertEqual(job.spec.persona, "qa")

    def test_a_pull_request_has_no_role_and_an_unknown_role_is_refused(self):
        pr = self.m.create(pr_spec())
        finish(pr)
        with self.assertRaises(ValueError):
            self.m.retry(pr.id, persona="qa")
        self.assertEqual(pr.status, J.DONE)                      # left as it was
        docs = self.m.create(doc_spec("ba", files=("docs/b.md",)))
        finish(docs)
        before = docs.job_id
        with self.assertRaises(ValueError):
            self.m.retry(docs.id, persona="pirate")
        self.assertEqual((docs.spec.persona, docs.status, docs.job_id),
                         ("ba", J.DONE, before))

    def test_a_refused_retry_changes_nothing(self):
        job = self.m.create(doc_spec("ba"))
        finish(job)
        self.m.create(doc_spec("qa"))                            # same documents, active
        with self.assertRaises(J.DuplicateJob):
            self.m.retry(job.id, persona="architect", model="m/x")
        self.assertEqual((job.spec.persona, job.spec.model, job.status),
                         ("ba", None, J.DONE))


# ======================================================== the chat cards =======
class Cards(unittest.TestCase):
    def test_the_pr_card_ends_with_its_job_id(self):
        card = N.build_card("repo", 7, True, verdict_raw="✅ Approve", verdict_key="approve",
                            impact_score="3", merged=True, job_id="3f9c2a71be")
        last = card["sections"][-1]["widgets"][-1]["textParagraph"]["text"]
        self.assertIn("Job ID 3f9c2a71be", last)
        self.assertNotIn("Job ID", json.dumps(N.build_card("repo", 7, True, verdict_raw="x",
                                                           verdict_key="approve")))

    def test_the_document_card_shows_its_job_id(self):
        card = N.build_doc_card("Business Analyst", owner="Priya", decision="approve",
                                job_id="3f9c2a71be", targets=[{"label": "Repository",
                                "role": "repo", "repo": "r", "branch": "main",
                                "documents": [{"file": "a.md", "verdict_key": "approve"}]}])
        self.assertIn("Job ID 3f9c2a71be",
                      card_texts({"cardsV2": [{"card": card}]})["_footer"])


class PrPipelineRunId(PrBase):
    def setUp(self):
        super().setUp()
        import orchestrator as o
        self.o = o
        self.posted = []
        p = mock.patch.object(N, "post_summary",
                              lambda url, **k: self.posted.append(k) or True)
        p.start()
        self.addCleanup(p.stop)
        o.get_pr_info = lambda *a, **k: {}

    def test_the_job_id_handed_in_is_printed_returned_and_on_the_card(self):
        lines = []
        res = self.run_pr(PR_REPORT.format(impact=3, why="small", scale="**Scale:** normal\n"),
                          job_id="3f9c2a71be", webhook_url="https://chat.example/x",
                          emit=lambda m, tag=None: lines.append(m))
        self.assertEqual(res["job_id"], "3f9c2a71be")
        self.assertTrue(any("Job ID 3f9c2a71be" in line for line in lines))
        self.assertEqual(self.posted[0]["job_id"], "3f9c2a71be")

    def test_without_one_a_fresh_id_is_made(self):
        for given in (None, "not-an-id"):
            res = self.run_pr(PR_REPORT.format(impact=3, why="s", scale="**Scale:** normal\n"),
                              job_id=given)
            self.assertRegex(res["job_id"], JOB_ID)


class HumanApprovalFlagged(PrBase):
    """A large or high-impact change waits for a person again (v7.2), and the card says
    so in its note."""

    def setUp(self):
        super().setUp()
        self.posted = []
        p = mock.patch.object(N, "post_summary",
                              lambda url, **k: self.posted.append(k) or True)
        p.start()
        self.addCleanup(p.stop)
        import orchestrator as o
        o.get_pr_info = lambda *a, **k: {}

    def card_note(self):
        card = N.build_card(**{k: v for k, v in self.posted[-1].items()
                               if k not in ("emit",)})
        return [w["decoratedText"] for sec in card["sections"] for w in sec["widgets"]
                if "decoratedText" in w and w["decoratedText"].get("startIcon", {})
                .get("materialIcon", {}).get("name") == "warning"]

    def run_with(self, impact, scale, answer):
        return self.run_pr(PR_REPORT.format(impact=impact, why="w",
                                            scale=f"**Scale:** {scale} — schema\n"),
                           webhook_url="https://chat.example/x",
                           ask=self.ask(answer) if answer else None)

    def test_a_large_change_a_person_approved_is_merged_and_flagged(self):
        res = self.run_with(3, "large", "proceed")
        self.assertTrue(res["merged"])
        self.assertEqual((self.posted[-1]["approval"], self.posted[-1]["scale"]),
                         ("approved", "large"))
        (note,) = self.card_note()
        self.assertEqual(note["text"], "Large or expensive change (impact 8/10): a person "
                                       "approved the merge.")

    def test_a_large_change_not_approved_is_not_merged_and_flagged(self):
        res = self.run_with(9, "large", "abort")
        self.assertFalse(res["merged"])
        self.assertEqual(self.posted[-1]["approval"], "not-approved")
        self.assertIn("a person did not approve the merge", self.card_note()[0]["text"])

    def test_with_nobody_to_ask_the_card_does_not_claim_a_person_said_no(self):
        res = self.run_with(9, "large", None)                # a detached or codegen run
        self.assertFalse(res["merged"])
        self.assertEqual(self.posted[-1]["approval"], "unasked")
        text = self.card_note()[0]["text"]
        self.assertIn("nobody was there to give it", text)
        self.assertNotIn("did not approve", text)

    def test_a_large_change_the_verdict_holds_says_only_that_it_is_large(self):
        report = ("**Verdict:** 🔴 Request changes\n**Impact score:** 9/10 — x\n"
                  "**Scale:** large — y\n")
        self.run_pr(report, webhook_url="https://chat.example/x", ask=self.ask("proceed"))
        self.assertEqual(self.asked, [])                     # no merge to approve
        self.assertIsNone(self.posted[-1]["approval"])
        self.assertEqual(self.card_note()[0]["text"],
                         "Large or expensive change (impact 9/10).")

    def test_a_normal_high_impact_change_is_flagged_too(self):
        self.run_with(7, "normal", "proceed")
        self.assertEqual(self.card_note()[0]["text"],
                         "High impact (7/10): a person approved the merge.")

    def test_a_low_impact_change_has_no_note(self):
        self.run_with(3, "normal", None)
        self.assertIsNone(self.posted[-1]["approval"])
        self.assertEqual(self.card_note(), [])

    def test_the_note_never_uses_an_emoji(self):
        for scale in ("large", "normal"):
            for approval in ("approved", "not-approved", "needed", None):
                text = N.approval_note(scale, approval, "8")
                self.assertFalse(any(ord(c) > 0x2000 for c in text), text)
        self.assertEqual(N.approval_note("large", "needed", "8"),
                         "Large or expensive change (impact 8/10): it is merged only "
                         "after a person approves it.")


class DocPipelineJobId(DocBase):
    def test_the_job_id_handed_in_names_the_review_and_its_commit(self):
        res = self.run_job(job_id="3f9c2a71be")
        self.assertEqual(res["job_id"], "3f9c2a71be")
        self.assertTrue(any("job ID 3f9c2a71be" in line for line in self.log))
        from test_v70 import sh
        self.assertIn("PRISM-3f9c2a71be:", sh(self.origin, "log", "-1", "--format=%s",
                                              "feat/x"))
        self.assertIn("· job `3f9c2a71be`", self.remote())

    def test_a_retry_keeps_the_job_id_and_both_reviews_are_kept(self):
        from test_v70 import sh
        self.run_job(job_id="3f9c2a71be")
        self.run_job(job_id="3f9c2a71be")               # the retry: same job, same ID
        subjects = sh(self.origin, "log", "-2", "--format=%s", "feat/x").splitlines()
        self.assertTrue(all(x.startswith("PRISM-3f9c2a71be:") for x in subjects), subjects)
        doc = self.remote()
        self.assertIn("Review v1", doc)
        self.assertIn("Review v2", doc)
        self.assertEqual(doc.count("· job `3f9c2a71be`"), 2)

    def test_a_retry_as_another_role_keeps_the_id_and_starts_that_roles_own_review(self):
        self.run_job(job_id="3f9c2a71be", persona="ba")
        self.run_job(job_id="3f9c2a71be", persona="architect")
        doc = self.remote()
        self.assertIn("Review v1 · Business Analyst", doc)
        self.assertIn("Review v2 · Architect", doc)
        self.assertNotIn("PREVIOUS REVIEW", self.prompts[1])   # not the BA's follow-up

    def test_each_run_has_its_own_working_names(self):
        # A kept commit's ref, the worktree and fallback copies never reuse an earlier
        # run's, though the job ID is shared.
        a, b = D._work_id("3f9c2a71be"), D._work_id("3f9c2a71be")
        self.assertNotEqual(a, b)
        self.assertTrue(a.startswith("3f9c2a71be-") and b.startswith("3f9c2a71be-"))

    def test_without_one_a_ten_character_id_is_made(self):
        self.assertRegex(self.run_job()["job_id"], JOB_ID)


# ======================================== security standards, an added layer ====
import security as SEC  # noqa: E402

STANDARDS = "**Standards:** ASVS: met · Top 10: F2 · AWS: n/a — no infrastructure · SSDF: met"


class SecurityStandards(unittest.TestCase):
    def test_the_four_standards_and_their_line(self):
        self.assertEqual(SEC.NAMES, ("ASVS", "Top 10", "AWS", "SSDF"))
        line = SEC.parse("**Verdict:** ✅ Approve\n" + STANDARDS + "\n- finding")
        self.assertEqual(line, "ASVS: met · Top 10: F2 · AWS: n/a — no infrastructure · "
                               "SSDF: met")
        self.assertEqual(SEC.missing(line), [])
        self.assertEqual(SEC.parse("- **Standards:** ASVS: met"), "ASVS: met")
        self.assertEqual(SEC.missing("ASVS: met"), ["Top 10", "AWS", "SSDF"])
        self.assertEqual(SEC.missing("CIS AWS 2.1: F3 · OWASP Top-10: met · ASVS: met · "
                                     "SSDF: met"), [])
        # A passing mention is not an answer: AWS has no conclusion of its own here.
        self.assertEqual(SEC.missing("ASVS: met · Top 10: met · SSDF: n/a — no AWS code"),
                         ["AWS"])
        self.assertEqual(SEC.parse("no line here"), "")
        self.assertEqual(SEC.missing(""), list(SEC.NAMES))

    def test_every_prompt_names_all_four_as_an_addition_to_the_review(self):
        for text in (SEC.PR_TEXT, SEC.DOC_TEXT):
            for name in ("OWASP ASVS", "OWASP Top 10", "CIS AWS Foundations Benchmark",
                         "AWS Foundational Security Best Practices", "NIST SSDF"):
                self.assertIn(name, text)
            self.assertIn("ADDITIONAL layer", text)
            self.assertIn("replaces and narrows nothing", text)
            self.assertIn(SEC.LINE_FORMAT, text)

    def test_the_document_brief_carries_them_after_the_always_checked_areas(self):
        t = D.DocTarget("repo", "r", "/r", "b", ("a.md",))
        brief = D.build_prompt(t, "a.md", "/wt/a.md", None, [], False, [], "/wt", [], "", "",
                               1, persona="qa")
        self.assertIn(D.ALWAYS_TEXT, brief)                     # nothing it replaces
        self.assertIn(SEC.DOC_TEXT, brief)
        self.assertLess(brief.index(D.ALWAYS_TEXT), brief.index(SEC.DOC_TEXT))

    def test_the_agents_say_it_is_on_top_of_their_review(self):
        doc = (ROOT / "agents" / "doc-reviewer.md").read_text(encoding="utf-8")
        self.assertIn("on top of everything above", doc)
        self.assertIn("- **Standards:** ASVS:", doc)
        for name in ("OWASP ASVS", "OWASP Top 10", "NIST", "CIS AWS"):
            self.assertIn(name, doc)
        contract = (ROOT / "agents" / "_shared-contract.md").read_text(encoding="utf-8")
        self.assertIn("Security standards are an added layer", contract)


class PrSecurityStandards(PrBase):
    def report(self, standards=True):
        return PR_REPORT.format(impact=2, why="w", scale="**Scale:** normal — x\n"
                                + (STANDARDS + "\n" if standards else ""))

    def test_a_report_with_the_line_is_read_logged_and_written_to_the_description(self):
        lines = []
        res = self.run_pr(self.report(), emit=lambda m, tag=None: lines.append(m))
        self.assertTrue(res["merged"])                           # the review is unchanged
        self.assertEqual(res["review"].standards,
                         "ASVS: met · Top 10: F2 · AWS: n/a — no infrastructure · SSDF: met")
        self.assertFalse(any("Standards line" in t for t in self.turns))
        self.assertTrue(any(m.startswith("▸ Security standards: ASVS") for m in lines))
        (_args, kw), = self.described
        self.assertEqual(kw["standards"], res["review"].standards)
        import orchestrator as o
        desc = o.compose_description("", "Approve", "2", ["- x"], standards=kw["standards"])
        self.assertIn("Security standards: ASVS: met · Top 10: F2", desc)

    def test_a_report_without_it_is_asked_once_and_never_held_up(self):
        import orchestrator as o
        o._continue_turn = lambda prompt, *a, **k: (self.turns.append(prompt)
                                                    or (0, STANDARDS, "ses_x"))
        res = self.run_pr(self.report(standards=False))
        self.assertEqual(sum("Standards line" in t for t in self.turns), 1)
        self.assertTrue(res["review"].standards.startswith("ASVS: met"))
        self.assertTrue(res["merged"])

    def test_still_without_it_the_log_names_what_is_missing(self):
        lines = []
        res = self.run_pr(self.report(standards=False), emit=lambda m, tag=None:
                          lines.append(m))
        self.assertEqual(sum("Standards line" in t for t in self.turns), 1)  # once, no loop
        self.assertTrue(res["merged"])           # an added check: it blocks nothing
        self.assertTrue(any("no conclusion on ASVS, Top 10, AWS, SSDF" in m for m in lines))


class DocSecurityStandards(DocBase):
    def test_the_conclusions_are_written_into_the_document(self):
        from test_v70 import doc_report
        self.reports.append(doc_report().replace(
            "- **Scalability:** nothing found.",
            "- **Scalability:** nothing found.\n- " + STANDARDS))
        res = self.run_job()
        self.assertIn("**Standards:** ASVS: met · Top 10: F2", self.remote())
        self.assertTrue(any("security standards — ASVS: met" in m for m in self.log))
        self.assertFalse(any("no conclusion on" in m for m in self.log))
        self.assertEqual(res["documents"], 1)

    def test_a_review_without_them_is_flagged_in_the_log(self):
        self.run_job()
        self.assertTrue(any("no conclusion on ASVS, Top 10, AWS, SSDF" in m
                            for m in self.log))


# =================================================== command-line jobs =========
class CliJobIds(Isolated):
    def test_a_new_cli_job_s_id_is_its_job_id_and_a_retry_keeps_it(self):
        job = CJ.new_id()
        self.assertRegex(job, JOB_ID)
        rec = CJ.create(pr_spec(), job)
        self.assertEqual((rec["id"], rec["job_id"]), (job, job))
        CJ.finish(job, J.DONE)
        self.assertEqual(CJ.create(pr_spec(), job)["job_id"], job)      # the retry
        self.assertEqual(CJ.find_by_job_id(job)["id"], job)
        self.assertEqual(CJ.find_by_job_id("Job ID " + job)["id"], job)
        self.assertIsNone(CJ.find_by_job_id("cccccccccc"))
        self.assertIsNone(CJ.find_by_job_id(job[:4]))                   # exact only
        self.assertTrue(CJ.matches(CJ.load(job), job[:4]))              # a search is not

    def test_a_job_from_before_v7_2_gets_a_job_id_when_rerun_and_keeps_it(self):
        rec = CJ.create(pr_spec(), "abc123")             # an old six-character id
        rec.pop("job_id")
        CJ._write("abc123", rec)
        self.assertEqual(CJ.job_id_of(CJ.load("abc123")), "")
        self.assertTrue(CJ.matches(CJ.load("abc123"), "abc1"))
        CJ.finish("abc123", J.DONE)
        given = CJ.create(pr_spec(), "abc123")["job_id"]            # first rerun
        self.assertRegex(given, JOB_ID)
        CJ.finish("abc123", J.DONE)
        self.assertEqual(CJ.create(pr_spec(), "abc123")["job_id"], given)
        self.assertEqual(CJ.find_by_job_id(given)["id"], "abc123")

    def test_execute_hands_the_job_id_to_the_run(self):
        seen = {}
        job = CJ.new_id()
        code = P.execute(pr_spec(), job, quiet=True, interactive=False,
                         runner=lambda **k: seen.update(k) or {"merged": False})
        self.assertEqual(code, P.EXIT_OK)
        self.assertEqual(seen["job_id"], job)
        self.assertEqual(CJ.load(job)["job_id"], job)
        self.assertIn(f"PRISM job {job}", CJ.read_log(job))

    def jobs(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = P.main(["jobs", *argv])
        return code, out.getvalue()

    def test_prism_jobs_finds_by_job_id(self):
        CJ.create(pr_spec("1", repo="alpha"), "1111111111")
        CJ.finish("1111111111", J.DONE)
        CJ.create(pr_spec("2", repo="beta"), "2222222222")
        CJ.finish("2222222222", J.DONE)
        code, out = self.jobs("--all")
        self.assertIn("1111111111", out)
        code, out = self.jobs("--find", "Job ID 2222222222")    # finished ones included
        self.assertEqual(code, P.EXIT_OK)
        self.assertIn("beta", out)
        self.assertNotIn("alpha", out)
        code, out = self.jobs("--find", "9999999999")
        self.assertIn("No job matches", out)
        code, out = self.jobs("--find", "1111", "--json")
        data = json.loads(out)
        self.assertEqual([(r["id"], r["job_id"]) for r in data],
                         [("1111111111", "1111111111")])
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(P.main(["jobs", "--find", "  "]), P.EXIT_USAGE)

    def test_an_older_job_shows_both_ids_and_either_works(self):
        rec = CJ.create(pr_spec(), "abc123")
        rec["job_id"] = "3f9c2a71be"
        CJ._write("abc123", rec)
        CJ.finish("abc123", J.DONE)
        code, out = self.jobs("--all")
        self.assertIn("abc123 (3f9c2a71be)", out)
        self.assertEqual(P.resolve_job_id("3f9c2a71be"), "abc123")
        self.assertEqual(P.resolve_job_id("abc123"), "abc123")
        self.assertEqual(P.resolve_job_id("nope"), "nope")
        CJ.log_path("abc123").write_text("the transcript\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(P.main(["logs", "3f9c2a71be"]), P.EXIT_OK)
        self.assertIn("the transcript", out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(P.main(["rm", "3f9c2a71be"]), P.EXIT_OK)
        self.assertIsNone(CJ.load("abc123"))

    def test_retry_as_another_role_from_the_command_line(self):
        CJ.create(doc_spec("ba"), "abc123")
        CJ.finish("abc123", J.DONE)
        ran = []
        with mock.patch.object(P, "execute", lambda spec, job_id, **k: ran.append(spec) or 0):
            self.assertEqual(P.main(["retry", "abc123", "--persona", "architect", "-q"]), 0)
        self.assertEqual(ran[0].persona, "architect")
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            self.assertEqual(P.main(["retry", "abc123", "--persona", "pirate", "-q"]),
                             P.EXIT_USAGE)
            CJ.create(pr_spec(), "def456")
            CJ.finish("def456", J.DONE)
            self.assertEqual(P.main(["retry", "def456", "--persona", "qa", "-q"]),
                             P.EXIT_USAGE)
        self.assertIn("document review", err.getvalue())

    def test_the_chat_about_a_cli_job_knows_its_job_id(self):
        rec = CJ.create(pr_spec(), "3f9c2a71be")
        self.assertIn("Job ID: 3f9c2a71be", CH.snapshot(P.record_details(rec, "")))


# =================================================== the file choosers ========
class FileChooser(unittest.TestCase):
    def test_the_chooser_shows_every_kind_of_document_a_review_reads(self):
        cmd, _sep = FP.files_command("t", "/tmp", lambda n: "/usr/bin/zenity"
                                     if n == "zenity" else None)
        first = next(a for a in cmd if a.startswith("--file-filter="))
        for suffix in D.UPLOAD_TYPES:
            self.assertIn(f"*{suffix}", first, suffix)
            self.assertIn(f"*{suffix.upper()}", first, suffix)
        self.assertIn("--file-filter=All files | *", cmd)


# ========================================================= the desktop =========
@unittest.skipUnless(_display(), "needs a display")
class Desktop(Isolated):
    def setUp(self):
        super().setUp()
        C.set_username("Tester")
        import app as A
        self.A = A
        p = mock.patch.object(A, "list_available_models", lambda *a, **k: [])
        p.start()
        self.addCleanup(p.stop)
        # Jobs are created but never actually run here.
        p2 = mock.patch.object(J.JobManager, "pump", lambda self: [])
        p2.start()
        self.addCleanup(p2.stop)
        self.app = A.App()
        self.app.geometry("1200x900")
        self.addCleanup(self.app.destroy)
        self.app.update()
        self.form = self.app.doc_form

    def settle(self):
        for _ in range(3):
            time.sleep(0.15)
            self.app.update()

    def type_search(self, entry, text):
        self.A._set_entry_text(entry, text)
        self.settle()

    def shown_rows(self):
        inner = self.app.jobs_list.inner
        return [w for w in inner.pack_slaves() if isinstance(w, self.A.JobRow)]

    # ---- the file buttons, and no drag-and-drop ----
    def test_the_file_buttons_say_where_files_come_from_and_nothing_takes_drops(self):
        for card in self.form.cards.values():
            self.assertEqual(card["browse"]._text, "Browse repository…")
            self.assertEqual(card["upload"]._text, "Select Files")
        # Drawn whole, never cut short with an ellipsis.
        self.app._show_new_tab("docs")
        self.app.show_new()
        self.settle()
        for key, label in (("browse", "Browse repository…"), ("upload", "Select Files")):
            btn = self.form.cards["BE"][key]
            drawn = [btn.itemcget(i, "text") for i in btn.find_all() if btn.type(i) == "text"]
            self.assertEqual(drawn, [label])
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        for gone in ("tkdnd", "_register_drop", "_dnd_available", "drop files"):
            self.assertNotIn(gone, source)

    # ---- the form starts empty ----
    def test_the_owner_is_never_prefilled(self):
        self.assertEqual(self.form.owner_var.get(), "")

    def start_a_document_review(self, owner="Priya"):
        f = self.form
        f.proj_var.set("")
        f.refresh_detection()
        doc = self.home / "spec.md"
        doc.write_text("# spec\n", encoding="utf-8")
        f.add_uploads("BE", [str(doc)])
        f.owner_var.set(owner)
        f.persona_btns["qa"]._click(None)
        # A model the engine lists (an unlisted one is dropped when the list loads).
        p = mock.patch.object(self.A, "list_available_models",
                              lambda *a, **k: ["anthropic/claude-sonnet-5-5"])
        p.start()
        self.addCleanup(p.stop)
        self.app._load_models_async(force=True)
        self.settle()
        f.model_picker.set_custom("anthropic/claude-sonnet-5-5")
        f.instr_text.insert("1.0", "check retries")
        self.app._show_new_tab("docs")
        self.app._start()
        self.settle()

    def test_starting_a_review_clears_the_form_and_a_new_job_starts_empty(self):
        self.start_a_document_review()
        (job,) = self.app.manager.jobs.values()
        self.assertEqual((job.spec.owner, job.spec.persona), ("Priya", "qa"))
        f = self.form
        self.assertEqual((f.owner_var.get(), f.instr_text.get("1.0", "end").strip(),
                          f.cards["BE"]["uploads"]), ("", "", []))
        # v7.2.1: the reviewer role and the model stay for the next request.
        self.assertEqual((f.persona_var.get(), f.model_picker.get()),
                         ("qa", "anthropic/claude-sonnet-5-5"))
        # "+ New job" (from the list or from the job's page) never refills it either.
        f.owner_var.set("Someone")
        self.app.show_new()
        self.assertEqual(f.owner_var.get(), "")
        self.app.show_detail(job.id)
        f.owner_var.set("Someone")
        self.app._new_from_detail()
        self.assertEqual(f.owner_var.get(), "")
        self.assertEqual(self.app.new_tab, "docs")
        self.assertIsNotNone(f.problem())                  # the owner must be typed again
        self.assertEqual((f.persona_var.get(), f.model_picker.get()),
                         ("qa", "anthropic/claude-sonnet-5-5"))

    # ---- job IDs in the list, and searching it ----
    def test_rows_show_the_job_id_and_the_search_filters_them(self):
        app = self.app
        a = app.manager.create(pr_spec("1", repo="alpha"))
        b = app.manager.create(pr_spec("2", repo="beta"))
        app.show_jobs()
        self.settle()
        self.assertIn(f"job ID {a.job_id}", app.rows[a.id].sub.cget("text"))
        self.assertEqual(len(self.shown_rows()), 2)
        self.type_search(app.jobs_search, f"Job ID {b.job_id}")
        self.assertEqual(self.shown_rows(), [app.rows[b.id]])
        self.type_search(app.jobs_search, a.job_id[:4])
        self.assertIn(app.rows[a.id], self.shown_rows())
        self.type_search(app.jobs_search, "ffffffffff")
        self.assertEqual(self.shown_rows(), [])
        self.assertTrue(app.jobs_empty.winfo_ismapped())
        self.assertIn("ffffffffff", app.jobs_empty.cget("text"))
        self.type_search(app.jobs_search, "")
        self.assertEqual(len(self.shown_rows()), 2)
        self.assertFalse(app.jobs_empty.winfo_ismapped())

    def test_a_retried_job_is_found_by_the_same_job_id(self):
        app = self.app
        job = app.manager.create(pr_spec())
        first = job.job_id
        job.status = J.DONE
        app.manager.retry(job.id)
        app.show_jobs()
        self.type_search(app.jobs_search, first)
        self.assertEqual(self.shown_rows(), [app.rows[job.id]])
        self.assertIn(f"job ID {first}", app.rows[job.id].sub.cget("text"))

    def test_a_search_only_a_cli_job_matches_leads_to_it(self):
        app = self.app
        CJ.create(pr_spec(repo="gamma"), "3f9c2a71be")
        CJ.finish("3f9c2a71be", J.DONE)
        app._poll_cli(force=True)
        app.show_jobs()
        self.type_search(app.jobs_search, "3f9c2a71be")
        self.assertIn("command-line job does", app.jobs_empty.cget("text"))
        app._search_in_cli()
        self.settle()
        self.assertIs(app.screen, app.cli_screen)
        self.assertEqual(self.A._entry_value(app.cli_search), "3f9c2a71be")
        row = app.cli_rows["3f9c2a71be"]
        self.assertTrue(row.winfo_ismapped())
        self.assertIn("job ID 3f9c2a71be", row.sub.cget("text"))
        self.type_search(app.cli_search, "0000000000")
        self.assertFalse(row.winfo_ismapped())
        self.assertIn("0000000000", app.cli_empty.cget("text"))

    # ---- pull requests and document reviews listed apart ----
    def test_pull_requests_and_document_reviews_have_a_tab_each(self):
        app = self.app
        pr = app.manager.create(pr_spec())
        docs = app.manager.create(doc_spec())
        app.show_jobs()
        self.settle()
        self.assertEqual(app.jobs_kind, "pr")
        self.assertEqual(self.shown_rows(), [app.rows[pr.id]])
        self.assertEqual(app.jobs_tabs["pr"]._text, "Pull requests · 1")
        self.assertEqual(app.jobs_tabs["docs"]._text, "Document reviews · 1")
        self.assertTrue(app.jobs_tabs["pr"]._selected)
        app.jobs_tabs["docs"]._click(None)
        self.settle()
        self.assertEqual(self.shown_rows(), [app.rows[docs.id]])
        self.assertTrue(app.jobs_tabs["docs"]._selected)
        # Opening a job and coming back shows that job's own tab.
        app.show_detail(pr.id)
        app.show_jobs()
        self.settle()
        self.assertEqual(self.shown_rows(), [app.rows[pr.id]])

    def test_a_search_matching_only_the_other_tab_says_so_and_goes_there(self):
        app = self.app
        app.manager.create(pr_spec())
        docs = app.manager.create(doc_spec())
        app.show_jobs()
        self.type_search(app.jobs_search, docs.job_id)
        self.assertEqual(self.shown_rows(), [])
        self.assertEqual(app.jobs_tabs["docs"]._text, "Document reviews · 1")
        self.assertEqual(app.jobs_tabs["pr"]._text, "Pull requests · 0")
        self.assertIn("1 document review matches", app.jobs_empty.cget("text"))
        app._jobs_empty_clicked()
        self.settle()
        self.assertEqual(app.jobs_kind, "docs")
        self.assertEqual(self.shown_rows(), [app.rows[docs.id]])

    def test_an_empty_tab_points_at_the_other(self):
        app = self.app
        app.manager.create(doc_spec())
        app.show_jobs()
        self.settle()
        self.assertEqual(self.shown_rows(), [])
        self.assertIn("No pull-request reviews yet", app.jobs_empty.cget("text"))
        self.assertIn("1 document review — click to see it", app.jobs_empty.cget("text"))

    def test_command_line_jobs_have_the_same_two_tabs(self):
        app = self.app
        CJ.create(pr_spec(), "aaa111")
        CJ.finish("aaa111", J.DONE)
        CJ.create(doc_spec(), "bbb222")
        CJ.finish("bbb222", J.DONE)
        app.show_cli()
        self.settle()
        a, b = app.cli_rows["aaa111"], app.cli_rows["bbb222"]
        self.assertTrue(a.winfo_ismapped())
        self.assertFalse(b.winfo_ismapped())
        self.assertEqual(app.cli_tabs["docs"]._text, "Document reviews · 1")
        app.cli_tabs["docs"]._click(None)
        self.settle()
        self.assertFalse(a.winfo_ismapped())
        self.assertTrue(b.winfo_ismapped())

    def test_ctrl_f_goes_to_the_search_box(self):
        app = self.app
        app.show_jobs()
        focused = []
        with mock.patch.object(app.jobs_search, "focus_set", lambda: focused.append(1)):
            self.assertEqual(app._focus_search(), "break")
        self.assertEqual(focused, [1])
        app.show_new()
        self.assertIsNone(app._focus_search())             # only on a job list

    # ---- a retry as another reviewer role ----
    def test_the_job_page_offers_the_reviewer_role_for_a_document_review_only(self):
        app = self.app
        pr = app.manager.create(pr_spec())
        docs = app.manager.create(doc_spec("ba"))
        app.show_detail(pr.id)
        self.settle()
        self.assertFalse(app.detail_role_card.winfo_ismapped())
        app.show_detail(docs.id)
        self.settle()
        self.assertTrue(app.detail_role_card.winfo_ismapped())
        self.assertEqual(app.detail_persona, "ba")
        docs.status = J.DONE
        first = docs.job_id
        app.detail_role_btns["architect"]._click(None)
        app._retry_current_job()
        self.assertEqual(docs.spec.persona, "architect")
        self.assertEqual(docs.job_id, first)                     # same job, same ID
        self.assertEqual(app.detail_persona, "architect")       # the page follows the job
        self.assertIn("Architect", app.rows[docs.id].title.cget("text"))
        # A retry from the list keeps the role the job has.
        docs.status = J.DONE
        app.show_jobs()
        app._retry_job(docs.id)
        self.assertEqual(docs.spec.persona, "architect")


if __name__ == "__main__":
    unittest.main()
