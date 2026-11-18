import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import docreview as DR  # noqa: E402
import jobs as J  # noqa: E402
import winproc  # noqa: E402
from pathutil import native_any, native_path, native_rel, posix_rel  # noqa: E402


class SeparatorTests(unittest.TestCase):
    def test_relative_either_slash_gives_the_same_path_on_each_os(self):
        for typed in ("docs/decomposition/feature.md", "docs\\decomposition\\feature.md"):
            self.assertEqual(native_rel(typed, "/"), "docs/decomposition/feature.md")
            self.assertEqual(native_rel(typed, "\\"), "docs\\decomposition\\feature.md")

    def test_tidies_quotes_dot_slash_and_doubles(self):
        self.assertEqual(native_rel('  "./docs//a.md" ', "/"), "docs/a.md")
        self.assertEqual(native_rel(".\\docs\\\\a.md", "\\"), "docs\\a.md")

    def test_git_form_is_always_forward(self):
        self.assertEqual(posix_rel("docs\\a\\b.md"), "docs/a/b.md")

    def test_native_path_expands_home_and_keeps_unc(self):
        self.assertEqual(native_path("~/x/y", "/"), os.path.expanduser("~/x/y"))
        self.assertTrue(native_path("\\\\srv\\share\\a", "\\").startswith("\\\\srv"))
        self.assertEqual(native_path("", "/"), "")

    def test_native_any(self):
        self.assertEqual(native_any("C:/a/b", "\\"), "C:\\a\\b")
        self.assertEqual(native_any("req/folder", "/"), "req/folder")

    def test_specs_normalise_what_was_typed(self):
        t = DR.DocTarget("BE", "r", "~/x\\repo", "b", ("docs\\d\\f.md", "docs/d/f.md"))
        self.assertEqual(t.files, (os.path.join("docs", "d", "f.md"),))
        self.assertEqual(t.local_repo, os.path.join(os.path.expanduser("~"), "x", "repo")
                         if os.sep == "\\" else os.path.expanduser("~") + "/x/repo")
        spec = J.JobSpec(project_dir="a\\b" if os.sep == "\\" else "a/b", repo_name="r", pr_id="1")
        self.assertEqual(spec.project_dir, os.path.join("a", "b"))

    def test_parse_files_accepts_both(self):
        got = DR.parse_files("docs\\a.md, docs/b.md")
        self.assertEqual(got, (os.path.join("docs", "a.md"), os.path.join("docs", "b.md")))

    def test_resolve_inside_accepts_backslashes(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "docs").mkdir()
            (Path(d) / "docs" / "f.md").write_text("x", encoding="utf-8")
            for typed in ("docs/f.md", "docs\\f.md"):
                self.assertIsNotNone(DR.resolve_inside(d, typed))
            self.assertIsNone(DR.resolve_inside(d, "..\\..\\etc"))


class LauncherTests(unittest.TestCase):
    def test_non_batch_is_untouched(self):
        self.assertEqual(winproc.direct_argv(["/bin/x", "run", "a\nb"], nt=True),
                         ["/bin/x", "run", "a\nb"])

    def test_npm_shim_is_replaced_by_node_and_script(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            script = root / "node_modules" / "opencode-ai" / "bin" / "opencode"
            script.parent.mkdir(parents=True)
            script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            (root / "node.exe").write_text("", encoding="utf-8")
            shim = root / "opencode.cmd"
            shim.write_text(
                '@ECHO off\r\nendLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & '
                '"%_prog%"  "%dp0%\\node_modules\\opencode-ai\\bin\\opencode" %*\r\n',
                encoding="utf-8")
            argv = winproc.direct_argv([str(shim), "run", "line1\nline2"], nt=True)
            self.assertEqual(argv[0], str(root / "node.exe"))
            self.assertEqual(argv[1], str(script))
            self.assertEqual(argv[2:], ["run", "line1\nline2"])

    def test_exe_target_runs_directly(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            exe = root / "bin" / "opencode.exe"
            exe.parent.mkdir()
            exe.write_text("", encoding="utf-8")
            shim = root / "opencode.cmd"
            shim.write_text('"%dp0%\\bin\\opencode.exe"   %*\r\n', encoding="utf-8")
            self.assertEqual(winproc.direct_argv([str(shim), "x"], nt=True), [str(exe), "x"])

    def test_unknown_shim_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            shim = Path(d) / "x.cmd"
            shim.write_text("@echo hi\r\n", encoding="utf-8")
            self.assertEqual(winproc.direct_argv([str(shim), "a"], nt=True), [str(shim), "a"])

    def test_install_is_a_noop_off_windows(self):
        if os.name != "nt":
            self.assertFalse(winproc.install())


class PromptDeliveryTests(unittest.TestCase):
    def setUp(self):
        import orchestrator as O
        from unittest import mock
        self.O = O
        self.patches = [mock.patch.object(winproc, "windows", return_value=True),
                        mock.patch.object(O, "_engine_run_help", return_value="--file --dir")]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_multiline_prompt_goes_to_stdin_by_default_on_windows(self):
        cmd = ["opencode.exe", "run", "--agent", "a", "--auto", "PERSONA: BA.\nline two"]
        argv, cleanup, stdin = self.O._launch_argv(cmd, lambda *_a: None)
        self.assertEqual(stdin, "PERSONA: BA.\nline two")
        self.assertIsNone(cleanup)
        self.assertNotIn("\n", argv[-1])
        self.assertNotIn("--file", argv)

    def test_the_file_fallback_attaches_the_prompt(self):
        cmd = ["opencode.exe", "run", "--agent", "a", "--auto", "PERSONA: BA.\nline two"]
        argv, cleanup, stdin = self.O._launch_argv(cmd, lambda *_a: None, "file")
        self.assertIsNone(stdin)
        name = argv[argv.index("--file") + 1]
        self.assertEqual(Path(name).read_text(encoding="utf-8"), "PERSONA: BA.\nline two")
        self.assertEqual(argv[-2], "--")
        cleanup()
        self.assertFalse(os.path.exists(name))

    def test_the_last_resort_flattens_to_one_line(self):
        cmd = ["opencode.exe", "run", "--auto", "a\nb"]
        argv, cleanup, stdin = self.O._launch_argv(cmd, lambda *_a: None, "flat")
        self.assertEqual((argv[-1], cleanup, stdin), ("a / b", None, None))

    def test_simple_commands_are_untouched(self):
        cmd = ["opencode.exe", "run", "--help"]
        self.assertEqual(self.O._launch_argv(cmd, lambda *_a: None), (cmd, None, None))

    @unittest.skipIf(os.name == "nt", "uses a shell-script stand-in for the engine")
    def test_a_real_process_receives_the_prompt_on_stdin(self):
        import sys
        import tempfile
        code = ("import sys; d=sys.stdin.read()\n"
                "print('GOT '+repr(d)) if d.strip() else print('run [message..] --dangerously-skip-permissions')")
        with tempfile.TemporaryDirectory() as d:
            eng = Path(d) / "eng"
            eng.write_text(f"#!/bin/sh\nexec {sys.executable} -c \"{code}\"\n", encoding="utf-8")
            eng.chmod(0o755)
            rc, text, _ = self.O._run_stream([str(eng), "run", "L1\nL2"], cwd=d,
                                             emit=lambda *_a: None)
        self.assertIn("GOT 'L1\\nL2'", text)

    def test_help_output_is_recognised_as_no_message(self):
        self.assertTrue(self.O._engine_showed_help(
            "opencode run [message..]\n--dangerously-skip-permissions auto-approve"))
        self.assertFalse(self.O._engine_showed_help("a review"))


if __name__ == "__main__":
    unittest.main()
