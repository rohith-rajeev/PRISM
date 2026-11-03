"""The CLI job detail view: how a transcript line is coloured.

The screen itself needs a display, so these load just the two pure pieces out
of app.py without importing Tkinter.
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    classify = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_classify")
    app_cls = next(n for n in tree.body
                   if isinstance(n, ast.ClassDef) and n.name == "App")
    tagger = next(n for n in app_cls.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_cli_line_tag")
    tagger.decorator_list = []              # plain function for the test
    ns = {}
    exec(compile(ast.Module([classify, tagger], []), "<app>", "exec"), ns)  # noqa: S102
    return lambda line: ns["_cli_line_tag"](line)


class CliLineTag(unittest.TestCase):
    def setUp(self):
        self.tag = load()

    def test_structure_and_tool_lines_are_quiet(self):
        self.assertEqual(self.tag("═══ STEP 1 — AI review ═══"), "step")
        self.assertEqual(self.tag("$ prism run --agent pr-reviewer"), "tool")
        self.assertEqual(self.tag("⚙ shell: git diff --stat"), "tool")
        self.assertEqual(self.tag("▸ Target: CodeCommit repo 'x'"), "tool")

    def test_outcomes_keep_their_colours(self):
        self.assertEqual(self.tag("◆ Verdict: Approve   Impact: 2/10"), "ok")
        self.assertEqual(self.tag("⚠ Stopping this step"), "warn")
        self.assertEqual(self.tag("⛔ ERROR: Review run failed"), "err")

    def test_ordinary_prose_is_untagged(self):
        self.assertIsNone(self.tag("Reading the changeset, 14 files."))
        self.assertIsNone(self.tag("its error handling looks fine"))


if __name__ == "__main__":
    unittest.main()
