"""Setup checks and the shareable log bundle - no network, no engine, no Tk."""
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cli_jobs as CJ  # noqa: E402
import config as C  # noqa: E402
import diagnostics as DG  # noqa: E402
import prism_cli as P  # noqa: E402


class Redaction(unittest.TestCase):
    def test_credentials_are_removed(self):
        text = ("webhook https://chat.googleapis.com/v1/spaces/X/messages?key=K&token=T "
                "AKIAABCDEFGHIJKLMNOP Authorization: Bearer abcdef1234567890xyz "
                "password=hunter2 https://user:pw@host/repo sk-abcdefghijklmnopqrstu")
        out = DG.redact(text)
        for secret in ("googleapis", "AKIAABCDEFGHIJKLMNOP", "abcdef1234567890xyz",
                       "hunter2", "user:pw", "sk-abcdefghijkl"):
            self.assertNotIn(secret, out)

    def test_ordinary_text_is_untouched(self):
        line = "▸ Source branch: feature/login. tokens used: 1200, 4 files reviewed"
        self.assertEqual(DG.redact(line), line)

    def test_home_folder_is_hidden(self):
        home = str(Path.home())
        self.assertNotIn(home, DG.redact(f"cloned to {home}/work/repo"))

    def test_nested_record_values_are_scrubbed(self):
        out = DG._scrub({"spec": {"webhook_url": "https://x", "pr_id": "7",
                                  "note": "token=abc123"}})
        self.assertEqual(out["spec"]["webhook_url"], "<redacted>")
        self.assertEqual(out["spec"]["pr_id"], "7")
        self.assertNotIn("abc123", out["spec"]["note"])


class Bundle(unittest.TestCase):
    def test_zip_holds_scrubbed_log_job_and_checks(self):
        with tempfile.TemporaryDirectory() as d:
            checks = [DG.Check("aws CLI", DG.FAIL, "not found", "install it")]
            path = DG.export_bundle(Path(d) / "out", "abc123", "line\npassword=secret1\n",
                                    record={"id": "abc123", "spec": {"webhook_url": "u"}},
                                    checks=checks)
            self.assertEqual(path.suffix, ".zip")
            with zipfile.ZipFile(path) as z:
                self.assertEqual(sorted(z.namelist()),
                                 ["README.txt", "doctor.txt", "job.json", "log.txt"])
                self.assertNotIn("secret1", z.read("log.txt").decode())
                self.assertEqual(json.loads(z.read("job.json"))["spec"]["webhook_url"],
                                 "<redacted>")
                self.assertIn("aws CLI", z.read("doctor.txt").decode())
            self.assertEqual([p.name for p in Path(d).iterdir()], ["out.zip"])  # no temp left


class Doctor(unittest.TestCase):
    def test_missing_aws_is_a_failure_with_a_fix(self):
        real = DG.shutil.which
        with mock.patch.object(DG.shutil, "which",
                               lambda n, *a, **k: None if n == "aws" else real(n, *a, **k)):
            checks = DG._check_aws("us-east-1", deep=True)
        self.assertEqual(checks[0].status, DG.FAIL)
        self.assertIn("aws configure", checks[0].hint)

    def test_exit_code_reflects_failures(self):
        args = P.build_parser().parse_args(["doctor", "--quick"])
        bad = [DG.Check("x", DG.FAIL, "broken")]
        with mock.patch.object(DG, "run_checks", return_value=bad), \
                mock.patch("builtins.print"):
            self.assertEqual(P.cmd_doctor(args), P.EXIT_ERROR)
        with mock.patch.object(DG, "run_checks", return_value=[DG.Check("x", DG.OK)]), \
                mock.patch("builtins.print"):
            self.assertEqual(P.cmd_doctor(args), P.EXIT_OK)

    def test_quick_run_makes_no_network_calls(self):
        with mock.patch.object(DG, "_run", return_value=(0, "v1")) as run:
            DG.run_checks(deep=False)
        joined = " ".join(" ".join(map(str, c.args[0])) for c in run.call_args_list)
        for word in ("sts", "codecommit", "ls-remote", "models"):
            self.assertNotIn(word, joined)


class ExportCommand(unittest.TestCase):
    def test_exports_a_cli_job(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(C, "CONFIG_DIR", Path(d)):
                CJ.jobs_dir(create=True)
                (CJ.jobs_dir() / "abc123.json").write_text(json.dumps({"id": "abc123"}))
                CJ.log_path("abc123").write_text("hello\n", encoding="utf-8")
                out = Path(d) / "bundle.zip"
                args = P.build_parser().parse_args(
                    ["export", "abc123", "-o", str(out), "--no-checks"])
                with mock.patch("builtins.print"):
                    self.assertEqual(P.cmd_export(args), P.EXIT_OK)
                with zipfile.ZipFile(out) as z:
                    self.assertIn("hello", z.read("log.txt").decode())


if __name__ == "__main__":
    unittest.main()
