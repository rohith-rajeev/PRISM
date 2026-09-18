"""Font resolution — guards a bug that was fatal on one platform only.

A blanket search-and-replace once rewrote the Windows preference list to
reference the very global it is used to define, so `import app` raised
NameError on Windows while Linux and macOS were unaffected and silent. These
tests exercise every platform branch without needing that platform.
"""
import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_ui_family():
    """Pull _ui_family out of app.py without importing Tkinter."""
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_ui_family")
    ns = {"sys": sys}
    exec(compile(ast.Module([fn], []), "<app>", "exec"), ns)  # noqa: S102
    return ns["_ui_family"]


class FontResolution(unittest.TestCase):
    def setUp(self):
        self.ui_family = load_ui_family()
        self.real = sys.platform

    def tearDown(self):
        sys.platform = self.real

    def test_every_platform_branch_returns_a_real_family(self):
        for platform, expected in (("darwin", "SF Pro Text"),
                                   ("win32", "Segoe UI"),
                                   ("linux", "Ubuntu")):
            sys.platform = platform
            got = self.ui_family()
            self.assertEqual(got, expected, f"{platform} guessed {got!r}")
            self.assertIsInstance(got, str)

    def test_no_branch_references_a_global_it_helps_define(self):
        """The exact shape of the bug: a preference list must be literals."""
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "_ui_family")
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id == "_FAMILY":
                self.fail("_ui_family must not reference _FAMILY — it defines it")

    def test_picks_from_what_is_installed(self):
        sys.platform = "darwin"
        self.assertEqual(self.ui_family({"Helvetica Neue", "Arial"}), "Helvetica Neue")
        sys.platform = "linux"
        self.assertEqual(self.ui_family({"DejaVu Sans"}), "DejaVu Sans")

    def test_returns_none_when_nothing_matches_so_tk_default_is_used(self):
        sys.platform = "linux"
        self.assertIsNone(self.ui_family({"Nonexistent Font"}))

    def test_no_stray_hardcoded_family_outside_the_resolver(self):
        """Every font literal must go through the resolved family."""
        src = (ROOT / "app.py").read_text(encoding="utf-8")
        body = src.split("def _ui_family", 1)[1].split("\n\n\n", 1)[1]
        self.assertNotIn('"Segoe UI"', body,
                         "a hardcoded Windows-only font escaped the resolver")


if __name__ == "__main__":
    unittest.main(verbosity=2)
