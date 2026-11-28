"""The documents a job was given, as text for its live chat (chat.py).

A document review's chat answers from the job's details (decision, comments, standards)
and, with this module, from the documents themselves: each reviewed document as it was
reviewed and each requirement file provided. Then a question about what a document or a
requirement says is answered from its words, not from memory or a guess.

Where each text comes from, read-only (the person's clone and files are never changed):

* a document on a branch: the commit the review read (from the comments' traceability),
  else the branch on the remote, else the local branch - via `git show`, with PRISM's own
  review section removed so it is what the author wrote;
* an uploaded document: its file where it is;
* the requirements: the folder on the reviewed commit (or the branch), else on disk.

Word, PDF and Excel files are read with extract.py. The chat prompt carries the text up
to a size budget; a document that does not fit whole is cut there and its full text is
saved under PRISM's settings folder, with the path given so the reviewer can open it.

A pull-request job is given no documents: its chat answers from the review's details and
the reviewer's own reading of the code. Stdlib only and free of Tkinter.
"""
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

import config
import extract as X

REMOTE = "origin"
# How much document text one chat prompt carries: whole documents up to these sizes, so
# a question is answered from the words themselves; the rest is a file to open.
MAX_FILE_CHARS = 40_000
MAX_TOTAL_CHARS = 100_000
MAX_REQ_FILES = 40
GIT_TIMEOUT = 60
TEXT_SUFFIXES = (".txt", ".text", ".md", ".markdown", ".rst", ".csv", ".json", ".yaml",
                 ".yml", ".xml", ".html", ".htm", ".adoc", "")
_SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".pptx", ".ico", ".svg", ".mp4"}


class Source:
    """One document's text: `kind` is "document" or "requirement"."""

    def __init__(self, kind, name, where, text="", problem=""):
        self.kind, self.name, self.where = kind, name, where
        self.text, self.problem = text, problem


# ---- reading ----------------------------------------------------------------------

def _git_bytes(repo, *args):
    try:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True,
                           timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _ref_ok(repo, ref):
    return bool(ref) and _git_bytes(repo, "rev-parse", "--verify", "--quiet",
                                    ref + "^{commit}") is not None


def _refs(repo, branch, commit=""):
    """The refs to read a target from, best first: the reviewed commit, then the branch."""
    out = []
    for ref in (commit, f"refs/remotes/{REMOTE}/{branch}" if branch else "",
                f"refs/heads/{branch}" if branch else ""):
        if ref and ref not in out and _ref_ok(repo, ref):
            out.append(ref)
    return out


def _decode(data, name):
    """Text from a file's bytes: Word, PDF and Excel extracted, anything else decoded."""
    suffix = Path(name).suffix.lower()
    if suffix in X.CONVERTIBLE:
        fd, tmp = tempfile.mkstemp(suffix=suffix)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return X.extract_text(tmp)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if b"\x00" in data[:4096]:
        raise X.ExtractError("it is not a text document")
    return data.decode("utf-8", "replace")


def _author_text(text):
    """The document without PRISM's review section (the comments are in the details)."""
    import docreview  # noqa: PLC0415 - docreview imports a lot; only needed here
    return docreview.strip_section(text)


def _read_blob(repo, refs, rel):
    """(text, ref used) for `rel` on the first ref that has it, or (None, "")."""
    spec_path = rel.replace(os.sep, "/")
    for ref in refs:
        data = _git_bytes(repo, "show", f"{ref}:{spec_path}")
        if data is not None:
            return _decode(data, rel), ref
    return None, ""


def _short(ref):
    if re.fullmatch(r"[0-9a-f]{7,40}", ref or ""):
        return ref[:8]
    return re.sub(r"^refs/(?:remotes|heads)/", "", ref or "")


def _commits_from(findings):
    """{(repo, path): commit} from the comments' traceability: what the review read."""
    out = {}
    for f in findings or []:
        trace = (f or {}).get("traceability") or {}
        if trace.get("commit") and trace.get("path"):
            out.setdefault((trace.get("repo") or "", str(trace["path"]).replace("\\", "/")),
                           trace["commit"])
    return out


def _documents(spec, commits):
    out = []
    for t in spec.targets:
        if t.has_repo:
            for rel in t.files:
                key = (t.repo_name, rel.replace("\\", "/"))
                refs = _refs(t.local_repo, t.branch, commits.get(key, ""))
                name = f"{t.label}: {rel.replace(os.sep, '/')}"
                try:
                    text, ref = _read_blob(t.local_repo, refs, rel)
                except X.ExtractError as e:
                    out.append(Source("document", name, t.branch, problem=str(e)))
                    continue
                if text is None:
                    out.append(Source("document", name, t.branch,
                                      problem=f"not found on {t.branch}"))
                else:
                    out.append(Source("document", name, f"{t.repo_name} @ {_short(ref)}",
                                      _author_text(text)))
        for u in t.uploads:
            name = f"{t.label}: {u.name} (uploaded)"
            try:
                text = _decode(Path(u.path).read_bytes(), u.path)
            except (OSError, X.ExtractError) as e:
                out.append(Source("document", name, u.path,
                                  problem=str(e) if isinstance(e, X.ExtractError)
                                  else "the file can no longer be read"))
                continue
            out.append(Source("document", name, u.path, _author_text(text)))
    return out


def _wanted(rel):
    p = Path(rel)
    return (p.suffix.lower() not in _SKIP_SUFFIXES and not p.name.startswith("~$")
            and ".git" not in p.parts)


def _requirements(spec, commits):
    raw = (getattr(spec, "requirements_path", "") or "").strip()
    if not raw:
        return []
    out = []
    if not Path(raw).is_absolute():
        # On the branch that was reviewed, as the reviewer read it.
        rel = raw.replace("\\", "/").strip("/")
        for t in spec.targets:
            if not t.has_repo:
                continue
            reviewed = next((c for (repo, _p), c in commits.items() if repo == t.repo_name), "")
            for ref in _refs(t.local_repo, t.branch, reviewed):
                listing = _git_bytes(t.local_repo, "ls-tree", "-r", "--name-only", ref,
                                     "--", rel + "/")
                names = [n for n in (listing or b"").decode("utf-8", "replace").splitlines()
                         if n and _wanted(n)]
                if not names:
                    continue
                for name in sorted(names)[:MAX_REQ_FILES]:
                    data = _git_bytes(t.local_repo, "show", f"{ref}:{name}")
                    out.append(_req_source(name, f"{t.repo_name} @ {_short(ref)}", data))
                return out
    import docreview  # noqa: PLC0415
    roots = [t.local_repo for t in spec.targets if t.has_repo] + [spec.project_dir]
    folder = docreview.find_requirements(raw, [r for r in roots if r])
    if folder is None:
        return [Source("requirement", raw, "", problem="the requirements folder was not found")]
    files = [p for p in sorted(Path(folder).rglob("*"))
             if p.is_file() and _wanted(str(p.relative_to(folder)))][:MAX_REQ_FILES]
    for p in files:
        try:
            data = p.read_bytes()
        except OSError:
            data = None
        out.append(_req_source(str(p.relative_to(folder)).replace(os.sep, "/"), str(folder),
                               data))
    return out


def _req_source(name, where, data):
    if data is None:
        return Source("requirement", name, where, problem="it could not be read")
    try:
        return Source("requirement", name, where, _decode(data, name))
    except X.ExtractError as e:
        return Source("requirement", name, where, problem=str(e))


def gather(spec, findings=None):
    """Every document the job was given, as Source objects (an empty list for a pull
    request). Never raises: what cannot be read says why."""
    if getattr(spec, "kind", "pr") != "docs":
        return []
    commits = _commits_from(findings)
    out = []
    for part in (_documents, _requirements):
        try:
            out += part(spec, commits)
        except Exception as e:  # noqa: BLE001 - the chat still answers from the details
            out.append(Source("document" if part is _documents else "requirement",
                              "(the job's " + ("documents" if part is _documents
                                               else "requirements") + ")", "",
                              problem=f"could not be read ({type(e).__name__})"))
    return out


# ---- as text for the prompt -----------------------------------------------------

def _full_text_file(job_id, source):
    """Save a document's whole text where the reviewer can open it; its path."""
    folder = config.CONFIG_DIR / "chat-workspace" / "sources" / re.sub(
        r"[^0-9A-Za-z_-]", "_", job_id or "job")
    folder.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^0-9A-Za-z._-]+", "_", source.name)[:80].strip("_") or "document"
    digest = hashlib.sha1(source.name.encode("utf-8")).hexdigest()[:8]
    path = folder / f"{stem}-{digest}.txt"
    path.write_text(source.text, encoding="utf-8", newline="\n")
    return path


def render(sources, job_id="", max_file=MAX_FILE_CHARS, max_total=MAX_TOTAL_CHARS):
    """The documents as one text block for the chat prompt ("" when there are none)."""
    if not sources:
        return ""
    parts, used = [], 0
    for s in sources:
        head = f"=== {s.kind.upper()}: {s.name}" + (f" ({s.where})" if s.where else "") + " ==="
        if s.problem:
            parts.append(f"{head}\n[Not available to this chat: {s.problem}.]")
            continue
        text = s.text.strip("\n")
        room = max(0, min(max_file, max_total - used))
        if len(text) <= room:
            parts.append(f"{head}\n{text}")
            used += len(text)
            continue
        try:
            full = _full_text_file(job_id, s)
            where = f"The whole text is in {full}: open it when the answer lies past this point."
        except OSError:
            where = "Its whole text could not be saved, so say the answer may lie past this point."
        shown = text[:room]
        used += len(shown)
        parts.append(f"{head}\n{shown}\n[... cut here: {len(shown):,} of {len(text):,} "
                     f"characters shown. {where}]")
    return "\n\n".join(parts)


def for_job(spec, findings=None, job_id=""):
    """gather() and render() together: the text block for one chat question."""
    try:
        return render(gather(spec, findings), job_id=job_id)
    except Exception:  # noqa: BLE001 - never stop the chat over its documents
        return ""
