"""Structured findings: what a review found, as data instead of text.

A review produces prose. This module turns the one-line comments in it into
records with a fixed shape, so a script (``prism jobs --json``) can read them
without scraping Markdown:

    {
      "id":          "F1"  (pull request)  |  "DR-3"  (document)
      "kind":        "pr" | "document"
      "category":    the severity, lower case:
                       pull request - critical, high, medium, low, nit
                       document     - blocker, major, minor, suggestion
      "type":        the reviewer's own label for what kind of problem it is
                     ("Security", "Gap", ...), or "" when it gave none
      "description": what is wrong and why it matters
      "late_find":   true when a pull-request reviewer marked it as missed by
                     an earlier review of the same change
      "persona":     the document reviewer's lens (ba, architect, qa); null for a PR
      "scale":       "normal" | "large" - the scale of the change the review it came
                     from classified (a large change is never approved); null when
                     that review stated none
      "traceability": {
          "repo":          repository the finding is about
          "pull_request":  PR id (pull request) | null
          "branch":        the branch reviewed (the PR's source branch)
          "target_branch": the PR's destination branch | null
          "commit":        the commit reviewed | null
          "path":          file the finding is about, relative to the repository
                           root (the document's own path for a document) | null
          "line":          first line, or null
          "line_end":      last line when a range was given, else null
          "section":       heading inside the document | null
          "role":          "BE" | "FE" | "repo" for a document | null
      }
    }

Every key is always present - a value that does not apply is ``null`` - so a
reader never has to test for a key. Nothing here needs Tk, the network or the
rest of PRISM; the two parsers are pure functions of the text they are given.
"""
import re

KIND_PR = "pr"
KIND_DOC = "document"

PR_CATEGORIES = ("critical", "high", "medium", "low", "nit")
DOC_CATEGORIES = ("blocker", "major", "minor", "suggestion")

FINDING_KEYS = ("id", "kind", "category", "type", "description", "late_find",
                "persona", "scale", "traceability")
SCALES = ("normal", "large")
TRACE_KEYS = ("repo", "pull_request", "branch", "target_branch", "commit", "path",
              "line", "line_end", "section", "role")

# A runaway guard, not a working trim: a finding is one comment.
MAX_DESCRIPTION = 1500

# The reviewer's own labels for a document comment (agents/doc-reviewer.md).
DOC_TYPES = ("gap", "feasibility", "contradiction", "ambiguity", "traceability",
             "testability", "risk", "consistency", "scope", "compliance", "security",
             "stability", "scalability", "pattern", "regression", "other")

_PR_LINE_RE = re.compile(r"^\s*-\s*\*\*\[(Critical|High|Medium|Low|Nit)\]", re.IGNORECASE)
_DOC_LINE_RE = re.compile(
    r"^\s*[-*]\s*\*\*\[(Blocker|Major|Minor|Suggestion)\]\s*(DR-\d+)\*\*",
    re.IGNORECASE | re.MULTILINE)
_BULLET_RE = re.compile(r"^\s*[-*+]\s")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s")
_LATE_RE = re.compile(r"\blate[\s-]+find\b", re.IGNORECASE)
_BACKTICKED_RE = re.compile(r"`([^`]+)`")
# "src/a.py", "src/a.py:12", "src/a.py:12-18"; also Windows "C:\\a\\b.py:12".
_PATH_LINE_RE = re.compile(r"^(?P<path>.+?)(?::(?P<line>\d+)(?:\s*[-–]\s*(?P<end>\d+))?)?$")
_DASH_SPLIT_RE = re.compile(r"\s+[—–-]\s+")


def _trace(**given):
    out = {key: None for key in TRACE_KEYS}
    out.update({k: v for k, v in given.items() if k in out})
    return out


def make_finding(id, kind, category, description, trace, type="", late_find=False,
                 persona=None, scale=None):
    """One finding with every key present and every value of a plain, JSON-safe type."""
    return {
        "id": str(id), "kind": kind, "category": str(category).lower(),
        "type": str(type or ""), "description": str(description or "")[:MAX_DESCRIPTION],
        "late_find": bool(late_find), "persona": persona or None,
        "scale": scale if scale in SCALES else None,
        "traceability": trace,
    }


# A backticked token in the *description* is often a code identifier (`foo()`, `x`), not
# a place. Only something that reads as a file path is taken from there; the bold heading
# is where the format puts the location, so anything without spaces or call parentheses
# is accepted from it.
_FILE_LIKE_RE = re.compile(r"^[^\s()]*(?:[/\\]|\.[A-Za-z0-9]{1,8})(?::\d+(?:\s*[-–]\s*\d+)?)?$")


def _location_in(head, text):
    """The backticked location in the heading, else a file-like one in the text, else None."""
    for m in _BACKTICKED_RE.finditer(head or ""):
        token = m.group(1).strip()
        if token and not re.search(r"[\s()]", token):
            return token
    for m in _BACKTICKED_RE.finditer(text or ""):
        token = m.group(1).strip()
        if _FILE_LIKE_RE.match(token):
            return token
    return None


def _split_location(text):
    """("path", line, line_end) from `path:12-18`; (None, None, None) for nothing usable."""
    text = (text or "").strip()
    if not text:
        return None, None, None
    m = _PATH_LINE_RE.match(text)
    if not m:
        return text, None, None
    line = int(m.group("line")) if m.group("line") else None
    end = int(m.group("end")) if m.group("end") else None
    return m.group("path").strip() or None, line, end


# ---- pull requests -----------------------------------------------------------

def parse_pr_findings(lines, repo="", pull_request="", branch="", target_branch="",
                      commit="", scale=None):
    """Findings from the reviewer's one-line bullets.

    Each line is ``- **[High] Category — `path/to/file:line`** what is wrong ...``
    (agents/pr-reviewer.md). A line that does not follow it exactly still becomes
    a finding - severity and text are all one needs - with whatever could be read.
    """
    out = []
    for raw in lines or []:
        m = _PR_LINE_RE.match(raw or "")
        if not m:
            continue
        severity = m.group(1).lower()
        rest = (raw[m.end():]).strip()
        # The bold span closes after the heading; text follows it. "[High]**" (nothing
        # between) leaves the heading empty and the whole remainder as the text.
        close = rest.find("**")
        if close == -1:
            head, text = "", rest
        else:
            head, text = rest[:close], rest[close + 2:]
        located = _location_in(head, text)
        path = line = end = None
        if located:
            path, line, end = _split_location(located)
        label = head[:head.find("`")] if "`" in head else head
        label = label.strip(" \t—–-:·*_")
        out.append(make_finding(
            f"F{len(out) + 1}", KIND_PR, severity, " ".join(text.split()),
            _trace(repo=repo or None, pull_request=str(pull_request) or None,
                   branch=branch or None, target_branch=target_branch or None,
                   commit=commit or None, path=path, line=line, line_end=end),
            type=label, late_find=bool(_LATE_RE.search(raw)), scale=scale))
    return out


# ---- documents ---------------------------------------------------------------

def _comment_text(body, start):
    """The comment that begins at `start`: its line and any indented continuation."""
    lines = body[start:].split("\n")
    parts = [lines[0].strip()]
    for follow in lines[1:]:
        if not follow.strip() or _BULLET_RE.match(follow) or _HEADING_RE.match(follow):
            break
        parts.append(follow.strip())
    return " ".join(parts)


def parse_doc_findings(body, path="", repo="", branch="", commit="", role="",
                       persona=None, scale=None):
    """Findings from one document's review body.

    ``body`` is the part of the review that lists the *current* comments (the caller
    removes the "Previous comments" status list, which describes comments already
    dealt with). The format is
    ``- **[Major] DR-2** · Feasibility · <section> — <problem>. Evidence: ...``.
    """
    out = []
    for m in _DOC_LINE_RE.finditer(body or ""):
        severity, ident = m.group(1).lower(), m.group(2).upper()
        text = _comment_text(body, m.start())
        head = _DOC_LINE_RE.match(text)
        # What follows the bold "[Sev] DR-n": " · Gap · Section — problem ...".
        after = text[head.end():] if head else ""
        kind_label, section, description = "", None, after.strip(" ·\t")
        pieces = [p.strip() for p in after.split("·", 2)]
        if len(pieces) >= 2 and pieces[0] == "" and pieces[1].lower() in DOC_TYPES:
            kind_label = pieces[1]
            rest = pieces[2] if len(pieces) > 2 else ""
            dash = _DASH_SPLIT_RE.search(rest)
            if dash:
                section = rest[:dash.start()].strip() or None
                description = rest[dash.end():].strip()
            else:
                description = rest.strip()
        out.append(make_finding(
            ident, KIND_DOC, severity, " ".join(description.split()),
            _trace(repo=repo or None, branch=branch or None, commit=commit or None,
                   path=path or None, section=section, role=role or None),
            type=kind_label, persona=persona, scale=scale))
    return out
