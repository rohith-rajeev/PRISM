"""Documentation review: the section format, versioning, git flow and pipeline.

The model is never called: the agent is replaced by a function that returns a
canned report, so these tests exercise everything PRISM itself is responsible for
(what it writes, where, and what it refuses to do) against real git repositories.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import docreview as D  # noqa: E402
import jobs as J  # noqa: E402
import orchestrator as O  # noqa: E402

REPORT = """**Verdict:** ✅ Approve
**Impact score:** 4/10 — small

<<<PRISM-REVIEW>>>
### Summary
Looks fine overall.

### Blockers
- **[Blocker] DR-{n}** · Gap · §2 Payments — REQ-7 has no spec. Evidence: docs/requirements/pay.md. Suggested fix: add one.

### Major
- **[Major] DR-{m}** · Feasibility · §3 — module does not exist. Evidence: src/x.py.

### Coverage
Read everything.
<<<END-PRISM-REVIEW>>>
"""


def _sh(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                   encoding="utf-8")


def make_remote_and_clone(tmp, doc_text="# Spec\n\nBody.\n"):
    """A bare 'origin' plus a clone with docs/ and a feature branch pushed."""
    origin = Path(tmp) / "origin.git"
    clone = Path(tmp) / "clone"
    _sh(tmp, "init", "--bare", "-b", "main", str(origin))
    _sh(tmp, "clone", str(origin), str(clone))
    _sh(clone, "config", "user.email", "t@example.com")
    _sh(clone, "config", "user.name", "T")
    (clone / "docs" / "requirements").mkdir(parents=True)
    (clone / "docs" / "requirements" / "pay.md").write_text("REQ-7 payments\n", encoding="utf-8")
    (clone / "docs" / "spec.md").write_text(doc_text, encoding="utf-8")
    _sh(clone, "add", "-A")
    _sh(clone, "commit", "-m", "init")
    _sh(clone, "push", "origin", "HEAD:refs/heads/main")
    _sh(clone, "checkout", "-b", "feat/x")
    _sh(clone, "push", "origin", "feat/x")
    return origin, clone


class Section(unittest.TestCase):
    def run_(self, v=1, rid="r1", body="### Summary\nok\n", **meta):
        m = {"id": rid, "v": v, "date": "2026-11-07 10:00", "commit": "abcdef1234",
             "verdict": "✅ Approve", "impact": "3/10", "counts": "no comments"}
        m.update(meta)
        return D.Run(m, body)

    def test_first_review_is_appended_at_the_end_and_nothing_else_changes(self):
        text = "# Spec\n\nBody.\n"
        new = D.with_review(text, self.run_())
        self.assertTrue(new.startswith(text))
        self.assertIn(D.SECTION_TITLE, new)
        self.assertTrue(D.outside_unchanged(text, new))
        self.assertTrue(new.endswith(D.SECTION_END + "\n"))

    def test_file_without_final_newline_is_handled(self):
        text = "# Spec\nno newline"
        new = D.with_review(text, self.run_())
        self.assertTrue(D.outside_unchanged(text, new))
        self.assertTrue(new.startswith("# Spec\nno newline\n"))

    def test_second_review_replaces_the_section_in_place_and_keeps_history(self):
        text = "# Spec\n\nBody.\n"
        one = D.with_review(text, self.run_(1, "r1", body="### Summary\nfirst\n"))
        two = D.with_review(one, self.run_(2, "r2", body="### Summary\nsecond\n"))
        self.assertEqual(two.count(D.SECTION_START), 1)
        self.assertTrue(D.outside_unchanged(one, two))
        _b, runs, _a = D.split_document(two)
        self.assertEqual([r.meta["v"] for r in runs], [2, 1])
        self.assertIn("<details>", two)           # the older review is collapsed
        self.assertEqual(D.strip_section(two), D.strip_section(text))

    def test_reviews_round_trip_without_growing(self):
        """Reading a review back must return exactly what was written. Found by a
        live run: the generated header was being read back as part of the body,
        so every re-render nested the older reviews deeper."""
        bodies = {1: "### Summary\nfirst\n- **[Major] DR-1** · Gap · s — a", 2: "### Summary\nsecond",
                  3: "### Summary\nthird"}
        text = "# Spec\n"
        for v in (1, 2, 3):
            text = D.with_review(text, self.run_(v, f"r{v}", body=bodies[v]))
        _b, runs, _a = D.split_document(text)
        self.assertEqual([r.body for r in runs], [bodies[3], bodies[2], bodies[1]])
        self.assertEqual(text.count("<details>"), 2)          # not nested, not repeated
        for v in (1, 2, 3):
            self.assertEqual(text.count(f"Review v{v} ·"), 1)   # one title per run
        self.assertEqual(text.count("**Verdict:**"), 1)       # newest only; older ones fold it into their summary
        again = D.with_review(D.strip_section(text), self.run_(9, "r9"))
        self.assertEqual(D.split_document(again)[1][0].meta["v"], 9)

    def test_a_section_without_body_markers_is_still_read(self):
        legacy = (D.SECTION_START + "\n" + D._RUN_OPEN + '{"v":1,"id":"x"} -->\n'
                  "- **[Major] DR-4** · Gap · s — a\n" + D._RUN_CLOSE + "\n" + D.SECTION_END)
        _b, runs, _a = D.split_document("# S\n" + legacy)
        self.assertEqual(D.open_findings(runs)[0][1], "DR-4")

    def test_text_a_human_added_after_the_section_survives(self):
        one = D.with_review("# Spec\n", self.run_())
        edited = one + "\n## Appendix\nhuman wrote this\n"
        two = D.with_review(edited, self.run_(2, "r2"))
        self.assertTrue(two.endswith("## Appendix\nhuman wrote this\n"))
        self.assertTrue(D.outside_unchanged(edited, two))

    def test_outside_unchanged_detects_tampering(self):
        text = "# Spec\n\nBody.\n"
        new = D.with_review(text, self.run_())
        self.assertFalse(D.outside_unchanged(text, new.replace("Body.", "Changed.")))

    def test_agent_text_cannot_forge_or_close_the_section(self):
        evil = "x <!-- prism-review:start --> y <!-- prism-review:run-end --> <!-- prism-review:end -->"
        clean = D.sanitise_body(evil)
        self.assertNotIn("<!-- prism-review", clean)
        new = D.with_review("# Spec\n", self.run_(body=clean))
        self.assertEqual(new.count(D.SECTION_START), 1)
        self.assertEqual(new.count(D.SECTION_END), 1)

    def test_metadata_cannot_break_out_of_its_comment(self):
        new = D.with_review("# S\n", self.run_(verdict="a --> b <!-- c"))
        _b, runs, _a = D.split_document(new)
        self.assertEqual(runs[0].meta["verdict"], "a --> b <!-- c")


class Findings(unittest.TestCase):
    BODY = ("### Summary\nx\n\n### Previous comments\n"
            "- **DR-1** · ✅ Resolved — fixed\n- **DR-2** · ❌ Still open — still\n\n"
            "### Blockers\n- **[Blocker] DR-2** · Gap · §1 — still there.\n"
            "### Minor\n- **[Minor] DR-5** · Ambiguity · §4 — vague.\n")

    def test_counts_ignore_the_previous_comments_list(self):
        c = D.severity_counts(self.BODY)
        self.assertEqual((c["Blocker"], c["Major"], c["Minor"]), (1, 0, 1))

    def test_ids_are_never_reused_across_versions(self):
        runs = [D.Run({"v": 2}, self.BODY), D.Run({"v": 1}, "- **[Major] DR-9** · Gap · x — y")]
        self.assertEqual(D.next_finding_number(runs), 10)
        self.assertEqual(D.next_finding_number([]), 1)

    def test_open_findings_come_from_the_newest_review_only(self):
        runs = [D.Run({"v": 2}, self.BODY), D.Run({"v": 1}, "- **[Major] DR-9** · Gap · x — y")]
        self.assertEqual([i for _s, i, _l in D.open_findings(runs)], ["DR-2", "DR-5"])

    def test_counts_text(self):
        self.assertEqual(D.counts_text({"Blocker": 2, "Major": 1, "Minor": 0, "Suggestion": 1}),
                         "2 blockers, 1 major, 1 suggestion")
        self.assertEqual(D.counts_text({s: 0 for s in D.SEVERITIES}), "no comments")


class Verdict(unittest.TestCase):
    def test_a_verdict_is_never_rosier_than_its_comments(self):
        z = {s: 0 for s in D.SEVERITIES}
        self.assertEqual(D.reconcile_verdict("approve", dict(z, Blocker=1)),
                         ("request-changes", True))
        self.assertEqual(D.reconcile_verdict("approve", dict(z, Major=1)),
                         ("approve-with-comments", True))
        self.assertEqual(D.reconcile_verdict("block", dict(z, Blocker=1)), ("block", False))
        self.assertEqual(D.reconcile_verdict("approve", z), ("approve", False))
        self.assertEqual(D.reconcile_verdict("", z)[0], "approve")

    def test_report_parsing_takes_the_last_complete_block(self):
        text = ("format example <<<PRISM-REVIEW>>>\nold\n<<<END-PRISM-REVIEW>>>\n"
                + REPORT.format(n=1, m=2))
        res, body = D.parse_agent_report(text)
        self.assertEqual(res.verdict_key, "approve")
        self.assertEqual(res.impact_score, "4")
        self.assertIn("### Summary", body)
        self.assertNotIn("old", body)

    def test_no_markers_means_no_body(self):
        self.assertIsNone(D.parse_agent_report("**Verdict:** ✅ Approve\nprose only")[1])
        self.assertIsNone(D.parse_agent_report("<<<PRISM-REVIEW>>>\nunterminated")[1])


class Inputs(unittest.TestCase):
    def test_parse_files(self):
        self.assertEqual(D.parse_files("a.md, b.md\n c.md;a.md "), ("a.md", "b.md", "c.md"))
        self.assertEqual(D.parse_files(""), ())

    def test_paths_cannot_escape_the_repository(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(D.resolve_inside(d, "../etc/passwd"))
            self.assertIsNone(D.resolve_inside(d, "/etc/passwd"))
            self.assertIsNone(D.resolve_inside(d, ".git/config"))
            self.assertIsNotNone(D.resolve_inside(d, "docs/a.md"))

    def test_branch_names(self):
        self.assertIsNone(D.check_branch("feat/x"))
        for bad in ("", "-rf", "a b", "refs/heads/x", "a..b"):
            self.assertIsNotNone(D.check_branch(bad), bad)

    def test_requirements_found_relative_or_absolute(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(b) / "docs" / "requirements").mkdir(parents=True)
            self.assertEqual(D.find_requirements("docs/requirements", [a, b]),
                             (Path(b) / "docs" / "requirements").resolve())
            self.assertEqual(D.find_requirements(str(Path(b) / "docs"), [a]), Path(b) / "docs")
            self.assertIsNone(D.find_requirements("nope", [a, b]))

    def test_commit_message_shape(self):
        m = D.commit_message("20261107-1432-ab12", "review\ncomments  v1")
        self.assertEqual(m, "PRISM-20261107-1432-ab12: review comments v1")

    def test_run_ids_are_unique(self):
        # Many, and from many threads at once: no id is ever handed out twice.
        import threading
        seen, lock = [], threading.Lock()

        def draw():
            ids = [D.new_run_id() for _ in range(2000)]
            with lock:
                seen.extend(ids)
        threads = [threading.Thread(target=draw) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(set(seen)), 8000)

    def test_run_ids_keep_their_readable_shape(self):
        import re
        self.assertRegex(D.new_run_id(), r"^\d{8}-\d{4}-[0-9a-f]{6}$")

    def test_a_clash_is_redrawn_not_returned(self):
        """If the random part repeated within a minute, the id handed out would still differ."""
        import uuid
        real = uuid.uuid4
        with mock.patch.object(D.time, "strftime", lambda fmt: "20261107-1432"):
            first = D.new_run_id()
            # The next draw reproduces first's random digits exactly (a true clash); the
            # one after it is genuinely different.
            clash = uuid.UUID(hex=first[-6:] + "0" * 26)
            self.assertEqual(clash.hex[:6], first[-6:])
            draws = iter([clash, real()])
            with mock.patch.object(D.uuid, "uuid4", lambda: next(draws)):
                second = D.new_run_id()
        self.assertEqual(first[:14], second[:14])             # same minute ...
        self.assertNotEqual(first, second)                     # ... still not the same id


class Browsing(unittest.TestCase):
    def test_picked_files_become_repository_relative_paths(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            (repo / "docs").mkdir(parents=True)
            (repo / ".git").mkdir()
            a, b = repo / "docs" / "a.md", repo / "b.md"
            a.write_text("x", encoding="utf-8")
            b.write_text("x", encoding="utf-8")
            outside = Path(d) / "elsewhere.md"
            outside.write_text("x", encoding="utf-8")
            rels, skipped = D.repo_relative(
                [str(a), str(b), str(a), str(outside), str(repo / ".git" / "config"),
                 str(repo)], repo)
            self.assertEqual(rels, ["docs/a.md", "b.md"])          # deduplicated, ordered
            self.assertEqual(len(skipped), 3)                       # outside, .git/config, the repo root
            self.assertIn(str(outside), skipped)

    def test_a_requirements_folder_is_kept_relative_to_where_it_lives(self):
        with tempfile.TemporaryDirectory() as d:
            work, clone = Path(d), Path(d) / "shop"
            (clone / "docs" / "requirements").mkdir(parents=True)
            (work / "shared").mkdir()
            reqs = clone / "docs" / "requirements"
            # inside a clone: relative to the clone, not to the (larger) working folder
            self.assertEqual(D.relativise_folder(reqs, [clone, work]), "docs/requirements")
            # not in a clone but under the working folder
            self.assertEqual(D.relativise_folder(work / "shared", [clone, work]), "shared")
            # nowhere known: the full path
            with tempfile.TemporaryDirectory() as other:
                self.assertEqual(D.relativise_folder(other, [clone, work]),
                                 str(Path(other).resolve()))
            # the repository root itself cannot be a relative path: keep it absolute
            self.assertEqual(D.relativise_folder(clone, [clone]), str(clone.resolve()))


class FilePickerCommands(unittest.TestCase):
    def setUp(self):
        import folderpicker as FP
        self.FP = FP

    def which(self, name):
        return lambda n: f"/usr/bin/{n}" if n == name else None

    def test_each_native_chooser_gets_a_multiple_selection_command(self):
        for tool, flag in (("zenity", "--multiple"), ("kdialog", "--multiple"),
                           ("yad", "--multiple")):
            cmd, sep = self.FP.files_command("t", "/tmp", self.which(tool))
            self.assertEqual(cmd[0], tool)
            self.assertIn(flag, cmd)
        self.assertEqual(self.FP.files_command("t", "/tmp", self.which("kdialog"))[1], "\n")
        self.assertEqual(self.FP.files_command("t", "/tmp", self.which("zenity"))[1],
                         self.FP.FILE_SEP)

    def test_no_chooser_means_none(self):
        self.assertIsNone(self.FP.files_command("t", "/tmp", lambda n: None))
        self.assertIsNone(self.FP.pick_files("t", "/tmp", platform="linux", which=lambda n: None))
        self.assertIsNone(self.FP.pick_files("t", "/tmp", platform="win32"))

    def run_with(self, stdout, code=0):
        import types
        run = lambda *a, **k: types.SimpleNamespace(returncode=code, stdout=stdout)  # noqa: E731
        return self.FP.pick_files("t", "/tmp", platform="linux", which=self.which("zenity"),
                                  run=run)

    def test_the_selection_is_split_even_when_names_contain_pipes_or_spaces(self):
        sep = self.FP.FILE_SEP
        out = f"/r/a|b.md{sep}/r/my spec.md\n"
        self.assertEqual(self.run_with(out), ["/r/a|b.md", "/r/my spec.md"])

    def test_cancel_is_an_empty_list_and_failure_is_none(self):
        self.assertEqual(self.run_with("", code=1), [])
        self.assertEqual(self.run_with("", code=252), [])
        self.assertIsNone(self.run_with("", code=5))


class Prompt(unittest.TestCase):
    def test_previous_block_lists_open_comments_and_the_delta(self):
        runs = [D.Run({"v": 1, "id": "r1", "commit": "abcdef123456", "verdict": "x"},
                      "- **[Major] DR-3** · Gap · s — y")]
        block = D.build_previous_block(runs, "-old\n+new\n", D.open_findings(runs))
        self.assertIn("INCREMENTAL", block)
        self.assertIn("DR-3", block)
        self.assertIn("+new", block)
        self.assertEqual(D.build_previous_block([], "", []), "")

    def test_the_agent_is_told_to_ignore_the_review_section(self):
        tgt = D.DocTarget("repo", "r", "/r", "b", ("a.md",))
        p = D.build_prompt(tgt, "a.md", "/wt/a.md", "/wt/docs", [], False, [], "/wt", [],
                           "", "", 4)
        self.assertIn("not part of the document under review", p)
        self.assertIn("Start new comments at DR-4", p)

    def test_delta_ignores_review_sections(self):
        old = D.with_review("# S\nline\n", D.Run({"v": 1}, "a"))
        new = D.with_review("# S\nline\nadded\n", D.Run({"v": 2}, "b"))
        delta = D.doc_delta(old, new)
        self.assertIn("+added", delta)
        self.assertNotIn("PRISM review comments", delta)


class Pipeline(unittest.TestCase):
    """Real git, canned agent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.origin, self.clone = make_remote_and_clone(self.tmp.name)
        self.project = Path(self.tmp.name) / "proj"
        self.project.mkdir()
        self.calls = []
        self.reports = []
        self._saved = {k: getattr(O, k) for k in ("ensure_bundled_agents", "require_engine")}
        O.ensure_bundled_agents = lambda *a, **k: ["doc-reviewer"]
        self._saved_run = D._run_agent
        D._run_agent = self.fake_agent
        self.addCleanup(self.restore)
        self.log = []

    def restore(self):
        for k, v in self._saved.items():
            setattr(O, k, v)
        D._run_agent = self._saved_run

    def fake_agent(self, prompt, project_dir, emit, model, control, meter):
        self.calls.append(prompt)
        n = 1
        import re
        m = re.search(r"Start new comments at DR-(\d+)", prompt)
        if m:
            n = int(m.group(1))
        text = self.reports.pop(0) if self.reports else REPORT
        return 0, text.format(n=n, m=n + 1), "sess-1"

    def target(self, files=("docs/spec.md",), branch="feat/x"):
        return D.DocTarget("repo", "clone", str(self.clone), branch, tuple(files))

    def run_job(self, **kw):
        args = dict(project_dir=str(self.project), targets=[self.target()],
                    emit=lambda m, tag=None: self.log.append(m))
        args.update(kw)
        return D._pipeline(**args)

    def remote_text(self, path="docs/spec.md", branch="feat/x"):
        return subprocess.run(["git", "show", f"{branch}:{path}"], cwd=self.origin,
                              capture_output=True, text=True, encoding="utf-8").stdout

    def remote_log(self, branch="feat/x"):
        return subprocess.run(["git", "log", "--format=%s", branch], cwd=self.origin,
                              capture_output=True, text=True, encoding="utf-8").stdout.splitlines()

    def test_full_run_writes_commits_and_pushes(self):
        res = self.run_job()
        self.assertTrue(res["pushed"] and res["committed"])
        text = self.remote_text()
        self.assertTrue(text.startswith("# Spec\n\nBody.\n"))
        self.assertIn(D.SECTION_TITLE, text)
        self.assertIn("DR-1", text)
        top = self.remote_log()[0]
        self.assertTrue(top.startswith(f"PRISM-{res['run_id']}: "), top)
        # the verdict was raised because it listed a blocker yet said Approve
        self.assertTrue(any("Request changes" in m for m in self.log if isinstance(m, str)))
        self.assertTrue(any(m.strip().startswith("◆ Verdict: 🔴 Request changes") for m in self.log))

    def test_users_own_clone_is_never_touched(self):
        (self.clone / "docs" / "spec.md").write_text("local edit\n", encoding="utf-8")
        before = subprocess.run(["git", "status", "--porcelain"], cwd=self.clone,
                                capture_output=True, text=True, encoding="utf-8").stdout
        head = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.clone,
                              capture_output=True, text=True, encoding="utf-8").stdout
        self.run_job()
        after = subprocess.run(["git", "status", "--porcelain"], cwd=self.clone,
                               capture_output=True, text=True, encoding="utf-8").stdout
        self.assertEqual(before, after)
        self.assertEqual((self.clone / "docs" / "spec.md").read_text(encoding="utf-8"),
                         "local edit\n")
        self.assertEqual(head, subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                              cwd=self.clone, capture_output=True, text=True,
                                              encoding="utf-8").stdout)

    def test_second_run_is_incremental_and_versioned(self):
        first = self.run_job()
        self.assertNotIn("PREVIOUS REVIEW", self.calls[0])
        # the author edits the doc on the remote between the two reviews
        _sh(self.clone, "fetch", "origin")
        _sh(self.clone, "checkout", "-q", "feat/x")
        _sh(self.clone, "pull", "-q", "origin", "feat/x")
        p = self.clone / "docs" / "spec.md"
        p.write_text(p.read_text(encoding="utf-8").replace("Body.", "Body, revised."),
                     encoding="utf-8")
        _sh(self.clone, "commit", "-qam", "revise")
        _sh(self.clone, "push", "-q", "origin", "feat/x")
        second = self.run_job()
        self.assertIn("PREVIOUS REVIEW", self.calls[1])
        self.assertIn("DR-1", self.calls[1])
        self.assertIn("+Body, revised.", self.calls[1])
        self.assertIn("Start new comments at DR-3", self.calls[1])
        _b, runs, _a = D.split_document(self.remote_text())
        self.assertEqual([r.meta["v"] for r in runs], [2, 1])
        self.assertEqual([r.meta["id"] for r in runs], [second["run_id"], first["run_id"]])
        self.assertIn("Body, revised.", self.remote_text())

    def test_dry_run_writes_nothing(self):
        before = self.remote_log()
        res = self.run_job(dry_run=True)
        self.assertFalse(res["committed"])
        self.assertEqual(self.remote_log(), before)
        self.assertNotIn(D.SECTION_TITLE, self.remote_text())

    def test_commit_without_push_leaves_the_remote_alone(self):
        before = self.remote_log()
        res = self.run_job(do_push=False)
        self.assertTrue(res["committed"] and not res["pushed"])
        self.assertEqual(self.remote_log(), before)

    def test_push_off_when_commit_off(self):
        res = self.run_job(do_commit=False)
        self.assertFalse(res["committed"] or res["pushed"])

    def test_missing_requirements_folder_stops_before_the_agent_runs(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(requirements_path="no/such/dir")
        self.assertIn("Requirement folder", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_requirement_folder_is_configurable(self):
        (self.clone / "specs-src").mkdir()
        (self.clone / "specs-src" / "a.md").write_text("req\n", encoding="utf-8")
        _sh(self.clone, "add", "-A")
        _sh(self.clone, "commit", "-qm", "reqs")
        _sh(self.clone, "push", "-q", "origin", "feat/x")
        self.run_job(requirements_path="specs-src")
        self.assertIn("specs-src", self.calls[0])

    def test_a_missing_document_fails_before_any_model_run(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(targets=[self.target(("docs/spec.md", "docs/typo.md"))])
        self.assertIn("typo.md", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_unknown_branch_is_reported(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(targets=[self.target(branch="nope")])
        self.assertIn("was not found", str(cm.exception))

    def test_a_reply_without_a_report_writes_nothing_and_fails_the_job(self):
        self.reports = ["I looked at it and it is fine."] * 2     # the retry fails too
        O_cont = O._continue_turn
        O._continue_turn = lambda *a, **k: (0, "still no report", "sess-1")
        self.addCleanup(setattr, O, "_continue_turn", O_cont)
        before = self.remote_log()
        with self.assertRaises(RuntimeError):
            self.run_job()
        self.assertEqual(self.remote_log(), before)

    def test_push_rejected_then_replayed_on_top_of_a_newer_branch(self):
        orig = D._run_agent

        def agent_then_remote_moves(prompt, *a, **k):
            out = orig(prompt, *a, **k)
            # someone else pushes to the branch while the review is running
            other = Path(self.tmp.name) / "other"
            _sh(self.tmp.name, "clone", "-q", str(self.origin), str(other))
            _sh(other, "config", "user.email", "o@example.com")
            _sh(other, "config", "user.name", "O")
            _sh(other, "checkout", "-q", "feat/x")
            (other / "unrelated.txt").write_text("hi\n", encoding="utf-8")
            _sh(other, "add", "-A")
            _sh(other, "commit", "-qm", "unrelated")
            _sh(other, "push", "-q", "origin", "feat/x")
            return out
        D._run_agent = agent_then_remote_moves
        res = self.run_job()
        self.assertTrue(res["pushed"])
        log = self.remote_log()
        self.assertEqual(log[1], "unrelated")
        self.assertTrue(log[0].startswith("PRISM-"))

    def test_a_push_that_cannot_succeed_keeps_the_commit_in_a_local_ref(self):
        _sh(self.clone, "remote", "set-url", "--push", "origin", "/nonexistent/remote.git")
        with self.assertRaises(RuntimeError) as cm:
            self.run_job()
        self.assertIn("refs/prism/docs/", str(cm.exception))
        refs = subprocess.run(["git", "for-each-ref", "refs/prism/docs"], cwd=self.clone,
                              capture_output=True, text=True, encoding="utf-8").stdout
        self.assertIn("refs/prism/docs/", refs)

    def test_worktrees_are_cleaned_up(self):
        self.run_job()
        out = subprocess.run(["git", "worktree", "list"], cwd=self.clone, capture_output=True,
                             text=True, encoding="utf-8").stdout.strip().splitlines()
        self.assertEqual(len(out), 1)

    def test_two_repositories_in_one_run_share_a_run_id(self):
        second = Path(self.tmp.name) / "second"
        second.mkdir()
        origin2, clone2 = make_remote_and_clone(second)
        t1 = D.DocTarget("BE", "clone", str(self.clone), "feat/x", ("docs/spec.md",))
        t2 = D.DocTarget("FE", "clone2", str(clone2), "feat/x", ("docs/spec.md",))
        res = self.run_job(targets=[t1, t2])
        self.assertTrue(res["pushed"])
        self.assertEqual(res["documents"], 2)
        self.assertEqual(self.remote_log()[0].split(":")[0], f"PRISM-{res['run_id']}")
        top2 = subprocess.run(["git", "log", "-1", "--format=%s", "feat/x"], cwd=origin2,
                              capture_output=True, text=True, encoding="utf-8").stdout
        self.assertTrue(top2.startswith(f"PRISM-{res['run_id']}:"))
        # each document is told about the other under review
        self.assertIn("Other documents under review", self.calls[0])

    def test_progress_stages_for_a_full_run(self):
        seen = []
        self.run_job(progress=lambda s, st: seen.append((s, st)))
        self.assertIn((D.STAGE_DOC_PUSH, "done"), seen)
        self.assertEqual(seen[0], (D.STAGE_DOC_REVIEW, "active"))


    def test_a_remark_about_coverage_does_not_discard_a_delivered_review(self):
        """The frontend document was dropped as "not examined" because its Coverage said
        it "did not read the requirements ..." - though a full review came with it."""
        report = REPORT.replace("### Coverage", "### Coverage\n- Did not read the requirements "
                                "folder again, as it was read for the other document.")
        self.reports = [report]
        res = self.run_job()
        self.assertEqual(res["documents"], 1)
        self.assertEqual(res["not_reviewed"], 0)
        self.assertTrue(any("check its Coverage" in m for m in self.log if isinstance(m, str)))

    def test_a_declared_incomplete_review_is_retried_once(self):
        gave_up = ("**Verdict:** \u2753 Review incomplete\n**Impact score:** n/a\n\n"
                   "Summary: could not read it. Coverage: 0 files reviewed.")
        self.reports = [gave_up]               # the second attempt gets the normal REPORT
        res = self.run_job()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(res["documents"], 1)

    def test_preflight_reports_every_missing_document_with_a_suggestion(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(targets=[self.target(("docs/spce.md", "docs/other.md"))])
        msg = str(cm.exception)
        self.assertIn("Pre-flight", msg)
        self.assertIn("docs/spce.md", msg)
        self.assertIn("docs/other.md", msg)
        self.assertIn("Did you mean: docs/spec.md", msg)
        self.assertEqual(self.calls, [])

    def test_preflight_reports_a_missing_branch_before_any_model_time(self):
        with self.assertRaises(RuntimeError) as cm:
            self.run_job(targets=[self.target(branch="no/such")])
        self.assertIn("Pre-flight", str(cm.exception))
        self.assertEqual(self.calls, [])


class Tone(unittest.TestCase):
    def test_narration_is_removed_and_prose_becomes_bullets(self):
        out = D.professionalise(
            "### Summary\nI reviewed the backend document against 12 requirements. "
            "Refunds are missing.\nLet me note the main risk.\n- **Security:** ok\n\n"
            "### Blockers\n- **[Blocker] DR-1** \u00b7 Gap \u00b7 s \u2014 I think x.\n")
        self.assertNotIn("I reviewed", out)
        self.assertNotIn("Let me", out)
        self.assertIn("- Refunds are missing.", out)
        self.assertIn("- **[Blocker] DR-1** \u00b7 Gap \u00b7 s \u2014 I think x.", out)

    def test_counting_is_unaffected(self):
        body = D.sanitise_body("### Summary\nWe checked it.\n### Major\n"
                               "- **[Major] DR-2** \u00b7 Gap \u00b7 s \u2014 y.")
        self.assertEqual(D.severity_counts(body)["Major"], 1)


class ContinuationTurns(unittest.TestCase):
    """Every follow-on turn must stay with the doc-reviewer agent.

    O._continue_turn defaults to the PR reviewer, so a call that forgets to name
    the agent silently hands the conversation to the wrong one. Found by a live
    run; the mocked pipeline tests above cannot see it.
    """

    def setUp(self):
        self.agents = []
        self._saved = (O._continue_turn, D._run_agent)
        O._continue_turn = self.fake_turn
        self.addCleanup(self.restore)

    def restore(self):
        O._continue_turn, D._run_agent = self._saved

    def fake_turn(self, prompt, project_dir, sid, emit, model, control, what,
                  agent=None, meter=None, timeout=None, idle_timeout=None):
        self.agents.append(agent)
        return 0, REPORT.format(n=1, m=2), sid

    def review(self, first_output, **kw):
        D._run_agent = lambda *a, **k: (0, first_output, "s1")
        return D.review_document("p", "/proj", lambda *a, **k: None, None, None, None, **kw)

    def test_no_report_nudge_uses_the_doc_reviewer(self):
        _res, body, _sid, _partial = self.review("prose with no report")
        self.assertIsNotNone(body)
        self.assertEqual(self.agents, [D.AGENT_DOC_REVIEWER])

    def test_answering_a_question_uses_the_doc_reviewer(self):
        _res, body, _sid, _p = self.review("Which requirement folder should I use?",
                                           ask=lambda q: "docs/requirements")
        self.assertIsNotNone(body)
        self.assertTrue(self.agents and set(self.agents) == {D.AGENT_DOC_REVIEWER})

    def test_a_steering_note_uses_the_doc_reviewer(self):
        notes = iter(["focus on payments"])
        self.review(REPORT.format(n=1, m=2), get_note=lambda: next(notes, None))
        self.assertEqual(self.agents, [D.AGENT_DOC_REVIEWER])

    def test_cut_short_wrap_up_uses_the_doc_reviewer(self):
        self.review(O.CUT_SHORT_MARKER + ": stalled]")
        self.assertIn(D.AGENT_DOC_REVIEWER, self.agents)
        self.assertEqual(set(self.agents), {D.AGENT_DOC_REVIEWER})

    def test_follow_up_questions_use_the_doc_reviewer(self):
        O.ensure_bundled_agents, saved = (lambda *a, **k: []), O.ensure_bundled_agents
        self.addCleanup(setattr, O, "ensure_bundled_agents", saved)
        D.run_followup("why DR-1?", "/proj", session_id="s1", emit=lambda *a, **k: None)
        self.assertEqual(self.agents, [D.AGENT_DOC_REVIEWER])


class Scheduler(unittest.TestCase):
    def spec(self, files=("a.md",), branch="b"):
        return D.DocJobSpec("/p", (D.DocTarget("repo", "r", "/p", branch, tuple(files)),))

    def test_kinds_and_labels(self):
        s = self.spec()
        self.assertEqual(s.kind, "docs")
        self.assertEqual(J.JobSpec("/p", "r", "1").kind, "pr")
        self.assertIn("1 file", s.label)
        self.assertIn("Business Analyst", s.label)
        self.assertEqual(s.summary_bits(), "Business Analyst review · no requirements")
        self.assertEqual(s.pipeline_kwargs()["targets"], s.targets)

    def test_duplicate_detection_is_per_kind_and_per_document(self):
        import queue
        m = J.JobManager(queue.Queue(), runner=lambda **k: {})
        m.create(self.spec())
        with self.assertRaises(J.DuplicateJob):
            m.create(self.spec())
        m.create(self.spec(files=("other.md",)))          # a different document is fine
        m.create(J.JobSpec("/p", "r", "7"))               # a PR is unaffected by docs jobs
        with self.assertRaises(J.DuplicateJob):
            m.create(J.JobSpec("/p", "r", "7"))

    def test_a_docs_job_runs_its_own_pipeline_not_the_pr_one(self):
        import queue
        q = queue.Queue()
        called = {}
        m = J.JobManager(q, runner=lambda **k: called.setdefault("pr", True) and {})
        spec = self.spec()
        job = m.create(spec)
        ran = {}
        # the spec's own runner is what the scheduler must use
        D_full = D.full_pipeline
        D.full_pipeline = lambda **k: ran.setdefault("docs", k) and {"documents": 0}
        try:
            m._work(job)
        finally:
            D.full_pipeline = D_full
        self.assertNotIn("pr", called)
        self.assertEqual(job.branches, "r: b")

    def test_a_pr_job_still_uses_the_managers_runner(self):
        import queue
        q = queue.Queue()
        seen = {}
        m = J.JobManager(q, runner=lambda **k: seen.update(k) or {"merged": True})
        job = m.create(J.JobSpec("/p", "r", "9"))
        m._work(job)
        self.assertEqual(seen["pr_id"], "9")


if __name__ == "__main__":
    unittest.main(verbosity=2)
