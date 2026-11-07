"""A packaged build must not break when its unpack folder disappears.

The failure behind these tests: "Bundled agents missing: /tmp/_MEI...". PyInstaller's
one-file build unpacks into a temp folder and deletes it on exit, so a PRISM started by
a process that then exited (the restart after an update), or one whose temp folder was
cleared while it was open, found its reviewer agents gone at the next review.
"""
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import bundle as B  # noqa: E402
import orchestrator as o  # noqa: E402

INHERITED = {"_MEIPASS2": "/tmp/_MEI9", "_PYI_APPLICATION_HOME_DIR": "/tmp/_MEI9",
             "HOME": "/home/me", "PATH": "/usr/bin"}


class Tmp(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)

    def unpacked(self, files=("pr-reviewer.md", "doc-reviewer.md")):
        d = self.root / "_MEIxyz" / "agents"
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_text(f"# {f}\n", encoding="utf-8")
        return d.parent


class CleanEnvironment(unittest.TestCase):
    def test_a_packaged_build_gets_a_clean_slate_and_keeps_everything_else(self):
        env = B.clean_env(INHERITED, frozen=True)
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("_MEIPASS2", env)
        self.assertFalse([k for k in env if k.startswith("_PYI_")])
        self.assertEqual((env["HOME"], env["PATH"]), ("/home/me", "/usr/bin"))

    def test_anything_else_is_unchanged_and_the_input_is_not_modified(self):
        self.assertEqual(B.clean_env(INHERITED, frozen=False), INHERITED)
        before = dict(INHERITED)
        B.clean_env(INHERITED, frozen=True)
        self.assertEqual(INHERITED, before)

    def test_the_two_existing_callers_use_it(self):
        import cli_jobs
        import updater
        with mock.patch.dict(os.environ, INHERITED, clear=True), \
                mock.patch.object(sys, "frozen", True, create=True):
            for env in (cli_jobs.child_env(), updater.relaunch_env()):
                self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
                self.assertNotIn("_MEIPASS2", env)


class StartupGuard(Tmp):
    def test_nothing_happens_outside_a_packaged_build(self):
        gone = self.root / "nowhere"
        execve = mock.Mock()
        self.assertFalse(B.restart_if_bundle_gone(gone, frozen=False, execve=execve))
        execve.assert_not_called()

    def test_nothing_happens_while_the_files_are_there(self):
        execve = mock.Mock()
        self.assertFalse(B.restart_if_bundle_gone(self.unpacked(), frozen=True, execve=execve))
        execve.assert_not_called()

    def test_a_build_whose_files_are_gone_restarts_itself_once_from_a_clean_slate(self):
        execve = mock.Mock()
        B.restart_if_bundle_gone(self.root / "_MEIgone", frozen=True, argv=["run", "7"],
                                 environ=INHERITED, execve=execve, windows=False)
        ((exe, args, env), _kw) = execve.call_args
        self.assertEqual(exe, sys.executable)
        self.assertEqual(args, [sys.executable, "run", "7"])           # same arguments
        self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertNotIn("_MEIPASS2", env)
        self.assertEqual(env[B.RESTARTED], "1")

    def test_it_never_restarts_twice(self):
        execve = mock.Mock()
        again = dict(INHERITED, **{B.RESTARTED: "1"})
        self.assertFalse(B.restart_if_bundle_gone(self.root / "_MEIgone", frozen=True,
                                                  environ=again, execve=execve, windows=False))
        execve.assert_not_called()

    def test_windows_runs_it_again_and_exits_with_its_status(self):
        call, exits = mock.Mock(return_value=3), mock.Mock()
        B.restart_if_bundle_gone(self.root / "_MEIgone", frozen=True, argv=["x"],
                                 environ=INHERITED, call=call, exit_=exits, windows=True)
        call.assert_called_once()
        exits.assert_called_once_with(3)

    def test_a_failed_restart_falls_through_instead_of_crashing(self):
        execve = mock.Mock(side_effect=OSError("no exec"))
        self.assertFalse(B.restart_if_bundle_gone(self.root / "_MEIgone", frozen=True,
                                                  environ=INHERITED, execve=execve,
                                                  windows=False))

    def test_the_guard_runs_before_tk_is_even_imported(self):
        """Tk's own files live in the unpack folder: a build missing them cannot get as
        far as `import tkinter`, so the guard has to be first."""
        src = (HERE.parent / "app.py").read_text(encoding="utf-8")
        guard = src.index("bundle.restart_if_bundle_gone()")
        self.assertLess(guard, src.index("import tkinter"))
        self.assertLess(guard, src.index("import collections"))


class DurableAgents(Tmp):
    def test_the_agents_are_copied_somewhere_that_outlives_the_temp_folder(self):
        dest = self.root / "durable"
        self.assertTrue(B.preserve(self.unpacked(), frozen=True, dest=dest))
        self.assertEqual(sorted(p.name for p in dest.glob("*.md")),
                         ["doc-reviewer.md", "pr-reviewer.md"])

    def test_it_only_acts_for_a_packaged_build_and_only_rewrites_what_changed(self):
        dest = self.root / "durable"
        self.assertFalse(B.preserve(self.unpacked(), frozen=False, dest=dest))
        self.assertFalse(dest.exists())
        B.preserve(self.unpacked(), frozen=True, dest=dest)
        mtime = (dest / "pr-reviewer.md").stat().st_mtime_ns
        B.preserve(self.unpacked(), frozen=True, dest=dest)
        self.assertEqual((dest / "pr-reviewer.md").stat().st_mtime_ns, mtime)
        (self.root / "_MEIxyz" / "agents" / "pr-reviewer.md").write_text("new", encoding="utf-8")
        B.preserve(self.root / "_MEIxyz", frozen=True, dest=dest)
        self.assertEqual((dest / "pr-reviewer.md").read_text(encoding="utf-8"), "new")

    def test_it_never_raises(self):
        blocker = self.root / "file"
        blocker.write_text("x", encoding="utf-8")
        self.assertFalse(B.preserve(self.unpacked(), frozen=True, dest=blocker / "sub"))
        self.assertFalse(B.preserve(self.root / "nowhere", frozen=True, dest=self.root / "d"))

    def test_the_unpacked_agents_win_and_the_durable_copy_is_the_fallback(self):
        durable = self.root / "durable"
        B.preserve(self.unpacked(), frozen=True, dest=durable)
        unpacked_agents = self.root / "_MEIxyz" / "agents"
        self.assertEqual(B.agents_dir(unpacked_agents, durable), unpacked_agents)
        self.assertEqual(B.agents_dir(self.root / "_MEIgone" / "agents", durable), durable)
        self.assertIsNone(B.agents_dir(self.root / "_MEIgone" / "agents", self.root / "none"))
        (self.root / "empty").mkdir()
        self.assertIsNone(B.agents_dir(self.root / "_MEIgone" / "agents", self.root / "empty"))


class EnsureAgentsSurvivesTheFolderGoing(Tmp):
    def setUp(self):
        super().setUp()
        importlib.reload(o)
        self.addCleanup(importlib.reload, o)
        self.project = self.root / "proj"
        self.project.mkdir()
        self.durable = self.root / "durable"

    def install(self, frozen):
        o.FROZEN = frozen
        o.AGENTS_DIR = self.root / "_MEIgone" / "agents"          # the folder that vanished
        with mock.patch.object(B, "durable_dir", return_value=self.durable):
            return o.ensure_bundled_agents(str(self.project), emit=lambda *a, **k: None)

    def test_a_vanished_unpack_folder_no_longer_breaks_a_review(self):
        B.preserve(self.unpacked(("pr-reviewer.md", "doc-reviewer.md", "conflict-analyst.md")),
                   frozen=True, dest=self.durable)
        names = self.install(frozen=True)
        self.assertIn("doc-reviewer", names)
        self.assertTrue((self.project / ".opencode" / "agents" / "pr-reviewer.md").is_file())

    def test_with_nothing_to_fall_back_on_the_error_says_what_to_do(self):
        with self.assertRaises(RuntimeError) as cm:
            self.install(frozen=True)
        msg = str(cm.exception)
        self.assertIn("Bundled agents missing", msg)
        self.assertIn("Quit PRISM and start it again", msg)

    def test_a_source_checkout_never_uses_the_durable_copy(self):
        B.preserve(self.unpacked(), frozen=True, dest=self.durable)
        with self.assertRaises(RuntimeError):
            self.install(frozen=False)

    def test_normal_operation_is_unchanged(self):
        importlib.reload(o)
        names = o.ensure_bundled_agents(str(self.project), emit=lambda *a, **k: None)
        self.assertIn("pr-reviewer", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
