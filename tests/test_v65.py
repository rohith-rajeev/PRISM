"""v6.5: who is reviewing, document-review alerts, and findings as data.

* the reviewer's name - stored once, cleaned, written onto the PR description, the chat
  cards and the comments in a reviewed document;
* the document-review webhook and the card it posts;
* `prism jobs --json` carrying a `findings` array whose every record has the same keys.

Everything runs offline: a canned reviewer, a real local git repository for the documents,
and a stand-in for the HTTP call.
"""
import importlib
import io
import json
import os
import queue
import sys
import tempfile
import unittest
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

import cli_jobs as CJ  # noqa: E402
import config as C  # noqa: E402
import docreview as D  # noqa: E402
import findings as F  # noqa: E402
import jobs as J  # noqa: E402
import notifier as N  # noqa: E402
import orchestrator as o  # noqa: E402
import prism_cli as P  # noqa: E402
from test_cli import Isolated  # noqa: E402
from test_cli_docs import fixture, sh  # noqa: E402

PR_REPORT = (
    "## PR #7\n**Verdict:** ⚠️ Approve with comments\n"
    "**Impact score:** 4/10 - moderate\n\n### Findings\n"
    "- **[High] Security — `src/auth.py:42-50`** token is logged in clear. Same pattern also "
    "at: src/b.py:3\n"
    "- **[Low] Tests — `tests/test_a.py:9`** no case for an empty list (late find — missed in PR #3)\n"
    "- **[Nit] Naming** `x` is unclear\n\n### Summary\nOk.\nCoverage: all.\n")

DOC_REPORT = """**Verdict:** ⚠️ Approve with comments
**Impact score:** 4/10 — small

<<<PRISM-REVIEW>>>
### Summary
- **Document:** a spec

### Major
- **[Major] DR-{n}** · Pattern · 2. Design — diverges from how the codebase does it.
  Evidence: src/x.py. Suggested fix: reuse it.

### Minor
- **[Minor] DR-{m}** · Ambiguity · 3. Limits — vague.

### Coverage
- **Security:** nothing found.
- **Stability:** nothing found.
- **Scalability:** nothing found.
<<<END-PRISM-REVIEW>>>
"""

# v7.0 added "scale" (FR-X-6); every record still has every key.
FINDING_KEYS = ("id", "kind", "category", "type", "description", "late_find", "persona",
                "scale", "traceability")
TRACE_KEYS = ("repo", "pull_request", "branch", "target_branch", "commit", "path", "line",
              "line_end", "section", "role")


class Capture:
    """Stands in for urllib's urlopen and keeps the JSON bodies it was given."""

    def __init__(self):
        self.bodies, self.urls = [], []

    def __call__(self, req, timeout=None, context=None):
        self.urls.append(req.full_url)
        self.bodies.append(json.loads(req.data.decode("utf-8")))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TTY(io.StringIO):
    """A stdout that claims to be a terminal."""

    def isatty(self):
        return True


def card_texts(body):
    """{topLabel: text} for every labelled fact on a card (columns included), plus the
    header subtitle as "_subtitle" and a document card's footer line as "_footer"."""
    card = body["cardsV2"][0]["card"]
    out = {"_subtitle": card.get("header", {}).get("subtitle", "")}

    def walk(widgets):
        for w in widgets:
            if "decoratedText" in w and w["decoratedText"].get("topLabel"):
                out[w["decoratedText"]["topLabel"]] = w["decoratedText"]["text"]
            elif "columns" in w:
                for col in w["columns"]["columnItems"]:
                    walk(col["widgets"])
            elif "textParagraph" in w:
                out["_footer"] = w["textParagraph"]["text"]
    for section in card["sections"]:
        walk(section["widgets"])
    return out


# ---------------------------------------------------------------- the name --
class UsernameConfig(Isolated):
    def test_nothing_saved_reads_as_empty_and_falls_back_to_the_machine_login(self):
        self.assertEqual(C.get_username(), "")
        with mock.patch("getpass.getuser", return_value="jdoe"):
            self.assertEqual(C.effective_username(), "jdoe")

    def test_a_saved_name_wins_and_other_settings_survive_it(self):
        C.set_webhook_url("https://chat.googleapis.com/a")
        C.set_cli(region="eu-west-1")
        C.set_username("  Alex   Rivera ")
        self.assertEqual(C.get_username(), "Alex Rivera")
        self.assertEqual(C.effective_username(), "Alex Rivera")
        self.assertEqual(C.get_webhook_url(), "https://chat.googleapis.com/a")
        self.assertEqual(C.get_cli()["region"], "eu-west-1")

    def test_it_is_kept_in_the_settings_file_so_an_upgrade_never_loses_it(self):
        C.set_username("Alex")
        self.assertEqual(json.loads(C.CONFIG_PATH.read_text(encoding="utf-8"))["username"], "Alex")
        C.set_codegen(enabled=True)               # later writes of other keys keep it
        C.set_docs_webhook_url("https://chat.googleapis.com/d")
        self.assertEqual(C.get_username(), "Alex")

    def test_markup_and_control_characters_cannot_reach_the_places_it_is_written(self):
        # It lands in a Markdown description and inside an HTML comment in a document.
        for hostile in ("a<!-- prism:end -->b", "x\nPRISM review — Approve", "`rm`", "a>b"):
            name = C.clean_username(hostile)
            for bad in ("<", ">", "`", "\n", "\r"):
                self.assertNotIn(bad, name, hostile)
        self.assertEqual(C.clean_username("a\x00b\tc"), "a b c")
        self.assertLessEqual(len(C.clean_username("x" * 500)), C.MAX_USERNAME)
        self.assertEqual(C.clean_username(None), "")
        self.assertEqual(C.clean_username(42), "")

    def test_a_hand_edited_file_with_a_bad_value_reads_as_not_set(self):
        C.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        C.CONFIG_PATH.write_text(json.dumps({"username": ["x"]}), encoding="utf-8")
        self.assertEqual(C.get_username(), "")

    def test_the_two_webhooks_are_independent(self):
        C.set_webhook_url("https://chat.googleapis.com/pr")
        self.assertEqual(C.get_docs_webhook_url(), "")
        C.set_docs_webhook_url(" https://chat.googleapis.com/docs ")
        self.assertEqual(C.get_docs_webhook_url(), "https://chat.googleapis.com/docs")
        self.assertEqual(C.get_webhook_url(), "https://chat.googleapis.com/pr")
        C.set_docs_webhook_url("")
        self.assertEqual(C.get_webhook_url(), "https://chat.googleapis.com/pr")


class CommandLineName(Isolated):
    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = P.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_config_sets_shows_and_unsets_the_name_and_the_docs_webhook(self):
        self.assertEqual(self.run_cmd("config", "set", "username", "Alex", "R")[0], 0)
        self.assertEqual(self.run_cmd("config", "set", "docs-webhook",
                                      "https://chat.googleapis.com/v1/spaces/S/messages?key=k")[0], 0)
        self.assertEqual((C.get_username(), bool(C.get_docs_webhook_url())), ("Alex R", True))
        _c, out, _e = self.run_cmd("config")
        self.assertIn("Alex R", out)
        self.assertIn("docs_webhook", out)
        self.assertNotIn("key=k", out, "a webhook URL is shown masked")
        self.run_cmd("config", "unset", "username")
        self.run_cmd("config", "unset", "docs_webhook")
        self.assertEqual((C.get_username(), C.get_docs_webhook_url()), ("", ""))

    def test_a_name_that_is_nothing_once_cleaned_is_refused(self):
        code, _o, err = self.run_cmd("config", "set", "username", "<>")
        self.assertEqual(code, 2)
        self.assertEqual(C.get_username(), "")

    def test_a_terminal_is_asked_once_and_enter_takes_the_suggestion(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("getpass.getuser", return_value="jdoe"), \
                mock.patch("builtins.input", return_value="") as ask, \
                mock.patch.object(sys, "stdout", TTY()):
            P.ensure_username()
            self.assertEqual(C.get_username(), "jdoe")
            P.ensure_username()                       # saved now: never asked again
        self.assertEqual(ask.call_count, 1)

    def test_nobody_at_the_keyboard_means_no_question_and_nothing_saved(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=False), \
                mock.patch("builtins.input", side_effect=AssertionError("asked")):
            P.ensure_username()
        self.assertEqual(C.get_username(), "")

    def test_an_interrupted_answer_saves_nothing(self):
        with mock.patch.object(sys.stdin, "isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=EOFError), \
                mock.patch.object(sys, "stdout", TTY()):
            P.ensure_username()
        self.assertEqual(C.get_username(), "")


# ---------------------------------------------------------------- findings --
class FindingRecords(unittest.TestCase):
    def pr(self, **kw):
        lines = [ln for ln in PR_REPORT.splitlines() if o.FINDING_RE.match(ln)]
        return F.parse_pr_findings(lines, repo="svc", pull_request="7", branch="feat",
                                   target_branch="main", commit="abc123", **kw)

    def test_every_record_has_exactly_the_same_keys(self):
        for f in self.pr() + F.parse_doc_findings(DOC_REPORT.format(n=1, m=2), path="docs/a.md"):
            self.assertEqual(tuple(f), FINDING_KEYS)
            self.assertEqual(tuple(f["traceability"]), TRACE_KEYS)
            json.dumps(f)

    def test_a_pull_request_finding_is_read_in_full(self):
        high, low, nit = self.pr()
        self.assertEqual((high["id"], high["kind"], high["category"], high["type"]),
                         ("F1", "pr", "high", "Security"))
        self.assertIn("token is logged in clear", high["description"])
        t = high["traceability"]
        self.assertEqual((t["repo"], t["pull_request"], t["branch"], t["target_branch"],
                          t["commit"]), ("svc", "7", "feat", "main", "abc123"))
        self.assertEqual((t["path"], t["line"], t["line_end"]), ("src/auth.py", 42, 50))
        self.assertEqual((t["section"], t["role"]), (None, None))
        self.assertFalse(high["late_find"])
        self.assertTrue(low["late_find"])
        self.assertEqual((low["category"], low["traceability"]["path"],
                          low["traceability"]["line"], low["traceability"]["line_end"]),
                         ("low", "tests/test_a.py", 9, None))

    def test_a_finding_without_a_location_still_becomes_a_record(self):
        nit = self.pr()[2]
        self.assertEqual(nit["category"], "nit")
        self.assertEqual((nit["traceability"]["path"], nit["traceability"]["line"]), (None, None))
        self.assertEqual(nit["traceability"]["repo"], "svc")

    def test_a_code_identifier_in_the_description_is_not_mistaken_for_a_path(self):
        lines = ["- **[Low] Style** rename `foo()` and `x` and `a b`",
                 "- **[Low] Style** see `src/util.py:12` for the helper",
                 "- **[Low] Build** the `Makefile` is missing a target"]
        a, b, c = F.parse_pr_findings(lines)
        self.assertIsNone(a["traceability"]["path"])
        self.assertEqual((b["traceability"]["path"], b["traceability"]["line"]),
                         ("src/util.py", 12))
        self.assertIsNone(c["traceability"]["path"], "no slash or extension: could be anything")

    def test_a_windows_path_keeps_its_drive_letter(self):
        (f,) = F.parse_pr_findings(["- **[Medium] Bug — `C:\\src\\a.py:7`** broken"])
        self.assertEqual((f["traceability"]["path"], f["traceability"]["line"]),
                         ("C:\\src\\a.py", 7))

    def test_all_five_pull_request_severities_are_recognised(self):
        lines = [f"- **[{s}] x — `a.py:1`** text" for s in
                 ("Critical", "High", "Medium", "Low", "Nit")]
        self.assertEqual([f["category"] for f in F.parse_pr_findings(lines)],
                         list(F.PR_CATEGORIES))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(F.parse_pr_findings([]), [])
        self.assertEqual(F.parse_pr_findings(None), [])
        self.assertEqual(F.parse_doc_findings(""), [])
        self.assertEqual(F.parse_doc_findings(None), [])

    def test_a_document_comment_carries_its_section_and_wrapped_text(self):
        body = DOC_REPORT.format(n=3, m=4)
        major, minor = F.parse_doc_findings(
            D._without_previous_block(body), path="docs/spec.md", repo="be", branch="feat/x",
            commit="c0ffee", role="BE", persona="architect")
        self.assertEqual((major["id"], major["category"], major["type"], major["kind"]),
                         ("DR-3", "major", "Pattern", "document"))
        self.assertEqual(major["traceability"]["section"], "2. Design")
        self.assertIn("Evidence: src/x.py", major["description"], "a wrapped comment is one record")
        self.assertEqual((minor["id"], minor["category"], minor["traceability"]["section"]),
                         ("DR-4", "minor", "3. Limits"))
        t = major["traceability"]
        self.assertEqual((t["repo"], t["branch"], t["commit"], t["path"], t["role"],
                          t["pull_request"], t["line"]),
                         ("be", "feat/x", "c0ffee", "docs/spec.md", "BE", None, None))
        self.assertEqual(major["persona"], "architect")

    def test_the_previous_comments_status_list_is_not_counted_as_findings(self):
        body = ("### Previous comments\n- **[Major] DR-1** · Gap · S — still open\n\n"
                "### Major\n- **[Major] DR-2** · Gap · S — new\n")
        ids = [f["id"] for f in F.parse_doc_findings(D._without_previous_block(body))]
        self.assertEqual(ids, ["DR-2"])
        # and it is exactly what the review's own counts are taken from
        self.assertEqual(len(ids), sum(D.severity_counts(body).values()))

    def test_the_four_document_severities_are_recognised(self):
        body = "\n".join(f"- **[{s}] DR-{i}** · Gap · S — text"
                         for i, s in enumerate(("Blocker", "Major", "Minor", "Suggestion"), 1))
        self.assertEqual([f["category"] for f in F.parse_doc_findings(body)],
                         list(F.DOC_CATEGORIES))


# ------------------------------------------------------------ chat cards --
class PrCard(unittest.TestCase):
    def test_the_reviewer_is_named_on_the_card_only_when_known(self):
        with_name = N.build_card("repo", "1", True, verdict_raw="Approve", verdict_key="approve",
                                 merged=True, reviewer="Alex")
        self.assertEqual(card_texts({"cardsV2": [{"card": with_name}]})["Reviewed by"], "Alex")
        without = N.build_card("repo", "1", True, verdict_raw="Approve", verdict_key="approve",
                               merged=True)
        self.assertNotIn("Reviewed by", card_texts({"cardsV2": [{"card": without}]}))


class DocCard(unittest.TestCase):
    """The v6.5 facts on the v7.0 card (FR-CARD): who reviewed, the decision, the counts,
    which branches and documents, escaped text, a bounded list."""
    TARGETS = [{"label": "Backend", "role": "BE", "repo": "shop-be", "branch": "feat/x",
                "documents": [{"file": "spec.md", "folder": "docs",
                               "verdict_key": "request-changes"}]},
               {"label": "Frontend", "role": "FE", "repo": "shop-fe", "branch": "feat/y",
                "documents": [{"file": "ui.md", "folder": "docs",
                               "verdict_key": "request-changes"}]}]

    def card(self, **kw):
        base = dict(persona="Architect", owner="Priya", decision="request-changes", impact="6",
                    counts={"Blocker": 1, "Major": 1}, targets=self.TARGETS, reviewer="Alex",
                    run_id="20261124-1200-abc123", outcome="committed and pushed")
        base.update(kw)
        return {"cardsV2": [{"card": N.build_doc_card(**base)}]}

    def test_it_says_who_reviewed_what_where_and_with_what_result(self):
        body = self.card()
        t = card_texts(body)
        self.assertEqual(t["Reviewer role"], "Architect")
        self.assertEqual(t["Reviewed by"], "Alex")
        self.assertEqual(t["Owner"], "Priya")
        self.assertEqual(t["_subtitle"], "Request changes — work may not start · Owner: Priya")
        self.assertEqual(t["Impact"], "6/10")
        self.assertEqual(t["Comments"], "1 blocker, 1 major")
        self.assertIn("committed and pushed", t["_footer"])
        self.assertIn("20261124-1200-abc123", t["_footer"])
        text = json.dumps(body, ensure_ascii=False)
        for expected in ("Backend · shop-be · feat/x", "Frontend · shop-fe · feat/y",
                         "spec.md — Request changes", "ui.md — Request changes"):
            self.assertIn(expected, text)
        self.assertEqual(body["cardsV2"][0]["card"]["header"]["title"], "Document review")

    def test_a_single_repository_is_not_labelled_twice(self):
        one = [{"label": "clone", "role": "repo", "repo": "clone", "branch": "main",
                "documents": [{"file": "a.md", "folder": "", "verdict_key": "approve"}]}]
        text = json.dumps(self.card(targets=one), ensure_ascii=False)
        self.assertIn("clone · main", text)
        self.assertNotIn("clone · clone", text)
        self.assertNotIn("epository", text)

    def test_without_a_name_there_is_no_reviewed_by_row(self):
        self.assertNotIn("Reviewed by", card_texts(self.card(reviewer=None)))

    def test_file_names_cannot_inject_markup(self):
        t = [{"label": "x", "role": "repo", "repo": "x", "branch": "b", "documents": [
            {"file": "<b>evil</b>.md", "folder": "", "verdict_key": "approve"}]}]
        text = json.dumps(self.card(targets=t))
        self.assertNotIn("<b>evil", text)
        self.assertIn("&lt;b&gt;evil", text)

    def test_a_long_list_is_cut_with_a_count_rather_than_run_off_the_screen(self):
        many = [{"file": f"{i}.md", "folder": "docs", "verdict_key": "approve"}
                for i in range(25)]
        body = self.card(targets=[{"label": "x", "role": "repo", "repo": "x", "branch": "b",
                                   "documents": many}])
        text = json.dumps(body, ensure_ascii=False)
        self.assertIn(f"and {25 - N.DOC_LINES_PER_REPO} more", text)
        self.assertNotIn("24.md", text)

    def test_documents_that_could_not_be_reviewed_are_called_out(self):
        self.assertIn("2 documents could not be reviewed",
                      card_texts(self.card(not_reviewed=2))["_footer"])

    def test_posting_needs_a_webhook_and_never_raises(self):
        cap = Capture()
        with mock.patch.object(N.urllib.request, "urlopen", cap):
            self.assertFalse(N.post_doc_summary(None, persona="QA"))
            self.assertFalse(N.post_doc_summary("", persona="QA"))
            self.assertEqual(cap.bodies, [])
            self.assertTrue(N.post_doc_summary("https://chat.googleapis.com/x", persona="QA"))
            self.assertEqual(cap.bodies[0]["cardsV2"][0]["cardId"], "prism-doc-summary")
        with mock.patch.object(N.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertFalse(N.post_doc_summary("https://chat.googleapis.com/x", persona="QA"))
        lines = []
        self.assertFalse(N.post_doc_summary("https://chat.googleapis.com/x",
                                            emit=lines.append, nonsense=1))
        self.assertTrue(lines and "Could not build" in lines[0])


# ---------------------------------------------------- PR description / run --
class PrDescription(unittest.TestCase):
    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self.written = []
        o.get_pr = lambda *a, **k: {"description": "Author text."}
        o.aws_cli = lambda *args, **k: self.written.append(args)

    def write(self, **kw):
        return o.update_description_direct("7", "⚠️ Approve with comments", "4",
                                           ["- **[Low] x — `a.py:1`** y"], **kw)

    def test_the_description_names_who_ran_the_review(self):
        text = self.write(reviewer="Alex Rivera")
        self.assertIn("**PRISM review — ⚠️ Approve with comments (impact 4/10)**\n"
                      "Reviewed by: Alex Rivera\n", text)
        self.assertIn("Author text.", text)

    def test_without_a_name_the_description_is_exactly_what_it_was_before(self):
        self.assertNotIn("Reviewed by", self.write())
        self.assertNotIn("Reviewed by", self.write(reviewer="   "))

    def test_a_hostile_name_cannot_close_the_block_or_start_a_line(self):
        text = self.write(reviewer="a -->\n<!-- prism:end -->\n# owned")
        self.assertEqual(text.count("<!-- prism:end -->"), 1)
        (line,) = [ln for ln in text.splitlines() if ln.startswith("Reviewed by:")]
        self.assertNotIn("<", line)

    def test_the_block_still_fits_the_description_limit_with_the_name_in_it(self):
        o.get_pr = lambda *a, **k: {"description": "x" * 9000}
        findings = [f"- **[Low] x — `a.py:1`** {'y' * 400}" for _ in range(20)]
        text = o.update_description_direct("7", "Approve", "1", findings, reviewer="Alex")
        self.assertLessEqual(len(text), o.PR_DESCRIPTION_MAX)
        self.assertTrue(text.endswith("<!-- prism:end -->"))
        self.assertIn("Reviewed by: Alex", text)


class PrPipeline(unittest.TestCase):
    """full_pipeline: the name reaches the description and the card; the findings reach the result."""

    def setUp(self):
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clone = self._tmp.name
        o._REVIEWED_STORE_PATH = Path(self.clone) / "reviewed_commits.json"
        self.described, self.posted = [], []
        o.ensure_bundled_agents = lambda *a, **k: ["pr-reviewer"]
        o.resolve_repo = lambda r, p, l: (r, self.clone)
        o.run_opencode_review = lambda *a, **k: (PR_REPORT, "ses_x")
        o.check_ff_mergeable = lambda *a, **k: (True, "ok")
        o.try_fast_forward_merge = lambda *a, **k: {}
        o.update_description_direct = lambda *a, **k: self.described.append(k) or ""
        o.get_pr = lambda *a, **k: {
            "status": "OPEN", "repositoryName": "repo", "destinationReference": "main",
            "sourceReference": "feat/x", "sourceCommit": "9f2c" * 10, "description": "",
            "authorArn": "arn:aws:iam::1:user/dev"}
        self._post = o.notifier.post_summary
        self.addCleanup(setattr, o.notifier, "post_summary", self._post)
        o.notifier.post_summary = lambda url, **kw: self.posted.append(kw) or True

    def run_(self, **kw):
        return o.full_pipeline(self.clone, "repo", "7", local_repo=self.clone,
                               emit=lambda *a, **k: None, do_merge=False, **kw)

    def test_the_findings_come_back_as_records_with_where_they_are(self):
        res = self.run_()
        self.assertEqual([f["category"] for f in res["findings"]], ["high", "low", "nit"])
        t = res["findings"][0]["traceability"]
        self.assertEqual((t["repo"], t["pull_request"], t["branch"], t["target_branch"],
                          t["commit"], t["path"], t["line"]),
                         ("repo", "7", "feat/x", "main", "9f2c" * 10, "src/auth.py", 42))
        self.assertTrue(res["findings"][1]["late_find"])
        json.dumps(res["findings"])

    def test_a_run_with_no_review_has_an_empty_list_not_a_missing_key(self):
        res = self.run_(do_review=False, ask=lambda *a, **k: "proceed")
        self.assertEqual(res["findings"], [])

    def test_the_name_is_passed_to_the_description_and_the_card(self):
        self.run_(reviewer="Alex", webhook_url="https://chat.googleapis.com/x")
        self.assertEqual(self.described[0]["reviewer"], "Alex")
        self.assertEqual(self.posted[0]["reviewer"], "Alex")

    def test_no_name_means_none_is_passed(self):
        self.run_(webhook_url="https://chat.googleapis.com/x")
        self.assertEqual(self.described[0]["reviewer"], "")
        self.assertIsNone(self.posted[0]["reviewer"])

    def test_the_findings_are_data_not_transcript(self):
        """A job's "finished: {...}" line and the UI payload must not carry the list."""
        out = queue.Queue()
        mgr = J.JobManager(out, runner=lambda **kw: {"merged": False, "findings": [{"a": 1}] * 50,
                                                     "results": [1], "stopped": "x",
                                                     "tokens": None})
        job = mgr.create(J.JobSpec(project_dir=self.clone, repo_name="r", pr_id="1"))
        mgr._work(job)
        events = {}
        while not out.empty():
            _id, kind, payload = out.get()
            events[kind] = payload
        self.assertEqual(events["done"]["stopped"], "x")
        self.assertNotIn("findings", events["done"])
        self.assertNotIn("results", events["done"])

    def test_the_jobspec_hands_the_name_to_the_pipeline(self):
        spec = J.JobSpec(project_dir="/p", repo_name="r", pr_id="1", reviewer="Alex")
        self.assertEqual(spec.pipeline_kwargs()["reviewer"], "Alex")
        self.assertEqual(J.JobSpec(project_dir="/p", repo_name="r", pr_id="1")
                         .pipeline_kwargs()["reviewer"], "")


# ------------------------------------------------------ documents, end to end --
class DocumentReviewEndToEnd(Isolated):
    def setUp(self):
        super().setUp()
        self.origin, self.clone = fixture(self.home / "one")
        saved = (o.ensure_bundled_agents, D._run_agent)
        o.ensure_bundled_agents = lambda *a, **k: ["doc-reviewer"]

        def agent(prompt, project_dir, emit, model, control, meter):
            import re
            n = int(re.search(r"Start new comments at DR-(\d+)", prompt).group(1))
            return 0, DOC_REPORT.format(n=n, m=n + 1), "s"
        D._run_agent = agent
        self.addCleanup(lambda: (setattr(o, "ensure_bundled_agents", saved[0]),
                                 setattr(D, "_run_agent", saved[1])))

    def run_docs(self, *extra):
        old = os.getcwd()
        os.chdir(str(self.clone))
        out = io.StringIO()
        try:
            with mock.patch.object(sys, "stdout", out), \
                    mock.patch.object(sys.stdin, "isatty", return_value=False):
                code = P.main(["docs", "docs/spec.md", "--persona", "architect", "--owner", "Priya", *extra])
        finally:
            os.chdir(old)
        return code, out.getvalue()

    def jobs_json(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            P.main(["jobs", "--all", "--json"])
        return json.loads(out.getvalue())

    def test_the_review_is_signed_alerted_and_its_findings_are_in_the_record(self):
        C.set_username("Alex Rivera")
        C.set_docs_webhook_url("https://chat.googleapis.com/v1/spaces/D/messages?key=k")
        C.set_webhook_url("https://chat.googleapis.com/v1/spaces/PR/messages?key=k")
        cap = Capture()
        with mock.patch.object(N.urllib.request, "urlopen", cap):
            code, out = self.run_docs()
        self.assertEqual(code, 0, out)

        # signed in the document itself
        doc = sh(self.origin, "show", "feat/x:docs/spec.md")
        self.assertIn("Review v1 · Architect · owner Priya · by Alex Rivera · run `", doc)
        self.assertIn('"reviewer":"Alex Rivera"', doc)

        # one alert, to the DOCUMENT webhook only
        self.assertEqual(cap.urls, ["https://chat.googleapis.com/v1/spaces/D/messages?key=k"])
        t = card_texts(cap.bodies[0])
        self.assertEqual(t["Reviewer role"], "Architect")
        self.assertEqual(t["Reviewed by"], "Alex Rivera")
        # v7.0: a Major is open, so the decision is Request changes (FR-VER-4).
        self.assertTrue(t["_subtitle"].startswith("Request changes — work may not start"))
        self.assertEqual(t["Owner"], "Priya")
        self.assertTrue(t["Impact"].startswith("4/10"))
        self.assertEqual(t["Comments"], "1 major, 1 minor")
        self.assertIn("committed and pushed", t["_footer"])
        text = json.dumps(cap.bodies[0], ensure_ascii=False)
        self.assertIn("feat/x", text)
        self.assertIn("spec.md — Request changes", text)
        self.assertIn("Posted document review summary to Google Chat.", out)

        # and the record carries the findings
        (rec,) = self.jobs_json()
        self.assertEqual(rec["reviewer"], "Alex Rivera")
        self.assertEqual([(f["id"], f["category"]) for f in rec["findings"]],
                         [("DR-1", "major"), ("DR-2", "minor")])
        first = rec["findings"][0]
        self.assertEqual(tuple(first), FINDING_KEYS)
        self.assertEqual(first["persona"], "architect")
        self.assertEqual(first["traceability"]["path"], "docs/spec.md")
        self.assertEqual(first["traceability"]["section"], "2. Design")
        self.assertEqual(first["traceability"]["branch"], "feat/x")
        self.assertEqual(first["traceability"]["repo"], "clone")
        self.assertEqual(first["traceability"]["role"], "repo")
        self.assertRegex(first["traceability"]["commit"], r"^[0-9a-f]{40}$")
        self.assertNotIn("findings", out.split("—— finished:")[1], "data, not transcript")

    def test_no_document_webhook_means_no_network_call(self):
        C.set_webhook_url("https://chat.googleapis.com/v1/spaces/PR/messages?key=k")  # PR one only
        cap = Capture()
        with mock.patch.object(N.urllib.request, "urlopen", cap):
            code, out = self.run_docs()
        self.assertEqual(code, 0, out)
        self.assertEqual(cap.bodies, [])

    def test_a_failing_webhook_does_not_fail_or_change_the_review(self):
        C.set_docs_webhook_url("https://chat.googleapis.com/x")
        with mock.patch.object(N.urllib.request, "urlopen", side_effect=OSError("down")):
            code, out = self.run_docs()
        self.assertEqual(code, 0, out)
        self.assertIn("Could not post to Google Chat", out)
        self.assertIn("Review v1", sh(self.origin, "show", "feat/x:docs/spec.md"))

    def test_a_dry_run_posts_no_card_and_still_reports_findings(self):
        """v7.0 (FR-DOC-IN-11, FR-X-5): a dry run reads and decides, and posts nothing."""
        C.set_docs_webhook_url("https://chat.googleapis.com/x")
        cap = Capture()
        with mock.patch.object(N.urllib.request, "urlopen", cap):
            code, out = self.run_docs("--dry-run")
        self.assertEqual(code, 0, out)
        self.assertEqual(cap.bodies, [])
        self.assertIn("Dry run: no chat card is posted.", out)
        (rec,) = self.jobs_json()
        self.assertEqual(len(rec["findings"]), 2)

    def test_the_name_falls_back_to_the_login_when_none_is_saved(self):
        with mock.patch("getpass.getuser", return_value="jdoe"):
            code, out = self.run_docs()
        self.assertEqual(code, 0, out)
        self.assertIn("· by jdoe · run `", sh(self.origin, "show", "feat/x:docs/spec.md"))

    def test_a_retry_signs_with_the_name_saved_now_and_alerts_the_saved_webhook(self):
        C.set_username("First")
        code, _o = self.run_docs()
        self.assertEqual(code, 0)
        (rec,) = self.jobs_json()
        self.assertNotIn("webhook_url", rec["spec"], "the secret is never written to the record")
        C.set_username("Second")
        C.set_docs_webhook_url("https://chat.googleapis.com/later")
        cap = Capture()
        out = io.StringIO()
        with mock.patch.object(N.urllib.request, "urlopen", cap), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys.stdin, "isatty", return_value=False):
            self.assertEqual(P.main(["retry", rec["id"]]), 0, out.getvalue())
        self.assertEqual(card_texts(cap.bodies[0])["Reviewed by"], "Second")
        doc = sh(self.origin, "show", "feat/x:docs/spec.md")
        self.assertIn("by First", doc)       # the earlier review keeps its own signature
        self.assertIn("by Second", doc)

    def test_an_old_record_still_loads_and_lists_with_an_empty_findings_array(self):
        CJ.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="1"), job_id="a1b2c3")
        rec = CJ.load("a1b2c3")
        for key in ("findings", "reviewer"):
            rec.pop(key)                                   # as written by v6.4
        CJ._write("a1b2c3", rec)
        CJ.finish("a1b2c3", J.DONE)
        (out,) = [r for r in self.jobs_json() if r["id"] == "a1b2c3"]
        self.assertEqual((out["findings"], out["reviewer"]), ([], ""))
        spec = CJ.spec_from_dict(CJ.load("a1b2c3")["spec"])        # and its spec loads
        self.assertEqual(spec.reviewer, "")


class RecordShape(Isolated):
    def test_a_new_pr_record_has_findings_and_reviewer_and_the_old_keys_are_unchanged(self):
        rec = CJ.create(J.JobSpec(project_dir="/p", repo_name="r", pr_id="42", reviewer="Alex"),
                        job_id="abc123")
        for key in ("id", "pid", "host", "cwd", "status", "stage", "started", "heartbeat",
                    "finished", "spec", "verdict", "impact", "branches", "author", "tokens",
                    "merged", "stopped", "error", "session_id"):
            self.assertIn(key, rec)
        self.assertEqual((rec["findings"], rec["reviewer"]), ([], "Alex"))
        self.assertEqual(rec["spec"]["reviewer"], "Alex")

    def test_a_pr_run_stores_the_findings_in_its_record(self):
        spec = J.JobSpec(project_dir="/p", repo_name="r", pr_id="42", reviewer="Alex")
        got = [{"id": "F1", "category": "high"}]
        runner = lambda **kw: {"merged": False, "stopped": "merge-disabled", "tokens": None,  # noqa: E731
                               "findings": got, "review": object()}
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = P.execute(spec, "abc123", quiet=False, runner=runner, interactive=False)
        self.assertEqual(code, 0)
        rec = CJ.load("abc123")
        self.assertEqual(rec["findings"], got)
        self.assertNotIn("F1", out.getvalue(), "the list is not echoed into the transcript")

    def test_with_identity_reads_the_saved_settings_for_either_kind_of_job(self):
        C.set_username("Alex")
        C.set_webhook_url("https://chat.googleapis.com/pr")
        C.set_docs_webhook_url("https://chat.googleapis.com/docs")
        pr = P.with_identity(J.JobSpec(project_dir="/p", repo_name="r", pr_id="1"))
        self.assertEqual((pr.reviewer, pr.webhook_url),
                         ("Alex", "https://chat.googleapis.com/pr"))
        doc = P.with_identity(D.DocJobSpec(project_dir="/p", targets=()))
        self.assertEqual((doc.reviewer, doc.webhook_url),
                         ("Alex", "https://chat.googleapis.com/docs"))

    def test_the_docs_record_keeps_the_name_but_not_the_webhook(self):
        spec = D.DocJobSpec(project_dir="/p", targets=(), reviewer="Alex",
                            webhook_url="https://chat.googleapis.com/secret")
        data = CJ.spec_to_dict(spec)
        self.assertEqual(data["reviewer"], "Alex")
        self.assertNotIn("secret", json.dumps(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
