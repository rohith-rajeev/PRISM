"""Add PRISM's review comments to a COPY of a Word or Excel file.

The caller passes the original file's bytes and gets new bytes back: the same
document with a "PRISM review comments" section added at the end.

* .docx - paragraphs appended to the end of the body (before the document's
  final section properties, which Word requires to stay last). Only
  word/document.xml changes; every other part is copied unchanged, in order.
* .xlsx - a NEW worksheet added after the existing ones, cells written as
  inline strings. Only xl/workbook.xml, xl/_rels/workbook.xml.rels and
  [Content_Types].xml change (plus the new sheet part); the existing sheets
  and shared strings are copied unchanged. docProps/app.xml is left alone
  (its sheet list is informational; Excel tolerates it being stale).

The XML is edited as text, not re-serialised, so every namespace prefix and
byte of the original markup survives. Stdlib only.
"""
import io
import re
import zipfile
from xml.etree import ElementTree as ET

SECTION_TITLE = "PRISM review comments"

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_W_NS = ("http://schemas.openxmlformats.org/wordprocessingml/2006/main",
         "http://purl.oclc.org/ooxml/wordprocessingml/main")
_S_NS = ("http://schemas.openxmlformats.org/spreadsheetml/2006/main",
         "http://purl.oclc.org/ooxml/spreadsheetml/main")
_R_NS = ("http://schemas.openxmlformats.org/officeDocument/2006/relationships",
         "http://purl.oclc.org/ooxml/officeDocument/relationships")
_WORKSHEET_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
_MAX_CELL = 32767                 # Excel's limit on characters in a cell
_MAX_ROWS = 1048576
_MAX_COLS = 16384


class OfficeWriteError(Exception):
    """The file could not be written; str() says why, in plain words."""


# ---- shared helpers -----------------------------------------------------------

def _clean(text):
    """Plain text safe for XML 1.0: illegal characters gone, line ends as \\n."""
    text = "" if text is None else str(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _ILLEGAL.sub("", text)


def _esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _esc_attr(text):
    return _esc(text).replace('"', "&quot;")


def _open_zip(src_bytes, kind):
    if not isinstance(src_bytes, (bytes, bytearray, memoryview)):
        raise OfficeWriteError(f"expected the file's bytes, got {type(src_bytes).__name__}")
    data = bytes(src_bytes)
    if data[:8] == _OLE_MAGIC:
        raise OfficeWriteError(f"it is password-protected (or an old binary {kind} file); "
                               "PRISM can only add comments to an unprotected file")
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        z.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError):
        raise OfficeWriteError(f"it is not a valid {kind} file (damaged, or not that type)")
    return z


def _read_member(z, name, kind):
    try:
        info = z.getinfo(name)
    except KeyError:
        raise OfficeWriteError(f"it is not a valid {kind} file (it has no {name})")
    if info.flag_bits & 0x1:
        raise OfficeWriteError("it is password-protected")
    try:
        return z.read(info)
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError, EOFError) as e:
        raise OfficeWriteError(f"it could not be read ({type(e).__name__}: {e})")
    except Exception as e:  # noqa: BLE001 - zlib.error and friends
        raise OfficeWriteError(f"it could not be read ({type(e).__name__}: {e})")


def _decode(raw):
    """(text, encoder) - encoder turns edited text back into bytes the same way."""
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8"), lambda s: b"\xef\xbb\xbf" + s.encode("utf-8")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), lambda s: s.encode("utf-16")
    return raw.decode("utf-8"), lambda s: s.encode("utf-8")


def _xml_text(z, name, kind):
    raw = _read_member(z, name, kind)
    try:
        return _decode(raw)
    except UnicodeDecodeError:
        raise OfficeWriteError(f"its {name} is not readable text")


def _check_xml(text, name):
    try:
        ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1))
    except ET.ParseError as e:
        raise OfficeWriteError(f"the edited {name} would not be valid XML ({e})")


def _prefix_for(text, namespaces):
    """The prefix ('w:' / '' for a default namespace) the text binds to a namespace."""
    for ns in namespaces:
        m = re.search(r'xmlns:([A-Za-z_][\w.-]*)\s*=\s*["\']' + re.escape(ns) + r'["\']', text)
        if m:
            return m.group(1) + ":", ns
    for ns in namespaces:
        if re.search(r'xmlns\s*=\s*["\']' + re.escape(ns) + r'["\']', text):
            return "", ns
    return None, None


def _rebuild(z, replace, add=()):
    """New zip bytes: every member of z in order (replaced where asked), then `add`."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        dst.comment = z.comment
        for info in z.infolist():
            data = replace.get(info.filename)
            if data is None:
                data = _read_member(z, info.filename, "Office")
            new = zipfile.ZipInfo(info.filename, info.date_time)
            new.compress_type = info.compress_type
            new.external_attr = info.external_attr
            new.create_system = info.create_system
            new.comment = info.comment
            dst.writestr(new, data)
        for name, data in add:
            new = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            new.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(new, data)
    return out.getvalue()


# ---- Word ---------------------------------------------------------------------

def _heading_styles(z):
    """{1: styleId, 2: ..., 3: ...} for the heading styles the document defines."""
    if "word/styles.xml" not in z.namelist():
        return {}
    try:
        root = ET.fromstring(_read_member(z, "word/styles.xml", "Word"))
    except (ET.ParseError, OfficeWriteError):
        return {}
    by_id, by_name = {}, {}
    for st in root.iter():
        if st.tag.rsplit("}", 1)[-1] != "style":
            continue
        attrs = {k.rsplit("}", 1)[-1]: v for k, v in st.attrib.items()}
        if attrs.get("type", "paragraph") != "paragraph" or not attrs.get("styleId"):
            continue
        sid = attrs["styleId"]
        by_id[sid] = sid
        for ch in st:
            if ch.tag.rsplit("}", 1)[-1] == "name":
                name = next((v for k, v in ch.attrib.items() if k.rsplit("}", 1)[-1] == "val"), "")
                by_name[name.strip().lower()] = sid
    out = {}
    for level in (1, 2, 3):
        sid = by_id.get(f"Heading{level}") or by_name.get(f"heading {level}")
        if sid:
            out[level] = sid
    return out


def _runs(p, text, bold):
    rpr = f"<{p}rPr><{p}b/></{p}rPr>" if bold else ""
    parts = []
    for i, line in enumerate(text.split("\n")):
        if i:
            parts.append(f"<{p}br/>")
        for j, chunk in enumerate(line.split("\t")):
            if j:
                parts.append(f"<{p}tab/>")
            if chunk:
                parts.append(f'<{p}t xml:space="preserve">{_esc(chunk)}</{p}t>')
    if not parts:
        return ""
    return f"<{p}r>{rpr}{''.join(parts)}</{p}r>"


def _paragraph(p, text, level, styles):
    text = _clean(text)
    if level and level in styles:
        ppr = f'<{p}pPr><{p}pStyle {p}val="{_esc_attr(styles[level])}"/></{p}pPr>'
        return f"<{p}p>{ppr}{_runs(p, text, False)}</{p}p>"
    return f"<{p}p>{_runs(p, text, bool(level))}</{p}p>"


def _docx_paragraphs(p, title, lines, styles):
    out = [_paragraph(p, title, 1, styles)]
    for line in lines:
        line = "" if line is None else str(line)
        if line.startswith("### "):
            out.append(_paragraph(p, line[4:], 3, styles))
        elif line.startswith("## "):
            out.append(_paragraph(p, line[3:], 2, styles))
        elif line.startswith("- "):
            out.append(_paragraph(p, "• " + line[2:], 0, styles))
        else:
            out.append(_paragraph(p, line, 0, styles))
    return "".join(out)


def _insert_point(xml, p):
    """Where new body content goes: before the body's final sectPr, else before </body>."""
    body_end = xml.rfind(f"</{p}body>")
    if body_end < 0:
        raise OfficeWriteError("it is not a valid Word file (its document has no body)")
    tag = re.escape(f"{p}sectPr")
    depth, last, start = 0, None, None
    for m in re.finditer(rf"<{tag}(?=[\s/>])[^>]*?(/?)>|</{tag}\s*>", xml[:body_end]):
        if m.group(0).startswith("</"):
            depth = max(depth - 1, 0)
            if depth == 0 and start is not None:
                last = (start, m.end())
                start = None
        elif m.group(1):            # self-closing <w:sectPr .../>
            if depth == 0:
                last = (m.start(), m.end())
        else:
            if depth == 0:
                start = m.start()
            depth += 1
    if last and not xml[last[1]:body_end].strip():
        return last[0]
    return body_end


def append_docx_section(src_bytes: bytes, title: str, lines: list) -> bytes:
    """The .docx with a review section (title + lines) appended to its body."""
    z = _open_zip(src_bytes, "Word")
    with z:
        xml, encode = _xml_text(z, "word/document.xml", "Word")
        p, _ns = _prefix_for(xml, _W_NS)
        if p is None:
            raise OfficeWriteError("it is not a valid Word file (unrecognised document markup)")
        at = _insert_point(xml, p)
        new_xml = xml[:at] + _docx_paragraphs(p, title, list(lines or []),
                                              _heading_styles(z)) + xml[at:]
        _check_xml(new_xml, "word/document.xml")
        return _rebuild(z, {"word/document.xml": encode(new_xml)})


# ---- Excel --------------------------------------------------------------------

def _col_letters(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sheet_name(wanted, taken):
    name = re.sub(r"[\[\]:*?/\\]", "", _clean(wanted).replace("\n", " ")).strip().strip("'")
    name = name or "PRISM review"
    taken = {t.lower() for t in taken} | {"history"}     # "History" is reserved by Excel
    base, n, cand = name[:31].rstrip(), 1, name[:31].rstrip()
    while cand.lower() in taken:
        n += 1
        suffix = f" ({n})"
        cand = base[:31 - len(suffix)].rstrip() + suffix
    return cand


def _worksheet_xml(ns, rows):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n',
           f'<worksheet xmlns="{ns}"><sheetData>']
    for r, row in enumerate(list(rows or [])[:_MAX_ROWS], 1):
        if isinstance(row, (str, bytes)) or row is None:
            row = [row]
        cells = []
        for c, value in enumerate(list(row)[:_MAX_COLS]):
            text = _clean(value.decode("utf-8", "replace") if isinstance(value, bytes)
                          else value)[:_MAX_CELL]
            if text:
                cells.append(f'<c r="{_col_letters(c)}{r}" t="inlineStr"><is>'
                             f'<t xml:space="preserve">{_esc(text)}</t></is></c>')
        out.append(f'<row r="{r}">{"".join(cells)}</row>' if cells else f'<row r="{r}"/>')
    out.append("</sheetData></worksheet>")
    return "".join(out).encode("utf-8")


def _insert_before_close(xml, close_tag_re, snippet, what):
    matches = list(re.finditer(close_tag_re, xml))
    if not matches:
        raise OfficeWriteError(f"it is not a valid Excel file (its {what} could not be read)")
    at = matches[-1].start()
    return xml[:at] + snippet + xml[at:]


def append_xlsx_sheet(src_bytes: bytes, sheet_name: str, rows: list) -> bytes:
    """The .xlsx with a new last worksheet `sheet_name` holding `rows` (lists of strings)."""
    z = _open_zip(src_bytes, "Excel")
    with z:
        names = z.namelist()
        wb, wb_enc = _xml_text(z, "xl/workbook.xml", "Excel")
        rels, rels_enc = _xml_text(z, "xl/_rels/workbook.xml.rels", "Excel")
        ct, ct_enc = _xml_text(z, "[Content_Types].xml", "Excel")
        try:
            wb_root = ET.fromstring(_read_member(z, "xl/workbook.xml", "Excel"))
            rel_root = ET.fromstring(_read_member(z, "xl/_rels/workbook.xml.rels", "Excel"))
        except ET.ParseError as e:
            raise OfficeWriteError(f"it is not a valid Excel file ({e})")

        sp, s_ns = _prefix_for(wb, _S_NS)
        if sp is None:
            raise OfficeWriteError("it is not a valid Excel file (unrecognised workbook markup)")
        rp, r_ns = _prefix_for(wb, _R_NS)

        taken, ids = [], [0]
        for el in wb_root.iter():
            if el.tag.rsplit("}", 1)[-1] == "sheet":
                taken.append(el.get("name") or "")
                try:
                    ids.append(int(el.get("sheetId") or 0))
                except ValueError:
                    pass
        name = _sheet_name(sheet_name, taken)

        rel_ids, ws_type = set(), None
        used_n = {0}
        for rel in rel_root:
            rel_ids.add(rel.get("Id") or "")
            if (rel.get("Type") or "").endswith("/worksheet"):
                ws_type = ws_type or rel.get("Type")
        for text in names + re.findall(r'(?:Target|PartName)\s*=\s*"([^"]*)"', rels + ct):
            m = re.search(r"worksheets/sheet(\d+)\.xml$", text, re.I)
            if m:
                used_n.add(int(m.group(1)))
        n = max(used_n) + 1
        while f"xl/worksheets/sheet{n}.xml" in names:
            n += 1
        k = 1
        while f"rId{k}" in rel_ids:
            k += 1
        rid = f"rId{k}"
        if ws_type is None:
            ws_type = (r_ns or _R_NS[0]) + "/worksheet"

        if rp is None:
            rp, decl = "r:", f' xmlns:r="{_R_NS[1] if s_ns == _S_NS[1] else _R_NS[0]}"'
        else:
            decl = ""
        sheet_el = (f'<{sp}sheet name="{_esc_attr(name)}" sheetId="{max(ids) + 1}" '
                    f'{rp}id="{rid}"{decl}/>')
        if re.search(rf"<{re.escape(sp)}sheets\b[^>]*/>", wb):
            raise OfficeWriteError("it is not a valid Excel file (its workbook lists no sheets)")
        wb = _insert_before_close(wb, rf"</{re.escape(sp)}sheets\s*>", sheet_el, "sheet list")

        relp = re.search(r"<([\w.-]+:)?Relationships\b", rels)
        relp = (relp.group(1) or "") if relp else ""
        rels = _insert_before_close(
            rels, rf"</{re.escape(relp)}Relationships\s*>",
            f'<{relp}Relationship Id="{rid}" Type="{_esc_attr(ws_type)}" '
            f'Target="worksheets/sheet{n}.xml"/>', "relationships")

        ctp = re.search(r"<([\w.-]+:)?Types\b", ct)
        ctp = (ctp.group(1) or "") if ctp else ""
        ct = _insert_before_close(
            ct, rf"</{re.escape(ctp)}Types\s*>",
            f'<{ctp}Override PartName="/xl/worksheets/sheet{n}.xml" '
            f'ContentType="{_WORKSHEET_CT}"/>', "content types")

        for text, part in ((wb, "xl/workbook.xml"), (rels, "xl/_rels/workbook.xml.rels"),
                           (ct, "[Content_Types].xml")):
            _check_xml(text, part)
        return _rebuild(z, {"xl/workbook.xml": wb_enc(wb),
                            "xl/_rels/workbook.xml.rels": rels_enc(rels),
                            "[Content_Types].xml": ct_enc(ct)},
                        add=[(f"xl/worksheets/sheet{n}.xml", _worksheet_xml(s_ns, rows))])
