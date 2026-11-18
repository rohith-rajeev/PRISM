import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import docreview as D  # noqa: E402
import extract as X  # noqa: E402

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def make_xlsx(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml",
                   f'<workbook xmlns="{NS}" xmlns:r="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships"><sheets>'
                   '<sheet name="Rules" sheetId="1" r:id="rId1"/>'
                   '<sheet name="Edge cases" sheetId="2" state="hidden" r:id="rId2"/>'
                   '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{NS}"><si><t>Name</t></si>'
                   '<si><r><t>Re</t></r><r><t>fund</t></r></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                   '<c r="C1" t="s"><v>1</v></c></row><row r="2"><c r="A2"><v>42</v></c></row>'
                   '</sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml",
                   f'<worksheet xmlns="{NS}"><sheetData><row r="1"><c r="A1" t="inlineStr">'
                   '<is><t>partial refund after 30 days</t></is></c></row></sheetData></worksheet>')


def make_docx(path):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml",
                   f'<w:document xmlns:w="{w}"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/>'
                   '</w:pPr><w:r><w:t>Refunds</w:t></w:r></w:p><w:p><w:r><w:t>Body text</w:t>'
                   '</w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>A</w:t></w:r></w:p></w:tc>'
                   '<w:tc><w:p><w:r><w:t>B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
                   '</w:body></w:document>')


def make_pdf(path, text=b"BT (Hello \\(PDF\\)) Tj T* [(Refund ) -20 (rules)] TJ ET"):
    # The compressed data ends in a CR byte, a real-world trap for naive parsers.
    data = zlib.compress(text)
    open(path, "wb").write(b"%PDF-1.4\n1 0 obj\n<< /Filter /FlateDecode >>\nstream\n"
                           + data + b"\nendstream\nendobj\n%%EOF")


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def test_every_sheet_of_a_workbook_is_read_with_its_name(self):
        make_xlsx(self.d / "r.xlsx")
        text = X.extract_text(self.d / "r.xlsx")
        self.assertIn("## Sheet: Rules", text)
        self.assertIn("Name |  | Refund", text)          # a gap keeps its column
        self.assertIn("## Sheet: Edge cases (hidden sheet)", text)
        self.assertIn("partial refund after 30 days", text)
        self.assertIn("2 sheet(s)", text)

    def test_docx_keeps_headings_and_tables(self):
        make_docx(self.d / "r.docx")
        text = X.extract_text(self.d / "r.docx")
        self.assertIn("# Refunds", text)
        self.assertIn("Body text", text)
        self.assertIn("| A | B |", text)

    def test_pdf_text(self):
        make_pdf(self.d / "r.pdf")
        self.assertIn("Refund rules", X.extract_text(self.d / "r.pdf"))

    def test_unreadable_files_say_why(self):
        (self.d / "bad.xlsx").write_bytes(b"not a zip")
        with self.assertRaises(X.ExtractError) as cm:
            X.extract_text(self.d / "bad.xlsx")
        self.assertIn("valid Office file", str(cm.exception))
        make_pdf(self.d / "scan.pdf", b"q Q")
        with self.assertRaises(X.ExtractError) as cm:
            X.extract_text(self.d / "scan.pdf")
        self.assertIn("no text", str(cm.exception))


class RequirementFolderTests(unittest.TestCase):
    def test_office_and_pdf_files_are_listed_and_converted(self):
        d = Path(tempfile.mkdtemp())
        req, scratch = d / "req", d / "scratch"
        (req / "sub").mkdir(parents=True)
        scratch.mkdir()
        make_xlsx(req / "rules.xlsx")
        make_docx(req / "sub" / "spec.docx")
        make_pdf(req / "old.pdf")
        (req / "bad.docx").write_bytes(b"junk")
        (req / "~$spec.docx").write_bytes(b"lock")
        (req / "notes.md").write_text("plain", encoding="utf-8")
        (req / "logo.png").write_bytes(b"x")
        files, trunc = D.list_requirement_files(req)
        self.assertEqual(sorted(p.name for p in files),
                         ["bad.docx", "notes.md", "old.pdf", "rules.xlsx", "spec.docx"])
        msgs = []
        ready, note, n = D.prepare_requirements(files, req, scratch, msgs.append)
        self.assertEqual(n, 3)
        self.assertEqual(len(ready), 4)                       # bad.docx dropped
        self.assertTrue(any("bad.docx" in m for m in msgs))
        self.assertIn("could NOT be read", note)
        self.assertIn("rules.xlsx", note)
        xl = next(p for p in ready if p.name.startswith("rules"))
        self.assertIn("Edge cases", xl.read_text(encoding="utf-8"))
        self.assertTrue(any(p == req / "notes.md" for p in ready))   # text passes through


if __name__ == "__main__":
    unittest.main()
