"""The Document review form's layout, measured on a real (hidden-from-test) Tk window.

Skipped when there is no display. Exists because the form once shipped with its
two repository cards drawn on top of each other: a leftover grid columnspan from
the single-repository layout, and text boxes that each asked for 80 characters.
Neither shows in a unit test of the logic, only in geometry.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _display():
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        return False
    try:
        import tkinter
        tkinter.Tk().destroy()
        return True
    except Exception:  # noqa: BLE001
        return False


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                   encoding="utf-8")


def _clone(path):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    (path / "README.md").write_text("x", encoding="utf-8")


@unittest.skipUnless(_display(), "needs a display")
class DocFormLayout(unittest.TestCase):
    def setUp(self):
        import app as A
        self.A = A
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        A._resolve_fonts()
        import tkinter as tk
        self.tk = tk.Tk()
        self.tk.geometry("1020x900")
        self.addCleanup(self.tk.destroy)
        tkroot = self.tk

        class Stub:
            _browsing = False
            _lab = A.App._lab
            _entry = A.App._entry
            _repaint = A.App._repaint
            update_idletasks = staticmethod(tkroot.update_idletasks)

            def _start(self, *_a): pass
            def _start_hotkey(self, *_a): pass
            def _cancel_new(self, *_a): pass

        self.form = A.DocReviewForm(self.tk, Stub())
        self.form.pack(fill="both", expand=True)
        self.pump()

    def pump(self, seconds=0.5):
        end = time.time() + seconds
        while time.time() < end:
            self.tk.update()
            time.sleep(0.02)

    def box(self, w):
        return (w.winfo_rootx(), w.winfo_rooty(), w.winfo_width(), w.winfo_height())

    @staticmethod
    def overlap(a, b):
        return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]

    def open_folder(self, path):
        self.form.proj_var.set(str(path))
        self.form.refresh_detection()
        self.pump()

    def test_switching_from_one_repository_to_two_does_not_overlap_the_cards(self):
        single = self.root / "single"
        _clone(single)
        multi = self.root / "multi"
        multi.mkdir()
        _clone(multi / "shop-backend")
        _clone(multi / "shop-frontend")
        self.open_folder(single)                 # lays the Backend card out full width ...
        self.assertEqual(self.form.mode, "single")
        self.open_folder(multi)                  # ... then two cards must share the row
        self.assertEqual(self.form.mode, "multi")
        be, fe = (self.box(self.form.cards[r]["card"]) for r in ("BE", "FE"))
        self.assertFalse(self.overlap(be, fe), f"cards overlap: {be} {fe}")
        self.assertLess(abs(be[2] - fe[2]), 4, "the two cards should be the same width")
        self.assertLessEqual(be[0] + be[2], fe[0])

    def test_nothing_asks_for_more_width_than_a_small_window_has(self):
        multi = self.root / "multi"
        multi.mkdir()
        _clone(multi / "x-backend")
        _clone(multi / "x-frontend")
        self.open_folder(multi)
        self.assertLessEqual(self.form.scroll.inner.winfo_reqwidth(), 880)
        for r in ("BE", "FE"):
            self.assertLessEqual(self.form.cards[r]["card"].winfo_reqwidth(), 430)

    def test_the_browse_files_button_is_below_the_document_box_not_the_branch_field(self):
        multi = self.root / "multi"
        multi.mkdir()
        _clone(multi / "x-backend")
        _clone(multi / "x-frontend")
        self.open_folder(multi)
        for role in ("BE", "FE"):
            c = self.form.cards[role]
            branch_entry = [w for w in c["card"].inner.winfo_children()
                            if w.winfo_class() == "Entry"][0]
            files, button = self.box(c["files"]), self.box(c["browse"])
            self.assertGreaterEqual(button[1], files[1] + files[3], "button must be under the box")
            self.assertGreater(button[1], self.box(branch_entry)[1] + self.box(branch_entry)[3])
            hint = [w for w in c["browse"].master.winfo_children()
                    if w.winfo_class() == "Label"][0]
            self.assertGreaterEqual(self.box(hint)[0], button[0] + button[2],
                                    "the hint sits beside the button, not on it")
            self.assertLessEqual(self.box(hint)[0] + self.box(hint)[2],
                                 self.box(c["card"])[0] + self.box(c["card"])[2])

    def test_content_starts_at_the_top_of_the_form(self):
        self.open_folder(self.root)
        top = self.form.scroll.inner.winfo_rooty() - self.form.scroll.canvas.winfo_rooty()
        self.assertEqual(top, 0)
        self.assertEqual(self.form.scroll.canvas.yview()[0], 0.0)

    def test_every_card_row_is_inside_the_form_and_does_not_overlap_another(self):
        multi = self.root / "multi"
        multi.mkdir()
        _clone(multi / "x-backend")
        _clone(multi / "x-frontend")
        self.open_folder(multi)
        rows = [self.box(c) for c in self.form.scroll.inner.winfo_children()]
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                self.assertFalse(self.overlap(a, b))
        form = self.box(self.form)
        for r in rows:
            self.assertLessEqual(r[0] + r[2], form[0] + form[2] + 1)


    def _with_dialogs(self, files=None, folder=None):
        import folderpicker as FP
        saved = (FP.native_available, FP.pick_files, FP.pick_folder, self.A.show_warning)
        self.warnings = []
        FP.native_available = lambda: True
        FP.pick_files = lambda title, initial: list(files or [])
        FP.pick_folder = lambda title, initial: folder
        self.A.show_warning = lambda parent, title, msg: self.warnings.append((title, msg))

        def restore():
            FP.native_available, FP.pick_files, FP.pick_folder, self.A.show_warning = saved
        self.addCleanup(restore)

    def _repo_with_docs(self):
        repo = self.root / "shop"
        _clone(repo)
        (repo / "docs" / "extra").mkdir(parents=True)
        for n in ("one.md", "two.md"):
            (repo / "docs" / "extra" / n).write_text("x", encoding="utf-8")
        self.open_folder(repo)
        return repo

    def test_browsing_for_documents_adds_repository_relative_paths_to_what_was_typed(self):
        repo = self._repo_with_docs()
        outside = self.root / "elsewhere.md"
        outside.write_text("x", encoding="utf-8")
        self._with_dialogs(files=[repo / "docs" / "extra" / "one.md",
                                  repo / "docs" / "extra" / "two.md", outside])
        card = self.form.cards["BE"]
        card["files"].insert("1.0", "docs/spec.md")
        self.form._browse_files("BE")
        self.pump(0.6)
        self.assertEqual(self.form.cards["BE"]["files"].get("1.0", "end").split(),
                         ["docs/spec.md", "docs/extra/one.md", "docs/extra/two.md"])
        self.assertEqual(self.warnings[-1][0], "Outside the repository")
        self.form._browse_files("BE")          # picking the same files again adds nothing
        self.pump(0.6)
        self.assertEqual(len(card["files"].get("1.0", "end").split()), 3)

    def test_browsing_for_documents_needs_a_clone_first(self):
        self._with_dialogs(files=["/x/a.md"])
        self.form.cards["BE"]["picker"].set_custom("")
        self.form._browse_files("BE")
        self.pump(0.3)
        self.assertEqual(self.warnings[-1][0], "Clone")
        self.assertEqual(self.form.cards["BE"]["files"].get("1.0", "end").strip(), "")

    def test_browsing_for_the_requirements_folder_stores_it_relative_to_the_repository(self):
        repo = self._repo_with_docs()
        (repo / "specs-src").mkdir()
        self._with_dialogs(folder=str(repo / "specs-src"))
        self.form._browse_requirements()
        self.pump(0.6)
        self.assertEqual(self.form.req_var.get(), "specs-src")


if __name__ == "__main__":
    unittest.main(verbosity=2)
