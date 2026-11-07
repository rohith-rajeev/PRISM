"""Documentation review: PRISM's second job, next to reviewing pull requests.

A requirement-decomposition document or a spec is reviewed against the
requirement documents it was derived from and against the codebase, and the
review is written into the same document as a "PRISM review comments" section
at its end, committed to the branch it lives on, and pushed.

The agent only *reports*. Everything that touches a file or a remote is done
here, by PRISM, behind its own checks - the same contract the PR review keeps
(see agents/_shared-contract.md). In particular PRISM, not the agent, writes the
section, and it verifies that nothing outside the section changed by a single
byte, so a document a human is about to review cannot be altered by the review.

This is an automated aid to a human reviewer, never a replacement for one: the
comments are labelled as such inside the document.

Nothing here imports Tk, and nothing here is used by the pull-request pipeline;
it only borrows engine plumbing from orchestrator.py.
"""

import json
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import orchestrator as O
from orchestrator import Cancelled, _emit_plain

# ---- identifiers -------------------------------------------------------------

AGENT_DOC_REVIEWER = "doc-reviewer"

# Pipeline stage ids. The UI progress bar knows these (app.STAGE_DEFS).
STAGE_DOC_REVIEW = "doc_review"
STAGE_DOC_WRITE = "doc_write"
STAGE_DOC_COMMIT = "doc_commit"
STAGE_DOC_PUSH = "doc_push"

# Where a feature's requirement documents often live, relative to a repo. Only a
# suggestion shown to people: requirements are optional, and nothing is assumed.
DEFAULT_REQUIREMENTS_DIR = "docs/requirements"

# The lens a document is reviewed through. One agent plays all three: PRISM puts the
# chosen persona's lens (and whether requirements were provided) into the brief, so
# each is defined here once, in one tested place, rather than in three agent files.
DEFAULT_PERSONA = "ba"
PERSONAS = {
    "ba": {
        "label": "Business Analyst",
        "aliases": ("ba", "business-analyst", "business analyst", "analyst", "functional"),
        "lens": (
            "You review as a BUSINESS ANALYST, from the functional side.\n"
            "- Do the functional requirements stated in the document align with the "
            "requirements (when they were provided)? Is every requirement covered, none "
            "contradicted or dropped, nothing added that no requirement asks for?\n"
            "- Is the document consistent within itself: business rules, flows, roles and "
            "permissions, field and validation rules, states and transitions, numbers, terms "
            "and acceptance criteria that agree with one another?\n"
            "- Is it consistent with what the codebase actually does today, functionally: the "
            "existing behaviour, rules and data the document relies on or would change?\n"
            "- What would two people implement differently? Which edge cases, error and empty "
            "states, and business rules are missing?\n"
            "Technical design is the Architect's lens: raise it only where it changes "
            "functional behaviour."),
    },
    "architect": {
        "label": "Architect",
        "aliases": ("architect", "arch", "architecture", "technical", "tech"),
        "lens": (
            "You review as an ARCHITECT, from the technical side.\n"
            "- Study the codebase's existing technical patterns first (layering and module "
            "boundaries, data access and transactions, API and error conventions, "
            "authentication and authorization, configuration, async work and queues, "
            "logging), then compare what the document proposes with them. Flag a proposal "
            "that diverges without a reason or is inconsistent with them.\n"
            "- Is the proposal valid and implementable as written: do the components, APIs, "
            "data models and integrations it relies on exist and behave as assumed; is the "
            "sequencing workable; are the data-model, migration and API-contract changes "
            "sound and backward compatible?\n"
            "- Does it lead to security loopholes (authn/authz gaps, injection, data "
            "exposure, unsafe defaults), bugs (races, partial failure, missing idempotency or "
            "transactions) or inconsistencies with the existing patterns? Flag each one.\n"
            "Functional wording is the Business Analyst's lens: raise it only where it makes "
            "the design wrong."),
    },
    "qa": {
        "label": "QA",
        "aliases": ("qa", "quality", "quality-assurance", "tester", "qe"),
        "lens": (
            "You review as QA, from the quality side.\n"
            "- Will implementing this break anything? Find what depends on the code the "
            "document changes (callers, consumers, reports, jobs, integrations, data) and "
            "where regressions are likely.\n"
            "- How does it affect the quality of the product: behaviour that silently changes, "
            "data migration and rollback risk, error handling and observability, performance "
            "and reliability under load.\n"
            "- Is it testable: acceptance criteria that can be verified, edge and negative "
            "cases, test data and environments, the automated and manual tests it will need "
            "and the existing tests it affects.\n"
            "- Release risk: feature flags, rollout and rollback, configuration that differs "
            "per environment.\n"
            "Functional intent is the Business Analyst's lens and technical design the "
            "Architect's: raise them only where they threaten quality."),
    },
}

# Never skipped, whatever the persona and whether or not requirements were provided.
ALWAYS_CHECKED = ("security", "stability", "scalab")      # "scalab" covers scalability / scalable
ALWAYS_NAMES = {"security": "security", "stability": "stability", "scalab": "scalability"}
ALWAYS_TEXT = (
    "ALWAYS CHECKED, whatever your persona: security (access control, data exposure, input "
    "validation, secrets), stability (failure modes, partial failure, retries and "
    "idempotency, backward compatibility, resource leaks) and scalability (growth in data, "
    "users and load; unbounded queries, loops and payloads; hot paths and contention). "
    "Check each through your persona's lens and state your conclusion on all three in "
    "Coverage, even when it is 'nothing found' and what you looked at.")


def normalise_persona(text):
    """The persona key for what a person typed ("BA", "business analyst", "Architect"...).

    ValueError, naming the choices, for anything else. Empty means the default.
    """
    raw = " ".join(str(text or "").strip().lower().replace("_", "-").split())
    if not raw:
        return DEFAULT_PERSONA
    for key, info in PERSONAS.items():
        if raw == key or raw in info["aliases"]:
            return key
    raise ValueError(f"Unknown reviewer persona {text!r}. Choose one of: "
                     + ", ".join(f"{k} ({v['label']})" for k, v in PERSONAS.items()))


def persona_label(key):
    return PERSONAS.get(key, PERSONAS[DEFAULT_PERSONA])["label"]


def missing_always_checks(body):
    """Which of security / stability / scalability the review never mentions.

    A check on what the reviewer *said*, not on what it did: enough to notice a review
    that skipped one of the three it is always meant to state a conclusion on.
    """
    low = (body or "").lower()
    return [w for w in ALWAYS_CHECKED if w not in low]

REMOTE = "origin"

# Limits that keep a runaway input from becoming a runaway prompt.
MAX_DOC_BYTES = 2 * 1024 * 1024
MAX_REQ_FILES = 200
MAX_DELTA_CHARS = 20000
MAX_REVIEW_CHARS = 300000
GIT_TIMEOUT = 300

SEVERITIES = ("Blocker", "Major", "Minor", "Suggestion")

# The section as it sits in the document. Markers are HTML comments, so they are
# invisible when rendered and survive a markdown round trip.
SECTION_START = "<!-- prism-review:start -->"
SECTION_END = "<!-- prism-review:end -->"
_RUN_OPEN = "<!-- prism-review:run "
_RUN_CLOSE = "<!-- prism-review:run-end -->"
# The agent's own text sits between these, so reading a review back returns
# exactly what was written and never the header PRISM generated around it.
_BODY_OPEN = "<!-- prism-review:body -->"
_BODY_CLOSE = "<!-- prism-review:body-end -->"

SECTION_TITLE = "## PRISM review comments"
SECTION_NOTICE = (
    "> Automated review by PRISM, written to **aid** the human review of this "
    "document, not to replace it. Comments can be wrong or miss things; a person "
    "decides what to act on. Newest review first; each review checks whether the "
    "previous review's comments were resolved.")

_SECTION_RE = re.compile(re.escape(SECTION_START) + r"(.*?)" + re.escape(SECTION_END),
                         re.DOTALL)
_RUN_RE = re.compile(re.escape(_RUN_OPEN) + r"(\{.*?\})\s*-->\n?(.*?)\n?"
                     + re.escape(_RUN_CLOSE), re.DOTALL)
_BODY_RE = re.compile(re.escape(_BODY_OPEN) + r"\n?(.*?)\n?" + re.escape(_BODY_CLOSE), re.DOTALL)
_FINDING_RE = re.compile(
    r"^\s*[-*]\s*\*\*\[(Blocker|Major|Minor|Suggestion)\]\s*(DR-(\d+))\*\*",
    re.IGNORECASE | re.MULTILINE)
_ID_RE = re.compile(r"\bDR-(\d+)\b")
_PREVIOUS_HEAD_RE = re.compile(r"^#{2,4}\s*Previous comments\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEAD_RE = re.compile(r"^#{2,4}\s+\S", re.MULTILINE)
_REVIEW_BLOCK_RE = re.compile(r"<<<PRISM-REVIEW>>>\s*\n(.*?)\n\s*<<<END-PRISM-REVIEW>>>",
                              re.DOTALL)


# ---- the job's shape ---------------------------------------------------------

@dataclass(frozen=True)
class DocTarget:
    """One repository whose documents are reviewed, on one branch."""

    role: str                 # "BE", "FE" or "repo" (a single-repository project)
    repo_name: str
    local_repo: str
    branch: str
    files: tuple              # paths of the documents, relative to the repository

    @property
    def label(self):
        return {"BE": "Backend", "FE": "Frontend"}.get(self.role, self.repo_name)


@dataclass(frozen=True)
class DocJobSpec:
    """Immutable snapshot of the Documentation review form.

    The counterpart of jobs.JobSpec. It carries a `kind` so the scheduler and the
    screens can tell the two apart; everything the pull-request flow reads is
    untouched.
    """

    project_dir: str
    targets: tuple                                  # of DocTarget
    requirements_path: str = ""                     # optional: "" reviews without requirements
    persona: str = DEFAULT_PERSONA                  # a key of PERSONAS
    model: str = None
    do_commit: bool = True
    do_push: bool = True
    dry_run: bool = False
    custom_instructions: str = ""
    origin: str = "manual"
    origin_ref: str = ""
    kind = "docs"

    @property
    def runner(self):
        return full_pipeline

    @property
    def label(self):
        names = " + ".join(t.label for t in self.targets)
        count = sum(len(t.files) for t in self.targets)
        return (f"Docs · {persona_label(self.persona)} · {names} · "
                f"{count} file{'' if count == 1 else 's'}")

    @property
    def dup_key(self):
        return ("docs",) + tuple(sorted(
            (str(Path(t.local_repo).expanduser()), t.branch, f)
            for t in self.targets for f in t.files))

    @property
    def clone_key(self):
        # Documents are reviewed in private worktrees, so a docs job never
        # shares a checkout with another job in the sense jobs.shares_clone_with
        # warns about. A key that can never equal a clone path says so.
        return "docs:" + "|".join(sorted(t.local_repo for t in self.targets))

    @property
    def branches_text(self):
        return "  ".join(f"{t.label}: {t.branch}" for t in self.targets)

    def summary_bits(self):
        bits = [f"{persona_label(self.persona)} review"]
        if not self.requirements_path.strip():
            bits.append("no requirements")
        if not self.do_commit:
            bits.append("no commit")
        elif not self.do_push:
            bits.append("no push")
        if self.dry_run:
            bits.append("dry run")
        return " · ".join(bits)

    def pipeline_kwargs(self):
        return dict(project_dir=self.project_dir, targets=self.targets,
                    requirements_path=self.requirements_path, persona=self.persona,
                    model=self.model,
                    do_commit=self.do_commit, do_push=self.do_push,
                    dry_run=self.dry_run, custom_instructions=self.custom_instructions)


def relevant_stages(spec):
    """The stages a document job can reach, for its progress bar."""
    stages = [STAGE_DOC_REVIEW, STAGE_DOC_WRITE]
    if spec.do_commit:
        stages.append(STAGE_DOC_COMMIT)
        if spec.do_push:
            stages.append(STAGE_DOC_PUSH)
    return stages


_ISSUED_IDS = set()
_ISSUED_LOCK = threading.Lock()


def new_run_id():
    """A readable, sortable, unique id for one run: 20261107-1432-a1b2c3.

    The id is how a run is found later (`git log --grep PRISM-<id>`) and it names the
    run's temporary worktree, so two runs must never share one. Six random hex digits
    make a clash between processes in the same minute about 1 in 16 million, and within
    this process a clash is impossible: an id already handed out is never handed out
    again.
    """
    stamp = time.strftime("%Y%m%d-%H%M")
    with _ISSUED_LOCK:
        while True:
            run_id = f"{stamp}-{uuid.uuid4().hex[:6]}"
            if run_id not in _ISSUED_IDS:
                _ISSUED_IDS.add(run_id)
                return run_id


def commit_message(run_id, brief):
    """`PRISM-<run id>: <brief>`, always one line."""
    brief = " ".join((brief or "document review comments").split())
    return f"PRISM-{run_id}: {brief}"[:200]


# ---- the review section: parse, version, render ------------------------------

@dataclass
class Run:
    """One review inside a document's section."""

    meta: dict
    body: str


def _meta_json(meta):
    # Keep the comment well-formed whatever the text holds: no "--", no ">".
    return (json.dumps(meta, ensure_ascii=True, separators=(",", ":"))
            .replace("--", "\\u002d\\u002d").replace(">", "\\u003e"))


def split_document(text):
    """(before, runs, after): the document around its review section.

    `runs` is newest first. A document without a section gives ("text", [], "").
    """
    m = _SECTION_RE.search(text or "")
    if not m:
        return text or "", [], ""
    runs = []
    for rm in _RUN_RE.finditer(m.group(1)):
        try:
            meta = json.loads(rm.group(1))
        except ValueError:
            meta = {}
        if isinstance(meta, dict):
            inner = rm.group(2)
            bm = _BODY_RE.search(inner)
            # No body markers: a section written by hand or by an older build.
            runs.append(Run(meta, (bm.group(1) if bm else inner).strip("\n")))
    return text[:m.start()], runs, text[m.end():]


def strip_section(text):
    """The document with its review section removed - what the author wrote."""
    before, runs, after = split_document(text)
    if not runs and SECTION_START not in (text or ""):
        return text or ""
    return before.rstrip("\n") + ("\n" + after.lstrip("\n") if after.strip() else "\n")


def _wrapped(body):
    return f"{_BODY_OPEN}\n{body.strip(chr(10))}\n{_BODY_CLOSE}"


def _render_run(run, newest):
    m = run.meta
    verdict = m.get("verdict") or "no verdict"
    who = f" · {m['persona_label']}" if m.get("persona_label") else ""
    title = (f"Review v{m.get('v', '?')}{who} · run `{m.get('id', '?')}` · {m.get('date', '')}"
             + (f" · commit `{m['commit'][:8]}`" if m.get("commit") else ""))
    head = f"{_RUN_OPEN}{_meta_json(m)} -->"
    if newest:
        inner = (f"### {title}\n\n**Verdict:** {verdict}"
                 + (f" · **Impact:** {m['impact']}" if m.get("impact") else "")
                 + (f" · {m['counts']}" if m.get("counts") else "")
                 + "\n\n" + _wrapped(run.body))
    else:
        inner = (f"<details>\n<summary>{title} · {verdict}"
                 + (f" · {m['counts']}" if m.get("counts") else "")
                 + "</summary>\n\n" + _wrapped(run.body) + "\n\n</details>")
    return f"{head}\n{inner}\n{_RUN_CLOSE}"


def render_section(runs):
    parts = [SECTION_START, SECTION_TITLE, "", SECTION_NOTICE, ""]
    for i, run in enumerate(runs):
        parts += [_render_run(run, newest=(i == 0)), ""]
    parts.append(SECTION_END)
    return "\n".join(parts)


def with_review(text, run):
    """`text` with `run` added as the newest review in its section.

    The section replaces itself where it already is; a document without one gets
    it appended at the very end. Everything outside the section is returned
    exactly as it came in - checked by the caller with outside_unchanged().
    """
    before, runs, after = split_document(text)
    section = render_section([run] + runs)
    if SECTION_START in (text or "") and _SECTION_RE.search(text):
        return before + section + after
    base = text or ""
    if base and not base.endswith("\n"):
        base += "\n"
    return base + ("\n" if base else "") + section + "\n"


def outside_unchanged(old_text, new_text):
    """True when only the review section differs between the two texts."""
    ob, _r, oa = split_document(old_text)
    nb, _r2, na = split_document(new_text)
    if SECTION_START in (old_text or ""):
        return ob == nb and oa == na
    # No section before: the old text must survive as a prefix, with at most the
    # one newline the append adds when the file lacked a final one.
    return nb.rstrip("\n") == (old_text or "").rstrip("\n") and not na.strip()


def _version_of(run):
    try:
        return int(run.meta.get("v"))
    except (TypeError, ValueError):
        return 0


def next_finding_number(runs):
    """The first unused DR-n, across every review kept in the section."""
    seen = [int(n) for run in runs for n in _ID_RE.findall(run.body)]
    return (max(seen) + 1) if seen else 1


def _without_previous_block(body):
    """The review body minus its "Previous comments" status list.

    Those lines describe comments that are already dealt with and must not be
    counted as open ones.
    """
    m = _PREVIOUS_HEAD_RE.search(body or "")
    if not m:
        return body or ""
    nxt = _NEXT_HEAD_RE.search(body, m.end())
    return body[:m.start()] + (body[nxt.start():] if nxt else "")


def findings(body):
    """[(severity, id, line)] for every comment in a review body, in order."""
    out = []
    for m in _FINDING_RE.finditer(_without_previous_block(body)):
        line_end = body.find("\n", m.start())
        text = body[m.start(): line_end if line_end != -1 else len(body)]
        out.append((m.group(1).capitalize(), m.group(2).upper(), text.strip()))
    return out


def severity_counts(body):
    counts = {s: 0 for s in SEVERITIES}
    for sev, _id, _line in findings(body):
        counts[sev] += 1
    return counts


def counts_text(counts):
    plural = {"Blocker": "blockers", "Suggestion": "suggestions"}
    bits = [f"{counts[s]} {plural.get(s, s.lower()) if counts[s] != 1 else s.lower()}"
            for s in SEVERITIES if counts.get(s)]
    return ", ".join(bits) or "no comments"


def latest_run(runs, persona=None):
    """The newest earlier review by this persona (newest-first `runs`).

    An Architect's re-review follows up the Architect's comments, not the Business
    Analyst's. A review written before personas existed has no persona and counts as
    a match for any: it was the one generic review of that document.
    """
    for run in runs:
        who = run.meta.get("persona")
        if persona is None or not who or who == persona:
            return run
    return None


def open_findings(runs, persona=None):
    """The comments the newest (matching) review left open - what the next must check."""
    run = latest_run(runs, persona)
    return findings(run.body) if run else []


def parse_agent_report(text):
    """(ReviewResult, body-or-None) from the agent's reply.

    The body is what is written into the document. The last complete marker pair
    wins, so an agent that quotes the format earlier does not confuse it.
    """
    clean = O.strip_ansi(text or "")
    res = O.parse_review_output(clean)
    blocks = _REVIEW_BLOCK_RE.findall(clean)
    if not blocks:
        return res, None
    body = blocks[-1].strip()
    if not body:
        return res, None
    return res, sanitise_body(body)[:MAX_REVIEW_CHARS]


def sanitise_body(body):
    """Make agent text safe to embed: it was derived from untrusted documents, so
    it must not be able to forge or close the section, or a run record."""
    body = body.replace("<!-- prism-review", "&lt;!-- prism-review")
    body = body.replace("<<<PRISM-REVIEW>>>", "").replace("<<<END-PRISM-REVIEW>>>", "")
    return body.strip("\n")


_VERDICT_FLOOR = (("block", 3), ("request-changes", 2), ("approve-with-comments", 1),
                  ("approve", 0))
_VERDICT_LABEL = {"approve": "✅ Approve", "approve-with-comments": "⚠️ Approve with comments",
                  "request-changes": "🔴 Request changes", "block": "⛔ Block"}


def reconcile_verdict(verdict_key, counts):
    """(verdict_key, changed): a verdict may not be rosier than its own comments.

    A reviewer that lists a Blocker yet says "Approve" would mislead the human
    reading the headline, so the headline is raised to match. It is only ever
    raised, never lowered.
    """
    rank = dict(_VERDICT_FLOOR)
    floor = 2 if counts.get("Blocker") else (1 if counts.get("Major") else 0)
    current = rank.get(verdict_key, None)
    if current is None:
        return {2: "request-changes", 1: "approve-with-comments", 0: "approve"}[floor], True
    if current < floor:
        return {2: "request-changes", 1: "approve-with-comments"}[floor], True
    return verdict_key, False


def verdict_label(key):
    return _VERDICT_LABEL.get(key, key)


def doc_delta(old_text, new_text, limit=MAX_DELTA_CHARS):
    """A unified diff of what the author changed, review sections ignored."""
    import difflib
    old = strip_section(old_text).splitlines(keepends=True)
    new = strip_section(new_text).splitlines(keepends=True)
    diff = "".join(difflib.unified_diff(old, new, "reviewed", "now", n=2))
    if len(diff) > limit:
        diff = diff[:limit] + f"\n... [diff cut at {limit} characters]\n"
    return diff


# ---- inputs: documents and requirements --------------------------------------

def parse_files(raw):
    """File names from free text: separated by commas, semicolons or new lines."""
    out = []
    for part in re.split(r"[\n,;]+", raw or ""):
        part = part.strip().strip("'\"")
        if part and part not in out:
            out.append(part)
    return tuple(out)


def repo_relative(paths, repo_root):
    """([relative posix paths], [skipped]) for files picked in a file browser.

    A picked file must lie inside the repository (and not in .git): documents
    are read from the branch, so a path outside the repo means nothing there.
    """
    root = Path(repo_root).expanduser().resolve()
    rels, skipped = [], []
    for raw in paths or []:
        try:
            rel = Path(raw).expanduser().resolve().relative_to(root)
        except (ValueError, OSError):
            skipped.append(str(raw))
            continue
        if not rel.parts or ".git" in rel.parts:
            skipped.append(str(raw))
        elif rel.as_posix() not in rels:
            rels.append(rel.as_posix())
    return rels, skipped


def relativise_folder(path, roots):
    """A picked requirements folder, stored relative to the repository (or the
    working folder) it lies in so it is found in the reviewed branch's own copy;
    an absolute path when it lies in none of `roots`, or is a root itself."""
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        return str(path)
    best = None
    for root in roots:
        try:
            rel = target.relative_to(Path(root).expanduser().resolve())
        except (ValueError, OSError):
            continue
        if rel.parts and (best is None or len(rel.parts) < len(best.parts)):
            best = rel               # the deepest root gives the shortest path
    return best.as_posix() if best is not None else str(target)


_BE_RE = re.compile(r"(^|[-_.])be([-_.]|$)|backend", re.IGNORECASE)
_FE_RE = re.compile(r"(^|[-_.])fe([-_.]|$)|frontend", re.IGNORECASE)


def guess_role(folder_name):
    """"BE" or "FE" from a clone's folder name, "" when it says neither."""
    be, fe = bool(_BE_RE.search(folder_name or "")), bool(_FE_RE.search(folder_name or ""))
    return "BE" if be and not fe else ("FE" if fe and not be else "")


def check_branch(branch):
    """A problem with a branch name, or None. Refuses anything git would read as
    an option or that no branch could be called."""
    branch = (branch or "").strip()
    if not branch:
        return "enter a branch"
    if branch.startswith("-") or branch.startswith("refs/") or any(c.isspace() for c in branch):
        return f"'{branch}' is not a valid branch name"
    try:
        r = subprocess.run(["git", "check-ref-format", "--branch", branch],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
    except Exception:  # noqa: BLE001
        return None            # cannot check; git itself will refuse a bad name later
    return None if r.returncode == 0 else f"'{branch}' is not a valid branch name"


def resolve_inside(root, rel):
    """`rel` resolved under `root`, or None when it escapes it or targets .git."""
    root = Path(root).resolve()
    try:
        path = (root / rel).resolve()
        parts = path.relative_to(root).parts
    except (ValueError, OSError):
        return None
    if not parts or ".git" in parts:
        return None
    return path


def find_requirements(requirements_path, roots):
    """The requirement folder, looked for where a person would have put it.

    An absolute path is taken as it is. A relative one is tried under each
    worktree root in turn (the requirements may live in only one of the repos),
    then returned as the first that exists. None when nothing matches.
    """
    raw = (requirements_path or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p if p.is_dir() else None
    for root in roots:
        cand = resolve_inside(root, raw)
        if cand is not None and cand.is_dir():
            return cand
    return None


def list_requirement_files(folder, limit=MAX_REQ_FILES):
    """Text-ish files in the folder, sorted; (paths, truncated)."""
    skip = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".pptx", ".docx",
            ".xlsx", ".ico", ".svg", ".mp4"}
    files = [p for p in sorted(Path(folder).rglob("*"))
             if p.is_file() and p.suffix.lower() not in skip
             and ".git" not in p.relative_to(folder).parts]
    return files[:limit], len(files) > limit


# ---- git ---------------------------------------------------------------------

def _git(cwd, *args, timeout=GIT_TIMEOUT, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:800]
        raise RuntimeError(f"git {args[0]} failed: {detail}")
    return r


class _Worktree:
    """A private, throwaway checkout of one branch.

    The user's own clone is never checked out, reset or edited: documents are
    read from, edited in and pushed from a worktree under the OS temp folder, so
    a review cannot disturb work in progress. (The reviewer's agent files still go
    under .opencode/ in the project folder, as they do for a pull request review.)
    """

    def __init__(self, target, run_id, emit):
        self.target = target
        self.clone = target.local_repo
        self.emit = emit
        self.root = O._worktree_root()
        self.path = self.root / f"docs-{run_id}-{target.role}"
        self.ref = ""
        self.base_commit = ""
        self.has_remote_branch = False
        self.changed = []         # documents written in this worktree
        self.commit = ""

    def __enter__(self):
        t = self.target
        self.root.mkdir(parents=True, exist_ok=True)
        fetched = _git(self.clone, "fetch", REMOTE, t.branch, check=False, timeout=GIT_TIMEOUT)
        remote_ref = f"refs/remotes/{REMOTE}/{t.branch}"
        have_remote = _git(self.clone, "rev-parse", "--verify", "--quiet", remote_ref,
                           check=False).returncode == 0
        have_local = _git(self.clone, "rev-parse", "--verify", "--quiet",
                          f"refs/heads/{t.branch}", check=False).returncode == 0
        if have_remote:
            self.ref, self.has_remote_branch = remote_ref, True
            if have_local:
                ahead = _git(self.clone, "rev-list", "--count",
                             f"{remote_ref}..refs/heads/{t.branch}", check=False)
                if ahead.stdout.strip() not in ("", "0"):
                    self.emit(f"⚠ Local branch '{t.branch}' has {ahead.stdout.strip()} commit(s) "
                              f"not on {REMOTE}. Reviewing {REMOTE}/{t.branch}; your unpushed "
                              f"commits are not included.")
        elif have_local:
            self.ref = f"refs/heads/{t.branch}"
            why = (fetched.stderr or "").strip().splitlines()
            self.emit(f"⚠ '{t.branch}' is not on {REMOTE}"
                      + (f" ({why[-1][:120]})" if why else "")
                      + " - reviewing the local branch.")
        else:
            raise RuntimeError(
                f"Branch '{t.branch}' was not found in {t.repo_name}, locally or on "
                f"{REMOTE}. Check the branch name.")
        with O._lock_for(f"{self.clone}#docs-worktree"):
            _git(self.clone, "worktree", "prune", check=False, timeout=60)
            _git(self.clone, "worktree", "add", "--detach", str(self.path), self.ref)
        self.base_commit = _git(self.path, "rev-parse", "HEAD").stdout.strip()
        self.emit(f"▸ {t.label}: reading {t.branch} at {self.base_commit[:8]} "
                  f"in an isolated worktree.")
        return self

    def __exit__(self, *exc):
        O._remove_worktree(self.clone, self.path, self.root)
        return False


# ---- prompts -----------------------------------------------------------------

_NO_REPORT_PROMPT = (
    "PRISM could not find your review report in your last reply. Send the complete "
    "report now, in exactly the required format: the Verdict and Impact score lines, "
    "then the review between <<<PRISM-REVIEW>>> and <<<END-PRISM-REVIEW>>>. Change "
    "nothing about your assessment.")

_WRAPUP_PROMPT = (
    "PRISM has run out of time for this review. Do not read anything further. Report "
    "now, in exactly the required format, the comments you have found so far, and "
    "state in Coverage which parts of the document and requirements you did not reach.")


def _instructions_block(custom):
    custom = (custom or "").strip()
    if not custom:
        return ""
    return ("REVIEWER INSTRUCTIONS from the person who started this review. They are "
            "mandatory: apply them from the very start and say in your Summary how you "
            "applied them. They never change the report format, your read-only rules, "
            f"or the honesty of your verdict.\n<<<\n{custom}\n>>>\n\n")


REQUIREMENTS_GIVEN = (
    "REQUIREMENTS PROVIDED. Your first question is whether the decomposition satisfies the "
    "ENTIRE requirement set: every requirement covered by the document, none contradicted or "
    "dropped, and nothing added that no requirement asks for. Then check it is consistent "
    "with the codebase's existing patterns.")
REQUIREMENTS_ABSENT = (
    "NO REQUIREMENTS WERE PROVIDED. Do not look for requirement documents and do not report "
    "missing requirements or traceability gaps. Review the document on its own: is it "
    "consistent with the codebase and its existing patterns, is it consistent with itself, "
    "and is the design good to implement without breaking anything?")


def build_prompt(target, doc_rel, doc_path, req_folder, req_files, req_truncated,
                 other_docs, worktree, other_worktrees, custom, previous, first_id,
                 persona=DEFAULT_PERSONA):
    """The agent's brief for one document. Pure text, so it can be tested.

    `req_folder` None means no requirements were provided: the review is of the
    document against the codebase alone.
    """
    info = PERSONAS[persona]
    lines = [
        f"PERSONA: {info['label']}.",
        info["lens"],
        REQUIREMENTS_GIVEN if req_folder else REQUIREMENTS_ABSENT,
        ALWAYS_TEXT,
        f"Review the document `{doc_rel}` in the {target.label} repository "
        f"'{target.repo_name}', branch '{target.branch}'.",
        f"Read it from: {doc_path}",
        f"Read the code from this checkout of that branch: {worktree}",
    ]
    if other_worktrees:
        lines.append("Other repositories in this project, for cross-checking (read only): "
                     + "; ".join(f"{label}: {path}" for label, path in other_worktrees))
    if req_folder:
        lines.append(f"Requirement documents (the source this document was decomposed from) "
                     f"are in: {req_folder}")
        shown = [str(p) for p in req_files]
        lines.append("Requirement files:\n" + "\n".join(f"  - {p}" for p in shown)
                     if shown else "That folder holds no readable files.")
        if req_truncated:
            lines.append(f"(Only the first {MAX_REQ_FILES} files are listed; look in the "
                         f"folder for the rest.)")
    if other_docs:
        lines.append("Other documents under review in this same run (check this one against "
                     "them for contradictions): " + "; ".join(other_docs))
    lines.append("The document may end with a '## PRISM review comments' section written by "
                 "earlier reviews. It is not part of the document under review: do not review "
                 "it or count it as content. Earlier comments that matter are given to you "
                 "below when there are any.")
    lines.append(f"Start new comments at DR-{first_id}.")
    lines.append("Follow your doc-reviewer workflow and finish with the report in the exact "
                 "format it specifies.")
    prompt = "\n".join(lines)
    if previous:
        prompt += "\n\n" + previous
    return _instructions_block(custom) + prompt


def build_previous_block(runs, delta, open_list, persona=None):
    """The incremental-review brief: what was open, and what the author changed."""
    prior = latest_run(runs, persona)
    if prior is None:
        return ""
    last = prior.meta
    listed = "\n".join(f"  {line}" for _sev, _id, line in open_list) or "  (none were open)"
    delta_text = delta.strip() if delta and delta.strip() else \
        "(no change to the document itself since that review)"
    return (
        f"PREVIOUS REVIEW - this is an INCREMENTAL review. PRISM reviewed this document "
        f"before (review v{last.get('v', '?')}, run {last.get('id', '?')}"
        + (f", at commit {last['commit'][:8]}" if last.get("commit") else "")
        + f"; verdict {last.get('verdict', 'unknown')}).\n"
        f"Comments that review left open:\n{listed}\n\n"
        f"What the author changed since (review sections ignored):\n<<<\n{delta_text}\n>>>\n\n"
        f"Follow the incremental-review steps in your workflow: report the status of every "
        f"comment above under '### Previous comments', carry still-open ones forward with "
        f"the same id, and review the changed parts in full.")


# ---- running the agent -------------------------------------------------------

def _run_agent(prompt, project_dir, emit, model, control, meter):
    """One doc-reviewer session. Returns (rc, output, session_id)."""
    exe = O.require_engine()
    O.ensure_bundled_agents(project_dir, emit=emit)
    json_mode = O.engine_supports_json(exe)
    cmd = [exe, "run", "--agent", AGENT_DOC_REVIEWER, *O._dir_args(exe, project_dir), "--auto"]
    if json_mode:
        cmd += ["--format", "json"]
    if model:
        cmd += ["--model", model]
    cmd += [prompt]
    rc, out, sid = O._run_stream_resilient(cmd, cwd=project_dir, emit=emit, control=control,
                                           json_mode=json_mode, meter=meter)
    if rc != 0 and "Agent not found" in out:
        emit(f"⚠ Engine could not find the '{AGENT_DOC_REVIEWER}' agent in {project_dir} - "
             f"reinstalling it and retrying once.")
        O.ensure_bundled_agents(project_dir, emit=emit)
        time.sleep(2)
        rc, out, sid = O._run_stream_resilient(cmd, cwd=project_dir, emit=emit, control=control,
                                               json_mode=json_mode, meter=meter)
        if rc != 0 and "Agent not found" in out:
            raise RuntimeError(f"The engine cannot find the '{AGENT_DOC_REVIEWER}' agent in "
                               f"{project_dir}/.opencode/agents.")
    return rc, out, sid


def review_document(prompt, project_dir, emit, model, control, meter, ask=None, get_note=None):
    """Run the agent on one document until it delivers a report.

    Handles what the PR flow handles: a question from the agent (handed to the
    person), a steering note left mid-run, and one nudge when the reply carried no
    parsable report. Returns (ReviewResult, body, session_id, partial). `body` is
    None when no report could be obtained - nothing is then written.
    """
    rc, raw, sid = _run_agent(prompt, project_dir, emit, model, control, meter)
    partial = O.CUT_SHORT_MARKER in (raw or "")
    res, body = parse_agent_report(raw)
    if res.incomplete_reason:
        # It says it could not read the document. Do not ask it again for a report: that
        # only pushes it towards inventing one.
        return res, None, sid, partial
    if partial and body is None and sid:
        emit("\n⚠ The review ran out of time - asking the reviewer to report what it has "
             "covered so far. This will be a partial review.")
        _rc, raw, sid = O._continue_turn(_WRAPUP_PROMPT, project_dir, sid, emit, model, control,
                                         what="wrap-up", meter=meter, timeout=O.WRAPUP_CEILING,
                                         idle_timeout=O.WRAPUP_IDLE_LIMIT,
                                         agent=AGENT_DOC_REVIEWER)
        res, body = parse_agent_report(raw)
    rounds = 0
    while rounds < O.MAX_CLARIFY_ROUNDS and sid:
        note = get_note() if get_note else None
        if not note:
            if body is not None or ask is None:
                break
            question = O.extract_question(raw)
            if not question:
                break
            if control is not None:
                control.check()
            emit("\n💬 The reviewer is asking a question - waiting for your reply …")
            answer = ask(question)
            if control is not None:
                control.check()
            if not answer:
                emit("▸ No reply given; continuing without one.")
                break
            emit(f"▸ You: {answer[:300]}")
        else:
            emit(f"\n📝 Steering note added mid-review - handing it to the reviewer: {note[:300]}")
            answer = note
        rounds += 1
        _rc, raw, sid = O._continue_turn(answer, project_dir, sid, emit, model, control,
                                         what="reply", meter=meter,
                                         agent=AGENT_DOC_REVIEWER)
        new_res, new_body = parse_agent_report(raw)
        if new_body is not None:
            res, body = new_res, new_body
    if body is None and sid and not res.incomplete_reason:
        emit("\n⚠ No review report could be read from the reviewer's reply - asking it to "
             "send the report once more.")
        if control is not None:
            control.check()
        _rc, raw, sid = O._continue_turn(_NO_REPORT_PROMPT, project_dir, sid, emit, model,
                                         control, what="report restatement", meter=meter,
                                         agent=AGENT_DOC_REVIEWER)
        res2, body2 = parse_agent_report(raw)
        if body2 is not None:
            res, body = res2, body2
    return res, body, sid, partial


# ---- the pipeline ------------------------------------------------------------

_VERDICT_ORDER = ("approve", "approve-with-comments", "request-changes", "block")


def _worst(keys):
    ranked = [k for k in keys if k in _VERDICT_ORDER]
    return max(ranked, key=_VERDICT_ORDER.index) if ranked else ""


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _write(path, text):
    # newline="" keeps line endings exactly as the text carries them.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def check_spec(spec):
    """A human-readable problem with the job as specified, or None."""
    if not spec.targets:
        return "Add at least one repository with a branch and document."
    seen = set()
    for t in spec.targets:
        if not t.files:
            return f"{t.label}: enter the document file name(s) to review."
        bad = check_branch(t.branch)
        if bad:
            return f"{t.label}: {bad}."
        if not Path(t.local_repo).is_dir() or not (Path(t.local_repo) / ".git").exists():
            return f"{t.label}: {t.local_repo} is not a git clone."
        key = str(Path(t.local_repo).resolve())
        if key in seen:
            return "Backend and Frontend point to the same clone."
        seen.add(key)
    return None


def _pipeline(project_dir, targets, requirements_path="", model=None,
              do_commit=True, do_push=True, dry_run=False, custom_instructions="",
              progress=None, emit=_emit_plain, control=None, ask=None, get_note=None,
              persona=DEFAULT_PERSONA):
    """Review -> write -> commit -> push, for every document of every target."""
    targets = tuple(targets)
    persona = normalise_persona(persona)
    spec = DocJobSpec(project_dir=project_dir, targets=targets)

    def pg(stage, state):
        if progress:
            try:
                progress(stage, state)
            except Exception:  # noqa: BLE001
                pass

    def ck():
        if control is not None:
            control.check()

    problem = check_spec(spec)
    if problem:
        raise RuntimeError(problem)
    if dry_run:
        do_commit = do_push = False
    do_push = do_push and do_commit

    run_id = new_run_id()
    emit(f"▸ Documentation review  |  run {run_id}  |  {persona_label(persona)}  |  "
         + "  |  ".join(f"{t.label} {t.branch}: {', '.join(t.files)}" for t in targets)
         + (f"  |  model {model}" if model else "  |  model (default)"))
    if dry_run:
        emit("▸ Dry run: the documents are reviewed, but nothing is written, "
             "committed or pushed.")
    O.ensure_bundled_agents(project_dir, emit=emit)
    meter = O.TokenMeter()
    stack, trees = [], []
    results, failed = [], []
    committed, pushed = [], []
    session_id = ""
    try:
        ck()
        pg(STAGE_DOC_REVIEW, "active")
        # Every worktree first: the requirements and the cross-repo reads must be
        # available to every document, and a bad branch should fail before any
        # model time is spent.
        for t in targets:
            wt = _Worktree(t, run_id, emit)
            stack.append(wt)         # registered first so a half-made one is cleaned up
            trees.append(wt.__enter__())
        # Requirements are optional. Given, the review is centred on whether the
        # decomposition satisfies all of them; absent, on the document against the codebase.
        req_folder, req_files, req_trunc = None, [], False
        if (requirements_path or "").strip():
            req_folder = find_requirements(requirements_path,
                                           [wt.path for wt in trees] + [Path(project_dir)])
            if req_folder is None:
                raise RuntimeError(
                    f"Requirement folder '{requirements_path}' was not found in the reviewed "
                    f"branch(es) or the project folder. Fix the path, or leave the "
                    f"requirements empty to review the documents against the codebase alone.")
            req_files, req_trunc = list_requirement_files(req_folder)
            emit(f"▸ Requirements: {req_folder} ({len(req_files)} file"
                 f"{'' if len(req_files) == 1 else 's'})")
            if not req_files:
                emit("⚠ The requirement folder holds no readable files; the review will say so.")
        else:
            emit("▸ No requirements given: reviewing the document against the codebase alone.")

        # Check every document up front, so a typo in the last file name does not
        # cost a model run on the first.
        docs = []                                   # (worktree, rel, path, old_text)
        for wt in trees:
            for rel in wt.target.files:
                path = resolve_inside(wt.path, rel)
                if path is None or not path.is_file():
                    raise RuntimeError(f"{wt.target.label}: '{rel}' is not a file on branch "
                                       f"'{wt.target.branch}'. Paths are relative to the "
                                       f"repository root.")
                if path.stat().st_size > MAX_DOC_BYTES:
                    raise RuntimeError(f"{wt.target.label}: '{rel}' is larger than "
                                       f"{MAX_DOC_BYTES // (1024 * 1024)} MB.")
                try:
                    docs.append((wt, rel, path, _read(path)))
                except UnicodeDecodeError:
                    raise RuntimeError(f"{wt.target.label}: '{rel}' is not a UTF-8 text "
                                       f"document.") from None
        all_docs = [f"{wt.target.label}: {rel}" for wt, rel, _p, _t in docs]
        if custom_instructions.strip():
            emit(f"▸ Custom instructions given to the reviewer: "
                 f"{custom_instructions.strip()[:300]}")

        for wt, rel, path, old_text in docs:
            ck()
            t = wt.target
            others = [(o.target.label, str(o.path)) for o in trees if o is not wt]
            _b, runs, _a = split_document(old_text)
            previous = ""
            prior = latest_run(runs, persona)
            if prior is not None:
                last = prior.meta
                delta = ""
                if last.get("commit"):
                    shown = _git(wt.path, "show", f"{last['commit']}:{rel}", check=False)
                    if shown.returncode == 0:
                        delta = doc_delta(shown.stdout, old_text)
                open_list = open_findings(runs, persona)
                previous = build_previous_block(runs, delta, open_list, persona)
                emit(f"\n▸ {rel}: incremental {persona_label(persona)} review - "
                     f"v{last.get('v', '?')} left {len(open_list)} comment(s) open.")
            else:
                emit(f"\n▸ {rel}: first {persona_label(persona)} review of this document.")
            prompt = build_prompt(
                t, rel, path, req_folder, req_files, req_trunc,
                [d for d in all_docs if d != f"{t.label}: {rel}"], wt.path, others,
                custom_instructions, previous, next_finding_number(runs), persona=persona)
            emit(f"═══ {t.label} · {rel} ═══")
            res, body, sid, partial = review_document(
                prompt, project_dir, emit, model, control, meter, ask=ask, get_note=get_note)
            session_id = sid or session_id
            if sid:
                emit(sid, "session")
            if res.incomplete_reason:
                # Same rule as a pull request: a reviewer that did not read the document
                # has no verdict. Nothing is written or committed for it.
                failed.append(rel)
                emit(f"⛔ The reviewer did not examine {rel} — {res.incomplete_reason}. "
                     f"Nothing is written for it. Fix the cause (is the file there, and "
                     f"the requirements folder readable?) and retry the job.")
                continue
            if body is None:
                failed.append(rel)
                emit(f"⚠ The reviewer did not deliver a review for {rel}; nothing is written "
                     f"for it. Retry the job to review it again.")
                continue
            skipped = missing_always_checks(body)
            if skipped:
                emit(f"⚠ The reviewer did not state a conclusion on "
                     f"{', '.join(ALWAYS_NAMES[w] for w in skipped)}"
                     f" - it is meant to for every review. Check that area yourself, or run "
                     f"the review again.")
            counts = severity_counts(body)
            key, raised = reconcile_verdict(res.verdict_key, counts)
            if raised:
                said = res.verdict_raw or "no verdict"
                emit(f"⚠ The reviewer's verdict ({said}) did not match its own comments "
                     f"({counts_text(counts)}); the headline is set to {verdict_label(key)}.")
            if partial:
                body += ("\n\n> ⚠️ **Partial review:** the reviewer ran out of time, so parts "
                         "of the document were not examined (see Coverage).")
            version = (max(_version_of(r) for r in runs) + 1) if runs else 1
            meta = {"id": run_id, "v": version, "date": time.strftime("%Y-%m-%d %H:%M"),
                    "persona": persona, "persona_label": persona_label(persona),
                    "commit": wt.base_commit,
                    "verdict": verdict_label(key) + (" (partial)" if partial else ""),
                    "impact": f"{res.impact_score}/10" if res.impact_score else "",
                    "counts": counts_text(counts)}
            new_text = with_review(old_text, Run(meta, body))
            if not outside_unchanged(old_text, new_text):
                raise RuntimeError(f"Refusing to write {rel}: the review would have changed "
                                   f"text outside its own section.")
            results.append({"role": t.label, "file": rel, "verdict_key": key,
                            "impact": res.impact_score, "impact_reason": res.impact_reason,
                            "counts": counts, "version": version})
            emit(f"▸ {rel}: {verdict_label(key)}  ·  {counts_text(counts)}  ·  review v{version}")
            if dry_run:
                emit(f"▸ Dry run - would add review v{version} to {rel}.")
                continue
            pg(STAGE_DOC_WRITE, "active")
            _write(path, new_text)
            wt.changed.append(rel)
            emit(f"▸ Wrote PRISM review comments (v{version}) to {rel}.")

        if not results:
            raise RuntimeError("No document could be reviewed. Nothing was changed.")
        pg(STAGE_DOC_REVIEW, "done")
        pg(STAGE_DOC_WRITE, "skipped" if dry_run else "done")
        if failed:
            emit(f"⚠ {len(failed)} document(s) were not reviewed: {', '.join(failed)}.")

        # ---- commit ----
        if not do_commit:
            pg(STAGE_DOC_COMMIT, "skipped")
            pg(STAGE_DOC_PUSH, "skipped")
            if not dry_run:
                emit("\n⚠ Commit is turned off: the review comments were written to "
                     "temporary worktrees that are discarded, so nothing is kept. "
                     "Turn on Commit to keep a review.")
        else:
            pg(STAGE_DOC_COMMIT, "active")
            emit("\n═══ Commit ═══")
            for wt in trees:
                ck()
                if not wt.changed:
                    continue
                mine = [r for r in results if r["role"] == wt.target.label]
                total = {s: sum(r["counts"][s] for r in mine) for s in SEVERITIES}
                who = persona_label(persona)
                brief = (f"{who} review comments v{mine[0]['version']} on {wt.changed[0]}"
                         if len(wt.changed) == 1
                         else f"{who} review comments on {len(wt.changed)} documents")
                msg = commit_message(run_id, f"{brief} ({counts_text(total)})")
                _git(wt.path, "add", "--", *wt.changed)
                _git(wt.path, "commit", "-m", msg)
                wt.commit = _git(wt.path, "rev-parse", "HEAD").stdout.strip()
                committed.append(wt)
                emit(f"✔ {wt.target.label}: committed {wt.commit[:8]} - {msg}")
            pg(STAGE_DOC_COMMIT, "done")

            # ---- push ----
            if not do_push:
                pg(STAGE_DOC_PUSH, "skipped")
                emit("\n⚠ Push is turned off: the commit lives in a temporary worktree "
                     "that is discarded, so it is not kept. Turn on Push to publish it.")
            else:
                pg(STAGE_DOC_PUSH, "active")
                emit("\n═══ Push ═══")
                for wt in committed:
                    ck()
                    _push(wt, run_id, emit)
                    pushed.append(wt)
                    emit(f"✔ {wt.target.label}: pushed {wt.commit[:8]} to "
                         f"{REMOTE}/{wt.target.branch}")
                pg(STAGE_DOC_PUSH, "done")
    finally:
        for wt in reversed(stack):
            try:
                wt.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    worst = _worst([r["verdict_key"] for r in results])
    scores = [int(r["impact"]) for r in results if str(r["impact"]).isdigit()]
    blockers = sum(r["counts"]["Blocker"] for r in results)
    comments = sum(sum(r["counts"].values()) for r in results)
    impact = max(scores) if scores else None
    reason = next((r["impact_reason"] for r in results
                   if impact is not None and str(r["impact"]) == str(impact)), "")
    emit(f"\n◆ Verdict: {verdict_label(worst) or 'unknown'}   "
         f"Impact: {impact if impact is not None else '?'}/10 {reason}".rstrip())
    emit(f"▸ {len(results)} document(s) reviewed, {comments} comment(s), {blockers} blocker(s)"
         f" - run {run_id}." + (f" Find it later with: git log --grep 'PRISM-{run_id}'"
                               if committed else ""))
    return {"run_id": run_id, "documents": len(results), "not_reviewed": len(failed),
            "comments": comments, "blockers": blockers, "committed": bool(committed),
            "pushed": bool(committed) and len(pushed) == len(committed),
            "stopped": None, "review": None, "tokens": meter.total(),
            "session_id": session_id}


def _push(wt, run_id, emit):
    """Push the worktree's commit to its branch - never forced.

    One automatic recovery: if the branch moved on the remote, the single commit
    is replayed on top of it (it only touches the end of documents) and pushed
    again. Anything beyond that stops with the commit kept under a local ref, so
    the review is never lost.
    """
    t = wt.target
    spec = f"HEAD:refs/heads/{t.branch}"
    r = _git(wt.path, "push", REMOTE, spec, check=False)
    if r.returncode == 0:
        return
    first = (r.stderr or r.stdout or "").strip()
    rejected = any(w in first.lower() for w in ("rejected", "non-fast-forward", "fetch first"))
    if rejected:
        emit(f"⚠ {t.label}: {REMOTE}/{t.branch} moved on - replaying the review commit on top.")
        f = _git(wt.path, "fetch", REMOTE, t.branch, check=False)
        if f.returncode == 0:
            rb = _git(wt.path, "rebase", f"{REMOTE}/{t.branch}", check=False)
            if rb.returncode == 0:
                r2 = _git(wt.path, "push", REMOTE, spec, check=False)
                if r2.returncode == 0:
                    return
                first = (r2.stderr or r2.stdout or "").strip()
            else:
                _git(wt.path, "rebase", "--abort", check=False)
                first = "the review commit conflicts with newer changes on the branch"
    keep = f"refs/prism/docs/{run_id}-{t.role}"
    try:
        sha = _git(wt.path, "rev-parse", "HEAD").stdout.strip()
        _git(wt.clone, "update-ref", keep, sha)
        saved = f" The commit is kept in your clone as {keep}."
    except Exception:  # noqa: BLE001
        saved = ""
    raise RuntimeError(f"{t.label}: push to {REMOTE}/{t.branch} failed - "
                       f"{first[:400]}.{saved}")


def full_pipeline(**kwargs):
    """Entry point the scheduler calls for a documentation job."""
    return _pipeline(**kwargs)


# ---- follow-up questions after a review --------------------------------------

def run_followup(request, project_dir, model=None, session_id=None, emit=_emit_plain,
                 control=None):
    """One follow-up turn with the document reviewer. Returns (reply, session_id).

    Continues the review's own session so the reviewer keeps the whole review in
    context. Read-only by construction: the agent's permissions deny every write.
    """
    prompt = ("A person is following up on your document review. Answer their question about "
              "that review and the documents it covered, grounded in what you read. Do not "
              "edit anything and do not repeat the whole report.\n<<<\n"
              f"{request}\n>>>")
    if not session_id:
        raise RuntimeError("This review has no conversation to continue. Retry the job first.")
    O.ensure_bundled_agents(project_dir, emit=emit)
    _rc, text, sid = O._continue_turn(prompt, project_dir, session_id, emit, model, control,
                                      what="follow-up", agent=AGENT_DOC_REVIEWER)
    return text, (sid or session_id)
