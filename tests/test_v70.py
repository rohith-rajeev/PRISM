"""v7.0 acceptance: one test (or more) per acceptance scenario in
docs/requirements/PRISM-v7.0.md, named after the requirement it proves.

Everything runs offline: canned reviewers, a fake GitLab client, a stand-in for the
chat webhook, and real local git repositories wherever a branch is written.
"""
import base64
import importlib
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
if "PRISM_HISTORY_DIR" not in os.environ:
    os.environ["PRISM_HISTORY_DIR"] = tempfile.mkdtemp(prefix="prism-test-history-")
    __import__("atexit").register(__import__("shutil").rmtree,
                                  os.environ["PRISM_HISTORY_DIR"], True)

import chat as CH  # noqa: E402
import cli_jobs as CJ  # noqa: E402
import codegen_bridge as IB  # noqa: E402
import config as C  # noqa: E402
import diagnostics as DG  # noqa: E402
import docreview as D  # noqa: E402
import extract as X  # noqa: E402
import findings as F  # noqa: E402
import gitlab as GL  # noqa: E402
import jobs as J  # noqa: E402
import notifier as N  # noqa: E402
import orchestrator as o  # noqa: E402
import prism_cli as P  # noqa: E402
from test_cli import Isolated  # noqa: E402
from test_docreview import make_remote_and_clone  # noqa: E402
from test_v65 import Capture, card_texts  # noqa: E402


def sh(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def doc_report(verdict="✅ Approve", impact=4, scale="normal", comments=(), extra=""):
    """A doc-reviewer report. `comments` are (severity, n, text)."""
    sections = {"Blocker": [], "Major": [], "Minor": [], "Suggestion": []}
    for sev, n, text in comments:
        sections[sev].append(f"- **[{sev}] DR-{n}** · Gap · §2 — {text}. Evidence: x.")
    body = ["### Summary", "- **Document:** a spec"]
    for sev, title in (("Blocker", "Blockers"), ("Major", "Major"), ("Minor", "Minor"),
                       ("Suggestion", "Suggestions")):
        if sections[sev]:
            body += ["", f"### {title}"] + sections[sev]
    body += ["", "### Coverage", "- **Security:** nothing found.",
             "- **Stability:** nothing found.", "- **Scalability:** nothing found."]
    if extra:
        body += ["", extra]
    scale_line = f"**Scale:** {scale} — why\n" if scale else ""
    return (f"**Verdict:** {verdict}\n**Impact score:** {impact}/10 — reason\n{scale_line}\n"
            "<<<PRISM-REVIEW>>>\n" + "\n".join(body) + "\n<<<END-PRISM-REVIEW>>>\n")


class DocBase(unittest.TestCase):
    """Real git, a canned doc-reviewer, nothing else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.origin, self.clone = make_remote_and_clone(self.tmp.name)
        self.project = self.home / "proj"
        self.project.mkdir()
        orig = (C.CONFIG_DIR, C.CONFIG_PATH)
        C.CONFIG_DIR = self.home / ".prism"
        C.CONFIG_PATH = C.CONFIG_DIR / "config.json"
        self.addCleanup(lambda: (setattr(C, "CONFIG_DIR", orig[0]),
                                 setattr(C, "CONFIG_PATH", orig[1])))
        self.reports, self.prompts, self.turns, self.log = [], [], [], []
        saved = (o.ensure_bundled_agents, D._run_agent, o._continue_turn)
        o.ensure_bundled_agents = lambda *a, **k: ["doc-reviewer"]
        D._run_agent = self.fake_agent
        o._continue_turn = self.fake_turn
        self.addCleanup(lambda: (setattr(o, "ensure_bundled_agents", saved[0]),
                                 setattr(D, "_run_agent", saved[1]),
                                 setattr(o, "_continue_turn", saved[2])))

    def fake_agent(self, prompt, project_dir, emit, model, control, meter):
        self.prompts.append(prompt)
        return 0, (self.reports.pop(0) if self.reports else doc_report()), "sess-1"

    def fake_turn(self, prompt, *a, **k):
        self.turns.append(prompt)
        return 0, "", "sess-1"           # a turn that says nothing (no scale, no report)

    def target(self, files=("docs/spec.md",), uploads=(), branch="feat/x"):
        return D.DocTarget("repo", "clone", str(self.clone), branch, tuple(files),
                           uploads=tuple(uploads))

    def run_job(self, **kw):
        args = dict(project_dir=str(self.project), targets=[self.target()],
                    emit=lambda m, tag=None: self.log.append(m), owner="Priya")
        args.update(kw)
        return D._pipeline(**args)

    def remote(self, path="docs/spec.md", branch="feat/x"):
        return sh(self.origin, "show", f"{branch}:{path}")


# =========================================================== FR-VER ==========
class FrVerDocumentDecision(DocBase):
    def test_acceptance_1_one_major_and_two_minors_is_request_changes(self):
        self.reports = [doc_report("✅ Approve", comments=[
            ("Major", 1, "payments missing"), ("Minor", 2, "typo"), ("Minor", 3, "wording")])]
        out = self.run_job()
        self.assertEqual(out["decision"], "request-changes")
        doc = self.remote()
        self.assertIn("**[Major] DR-1**", doc)
        head = next(ln for ln in doc.splitlines() if ln.startswith("**Decision:**"))
        self.assertIn("Request changes — work may not start", head)
        self.assertNotIn("Approve", head)

    def test_acceptance_2_only_suggestions_is_approve_and_they_are_written(self):
        self.reports = [doc_report("🔴 Request changes", comments=[
            ("Suggestion", 1, "add a diagram"), ("Suggestion", 2, "name the owner")])]
        out = self.run_job()
        self.assertEqual(out["decision"], "approve")
        doc = self.remote()
        self.assertIn("**Decision:** ✅ Approve — work may start", doc)
        self.assertIn("**[Suggestion] DR-1**", doc)
        self.assertIn("**[Suggestion] DR-2**", doc)

    def test_acceptance_3_the_reviewers_approve_cannot_override_a_blocker(self):
        self.reports = [doc_report("✅ Approve", comments=[("Blocker", 1, "contradiction")])]
        out = self.run_job()
        self.assertEqual(out["decision"], "request-changes")
        self.assertEqual(out["results"][0]["verdict_key"], "request-changes")
        self.assertTrue(any("does not decide a document review" in m for m in self.log))

    def test_acceptance_4_an_old_approve_with_comments_heading_stays_readable(self):
        # A 6.5.1 section with a Major still open, at the current commit.
        base = sh(self.clone, "rev-parse", "HEAD").strip()
        old = D.Run({"id": "20261101-1000-aaaaaa", "v": 1, "date": "2026-11-01 10:00",
                     "persona": "ba", "persona_label": "Business Analyst", "commit": base,
                     "verdict": "⚠️ Approve with comments", "impact": "5/10",
                     "counts": "1 major"},
                    "### Major\n- **[Major] DR-1** · Gap · §2 — payments missing. Evidence: x.")
        p = self.clone / "docs" / "spec.md"
        p.write_text(D.with_review(p.read_text(encoding="utf-8"), old), encoding="utf-8")
        sh(self.clone, "commit", "-am", "6.5.1 review")
        sh(self.clone, "push", "origin", "feat/x")
        self.reports = [doc_report("✅ Approve", comments=[("Major", 1, "still missing")],
                                   extra="### Previous comments\n"
                                         "- **DR-1** · ❌ Still open — still missing")]
        out = self.run_job()
        self.assertEqual(out["decision"], "request-changes")
        doc = self.remote()
        _b, runs, _a = D.split_document(doc)
        self.assertEqual([r.meta["v"] for r in runs], [2, 1])
        self.assertEqual(runs[0].meta["decision"], "request-changes")
        self.assertEqual(runs[1].meta["verdict"], "⚠️ Approve with comments")
        self.assertIn("Approve with comments", doc)          # the old heading, as written
        self.assertIn("**Decision:** 🔴 Request changes — work may not start", doc)

    def test_fr_ver_1_the_retired_labels_never_appear_for_a_v7_run(self):
        C.set_docs_webhook_url("https://chat.googleapis.com/v1/spaces/D/messages?key=k")
        self.reports = [doc_report("⚠️ Approve with comments",
                                   comments=[("Minor", 1, "x")])]
        cap = Capture()
        with mock.patch.object(N.urllib.request, "urlopen", cap):
            out = self.run_job(webhook_url=C.get_docs_webhook_url())
        text = "\n".join(self.log) + json.dumps(cap.bodies, ensure_ascii=False) \
            + self.remote().split("<!-- prism-review:start -->")[1] \
            + json.dumps(out["findings"], ensure_ascii=False)
        for retired in ("Approve with comments", "⛔ Block", "Block —"):
            self.assertNotIn(retired, text)
        self.assertEqual(out["decision"], "approve")

    def test_a_major_reported_still_open_but_not_relisted_stays_open(self):
        """The independent review's finding: a Major the reviewer only marks "Still open"
        under Previous comments must keep the document at Request changes."""
        body = ("### Summary\n- x\n\n### Previous comments\n"
                "- **DR-1** · ❌ Still open — still missing\n- **DR-2** · ✅ Resolved — done\n\n"
                "### Minor\n- **[Minor] DR-3** · Gap · §1 — typo.\n\n### Coverage\n- ok\n")
        open_list = [("Major", "DR-1", "- **[Major] DR-1** · Gap · §2 — payments missing."),
                     ("Minor", "DR-2", "- **[Minor] DR-2** · Gap · §3 — x.")]
        new, unassessed = D.carry_unassessed(body, open_list)
        self.assertEqual(unassessed, [])
        counts = D.severity_counts(new)
        self.assertEqual((counts["Major"], counts["Minor"]), (1, 1))
        self.assertEqual(D.decide(counts), "request-changes")
        self.assertIn("### Still open", new)
        self.assertIn("DR-1", [f for _s, f, _l in D.findings(new)])
        self.assertNotIn("DR-2", [f for _s, f, _l in D.findings(new)])

    def test_a_run_with_an_unreviewed_document_is_not_approved(self):
        f = self.home / "broken.docx"
        f.write_bytes(b"not a zip")
        self.reports = [doc_report(comments=[("Minor", 1, "x")])]
        out = self.run_job(targets=[self.target(uploads=[D.Upload(str(f))])])
        self.assertEqual(out["results"][0]["verdict_key"], "approve")
        self.assertEqual(out["decision"], "request-changes")

    def test_fr_x_4_a_cut_short_review_is_request_changes(self):
        self.assertEqual(D.decide({"Minor": 1}, partial=True), "request-changes")


# =========================================================== FR-SCALE ========
PR_REPORT = ("## PR #7\n**Verdict:** ✅ Approve\n**Impact score:** {impact}/10 — {why}\n"
             "{scale}- **[Low] style — `x.py:1`** nit\n\n### Summary\nCoverage: x.py.\n")


class PrBase(unittest.TestCase):
    """The PR pipeline with CodeCommit stubbed out, as the SafetyGates tests do."""

    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clone = self._tmp.name
        o._REVIEWED_STORE_PATH = Path(self.clone) / "reviewed.json"
        self.merged, self.asked, self.described, self.turns = [], [], [], []
        o.ensure_bundled_agents = lambda *a, **k: ["pr-reviewer"]
        o.resolve_repo = lambda r, p, l: (r, self.clone)
        o.check_ff_mergeable = lambda *a, **k: (True, "ok")
        o.try_fast_forward_merge = lambda *a, **k: self.merged.append(1) or {}
        o.update_description_direct = lambda *a, **k: self.described.append((a, k)) or ""
        o._continue_turn = lambda prompt, *a, **k: (self.turns.append(prompt)
                                                    or (0, "", "ses_x"))
        o.get_pr = lambda *a, **k: {"status": "OPEN", "repositoryName": "repo",
                                    "destinationReference": "main",
                                    "sourceReference": "feat", "description": "",
                                    "sourceCommit": "abc"}

    def ask(self, answer):
        def _ask(q, choices=None):
            self.asked.append(q)
            return answer
        return _ask

    def run_pr(self, report, **kw):
        o.run_opencode_review = lambda *a, **k: (report, "ses_x")
        kw.setdefault("emit", lambda *a, **k: None)
        return o.full_pipeline(self.clone, "repo", "7", local_repo=self.clone, **kw)


class FrScale(PrBase):
    def test_acceptance_1_a_large_change_scored_4_is_raised_to_8_and_waits_for_a_person(self):
        # v7.2: a large change keeps its verdict and a person approves its merge (before
        # v7.2 it was turned into Request changes and never merged).
        report = PR_REPORT.format(impact=4, why="small",
                                  scale="**Scale:** large — migrates the orders table\n")
        res = self.run_pr(report, ask=self.ask("abort"))
        review = res["review"]
        self.assertEqual(review.verdict_key, "approve")
        self.assertEqual(review.impact_score, "8")
        self.assertTrue(review.impact_reason.startswith(o.LARGE_REASON_PREFIX))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("LARGE OR EXPENSIVE CHANGE", self.asked[0])
        self.assertIn("migrates the orders table", self.asked[0])
        self.assertEqual(self.merged, [])
        self.assertEqual(res["stopped"], "high-impact-not-confirmed")
        self.assertFalse(review.person_approved)
        (args, kw), = self.described
        self.assertIn("Approve", args[1])
        self.assertEqual(args[2], "8")
        self.assertEqual(kw.get("scale"), "large")
        self.asked.clear()
        res = self.run_pr(report, ask=self.ask("proceed"))
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(res["merged"])
        self.assertTrue(res["review"].person_approved)

    def test_acceptance_2_a_codegen_client_cannot_merge_a_large_change(self):
        report = PR_REPORT.format(impact=9, why="x", scale="**Scale:** large — infra\n")
        res = self.run_pr(report, ask=None, do_merge=True)      # codegen: nobody to ask
        self.assertEqual(self.merged, [])
        self.assertEqual(res["stopped"], "high-impact-not-confirmed")
        decision, reason = IB.outcome_from_summary(res, res["review"])
        self.assertEqual(decision, IB.DECISION_NEEDS_HUMAN)
        self.assertIn("person", reason)

    def test_acceptance_3_a_normal_high_impact_change_still_asks_and_merges_on_proceed(self):
        report = PR_REPORT.format(impact=8, why="auth", scale="**Scale:** normal — one line\n")
        res = self.run_pr(report, ask=self.ask("proceed"))
        self.assertEqual(len(self.asked), 1)
        self.assertTrue(res["merged"])
        self.asked.clear()
        res = self.run_pr(report, ask=self.ask("abort"))
        self.assertEqual(len(self.asked), 1)
        self.assertFalse(res["merged"])
        self.assertEqual(res["stopped"], "high-impact-not-confirmed")

    def test_acceptance_5_no_scale_line_and_impact_9_is_large_after_one_prompt(self):
        res = self.run_pr(PR_REPORT.format(impact=9, why="x", scale=""),
                          ask=self.ask("proceed"))
        self.assertEqual(sum("Scale line" in t for t in self.turns), 1, "asked exactly once")
        self.assertEqual(res["review"].scale, "large")
        self.assertTrue(res["review"].scale_missing)
        self.assertEqual(res["review"].verdict_key, "approve")
        self.assertEqual(len(self.asked), 1)                 # a person approved it
        self.assertTrue(res["merged"])

    def test_fr_scale_2_no_scale_line_and_impact_5_is_normal_and_recorded_missing(self):
        res = self.run_pr(PR_REPORT.format(impact=5, why="x", scale=""))
        self.assertEqual(res["review"].scale, "normal")
        self.assertTrue(res["review"].scale_missing)
        self.assertTrue(res["merged"])

    def test_fr_scale_2_a_scale_given_on_the_re_ask_is_used(self):
        o._continue_turn = lambda p, *a, **k: (0, "**Scale:** large — rewrites auth", "ses_x")
        res = self.run_pr(PR_REPORT.format(impact=3, why="x", scale=""))
        self.assertEqual(res["review"].scale, "large")
        self.assertFalse(res["review"].scale_missing)
        self.assertEqual(res["stopped"], "high-impact-not-confirmed")   # nobody asked

    def test_fr_scale_4_block_is_kept_for_a_large_change(self):
        report = ("**Verdict:** ⛔ Block\n**Impact score:** 9/10 — x\n**Scale:** large — y\n")
        res = self.run_pr(report)
        self.assertEqual(res["review"].verdict_key, "block")
        self.assertEqual(self.merged, [])

    def test_fr_scale_5_a_large_change_is_not_merged_in_a_dry_run(self):
        res = self.run_pr(PR_REPORT.format(impact=8, why="x", scale="**Scale:** large — y\n"),
                          dry_run=True, ask=self.ask("proceed"))
        self.assertEqual(self.merged, [])
        self.assertFalse(res["merged"])

    def test_findings_carry_the_scale(self):
        res = self.run_pr(PR_REPORT.format(impact=2, why="x", scale="**Scale:** normal — y\n"))
        self.assertTrue(res["findings"])
        self.assertTrue(all(f["scale"] == "normal" for f in res["findings"]))
        self.assertEqual(tuple(res["findings"][0]), F.FINDING_KEYS)

    def test_scale_parsing(self):
        for text, want in (("**Scale:** large — many modules", ("large", "many modules")),
                           ("Scale: normal", ("normal", "")),
                           ("**Scale**: `Large` - schema migration", ("large",
                                                                      "schema migration")),
                           ("- **Scalability:** nothing found.", ("", "")),
                           ("no scale here", ("", ""))):
            self.assertEqual(o.parse_scale(text), want, text)
        r = o.ReviewResult(verdict_key="approve", scale="large")
        self.assertTrue(r.merge_allowed())           # v7.2: once a person approves it

    def test_the_agent_instructions_state_the_scale_rules(self):
        """FR-SCALE-7: both reviewers are told what large means and the 8/10 floor; a
        large document is never approved (a large PR waits for a person, since v7.2)."""
        for name in ("pr-reviewer.md", "doc-reviewer.md"):
            text = " ".join((ROOT / "agents" / name).read_text(encoding="utf-8").split())
            with self.subTest(agent=name):
                self.assertIn("**Scale:**", text)
                self.assertIn("migrates data or schema", text)
                self.assertIn("8/10", text)
                if name == "doc-reviewer.md":
                    self.assertIn("never approved", text)
                else:
                    self.assertNotIn("never approved", text)


class FrScaleDocuments(DocBase):
    def test_acceptance_4_a_large_document_with_no_blocker_is_request_changes(self):
        self.reports = [doc_report("✅ Approve", impact=3, scale="large",
                                   comments=[("Minor", 1, "typo")])]
        out = self.run_job()
        self.assertEqual(out["decision"], "request-changes")
        self.assertEqual(out["scale"], "large")
        self.assertEqual(out["results"][0]["impact"], "8")
        doc = self.remote()
        self.assertIn("the change is large or expensive", doc)
        self.assertIn("even though no Blocker or Major comment is open", doc)


# =========================================================== FR-OWN ==========
class FrOwn(DocBase):
    def test_acceptance_1_no_owner_does_not_start(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(owner="  ")
        self.assertIn("owner", str(cm.exception))
        self.assertEqual(self.prompts, [], "no model time spent")

    def test_acceptance_2_the_card_shows_owner_and_reviewer_separately(self):
        C.set_docs_webhook_url("https://chat.googleapis.com/v1/spaces/D/messages?key=k")
        cap = Capture()
        with mock.patch.object(N.urllib.request, "urlopen", cap):
            self.run_job(webhook_url=C.get_docs_webhook_url(), reviewer="Alex")
        t = card_texts(cap.bodies[0])
        self.assertEqual(t["_subtitle"], "Owner: Priya")
        self.assertEqual(t["Reviewed by"], "Alex")
        # the owner is the first fact after the title, before the reviewer role
        order = json.dumps(cap.bodies[0], ensure_ascii=False)
        self.assertLess(order.index('"Owner: Priya"'), order.index('"Reviewer role"'))

    def test_acceptance_3_the_owner_is_on_the_job_and_in_the_document(self):
        spec = D.DocJobSpec(project_dir=str(self.project), targets=(self.target(),),
                            owner="Priya", reviewer="Alex")
        job = J.Job(1, spec, queue.Queue())
        self.assertIn("owner Priya", job.summary_line())
        self.assertIn("owner Priya", spec.summary_bits())
        self.run_job(reviewer="Alex")
        self.assertIn("· owner Priya · by Alex · job `", self.remote())

    def test_the_owner_is_cleaned_and_survives_a_record(self):
        spec = D.DocJobSpec(project_dir="/p", targets=(self.target(),), owner=" <Priya>\n")
        self.assertEqual(spec.owner, "Priya")
        back = CJ.spec_from_dict(CJ.spec_to_dict(spec))
        self.assertEqual(back.owner, "Priya")

    def test_pull_request_jobs_have_no_owner(self):
        self.assertFalse(hasattr(J.JobSpec(project_dir="/p", repo_name="r", pr_id="1"),
                                 "owner"))


class FrOwnCommandLine(Isolated):
    def test_the_command_line_requires_owner(self):
        origin, clone = make_remote_and_clone(str(self.home))
        from test_cli_docs import args
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(files=["docs/spec.md"], owner=None), cwd=clone)
        self.assertIn("--owner", str(cm.exception))
        spec = P.build_docs_spec(args(files=["docs/spec.md"], owner="Priya"), cwd=clone)
        self.assertEqual(spec.owner, "Priya")

    def test_the_owner_is_never_remembered(self):
        # v7.2: a name carried over from the last review is how a review ends up under
        # the wrong owner, so none is kept.
        self.assertFalse(hasattr(C, "get_last_owner") or hasattr(C, "set_last_owner"))


# =========================================================== FR-CARD =========
def doc(file, folder="docs", key="approve"):
    return {"file": file, "folder": folder, "verdict_key": key}


class FrCard(unittest.TestCase):
    def card(self, targets, **kw):
        base = dict(persona="QA", owner="Priya", reviewer="Alex", decision="request-changes",
                    impact=6, impact_reason="", counts={"Major": 1}, targets=targets,
                    job_id="r1", outcome="committed and pushed",
                    decision_reason="1 major comment open")
        base.update(kw)
        return N.build_doc_card(**base)

    @staticmethod
    def docs(card):
        """[(icon, text, bottomLabel)] of the Documents section, headings included."""
        out = []
        for w in card["sections"][1]["widgets"]:
            d = w["decoratedText"]
            out.append(((d.get("startIcon") or {}).get("materialIcon", {}).get("name"),
                        d["text"], d.get("bottomLabel")))
        return out

    def test_acceptance_1_two_repositories_and_long_branches_stay_short(self):
        branch = "feature/" + "very-long-branch-name-" * 5
        card = self.card([
            {"label": "Backend", "role": "BE", "repo": "acme/payments-backend-service",
             "branch": branch, "documents": [doc("spec.md"), doc("api.md", key="request-changes")]},
            {"label": "Frontend", "role": "FE", "repo": "acme-portal", "branch": "feat/ui",
             "documents": [doc("ui.md"), doc("flows.md")]}])
        self.assertEqual(card["header"], {"title": "Document review",
                                          "subtitle": "Owner: Priya"})
        decision = card["sections"][0]["widgets"][0]["decoratedText"]
        self.assertEqual(decision["startIcon"], {"materialIcon": {"name": "cancel"}})
        self.assertEqual(decision["text"], "<b>Work may not start</b>")
        rows = self.docs(card)
        self.assertEqual(rows[0][1], f"<b>Backend · acme/payments-backend-service · "
                                     f"{N.short_branch(branch)}</b>")
        self.assertLessEqual(len(N.short_branch(branch)), 60)
        self.assertEqual(rows[3][1], "<b>Frontend · acme-portal · feat/ui</b>")
        self.assertEqual([(i, t) for i, t, _b in rows[1:3]],
                         [("check_circle", "spec.md"), ("cancel", "api.md")])
        text = json.dumps(card, ensure_ascii=False)
        for word in ("Request changes", "— Approve", "✅", "🔴", "⚠️", "⛔"):
            self.assertNotIn(word, text, "icons, not words or emoji")
        self.assertEqual(len(card["sections"]), 3, "summary, documents, note + footer")

    def test_acceptance_2_twelve_documents_show_five_and_a_count(self):
        many = [doc(f"doc-{i:02d}.md") for i in range(12)]
        rows = self.docs(self.card([{"label": "r", "role": "repo", "repo": "r", "branch": "b",
                                     "documents": many}]))
        self.assertEqual(sum(1 for i, _t, _b in rows if i == "check_circle"), 5)
        self.assertEqual(rows[-1][1], "and 7 more")

    def test_acceptance_3_two_files_with_one_name_get_their_parent_folder(self):
        rows = self.docs(self.card([{"label": "r", "role": "repo", "repo": "r", "branch": "b",
                                     "documents": [doc("spec.md", "billing"),
                                                   doc("spec.md", "auth"),
                                                   doc("other.md", "auth")]}]))
        self.assertEqual([b for _i, _t, b in rows[1:]], ["in billing", "in auth", None])

    def test_acceptance_4_markup_in_a_file_name_is_shown_as_text(self):
        rows = self.docs(self.card([{"label": "r", "role": "repo", "repo": "r&co",
                                     "branch": "a<b", "documents": [doc("<b>x</b> & y.md")]}]))
        self.assertEqual(rows[0][1], "<b>r&amp;co · a&lt;b</b>")
        self.assertEqual(rows[1][1], "&lt;b&gt;x&lt;/b&gt; &amp; y.md")

    def test_the_impact_is_a_score_and_the_reason_is_a_short_note(self):
        reason = ("Raised to 8/10 because the change is large or expensive (the reviewer gave "
                  "4/10): touches the payment retry path and the refunds service. More text.")
        card = self.card([], counts={"Blocker": 1, "Major": 2, "Minor": 1, "Suggestion": 0},
                         impact=8, impact_reason=reason,
                         decision_reason="1 blocker and 2 major comments open")
        t = card_texts({"cardsV2": [{"card": card}]})
        self.assertEqual(t["Comments"], "1 blocker, 2 major, 1 minor")
        self.assertEqual(t["Impact"], "8/10")
        self.assertEqual(t["_note"], "1 blocker and 2 major comments open. Impact 8/10: "
                                     "touches the payment retry path and the refunds service.")
        self.assertEqual(N.comment_counts_text({}), "No comments")

    def test_a_long_reason_is_shortened_without_an_ellipsis(self):
        long = ("touches " + ", ".join(f"module {i}" for i in range(40)))
        short = N.short_reason(long)
        self.assertLessEqual(len(short), N.NOTE_MAX)
        self.assertNotIn("…", short)
        self.assertFalse(short.endswith(","))
        self.assertEqual(N.short_reason("One sentence. Two."), "One sentence")
        self.assertEqual(N.short_reason(""), "")

    def test_fr_card_2_footer_and_fr_card_4_names(self):
        card = self.card([], not_reviewed=1)
        foot = card["sections"][-1]["widgets"][-1]["textParagraph"]["text"]
        self.assertIn("Job ID r1 · committed and pushed · 1 document could not be reviewed", foot)
        self.assertEqual(N.short_repo("group/sub/" + "a" * 40), "a" * 40)
        self.assertEqual(N.short_repo("short-name"), "short-name")
        self.assertEqual(N.short_branch("x" * 60), "x" * 60)
        long = "s" * 30 + "MIDDLE" + "e" * 30
        self.assertTrue(N.short_branch(long).startswith("s" * 29))
        self.assertTrue(N.short_branch(long).endswith("e" * 29))

    def test_fr_card_5_one_repository_has_no_role_prefix(self):
        rows = self.docs(self.card([{"label": "Backend", "role": "BE", "repo": "svc",
                                     "branch": "b", "documents": [doc("a.md")]}]))
        self.assertEqual(rows[0][1], "<b>svc · b</b>")

    def test_an_approval_has_a_tick(self):
        card = self.card([], decision="approve")
        d = card["sections"][0]["widgets"][0]["decoratedText"]
        self.assertEqual((d["startIcon"]["materialIcon"]["name"], d["text"]),
                         ("check_circle", "<b>Work may start</b>"))


# =========================================================== FR-DOC-IN =======
def tiny_pdf(text):
    """A minimal, valid text PDF (one page, one line)."""
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def tiny_docx(paragraphs):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    from xml.sax.saxutils import escape
    body = "".join(f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Default Extension="xml" ContentType='
                   '"application/xml"/></Types>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {ns}><w:body>'
                                        f"{body}<w:sectPr/></w:body></w:document>")
    return buf.getvalue()


class FrDocIn(DocBase):
    def setUp(self):
        super().setUp()
        self.desktop = self.home / "Desktop"
        self.desktop.mkdir()

    def upload(self, name, data):
        p = self.desktop / name
        p.write_bytes(data) if isinstance(data, bytes) else p.write_text(data, encoding="utf-8")
        return p

    def test_acceptance_1_a_dropped_markdown_spec_is_reviewed_against_the_branch(self):
        f = self.upload("feature.md", "# Feature\n\nDo the thing.\n")
        before = sh(self.origin, "rev-parse", "feat/x")
        out = self.run_job(targets=[self.target(files=(), uploads=[D.Upload(str(f))])])
        self.assertEqual(out["documents"], 1)
        prompt = self.prompts[0]
        self.assertIn("uploaded document `feature.md`", prompt)
        self.assertIn("Read the code from this checkout of that branch", prompt)
        self.assertIn("branch 'feat/x'", prompt)
        copy = self.desktop / "feature.prism-review.md"
        self.assertTrue(copy.is_file())
        self.assertIn(D.SECTION_TITLE, copy.read_text(encoding="utf-8"))
        self.assertEqual(f.read_text(encoding="utf-8"), "# Feature\n\nDo the thing.\n")
        self.assertEqual(sh(self.origin, "rev-parse", "feat/x"), before, "branch unchanged")
        self.assertFalse(out["committed"])

    def test_acceptance_2_a_destination_path_commits_and_pushes_it(self):
        f = self.upload("feature.md", "# Feature\n\nDo the thing.\n")
        out = self.run_job(targets=[self.target(
            files=(), uploads=[D.Upload(str(f), "docs/specs/feature.md")])])
        self.assertTrue(out["committed"] and out["pushed"])
        text = self.remote("docs/specs/feature.md")
        self.assertTrue(text.startswith("# Feature\n\nDo the thing.\n"))
        self.assertIn(D.SECTION_TITLE, text)
        # pushed without force: the old tip is an ancestor of the new one
        sh(self.origin, "merge-base", "--is-ancestor", "main", "feat/x")

    def test_acceptance_3_a_png_is_not_added_and_is_named(self):
        bad = D.upload_type_problem("diagram.png")
        self.assertIn("diagram.png", bad)
        self.assertIn(".png", bad)
        self.assertIsNone(D.upload_type_problem("spec.MD"))
        for ok in ("a.txt", "a.md", "a.docx", "a.pdf", "a.xlsx"):
            self.assertIsNone(D.upload_type_problem(ok), ok)
        png = self.upload("diagram.png", b"\x89PNG")
        with self.assertRaises(RuntimeError):
            self.run_job(targets=[self.target(files=(), uploads=[D.Upload(str(png))])])

    def test_acceptance_4_a_pdf_gets_a_sidecar_and_is_not_changed(self):
        data = tiny_pdf("Payments must retry three times")
        f = self.upload("spec.pdf", data)
        self.assertIn("retry three times", X.extract_text(f))      # the reviewer reads text
        out = self.run_job(do_commit=False,
                           targets=[self.target(files=(), uploads=[D.Upload(str(f))])])
        self.assertEqual(out["documents"], 1)
        self.assertEqual(f.read_bytes(), data, "PDF bytes unchanged")
        side = self.desktop / "spec.pdf.prism-review.md"
        self.assertTrue(side.is_file())
        self.assertIn(D.SECTION_TITLE, side.read_text(encoding="utf-8"))
        self.assertIn("you are given its text, extracted by PRISM", self.prompts[0])

    def test_acceptance_5_no_working_folder_still_reviews_and_says_no_codebase(self):
        f = self.upload("feature.md", "# Feature\n")
        reqs = self.home / "reqs"
        reqs.mkdir()
        (reqs / "r.md").write_text("REQ-1\n", encoding="utf-8")
        t = D.DocTarget(D.ROLE_NONE, "", "", "", (), uploads=(D.Upload(str(f)),))
        out = self.run_job(project_dir="", targets=[t], requirements_path=str(reqs))
        self.assertEqual(out["documents"], 1)
        self.assertIn("NO CODEBASE IS AVAILABLE", self.prompts[0])
        self.assertNotIn("Read the code from", self.prompts[0])
        copy = (self.desktop / "feature.prism-review.md").read_text(encoding="utf-8")
        self.assertIn(D.NO_CODEBASE_LINE, copy)
        self.assertIn("docs-workspace", D.workspace_dir(""))

    def test_fr_doc_in_9_a_word_file_gets_a_reviewed_copy(self):
        data = tiny_docx(["Intro", "Payments & refunds"])
        f = self.upload("spec.docx", data)
        self.reports = [doc_report(comments=[("Major", 1, "refunds <missing>")])]
        self.run_job(targets=[self.target(files=(), uploads=[D.Upload(str(f))])])
        self.assertEqual(f.read_bytes(), data, "the original is never modified")
        copy = self.desktop / "spec.prism-review.docx"
        text = X.extract_text(copy)
        self.assertLess(text.index("Payments & refunds"), text.index("PRISM review comments"))
        self.assertIn("[Major] DR-1", text)
        self.assertIn("refunds <missing>", text)
        self.assertIn("Request changes", text)

    def test_fr_doc_in_4_an_unreadable_upload_is_reported_not_reviewed(self):
        good = self.upload("ok.md", "# OK\n")
        broken = self.upload("broken.docx", b"not a zip")
        out = self.run_job(targets=[self.target(
            files=(), uploads=[D.Upload(str(broken)), D.Upload(str(good))])])
        self.assertEqual(out["documents"], 1)
        self.assertEqual(out["not_reviewed"], 1)
        self.assertTrue(any("broken.docx could not be read" in m for m in self.log))

    def test_fr_doc_in_11_a_dry_run_writes_no_copy(self):
        f = self.upload("feature.md", "# Feature\n")
        self.run_job(dry_run=True, targets=[self.target(
            files=(), uploads=[D.Upload(str(f), "docs/new.md")])])
        self.assertEqual(sorted(p.name for p in self.desktop.iterdir()), ["feature.md"])
        with self.assertRaises(subprocess.CalledProcessError):
            self.remote("docs/new.md")

    def test_fr_doc_in_10_a_destination_on_the_branch_already_is_refused(self):
        f = self.upload("spec.md", "# Other\n")
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(targets=[self.target(
                files=(), uploads=[D.Upload(str(f), "docs/spec.md")])])
        self.assertIn("already exists on branch", str(cm.exception))
        self.assertEqual(self.prompts, [])

    def test_a_copy_never_overwrites_an_earlier_one(self):
        f = self.upload("feature.md", "# Feature\n")
        for _ in range(2):
            self.run_job(targets=[self.target(files=(), uploads=[D.Upload(str(f))])])
        names = sorted(p.name for p in self.desktop.iterdir())
        self.assertEqual(names, ["feature.md", "feature.prism-review-2.md",
                                 "feature.prism-review.md"])

    def test_repository_documents_keep_todays_path(self):
        f = self.upload("feature.md", "# Feature\n")
        out = self.run_job(targets=[self.target(uploads=[D.Upload(str(f))])])
        self.assertEqual(out["documents"], 2)
        self.assertIn(D.SECTION_TITLE, self.remote())                 # FR-DOC-IN-8
        with self.assertRaises(subprocess.CalledProcessError):
            self.remote("feature.md")                                 # not added on its own


class FrDocInCommandLine(Isolated):
    def test_dest_maps_uploads_and_unknown_types_are_refused(self):
        from test_cli_docs import args, fixture
        origin, clone = fixture(self.home / "w")
        a = self.home / "a.md"
        b = self.home / "b.pdf"
        a.write_text("x", encoding="utf-8")
        b.write_bytes(tiny_pdf("y"))
        spec = P.build_docs_spec(args(files=[str(a), str(b)],
                                      dest=["a.md=docs/a.md", f"{b}=docs/b.pdf"]), cwd=clone)
        ups = {Path(u.path).name: u.dest for u in spec.targets[0].uploads}
        # Stored with this OS's separators, like every path typed on the command line.
        self.assertEqual(ups, {"a.md": os.path.join("docs", "a.md"),
                               "b.pdf": os.path.join("docs", "b.pdf")})
        png = self.home / "c.png"
        png.write_bytes(b"x")
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(files=[str(png)]), cwd=clone)
        self.assertIn("c.png", str(cm.exception))
        with self.assertRaises(P.UsageError):
            P.build_docs_spec(args(files=[str(a), str(b)], dest=["docs/x.md"]), cwd=clone)


# =========================================================== FR-GL ===========
class FakeGitLab:
    """Stands in for gitlab.Client: one merge request, recorded writes."""

    def __init__(self, base_url, token=None, **_kw):
        self.base_url = base_url
        self.mr = {"repositoryName": "grp/app", "sourceReference": "feat",
                   "destinationReference": "main", "sourceCommit": "c0ffee",
                   "destinationCommit": "d00d", "mergeBase": "d00d", "isMerged": False,
                   "title": "t", "description": "Author text.", "status": "OPEN",
                   "authorArn": "dev", "detailedMergeStatus": "mergeable",
                   "hasConflicts": False, "divergedCommitsCount": 0,
                   "sourceProjectId": 1, "targetProjectId": 1, "draft": False}
        self.method = "ff"
        self.ff = (True, "fast-forward possible")
        self.descriptions, self.merges = [], []
        self.merge_error = None
        FakeGitLab.last = self

    def get_pr(self, project, iid):
        return dict(self.mr)

    def update_description(self, project, iid, text):
        self.descriptions.append(text)
        self.mr["description"] = text

    def ff_mergeable(self, project, iid):
        return self.ff

    def merge_method_allows_ff(self, project):
        return (self.method == "ff", f"the project's merge method is '{self.method}'")

    def merge(self, project, iid, sha):
        if self.merge_error:
            raise self.merge_error
        self.merges.append(sha)


GL_REPORT = ("## PR !5\n**Verdict:** ✅ Approve\n**Impact score:** 3/10 — small\n"
             "**Scale:** normal — one module\n- **[Low] style — `a.py:1`** nit\n")


class FrGitLab(PrBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(GL, "Client", FakeGitLab)
        p.start()
        self.addCleanup(p.stop)
        self.synced = []
        o.sync_destination_into_source = lambda *a, **k: self.synced.append(a)
        # The synced head is a merge whose first parent is the reviewed commit.
        self.heads = {"5ynced^1": "c0ffee"}
        o._git_head = lambda repo, ref: self.heads.get(ref, "5ynced")
        # o.time is the process-wide time module: patch it so the real sleep comes back.
        # (Restoring "time.sleep" read after the stub was set left it a no-op for every
        # later test.)
        p2 = mock.patch.object(o.time, "sleep", lambda s: None)
        p2.start()
        self.addCleanup(p2.stop)

    def run_mr(self, report=GL_REPORT, setup=None, review=None, **kw):
        o.run_opencode_review = review or (lambda *a, **k: (report, "ses_x"))
        if setup:
            orig = FakeGitLab.__init__

            def init(fake, *a, **k):
                orig(fake, *a, **k)
                setup(fake)
            mock.patch.object(FakeGitLab, "__init__", init).start()
            self.addCleanup(mock.patch.stopall)
        kw.setdefault("emit", lambda *a, **k: None)
        return o.full_pipeline(self.clone, "grp/app", "5", local_repo=self.clone,
                               host="gitlab", host_url="https://gitlab.example.com", **kw)

    def test_acceptance_1_approve_at_3_describes_and_fast_forwards(self):
        res = self.run_mr(ask=self.ask("proceed"))
        fake = FakeGitLab.last
        self.assertTrue(res["merged"])
        self.assertEqual(fake.merges, ["c0ffee"], "the reviewed commit, by fast-forward")
        (desc,) = fake.descriptions
        self.assertTrue(desc.startswith("Author text."), "the author's text is kept")
        self.assertIn("<!-- prism:start -->", desc)
        self.assertIn("- **[Low] style — `a.py:1`** nit", desc)
        self.assertEqual(self.merged, [], "never the CodeCommit call")

    def test_fr_gl_10_a_stale_block_is_replaced_and_dry_run_writes_nothing(self):
        def stale(fake):
            fake.mr["description"] = ("Mine.\n\n<!-- prism:start -->\nold\n"
                                      "<!-- prism:end -->")
        self.run_mr(setup=stale)
        (desc,) = FakeGitLab.last.descriptions
        self.assertEqual(desc.count("<!-- prism:start -->"), 1)
        self.assertNotIn("\nold\n", desc)
        res = self.run_mr(dry_run=True)
        self.assertEqual(FakeGitLab.last.descriptions, [])
        self.assertEqual(FakeGitLab.last.merges, [])
        self.assertEqual(res["stopped"], "dry-run")

    def test_acceptance_2_a_large_merge_request_waits_for_a_person(self):
        report = GL_REPORT.replace("normal — one module", "large — schema")
        res = self.run_mr(report)                  # nobody to ask: stays open
        self.assertEqual(FakeGitLab.last.merges, [])
        self.assertEqual(res["review"].verdict_key, "approve")
        self.assertEqual(res["stopped"], "high-impact-not-confirmed")
        res = self.run_mr(report, ask=self.ask("proceed"))
        self.assertTrue(res["merged"])

    def test_acceptance_3_a_project_that_cannot_fast_forward_stops_without_a_push(self):
        def merge_commit(fake):
            fake.method = "merge"
            fake.ff = (False, "the project's merge method is 'merge'")
        res = self.run_mr(setup=merge_commit)
        self.assertEqual(res["stopped"], "not-fast-forwardable")
        self.assertIn("merge", res["reason"])
        self.assertEqual(self.synced, [], "nothing synced or pushed")
        self.assertEqual(FakeGitLab.last.merges, [])

    def test_acceptance_4_a_diverged_merge_request_is_synced_then_fast_forwarded(self):
        def diverged(fake):
            fake.ff = (False, "the source branch is 2 commit(s) behind the target")
            fake.mr["divergedCommitsCount"] = 2
        res = self.run_mr(setup=diverged)
        self.assertEqual(len(self.synced), 1)
        self.assertEqual(FakeGitLab.last.merges, ["5ynced"], "the synced head, nothing else")
        self.assertTrue(res["merged"])
        self.assertEqual(res["strategy"], "sync-then-fast-forward")

    def test_fr_gl_13_a_push_during_the_sync_window_is_never_merged(self):
        """The independent review's finding: someone pushes while a person confirms or
        resolves conflicts; the sync picks the push up; the merge must not happen."""
        def diverged(fake):
            fake.ff = (False, "behind")
        self.heads = {"5ynced^1": "a11en"}         # the sync sat on an unreviewed commit
        res = self.run_mr(setup=diverged)
        self.assertEqual(res["stopped"], "pr-changed-since-review")
        self.assertEqual(FakeGitLab.last.merges, [])

    def test_fr_gl_13_a_move_during_the_review_is_caught(self):
        calls = {"n": 0}

        def moves_once(fake):
            orig = fake.get_pr

            def get_pr(project, iid):
                calls["n"] += 1
                mr = orig(project, iid)
                if calls["n"] > 2:              # after the up-front read and the review
                    mr["sourceCommit"] = "beef"
                return mr
            fake.get_pr = get_pr
        res = self.run_mr(setup=moves_once)
        self.assertEqual(res["stopped"], "pr-changed-since-review")
        self.assertEqual(FakeGitLab.last.merges, [])

    def test_acceptance_5_a_conflict_the_person_aborts_pushes_nothing(self):
        def conflict(*a, **k):
            raise o.SyncConflict([{"file": "a.py", "binary": False, "hunks": [("x", "y")]}],
                                 "main", "feat")
        o.sync_destination_into_source = conflict
        o.resolve_conflicts_with_user = lambda *a, **k: None          # the person aborts

        def diverged(fake):
            fake.ff = (False, "behind")
        with self.assertRaises(RuntimeError):
            self.run_mr(setup=diverged, ask=self.ask("abort"))
        self.assertEqual(FakeGitLab.last.merges, [])

    def test_acceptance_6_no_merge_permission_stops_with_the_hosts_error(self):
        def forbidden(fake):
            fake.merge_error = GL.GitLabError(
                "GitLab PUT /projects/x/merge_requests/5/merge failed (403): 403 Forbidden",
                status=403)
        with self.assertRaises(GL.GitLabError) as cm:
            self.run_mr(setup=forbidden)
        self.assertIn("403", str(cm.exception))
        self.assertEqual(len(FakeGitLab.last.descriptions), 1, "description left as saved")
        self.assertEqual(self.synced, [], "no sync attempted on a permission error")

    def test_fr_gl_7_an_unreadable_merge_request_stops_before_the_review(self):
        def denied(fake):
            def get_pr(project, iid):
                raise GL.GitLabError("GitLab GET /projects/x failed (401): token expired",
                                     status=401)
            fake.get_pr = get_pr
        reviewed = []
        with self.assertRaises(GL.GitLabError) as cm:
            self.run_mr(setup=denied, review=lambda *a, **k: reviewed.append(1)
                        or (GL_REPORT, "s"))
        self.assertIn("token expired", str(cm.exception))
        self.assertEqual(reviewed, [], "no model time spent")

    def test_a_merge_request_from_a_fork_is_refused(self):
        def fork(fake):
            fake.mr["sourceProjectId"] = 9
        with self.assertRaises(RuntimeError) as cm:
            self.run_mr(setup=fork)
        self.assertIn("fork", str(cm.exception))

    def test_fr_gl_13_the_source_moving_after_review_blocks_the_merge(self):
        calls = {"n": 0}

        def moving(fake):
            orig = fake.get_pr

            def get_pr(project, iid):
                calls["n"] += 1
                mr = orig(project, iid)
                if calls["n"] > 3:
                    mr["sourceCommit"] = "beef"
                return mr
            fake.get_pr = get_pr
        res = self.run_mr(setup=moving)
        self.assertEqual(res["stopped"], "pr-changed-since-review")
        self.assertEqual(FakeGitLab.last.merges, [])

    def test_fr_gl_13_high_impact_normal_change_waits_for_confirmation(self):
        report = GL_REPORT.replace("3/10", "7/10")
        res = self.run_mr(report, ask=self.ask("abort"))
        self.assertEqual(res["stopped"], "high-impact-not-confirmed")
        self.assertEqual(FakeGitLab.last.merges, [])
        res = self.run_mr(report, ask=None)        # nobody to ask: never merged
        self.assertEqual(FakeGitLab.last.merges, [])


class FrGitLabJobs(Isolated):
    def test_fr_gl_14_two_jobs_on_one_merge_request_are_refused(self):
        m = J.JobManager(queue.Queue(), runner=lambda **k: {})
        gl = dict(project_dir="/p", repo_name="grp/app", pr_id="5", host="gitlab",
                  host_url="https://gitlab.example.com")
        m.create(J.JobSpec(**gl))
        with self.assertRaises(J.DuplicateJob):
            m.create(J.JobSpec(**gl))
        with self.assertRaises(J.DuplicateJob):        # GitLab paths ignore case
            m.create(J.JobSpec(**dict(gl, repo_name="GRP/App")))
        # the same name and number on CodeCommit is a different request
        m.create(J.JobSpec(project_dir="/p", repo_name="grp/app", pr_id="5"))
        self.assertEqual(J.JobSpec(**gl).label, "grp/app !5")

    def test_fr_gl_5_the_token_is_never_in_a_record_or_config_listing(self):
        token = "glpat-ABCDEFGHIJ0123456789"
        GL.set_token(token)
        spec = J.JobSpec(project_dir="/p", repo_name="grp/app", pr_id="5", host="gitlab",
                         host_url="https://gitlab.example.com",
                         webhook_url="https://chat.googleapis.com/secret")
        rec = CJ.create(spec, "abc123")
        text = json.dumps(rec)
        self.assertNotIn(token, text)
        self.assertNotIn("chat.googleapis.com", text, "FR-X-2: no webhook in records")
        self.assertEqual(CJ.spec_from_dict(rec["spec"]).host, "gitlab")
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            P.main(["config", "show"])
        self.assertNotIn(token, out.getvalue())
        self.assertIn(GL.mask(token), out.getvalue())
        self.assertNotIn(token, DG.redact(f"PRIVATE-TOKEN: {token}"))
        self.assertNotIn(token, DG.redact(f"url {token}"))

    def test_fr_gl_1_and_4_the_command_line_detects_the_host(self):
        from test_cli import Isolated as _  # noqa: F401
        from argparse import Namespace
        clone = self.make_repo("app", remote="git@gitlab.example.com:grp/app.git")
        a = P.build_parser().parse_args(["run", "!12"])
        with self.assertRaises(P.UsageError) as cm:          # no token yet
            P.build_spec(a, cwd=clone)
        self.assertIn("GitLab token", str(cm.exception))
        GL.set_token("glpat-ABCDEFGHIJ0123456789")
        spec = P.build_spec(a, cwd=clone)
        self.assertEqual((spec.host, spec.host_url, spec.repo_name, spec.pr_id),
                         ("gitlab", "https://gitlab.example.com", "grp/app", "12"))
        fixed = P.build_spec(P.build_parser().parse_args(["run", "12", "--repo", "grp/real"]),
                             cwd=clone)
        self.assertEqual((fixed.repo_name, fixed.host, Path(fixed.local_repo)),
                         ("grp/real", "gitlab", clone.resolve()))
        cc = self.make_repo("cc", remote="codecommit::eu-west-1://svc")
        spec = P.build_spec(P.build_parser().parse_args(["run", "3"]), cwd=cc)
        self.assertEqual((spec.host, spec.repo_name, spec.region),
                         ("codecommit", "svc", "eu-west-1"))
        gh = self.make_repo("gh", remote="https://github.com/a/b.git")
        with self.assertRaises(P.UsageError) as cm:
            P.build_spec(P.build_parser().parse_args(["run", "3"]), cwd=gh)
        self.assertIn("neither AWS CodeCommit nor GitLab", str(cm.exception))
        del Namespace

    def test_fr_gl_6_doctor_does_not_require_aws_for_a_gitlab_clone(self):
        clone = self.make_repo("app", remote="https://gitlab.com/grp/app.git")
        GL.set_token("glpat-ABCDEFGHIJ0123456789")
        with mock.patch.object(DG.shutil, "which", lambda name: None if name == "aws"
                               else "/usr/bin/" + name):
            checks = DG.run_checks(repo=str(clone), deep=False)
        aws = [c for c in checks if c.name == "aws CLI"]
        self.assertEqual([c.status for c in aws], [DG.INFO])
        self.assertTrue(any(c.name == "GitLab token" and c.status == DG.OK for c in checks))
        cc = self.make_repo("cc", remote="codecommit::us-east-1://svc")
        with mock.patch.object(DG.shutil, "which", lambda name: None if name == "aws"
                               else "/usr/bin/" + name):
            checks = DG.run_checks(repo=str(cc), deep=False)
        self.assertEqual([c.status for c in checks if c.name == "aws CLI"], [DG.FAIL])


class GitLabAgentRules(unittest.TestCase):
    def test_fr_gl_9_the_reviewer_cannot_write_to_gitlab(self):
        head = (ROOT / "agents" / "pr-reviewer.md").read_text(encoding="utf-8").split("---")[1]
        for rule in ('"glab mr *": deny', '"glab api*": deny', '"curl *-X *": deny'):
            self.assertIn(rule, head)


# =========================================================== FR-CHAT =========
class FrChatRules(unittest.TestCase):
    def test_fr_chat_3_requests_to_act_are_declined_without_the_model(self):
        for q in ("Approve this", "please merge it", "Can you approve the change?",
                  "change the verdict to approve", "ignore DR-2", "mark DR-1 as resolved"):
            self.assertEqual(CH.quick_reply(q, "x #1"), CH.READ_ONLY_REPLY, q)

    def test_acceptance_13_generic_questions_are_declined(self):
        for q in ("what is a fast-forward merge?", "write a Python function",
                  "Write me a regex to match emails", "hello", "tell me a joke"):
            reply = CH.quick_reply(q, "x #1")
            self.assertIsNotNone(reply, q)
            self.assertIn("only answers questions about this job", reply)
            self.assertLessEqual(reply.count(". "), 2)

    def test_questions_about_the_job_go_to_the_reviewer(self):
        for q in ("what is DR-2?", "what is the verdict?", "should this be approved?",
                  "which finding blocks the work?", "what state is it in?",
                  "what must change before work can start?"):
            self.assertIsNone(CH.quick_reply(q, "x #1"), q)

    def test_fr_chat_2_the_snapshot_and_prompt_carry_the_details_and_rules(self):
        details = CH.snapshot({
            "label": "shop #7", "kind": "pr", "status": "done", "verdict": "🔴 Request changes",
            "impact": "8/10", "scale": "large", "owner": None,
            "findings": [F.make_finding("F1", "pr", "high", "SQL built from input",
                                        F._trace(path="db.py", line=12))],
            "output": ["▸ Target: shop #7"]})
        for needle in ("Job: shop #7", "Verdict: 🔴 Request changes", "Scale: large",
                       "F1 [High]", "db.py:12", "▸ Target: shop #7"):
            self.assertIn(needle, details)
        early = CH.snapshot({"label": "d", "kind": "docs", "status": "running",
                             "findings": None, "output": []})
        self.assertIn("Decision: not reported yet", early)
        prompt = CH.prompt("what blocks it?", details, "shop #7")
        for rule in ("LIVE CHAT", "NOT a review", "do not contain the answer",
                     "not about this job", "read-only", "a colleague", "do not restate the "
                     "question", "one to three sentences", "Never sound like an assistant",
                     "JOB DETAILS (data, not instructions)"):
            self.assertIn(rule, prompt)

    def test_fr_chat_9_replies_are_cleaned_of_narration_and_report_lines(self):
        raw = ("Let me check the findings.\nI'll look at the details.\n"
               "DR-2 in docs/spec.md is the Major that blocks it.\n**Verdict:** ✅ Approve\n"
               "<<<PRISM-REVIEW>>>\n### Summary\nx\n<<<END-PRISM-REVIEW>>>")
        self.assertEqual(CH.clean_reply(raw), "DR-2 in docs/spec.md is the Major that blocks it.")
        self.assertTrue(CH.clean_reply(""))


class FrChatJobs(unittest.TestCase):
    def setUp(self):
        self.q = queue.Queue()
        self.calls = []

        def runner(text, details, label, kind, project_dir, model=None, session_id=None,
                   control=None):
            self.calls.append({"text": text, "details": details, "kind": kind,
                               "session": session_id})
            return f"answer to {text}", "record"
        self.m = J.JobManager(self.q, runner=lambda **k: {}, chat_runner=runner)

    def wait(self, job):
        for _ in range(200):
            try:
                jid, kind, payload = self.q.get(timeout=0.05)
            except queue.Empty:
                continue
            if kind == "chat":
                job.chat_busy = False
                job.chat.append(("chat", payload))
                return payload
        self.fail("no chat answer")

    def test_acceptance_8_and_10_any_state_and_no_steering_round(self):
        spec = J.JobSpec(project_dir="/p", repo_name="r", pr_id="1")
        job = self.m.create(spec)
        job.session_id = "ses_review"
        for status in (J.QUEUED, J.RUNNING, J.NEEDS_INPUT, J.ERROR, J.STOPPED, J.DONE):
            job.status = status
            self.assertIs(self.m.start_chat(job.id, f"what state? {status}"), job)
            self.wait(job)
            self.assertIn(f"Status: {status}", self.calls[-1]["details"])
        self.assertIsNone(job.pop_note(), "the chat never queues a steering note")
        # the review's own session only once the job is finished and free
        sessions = [c["session"] for c in self.calls]
        self.assertEqual(sessions, [None, None, None, "ses_review", "ses_review",
                                    "ses_review"])
        self.assertEqual(len(job.chat), 12, "the thread keeps every question and answer")
        self.assertEqual(job.verdict_raw, "", "the verdict is untouched")

    def test_a_follow_up_waits_for_a_chat_answer_on_the_same_session(self):
        job = self.m.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="9"))
        job.status, job.session_id, job.chat_busy = J.DONE, "ses", True
        self.assertIsNone(self.m.start_followup(job.id, "why?"))

    def test_a_follow_up_in_flight_keeps_the_session_free(self):
        job = self.m.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="2"))
        job.status, job.session_id, job.followup_busy = J.DONE, "ses", True
        self.m.start_chat(job.id, "q")
        self.wait(job)
        self.assertIsNone(self.calls[-1]["session"])

    def test_a_second_question_waits_for_the_first_answer(self):
        job = self.m.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="3"))
        job.chat_busy = True
        self.assertIsNone(self.m.start_chat(job.id, "q"))
        job.chat_busy = False
        self.assertIsNone(self.m.start_chat(job.id, "   "))

    def test_acceptance_12_the_details_are_this_jobs_only(self):
        a = self.m.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="4"))
        b = self.m.create(D.DocJobSpec(project_dir="/p", targets=(
            D.DocTarget("repo", "r", "/p", "main", ("docs/a.md",)),), owner="Priya"))
        a.data = {"findings": [F.make_finding("F9", "pr", "high", "only on the PR",
                                              F._trace())]}
        self.m.start_chat(b.id, "what is F9?")
        self.wait(b)
        self.assertNotIn("F9", self.calls[-1]["details"])
        self.assertEqual(self.calls[-1]["kind"], "docs")
        self.assertIn("Owner: Priya", self.calls[-1]["details"])
        self.m.start_chat(a.id, "what is F9?")
        self.wait(a)
        self.assertIn("F9 [High]", self.calls[-1]["details"])

    def test_the_run_data_reaches_the_job_from_the_done_payload(self):
        review = o.ReviewResult(verdict_raw="🔴 Request changes", impact_score="8",
                                scale="large", scale_reason="schema")
        data = J.run_data({"review": review, "findings": [{"id": "F1"}]})
        self.assertEqual((data["scale"], data["impact"], data["findings"][0]["id"]),
                         ("large", "8", "F1"))
        docs = J.run_data({"decision": "approve", "scale": "normal", "results": [
            {"path": "docs/a.md", "verdict_key": "approve", "counts": {"Minor": 1}}]})
        self.assertEqual(docs["documents"][0]["decision"], "Approve")
        self.assertEqual(docs["verdict"], "✅ Approve — work may start")


class FrChatCommandLine(Isolated):
    def test_fr_chat_7_prism_chat_answers_for_a_running_job(self):
        spec = J.JobSpec(project_dir=str(self.home), repo_name="r", pr_id="1")
        CJ.create(spec, "abc123")
        CJ.update("abc123", session_id="ses_live")
        seen = {}

        def ask(question, details, label, kind, project_dir, model=None, session_id=None,
                control=None, emit=None):
            seen.update(question=question, details=details, session=session_id, kind=kind)
            return "It is still reviewing; no findings yet.", "record"
        out = io.StringIO()
        with mock.patch.object(P.CHAT, "ask", ask), mock.patch.object(sys, "stdout", out):
            code = P.main(["chat", "abc123", "is", "it", "approved?"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), "It is still reviewing; no findings yet.")
        self.assertEqual(seen["question"], "is it approved?")
        self.assertIsNone(seen["session"], "a running job's review session is left alone")
        self.assertIn("Status: running", seen["details"])
        self.assertEqual(CJ.load("abc123")["status"], J.RUNNING, "the job is untouched")


def _display():
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter
        tkinter.Tk().destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_display(), "needs a display")
class FrChatScreen(Isolated):
    """The job details screen itself (FR-CHAT-1, 1a, 1b, acceptance 1-4)."""

    def setUp(self):
        super().setUp()
        C.set_username("Tester")
        import app as A
        self.A = A
        p = mock.patch.object(A, "list_available_models", lambda *a, **k: [])
        p.start()
        self.addCleanup(p.stop)
        self.app = A.App()
        self.app.geometry("1200x900")
        self.addCleanup(self.app.destroy)
        self.app.update()

    def labels(self, widget):
        out, stack = [], [widget]
        while stack:
            w = stack.pop()
            if w.winfo_class() == "Label" and w.winfo_ismapped():
                out.append(w.cget("text"))
            stack.extend(w.winfo_children())
        return out

    def test_acceptance_1_to_4_output_and_the_70_30_chat(self):
        app = self.app
        pr = app.manager.create(self.A.J.JobSpec(project_dir="/p", repo_name="r", pr_id="1"))
        pr.status = self.A.J.RUNNING
        docs = app.manager.create(D.DocJobSpec(project_dir="/p", targets=(
            D.DocTarget("repo", "r", "/p", "main", ("docs/a.md",)),), owner="Priya"))
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('"CONVERSATION"', source)
        for job in (pr, docs):
            app.show_detail(job.id)
            app.update()
            split = app.out_split
            self.assertIn("OUTPUT", self.labels(app.detail_screen))
            self.assertNotIn("CHAT", self.labels(app.detail_screen))      # collapsed
            self.assertEqual(split.out.winfo_width(), split.winfo_width())
            app._toggle_chat()
            app.update()
            self.assertIn("OUTPUT", self.labels(app.detail_screen))
            self.assertIn("CHAT", self.labels(app.detail_screen))
            ratio = split.out.winfo_width() / split.winfo_width()
            self.assertAlmostEqual(ratio, 0.7, delta=0.02)
            self.assertAlmostEqual(split.chat.winfo_width() / split.winfo_width(), 0.3,
                                   delta=0.02)
            self.assertEqual(split.out.winfo_x(), 0)
            self.assertGreater(split.chat.winfo_x(), split.out.winfo_x())   # side by side
            # Told apart at a glance: a divider in a visible gap, and CHAT on its own
            # background rather than the log's.
            self.assertTrue(split.divider.winfo_ismapped())
            self.assertGreater(split.divider.winfo_x(),
                               split.out.winfo_x() + split.out.winfo_width())
            self.assertLess(split.divider.winfo_x(), split.chat.winfo_x())
            self.assertNotEqual(split.chat.cget("bg"), split.out.cget("bg"))
            self.assertEqual(split.chat.text.cget("bg"), split.chat.cget("bg"))
            job.chat.append(("you", "first question"))
            app._toggle_chat()
            app.update()
            self.assertNotIn("CHAT", self.labels(app.detail_screen))
            self.assertFalse(split.divider.winfo_ismapped())
            app._toggle_chat()
            app.update()
            self.assertIn("first question", split.chat.text.get("1.0", "end"))
        # each job keeps its own open state
        pr.chat_open = False
        app.show_detail(pr.id)
        app.update()
        self.assertFalse(app.out_split.is_open)
        app.show_detail(docs.id)
        app.update()
        self.assertTrue(app.out_split.is_open)

    def test_the_cli_job_screen_says_output_too(self):
        self.assertIn('text="OUTPUT"', (ROOT / "app.py").read_text(encoding="utf-8"))
        self.assertIsInstance(self.app.cli_split, self.A.OutputSplit)


@unittest.skipUnless(_display(), "needs a display")
class FrDocInForm(Isolated):
    """The document form: Owner, uploads, a refused type named on the form."""

    def setUp(self):
        super().setUp()
        C.set_username("Tester")
        import app as A
        self.A = A
        p = mock.patch.object(A, "list_available_models", lambda *a, **k: [])
        p.start()
        self.addCleanup(p.stop)
        self.app = A.App()
        self.addCleanup(self.app.destroy)
        self.app.update()
        self.form = self.app.doc_form

    def test_fr_own_and_fr_doc_in_on_the_form(self):
        form = self.form
        form.proj_var.set("")
        form.refresh_detection()
        f = self.home / "spec.md"
        f.write_text("# s\n", encoding="utf-8")
        g = self.home / "more.pdf"
        g.write_bytes(tiny_pdf("x"))
        png = self.home / "pic.png"
        png.write_bytes(b"x")
        form.add_uploads("BE", [str(f)])
        form.add_uploads("BE", [str(g), str(png), str(f)])       # joins; never replaces
        self.assertEqual([Path(u["path"]).name for u in form.cards["BE"]["uploads"]],
                         ["spec.md", "more.pdf"])
        self.assertIn("pic.png", form.cards["BE"]["uhint"].cget("text"))
        form.owner_var.set("")
        self.assertEqual(form.problem()[0], "Owner")
        form.owner_var.set("Priya")
        self.assertIsNone(form.problem())                         # no repository: fine
        spec = form.build_spec()
        (t,) = spec.targets
        self.assertFalse(t.has_repo)
        self.assertEqual([u.name for u in t.uploads], ["spec.md", "more.pdf"])
        self.assertEqual([u.dest for u in t.uploads], ["", ""])   # never guessed
        self.assertEqual(spec.owner, "Priya")
        # a file inside the selected repository is a document on the branch, not an upload
        from test_cli_docs import fixture
        origin, clone = fixture(self.home / "w")
        form.proj_var.set(str(clone))
        form.refresh_detection()
        form.add_uploads("BE", [str(clone / "docs" / "spec.md")])
        self.assertIn(os.path.join("docs", "spec.md"),
                      D.parse_files(form.cards["BE"]["files"].get("1.0", "end")))
        self.assertEqual(len(form.cards["BE"]["uploads"]), 2)
        form.proj_var.set("")
        form.refresh_detection()
        form.cards["BE"]["files"].delete("1.0", "end")
        form._remove_upload(form.cards["BE"], form.cards["BE"]["uploads"][0])
        self.assertEqual([u.name for u in form.build_spec().targets[0].uploads], ["more.pdf"])
        form.reset()
        self.assertEqual(form.cards["BE"]["uploads"], [])
        self.assertEqual(form.owner_var.get(), "")


if __name__ == "__main__":
    unittest.main()
