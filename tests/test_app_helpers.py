"""Pure display-formatting helpers from app.py, tested without Tkinter.

Same technique as test_fonts.py's load_ui_family: pull one function out of
app.py by AST rather than `import app`, so this stays runnable on a CI box
with no Tk/Tcl installed at all.
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_format_tokens():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_format_tokens")
    ns = {}
    exec(compile(ast.Module([fn], []), "<app>", "exec"), ns)  # noqa: S102
    return ns["_format_tokens"]


class TokenFormatting(unittest.TestCase):
    """TOKENS has to stay readable from a handful of tokens up to the
    millions a reasoning-heavy free-tier model can burn on one review."""

    def setUp(self):
        self.format_tokens = load_format_tokens()

    def test_small_counts_are_exact(self):
        self.assertEqual(self.format_tokens(0), "0")
        self.assertEqual(self.format_tokens(342), "342")

    def test_thousands_are_abbreviated(self):
        self.assertEqual(self.format_tokens(1_500), "1.5k")

    def test_millions_are_abbreviated(self):
        # The real number from a live run that hit the free-tier bug: a
        # single review burned 3.8M+ reasoning tokens.
        self.assertEqual(self.format_tokens(3_921_713), "3.92M")

    def test_missing_or_junk_reads_as_a_dash(self):
        self.assertEqual(self.format_tokens(None), "—")
        self.assertEqual(self.format_tokens("not a number"), "—")


if __name__ == "__main__":
    unittest.main(verbosity=2)
