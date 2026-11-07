"""`prism docs`: document review from the command line.

No Tk, no network. Real git repositories for everything that touches git; the
reviewer is replaced by a canned reply, so what is tested is PRISM's own part:
resolving the target from where you are, precedence of flag / saved default /
inferred value, the job record, and the commit that lands on the remote.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import cli_jobs as CJ  # noqa: E402
import config as C  # noqa: E402
import docreview as D  # noqa: E402
import jobs as J  # noqa: E402
import orchestrator as O  # noqa: E402
import prism_cli as P  # noqa: E402
from test_cli import Isolated  # noqa: E402
from test_docreview import REPORT, make_remote_and_clone  # noqa: E402

ROOT = HERE.parent


def args(**kw):
    """The Namespace `prism docs` would parse, with every option unset."""
    base = dict(files=[], path=None, clone=None, branch=None, be=None, be_clone=None,
                be_branch=None, fe=None, fe_clone=None, fe_branch=None, requirements=None,
                model=None, commit=None, push=None, dry_run=None, instructions=None,
                instructions_file=None, detach=False, quiet=False, job_id=None, spec_json=None)
    base.update(kw)
    return Namespace(**base)


def fixture(path):
    """make_remote_and_clone in a folder it creates first."""
    Path(path).mkdir(parents=True, exist_ok=True)
    return make_remote_and_clone(path)


def sh(cwd, *a):
    return subprocess.run(["git", *a], cwd=str(cwd), check=True, capture_output=True, text=True,
                          encoding="utf-8").stdout


class Resolution(Isolated):
    def setUp(self):
        super().setUp()
        self.origin, self.clone = fixture(self.home / "one")

    def test_defaults_come_from_where_you_are(self):
        spec = P.build_docs_spec(args(files=["docs/spec.md"]), cwd=self.clone)
        (t,) = spec.targets
        self.assertEqual((t.role, t.branch, t.files), ("repo", "feat/x", ("docs/spec.md",)))
        self.assertEqual(Path(t.local_repo).resolve(), self.clone.resolve())
        self.assertEqual(spec.requirements_path, "docs/requirements")
        self.assertTrue(spec.do_commit and spec.do_push and not spec.dry_run)
        self.assertEqual(spec.origin, "cli")
        self.assertEqual(spec.kind, "docs")

    def test_a_subfolder_works_and_files_are_typed_the_way_a_shell_would(self):
        sub = self.clone / "docs"
        spec = P.build_docs_spec(args(files=["spec.md"]), cwd=sub)   # relative to where you are
        self.assertEqual(spec.targets[0].files, ("docs/spec.md",))
        spec = P.build_docs_spec(args(files=[str(self.clone / "docs" / "spec.md"), "./docs/spec.md"]),
                                 cwd=self.clone)                      # absolute, ./, duplicates
        self.assertEqual(spec.targets[0].files, ("docs/spec.md",))

    def test_a_file_only_on_the_branch_is_taken_from_the_repository_root(self):
        spec = P.build_docs_spec(args(files=["specs/not-here-yet.md"]), cwd=self.clone)
        self.assertEqual(spec.targets[0].files, ("specs/not-here-yet.md",))

    def test_an_absolute_path_outside_the_clone_is_refused(self):
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(files=["/etc/hosts"]), cwd=self.clone)
        self.assertIn("not inside", str(cm.exception))

    def test_flags_beat_saved_defaults_which_beat_built_ins(self):
        C.set_cli(requirements="reqs-from-config", model="m-config", commit=False,
                  instructions="from config")
        spec = P.build_docs_spec(args(files=["docs/spec.md"]), cwd=self.clone)
        self.assertEqual((spec.requirements_path, spec.model, spec.do_commit, spec.do_push,
                          spec.custom_instructions),
                         ("reqs-from-config", "m-config", False, False, "from config"))
        spec = P.build_docs_spec(args(files=["docs/spec.md"], requirements="docs/requirements",
                                      model="m-flag", commit=True, push=False,
                                      instructions="from flag", branch="main"), cwd=self.clone)
        self.assertEqual((spec.requirements_path, spec.model, spec.do_commit, spec.do_push,
                          spec.custom_instructions, spec.targets[0].branch),
                         ("docs/requirements", "m-flag", True, False, "from flag", "main"))

    def test_push_is_never_on_without_commit(self):
        spec = P.build_docs_spec(args(files=["docs/spec.md"], commit=False, push=True),
                                 cwd=self.clone)
        self.assertFalse(spec.do_push)

    def test_a_requirements_folder_you_can_see_is_stored_relative_to_its_repository(self):
        (self.clone / "specs-src").mkdir()
        spec = P.build_docs_spec(args(files=["docs/spec.md"], requirements="specs-src"),
                                 cwd=self.clone)
        self.assertEqual(spec.requirements_path, "specs-src")
        spec = P.build_docs_spec(args(files=["docs/spec.md"],
                                      requirements=str(self.clone / "docs" / "requirements")),
                                 cwd=self.clone)
        self.assertEqual(spec.requirements_path, "docs/requirements")

    def test_instructions_file(self):
        f = self.home / "i.txt"
        f.write_text("  look at payments  ", encoding="utf-8")
        spec = P.build_docs_spec(args(files=["docs/spec.md"], instructions_file=str(f)),
                                 cwd=self.clone)
        self.assertEqual(spec.custom_instructions, "look at payments")
        with self.assertRaises(P.UsageError):
            P.build_docs_spec(args(files=["docs/spec.md"], instructions_file="/no/such"),
                              cwd=self.clone)

    def test_the_spec_is_valid_pipeline_input(self):
        import inspect
        spec = P.build_docs_spec(args(files=["docs/spec.md"]), cwd=self.clone)
        self.assertTrue(set(spec.pipeline_kwargs()) <= set(inspect.signature(D._pipeline).parameters))

    # ----- errors say what to do -----
    def test_no_documents_named(self):
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(), cwd=self.clone)
        self.assertIn("Say which document", str(cm.exception))

    def test_detached_head_asks_for_a_branch(self):
        sh(self.clone, "checkout", "-q", "--detach")
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(files=["docs/spec.md"]), cwd=self.clone)
        self.assertIn("--branch", str(cm.exception))
        spec = P.build_docs_spec(args(files=["docs/spec.md"], branch="feat/x"), cwd=self.clone)
        self.assertEqual(spec.targets[0].branch, "feat/x")

    def test_a_bad_branch_name_is_refused(self):
        with self.assertRaises(P.UsageError):
            P.build_docs_spec(args(files=["docs/spec.md"], branch="-rf"), cwd=self.clone)

    def test_not_in_a_clone(self):
        empty = self.home / "empty"
        empty.mkdir()
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(files=["a.md"]), cwd=empty)
        self.assertIn("No git clone", str(cm.exception))

    def test_a_missing_path_or_clone(self):
        with self.assertRaises(P.UsageError):
            P.build_docs_spec(args(files=["a.md"], path="/no/such/dir"), cwd=self.home)
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(files=["a.md"], clone=str(self.home)), cwd=self.home)
        self.assertIn("not a git clone", str(cm.exception))


class TwoRepositories(Isolated):
    def setUp(self):
        super().setUp()
        self.top = self.home / "work"
        self.top.mkdir()
        self.be = make_remote_and_clone_into(self.top / "shop-backend")
        self.fe = make_remote_and_clone_into(self.top / "shop-frontend")

    def test_backend_and_frontend_are_found_by_folder_name(self):
        spec = P.build_docs_spec(args(be=["docs/spec.md"], fe=["docs/spec.md", "docs/ui.md"]),
                                 cwd=self.top)
        roles = {t.role: t for t in spec.targets}
        self.assertEqual(Path(roles["BE"].local_repo).name, "shop-backend")
        self.assertEqual(Path(roles["FE"].local_repo).name, "shop-frontend")
        self.assertEqual(roles["FE"].files, ("docs/spec.md", "docs/ui.md"))
        self.assertEqual({t.branch for t in spec.targets}, {"feat/x"})

    def test_either_one_alone(self):
        spec = P.build_docs_spec(args(fe=["docs/spec.md"], fe_branch="feat/x"), cwd=self.top)
        self.assertEqual([t.role for t in spec.targets], ["FE"])
        self.assertEqual(Path(spec.targets[0].local_repo).name, "shop-frontend")

    def test_one_role_inside_a_clone_means_that_clone(self):
        spec = P.build_docs_spec(args(be=["docs/spec.md"]), cwd=self.be)
        self.assertEqual(Path(spec.targets[0].local_repo).resolve(), self.be.resolve())

    def test_explicit_clones_and_branches_win(self):
        spec = P.build_docs_spec(
            args(be=["docs/spec.md"], be_clone=str(self.fe), be_branch="main",
                 fe=["docs/spec.md"], fe_clone=str(self.be), fe_branch="feat/x"), cwd=self.home)
        roles = {t.role: t for t in spec.targets}
        self.assertEqual(Path(roles["BE"].local_repo).name, "shop-frontend")   # as told
        self.assertEqual(roles["BE"].branch, "main")

    def test_folders_that_do_not_say_which_is_which_are_not_guessed(self):
        top = self.home / "other"
        top.mkdir()
        make_remote_and_clone_into(top / "alpha")
        make_remote_and_clone_into(top / "beta")
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(be=["docs/spec.md"]), cwd=top)
        self.assertIn("--be-clone", str(cm.exception))

    def test_the_other_clone_is_inferred_when_one_name_is_clear(self):
        top = self.home / "third"
        top.mkdir()
        make_remote_and_clone_into(top / "portal-frontend")
        make_remote_and_clone_into(top / "api")
        spec = P.build_docs_spec(args(be=["docs/spec.md"]), cwd=top)
        self.assertEqual(Path(spec.targets[0].local_repo).name, "api")

    def test_same_clone_twice_is_refused(self):
        with self.assertRaises(P.UsageError) as cm:
            P.build_docs_spec(args(be=["a.md"], be_clone=str(self.be), fe=["a.md"],
                                   fe_clone=str(self.be)), cwd=self.top)
        self.assertIn("same clone", str(cm.exception))

    def test_mixed_and_stray_options_are_refused(self):
        for bad in (args(files=["a.md"], be=["b.md"]),                 # plain + --be
                    args(files=["a.md"], fe_branch="x"),               # --fe-* with no --fe
                    args(be=["a.md"], branch="x"),                     # --branch with --be
                    args(be=["a.md"], fe_clone=str(self.fe))):         # --fe-clone, no --fe files
            with self.assertRaises(P.UsageError, msg=str(bad)):
                P.build_docs_spec(bad, cwd=self.top)


def make_remote_and_clone_into(path):
    """A clone with a remote and a pushed feature branch, at exactly `path`."""
    parent = path.parent
    tmp = Path(tempfile.mkdtemp(dir=str(parent)))
    _origin, clone = make_remote_and_clone(tmp)
    shutil.move(str(clone), str(path))
    sh(path, "remote", "set-url", "origin", str(_origin))
    return path


class Records(Isolated):
    def spec(self, **kw):
        t = D.DocTarget("BE", "shop-backend", "/w/shop-backend", "feat/x", ("a.md", "b.md"))
        return D.DocJobSpec(project_dir="/w", targets=(t,), origin="cli", **kw)

    def test_a_document_spec_survives_a_record(self):
        original = self.spec(requirements_path="reqs", model="m", do_push=False, dry_run=True,
                             custom_instructions="x")
        back = CJ.spec_from_dict(json.loads(json.dumps(CJ.spec_to_dict(original))))
        self.assertEqual(back, original)
        self.assertEqual(back.kind, "docs")

    def test_pull_request_records_are_unchanged(self):
        pr = J.JobSpec(project_dir="/p", repo_name="r", pr_id="7")
        self.assertNotIn("kind", CJ.spec_to_dict(pr))
        self.assertEqual(CJ.spec_from_dict(CJ.spec_to_dict(pr)).pr_id, "7")

    def test_unusable_document_records_are_rejected(self):
        for bad in ({"kind": "docs"}, {"kind": "docs", "project_dir": "/p", "targets": []},
                    {"kind": "docs", "project_dir": "/p",
                     "targets": [{"local_repo": "/x", "branch": "b", "files": []}]}):
            with self.assertRaises(ValueError, msg=str(bad)):
                CJ.spec_from_dict(bad)

    def test_label_and_branches(self):
        CJ.create(self.spec(), "d0c001")
        rec = CJ.load("d0c001")
        self.assertEqual(CJ.label(rec), "Docs · Backend · 2 files")
        self.assertEqual(rec["branches"], "Backend: feat/x")

    def test_duplicates_are_per_kind_and_per_documents(self):
        CJ.create(self.spec(), "d0c002")
        self.assertEqual(CJ.find_duplicate(self.spec())["id"], "d0c002")
        other = D.DocJobSpec("/w", (D.DocTarget("BE", "shop-backend", "/w/shop-backend",
                                                "feat/x", ("c.md",)),))
        self.assertIsNone(CJ.find_duplicate(other))
        self.assertIsNone(CJ.find_duplicate(J.JobSpec("/p", "shop-backend", "7")))
        CJ.create(J.JobSpec("/p", "r", "7"), "a11e02")
        self.assertEqual(CJ.find_duplicate(J.JobSpec("/p", "r", "7"))["id"], "a11e02")
        self.assertIsNone(CJ.find_active("shop-backend", "7"))        # docs never match a PR


class Running(Isolated):
    """The whole command with real git and a canned reviewer."""

    def setUp(self):
        super().setUp()
        self.origin, self.clone = fixture(self.home / "one")
        self.calls = []
        saved = (O.ensure_bundled_agents, D._run_agent, O._continue_turn)
        O.ensure_bundled_agents = lambda *a, **k: ["doc-reviewer"]
        D._run_agent = self.fake_agent
        self.addCleanup(lambda: (setattr(O, "ensure_bundled_agents", saved[0]),
                                 setattr(D, "_run_agent", saved[1]),
                                 setattr(O, "_continue_turn", saved[2])))

    def fake_agent(self, prompt, project_dir, emit, model, control, meter):
        import re
        self.calls.append(prompt)
        emit("reading …", "agent")
        n = int(re.search(r"Start new comments at DR-(\d+)", prompt).group(1))
        return 0, REPORT.format(n=n, m=n + 1), "sess-9"

    def run_cmd(self, *argv, cwd=None):
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(str(cwd or self.clone))
        try:
            with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err), \
                    mock.patch.object(sys.stdin, "isatty", return_value=False):
                code = P.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def remote(self, path="docs/spec.md"):
        return sh(self.origin, "show", f"feat/x:{path}")

    def test_prism_docs_reviews_commits_and_pushes(self):
        code, out, err = self.run_cmd("docs", "docs/spec.md")
        self.assertEqual(code, 0, err + out)
        self.assertIn("PRISM review comments", self.remote())
        subject = sh(self.origin, "log", "-1", "--format=%s", "feat/x").strip()
        self.assertTrue(subject.startswith("PRISM-") and "review comments v1" in subject, subject)
        (rec,) = CJ.list_jobs()
        self.assertEqual((rec["status"], rec["committed"], rec["pushed"]), ("done", True, True))
        self.assertIn("Request changes", rec["verdict"])
        self.assertEqual(rec["session_id"], "sess-9")
        self.assertEqual(rec["spec"]["kind"], "docs")
        self.assertIn("PRISM job", CJ.read_log(rec["id"]))

    def test_dry_run_changes_nothing(self):
        before = sh(self.origin, "log", "--format=%H", "feat/x")
        code, _out, err = self.run_cmd("docs", "docs/spec.md", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual(sh(self.origin, "log", "--format=%H", "feat/x"), before)
        (rec,) = CJ.list_jobs()
        self.assertFalse(rec["committed"] or rec["pushed"])

    def test_no_push_keeps_the_remote_untouched(self):
        before = sh(self.origin, "log", "--format=%H", "feat/x")
        code, _o, err = self.run_cmd("docs", "docs/spec.md", "--no-push")
        self.assertEqual(code, 0, err)
        self.assertEqual(sh(self.origin, "log", "--format=%H", "feat/x"), before)
        (rec,) = CJ.list_jobs()
        self.assertEqual((rec["committed"], rec["pushed"]), (True, False))

    def test_the_users_clone_is_not_touched(self):
        (self.clone / "scratch.txt").write_text("wip", encoding="utf-8")
        self.run_cmd("docs", "docs/spec.md")
        self.assertEqual(sh(self.clone, "branch", "--show-current").strip(), "feat/x")
        self.assertTrue((self.clone / "scratch.txt").exists())
        self.assertNotIn("PRISM review comments",
                         (self.clone / "docs" / "spec.md").read_text(encoding="utf-8"))

    def test_a_failure_is_recorded_and_exits_1(self):
        code, out, _err = self.run_cmd("docs", "docs/typo.md")
        self.assertEqual(code, 1)
        self.assertIn("typo.md", out)
        (rec,) = CJ.list_jobs()
        self.assertEqual(rec["status"], "error")
        self.assertEqual(self.calls, [])

    def test_a_usage_error_exits_2_without_a_job(self):
        code, _out, err = self.run_cmd("docs")
        self.assertEqual(code, 2)
        self.assertIn("Say which document", err)
        self.assertEqual(CJ.list_jobs(), [])

    def test_jobs_listing_shows_documents_and_the_result(self):
        self.run_cmd("docs", "docs/spec.md")
        code, out, _e = self.run_cmd("jobs", "--all")
        self.assertEqual(code, 0)
        self.assertIn("TARGET", out)
        self.assertIn("Docs · clone · 1 file", out)         # the clone's folder name, one document
        self.assertIn("pushed", out)

    def test_pull_request_listing_is_unchanged(self):
        CJ.create(J.JobSpec("/p", "acme", "9"), "a11e09")
        CJ.finish("a11e09", J.DONE, merged=True, verdict="✅ Approve")
        _c, out, _e = self.run_cmd("jobs", "--all")
        self.assertIn("PULL REQUEST", out)
        self.assertNotIn("TARGET", out)
        self.assertIn("merged", out)

    def test_retry_reviews_again_incrementally_and_reuses_the_job(self):
        self.run_cmd("docs", "docs/spec.md")
        (rec,) = CJ.list_jobs()
        # the author edits and pushes between the two reviews
        sh(self.clone, "fetch", "-q", "origin")
        sh(self.clone, "reset", "-q", "--hard", "origin/feat/x")
        p = self.clone / "docs" / "spec.md"
        p.write_text(p.read_text(encoding="utf-8").replace("Body.", "Body, revised."),
                     encoding="utf-8")
        sh(self.clone, "commit", "-qam", "revise")
        sh(self.clone, "push", "-q", "origin", "feat/x")
        code, _o, err = self.run_cmd("retry", rec["id"])
        self.assertEqual(code, 0, err)
        self.assertIn("PREVIOUS REVIEW", self.calls[-1])
        _b, runs, _a = D.split_document(self.remote())
        self.assertEqual([r.meta["v"] for r in runs], [2, 1])
        self.assertEqual(len(CJ.list_jobs()), 1)                    # same record, rerun in place

    def test_ask_continues_the_document_reviewer(self):
        self.run_cmd("docs", "docs/spec.md")
        (rec,) = CJ.list_jobs()
        seen = {}

        def turn(prompt, project_dir, sid, emit, model, control, what, agent=None, **k):
            seen.update(agent=agent, sid=sid, prompt=prompt)
            emit("Because REQ-7 has no spec.", "agent")
            return 0, "Because REQ-7 has no spec.", sid
        O._continue_turn = turn
        code, out, err = self.run_cmd("ask", rec["id"], "why", "is", "DR-1", "a", "blocker?")
        self.assertEqual(code, 0, err)
        self.assertEqual((seen["agent"], seen["sid"]), ("doc-reviewer", "sess-9"))
        self.assertIn("why is DR-1 a blocker?", seen["prompt"])
        self.assertIn("Follow-up answered", out)

    def test_a_second_run_on_the_same_documents_is_refused_while_one_is_live(self):
        CJ.create(P.build_docs_spec(args(files=["docs/spec.md"]), cwd=self.clone), "d0c0aa")
        code, _o, err = self.run_cmd("docs", "docs/spec.md")
        self.assertEqual(code, 2)
        self.assertIn("already being reviewed", err)

    def test_the_two_repository_form_runs_both(self):
        top = self.home / "pair"
        top.mkdir()
        be = make_remote_and_clone_into(top / "shop-backend")
        fe = make_remote_and_clone_into(top / "shop-frontend")
        code, _o, err = self.run_cmd("docs", "--be", "docs/spec.md", "--fe", "docs/spec.md",
                                     cwd=top)
        self.assertEqual(code, 0, err)
        for clone in (be, fe):
            origin = sh(clone, "remote", "get-url", "origin").strip()
            self.assertTrue(sh(origin, "log", "-1", "--format=%s", "feat/x").startswith("PRISM-"))
        (rec,) = CJ.list_jobs()
        self.assertEqual(CJ.label(rec), "Docs · Backend + Frontend · 2 files")

    def test_config_keys_for_documents(self):
        for key, value in (("requirements", "reqs"), ("commit", "no"), ("push", "no")):
            self.assertEqual(self.run_cmd("config", "set", key, value)[0], 0)
        cfg = C.get_cli()
        self.assertEqual((cfg["requirements"], cfg["commit"], cfg["push"]), ("reqs", False, False))
        _c, out, _e = self.run_cmd("config")
        for key in ("requirements", "commit", "push"):
            self.assertIn(key, out)


class Detached(Isolated):
    """A real second process, as with `--detach` over ssh."""

    def test_detached_document_job_registers_with_its_spec_and_runs_to_the_end(self):
        _origin, clone = fixture(self.home / "one")
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "git").symlink_to(shutil.which("git"))
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "PATH": str(bin_dir),
               "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        run = subprocess.run(
            [sys.executable, str(ROOT / "prism_cli.py"), "docs", "docs/spec.md", "--path",
             str(clone), "--detach", "--dry-run"],
            env=env, cwd=str(clone), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("in the background as job", run.stdout)
        job_id = run.stdout.split("as job ")[1].split(".")[0]
        path = self.home / ".prism" / "cli-jobs" / f"{job_id}.json"
        deadline, rec = time.time() + 60, None
        while time.time() < deadline:
            rec = json.loads(path.read_text(encoding="utf-8"))
            if rec["status"] in (J.ERROR, J.DONE, J.STOPPED):
                break
            time.sleep(0.3)
        # No reviewer engine on this PATH, so it ends in an error - what matters is that a
        # separate process rebuilt the document job from the forwarded spec and ran it.
        self.assertIn(rec["status"], (J.ERROR, J.DONE), rec)
        self.assertEqual(rec["spec"]["kind"], "docs")
        self.assertEqual(rec["spec"]["targets"][0]["files"], ["docs/spec.md"])
        self.assertNotEqual(rec["pid"], os.getpid())
        self.assertIn("▸ Documentation review", CJ.read_log(job_id))


class Parser(unittest.TestCase):
    def test_docs_is_a_command_with_a_handler(self):
        self.assertIn("docs", P.COMMANDS)
        self.assertIs(P.HANDLERS["docs"], P.cmd_docs)

    def test_the_parsed_namespace_matches_what_build_docs_spec_reads(self):
        ns = P.build_parser().parse_args(
            ["docs", "a.md", "b.md", "--branch", "x", "--no-commit", "--requirements", "r",
             "-i", "t", "--dry-run", "-d", "-q"])
        self.assertEqual((ns.files, ns.branch, ns.commit, ns.requirements, ns.dry_run),
                         (["a.md", "b.md"], "x", False, "r", True))
        ns = P.build_parser().parse_args(["docs", "--be", "a.md", "b.md", "--fe", "c.md",
                                          "--be-branch", "p", "--fe-clone", "/f"])
        self.assertEqual((ns.be, ns.fe, ns.be_branch, ns.fe_clone, ns.files),
                         (["a.md", "b.md"], ["c.md"], "p", "/f", []))

    def test_stage_names_cover_the_document_stages(self):
        for stage in (D.STAGE_DOC_REVIEW, D.STAGE_DOC_WRITE, D.STAGE_DOC_COMMIT, D.STAGE_DOC_PUSH):
            self.assertIn(stage, P.STAGE_NAMES)

    def test_the_cli_still_imports_without_tk(self):
        code = ("import sys; sys.path.insert(0, %r); import prism_cli, docreview, cli_jobs; "
                "assert 'tkinter' not in sys.modules" % str(ROOT))
        self.assertEqual(subprocess.run([sys.executable, "-c", code]).returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
