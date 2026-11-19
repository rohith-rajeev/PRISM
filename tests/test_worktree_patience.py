"""Creating the review worktree: slow checkouts are waited for, hung ones are cut
and retried, and fix-up version numbers (6.4.1) sort correctly."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import docreview as D
import version


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)


class Patient(unittest.TestCase):
    def test_a_quick_command_returns_its_output(self):
        rc, text = D._git_patient(".", lambda *_: None, "x", "--version")
        self.assertEqual(rc, 0)
        self.assertIn("git version", text)

    def test_a_failing_command_reports_why(self):
        rc, text = D._git_patient(".", lambda *_: None, "x", "no-such-subcommand")
        self.assertNotEqual(rc, 0)
        self.assertTrue(text)

    def test_an_overrunning_command_is_stopped_and_reported_as_none(self):
        beats = []
        with mock.patch.object(D, "CHECKOUT_BEAT", 0.2):
            rc, _ = D._git_patient(".", beats.append, "slow",
                                   "-c", "alias.nap=!sleep 30", "nap", timeout=0.7)
        if os.name == "nt":
            self.skipTest("shell alias differs on Windows")
        self.assertIsNone(rc)
        self.assertTrue(beats, "a heartbeat should have been emitted")


class WorktreeRetry(unittest.TestCase):
    def _target(self, tmp):
        repo = Path(tmp) / "repo"
        repo.mkdir()
        _git(repo, "init", "-q")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
             "--allow-empty", "-m", "x")
        t = mock.Mock(role="BE", label="Backend", branch="main", repo_name="r",
                      local_repo=str(repo))
        return t, repo

    def test_a_first_timeout_is_cleared_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            t, repo = self._target(tmp)
            wt = D._Worktree(t, "r1", lambda *_: None)
            wt.root = Path(tmp) / "wt"
            wt.root.mkdir()
            wt.path = wt.root / "w"
            wt.ref = "HEAD"
            real, calls = D._git_patient, []

            def flaky(*a, **k):
                calls.append(1)
                return (None, "") if len(calls) == 1 else real(*a, **k)

            with mock.patch.object(D, "_git_patient", flaky):
                wt._add_worktree()
            self.assertEqual(len(calls), 2)
            self.assertTrue((wt.path / ".git").exists())

    def test_two_timeouts_raise_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            t, repo = self._target(tmp)
            wt = D._Worktree(t, "r2", lambda *_: None)
            wt.root = Path(tmp) / "wt"
            wt.root.mkdir()
            wt.path = wt.root / "w"
            wt.ref = "HEAD"
            with mock.patch.object(D, "_git_patient", lambda *a, **k: (None, "")):
                with self.assertRaises(RuntimeError) as cm:
                    wt._add_worktree()
            self.assertIn("minutes", str(cm.exception))


class FixUpVersions(unittest.TestCase):
    def test_ordering_and_next(self):
        self.assertGreater(version.parse("v6.4.1"), version.parse("6.4"))
        self.assertLess(version.parse("6.4.1"), version.parse("6.5"))
        self.assertEqual(version.next_version("v6.4.1", "v6.4"), "6.5")
        self.assertEqual(version.next_version("v6.9.2"), "7.0")

    def test_updater_sees_a_fix_up_as_newer(self):
        import updater
        self.assertTrue(updater.is_newer("6.4.1", "6.4"))
        self.assertFalse(updater.is_newer("6.4", "6.4.1"))
        self.assertTrue(updater.is_newer("6.5", "6.4.1"))


if __name__ == "__main__":
    unittest.main()
