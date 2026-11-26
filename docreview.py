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

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import config
import extract as X
import findings as findings_mod
import notifier
import officewrite
import orchestrator as O
from pathutil import native_any, native_path, native_rel, posix_rel
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
# Checking a whole repository out can legitimately take many minutes on a slow disk
# with a virus scanner watching it, so that one step gets far longer than a query
# does - and says it is alive, so a slow machine is not mistaken for a stuck one.
CHECKOUT_TIMEOUT = 1200
CHECKOUT_BEAT = 30

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

# Documents a review can read (FR-DOC-IN-4): plain text, Markdown, Word, PDF, Excel.
TEXT_TYPES = (".txt", ".text", ".md", ".markdown", ".rst")
UPLOAD_TYPES = TEXT_TYPES + (".docx", ".pdf", ".xlsx")
UPLOAD_TYPE_NAMES = "plain text, Markdown, Word (.docx), PDF or Excel (.xlsx)"
# The role of the one target of a review that has no repository at all (FR-DOC-IN-7).
ROLE_NONE = "none"


def upload_type_problem(path):
    """Why a file cannot be added as an uploaded document, or None when it can."""
    suffix = Path(str(path)).suffix.lower()
    if suffix not in UPLOAD_TYPES:
        return (f"{Path(str(path)).name}: {suffix or 'a file without an extension'} is not a "
                f"document type a review can read ({UPLOAD_TYPE_NAMES}).")
    return None


@dataclass(frozen=True)
class Upload:
    """A document given as a file (dropped, uploaded, or a path outside the repository)
    rather than as a path on the branch.

    It is reviewed even when it is not committed anywhere. The comments go into a local
    copy next to it; it reaches the branch only when the person set `dest` - a path
    inside the repository - and Commit is on (FR-DOC-IN-10). PRISM never guesses `dest`.
    """

    path: str                 # the local file, absolute
    dest: str = ""            # where to commit it in the target's repository; "" = nowhere

    def __post_init__(self):
        object.__setattr__(self, "path", native_path(self.path))
        object.__setattr__(self, "dest", native_rel(self.dest))

    @property
    def name(self):
        return Path(self.path).name


@dataclass(frozen=True)
class DocTarget:
    """One repository whose documents are reviewed, on one branch.

    A target with no `local_repo` (role ROLE_NONE) is a review with no repository: it
    carries uploaded documents only and is judged without a codebase.
    """

    role: str                 # "BE", "FE", "repo" (a single-repository project) or "none"
    repo_name: str
    local_repo: str
    branch: str
    files: tuple              # paths of the documents, relative to the repository
    uploads: tuple = ()       # of Upload: files reviewed from where they are

    def __post_init__(self):
        # Whatever separators were typed, store the ones this OS understands.
        object.__setattr__(self, "local_repo", native_path(self.local_repo))
        object.__setattr__(self, "files", tuple(dict.fromkeys(
            f for f in (native_rel(x) for x in (self.files or ())) if f)))
        ups, seen = [], set()
        for u in self.uploads or ():
            u = u if isinstance(u, Upload) else Upload(*u) if isinstance(u, (tuple, list)) \
                else Upload(str(u))
            if u.path and u.path not in seen:
                seen.add(u.path)
                ups.append(u)
        object.__setattr__(self, "uploads", tuple(ups))

    @property
    def has_repo(self):
        return bool(self.local_repo)

    @property
    def label(self):
        if not self.has_repo:
            return "Uploaded documents"
        return {"BE": "Backend", "FE": "Frontend"}.get(self.role, self.repo_name)

    @property
    def count(self):
        return len(self.files) + len(self.uploads)


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
    # Where to post the alert for a finished review ("" = nowhere) and the name of
    # the person who ran it; both are read from the saved settings when the job is
    # created, like a pull-request job's webhook.
    webhook_url: str = None
    reviewer: str = ""
    # The person responsible for the document and for answering the review (FR-OWN).
    # Not the reviewer: that is whoever ran PRISM. Required - a job does not start
    # without one.
    owner: str = ""
    kind = "docs"

    def __post_init__(self):
        object.__setattr__(self, "project_dir", native_path(self.project_dir))
        object.__setattr__(self, "requirements_path", native_any(self.requirements_path))
        object.__setattr__(self, "owner", config.clean_username(self.owner))

    @property
    def runner(self):
        return full_pipeline

    @property
    def label(self):
        names = " + ".join(t.label for t in self.targets)
        count = sum(t.count for t in self.targets)
        return (f"Docs · {persona_label(self.persona)} · {names} · "
                f"{count} file{'' if count == 1 else 's'}")

    @property
    def dup_key(self):
        return ("docs",) + tuple(sorted(
            [(str(Path(t.local_repo)) if t.has_repo else "", t.branch, f)
             for t in self.targets for f in t.files]
            + [(str(Path(t.local_repo)) if t.has_repo else "", t.branch, "upload:" + u.path)
               for t in self.targets for u in t.uploads]))

    @property
    def clone_key(self):
        # Documents are reviewed in private worktrees, so a docs job never
        # shares a checkout with another job in the sense jobs.shares_clone_with
        # warns about. A key that can never equal a clone path says so.
        return "docs:" + "|".join(sorted(t.local_repo for t in self.targets))

    @property
    def branches_text(self):
        return "  ".join(f"{t.label}: {t.branch}" if t.has_repo else "no repository"
                         for t in self.targets)

    def summary_bits(self):
        bits = [f"{persona_label(self.persona)} review"]
        if self.owner:
            bits.append(f"owner {self.owner}")
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
                    dry_run=self.dry_run, custom_instructions=self.custom_instructions,
                    webhook_url=self.webhook_url, reviewer=self.reviewer, owner=self.owner)


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
    # Who owns the request (answers the review) and who ran it, side by side on the
    # version's heading - never one name for both. A review written before owners
    # existed has none and reads exactly as it was written.
    owner = config.clean_username(m.get("owner"))
    if owner:
        who += f" · owner {owner}"
    # Written into the document so the review can be traced to the person who ran it.
    by = config.clean_username(m.get("reviewer"))
    if by:
        who += f" · by {by}"
    title = (f"Review v{m.get('v', '?')}{who} · run `{m.get('id', '?')}` · {m.get('date', '')}"
             + (f" · commit `{m['commit'][:8]}`" if m.get("commit") else ""))
    head = f"{_RUN_OPEN}{_meta_json(m)} -->"
    # v7.0 reviews carry a binary decision; an older version keeps its own word.
    label = "Decision" if m.get("decision") else "Verdict"
    if newest:
        inner = (f"### {title}\n\n**{label}:** {verdict}"
                 + (f" · **Impact:** {m['impact']}" if m.get("impact") else "")
                 + (f" · {m['counts']}" if m.get("counts") else "")
                 + (f"\n\n**Earlier comments:** {m['earlier']}" if m.get("earlier") else "")
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


_CONTINUES_RE = re.compile(r"^\s+\S")          # an indented line continues the bullet above
_BULLET_RE = re.compile(r"^\s*[-*]\s")


def findings(body):
    """[(severity, id, line)] for every comment in a review body, in order.

    `line` is the whole comment as ONE line. A reviewer sometimes wraps a long bullet
    over several indented lines (the report template itself shows one wrapped); those
    continuation lines belong to the comment and are joined on, so the text a later
    review is shown, and the text PRISM puts back when it keeps a comment open, is
    never cut off after its first line.
    """
    text = _without_previous_block(body)
    lines = text.split("\n")
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos)
        pos += len(ln) + 1
    out = []
    for m in _FINDING_RE.finditer(text):
        i = max(k for k, st in enumerate(starts) if st <= m.start())
        parts = [lines[i].strip()]
        for nxt in lines[i + 1:]:
            if (not nxt.strip() or _BULLET_RE.match(nxt) or _NEXT_HEAD_RE.match(nxt)
                    or nxt.lstrip().startswith("<<<") or not _CONTINUES_RE.match(nxt)):
                break
            parts.append(nxt.strip())
        out.append((m.group(1).capitalize(), m.group(2).upper(), " ".join(parts)))
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
    if res.incomplete_reason and res.verdict_key != "incomplete" and findings(body):
        # The reviewer is told to write no review block when it could not read what it
        # was given, so a delivered block IS a review. A phrase like "did not read the
        # requirements" inside it is the reviewer describing its own coverage (often
        # about a part of the requirements, or a repeat of the other document's read)
        # and failed a perfectly good review of the frontend document as "not examined".
        # Keep the review and carry the phrase as a note. Only when it carries comments:
        # a block with none that says it read nothing is the placeholder PRISM refuses.
        res.caveat, res.incomplete_reason = res.incomplete_reason, ""
    return res, sanitise_body(body)[:MAX_REVIEW_CHARS]


def sanitise_body(body):
    """Make agent text safe to embed: it was derived from untrusted documents, so
    it must not be able to forge or close the section, or a run record."""
    body = body.replace("<!-- prism-review", "&lt;!-- prism-review")
    body = body.replace("<<<PRISM-REVIEW>>>", "").replace("<<<END-PRISM-REVIEW>>>", "")
    return professionalise(body.strip("\n"))


# Conversational narration the comments must not carry: "I reviewed the document",
# "We checked ...", "Let me start by ...". The comments are a record, not a chat.
_NARRATION_RE = re.compile(
    r"^(?:(?:I|We)(?:'ve|\s+have|\s+also|\s+then|\s+first)*\s+"
    r"(?:reviewed|read|examined|checked|looked|went|analy[sz]ed|inspected|verified|"
    r"cross-?checked|compared|started|began|focused|noticed|found|observed)\b"
    r"|(?:Let me|Let's|Here(?:'s| is| are)|Below (?:is|are)|As requested|Sure|Certainly|"
    r"Based on my (?:review|analysis))\b)[^.\n]*\.?\s*", re.IGNORECASE)
_LEADING_PERSON_RE = re.compile(r"^(?:I|We)\s+(?=(?:could|couldn't|did|didn't|was|were|recommend|"
                                r"suggest|am|are|cannot|can't)\b)", re.IGNORECASE)


def professionalise(body):
    """The review body with narration removed and prose lines made into bullets.

    Applies only to the free-text parts (Summary and Coverage). Comment lines,
    headings, tables, quotes and anything already a list item are left alone, so
    the shape PRISM counts and tracks comments by is never touched.
    """
    out, section = [], ""
    for line in body.split("\n"):
        head = re.match(r"^#{2,4}\s*(.+?)\s*$", line)
        if head:
            section = head.group(1).strip().lower()
            out.append(line)
            continue
        text = line.strip()
        if not text or section not in ("summary", "coverage") or text[0] in "-*+|>#<`" \
                or re.match(r"^\d+[.)]\s", text):
            out.append(line)
            continue
        text = _NARRATION_RE.sub("", text, count=1).strip()
        text = _LEADING_PERSON_RE.sub("", text)
        if text:
            out.append("- " + text[0].upper() + text[1:])
    return "\n".join(out)


# A document review ends in exactly one of two decisions (FR-VER). Approve: work may
# start from this document. Request changes: work may not start until the open
# findings are addressed and the document is reviewed again. There is no third one:
# "Approve with comments" read as permission to proceed while a Major was open, and
# a Blocker is a comment, not a decision.
DECISION_APPROVE = "approve"
DECISION_CHANGES = "request-changes"
DECISIONS = (DECISION_APPROVE, DECISION_CHANGES)
_DECISION_WORDS = {DECISION_APPROVE: "Approve", DECISION_CHANGES: "Request changes"}
_DECISION_MEANING = {DECISION_APPROVE: "work may start",
                     DECISION_CHANGES: "work may not start"}
_DECISION_GLYPH = {DECISION_APPROVE: "✅", DECISION_CHANGES: "🔴"}


def decide(counts, partial=False, large=False):
    """The decision for one document, from its OPEN comments - PRISM's, never the reviewer's.

    Any open Blocker or Major comment means Request changes; so does a review PRISM had
    to cut short (parts were not read) and a change classified as large or expensive.
    Otherwise - only Minor, only Suggestion, or no comments - Approve. `counts` are the
    comments that remain open: carried forward, partly resolved and not assessed ones
    count; resolved and withdrawn ones are not in the body's comment list at all.
    """
    if partial or large or counts.get("Blocker") or counts.get("Major"):
        return DECISION_CHANGES
    return DECISION_APPROVE


def decision_words(key):
    """"Approve" / "Request changes" - the decision in words, no glyph."""
    return _DECISION_WORDS.get(key, "Request changes")


def decision_meaning(key):
    return _DECISION_MEANING.get(key, _DECISION_MEANING[DECISION_CHANGES])


def verdict_label(key):
    """The decision as shown everywhere, in the same words every time (FR-VER-7):
    "✅ Approve — work may start" / "🔴 Request changes — work may not start".

    Anything that is not one of the two decisions reads as Request changes: a document
    decision is never rosier than PRISM can show it to be.
    """
    key = key if key in DECISIONS else DECISION_CHANGES
    return f"{_DECISION_GLYPH[key]} {decision_words(key)} — {decision_meaning(key)}"


def doc_delta(old_text, new_text, limit=MAX_DELTA_CHARS):
    """A unified diff of what the author changed, review sections ignored."""
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
        part = native_rel(part)
        if part and part not in out:
            out.append(part)
    return tuple(out)


def repo_relative(paths, repo_root):
    """([relative posix paths], [skipped]) for files picked in a file browser.

    A picked file must lie inside the repository (and not in .git): documents
    are read from the branch, so a path outside the repo means nothing there.
    """
    root = Path(native_path(repo_root)).resolve()
    rels, skipped = [], []
    for raw in paths or []:
        try:
            rel = Path(native_path(raw)).resolve().relative_to(root)
        except (ValueError, OSError):
            skipped.append(str(raw))
            continue
        if not rel.parts or ".git" in rel.parts:
            skipped.append(str(raw))
        elif str(rel) not in rels:
            rels.append(str(rel))
    return rels, skipped


def relativise_folder(path, roots):
    """A picked requirements folder, stored relative to the repository (or the
    working folder) it lies in so it is found in the reviewed branch's own copy;
    an absolute path when it lies in none of `roots`, or is a root itself."""
    try:
        target = Path(native_path(path)).resolve()
    except OSError:
        return str(path)
    best = None
    for root in roots:
        try:
            rel = target.relative_to(Path(native_path(root)).resolve())
        except (ValueError, OSError):
            continue
        if rel.parts and (best is None or len(rel.parts) < len(best.parts)):
            best = rel               # the deepest root gives the shortest path
    return str(best) if best is not None else str(target)


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
    root = Path(native_path(root)).resolve()
    try:
        path = (root / native_rel(rel)).resolve()
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
    raw = native_any(requirements_path)
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p if p.is_dir() else None
    for root in roots:
        cand = resolve_inside(root, raw)
        if cand is not None and cand.is_dir():
            return cand
    return None


def list_requirement_files(folder, limit=MAX_REQ_FILES):
    """Text-ish files in the folder, sorted; (paths, truncated)."""
    skip = {".png", ".jpg", ".jpeg", ".gif", ".zip", ".pptx", ".ico", ".svg", ".mp4"}
    files = [p for p in sorted(Path(folder).rglob("*"))
             if p.is_file() and p.suffix.lower() not in skip
             and not p.name.startswith("~$")             # Office's lock files
             and ".git" not in p.relative_to(folder).parts]
    return files[:limit], len(files) > limit


def prepare_requirements(files, folder, scratch, emit=_emit_plain):
    """Turn Word, PDF and Excel requirements into text the reviewer can read.

    Returns (files to give the reviewer, note for the prompt, number converted).
    Text files pass through untouched. Each .docx/.xlsx/.pdf is extracted into
    `scratch` (every sheet of a workbook, under its own heading); the originals
    are never modified. One that cannot be read is reported - in the log and to
    the reviewer - rather than silently dropped.
    """
    ready, notes, unreadable, converted = [], [], [], 0
    scratch = Path(scratch)
    for p in files:
        if p.suffix.lower() not in X.CONVERTIBLE:
            ready.append(p)
            continue
        try:
            rel = p.relative_to(folder)
        except ValueError:
            rel = Path(p.name)
        try:
            text = X.extract_text(p)
        except X.ExtractError as e:
            unreadable.append(f"{rel} ({e})")
            emit(f"⚠ Requirement {rel}: {e}; the reviewer is told it could not be read.")
            continue
        out = scratch / (re.sub(r"[\\/]+", "__", str(rel)) + ".txt")
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        ready.append(out)
        converted += 1
        notes.append(f"  - {out} is the text of {rel}")
    parts = []
    if notes:
        parts.append("These requirement files are text extracted from the original Word, PDF "
                     "or Excel files (an Excel workbook lists every sheet under its own "
                     "heading):\n" + "\n".join(notes))
    if unreadable:
        parts.append("These requirement files could NOT be read as text, so say in the "
                     "review that they were not considered: " + "; ".join(unreadable))
    return ready, "\n".join(parts), converted


# ---- git ---------------------------------------------------------------------

def _git(cwd, *args, timeout=GIT_TIMEOUT, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:800]
        raise RuntimeError(f"git {args[0]} failed: {detail}")
    return r


def _git_patient(cwd, emit, what, *args, timeout=CHECKOUT_TIMEOUT, env_extra=None):
    """Run a slow git command, reporting progress, and stop its whole process tree
    if it overruns. Returns (returncode, output); returncode is None on a timeout.

    Output goes to a temp file rather than a pipe so a chatty child can never fill
    a pipe and stall the command, and a helper git started (a filter, a credential
    prompt) that outlives `git` itself cannot keep us waiting on a closed pipe.
    """
    env = dict(os.environ)
    env.update(env_extra or {})
    with tempfile.TemporaryFile() as out:
        proc = subprocess.Popen(["git", *args], cwd=str(cwd), stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT, env=env)
        started = time.monotonic()
        while True:
            try:
                proc.wait(timeout=CHECKOUT_BEAT)
                break
            except subprocess.TimeoutExpired:
                spent = time.monotonic() - started
                if spent >= timeout:
                    O.kill_tree(proc, force=True)
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pass
                    return None, ""
                emit(f"⏳ {what} - still working ({int(spent) // 60}m{int(spent) % 60:02d}s). "
                     f"Large repositories can take a while on this disk.")
        out.seek(0)
        text = out.read().decode("utf-8", "replace").strip()
    return proc.returncode, text


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
        self._add_worktree()
        self.base_commit = _git(self.path, "rev-parse", "HEAD").stdout.strip()
        self.emit(f"▸ {t.label}: reading {t.branch} at {self.base_commit[:8]} "
                  f"in an isolated worktree.")
        return self

    def _add_worktree(self):
        """`git worktree add`, patiently, and once more if the first try overran.

        Nothing here needs the network or a person, so git is told never to ask
        (a hidden credential or LFS prompt is the classic silent hang on Windows),
        to accept long paths, and to skip LFS downloads: the review reads text
        documents, and a pointer file stands in for a binary it could not read anyway.
        """
        t = self.target
        env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never",
               "GIT_LFS_SKIP_SMUDGE": "1"}
        args = ("-c", "core.longpaths=true", "-c", "core.fsmonitor=false",
                "worktree", "add", "--detach", str(self.path), self.ref)
        last = ""
        for attempt in (1, 2):
            with O._lock_for(f"{self.clone}#docs-worktree"):
                _git(self.clone, "worktree", "prune", check=False, timeout=60)
                self.emit(f"▸ {t.label}: preparing an isolated copy of {t.branch}"
                          + ("" if attempt == 1 else " (second try)") + ".")
                rc, text = _git_patient(self.clone, self.emit, f"{t.label}: checking out",
                                        *args, env_extra=env)
                if rc == 0:
                    return
                last = (f"it ran past {CHECKOUT_TIMEOUT // 60} minutes" if rc is None
                        else text[:800])
                O._remove_worktree(self.clone, self.path, self.root)
                if self.path.exists():
                    shutil.rmtree(self.path, ignore_errors=True)
            if attempt == 1:
                self.emit(f"⚠ {t.label}: the checkout did not finish ({last}). Clearing it and trying again.")
        raise RuntimeError(f"git worktree add failed: {last}")

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

_SCALE_PROMPT = (
    "PRISM could not find your Scale line. Classify what this document asks to build, "
    "changing nothing else about your review, as exactly this one line and nothing else:\n"
    "**Scale:** <normal | large> — <one sentence saying why>\n"
    "Large means very large or expensive to build, roll back or review safely as "
    "specified: it cuts across many modules, migrates data or schema, changes "
    "infrastructure or a pipeline, or moves a security boundary. Document length alone "
    "does not decide it.")


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


INCREMENTAL_MODE = (
    "INCREMENTAL REVIEW - a VERIFICATION pass, not a fresh review. This document was already "
    "reviewed and the earlier comments are listed below. Your job, in this order: (1) decide "
    "for EVERY earlier comment whether it is now resolved, by checking the document (and the "
    "code or requirement it cites) as they are today; (2) read the changes the author made "
    "and report only NEW problems those changes introduce. Do NOT re-review the parts that "
    "did not change, do not re-read the whole requirement set, and do not re-report a "
    "problem that is resolved. Your conclusions on security, stability and scalability "
    "cover the changed parts only; say 'unchanged since the last review' for the rest.")


NO_CODEBASE = (
    "NO CODEBASE IS AVAILABLE for this review: the document was given as a file and no "
    "repository was selected. Judge the document against itself"
    " {req}. Do not look for, assume or invent a repository, code paths or existing "
    "behaviour. Under Coverage add exactly this bullet: "
    "`- **Codebase:** No codebase was available for this review.`")
NO_CODEBASE_LINE = "- **Codebase:** No codebase was available for this review."


def build_prompt(target, doc_rel, doc_path, req_folder, req_files, req_truncated,
                 other_docs, worktree, other_worktrees, custom, previous, first_id,
                 persona=DEFAULT_PERSONA, req_note="", uploaded=None):
    """The agent's brief for one document. Pure text, so it can be tested.

    `req_folder` None means no requirements were provided: the review is of the
    document against the codebase alone. `uploaded` (a short description such as
    "a Word file", or None) marks a document given as a file rather than a path on
    the branch; `worktree` None means there is no codebase at all.
    """
    info = PERSONAS[persona]
    incremental = bool(previous)
    lines = [
        f"PERSONA: {info['label']}.",
        info["lens"],
    ]
    if incremental:
        lines.append(INCREMENTAL_MODE)
    else:
        if worktree is None:
            lines.append(NO_CODEBASE.format(
                req="and against the requirements provided" if req_folder
                else "(no requirements were provided either)"))
            if req_folder:
                lines.append(REQUIREMENTS_GIVEN.split(" Then check")[0])
        else:
            lines.append(REQUIREMENTS_GIVEN if req_folder else REQUIREMENTS_ABSENT)
        lines.append(ALWAYS_TEXT)
    if worktree is None:
        lines += [f"Review the uploaded document `{doc_rel}` ({uploaded or 'a file'}).",
                  f"Read it from: {doc_path}"]
    elif uploaded:
        lines += [
            f"Review the uploaded document `{doc_rel}` ({uploaded}). It is not committed on "
            f"the branch; judge it against the {target.label} repository "
            f"'{target.repo_name}', branch '{target.branch}', exactly as you would a "
            f"document on that branch.",
            f"Read it from: {doc_path}",
            f"Read the code from this checkout of that branch: {worktree}",
        ]
    else:
        lines += [
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
        if incremental:
            lines.append("Open a requirement file only when an earlier comment or a change "
                         "cites it. Do not re-read the set.")
        else:
            lines.append("Requirement files:\n" + "\n".join(f"  - {p}" for p in shown)
                         if shown else "That folder holds no readable files.")
        if req_note:
            lines.append(req_note)
        if req_truncated and not incremental:
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
    ids = ", ".join(_id for _sev, _id, _line in open_list)
    delta_text = delta.strip() if delta and delta.strip() else \
        "(no change to the document itself since that review)"
    todo = (f"1. Report the status of EVERY earlier comment above ({ids}) under "
            f"'### Previous comments', one line each: Resolved, Partially resolved, Still "
            f"open or Withdrawn, with the evidence. A comment you do not mention is treated "
            f"as not assessed.\n" if open_list else
            "1. Nothing was left open, so there is nothing to verify.\n")
    return (
        f"PREVIOUS REVIEW - this is an INCREMENTAL review. PRISM reviewed this document "
        f"before (review v{last.get('v', '?')}, run {last.get('id', '?')}"
        + (f", at commit {last['commit'][:8]}" if last.get("commit") else "")
        + f"; verdict {last.get('verdict', 'unknown')}).\n"
        f"Comments that review left open (each is yours to verify):\n{listed}\n\n"
        f"What the author changed since (review sections ignored):\n<<<\n{delta_text}\n>>>\n\n"
        f"Your tasks:\n{todo}"
        f"2. Carry Partially resolved and Still open comments forward with the same id and "
        f"updated text.\n"
        f"3. Review ONLY the changed text above for new problems; give them new ids.\n"
        f"4. Do not review anything else.")


_STATUS_LINE_RE = re.compile(
    r"^\s*[-*]\s*\*\*(DR-\d+)\*\*\s*[·|:-]*\s*(.*)$", re.IGNORECASE | re.MULTILINE)
_STATUS_WORDS = (("partial", "partial"), ("still open", "open"), ("open", "open"),
                 ("resolved", "resolved"), ("withdrawn", "withdrawn"))


def previous_statuses(body):
    """{DR-n: resolved | partial | open | withdrawn} from a review's 'Previous comments'."""
    m = _PREVIOUS_HEAD_RE.search(body or "")
    if not m:
        return {}
    nxt = _NEXT_HEAD_RE.search(body, m.end())
    section = body[m.end(): nxt.start() if nxt else len(body)]
    out = {}
    for lm in _STATUS_LINE_RE.finditer(section):
        text = lm.group(2).lower()
        head = text.split("\u2014")[0].split(" - ")[0]
        for word, key in _STATUS_WORDS:
            if word in head:
                out[lm.group(1).upper()] = key
                break
    return out


def carry_unassessed(body, open_list):
    """(body, [ids]): earlier comments the reviewer never reported on stay OPEN.

    A comment that is silently dropped would look resolved to every later review, so
    PRISM puts each one back, verbatim, under its own heading. Nothing is called
    resolved unless the reviewer said so.
    """
    statuses = previous_statuses(body)
    seen = {fid for _s, fid, _l in findings(body)}
    missing = [(sev, fid, line) for sev, fid, line in open_list
               if fid.upper() not in statuses and fid.upper() not in seen]
    # Reported as still open or partly resolved but not carried forward under its
    # severity: it is still open, so it is put back - or a Major the reviewer only
    # mentioned in "Previous comments" would let the document be approved (FR-VER-4).
    kept = [(sev, fid, line) for sev, fid, line in open_list
            if statuses.get(fid.upper()) in ("open", "partial") and fid.upper() not in seen]
    blocks = []
    if kept:
        blocks.append("### Still open\n" + "\n".join(line for _s, _f, line in kept) + "\n")
    if missing:
        blocks.append("### Not assessed\n" + "\n".join(line for _s, _f, line in missing)
                      + "\n")
    if not blocks:
        return body, []
    block = "\n".join(blocks)
    cov = re.search(r"^#{2,4}\s*Coverage\s*$", body, re.IGNORECASE | re.MULTILINE)
    body = (body[:cov.start()] + block + "\n" + body[cov.start():]) if cov \
        else body.rstrip("\n") + "\n\n" + block
    return body, [fid for _s, fid, _l in missing]


def earlier_summary(statuses, unassessed=()):
    names = (("resolved", "resolved"), ("partial", "partially resolved"),
             ("open", "still open"), ("withdrawn", "withdrawn"))
    bits = [f"{sum(1 for v in statuses.values() if v == k)} {label}" for k, label in names
            if any(v == k for v in statuses.values())]
    if unassessed:
        bits.append(f"{len(unassessed)} not assessed")
    return ", ".join(bits)


# ---- running the agent -------------------------------------------------------

def _run_agent(prompt, project_dir, emit, model, control, meter):
    """One doc-reviewer session. Returns (rc, output, session_id)."""
    exe = O.require_engine()
    O.ensure_bundled_agents(project_dir, emit=emit)
    json_mode = O.engine_supports_json(exe)
    cmd = [exe, "run", "--agent", AGENT_DOC_REVIEWER, *O._dir_args(exe, project_dir), *O._approve_args(exe)]
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
    if body is not None and not res.scale and sid and not res.incomplete_reason:
        # Every review states the scale of what the document asks to build; a report
        # without the line is asked for it once (FR-SCALE-2). The review itself stays
        # exactly as delivered.
        emit("\n⚠ The report has no Scale line — asking the reviewer for it once.")
        if control is not None:
            control.check()
        _rc, raw, sid2 = O._continue_turn(_SCALE_PROMPT, project_dir, sid, emit, model,
                                          control, what="scale classification", meter=meter,
                                          agent=AGENT_DOC_REVIEWER)
        sid = sid2 or sid
        res.scale, res.scale_reason = O.parse_scale(raw)
    return res, body, sid, partial


# ---- the pipeline ------------------------------------------------------------

def _worst(keys):
    """The run's decision: Request changes when any document's is, else Approve."""
    keys = list(keys)
    if not keys:
        return ""
    return DECISION_APPROVE if all(k == DECISION_APPROVE for k in keys) else DECISION_CHANGES


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _write(path, text):
    # newline="" keeps line endings exactly as the text carries them.
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _readable(path):
    try:
        with open(path, "rb") as fh:
            fh.read(1)
        return True
    except OSError:
        return False


def _did_you_mean(wt, rel):
    """" Did you mean ...?" for a path that is not on the branch, or "". Catches the
    usual causes: a different case, a typo, a file in another folder."""
    try:
        listing = _git(wt.path, "ls-tree", "-r", "--name-only", "HEAD", check=False).stdout
        names = [n for n in listing.splitlines() if n.lower().endswith((".md", ".txt", ".rst"))]
    except Exception:  # noqa: BLE001
        return ""
    norm = rel.replace("\\", "/")
    near = [n for n in names if n.lower() == norm.lower()] \
        or difflib.get_close_matches(norm, names, n=3, cutoff=0.6)
    return f" Did you mean: {', '.join(near)}?" if near else ""


def check_spec(spec):
    """A human-readable problem with the job as specified, or None."""
    if not spec.targets:
        return "Add a document to review: a path on a branch, or an uploaded file."
    if not config.clean_username(getattr(spec, "owner", "")):
        return ("Enter the owner of this review request - the person responsible for the "
                "document and for answering the review.")
    if any(not t.has_repo for t in spec.targets) and len(spec.targets) > 1:
        return "A review without a repository cannot also review repositories."
    seen = set()
    for t in spec.targets:
        if not t.count:
            return f"{t.label}: add the document(s) to review."
        for u in t.uploads:
            bad = upload_type_problem(u.path)
            if bad:
                return bad
            if not Path(u.path).is_absolute() or not Path(u.path).is_file():
                return f"{u.path or u.name} is not a file."
            if u.dest and not t.has_repo:
                return (f"{u.name}: a destination path needs a repository and a branch to "
                        f"commit to.")
        if not t.has_repo:
            if t.files:
                return "A document path on a branch needs a repository."
            continue
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


# An uploaded Word, PDF or Excel file may be larger than a text document; its text is
# what is reviewed, and that is capped by extract.MAX_CHARS.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
COPY_TAG = ".prism-review"
_FORMAT_NAMES = {".docx": "a Word file", ".pdf": "a PDF", ".xlsx": "an Excel workbook"}


def workspace_dir(project_dir):
    """Where the reviewer runs: the working folder, or - for a review with no working
    folder at all (FR-DOC-IN-7) - a folder PRISM keeps for that, so the reviewer's
    conversation can still be continued after the job."""
    if project_dir and Path(project_dir).is_dir():
        return project_dir
    workspace = config.CONFIG_DIR / "docs-workspace"     # read now: tests move CONFIG_DIR
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


@dataclass
class _Doc:
    """One document in a run: a path on a branch, or an uploaded file."""

    target: DocTarget
    wt: object                # the target's _Worktree; None when there is no repository
    name: str                 # the path from the repository root, or the uploaded file's name
    path: Path                # what the reviewer reads
    old_text: str
    upload: Upload = None
    suffix: str = ""

    @property
    def key(self):
        return f"{self.target.label}: {self.name}"


def _copy_name(path, sidecar=False):
    """`spec.md` -> `spec.prism-review.md`; a PDF's sidecar is `spec.pdf.prism-review.md`.

    A file that is itself a copy is not tagged twice: dropping `spec.prism-review.md`
    again gives `spec.prism-review-2.md`, never `spec.prism-review.prism-review.md`.
    """
    p = Path(path)
    if sidecar:
        return p.name + COPY_TAG + ".md"
    stem = p.stem[:-len(COPY_TAG)] if p.stem.endswith(COPY_TAG) else p.stem
    return stem + COPY_TAG + p.suffix


def _unused(folder, name):
    candidate = Path(folder) / name
    if not candidate.exists():
        return candidate
    base, dot, ext = name.partition(COPY_TAG)
    for n in range(2, 1000):
        candidate = Path(folder) / f"{base}{COPY_TAG}-{n}{ext}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many review copies of {name} in {folder}.")


def _write_copy(upload_path, data, run_id, emit, sidecar=False):
    """Write a review copy next to the uploaded file; never over it, never over another
    copy. Falls back to ~/.prism/reviewed-documents/<run id>/ when that folder cannot be
    written. Returns the path written."""
    name = _copy_name(upload_path, sidecar=sidecar)
    for folder in (Path(upload_path).parent, config.CONFIG_DIR / "reviewed-documents" / run_id):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = _unused(folder, name)
            if isinstance(data, bytes):
                with open(target, "xb") as fh:
                    fh.write(data)
            else:
                with open(target, "x", encoding="utf-8", newline="") as fh:
                    fh.write(data)
            return target
        except OSError as e:
            emit(f"⚠ Could not write the review copy in {folder} ({e.strerror or e}); "
                 f"trying PRISM's own folder.")
    raise RuntimeError(f"Could not write a review copy of {Path(upload_path).name}.")


def section_plain_lines(run):
    """The review section as plain lines, for a format that cannot hold Markdown
    (a Word document or an Excel sheet): heading, decision, impact, comments, notice,
    then the review itself."""
    m = run.meta
    who = " · ".join(x for x in (
        m.get("persona_label") or "",
        f"owner {config.clean_username(m['owner'])}" if m.get("owner") else "",
        f"by {config.clean_username(m['reviewer'])}" if m.get("reviewer") else "") if x)
    title = (f"Review v{m.get('v', '?')}" + (f" · {who}" if who else "")
             + f" · run {m.get('id', '?')} · {m.get('date', '')}")
    lines = [f"## {title}", f"Decision: {m.get('verdict') or ''}"]
    if m.get("impact"):
        lines.append(f"Impact: {m['impact']}")
    if m.get("counts"):
        lines.append(f"Comments: {m['counts']}")
    lines += ["", SECTION_NOTICE.lstrip("> ").replace("**", ""), ""]
    for line in run.body.split("\n"):
        line = line.replace("**", "").replace("`", "")
        if line.startswith("> "):
            line = line[2:]
        lines.append(line.rstrip())
    return lines


def _with_no_codebase_line(body):
    """A review with no repository says so, whatever the reviewer wrote (FR-DOC-IN-7)."""
    if "no codebase was available" in body.lower():
        return body
    cov = re.search(r"^#{2,4}\s*Coverage\s*$", body, re.IGNORECASE | re.MULTILINE)
    if cov:
        return body[:cov.end()] + "\n" + NO_CODEBASE_LINE + body[cov.end():]
    return body.rstrip("\n") + "\n\n### Coverage\n" + NO_CODEBASE_LINE


def _load_upload(u, scratch, index):
    """(path for the reviewer, text) of an uploaded document, or raise X.ExtractError /
    UnicodeDecodeError / OSError for one that cannot be read."""
    suffix = Path(u.path).suffix.lower()
    if suffix in TEXT_TYPES:
        return Path(u.path), _read(u.path)
    text = X.extract_text(u.path)
    out = Path(scratch) / f"{index:02d}-{re.sub(r'[^A-Za-z0-9._-]+', '_', u.name)}.txt"
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return out, text


def _pipeline(project_dir, targets, requirements_path="", model=None,
              do_commit=True, do_push=True, dry_run=False, custom_instructions="",
              progress=None, emit=_emit_plain, control=None, ask=None, get_note=None,
              persona=DEFAULT_PERSONA, reviewer="", webhook_url=None, owner=""):
    """Review -> write -> commit -> push, for every document of every target.

    A document is a path on a branch (written into a temporary worktree, committed and
    pushed as before), or an uploaded file: reviewed where it is, its comments written
    into a copy next to it (a PDF gets a Markdown sidecar), and committed only to the
    destination path the person gave, with Commit on. A target with no repository is
    reviewed with no codebase.

    Then, when `webhook_url` is given and this is not a dry run, one alert to the
    document-review Google Chat webhook. The alert is a courtesy: whether it arrives
    never changes what the review did or returned, and a run that fails outright posts
    nothing (there is no outcome to report) - the same rule as for a pull request.
    """
    targets = tuple(targets)
    persona = normalise_persona(persona)
    reviewer = config.clean_username(reviewer)
    owner = config.clean_username(owner)
    spec = DocJobSpec(project_dir=project_dir, targets=targets, owner=owner)

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
    run_dir = workspace_dir(project_dir)

    def _what(t):
        docs = list(t.files) + [f"{u.name} (uploaded)" for u in t.uploads]
        where = f"{t.label} {t.branch}" if t.has_repo else t.label
        return f"{where}: {', '.join(docs)}"

    run_id = new_run_id()
    emit(f"▸ Documentation review  |  run {run_id}  |  {persona_label(persona)}  |  "
         f"owner {owner}  |  "
         + (f"reviewer {reviewer}  |  " if reviewer else "")
         + "  |  ".join(_what(t) for t in targets)
         + (f"  |  model {model}" if model else "  |  model (default)"))
    if dry_run:
        emit("▸ Dry run: the documents are reviewed, but nothing is written, "
             "committed, pushed or posted.")
    O.ensure_bundled_agents(run_dir, emit=emit)
    meter = O.TokenMeter()
    stack, trees = [], []
    by_target = {}                            # id(target) -> its worktree
    results, failed = [], []
    all_findings = []
    committed, pushed = [], []
    copies = []
    session_id = ""
    scratch_dir = tempfile.mkdtemp(prefix="prism-docs-")
    try:
        ck()
        pg(STAGE_DOC_REVIEW, "active")
        # Every worktree first: the requirements and the cross-repo reads must be
        # available to every document, and a bad branch should fail before any
        # model time is spent.
        # Pre-flight, part 1: every branch. All problems are gathered and reported
        # together, so one typo does not hide another behind a retry.
        problems = []
        for t in targets:
            if not t.has_repo:
                emit("▸ No repository: the uploaded document(s) are reviewed without a "
                     "codebase.")
                continue
            wt = _Worktree(t, run_id, emit)
            stack.append(wt)         # registered first so a half-made one is cleaned up
            try:
                trees.append(wt.__enter__())
                by_target[id(t)] = wt
            except O.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                problems.append(f"{t.label}: {e}")
        if problems:
            raise RuntimeError("Pre-flight check failed - nothing was reviewed.\n  - "
                               + "\n  - ".join(problems))
        # Requirements are optional. Given, the review is centred on whether the
        # decomposition satisfies all of them; absent, on the document against the codebase.
        req_folder, req_files, req_trunc, req_note = None, [], False, ""
        if (requirements_path or "").strip():
            roots = [wt.path for wt in trees] + ([Path(project_dir)] if project_dir else [])
            req_folder = find_requirements(requirements_path, roots)
            if req_folder is None:
                raise RuntimeError(
                    f"Requirement folder '{requirements_path}' was not found in the reviewed "
                    f"branch(es) or the project folder. Fix the path, or leave the "
                    f"requirements empty to review the documents against the codebase alone.")
            req_files, req_trunc = list_requirement_files(req_folder)
            req_scratch = Path(scratch_dir) / "requirements"
            req_scratch.mkdir()
            req_files, req_note, n_conv = prepare_requirements(
                req_files, req_folder, req_scratch, emit)
            emit(f"▸ Requirements: {req_folder} ({len(req_files)} file"
                 f"{'' if len(req_files) == 1 else 's'}"
                 + (f"; {n_conv} read from Word/PDF/Excel" if n_conv else "") + ")")
            if not req_files:
                emit("⚠ The requirement folder holds no readable files; the review will say so.")
        elif trees:
            emit("▸ No requirements given: reviewing the document against the codebase alone.")
        else:
            emit("▸ No requirements given: reviewing the document against itself.")

        # Pre-flight, part 2: every document, before any model time.
        docs = []
        problems = []
        index = 0
        for t in targets:
            wt = by_target.get(id(t))
            for rel in t.files:
                path = resolve_inside(wt.path, rel)
                if path is None or not path.is_file():
                    problems.append(
                        f"{t.label}: '{rel}' is not a file on branch "
                        f"'{t.branch}'. Paths are relative to the repository root."
                        + _did_you_mean(wt, rel))
                    continue
                if path.stat().st_size > MAX_DOC_BYTES:
                    problems.append(f"{t.label}: '{rel}' is larger than "
                                    f"{MAX_DOC_BYTES // (1024 * 1024)} MB.")
                    continue
                try:
                    docs.append(_Doc(t, wt, rel, path, _read(path)))
                except (UnicodeDecodeError, OSError):
                    problems.append(f"{t.label}: '{rel}' is not a readable UTF-8 "
                                    f"text document.")
            dests = set()
            for u in t.uploads:
                index += 1
                suffix = Path(u.path).suffix.lower()
                try:
                    size = Path(u.path).stat().st_size
                except OSError as e:
                    problems.append(f"{u.name}: cannot be read ({e.strerror or e}).")
                    continue
                limit = MAX_DOC_BYTES if suffix in TEXT_TYPES else MAX_UPLOAD_BYTES
                if size > limit:
                    problems.append(f"{u.name} is larger than {limit // (1024 * 1024)} MB.")
                    continue
                if u.dest:
                    dpath = resolve_inside(wt.path, u.dest)
                    if dpath is None:
                        problems.append(f"{u.name}: the destination '{u.dest}' must be a path "
                                        f"inside the repository.")
                        continue
                    if Path(u.dest).suffix.lower() != suffix:
                        problems.append(f"{u.name}: the destination '{u.dest}' must keep the "
                                        f"file's {suffix} extension.")
                        continue
                    clash = [p for p in ([dpath] + ([Path(str(dpath) + COPY_TAG + ".md")]
                                                    if suffix == ".pdf" else []))
                             if p.exists()]
                    if clash:
                        problems.append(
                            f"{u.name}: '{u.dest}' already exists on branch '{t.branch}'. "
                            f"Review it as a document on the branch instead, or choose "
                            f"another destination - PRISM does not replace a file on the "
                            f"branch with an uploaded one.")
                        continue
                    key = posix_rel(u.dest).lower()
                    if key in dests or any(posix_rel(f).lower() == key for f in t.files):
                        problems.append(f"{u.name}: the destination '{u.dest}' is used twice.")
                        continue
                    dests.add(key)
                try:
                    read_path, text = _load_upload(u, scratch_dir, index)
                except X.ExtractError as e:
                    failed.append(u.name)
                    emit(f"⚠ {u.name} could not be read ({e}). It is not reviewed.")
                    continue
                except (UnicodeDecodeError, OSError) as e:
                    failed.append(u.name)
                    why = "it is not readable UTF-8 text" if isinstance(e, UnicodeDecodeError) \
                        else (e.strerror or str(e))
                    emit(f"⚠ {u.name} could not be read ({why}). It is not reviewed.")
                    continue
                if not text.strip():
                    failed.append(u.name)
                    emit(f"⚠ {u.name} is empty. It is not reviewed.")
                    continue
                docs.append(_Doc(t, wt, u.name, read_path, text, upload=u, suffix=suffix))
        unreadable = [str(p) for p in req_files if not _readable(p)]
        if unreadable:
            problems.append(f"{len(unreadable)} requirement file(s) cannot be read, for "
                            f"example {unreadable[0]}.")
        if problems:
            raise RuntimeError("Pre-flight check failed - nothing was reviewed.\n  - "
                               + "\n  - ".join(problems))
        if not docs:
            raise RuntimeError("No document could be read, so nothing was reviewed: "
                               + ", ".join(failed) + ".")
        emit("✔ Pre-flight: " + "; ".join(
            (f"{t.label} {t.branch}" if t.has_repo else t.label)
            + f" ({sum(1 for d in docs if d.target is t)} document"
            f"{'' if sum(1 for d in docs if d.target is t) == 1 else 's'} ready)"
            for t in targets)
            + (f"; {len(req_files)} requirement file(s) readable" if req_folder else "") + ".")
        all_docs = [d.key for d in docs]
        if custom_instructions.strip():
            emit(f"▸ Custom instructions given to the reviewer: "
                 f"{custom_instructions.strip()[:300]}")

        for d in docs:
            ck()
            t, wt, rel, old_text = d.target, d.wt, d.name, d.old_text
            others = [(o.target.label, str(o.path)) for o in trees if o is not wt]
            # An uploaded Word, PDF or Excel file holds no PRISM section markers, so only
            # a document on the branch or an uploaded text file can be incremental.
            _b, runs, _a = split_document(old_text) if d.suffix in ("",) + TEXT_TYPES \
                else ("", [], "")
            previous, open_list = "", []
            prior = latest_run(runs, persona)
            if prior is not None:
                last = prior.meta
                delta = ""
                if last.get("commit") and wt is not None and d.upload is None:
                    shown = _git(wt.path, "show", f"{last['commit']}:{posix_rel(rel)}", check=False)
                    if shown.returncode == 0:
                        delta = doc_delta(shown.stdout, old_text)
                open_list = open_findings(runs, persona)
                previous = build_previous_block(runs, delta, open_list, persona)
                emit(f"\n▸ {rel}: incremental {persona_label(persona)} review - "
                     f"v{last.get('v', '?')} left {len(open_list)} comment(s) open.")
            else:
                emit(f"\n▸ {rel}: first {persona_label(persona)} review of this document"
                     + (" (uploaded)." if d.upload else "."))
            uploaded = None
            if d.upload is not None:
                uploaded = _FORMAT_NAMES.get(d.suffix, "a text file")
                if d.suffix in _FORMAT_NAMES:
                    uploaded += f"; you are given its text, extracted by PRISM"
            prompt = build_prompt(
                t, rel, d.path, req_folder, req_files, req_trunc,
                [x for x in all_docs if x != d.key], wt.path if wt else None, others,
                custom_instructions, previous, next_finding_number(runs), persona=persona,
                req_note=req_note, uploaded=uploaded)
            emit(f"═══ {d.key.replace(': ', ' · ', 1)} ═══")
            res, body, sid, partial = review_document(
                prompt, run_dir, emit, model, control, meter, ask=ask, get_note=get_note)
            if res.incomplete_reason or body is None:
                # One automatic second attempt in a fresh session: a reviewer that gave up
                # (or lost its way) once usually does not do so twice, and a person should
                # not have to retry a whole job for one document.
                why = res.incomplete_reason or "no review report was delivered"
                emit(f"⚠ {rel}: first attempt gave no review ({why}). Trying once more.")
                ck()
                retry_prompt = (prompt + "\n\nPRISM has verified that every path above exists "
                                "and is readable; read them with your tools. A previous "
                                "attempt at this document did not deliver a review. Read the "
                                "document and the requirements, then report in the required "
                                "format.")
                res, body, sid, partial = review_document(
                    retry_prompt, run_dir, emit, model, control, meter, ask=ask,
                    get_note=get_note)
            session_id = sid or session_id
            if sid:
                emit(sid, "session")
            if res.caveat:
                emit(f"⚠ {rel}: the report contains \"{res.caveat[:140]}\" - the review was "
                     f"delivered and is kept; check its Coverage section for what was not read.")
            if res.incomplete_reason:
                # Same rule as a pull request: a reviewer that did not read the document
                # has no verdict. Nothing is written or committed for it.
                failed.append(rel)
                emit(f"⛔ The reviewer did not examine {rel} — {res.incomplete_reason}. "
                     f"Nothing is written for it. Both attempts failed; check the "
                     f"requirements folder and the document, then retry the job.")
                continue
            if body is None:
                failed.append(rel)
                emit(f"⚠ The reviewer did not deliver a review for {rel}; nothing is written "
                     f"for it. Retry the job to review it again.")
                continue
            if wt is None:
                body = _with_no_codebase_line(body)
            skipped = missing_always_checks(body)
            if skipped:
                emit(f"⚠ The reviewer did not state a conclusion on "
                     f"{', '.join(ALWAYS_NAMES[w] for w in skipped)}"
                     f" - it is meant to for every review. Check that area yourself, or run "
                     f"the review again.")
            earlier = ""
            if previous:
                # An incremental review is a verification pass: every comment that was
                # open must come back with a status, or stay open.
                body, unassessed = carry_unassessed(body, open_list)
                statuses = previous_statuses(body)
                earlier = earlier_summary(statuses, unassessed)
                if unassessed:
                    emit(f"⚠ The reviewer did not report on {', '.join(unassessed)}; "
                         f"{'it stays' if len(unassessed) == 1 else 'they stay'} open "
                         f"under 'Not assessed'.")
                if earlier:
                    emit(f"▸ {rel}: earlier comments - {earlier}.")
            counts = severity_counts(body)
            # Scale first: it can raise the impact and it is part of the decision.
            note = O.resolve_scale(res)
            if note:
                emit(note)
            for line in O.enforce_scale(res, kind="docs"):
                emit(line)
            large = res.scale == O.SCALE_LARGE
            key = decide(counts, partial=partial, large=large)
            said = O.normalise_verdict(res.verdict_raw) if res.verdict_raw else ""
            said_ok = said in ("approve", "approve-with-comments")
            said_no = said in ("request-changes", "block")
            if (said_ok and key != DECISION_APPROVE) or (said_no and key == DECISION_APPROVE):
                # The reviewer's own wording never decides (FR-VER-4); say so without
                # repeating a label the document decision no longer has.
                emit(f"⚠ The reviewer's verdict line does not decide a document review. "
                     f"PRISM's decision, from the open comments ({counts_text(counts)})"
                     + (", the partial review" if partial else "")
                     + (" and the large scale" if large else "")
                     + f", is {decision_words(key)}.")
            if large:
                plain = not (counts.get("Blocker") or counts.get("Major"))
                body += ("\n\n> 🔴 **Request changes — the change is large or expensive** to "
                         "build, roll back or review safely as specified, so work may not "
                         "start from this document as it stands"
                         + (", even though no Blocker or Major comment is open" if plain else "")
                         + "." + (f" Scale: {res.scale_reason}" if res.scale_reason else ""))
            if partial:
                body += ("\n\n> ⚠️ **Partial review:** the reviewer ran out of time, so parts "
                         "of the document were not examined (see Coverage). A partial review "
                         "is never an approval: the decision is Request changes.")
            version = (max(_version_of(r) for r in runs) + 1) if runs else 1
            meta = {"id": run_id, "v": version, "date": time.strftime("%Y-%m-%d %H:%M"),
                    "persona": persona, "persona_label": persona_label(persona),
                    "commit": wt.base_commit if (wt is not None and d.upload is None) else "",
                    "verdict": verdict_label(key) + (" (partial review)" if partial else ""),
                    "decision": key, "scale": res.scale,
                    "impact": f"{res.impact_score}/10" if res.impact_score else "",
                    "counts": counts_text(counts)}
            if d.upload is not None:
                meta["source"] = "upload"
            if earlier:
                meta["earlier"] = earlier
            if owner:
                meta["owner"] = owner
            if reviewer:
                meta["reviewer"] = reviewer
            run = Run(meta, body)
            text_doc = d.upload is None or d.suffix in TEXT_TYPES
            new_text = with_review(old_text, run) if text_doc else ""
            if text_doc and not outside_unchanged(old_text, new_text):
                raise RuntimeError(f"Refusing to write {rel}: the review would have changed "
                                   f"text outside its own section.")
            where = posix_rel(d.upload.dest) if (d.upload is not None and d.upload.dest) else (
                d.upload.name if d.upload is not None else posix_rel(rel))
            result = {"role": t.label, "file": rel, "verdict_key": key,
                      "impact": res.impact_score, "impact_reason": res.impact_reason,
                      "counts": counts, "version": version, "scale": res.scale,
                      "scale_reason": res.scale_reason, "uploaded": d.upload is not None,
                      "path": where, "repo": t.repo_name if t.has_repo else "",
                      "branch": t.branch if t.has_repo else "",
                      "folder": (Path(posix_rel(where)).parent.name if "/" in posix_rel(where)
                                 else (Path(d.upload.path).parent.name
                                       if d.upload is not None and not d.upload.dest
                                       else "")),
                      "copy": "", "dest": d.upload.dest if d.upload is not None else ""}
            results.append(result)
            # The comments as data: the same ones the counts above were taken from, each
            # with where it is (repository, branch, document, heading) - see findings.py.
            all_findings.extend(findings_mod.parse_doc_findings(
                _without_previous_block(body), path=where,
                repo=t.repo_name if t.has_repo else "", branch=t.branch if t.has_repo else "",
                commit=(wt.base_commit or "") if wt is not None else "",
                role=t.role, persona=persona, scale=res.scale or None))
            emit(f"▸ {rel}: {verdict_label(key)}  ·  {counts_text(counts)}  ·  review v{version}")
            if dry_run:
                emit(f"▸ Dry run - would add review v{version} to "
                     + (f"a copy of {rel}." if d.upload is not None else f"{rel}."))
                continue
            pg(STAGE_DOC_WRITE, "active")
            if d.upload is None:
                _write(d.path, new_text)
                wt.changed.append(rel)
                emit(f"▸ Wrote PRISM review comments (v{version}) to {rel}.")
                continue
            # An uploaded file: the comments go into a copy (or, for a PDF, a sidecar);
            # the file the person gave is never modified.
            u = d.upload
            sidecar = ""
            if d.suffix in TEXT_TYPES:
                data = new_text
            elif d.suffix == ".docx":
                data = officewrite.append_docx_section(
                    Path(u.path).read_bytes(), officewrite.SECTION_TITLE, section_plain_lines(run))
            elif d.suffix == ".xlsx":
                data = officewrite.append_xlsx_sheet(
                    Path(u.path).read_bytes(), "PRISM review",
                    [[line] for line in [officewrite.SECTION_TITLE] + section_plain_lines(run)])
            else:                       # .pdf: a Markdown sidecar; the PDF is not touched
                sidecar = with_review(
                    f"# PRISM review of {u.name}\n\nThe review of the PDF `{u.name}`. The PDF "
                    f"itself is not changed.\n", run)
                data = None
            if data is not None:
                copy = _write_copy(u.path, data, run_id, emit)
            else:
                copy = _write_copy(u.path, sidecar, run_id, emit, sidecar=True)
            copies.append(str(copy))
            result["copy"] = str(copy)
            emit(f"▸ Wrote PRISM review comments (v{version}) for {u.name} to {copy}"
                 + (" - the PDF itself is unchanged." if data is None else "."))
            if u.dest and do_commit:
                dpath = resolve_inside(wt.path, u.dest)
                dpath.parent.mkdir(parents=True, exist_ok=True)
                if data is None:
                    shutil.copyfile(u.path, dpath)
                    _write(Path(str(dpath) + COPY_TAG + ".md"), sidecar)
                    wt.changed += [u.dest, u.dest + COPY_TAG + ".md"]
                elif isinstance(data, bytes):
                    dpath.write_bytes(data)
                    wt.changed.append(u.dest)
                else:
                    _write(dpath, data)
                    wt.changed.append(u.dest)
                emit(f"▸ {u.name} will be committed to '{u.dest}' on {t.branch}.")
            elif u.dest:
                emit(f"▸ {u.name} is not committed: Commit is off.")
            elif t.has_repo and do_commit:
                emit(f"▸ {u.name} is not committed: no destination path was given, so it "
                     f"is kept as the local copy only.")

        if not results:
            raise RuntimeError("No document could be reviewed. Nothing was changed.")
        pg(STAGE_DOC_REVIEW, "done")
        pg(STAGE_DOC_WRITE, "skipped" if dry_run else "done")
        if failed:
            emit(f"⚠ {len(failed)} document(s) were not reviewed: {', '.join(failed)}.")

        # ---- commit ----
        repo_changes = any(wt.changed for wt in trees)
        if not do_commit:
            pg(STAGE_DOC_COMMIT, "skipped")
            pg(STAGE_DOC_PUSH, "skipped")
            if not dry_run and any(not r["uploaded"] for r in results):
                emit("\n⚠ Commit is turned off: the review comments were written to "
                     "temporary worktrees that are discarded, so nothing is kept on the "
                     "branch. Turn on Commit to keep a review."
                     + (" The copies of uploaded documents are kept." if copies else ""))
        else:
            pg(STAGE_DOC_COMMIT, "active")
            emit("\n═══ Commit ═══")
            if not repo_changes:
                emit("▸ Nothing to commit: no document on a branch was written.")
            for wt in trees:
                ck()
                if not wt.changed:
                    continue
                mine = [r for r in results if r["role"] == wt.target.label]
                total = {s: sum(r["counts"][s] for r in mine) for s in SEVERITIES}
                who = persona_label(persona)
                brief = (f"{who} review comments v{mine[0]['version']} on {wt.changed[0]}"
                         if len(mine) == 1
                         else f"{who} review comments on {len(mine)} documents")
                msg = commit_message(run_id, f"{brief} ({counts_text(total)})")
                _git(wt.path, "add", "--", *[posix_rel(c) for c in wt.changed])
                _git(wt.path, "commit", "-m", msg)
                wt.commit = _git(wt.path, "rev-parse", "HEAD").stdout.strip()
                committed.append(wt)
                emit(f"✔ {wt.target.label}: committed {wt.commit[:8]} - {msg}")
            pg(STAGE_DOC_COMMIT, "done")

            # ---- push ----
            if not do_push:
                pg(STAGE_DOC_PUSH, "skipped")
                if committed:
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
        shutil.rmtree(scratch_dir, ignore_errors=True)
        for wt in reversed(stack):
            try:
                wt.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass

    worst = _worst([r["verdict_key"] for r in results])
    if failed and worst == DECISION_APPROVE:
        # A run in which a document was not reviewed is not an approval of the run:
        # work may not start from a document nobody read (FR-X-4).
        emit(f"⚠ {len(failed)} document(s) could not be reviewed, so the run's decision is "
             f"Request changes even though every reviewed document is Approve.")
        worst = DECISION_CHANGES
    scores = [int(r["impact"]) for r in results if str(r["impact"]).isdigit()]
    blockers = sum(r["counts"]["Blocker"] for r in results)
    comments = sum(sum(r["counts"].values()) for r in results)
    impact = max(scores) if scores else None
    reason = next((r["impact_reason"] for r in results
                   if impact is not None and str(r["impact"]) == str(impact)), "")
    scale = O.SCALE_LARGE if any(r["scale"] == O.SCALE_LARGE for r in results) else (
        O.SCALE_NORMAL if results else "")
    emit(f"\n◆ Verdict: {verdict_label(worst) if worst else 'unknown'}   "
         f"Impact: {impact if impact is not None else '?'}/10 {reason}".rstrip())
    emit(f"▸ {len(results)} document(s) reviewed, {comments} comment(s), {blockers} blocker(s)"
         f" - run {run_id}." + (f" Find it later with: git log --grep 'PRISM-{run_id}'"
                               if committed else ""))
    all_pushed = bool(committed) and len(pushed) == len(committed)
    if webhook_url and not dry_run:
        notifier.post_doc_summary(
            webhook_url, emit=emit,
            **_notice(persona, reviewer, owner, run_id, targets, results, worst, impact,
                      reason, len(failed), do_commit, bool(committed), all_pushed, copies))
    elif webhook_url:
        emit("▸ Dry run: no chat card is posted.")
    return {"run_id": run_id, "documents": len(results), "not_reviewed": len(failed),
            "comments": comments, "blockers": blockers, "committed": bool(committed),
            "pushed": all_pushed, "decision": worst, "scale": scale, "owner": owner,
            "copies": len(copies),
            "stopped": None, "review": None, "tokens": meter.total(),
            "session_id": session_id,
            # Data for whoever reads the run, not text for the transcript (see
            # orchestrator.INTERNAL_SUMMARY_KEYS).
            "findings": all_findings, "results": results}


def _notice(persona, reviewer, owner, run_id, targets, results, worst, impact, impact_reason,
            not_reviewed, do_commit, committed, pushed, copies):
    """What the document-review alert says, as plain data for notifier.build_doc_card."""
    if pushed:
        outcome = "committed and pushed"
    elif committed:
        outcome = "committed, not pushed"
    elif copies:
        outcome = "kept locally"
    elif not do_commit:
        outcome = "not committed"
    else:
        outcome = "not pushed"
    total = {sev: sum(r["counts"][sev] for r in results) for sev in SEVERITIES}
    blocks = []
    for t in targets:
        docs = [{"file": Path(posix_rel(r["path"])).name or r["file"],
                 "folder": r.get("folder") or "",
                 "verdict_key": r["verdict_key"]}
                for r in results if r["role"] == t.label]
        if docs:
            blocks.append({"label": t.label, "role": t.role,
                           "repo": t.repo_name if t.has_repo else "",
                           "branch": t.branch if t.has_repo else "",
                           "documents": docs})
    return {"persona": persona_label(persona), "owner": owner, "reviewer": reviewer,
            "decision": worst, "impact": impact, "impact_reason": impact_reason,
            "counts": total, "targets": blocks, "run_id": run_id, "outcome": outcome,
            "not_reviewed": not_reviewed}


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
    project_dir = workspace_dir(project_dir)
    O.ensure_bundled_agents(project_dir, emit=emit)
    _rc, text, sid = O._continue_turn(prompt, project_dir, session_id, emit, model, control,
                                      what="follow-up", agent=AGENT_DOC_REVIEWER)
    return text, (sid or session_id)
