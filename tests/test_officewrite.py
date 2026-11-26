import io
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import extract as X  # noqa: E402
import officewrite as O  # noqa: E402

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"
DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'

STYLES_WITH_HEADINGS = (
    DECL + f'<w:styles xmlns:w="{W}">'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/></w:style>'
    '</w:styles>')
STYLES_PLAIN = (DECL + f'<w:styles xmlns:w="{W}"><w:style w:type="paragraph" w:styleId="Normal">'
                '<w:name w:val="Normal"/></w:style></w:styles>')

DOC_BODY = (
    '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Refund policy</w:t></w:r></w:p>'
    '<w:p><w:r><w:t>Refunds within 30 days.</w:t></w:r></w:p>'
    # A paragraph-level section break: its sectPr must not be mistaken for the final one.
    '<w:p><w:pPr><w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:pPr>'
    '<w:r><w:t>End of part one.</w:t></w:r></w:p>'
    '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell A</w:t></w:r></w:p></w:tc>'
    '<w:tc><w:p><w:r><w:t>Cell B</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
    '<w:p><w:r><w:t>Closing line.</w:t></w:r></w:p>')
FINAL_SECTPR = ('<w:sectPr w:rsidR="00AB"><w:pgSz w:w="12240" w:h="15840"/>'
                '<w:sectPrChange w:id="1" w:author="x"><w:sectPr/></w:sectPrChange>'
                '</w:sectPr>')


def make_docx(styles=STYLES_WITH_HEADINGS, sectpr=FINAL_SECTPR):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
                   compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("_rels/.rels", DECL + f'<Relationships xmlns="{PR}"><Relationship Id="rId1" '
                   f'Type="{R}/officeDocument" Target="word/document.xml"/></Relationships>',
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("word/document.xml",
                   DECL + f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:w14="http://x/w14">'
                   f'<w:body>{DOC_BODY}{sectpr}\r\n</w:body></w:document>',
                   compress_type=zipfile.ZIP_DEFLATED)
        if styles is not None:
            z.writestr("word/styles.xml", styles, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("word/media/image1.png", b"\x89PNG\r\n\x1a\n\x00binary", compress_type=zipfile.ZIP_STORED)
    return buf.getvalue()


def make_xlsx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/xl/workbook.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                   f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{O._WORKSHEET_CT}"/>'
                   f'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="{O._WORKSHEET_CT}"/>'
                   '</Types>')
        z.writestr("_rels/.rels", DECL + f'<Relationships xmlns="{PR}"><Relationship Id="rId1" '
                   f'Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        # Relationships namespace bound to an unusual prefix on purpose.
        z.writestr("xl/workbook.xml",
                   DECL + f'<workbook xmlns="{S}" xmlns:rel="{R}"><bookViews><workbookView/></bookViews>'
                   '<sheets><sheet name="Rules" sheetId="1" rel:id="rId1"/>'
                   '<sheet name="Edge cases" sheetId="4" state="hidden" rel:id="rId2"/>'
                   '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   DECL + f'<Relationships xmlns="{PR}">'
                   f'<Relationship Id="rId1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/>'
                   f'<Relationship Id="rId2" Type="{R}/worksheet" Target="worksheets/sheet2.xml"/>'
                   f'<Relationship Id="rId3" Type="{R}/sharedStrings" Target="sharedStrings.xml"/>'
                   '</Relationships>')
        z.writestr("xl/sharedStrings.xml", DECL + f'<sst xmlns="{S}"><si><t>Name</t></si>'
                   '<si><t>Refund</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml",
                   DECL + f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c>'
                   '<c r="B1" t="s"><v>1</v></c></row></sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml",
                   DECL + f'<worksheet xmlns="{S}"><sheetData><row r="1"><c r="A1" t="inlineStr">'
                   '<is><t>partial refund</t></is></c></row></sheetData></worksheet>')
    return buf.getvalue()


def members(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [(i.filename, i.compress_type, z.read(i)) for i in z.infolist()]


class Base(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def extract(self, data, suffix):
        path = self.d / f"out{suffix}"
        path.write_bytes(data)
        return X.extract_text(path)


class DocxTests(Base):
    LINES = ["## Summary", "Overall <fine> & ready — naïve café 日本語", "",
             "### Findings", "- Missing \"refund\" rule", "- Bad\x00ch\x0bar\x1fs", "plain"]

    def setUp(self):
        super().setUp()
        self.src = make_docx()
        self.out = O.append_docx_section(self.src, O.SECTION_TITLE, self.LINES)
        self.doc = zipfile.ZipFile(io.BytesIO(self.out)).read("word/document.xml").decode("utf-8")

    def test_result_is_valid_zip_and_document_xml_is_well_formed(self):
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(self.out)))
        with zipfile.ZipFile(io.BytesIO(self.out)) as z:
            self.assertIsNone(z.testzip())
        root = ET.fromstring(zipfile.ZipFile(io.BytesIO(self.out)).read("word/document.xml"))
        body = root.find(f"{{{W}}}body")
        self.assertEqual(body[-1].tag, f"{{{W}}}sectPr")     # sectPr stays last

    def test_other_members_unchanged_same_order_and_compression(self):
        before, after = members(self.src), members(self.out)
        self.assertEqual([(n, c) for n, c, _ in before], [(n, c) for n, c, _ in after])
        for (n, _, a), (_, _, b) in zip(before, after):
            if n != "word/document.xml":
                self.assertEqual(a, b, n)

    def test_original_content_kept_and_new_text_between_content_and_sectpr(self):
        orig = zipfile.ZipFile(io.BytesIO(self.src)).read("word/document.xml").decode("utf-8")
        at = orig.index(FINAL_SECTPR)
        self.assertTrue(self.doc.startswith(orig[:at]))
        self.assertTrue(self.doc.endswith(orig[at:]))
        new = self.doc[at:len(self.doc) - len(orig) + at]
        self.assertIn(O.SECTION_TITLE, new)
        self.assertNotIn("sectPr", new)
        self.assertLess(self.doc.index("Closing line."), self.doc.index(O.SECTION_TITLE))
        self.assertLess(self.doc.index(O.SECTION_TITLE), self.doc.rindex("<w:sectPr w:rsidR"))

    def test_escaping_preserve_and_bullets(self):
        self.assertIn("Overall &lt;fine&gt; &amp; ready", self.doc)
        self.assertIn('xml:space="preserve"', self.doc)
        self.assertIn("• Missing \"refund\" rule", self.doc)
        self.assertNotIn("numPr", self.doc)

    def test_illegal_control_chars_stripped(self):
        self.assertIn("• Badchars", self.doc)
        self.assertIsNone(re.search("[\x00-\x08\x0b\x0c\x0e-\x1f]", self.doc))

    def test_heading_styles_used_when_defined(self):
        root = ET.fromstring(self.doc.split("\n", 1)[1])
        styled = {}
        for p in root.iter(f"{{{W}}}p"):
            st = p.find(f"{{{W}}}pPr/{{{W}}}pStyle")
            text = "".join(t.text or "" for t in p.iter(f"{{{W}}}t"))
            if st is not None:
                styled[text] = st.get(f"{{{W}}}val")
        self.assertEqual(styled[O.SECTION_TITLE], "Heading1")
        self.assertEqual(styled["Summary"], "Heading2")
        self.assertEqual(styled["Findings"], "Heading3")
        self.assertNotIn("<w:b/>", self.doc)

    def test_bold_fallback_without_heading_styles(self):
        for styles in (STYLES_PLAIN, None):
            out = O.append_docx_section(make_docx(styles=styles), "Title", ["## Sub", "x"])
            doc = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode("utf-8")
            self.assertIn('<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Title</w:t></w:r>', doc)
            self.assertIn('<w:b/></w:rPr><w:t xml:space="preserve">Sub</w:t>', doc)
            self.assertNotIn('w:val="Heading2"', doc)

    def test_extract_text_round_trip_in_order(self):
        text = self.extract(self.out, ".docx")
        order = ["Refund policy", "Refunds within 30 days.", "| Cell A | Cell B |", "Closing line.",
                 "# " + O.SECTION_TITLE, "## Summary", "Overall <fine> & ready — naïve café 日本語",
                 "### Findings", "• Missing \"refund\" rule", "• Badchars", "plain"]
        pos = -1
        for item in order:
            nxt = text.find(item, pos + 1)
            self.assertGreater(nxt, pos, f"{item!r} missing or out of order in:\n{text}")
            pos = nxt

    def test_no_final_sectpr_appends_before_body_end(self):
        out = O.append_docx_section(make_docx(sectpr=""), "T", ["hello"])
        doc = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode("utf-8")
        ET.fromstring(doc.split("\n", 1)[1])
        self.assertLess(doc.index("Closing line."), doc.index("hello"))
        self.assertLess(doc.index("hello"), doc.index("</w:body>"))

    def test_self_closing_final_sectpr(self):
        out = O.append_docx_section(make_docx(sectpr="<w:sectPr/>"), "T", ["hello"])
        doc = zipfile.ZipFile(io.BytesIO(out)).read("word/document.xml").decode("utf-8")
        self.assertLess(doc.index("hello"), doc.index("<w:sectPr/>"))

    def test_errors(self):
        for bad in (b"", b"not a zip at all", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600):
            with self.assertRaises(O.OfficeWriteError):
                O.append_docx_section(bad, "T", ["x"])
        with self.assertRaisesRegex(O.OfficeWriteError, "password"):
            O.append_docx_section(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600, "T", [])
        with self.assertRaisesRegex(O.OfficeWriteError, "word/document.xml"):
            O.append_docx_section(make_xlsx(), "T", ["x"])
        with self.assertRaises(O.OfficeWriteError):
            O.append_docx_section("text", "T", ["x"])


class XlsxTests(Base):
    ROWS = [["Severity", "Finding"], ["High", "Refund <30 days> & tax — café"],
            ["Low", "bad\x01char\x0c"], [], ["", "skip first"]]

    def setUp(self):
        super().setUp()
        self.src = make_xlsx()
        self.out = O.append_xlsx_sheet(self.src, O.SECTION_TITLE, self.ROWS)
        self.z = zipfile.ZipFile(io.BytesIO(self.out))

    def test_valid_zip_and_new_sheet_last(self):
        self.assertIsNone(self.z.testzip())
        wb = ET.fromstring(self.z.read("xl/workbook.xml"))
        sheets = [s for s in wb.iter(f"{{{S}}}sheet")]
        self.assertEqual([s.get("name") for s in sheets], ["Rules", "Edge cases", O.SECTION_TITLE])
        self.assertEqual(sheets[-1].get("sheetId"), "5")
        self.assertEqual(sheets[-1].get(f"{{{R}}}id"), "rId4")
        self.assertIn('rel:id="rId4"', self.z.read("xl/workbook.xml").decode())

    def test_rels_and_content_types_updated(self):
        rels = ET.fromstring(self.z.read("xl/_rels/workbook.xml.rels"))
        new = [r for r in rels if r.get("Id") == "rId4"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0].get("Target"), "worksheets/sheet3.xml")
        self.assertEqual(new[0].get("Type"), f"{R}/worksheet")
        ct = self.z.read("[Content_Types].xml").decode()
        self.assertIn(f'<Override PartName="/xl/worksheets/sheet3.xml" ContentType="{O._WORKSHEET_CT}"/>', ct)
        ET.fromstring(self.z.read("[Content_Types].xml"))
        self.assertEqual(self.z.namelist()[-1], "xl/worksheets/sheet3.xml")

    def test_original_members_byte_identical(self):
        before = dict((n, d) for n, _, d in members(self.src))
        changed = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml"}
        for n, d in before.items():
            if n not in changed:
                self.assertEqual(self.z.read(n), d, n)
        self.assertEqual([n for n, _, _ in members(self.src)], self.z.namelist()[:len(before)])

    def test_new_sheet_cells_inline_and_clean(self):
        xml = self.z.read("xl/worksheets/sheet3.xml").decode()
        ET.fromstring(xml.split("\n", 1)[1])
        self.assertIn('<c r="B2" t="inlineStr"><is><t xml:space="preserve">Refund &lt;30 days&gt; &amp;', xml)
        self.assertIn(">badchar<", xml)
        self.assertIn('<c r="B5"', xml)
        self.assertNotIn('r="A5"', xml)
        self.assertEqual(self.z.read("xl/sharedStrings.xml"), dict(
            (n, d) for n, _, d in members(self.src))["xl/sharedStrings.xml"])

    def test_extract_text_lists_new_sheet_with_rows(self):
        text = self.extract(self.out, ".xlsx")
        self.assertIn("Workbook with 3 sheet(s).", text)
        self.assertIn("## Sheet: Edge cases (hidden sheet)", text)
        tail = text.split(f"## Sheet: {O.SECTION_TITLE}\n", 1)[1]
        self.assertEqual(tail.splitlines()[:4], ["Severity | Finding",
                                                 "High | Refund <30 days> & tax — café",
                                                 "Low | badchar", " | skip first"])
        self.assertLess(text.index("Name | Refund"), text.index(O.SECTION_TITLE))

    def test_unique_names_and_31_char_limit(self):
        out = O.append_xlsx_sheet(self.out, O.SECTION_TITLE, [["x"]])
        out = O.append_xlsx_sheet(out, "prism REVIEW comments", [["y"]])
        wb = ET.fromstring(zipfile.ZipFile(io.BytesIO(out)).read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.iter(f"{{{S}}}sheet")]
        self.assertEqual(names[2:], [O.SECTION_TITLE, O.SECTION_TITLE + " (2)",
                                     "prism REVIEW comments (3)"])
        self.assertEqual(len(set(zipfile.ZipFile(io.BytesIO(out)).namelist())), 10)

        long = "A very long sheet name: [with] *bad* chars?/\\ that goes on"
        out = O.append_xlsx_sheet(self.src, long, [["x"]])
        out = O.append_xlsx_sheet(out, long, [["x"]])
        wb = ET.fromstring(zipfile.ZipFile(io.BytesIO(out)).read("xl/workbook.xml"))
        names = [s.get("name") for s in wb.iter(f"{{{S}}}sheet")][2:]
        self.assertEqual(names[0], "A very long sheet name with bad")
        for n in names:
            self.assertLessEqual(len(n), 31)
            self.assertIsNone(re.search(r"[\[\]:*?/\\]", n))
        self.assertTrue(names[1].endswith(" (2)"))
        self.assertIn("Workbook with 4 sheet(s).", self.extract(out, ".xlsx"))

    def test_errors(self):
        for bad in (b"", b"PK\x03\x04garbage", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 600):
            with self.assertRaises(O.OfficeWriteError):
                O.append_xlsx_sheet(bad, "S", [["x"]])
        with self.assertRaisesRegex(O.OfficeWriteError, "xl/workbook.xml"):
            O.append_xlsx_sheet(make_docx(), "S", [["x"]])


if __name__ == "__main__":
    unittest.main()
