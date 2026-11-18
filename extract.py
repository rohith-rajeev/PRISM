"""Plain text from the document types people keep requirements in.

The reviewer reads text. Requirements often arrive as Word documents, PDFs and
spreadsheets, so PRISM turns them into text first (into a scratch folder; the
originals are never touched).

* .docx - paragraphs (headings and list items marked) and tables, in order.
* .xlsx - EVERY sheet, in workbook order, each under its own heading (hidden
  sheets included and labelled), rows as ` | `-separated cells.
* .pdf  - text of every page. Uses `pypdf` when it is installed; otherwise a
  small built-in reader that handles ordinary text PDFs. A scanned (image-only)
  or password-protected PDF has no text to get, and says so.

Stdlib only (pypdf is optional).
"""
import re
import zipfile
import zlib
from xml.etree import ElementTree as ET

CONVERTIBLE = (".docx", ".xlsx", ".pdf")
MAX_CHARS = 2_000_000           # one file's text is cut here, and says so
MAX_ROWS = 50_000               # per sheet


class ExtractError(Exception):
    """The file could not be turned into text; str() says why, in plain words."""


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _clip(text):
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + "\n\n[... text cut here: the file is very large ...]\n"
    return text


def extract_text(path):
    """The text of a .docx, .xlsx or .pdf. Raises ExtractError when it has none."""
    suffix = str(path).lower().rsplit(".", 1)[-1]
    try:
        if suffix == "docx":
            text = _docx(path)
        elif suffix == "xlsx":
            text = _xlsx(path)
        elif suffix == "pdf":
            text = _pdf(path)
        else:
            raise ExtractError(f"unsupported type .{suffix}")
    except ExtractError:
        raise
    except zipfile.BadZipFile:
        raise ExtractError("it is not a valid Office file (damaged, or password-protected)")
    except (OSError, ET.ParseError, KeyError, ValueError, zlib.error) as e:
        raise ExtractError(f"it could not be read ({type(e).__name__}: {e})")
    if not text.strip():
        raise ExtractError("it contains no text" + (
            " (a scanned PDF needs OCR first)" if suffix == "pdf" else ""))
    return _clip(text)


# ---- Word ---------------------------------------------------------------------

def _runs_text(node):
    out = []
    for el in node.iter():
        name = _local(el.tag)
        if name == "t" and el.text:
            out.append(el.text)
        elif name == "tab":
            out.append("\t")
        elif name in ("br", "cr"):
            out.append("\n")
    return "".join(out)


def _para(p):
    text = _runs_text(p).strip()
    if not text:
        return ""
    style, listed = "", False
    for el in p.iter():
        name = _local(el.tag)
        if name == "pStyle":
            style = next((v for k, v in el.attrib.items() if _local(k) == "val"), "")
        elif name == "numPr":
            listed = True
    m = re.match(r"(?i)heading\s*(\d)", style or "")
    if m:
        return "#" * min(int(m.group(1)), 6) + " " + text
    if (style or "").lower() == "title":
        return "# " + text
    return ("- " + text) if listed else text


def _docx(path):
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = next((c for c in root if _local(c.tag) == "body"), root)
    lines = []

    def table(tbl):
        for tr in tbl:
            if _local(tr.tag) != "tr":
                continue
            cells = []
            for tc in tr:
                if _local(tc.tag) == "tc":
                    cells.append(" ".join(t for t in (_para(p).lstrip("-# ").strip()
                                                      for p in tc.iter()
                                                      if _local(p.tag) == "p") if t))
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    for child in body:
        name = _local(child.tag)
        if name == "p":
            line = _para(child)
            if line:
                lines.append(line)
                lines.append("")
        elif name == "tbl":
            table(child)
    return "\n".join(lines)


# ---- Excel --------------------------------------------------------------------

def _col_index(ref):
    letters = re.match(r"[A-Za-z]+", ref or "")
    n = 0
    for ch in (letters.group(0).upper() if letters else ""):
        n = n * 26 + ord(ch) - 64
    return max(n - 1, 0)


def _shared_strings(z):
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root:
        parts = []

        def walk(node):
            for ch in node:
                name = _local(ch.tag)
                if name == "t" and ch.text:
                    parts.append(ch.text)
                elif name in ("r", "is"):
                    walk(ch)
        walk(si)
        out.append("".join(parts))
    return out


def _sheet_rows(xml, strings):
    root = ET.fromstring(xml)
    rows = []
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        cells = {}
        for c in row:
            if _local(c.tag) != "c":
                continue
            kind, value = c.get("t", "n"), ""
            v = next((x for x in c if _local(x.tag) == "v"), None)
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in c.iter() if _local(t.tag) == "t")
            elif v is not None and v.text is not None:
                value = v.text
                if kind == "s":
                    try:
                        value = strings[int(value)]
                    except (ValueError, IndexError):
                        value = ""
                elif kind == "b":
                    value = "TRUE" if value.strip() == "1" else "FALSE"
            value = re.sub(r"\s*\n\s*", " / ", value.strip())
            if value:
                cells[_col_index(c.get("r"))] = value
        if cells:
            width = max(cells) + 1
            rows.append(" | ".join(cells.get(i, "") for i in range(width)))
        if len(rows) >= MAX_ROWS:
            rows.append(f"[... more than {MAX_ROWS} rows; the rest are not shown ...]")
            break
    return rows


def _xlsx(path):
    with zipfile.ZipFile(path) as z:
        strings = _shared_strings(z)
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = {}
        try:
            for rel in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                rels[rel.get("Id")] = rel.get("Target", "")
        except KeyError:
            pass
        sheets = []
        for el in wb.iter():
            if _local(el.tag) == "sheet":
                rid = next((v for k, v in el.attrib.items() if _local(k) == "id"), None)
                sheets.append((el.get("name") or f"Sheet{len(sheets) + 1}", rid,
                               el.get("state") or ""))
        names = set(z.namelist())
        out = []
        for n, (name, rid, state) in enumerate(sheets, 1):
            target = rels.get(rid, f"worksheets/sheet{n}.xml")
            target = target.lstrip("/")
            member = target if target.startswith("xl/") else "xl/" + target
            if member not in names:
                continue
            rows = _sheet_rows(z.read(member), strings)
            hidden = " (hidden sheet)" if state in ("hidden", "veryHidden") else ""
            out.append(f"## Sheet: {name}{hidden}\n" + ("\n".join(rows) if rows
                                                         else "(this sheet is empty)"))
    return f"Workbook with {len(out)} sheet(s).\n\n" + "\n\n".join(out) if out else ""


# ---- PDF ----------------------------------------------------------------------

def _pdf(path):
    try:
        import pypdf  # noqa: PLC0415 - optional
    except ImportError:
        pypdf = None
    if pypdf is not None:
        reader = pypdf.PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            try:
                if not reader.decrypt(""):
                    raise ExtractError("it is password-protected")
            except ExtractError:
                raise
            except Exception:  # noqa: BLE001
                raise ExtractError("it is password-protected")
        pages = []
        for i, page in enumerate(reader.pages, 1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                pages.append(f"## Page {i}\n{text}")
        return "\n\n".join(pages)
    return _pdf_basic(path)


_STREAM = re.compile(rb"<<(.*?)>>\s*stream\r?\n(.*?)endstream", re.S)
_TEXT_OPS = re.compile(rb"\((?:\\.|[^\\()])*\)|\[(?:\\.|[^\]])*\]\s*TJ|T\*|Td|TD|ET|'|\"", re.S)
_LITERAL = re.compile(rb"\(((?:\\.|[^\\()])*)\)", re.S)


def _unescape(raw):
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i:i + 1]
        if ch == b"\\" and i + 1 < len(raw):
            nxt = raw[i + 1:i + 2]
            simple = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
            if nxt in simple:
                out += simple[nxt]
                i += 2
            elif nxt.isdigit():
                j = i + 1
                while j < len(raw) and j < i + 4 and raw[j:j + 1].isdigit():
                    j += 1
                out.append(int(raw[i + 1:j], 8) & 255)
                i = j
            else:
                out += nxt
                i += 2
        else:
            out += ch
            i += 1
    return bytes(out).decode("cp1252", errors="replace")


def _pdf_basic(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if b"/Encrypt" in data[-2048:] or b"/Encrypt" in data[:2048]:
        raise ExtractError("it is password-protected")
    chunks = []
    for head, body in _STREAM.findall(data):
        if b"FlateDecode" in head:
            try:
                # decompressobj ignores the line break before "endstream"; the
                # last byte of the compressed data can itself be a CR.
                body = zlib.decompressobj().decompress(body)
            except zlib.error:
                continue
        if b"BT" not in body:
            continue
        for block in re.findall(rb"BT(.*?)ET", body, re.S):
            line = []
            for tok in re.finditer(rb"\[((?:\\.|[^\]])*)\]\s*TJ|\(((?:\\.|[^\\()])*)\)\s*(?:Tj|'|\")"
                                   rb"|(T\*|Td|TD)", block, re.S):
                if tok.group(1) is not None:
                    pieces = [_unescape(m) for m in _LITERAL.findall(tok.group(1))]
                    line.append("".join(pieces))
                elif tok.group(2) is not None:
                    line.append(_unescape(tok.group(2)))
                else:
                    line.append("\n")
            text = "".join(line).strip()
            if text:
                chunks.append(text)
    return "\n".join(chunks)
