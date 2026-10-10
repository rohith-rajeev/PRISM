"""Command line, CLI job registry and folder chooser - no Tk, no network, no AWS.

Every test points CONFIG_DIR at a temporary directory so nothing here touches
the real ~/.prism of the machine running the suite.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cli_jobs as CJ  # noqa: E402
import config as C  # noqa: E402
import folderpicker as FP  # noqa: E402
import jobs as J  # noqa: E402
import prism_cli as P  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        orig = (C.CONFIG_DIR, C.CONFIG_PATH)
        C.CONFIG_DIR = self.home / ".prism"
        C.CONFIG_PATH = C.CONFIG_DIR / "config.json"
        self.addCleanup(lambda: setattr(C, "CONFIG_DIR", orig[0]))
        self.addCleanup(lambda: setattr(C, "CONFIG_PATH", orig[1]))

    def make_repo(self, name, remote=None, parent=None):
        path = (parent or self.home) / name
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        if remote:
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote],
                           check=True)
        return path


def spec(pr="7", repo="acme-be", **kw):
    return J.JobSpec(project_dir="/tmp/p", repo_name=repo, pr_id=pr, **kw)


class ConfigTests(Isolated):
    def test_defaults_when_nothing_saved(self):
        cfg = C.get_cli()
        self.assertEqual(cfg["region"], "")
        self.assertTrue(cfg["review"] and cfg["merge"])
        self.assertFalse(cfg["dry_run"])

    def test_round_trip_and_unset(self):
        C.set_cli(region="eu-west-1", merge=False, bogus="x")
        cfg = C.get_cli()
        self.assertEqual((cfg["region"], cfg["merge"]), ("eu-west-1", False))
        self.assertNotIn("bogus", cfg)
        C.unset_cli("region")
        self.assertEqual(C.get_cli()["region"], "")

    def test_wrong_types_fall_back_to_defaults(self):
        C.save({"cli": {"merge": "yes", "region": 5, "review": False}})
        cfg = C.get_cli()
        self.assertTrue(cfg["merge"])       # a string is not a bool
        self.assertEqual(cfg["region"], "")
        self.assertFalse(cfg["review"])

    def test_cli_settings_leave_other_settings_alone(self):
        C.set_webhook_url("https://example.invalid/h")
        C.set_cli(region="ap-south-1")
        self.assertEqual(C.get_webhook_url(), "https://example.invalid/h")


class RemoteParsing(unittest.TestCase):
    def test_known_shapes(self):
        cases = {
            "codecommit::eu-west-1://my-repo": ("my-repo", "eu-west-1"),
            "codecommit://prof@my-repo": ("my-repo", ""),
            "https://git-codecommit.us-east-2.amazonaws.com/v1/repos/svc-api":
                ("svc-api", "us-east-2"),
            "ssh://git-codecommit.ap-south-1.amazonaws.com/v1/repos/web": ("web", "ap-south-1"),
            "https://github.com/x/y.git": ("", ""),
            "": ("", ""),
        }
        for url, want in cases.items():
            self.assertEqual(P.parse_remote(url), want, url)


class TargetResolution(Isolated):
    def test_inside_a_clone_uses_it_and_its_remote(self):
        repo = self.make_repo("whatever", "codecommit::eu-west-1://real-name")
        sub = repo / "src"
        sub.mkdir()
        project, name, local, region = P.resolve_target(cwd=str(sub), cfg=C.get_cli())
        self.assertEqual((Path(project).resolve(), name, region),
                         (repo.resolve(), "real-name", "eu-west-1"))
        self.assertEqual(Path(local).resolve(), repo.resolve())

    def test_falls_back_to_folder_name_without_a_remote(self):
        repo = self.make_repo("plain-repo")
        _, name, _, region = P.resolve_target(cwd=str(repo), cfg=C.get_cli())
        self.assertEqual((name, region), ("plain-repo", ""))

    def test_one_clone_below_is_chosen(self):
        root = self.home / "work"
        self.make_repo("only", parent=root)
        project, name, local, _ = P.resolve_target(cwd=str(root), cfg=C.get_cli())
        self.assertEqual(name, "only")
        self.assertTrue(local.endswith("only"))

    def test_several_clones_never_guess(self):
        root = self.home / "work"
        self.make_repo("a-be", parent=root)
        self.make_repo("a-fe", parent=root)
        with self.assertRaises(P.UsageError) as cm:
            P.resolve_target(cwd=str(root), cfg=C.get_cli())
        self.assertIn("--repo", str(cm.exception))
        _, name, local, _ = P.resolve_target(repo="a-fe", cwd=str(root), cfg=C.get_cli())
        self.assertEqual(name, "a-fe")
        self.assertTrue(local.endswith("a-fe"))

    def test_no_clone_at_all_says_what_to_do(self):
        empty = self.home / "empty"
        empty.mkdir()
        with self.assertRaises(P.UsageError):
            P.resolve_target(cwd=str(empty), cfg=C.get_cli())

    def test_missing_folder_is_a_usage_error(self):
        with self.assertRaises(P.UsageError):
            P.resolve_target(path=str(self.home / "nope"), cfg=C.get_cli())

    def test_saved_defaults_are_used(self):
        repo = self.make_repo("saved")
        _, name, _, _ = P.resolve_target(
            cwd="/", cfg=dict(C.get_cli(), project_dir=str(repo), repo="from-config"))
        self.assertEqual(name, "from-config")


def parse(*argv):
    return P.build_parser().parse_args(list(argv))


class BuildSpec(Isolated):
    def test_flags_and_precedence(self):
        repo = self.make_repo("r", "codecommit::eu-west-1://r")
        C.set_cli(merge=False, model="from/config")
        # config only: merge off, region from the remote
        s = P.build_spec(parse("run", "#42", "--path", str(repo)))
        self.assertEqual((s.pr_id, s.do_merge, s.region, s.model),
                         ("42", False, "eu-west-1", "from/config"))
        self.assertEqual(s.origin, "cli")
        # an explicit flag beats the saved default; --region beats the remote
        s = P.build_spec(parse("run", "42", "--path", str(repo), "--merge",
                               "--region", "us-west-2", "--model", "x/y", "--dry-run"))
        self.assertEqual((s.do_merge, s.region, s.model, s.dry_run),
                         (True, "us-west-2", "x/y", True))

    def test_no_review_forces_describe_off(self):
        repo = self.make_repo("r2")
        s = P.build_spec(parse("run", "1", "--path", str(repo), "--no-review"))
        self.assertFalse(s.do_review)
        self.assertFalse(s.do_update_desc)

    def test_pr_id_must_be_a_number(self):
        repo = self.make_repo("r3")
        with self.assertRaises(P.UsageError):
            P.build_spec(parse("run", "abc", "--path", str(repo)))

    def test_instructions_file(self):
        repo = self.make_repo("r4")
        note = self.home / "note.txt"
        note.write_text("  check the migration  \n", encoding="utf-8")
        s = P.build_spec(parse("run", "1", "--path", str(repo),
                               "--instructions-file", str(note)))
        self.assertEqual(s.custom_instructions, "check the migration")

    def test_spec_is_valid_pipeline_input(self):
        import inspect
        from orchestrator import full_pipeline
        repo = self.make_repo("r5")
        s = P.build_spec(parse("run", "1", "--path", str(repo)))
        self.assertTrue(set(s.pipeline_kwargs()) <= set(inspect.signature(full_pipeline).parameters))


class Registry(Isolated):
    def test_create_load_update_finish(self):
        rec = CJ.create(spec(), "abc123")
        self.assertEqual(CJ.load("abc123")["status"], J.RUNNING)
        self.assertTrue(CJ.is_active(rec))
        CJ.update("abc123", stage="Review")
        CJ.finish("abc123", J.DONE, merged=True)
        rec = CJ.load("abc123")
        self.assertEqual((rec["stage"], rec["merged"], CJ.is_active(rec)), ("Review", True, False))

    def test_spec_round_trip(self):
        original = spec("9", do_merge=False, region="eu-west-1", custom_instructions="x")
        back = CJ.spec_from_dict(CJ.spec_to_dict(original))
        self.assertEqual((back.pr_id, back.do_merge, back.region, back.custom_instructions,
                          back.origin), ("9", False, "eu-west-1", "x", "cli"))

    def test_unusable_spec_is_rejected(self):
        with self.assertRaises(ValueError):
            CJ.spec_from_dict({"repo_name": "x"})

    def test_silent_job_is_interrupted_not_running(self):
        CJ.create(spec(), "dead01")
        rec = CJ.load("dead01")
        self.assertEqual(CJ.status_of(rec, now=rec["heartbeat"] + CJ.STALE_AFTER + 1),
                         CJ.INTERRUPTED)
        self.assertEqual(CJ.status_of(rec, now=rec["heartbeat"] + 2), J.RUNNING)
        self.assertFalse(CJ.is_active(rec, now=rec["heartbeat"] + CJ.STALE_AFTER + 1))

    def test_stop_is_a_file_not_a_write(self):
        CJ.create(spec(), "5709a1")
        before = CJ.load("5709a1")
        self.assertTrue(CJ.request_stop("5709a1"))
        self.assertTrue(CJ.stop_requested("5709a1"))
        after = CJ.load("5709a1")
        self.assertEqual(before, after)                       # record untouched
        self.assertEqual(CJ.status_of(after), J.STOPPING)     # but reported stopping
        CJ.clear_stop("5709a1")
        self.assertFalse(CJ.stop_requested("5709a1"))

    def test_cannot_stop_what_is_not_running(self):
        CJ.create(spec(), "d0ae01")
        CJ.finish("d0ae01", J.DONE)
        self.assertFalse(CJ.request_stop("d0ae01"))
        self.assertFalse(CJ.request_stop("nothere"))

    def test_duplicate_detection_only_for_live_jobs(self):
        CJ.create(spec("5"), "a11e01")
        self.assertEqual(CJ.find_active("acme-be", "5")["id"], "a11e01")
        self.assertIsNone(CJ.find_active("acme-be", "6"))
        CJ.finish("a11e01", J.DONE)
        self.assertIsNone(CJ.find_active("acme-be", "5"))

    def test_remove_refuses_live_jobs(self):
        CJ.create(spec(), "a11e02")
        self.assertFalse(CJ.remove("a11e02"))
        CJ.finish("a11e02", J.ERROR, error="x")
        self.assertTrue(CJ.remove("a11e02"))
        self.assertIsNone(CJ.load("a11e02"))

    def test_ids_cannot_escape_the_directory(self):
        for bad in ("../x", "a/b", "", "ZZ", None):
            self.assertIsNone(CJ.load(bad))
            self.assertFalse(CJ.stop_requested(bad))

    def test_unreadable_record_is_skipped(self):
        CJ.create(spec(), "900d01")
        (CJ.jobs_dir() / "bad000.json").write_text("{nope", encoding="utf-8")
        self.assertEqual([r["id"] for r in CJ.list_jobs()], ["900d01"])

    def test_counts_split_running_and_asking(self):
        CJ.create(spec("1"), "a00001")
        CJ.create(spec("2"), "a5c001")
        CJ.update("a5c001", status=J.NEEDS_INPUT)
        CJ.create(spec("3"), "f10001")
        CJ.finish("f10001", J.DONE)
        self.assertEqual(CJ.counts(), (1, 1))

    def test_prune_keeps_the_newest_finished(self):
        for n in range(5):
            CJ.create(spec(str(n)), f"0000{n}0")
            CJ.update(f"0000{n}0", started=1000 + n)
            CJ.finish(f"0000{n}0", J.DONE)
        CJ.create(spec("99"), "a11e99")
        CJ.prune(keep=2)
        ids = {r["id"] for r in CJ.list_jobs()}
        self.assertEqual(ids, {"a11e99", "000040", "000030"})


class Execute(Isolated):
    """The real execute() with the pipeline replaced by a stub."""

    def run_stub(self, runner, **kw):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = P.execute(spec(), "abc111", runner=runner, interactive=False, **kw)
        return code, out.getvalue()

    def test_success_is_recorded(self):
        def runner(emit, progress, control, ask, get_note, **kw):
            progress("review", "active")
            emit('{"source": "feat", "dest": "main", "author": "sam"}', "prmeta")
            emit("◆ Verdict: Approve  Impact: 3/10 low")
            emit('{"total": 1234}', "tokens")
            emit("sess-1", "session")
            return {"merged": True, "stopped": None, "tokens": {"total": 1234}}
        code, out = self.run_stub(runner)
        rec = CJ.load("abc111")
        self.assertEqual(code, 0)
        self.assertEqual((rec["status"], rec["merged"], rec["verdict"], rec["impact"],
                          rec["branches"], rec["author"], rec["session_id"]),
                         (J.DONE, True, "Approve", "3/10 low", "feat → main", "sam", "sess-1"))
        self.assertEqual(rec["tokens"], {"total": 1234})
        self.assertIn("◆ Verdict", out)
        self.assertNotIn("sess-1", out)           # structured lines are not transcript
        self.assertIn("Verdict", CJ.read_log("abc111"))

    def test_failure_is_recorded(self):
        def runner(**kw):
            raise RuntimeError("aws exploded")
        code, _ = self.run_stub(runner)
        rec = CJ.load("abc111")
        self.assertEqual((code, rec["status"], rec["error"]), (1, J.ERROR, "aws exploded"))

    def test_stop_file_cancels_the_run(self):
        def runner(emit, progress, control, **kw):
            CJ.request_stop("abc111")
            deadline = time.time() + 10
            while not control.cancelled() and time.time() < deadline:
                time.sleep(0.05)
            control.check()
            return {}
        code, _ = self.run_stub(runner)
        self.assertEqual((code, CJ.load("abc111")["status"]), (130, J.STOPPED))
        self.assertFalse(CJ.stop_requested("abc111"))      # consumed, so a retry starts clean

    def test_question_without_a_terminal_takes_the_cautious_answer(self):
        seen = {}

        def runner(emit, ask, **kw):
            seen["answer"] = ask("Merge this high-impact change?")
            return {}
        code, out = self.run_stub(runner)
        self.assertIsNone(seen["answer"])
        self.assertIn("no terminal", out)
        self.assertEqual(CJ.load("abc111")["status"], J.DONE)

    def test_duplicate_pr_is_refused(self):
        CJ.create(spec(), "07be01")
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            code = P.execute(spec(), "abc111", runner=lambda **k: {}, interactive=False)
        self.assertEqual(code, 2)
        self.assertIsNone(CJ.load("abc111"))

    def test_retry_reuses_the_id_and_starts_clean(self):
        CJ.create(spec(), "abc111")
        CJ.finish("abc111", J.ERROR, error="old")
        CJ.log_path("abc111").write_text("old log\n", encoding="utf-8")
        code, _ = self.run_stub(lambda **k: {"merged": False})
        rec = CJ.load("abc111")
        self.assertEqual((rec["status"], rec["error"]), (J.DONE, ""))
        self.assertNotIn("old log", CJ.read_log("abc111"))


class Commands(Isolated):
    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdout", out), mock.patch.object(sys, "stderr", err):
            code = P.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_jobs_lists_running_and_all(self):
        CJ.create(spec("1"), "aaaaaa")
        CJ.create(spec("2"), "bbbbbb")
        CJ.finish("bbbbbb", J.DONE, verdict="Approve", merged=True)
        code, out, _ = self.run_cmd("jobs")
        self.assertIn("aaaaaa", out)
        self.assertNotIn("bbbbbb", out)
        _, out, _ = self.run_cmd("jobs", "--all")
        self.assertIn("bbbbbb", out)
        self.assertIn("merged", out)
        _, out, _ = self.run_cmd("jobs", "--all", "--json")
        self.assertEqual({r["id"] for r in json.loads(out)}, {"aaaaaa", "bbbbbb"})

    def test_stop_and_retry_guards(self):
        CJ.create(spec("1"), "aaaaaa")
        code, _, err = self.run_cmd("retry", "aaaaaa")
        self.assertEqual(code, 2)
        self.assertIn("still running", err)
        code, out, _ = self.run_cmd("stop", "aaaaaa")
        self.assertEqual(code, 0)
        self.assertTrue(CJ.stop_requested("aaaaaa"))
        self.assertEqual(self.run_cmd("stop", "ffffff")[0], 2)

    def test_logs(self):
        CJ.create(spec(), "aaaaaa")
        CJ.log_path("aaaaaa").write_text("one\ntwo\nthree\n", encoding="utf-8")
        _, out, _ = self.run_cmd("logs", "aaaaaa", "-n", "2")
        self.assertEqual(out.strip().splitlines(), ["two", "three"])
        CJ.finish("aaaaaa", J.DONE)
        _, out, _ = self.run_cmd("logs", "aaaaaa", "-f")      # ends because the job did
        self.assertIn("three", out)

    def test_rm(self):
        CJ.create(spec(), "aaaaaa")
        self.assertEqual(self.run_cmd("rm", "aaaaaa")[0], 2)
        CJ.finish("aaaaaa", J.DONE)
        self.assertEqual(self.run_cmd("rm", "aaaaaa")[0], 0)
        self.assertIsNone(CJ.load("aaaaaa"))

    def test_config_commands(self):
        self.assertEqual(self.run_cmd("config", "set", "region", "eu-west-1")[0], 0)
        self.assertEqual(self.run_cmd("config", "set", "merge", "no")[0], 0)
        self.assertEqual(self.run_cmd("config", "set", "webhook", "https://h.invalid/x")[0], 0)
        cfg = C.get_cli()
        self.assertEqual((cfg["region"], cfg["merge"]), ("eu-west-1", False))
        self.assertEqual(C.get_webhook_url(), "https://h.invalid/x")
        _, out, _ = self.run_cmd("config")
        self.assertIn("eu-west-1", out)
        self.assertEqual(self.run_cmd("config", "unset", "region")[0], 0)
        self.assertEqual(C.get_cli()["region"], "")
        self.assertEqual(self.run_cmd("config", "set", "nonsense", "1")[0], 2)
        self.assertEqual(self.run_cmd("config", "set", "merge", "maybe")[0], 2)
        self.assertEqual(self.run_cmd("config", "set", "project_dir", "/no/such/dir")[0], 2)

    def test_version_and_help(self):
        from version import __version__
        self.assertIn(__version__, self.run_cmd("version")[1])
        self.assertEqual(self.run_cmd()[0], 0)

    def test_every_advertised_command_has_a_handler(self):
        for word in P.COMMANDS:
            self.assertTrue(word in P.HANDLERS or word in ("version", "help"), word)


class DetachedRun(Isolated):
    """A real second process: the part that must survive an ssh logout."""

    def test_detached_job_registers_and_can_be_stopped(self):
        repo = self.make_repo("detached")
        # Hermetic: a PATH holding only git and an `aws` that always fails, so
        # the child can reach neither a real AWS account nor a real reviewer
        # engine, whatever is installed on the machine running the suite.
        bin_dir = self.home / "bin"
        bin_dir.mkdir()
        (bin_dir / "aws").write_text("#!/bin/sh\nexit 255\n")
        (bin_dir / "aws").chmod(0o755)
        (bin_dir / "git").symlink_to(shutil.which("git"))
        env = {"HOME": str(self.home), "USERPROFILE": str(self.home),
               "PATH": str(bin_dir), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
        run = subprocess.run(
            [sys.executable, str(ROOT / "prism_cli.py"), "run", "3", "--path", str(repo),
             "--detach", "--dry-run"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("in the background as job", run.stdout)
        job_id = run.stdout.split("as job ")[1].split(".")[0]
        # With no engine and no AWS the run ends in an error - that is fine; what
        # matters is that a separate process ran it to completion and recorded it.
        deadline = time.time() + 60
        rec = None
        while time.time() < deadline:
            rec = json.loads((self.home / ".prism" / "cli-jobs" / f"{job_id}.json").read_text())
            if rec["status"] in (J.ERROR, J.DONE, J.STOPPED):
                break
            time.sleep(0.3)
        self.assertIn(rec["status"], (J.ERROR, J.DONE, J.STOPPED), rec)
        self.assertNotEqual(rec["pid"], os.getpid())
        self.assertTrue((self.home / ".prism" / "cli-jobs" / f"{job_id}.log").is_file())


class FolderChooser(unittest.TestCase):
    def which(self, *present):
        return lambda name: f"/usr/bin/{name}" if name in present else None

    def test_prefers_zenity_then_kdialog_then_yad(self):
        home = str(Path.home())
        self.assertEqual(FP.chooser_command("T", home, self.which("zenity", "kdialog"))[0], "zenity")
        self.assertEqual(FP.chooser_command("T", home, self.which("kdialog", "yad"))[0], "kdialog")
        self.assertEqual(FP.chooser_command("T", home, self.which("yad"))[0], "yad")
        self.assertIsNone(FP.chooser_command("T", home, self.which()))

    def test_starts_in_the_nearest_existing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = FP.chooser_command("T", os.path.join(tmp, "gone", "deeper"), self.which("zenity"))
            self.assertEqual(cmd[-1], tmp + "/")

    def result(self, code, out=""):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout=out)

    def test_outcomes(self):
        kw = dict(platform="linux", which=self.which("zenity"))
        self.assertEqual(FP.pick_folder(run=self.result(0, "/home/x/proj\n"), **kw), "/home/x/proj")
        self.assertEqual(FP.pick_folder(run=self.result(1), **kw), "")      # cancelled
        self.assertIsNone(FP.pick_folder(run=self.result(255), **kw))        # could not open

    def test_other_platforms_and_missing_chooser_defer_to_tk(self):
        self.assertIsNone(FP.pick_folder(platform="darwin", which=self.which("zenity")))
        self.assertIsNone(FP.pick_folder(platform="win32", which=self.which("zenity")))
        self.assertIsNone(FP.pick_folder(platform="linux", which=self.which()))
        self.assertFalse(FP.native_available("darwin", self.which("zenity")))

    def test_a_missing_binary_defers_to_tk(self):
        def boom(*a, **k):
            raise FileNotFoundError
        self.assertIsNone(FP.pick_folder(platform="linux", which=self.which("zenity"), run=boom))

    def test_bundled_library_paths_are_not_handed_to_zenity(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI123",
                                          "LD_LIBRARY_PATH_ORIG": "/usr/lib/x"}):
            self.assertEqual(FP._clean_env()["LD_LIBRARY_PATH"], "/usr/lib/x")
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI123"}), \
                mock.patch.object(sys, "frozen", True, create=True):
            self.assertNotIn("LD_LIBRARY_PATH", FP._clean_env())


class ManPage(Isolated):
    page = (ROOT / "man" / "prism.1").read_text(encoding="utf-8")

    def long_options(self, parser):
        import argparse
        found = set()
        stack = [parser]
        while stack:
            p = stack.pop()
            for action in p._actions:
                if isinstance(action, argparse._SubParsersAction):
                    stack.extend(action.choices.values())
                elif action.help is not argparse.SUPPRESS:
                    found.update(o for o in action.option_strings if o.startswith("--"))
        found.discard("--help")
        return found

    def test_every_command_is_documented(self):
        for word in P.COMMANDS:
            self.assertRegex(self.page, r'(?m)^\.TP\n\.B[IR]? "?%s\b' % word)

    def test_every_option_is_documented(self):
        flat = self.page.replace("\\-", "-")
        for option in self.long_options(P.build_parser()):
            self.assertIn(option, flat, option)

    def test_every_config_key_is_documented(self):
        for key in P.CONFIG_KEYS_HELP:
            self.assertIn(key, self.page, key)

    def test_page_is_well_formed_roff(self):
        if not shutil.which("groff"):
            self.skipTest("groff is not installed")
        out = subprocess.run(["groff", "-man", "-ww", "-z", str(ROOT / "man" / "prism.1")],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
        self.assertEqual(out.stderr.strip(), "")

    def test_man_command_installs_it(self):
        target = self.home / "man1"
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = P.main(["man", "--dir", str(target)])
        self.assertEqual(code, 0)
        self.assertEqual((target / "prism.1").read_text(encoding="utf-8"), self.page)


class Entrypoints(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "the launcher is a POSIX shell script")
    def test_launcher_script_runs_the_cli(self):
        out = subprocess.run([str(ROOT / "prism"), "version"], stdout=subprocess.PIPE,
                             universal_newlines=True, timeout=60)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.startswith("PRISM "))

    def test_app_dispatches_commands_without_opening_a_window(self):
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("this Python has no tkinter; app.py needs it")
        out = subprocess.run([sys.executable, str(ROOT / "app.py"), "version"],
                             stdout=subprocess.PIPE, universal_newlines=True, timeout=60)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.startswith("PRISM "))

    def test_cli_imports_without_tk(self):
        code = ("import sys; sys.modules['tkinter'] = None; sys.path.insert(0, %r); "
                "import prism_cli, cli_jobs, folderpicker" % str(ROOT))
        self.assertEqual(subprocess.run([sys.executable, "-c", code]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
