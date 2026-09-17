"""Backend orchestration for the PR Review desktop tool.

Stdlib only (subprocess / json / re) so the desktop app stays lightweight.
Runs `opencode` (pr-reviewer agent) behind the scenes, then automates:
  1. Review PR  -> parse verdict + impact score from agent output
  2. Update PR description (asks the agent, with direct AWS-CLI fallback)
  3. Fast-forward merge if verdict allows; otherwise sync destination
     into source and then merge.

All long-running work streams line-events via an optional `emit` callback
so the UI can show live progress without freezing.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

REGION_DEFAULT = "us-east-1"

# CodeCommit rejects descriptions longer than this.
PR_DESCRIPTION_MAX = 10240

# Self-contained bundle: this tool ships its own copy of the pr-reviewer
# agent so it has zero dependency on any other skill/agent checkout.
# It is installed into <project>/.opencode/agents/ before each run, so the
# tool works on any machine where `opencode` + `aws` are on PATH.
#
# When frozen by PyInstaller the sources live inside a temporary extraction
# directory exposed as sys._MEIPASS, so the bundled data file has to be looked
# up there rather than next to __file__.
FROZEN = getattr(sys, "frozen", False)
TOOL_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)).resolve()
BUNDLED_AGENT = TOOL_DIR / "agents" / "pr-reviewer.md"
AGENT_NAME = "pr-reviewer"

# Pipeline stage ids (also consumed by the UI progress bar — keep stable).
STAGE_REVIEW = "review"
STAGE_DESCRIBE = "describe"
STAGE_MERGE_CHECK = "merge_check"
STAGE_SYNC = "sync"
STAGE_MERGE = "merge"

VERDICT_RE = re.compile(r"\*\*Verdict:\*\*\s*(.+)", re.IGNORECASE)
IMPACT_RE = re.compile(r"\*\*Impact score:\*\*\s*(\d+)\s*/\s*10\s*[-–—:]?\s*(.*)", re.IGNORECASE)
FINDING_RE = re.compile(r"^\s*-\s*\*\*\[(Critical|High|Medium|Low|Nit)\]", re.IGNORECASE | re.MULTILINE)
# The engine renders its own chrome with ANSI colour codes; strip them before
# pattern-matching the report so styled output can't hide the verdict line.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

DESC_BLOCK_RE = re.compile(r"<!-- pr-reviewer:start -->.*?<!-- pr-reviewer:end -->", re.DOTALL)

MERGEABLE_VERDICTS = ("approve", "approve with comments")


def _emit_plain(text, tag=None):
    """Default emit target. The tag is what the UI colours by; on stdout it
    carries no meaning, so it is accepted and ignored."""
    print(text)


# ---------------------------------------------------------------- locking --
# Jobs run concurrently, and several of them commonly share one project folder
# or one local clone. These locks serialise the few operations that mutate
# shared state on disk.
#
# INVARIANT: no code path in this module may hold two of these locks at once.
# It holds structurally today — the fallback directory lock is taken only in
# the agent-turn helpers (pipeline steps 1-2) and the clone lock only inside
# sync_destination_into_source (step 4) — and full_pipeline is straight-line,
# so one is always released before the other is requested. That makes
# lock-order inversion impossible. No lock is ever held across a human wait.
_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path):
    """Process-wide mutex keyed on a resolved absolute path.

    One table for both the clone lock and the project-directory lock on
    purpose: resolve_repo() legitimately returns local_clone == project_dir
    for the common "the project folder *is* the repo" layout, and two separate
    tables would hand out two different locks for the same real directory.
    """
    try:
        key = str(Path(path).expanduser().resolve())
    except Exception:  # noqa: BLE001
        key = str(path)
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.Lock())


_JSON_SUPPORT = None
_JSON_SUPPORT_GUARD = threading.Lock()


def engine_supports_json(exe):
    """Does this engine build understand `run --format json`?

    Probed once and cached. An engine too old to know the flag fails the run
    outright with no usable output, which is worse than the session race the
    flag exists to fix — so when it is absent we simply never ask for it.
    """
    global _JSON_SUPPORT
    with _JSON_SUPPORT_GUARD:
        if _JSON_SUPPORT is None:
            try:
                proc = subprocess.run([exe, "run", "--help"], capture_output=True,
                                      text=True, timeout=20)
                _JSON_SUPPORT = "--format" in ((proc.stdout or "") + (proc.stderr or ""))
            except Exception:  # noqa: BLE001
                _JSON_SUPPORT = False
        return _JSON_SUPPORT


class Cancelled(RuntimeError):
    """Raised inside the pipeline when the user hits Stop."""


class RunControl:
    """Cancellation token shared between the UI thread and the worker.

    The UI calls cancel() from Tk; the worker checks at stage boundaries. A
    flag alone is not enough — an agent run can sit for minutes without
    emitting anything — so the live child process is tracked and killed, which
    unblocks the reader immediately.
    """

    def __init__(self):
        self._event = threading.Event()
        self._proc = None
        self._lock = threading.Lock()

    def cancel(self):
        self._event.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass

    def cancelled(self):
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise Cancelled("Run stopped.")

    def attach(self, proc):
        with self._lock:
            self._proc = proc
        # Cancel may have landed between spawning and attaching; don't strand it.
        if self._event.is_set():
            self.cancel()

    def detach(self):
        with self._lock:
            self._proc = None


# A trailing question means the agent stopped and is waiting on the user
# (missing PR id, ambiguous branch, description-update approval, …).
_ASKING_RE = re.compile(
    r"(please provide|could you|can you|which of|should i|do you want|would you like|"
    r"let me know|need (?:the|more|these)|waiting for|confirm whether|shall i)",
    re.IGNORECASE)


def extract_question(text):
    """Return the agent's trailing question, or "" when it isn't asking.

    Deliberately conservative. A finished report is not a question even though
    findings routinely end in one ("should this be renamed?"), so any output
    carrying a verdict is excluded outright, and only the closing block (plus
    the couple before it, for context like a bullet list of what's missing) is
    considered.
    """
    body = strip_ansi(text or "").strip()
    if not body or VERDICT_RE.search(body):
        return ""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    if not blocks:
        return ""
    tail = blocks[-3:]
    first_hit = None
    for i, block in enumerate(tail):
        if block.endswith("?") or _ASKING_RE.search(block):
            first_hit = i if first_hit is None else first_hit
    if first_hit is None:
        return ""
    return "\n\n".join(tail[first_hit:]).strip()[:1200]


@dataclass
class ReviewResult:
    verdict_raw: str = ""
    verdict_key: str = ""      # approve | approve-with-comments | request-changes | block | unknown
    impact_score: str = ""
    impact_reason: str = ""
    summary: str = ""
    findings: list = None
    raw_output: str = ""

    def merge_allowed(self) -> bool:
        v = self.verdict_key
        return v in ("approve", "approve-with-comments")


def normalise_verdict(raw: str) -> str:
    t = raw.lower()
    if "approve with comments" in t or "⚠️" in raw:
        return "approve-with-comments"
    if "request change" in t or "🔴" in raw:
        return "request-changes"
    if "block" in t or "⛔" in raw:
        return "block"
    if "approve" in t or "✅" in raw:
        return "approve"
    return "unknown"


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def parse_review_output(text: str) -> ReviewResult:
    """Parse the pr-reviewer agent's chat report (Step 4 shape)."""
    res = ReviewResult(raw_output=text, findings=[])
    text = strip_ansi(text)
    m = VERDICT_RE.search(text or "")
    if m:
        res.verdict_raw = m.group(1).strip()
        res.verdict_key = normalise_verdict(res.verdict_raw)
    m2 = IMPACT_RE.search(text or "")
    if m2:
        res.impact_score = m2.group(1).strip()
        res.impact_reason = m2.group(2).strip()
    # findings: keep the one-line bullets
    for line in (text or "").splitlines():
        if FINDING_RE.match(line):
            res.findings.append(line.strip()[:300])
    # summary block
    ms = re.search(r"###\s*Summary\s*\n(.+)", text or "", re.IGNORECASE | re.DOTALL)
    if ms:
        res.summary = ms.group(1).strip()[:2000]
    return res


def ensure_bundled_agent(project_dir: str, emit=_emit_plain) -> str:
    """Install the bundled pr-reviewer agent into the target project.

    opencode resolves `--agent pr-reviewer` from the run directory's
    `.opencode/agents/` folder, so without this the tool would depend on
    whatever agent checkout happens to exist on the machine. Copying our
    bundled file makes the tool self-contained: any system with `opencode`
    installed can run it. Returns the agent name.
    """
    if not BUNDLED_AGENT.is_file():
        raise RuntimeError(f"Bundled agent missing: {BUNDLED_AGENT}")
    dest_dir = Path(project_dir).expanduser().resolve() / ".opencode" / "agents"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "pr-reviewer.md"
    src_text = BUNDLED_AGENT.read_text()
    if not dest.is_file() or dest.read_text() != src_text:
        dest.write_text(src_text)
        emit("▸ Reviewer agent installed for this run.")
    return AGENT_NAME


ENGINE_BIN = "opencode"
# The engine's own installer drops the binary here, which is not on PATH for
# GUI/desktop launches (they don't source the shell profile). Fall back to it
# so PRISM works whether it was started from a terminal or a launcher.
ENGINE_FALLBACKS = (Path.home() / ".opencode" / "bin" / ENGINE_BIN,
                    Path.home() / ".local" / "bin" / ENGINE_BIN)


def engine_path():
    """Absolute path to the review engine binary, or None when unavailable."""
    found = shutil.which(ENGINE_BIN)
    if found:
        return found
    for cand in ENGINE_FALLBACKS:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def require_engine():
    exe = engine_path()
    if exe is None:
        raise RuntimeError(
            "Review engine is not installed or not on PATH — see README (Prerequisites).")
    return exe


def list_available_models(timeout=60):
    """Return `provider/model` ids from `opencode models` (empty list on failure)."""
    exe = engine_path()
    if exe is None:
        return []
    try:
        proc = subprocess.run([exe, "models"], capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode != 0:
            return []
        models = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if line and "/" in line and not line.startswith(("#", "-", " ")):
                models.append(line.split()[0])
        return sorted(set(models))
    except Exception:  # noqa: BLE001
        return []


def detect_local_repos(project_dir):
    """Return [(display_name, path)] for git clones found under project_dir.

    Includes direct subdirectories containing a `.git` entry, plus
    project_dir itself when it is a repo. Sorted by name. Project-agnostic:
    no repo names are assumed. Never raises — returns [] when unscannable.
    """
    try:
        base = Path(project_dir).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return []
    if not base.is_dir():
        return []
    found = {}
    try:
        if (base / ".git").exists():
            found[base.name] = str(base)
        children = sorted(base.iterdir())
    except Exception:  # noqa: BLE001
        children = []
    for child in children:
        try:
            if child.is_dir() and not child.is_symlink() and (child / ".git").exists():
                found.setdefault(child.name, str(child))
        except Exception:  # noqa: BLE001
            continue
    return sorted(found.items(), key=lambda kv: kv[0].lower())


def resolve_repo(repo_name, project_dir, local_repo=None):
    """Resolve (repo_name, local_clone_path) for a project-agnostic run.

    - repo_name: CodeCommit repository name (required, non-empty).
    - local_repo: explicit local clone path, or None to auto-detect:
      prefer <project>/<repo_name>/, else the single git repo found under
      the project, else the project dir itself (the reviewer then falls
      back to the CodeCommit API when no usable clone exists).
    """
    repo_name = (repo_name or "").strip()
    if not repo_name:
        raise ValueError("CodeCommit repository name is required.")
    base = Path(project_dir).expanduser().resolve()
    if local_repo:
        p = Path(local_repo).expanduser().resolve()
        if not p.is_dir():
            raise RuntimeError(f"Local clone path not found: {p}")
        return repo_name, str(p)
    candidate = base / repo_name
    if candidate.is_dir():
        return repo_name, str(candidate)
    detected = detect_local_repos(str(base))
    if len(detected) == 1:
        return repo_name, detected[0][1]
    return repo_name, str(base)


def _display_cmd(cmd):
    """UI-safe rendering of a subprocess command: never leaks the engine
    binary name, and truncates very long arguments (e.g. agent prompts)."""
    parts = []
    for c in cmd:
        c = str(c)
        if c == ENGINE_BIN or Path(c).name == ENGINE_BIN:
            c = "prism"
        if len(c) > 140:
            c = c[:137] + "…"
        parts.append(c)
    return " ".join(parts)


def _event_session_id(evt):
    """Session id carried by an engine event, if any."""
    sid = evt.get("sessionID")
    if sid:
        return sid
    part = evt.get("part")
    if isinstance(part, dict):
        return part.get("sessionID")
    return None


def _event_text(evt):
    """Assistant prose an event carries, or None.

    Verified against a live run: text sits at part.text, and one event carries
    one *complete* part — an 844-character answer arrived in a single event,
    not as token deltas. Parts must therefore be separated when emitted, or
    consecutive messages run together into one paragraph.
    """
    part = evt.get("part")
    if isinstance(part, dict) and part.get("type") == "text":
        return part.get("text") or ""
    if evt.get("type") == "text" and isinstance(evt.get("text"), str):
        return evt["text"]
    return None


def _event_tool(evt):
    """One-line description of a tool the agent just used, or None.

    Without this the transcript shows only the agent's commentary and looks
    stalled during the long stretches where it is reading files and running
    git — which is most of a review.
    """
    part = evt.get("part")
    if not isinstance(part, dict) or part.get("type") != "tool":
        return None
    name = part.get("tool") or "tool"
    state = part.get("state") or {}
    detail = state.get("title")
    if not detail:
        args = state.get("input")
        if isinstance(args, dict):
            detail = (args.get("command") or args.get("filePath")
                      or args.get("path") or args.get("pattern") or "")
        else:
            detail = ""
    detail = " ".join(str(detail).split())
    if len(detail) > 160:
        detail = detail[:157] + "…"
    status = state.get("status")
    mark = "✗" if status == "error" else "⚙"
    return f"{mark} {name}" + (f": {detail}" if detail else "")


def _run_stream(cmd, cwd, emit, timeout=1200, control=None, json_mode=False):
    """Run cmd, streaming output to emit.

    Returns (rc, text, session_id). In json_mode the engine speaks a stream of
    JSON events instead of rendered markdown: prose is reassembled from the
    text events for the verdict parser and the log, and the session id is
    captured so later turns can be pinned to this exact conversation with
    --session rather than racing on --continue. session_id is None whenever
    anything about the stream was unexpected, which tells the caller not to
    trust continuation.
    """
    if control is not None:
        control.check()
    emit(f"$ {_display_cmd(cmd)}")
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    if control is not None:
        control.attach(proc)
    out_lines = []
    timed_out = threading.Event()

    def _kill_on_timeout():
        # A wall-clock watchdog, not a per-line check: an agent that hangs
        # without printing would otherwise block the reader forever and leave
        # the UI stuck on "Running…" with no way out.
        timed_out.set()
        emit(f"⚠ No completion after {timeout}s — terminating the run.")
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.daemon = True
    watchdog.start()
    session_id = None
    text_parts = []
    seen_parts = {}
    malformed = False

    def emit_block(block):
        """Release one completed part, line by line."""
        for out_line in block.split("\n"):
            emit(out_line, "agent")

    try:
        for line in proc.stdout:
            out_lines.append(line)
            if not json_mode:
                emit(line.rstrip())
                continue
            stripped = line.strip()
            if not stripped:
                continue
            try:
                evt = json.loads(stripped)
            except ValueError:
                malformed = True
                continue
            if not isinstance(evt, dict):
                malformed = True
                continue
            if session_id is None:
                session_id = _event_session_id(evt)
            tool = _event_tool(evt)
            if tool is not None:
                emit(tool, "tool")
                continue
            piece = _event_text(evt)
            if piece is None:
                if str(evt.get("type", "")).endswith("error"):
                    emit(f"⚠ {evt.get('message') or stripped[:200]}")
                continue
            part_id = ((evt.get("part") or {}).get("id")
                       if isinstance(evt.get("part"), dict) else None)
            if part_id is not None and part_id in seen_parts:
                # Same part seen again: cumulative if it extends what we have,
                # otherwise a delta. Handle both rather than assume.
                prev = seen_parts[part_id]
                addition = piece[len(prev):] if piece.startswith(prev) else piece
                seen_parts[part_id] = prev + addition if not piece.startswith(prev) else piece
                if addition:
                    emit_block(addition)
                    text_parts.append(addition)
                continue
            if part_id is not None:
                seen_parts[part_id] = piece
            # A new part always starts on its own line, or consecutive replies
            # concatenate into one unreadable paragraph.
            if text_parts and not text_parts[-1].endswith("\n"):
                text_parts.append("\n")
            text_parts.append(piece)
            emit_block(piece)
        proc.wait(timeout=60)
    except Exception as e:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        out_lines.append(f"\n[process error: {e}]\n")
        return 1, "".join(out_lines), None
    finally:
        watchdog.cancel()
        # Close the pipe explicitly: with many jobs in flight, relying on GC to
        # reclaim these leaks file descriptors (and trips ResourceWarning).
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        if control is not None:
            control.detach()
    if control is not None and control.cancelled():
        raise Cancelled("Run stopped.")
    if json_mode:
        text = "".join(text_parts)
        if malformed:
            # Any surprise in the stream means the id may not belong to this
            # conversation; refuse it rather than pin the wrong session.
            session_id = None
    else:
        text = "".join(out_lines)
    if timed_out.is_set():
        out_lines.append("\n[TIMEOUT after %ss]\n" % timeout)
        return 1, text + "\n[TIMEOUT after %ss]\n" % timeout, session_id
    rc = proc.returncode
    return (1 if rc is None else rc), text, session_id


def run_opencode_review(pr_id, repo_name, local_repo, project_dir,
                        region=REGION_DEFAULT, emit=_emit_plain, model=None,
                        control=None):
    """Step 1: trigger the pr-reviewer agent. Returns raw agent output."""
    exe = require_engine()
    agent = ensure_bundled_agent(project_dir, emit=emit)
    prompt = (
        f"Review CodeCommit pull request {pr_id} in CodeCommit repository "
        f"'{repo_name}' (AWS region {region}). Local clone path: {local_repo}. "
        f"Run directory: {project_dir}. "
        f"Follow your pr-reviewer workflow and report the verdict in chat, "
        f"then stop and ask about updating the PR description."
    )
    # No --session/--continue: a plain run always creates a fresh session, and
    # --format json is what lets us capture its id for the follow-up turns.
    json_mode = engine_supports_json(exe)
    cmd = [exe, "run", "--agent", agent, "--dir", project_dir, "--auto"]
    if json_mode:
        cmd += ["--format", "json"]
    if model:
        cmd += ["--model", model]
    cmd += [prompt]
    rc, out, session_id = _run_stream(cmd, cwd=project_dir, emit=emit,
                                      control=control, json_mode=json_mode)
    if rc != 0 and "Verdict" not in out:
        raise RuntimeError(f"Review run failed (exit {rc}). See log.")
    return out, session_id


def run_opencode_reply(message, project_dir, session_id=None, emit=_emit_plain,
                       model=None, control=None):
    """Send a free-text reply into the agent's existing session.

    Each `opencode run` is one turn, so answering a question is another turn
    continued onto the same session — which is what lets the UI hold a real
    back-and-forth with the reviewer. Returns (output, session_id).
    """
    return _continue_turn(message, project_dir, session_id, emit, model, control,
                          what="reply")


def _continue_turn(prompt, project_dir, session_id, emit, model, control, what):
    """One follow-up agent turn, pinned to a session where possible.

    `--session <id>` addresses one specific conversation and cannot be hijacked
    by a concurrent `--continue` elsewhere. When no id was captured we fall
    back to `--continue`, which resolves to "the newest top-level session in
    this directory" and therefore *can* pick up another job's conversation —
    so that path is serialised on the project directory.
    """
    exe = require_engine()
    json_mode = engine_supports_json(exe)
    cmd = [exe, "run", "--agent", AGENT_NAME, "--dir", project_dir, "--auto"]
    if json_mode:
        cmd += ["--format", "json"]
    if model:
        cmd += ["--model", model]
    if session_id:
        cmd += ["--session", session_id]
        cmd += [prompt]
        rc, out, sid = _run_stream(cmd, cwd=project_dir, emit=emit,
                                   control=control, json_mode=json_mode)
        return rc, out, (sid or session_id)
    emit(f"⚠ No session id was captured — running this {what} with --continue "
         f"and holding a lock on {project_dir} so a concurrent job here cannot "
         f"take over the wrong conversation.")
    cmd += ["--continue", prompt]
    with _lock_for(project_dir):
        rc, out, sid = _run_stream(cmd, cwd=project_dir, emit=emit,
                                   control=control, json_mode=json_mode)
    # If this turn did yield a clean id, later turns pin properly again.
    return rc, out, sid


def run_opencode_approve_description(pr_id, project_dir, session_id=None,
                                     emit=_emit_plain, model=None,
                                     repo_name=None, region=REGION_DEFAULT,
                                     control=None):
    """Step 2: tell the agent 'yes' — append findings to the PR description.

    Continues the review's own session so the agent still has the findings it
    just produced. Returns (output, session_id).
    """
    prompt = (
        f"Yes — append your findings as bullet points to the description of "
        f"pull request {pr_id}"
        + (f" (CodeCommit repository '{repo_name}', AWS region {region})" if repo_name else "")
        + " now (I approve this specific update)."
    )
    rc, out, sid = _continue_turn(prompt, project_dir, session_id, emit, model,
                                  control, what="description update")
    if rc != 0:
        raise RuntimeError(f"Agent description-update run failed (exit {rc}).")
    return out, sid


def aws_cli(*args, region=REGION_DEFAULT):
    cmd = ["aws", "codecommit"] + list(args) + (["--region", region] if region else [])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"aws {' '.join(cmd[2:])} failed:\n{proc.stderr.strip() or proc.stdout.strip()}")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"_raw": proc.stdout}


def get_pr(pr_id, region=REGION_DEFAULT):
    data = aws_cli("get-pull-request", "--pull-request-id", str(pr_id), region=region)
    pr = data.get("pullRequest", {})
    targets = pr.get("pullRequestTargets") or [{}]
    t = targets[0]
    return {
        "repositoryName": t.get("repositoryName", ""),
        "sourceReference": (t.get("sourceReference") or "").replace("refs/heads/", ""),
        "destinationReference": (t.get("destinationReference") or "").replace("refs/heads/", ""),
        "sourceCommit": t.get("sourceCommit", ""),
        "destinationCommit": t.get("destinationCommit", ""),
        "title": pr.get("title", ""),
        "description": pr.get("description", "") or "",
        "status": pr.get("pullRequestStatus", ""),
        "raw": data,
    }


def description_has_review_block(pr_id, region=REGION_DEFAULT):
    """True when the PR description already carries a pr-reviewer marker block.

    Used to verify the agent actually performed the Step-5 write: the engine
    exits 0 even when the agent declines to act, so a zero exit code alone is
    not evidence that anything was written.
    """
    try:
        return bool(DESC_BLOCK_RE.search(get_pr(pr_id, region=region)["description"]))
    except Exception:  # noqa: BLE001
        return False


def update_description_direct(pr_id, verdict_text, impact, findings, region=REGION_DEFAULT):
    """Fallback: replicate the agent's Step-5 marker block via AWS CLI directly."""
    pr = get_pr(pr_id, region=region)
    desc = DESC_BLOCK_RE.sub("", pr["description"] or "").strip()  # drop stale block
    bullets = "\n".join(f"{b}" for b in (findings or [])[:20]) or "- (no discrete findings)"
    header = (
        "\n\n<!-- pr-reviewer:start -->\n---\n"
        f"**Automated review — {verdict_text} (impact {impact}/10)**\n"
    )
    footer = "\n<!-- pr-reviewer:end -->"
    merged = (desc + header + bullets + footer).strip()
    if len(merged) > PR_DESCRIPTION_MAX:
        # CodeCommit hard-rejects oversized descriptions. Shrink the parts we
        # own (bullets first, then the author's text) and never cut mid-block:
        # an unclosed marker would break the next run's stale-block strip.
        note = "- … findings truncated to fit the CodeCommit description limit."
        fixed = len(header) + len(footer) + len(note) + 1
        room = PR_DESCRIPTION_MAX - len(desc) - fixed
        if room < 0:  # the author's own text alone blows the budget
            desc = desc[:max(0, PR_DESCRIPTION_MAX - fixed)]
            room = 0
        kept, used = [], 0
        for b in bullets.splitlines():
            if used + len(b) + 1 > room:
                break
            kept.append(b)
            used += len(b) + 1
        kept.append(note)
        merged = (desc + header + "\n".join(kept) + footer).strip()
    aws_cli("update-pull-request-description", "--pull-request-id", str(pr_id),
            "--description", merged, region=region)
    return merged


def check_ff_mergeable(repo_name, dest, src, region=REGION_DEFAULT):
    """Ask CodeCommit whether a fast-forward merge is currently possible."""
    try:
        data = aws_cli(
            "get-merge-conflicts",
            "--repository-name", repo_name,
            "--destination-commit-specifier", dest,
            "--source-commit-specifier", src,
            "--merge-option", "FAST_FORWARD_MERGE",
            region=region,
        )
    except RuntimeError as e:
        # e.g. MergeOptionNotSupported / commits unknown — surface as not mergeable
        return False, str(e)
    mergeable = data.get("mergeable")
    if mergeable is True:
        return True, "mergeable=true"
    return bool(mergeable), json.dumps(data)[:500]


def try_fast_forward_merge(pr_id, repo_name, region=REGION_DEFAULT):
    data = aws_cli("merge-pull-request-by-fast-forward",
                   "--pull-request-id", str(pr_id),
                   "--repository-name", repo_name, region=region)
    return data


def sync_destination_into_source(local_repo, dest, src, pr_id, emit=_emit_plain):
    """Sync destination commits onto the source branch locally, then push.

    Equivalent of: git fetch, checkout source, merge origin/destination, push.
    Raises on merge conflicts (aborts the merge first).

    Serialised on the clone: this is the one place that mutates the working
    tree, and two jobs reviewing different PRs out of the same checkout would
    otherwise interleave each other's `git checkout`. Reviews deliberately run
    outside this lock — they read remote refs (origin/<dest>...origin/<src>),
    which a concurrent checkout does not disturb, and holding the lock across a
    multi-minute agent turn would serialise the whole tool.
    """
    with _lock_for(local_repo):
        return _sync_locked(local_repo, dest, src, pr_id, emit)


def _sync_locked(local_repo, dest, src, pr_id, emit):
    def git(*args, quiet=False):
        cmd = ["git"] + list(args)
        if not quiet:
            emit(f"(git:{Path(local_repo).name})$ {' '.join(cmd)}")
        proc = subprocess.run(cmd, cwd=local_repo, capture_output=True, text=True, timeout=300)
        out = (proc.stdout or "") + (proc.stderr or "")
        if not quiet:
            for line in out.strip().splitlines()[-8:]:
                emit("  " + line)
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed:\n{out.strip()[-2000:]}")
        return out

    # A sync mutates the user's working clone, so refuse to start on a dirty
    # tree rather than letting `git checkout` fail with a cryptic message
    # halfway through. Untracked files are excluded (-uno): they don't block a
    # checkout or merge, and PRISM's own `.opencode/` agent install would
    # otherwise make every single-repo run look dirty.
    if git("status", "--porcelain", "-uno", quiet=True).strip():
        raise RuntimeError(
            f"Working clone {local_repo} has uncommitted changes to tracked files — "
            f"commit or stash them before PRISM can sync {dest} into {src}.")
    try:
        original = git("rev-parse", "--abbrev-ref", "HEAD", quiet=True).strip()
    except RuntimeError:
        original = ""

    try:
        git("fetch", "origin", dest, src)
        # checkout source (create tracking branch if needed)
        try:
            git("checkout", src)
        except RuntimeError:
            git("checkout", "-b", src, f"origin/{src}")
        # Align the local branch with the remote *without* a merge commit.
        # Anything other than "equal" or "strictly behind" means the clone
        # holds commits the PR does not, and the push below would silently add
        # them to the pull request — so stop instead. Note `merge --ff-only`
        # alone is not enough: it reports success ("already up to date") when
        # the local branch is ahead, which is exactly the case to refuse.
        local = git("rev-parse", "HEAD", quiet=True).strip()
        remote = git("rev-parse", f"origin/{src}", quiet=True).strip()
        if local != remote:
            behind = subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{src}"],
                cwd=local_repo, capture_output=True, timeout=60).returncode == 0
            if not behind:
                raise RuntimeError(
                    f"Local branch '{src}' has commits that origin/{src} does not "
                    f"({local[:8]} vs {remote[:8]}). PRISM will not push local-only "
                    f"commits into PR #{pr_id}. Reconcile the branch manually, then re-run.")
            git("merge", "--ff-only", f"origin/{src}")
        try:
            git("merge", f"origin/{dest}", "-m",
                f"chore: sync {dest} into {src} for PR #{pr_id} fast-forward")
        except RuntimeError as e:
            try:
                subprocess.run(["git", "merge", "--abort"], cwd=local_repo,
                               capture_output=True, timeout=60)
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"Sync merge hit conflicts ({dest} → {src}). Aborted; resolve manually.\n{e}")
        git("push", "origin", src)
    finally:
        # Leave the clone on the branch the user had checked out — PRISM is a
        # reviewer, not something that should silently move their HEAD.
        if original and original != "HEAD" and original != src:
            try:
                git("checkout", original, quiet=True)
                emit(f"  (restored branch {original})")
            except RuntimeError:
                emit(f"⚠ Could not restore branch {original}; clone is left on {src}.")


MAX_CLARIFY_ROUNDS = 4


def full_pipeline(project_dir, repo_name, pr_id, local_repo=None,
                  region=REGION_DEFAULT,
                  do_update_desc=True, do_merge=True, do_sync=True,
                  dry_run=False, model=None, progress=None, emit=_emit_plain,
                  control=None, ask=None):
    """Run the whole review → describe → merge pipeline. Returns dict summary.

    Project-agnostic: works with any CodeCommit repo + local clone.
    `progress(stage_id, state)` is called with state in
    {"active", "done", "skipped", "error"} so the UI progress bar can show
    the live stage and what's still pending — the stage set is dynamic
    (describe/merge stages are skipped when disabled, blocked by verdict,
    or dry-run; a sync stage appears only when a fast-forward needs it).
    """
    def pg(stage, state):
        if progress:
            try:
                progress(stage, state)
            except Exception:  # noqa: BLE001
                pass

    def ck():
        if control is not None:
            control.check()

    emit(f"▸ Target: CodeCommit repo '{repo_name}'  |  PR #{pr_id}  |  region {region}"
         + (f"  |  model {model}" if model else "  |  model (default)"))
    repo_name, local_clone = resolve_repo(repo_name, project_dir, local_repo)
    emit(f"▸ Local clone: {local_clone}")
    ensure_bundled_agent(project_dir, emit=emit)

    if not Path(local_clone).is_dir():
        raise RuntimeError(f"Local clone path not found: {local_clone}")

    # ---- Step 1: review ----
    ck()
    pg(STAGE_REVIEW, "active")
    emit("\n═══ STEP 1 — AI review ═══")
    raw, session_id = run_opencode_review(pr_id, repo_name, local_clone, project_dir,
                                          region=region, emit=emit, model=model,
                                          control=control)
    review = parse_review_output(raw)

    # The agent stops and asks when something is missing or ambiguous. Rather
    # than dead-ending on "no verdict", hand the question to the UI and feed
    # the answer back into the same session.
    rounds = 0
    while ask is not None and rounds < MAX_CLARIFY_ROUNDS:
        if review.verdict_key and review.verdict_key != "unknown":
            break
        question = extract_question(raw)
        if not question:
            break
        ck()
        emit("\n💬 The reviewer is asking a question — waiting for your reply …")
        answer = ask(question)
        ck()
        if not answer:
            emit("▸ No reply given; continuing without one.")
            break
        rounds += 1
        emit(f"▸ You: {answer[:300]}")
        _rc, raw, session_id = _continue_turn(answer, project_dir, session_id,
                                              emit, model, control, what="reply")
        review = parse_review_output(raw)

    emit(f"\n◆ Verdict: {review.verdict_raw or review.verdict_key}   "
         f"Impact: {review.impact_score or '?'}/10 {review.impact_reason}")
    pg(STAGE_REVIEW, "done")
    if not review.verdict_key or review.verdict_key == "unknown":
        emit("⚠ Could not parse a verdict — stopping before any write/merge.")
        pg(STAGE_DESCRIBE, "skipped")
        pg(STAGE_MERGE_CHECK, "skipped")
        pg(STAGE_MERGE, "skipped")
        return {"review": review, "merged": False, "stopped": "unparsed-verdict"}

    # ---- Step 2: description ----
    ck()
    if do_update_desc:
        pg(STAGE_DESCRIBE, "active")
        emit("\n═══ STEP 2 — Update PR description ═══")
        if dry_run:
            emit("(dry-run) would ask the reviewer to append findings + fallback direct update.")
            pg(STAGE_DESCRIBE, "skipped")
        else:
            try:
                _out, session_id = run_opencode_approve_description(
                    pr_id, project_dir, session_id, emit=emit, model=model,
                    repo_name=repo_name, region=region, control=control)
            except Exception as e:  # noqa: BLE001
                emit(f"⚠ Reviewer update run failed ({e}).")
            # A zero exit code only means the engine ran, not that the agent
            # wrote anything — confirm against the live PR before believing it.
            if description_has_review_block(pr_id, region=region):
                emit("◆ Description update finished (marker block present on the PR).")
                pg(STAGE_DESCRIBE, "done")
            else:
                emit("⚠ No reviewer block on the PR — falling back to direct AWS update …")
                try:
                    merged = update_description_direct(
                        pr_id, review.verdict_raw or review.verdict_key,
                        review.impact_score or "?", review.findings, region=region)
                    emit(f"◆ Direct description update wrote {len(merged)} chars.")
                    pg(STAGE_DESCRIBE, "done")
                except Exception as e:  # noqa: BLE001
                    # A description is cosmetic — report loudly, but don't throw
                    # away a completed review by aborting the whole run.
                    emit(f"⚠ Direct description update failed too: {e}")
                    emit("⚠ Continuing — the PR description was NOT updated.")
                    pg(STAGE_DESCRIBE, "error")
    else:
        emit("\n═══ STEP 2 — Update PR description (skipped by option) ═══")
        pg(STAGE_DESCRIBE, "skipped")

    # ---- Steps 3/4: merge ----
    ck()
    if not do_merge:
        emit("\n═══ Merge skipped by option. Done. ═══")
        pg(STAGE_MERGE_CHECK, "skipped")
        pg(STAGE_MERGE, "skipped")
        return {"review": review, "merged": False, "stopped": "merge-disabled"}

    if not review.merge_allowed():
        emit(f"\n⛔ Verdict is '{review.verdict_raw}' — NOT merging (needs changes/blocked). Done.")
        pg(STAGE_MERGE_CHECK, "skipped")
        pg(STAGE_MERGE, "skipped")
        return {"review": review, "merged": False, "stopped": "verdict-blocks-merge"}

    ck()
    pr = get_pr(pr_id, region=region)
    if pr["status"] != "OPEN":
        emit(f"\n⛔ PR status is {pr['status']} (not OPEN) — will not merge.")
        pg(STAGE_MERGE_CHECK, "skipped")
        pg(STAGE_MERGE, "skipped")
        return {"review": review, "merged": False, "stopped": f"status-{pr['status']}"}
    if pr["repositoryName"] and pr["repositoryName"] != repo_name:
        emit(f"⚠ PR lives in '{pr['repositoryName']}' (target said '{repo_name}'); "
             f"using '{pr['repositoryName']}' for merge calls.")
        repo_name = pr["repositoryName"]
    dest, src = pr["destinationReference"], pr["sourceReference"]
    pg(STAGE_MERGE_CHECK, "active")
    emit(f"\n═══ STEP 3 — Merge check ({src} → {dest}) ═══")
    mergeable, detail = check_ff_mergeable(repo_name, dest, src, region=region)
    emit(f"◆ Fast-forward mergeable: {mergeable} ({detail[:200]})")
    pg(STAGE_MERGE_CHECK, "done")

    if dry_run:
        emit("(dry-run) would merge now. Stopping.")
        pg(STAGE_MERGE, "skipped")
        return {"review": review, "merged": False, "stopped": "dry-run"}

    def _sync_then_merge():
        """Sync destination into source, then retry the fast-forward merge."""
        pg(STAGE_SYNC, "active")
        emit(f"▸ Syncing {dest} into {src}, then retrying merge …")
        try:
            sync_destination_into_source(local_clone, dest, src, pr_id, emit=emit)
        except RuntimeError:
            pg(STAGE_SYNC, "error")
            raise
        pg(STAGE_SYNC, "done")
        # The push above has to land on CodeCommit's side before the PR's
        # source tip reflects it, so give it a few bounded attempts.
        last = None
        for attempt in range(3):
            time.sleep(2)
            try:
                try_fast_forward_merge(pr_id, repo_name, region=region)
                emit(f"✅ Synced + merged PR #{pr_id} by fast-forward. Closed.")
                pg(STAGE_MERGE, "done")
                return {"review": review, "merged": True,
                        "strategy": "sync-then-fast-forward"}
            except RuntimeError as err:
                last = err
                emit(f"  merge retry {attempt + 1}/3 failed: {str(err)[:200]}")
        pg(STAGE_MERGE, "error")
        raise RuntimeError(f"Merge still failed after syncing {dest} into {src}.\n{last}")

    ck()
    pg(STAGE_MERGE, "active")
    emit("\n═══ STEP 4 — Merge ═══")
    if not mergeable:
        # The merge check already told us a fast-forward can't work; go straight
        # to the sync instead of spending a doomed write call on CodeCommit.
        if not do_sync:
            emit("⛔ Not fast-forward mergeable and sync is disabled — stopping.")
            pg(STAGE_MERGE, "error")
            return {"review": review, "merged": False, "stopped": "not-fast-forwardable"}
        return _sync_then_merge()
    try:
        try_fast_forward_merge(pr_id, repo_name, region=region)
        emit(f"✅ Merged PR #{pr_id} by fast-forward. Closed.")
        pg(STAGE_MERGE, "done")
        return {"review": review, "merged": True, "strategy": "fast-forward"}
    except RuntimeError as e:
        emit(f"⚠ Fast-forward failed: {str(e)[:400]}")
        if not do_sync:
            pg(STAGE_MERGE, "error")
            raise
        return _sync_then_merge()
