#!/usr/bin/env python3
"""PRISM on the command line.

    prism run 214                 review PR 214 of the repository in this folder
    prism docs docs/spec.md       review a spec / requirement document in this repository
    prism jobs                    what is running, and what finished
    prism logs 7f3a9c -f          follow a job's transcript
    prism stop 7f3a9c             stop a job
    prism config set region eu-west-1

It runs exactly the pipeline the desktop application runs (orchestrator.
full_pipeline) with the same gates, so a review means the same thing wherever
it was started. It needs no display, which is the point: ssh into the machine
that has AWS access and the reviewer, and work from there.

Every run leaves a record under ~/.prism/cli-jobs/ (see cli_jobs.py), which is
how the desktop application lists, stops and retries jobs it did not start.

Stdlib only, and no Tkinter - this module must import on a machine with no
display and no Tk.
"""
import argparse
import dataclasses
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as CFG  # noqa: E402
import chat as CHAT  # noqa: E402
import chatsources as CHS  # noqa: E402
import cli_jobs as CJ  # noqa: E402
import diagnostics as DG  # noqa: E402
import docreview as DR  # noqa: E402
import gitlab as GL  # noqa: E402
import jobs as J  # noqa: E402
import jobid  # noqa: E402
import winproc  # noqa: E402
from pathutil import native_path, native_rel  # noqa: E402
from orchestrator import (  # noqa: E402
    INTERNAL_SUMMARY_KEYS, REGION_DEFAULT, TOOL_DIR, Cancelled, RunControl, detect_local_repos, followup_declined,
    full_pipeline, list_available_models, normalise_verdict, run_followup,
    STAGE_REVIEW, STAGE_DESCRIBE, STAGE_MERGE_CHECK, STAGE_SYNC, STAGE_MERGE,
)
from version import __version__  # noqa: E402

PROG = "prism"

# Words the first argument can be. The desktop binary uses this to decide
# whether it was started as a command or as the application.
COMMANDS = ("run", "docs", "jobs", "logs", "stop", "retry", "ask", "chat", "rm", "models",
            "repos",
            "doctor", "export", "config", "man", "version", "update", "help")

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_STOPPED = 0, 1, 2, 130

STAGE_NAMES = {STAGE_REVIEW: "Review", STAGE_DESCRIBE: "Describe",
               STAGE_MERGE_CHECK: "Merge check", STAGE_SYNC: "Sync", STAGE_MERGE: "Merge",
               DR.STAGE_DOC_REVIEW: "Review", DR.STAGE_DOC_WRITE: "Write",
               DR.STAGE_DOC_COMMIT: "Commit", DR.STAGE_DOC_PUSH: "Push"}

_ANSI = {"ok": "32", "warn": "33", "err": "31", "dim": "2", "bold": "1"}


class UsageError(Exception):
    """Something the person can fix by changing the command."""


# ---------------------------------------------------------------- output --
def _color_on(stream):
    return (hasattr(stream, "isatty") and stream.isatty()
            and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb")


def paint(text, kind, stream=None):
    stream = stream or sys.stdout
    code = _ANSI.get(kind)
    return f"\033[{code}m{text}\033[0m" if code and _color_on(stream) else text


def classify_line(text):
    """Tag for a line PRISM emitted itself - same markers the desktop colours by."""
    if text.startswith(("◆", "✅")):
        return "ok"
    if text.startswith(("⚠", "■")):
        return "warn"
    if text.startswith("⛔") or "ERROR:" in text or "Traceback" in text:
        return "err"
    return None


def human_age(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d"


# ----------------------------------------------------- finding the target --
def _git(path, *args):
    try:
        out = subprocess.run(["git", "-C", str(path)] + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             universal_newlines=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


_REMOTE_PATTERNS = (
    # codecommit::us-east-1://repo   /   codecommit://profile@repo
    re.compile(r"^codecommit::(?P<region>[a-z]{2}(?:-[a-z]+)+-\d)://(?:[^@/]+@)?(?P<repo>[^/]+)$"),
    re.compile(r"^codecommit://(?:[^@/]+@)?(?P<repo>[^/]+)$"),
    # https / ssh git-codecommit.<region>.amazonaws.com/v1/repos/<repo>
    re.compile(r"git-codecommit\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?"
               r"/v1/repos/(?P<repo>[^/?#]+)"),
)


def parse_remote(url):
    """(repo, region) from a CodeCommit remote URL; ("", "") for anything else."""
    url = (url or "").strip()
    for pattern in _REMOTE_PATTERNS:
        m = pattern.search(url)
        if m:
            groups = m.groupdict()
            return groups["repo"].rstrip("/"), groups.get("region") or ""
    return "", ""


def resolve_target(path=None, repo=None, clone=None, cfg=None, cwd=None):
    """Work out (project_dir, repo_name, local_repo, region_hint) for a run.

    "Run at the current path" is the whole idea, so nothing is required:

    * the folder is `path`, else the saved default, else where you are - and
      inside a git repository it is that repository's top, so running from a
      subdirectory works;
    * the repository name is `repo`, else the saved default, else read from
      the clone's CodeCommit remote, else the folder's name (the same last
      resort the desktop uses);
    * a folder holding several clones needs `repo` to say which one.

    Raises UsageError with what to do next, never guesses between clones.
    """
    cfg = cfg if cfg is not None else CFG.get_cli()
    start = Path(native_path(path or cfg.get("project_dir") or cwd or os.getcwd()))
    try:
        start = start.resolve()
    except OSError:
        pass
    if not start.is_dir():
        raise UsageError(f"{start} is not a folder.")

    top = _git(start, "rev-parse", "--show-toplevel")
    base = Path(top) if top and not path else start
    is_repo = (base / ".git").exists()
    remote_repo, remote_region = (parse_remote(_git(base, "config", "--get", "remote.origin.url"))
                                  if is_repo else ("", ""))

    name = (repo or cfg.get("repo") or "").strip()
    subs = [(n, p) for n, p in detect_local_repos(str(base)) if Path(p) != base]
    local = str(Path(native_path(clone)).resolve()) if clone else None

    if not name:
        if is_repo:
            name = remote_repo or base.name
        elif len(subs) == 1:
            name = subs[0][0]
        elif len(subs) > 1:
            raise UsageError(
                f"{base} holds {len(subs)} clones ({', '.join(n for n, _ in subs)}). "
                f"Say which one: prism run <pr> --repo <name>")
        else:
            raise UsageError(
                f"No git clone found in {base}. Run from inside one, or pass "
                f"--repo <CodeCommit repository name>.")

    if local is None:
        if is_repo and name in (base.name, remote_repo):
            local = str(base)
        else:
            for n, p in subs:
                if n == name:
                    local = p
    # Only trust the remote's region when it is the repo being reviewed.
    region_hint = remote_region if (is_repo and name in (base.name, remote_repo)) else ""
    return str(base), name, local, region_hint


def detect_host(clone):
    """What the clone's origin remote is: classify_remote's dict (host, base_url,
    project, region). No clone, or no remote, is CodeCommit as before."""
    url = _git(clone, "config", "--get", "remote.origin.url") if clone else ""
    return GL.classify_remote(url)


def build_spec(args, cfg=None, cwd=None):
    cfg = cfg if cfg is not None else CFG.get_cli()
    project_dir, repo, local, region_hint = resolve_target(
        args.path, args.repo, args.clone, cfg=cfg, cwd=cwd)
    if local is None and args.repo:
        # `--repo` may be the GitLab project path correcting what the remote says
        # (FR-GL-2); the clone is then still the one you are in.
        _p, _r, here, _h = resolve_target(args.path, None, args.clone, cfg=cfg, cwd=cwd)
        if here and detect_host(here)["host"] == GL.HOST_GITLAB:
            project_dir, local = _p, here
    pr = str(args.pr_id).strip().lstrip("#!")
    if not pr.isdigit():
        raise UsageError("The pull request or merge request id must be a number, e.g. "
                         "prism run 214 (a GitLab merge request may be written !214)")
    info = detect_host(local)
    if info["host"] == GL.HOST_UNSUPPORTED:
        raise UsageError(f"{local}: its origin remote is neither AWS CodeCommit nor GitLab. "
                         f"PRISM reviews CodeCommit pull requests and GitLab merge requests "
                         f"only. (A self-managed GitLab whose name does not contain "
                         f"'gitlab': prism config set gitlab_hosts <host>)")
    host, host_url = info["host"], ""
    if host == GL.HOST_GITLAB:
        host_url = info["base_url"]
        repo = (args.repo or "").strip() or info["project"]
        if not GL.get_token(host_url):
            raise UsageError("No GitLab token. Set GITLAB_TOKEN, or save one with: "
                             f"{PROG} config set gitlab_token <token> (it needs the api scope "
                             f"to read, describe and merge the merge request).")

    def flag(cli_value, key):
        return cfg[key] if cli_value is None else cli_value

    do_review = flag(args.review, "review")
    do_describe = flag(args.describe, "describe") and do_review
    instructions = args.instructions if args.instructions is not None else cfg["instructions"]
    if args.instructions_file:
        try:
            instructions = Path(native_path(args.instructions_file)).read_text(encoding="utf-8")
        except OSError as e:
            raise UsageError(f"Cannot read {args.instructions_file}: {e.strerror or e}")
    return J.JobSpec(
        project_dir=project_dir, repo_name=repo, pr_id=pr, local_repo=local,
        region=args.region or cfg["region"] or region_hint or REGION_DEFAULT,
        model=args.model or cfg["model"] or None,
        do_review=do_review, do_update_desc=do_describe,
        do_merge=flag(args.merge, "merge"), do_sync=flag(args.sync, "sync"),
        dry_run=flag(args.dry_run, "dry_run"),
        webhook_url=CFG.get_webhook_url() or None, reviewer=CFG.effective_username(),
        custom_instructions=(instructions or "").strip(), origin="cli",
        host=host, host_url=host_url)


def with_identity(spec):
    """`spec` with the saved name and webhook, read now.

    These are machine-wide settings, not part of what a job is: a retry, or a
    background run that received its spec as text, picks up whatever is saved
    today - and the webhook URL (a secret) never has to travel on a command line.
    """
    if getattr(spec, "kind", "pr") == "docs":
        return dataclasses.replace(spec, reviewer=CFG.effective_username(),
                                   webhook_url=CFG.get_docs_webhook_url() or None)
    return dataclasses.replace(spec, reviewer=CFG.effective_username(),
                               webhook_url=CFG.get_webhook_url() or None)


def _current_branch(clone):
    branch = _git(clone, "rev-parse", "--abbrev-ref", "HEAD")
    return "" if branch in ("", "HEAD") else branch


def _doc_path(arg, clone, cwd):
    """A document argument: ("repo", path from the repository root) or ("upload", file).

    Typed the way a shell would: relative to where you are (so tab completion from a
    subfolder works), or absolute, or - for a file that only exists on the branch -
    already from the repository root. A file that exists on this machine but outside
    the repository is an uploaded document (FR-DOC-IN-5): reviewed from where it is,
    and committed only to a --dest you give.
    """
    given = Path(native_path(arg))
    local = given if given.is_absolute() else Path(cwd) / given
    if clone is None:
        if local.is_file():
            return "upload", str(local.resolve())
        raise UsageError(f"{arg} is not a file. With no repository, documents are files on "
                         f"this machine.")
    clone_root = Path(native_path(clone)).resolve()
    for candidate in ([given] if given.is_absolute() else [Path(cwd) / given, clone_root / given]):
        try:
            rel = candidate.resolve().relative_to(clone_root)
        except (ValueError, OSError):
            continue
        if candidate.exists() and rel.parts:
            return "repo", str(rel)
    if local.is_file():
        return "upload", str(local.resolve())
    if given.is_absolute():
        raise UsageError(f"{arg} is not a file.")
    return "repo", native_rel(arg) or arg


def _dest_map(values, uploads):
    """{upload path: destination} from --dest FILE=PATH (or a bare PATH with one upload)."""
    out = {}
    for raw in values or []:
        raw = str(raw)
        if "=" in raw:
            src, dest = raw.split("=", 1)
            src_path = str(Path(native_path(src)).resolve()) if src.strip() else ""
            match = [u for u in uploads if u == src_path or Path(u).name == src.strip()]
            if len(match) != 1:
                raise UsageError(f"--dest {raw}: {src} is not one of the uploaded documents"
                                 + (f" ({', '.join(Path(u).name for u in uploads)})"
                                    if uploads else "") + ".")
            out[match[0]] = dest.strip()
        elif len(uploads) == 1:
            out[uploads[0]] = raw.strip()
        else:
            raise UsageError("--dest needs FILE=PATH when more than one document is uploaded.")
    return out


def build_docs_spec(args, cfg=None, cwd=None):
    """The DocJobSpec for `prism docs`, from the command line, saved defaults and
    where you are - the counterpart of build_spec.

    One repository (the usual case) is plain arguments: `prism docs docs/spec.md`
    reviews it in the repository you are in, on the branch you have checked out.
    A backend and/or frontend are `--be FILE...` and `--fe FILE...`, each with an
    optional `--be-clone/--be-branch` (`--fe-...`). PRISM never guesses between
    clones: it says what to pass.
    """
    cfg = cfg if cfg is not None else CFG.get_cli()
    cwd = Path(cwd or os.getcwd())
    start = Path(native_path(args.path or cfg.get("project_dir") or cwd))
    try:
        start = start.resolve()
    except OSError:
        pass
    if not start.is_dir():
        raise UsageError(f"{start} is not a folder.")
    top = _git(start, "rev-parse", "--show-toplevel")
    base = Path(top) if top and not args.path else start
    is_repo = (base / ".git").exists()
    subs = [(n, p) for n, p in detect_local_repos(str(base)) if Path(p) != base]

    plain, be, fe = list(args.files or []), list(args.be or []), list(args.fe or [])
    if plain and (be or fe):
        raise UsageError("Give the documents either as plain arguments (one repository) or "
                         "with --be / --fe, not both.")
    if not (plain or be or fe):
        raise UsageError(f"Say which document(s) to review: {PROG} docs docs/spec.md   "
                         f"(or --be / --fe FILE...)")
    if plain:
        if args.be_clone or args.be_branch or args.fe_clone or args.fe_branch:
            raise UsageError("--be-clone/--be-branch/--fe-clone/--fe-branch go with --be / --fe.")
        wanted = [("repo", args.clone, args.branch, plain)]
    else:
        if args.clone or args.branch:
            raise UsageError("--clone and --branch are for a single repository; with --be / "
                             "--fe use --be-clone/--be-branch and --fe-clone/--fe-branch.")
        for role, files, clone, branch in (("be", be, args.be_clone, args.be_branch),
                                           ("fe", fe, args.fe_clone, args.fe_branch)):
            if not files and (clone or branch):
                raise UsageError(f"--{role}-clone/--{role}-branch need --{role} FILE...")
        wanted = ([("BE", args.be_clone, args.be_branch, be)] if be else []) + \
                 ([("FE", args.fe_clone, args.fe_branch, fe)] if fe else [])

    def pick_clone(role, explicit):
        if explicit:
            path = Path(native_path(explicit))
            try:
                path = path.resolve()
            except OSError:
                pass
            if not (path.is_dir() and (path / ".git").exists()):
                raise UsageError(f"{explicit} is not a git clone.")
            return str(path)
        if role == "repo" or (len(wanted) == 1 and is_repo):
            if is_repo:
                return str(base)
            if len(subs) == 1:
                return subs[0][1]
            if subs:
                raise UsageError(
                    f"{base} holds {len(subs)} clones ({', '.join(n for n, _ in subs)}). "
                    f"Say which one: --clone <dir>")
            if role == "repo" and not args.branch:
                return None             # no repository: uploaded files only (FR-DOC-IN-7)
            raise UsageError(f"No git clone found in {base}. Run from inside one, or pass "
                             f"--clone <dir>.")
        named = [(n, p) for n, p in subs if DR.guess_role(n) == role]
        if len(named) == 1:
            return named[0][1]
        # Two clones, one of which is plainly the other role: this one is the rest.
        opposite = [(n, p) for n, p in subs if DR.guess_role(n) == ("FE" if role == "BE" else "BE")]
        if len(subs) == 2 and len(opposite) == 1:
            return [p for n, p in subs if (n, p) != opposite[0]][0]
        what = "backend" if role == "BE" else "frontend"
        raise UsageError(f"Cannot tell which clone is the {what}"
                         + (f" in {base} ({', '.join(n for n, _ in subs)})" if subs else "")
                         + f". Say so: --{role.lower()}-clone <dir>")

    targets = []
    dest_values = list(getattr(args, "dest", None) or [])
    all_uploads = []
    for role, explicit, branch, files in wanted:
        clone = pick_clone(role, explicit)
        kinds = [_doc_path(f, clone, cwd) for f in files]
        rels = tuple(dict.fromkeys(p for k, p in kinds if k == "repo"))
        ups = list(dict.fromkeys(p for k, p in kinds if k == "upload"))
        for u in ups:
            bad = DR.upload_type_problem(u)
            if bad:
                raise UsageError(bad)
        all_uploads += [(u, len(targets)) for u in ups]
        if clone is None:
            targets.append(DR.DocTarget(role=DR.ROLE_NONE, repo_name="", local_repo="",
                                        branch="", files=(), uploads=tuple(ups)))
            continue
        name = branch or _current_branch(clone)
        if not name:
            raise UsageError(f"{clone} is not on a branch (detached HEAD). Say which one: "
                             + ("--branch <name>" if role == "repo"
                                else f"--{role.lower()}-branch <name>"))
        bad = DR.check_branch(name)
        if bad:
            raise UsageError(f"{bad[0].upper()}{bad[1:]}.")
        targets.append(DR.DocTarget(
            role=role, repo_name=Path(clone).name, local_repo=clone, branch=name,
            files=rels, uploads=tuple(ups)))
    dests = _dest_map(dest_values, [u for u, _i in all_uploads])
    if dests:
        fixed = []
        for i, t in enumerate(targets):
            ups = tuple(DR.Upload(u.path, dests.get(u.path, "")) for u in t.uploads)
            if any(u.dest for u in ups) and not t.has_repo:
                raise UsageError("--dest needs a repository and a branch to commit to.")
            fixed.append(dataclasses.replace(t, uploads=ups))
        targets = fixed
    if len({t.local_repo for t in targets}) != len(targets):
        raise UsageError("The backend and the frontend are the same clone. Pass "
                         "--be-clone and --fe-clone.")

    # Optional: nothing is assumed. Given, the review centres on whether the decomposition
    # satisfies the requirements; absent, on the documents against the codebase alone.
    requirements = (args.requirements or cfg.get("requirements") or "").strip()
    if requirements:
        typed = (cwd / Path(native_path(requirements))) if not Path(native_path(requirements)) \
            .is_absolute() else Path(native_path(requirements))
        if typed.is_dir():     # a folder you can see from here: keep it relative to its repo
            requirements = DR.relativise_folder(
                typed, [t.local_repo for t in targets] + [str(base)])
    try:
        persona = DR.normalise_persona(args.persona or cfg.get("persona"))
    except ValueError as e:
        raise UsageError(str(e))

    def flag(cli_value, key):
        return cfg[key] if cli_value is None else cli_value

    instructions = args.instructions if args.instructions is not None else cfg["instructions"]
    if args.instructions_file:
        try:
            instructions = Path(native_path(args.instructions_file)).read_text(encoding="utf-8")
        except OSError as e:
            raise UsageError(f"Cannot read {args.instructions_file}: {e.strerror or e}")
    do_commit = flag(args.commit, "commit")
    # Who owns the request and answers the review (FR-OWN-1): required, never guessed.
    owner = CFG.clean_username(getattr(args, "owner", "") or "")
    if not owner:
        raise UsageError("Say who owns this review request - the person responsible for "
                         f"the document and for answering the review: --owner <name>")
    return DR.DocJobSpec(
        owner=owner,
        project_dir=str(base) if any(t.has_repo for t in targets) or is_repo or subs
        else (str(start) if args.path else ""),
        targets=tuple(targets), requirements_path=requirements,
        persona=persona, model=args.model or cfg["model"] or None, do_commit=do_commit,
        do_push=flag(args.push, "push") and do_commit, dry_run=flag(args.dry_run, "dry_run"),
        custom_instructions=(instructions or "").strip(), origin="cli",
        webhook_url=CFG.get_docs_webhook_url() or None, reviewer=CFG.effective_username())


# ----------------------------------------------------------------- input --
def read_line(prompt, control):
    """One line from the terminal, or None if the run is stopped meanwhile.

    Polled rather than blocking so a stop from the desktop (or Ctrl-C) frees a
    run that is waiting for an answer, as the desktop's own question box does.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    while True:
        if control.cancelled():
            return None
        try:
            if os.name != "nt":
                import select
                ready, _, _ = select.select([sys.stdin], [], [], 0.5)
                if not ready:
                    continue
            line = sys.stdin.readline()
        except (OSError, ValueError):
            return None
        if line == "":            # EOF: nobody is there any more
            return None
        return line.rstrip("\n")


# -------------------------------------------------------------- the run --
class Reporter:
    """Turns pipeline callbacks into terminal output, a log file and a record.

    The record is written from one place (flush) under one lock, by the main
    thread at state changes and by the heartbeat thread in between, so two
    writers can never interleave. Token meter updates arrive many times a
    second and are coalesced into the next flush rather than written each time.
    """

    def __init__(self, job_id, control, quiet=False, interactive=False, out=None):
        self.id = job_id
        self.control = control
        self.quiet = quiet
        self.interactive = interactive
        self.out = out or sys.stdout
        self._lock = threading.Lock()
        self._pending = {}
        self._log = open(str(CJ.log_path(job_id)), "a", encoding="utf-8")
        self._stop_beat = threading.Event()
        self._beat = threading.Thread(target=self._heartbeat, daemon=True)
        self.status = J.RUNNING

    # ----- lifecycle -----
    def start(self):
        self._beat.start()

    def close(self):
        """Stop the heartbeat, write whatever is still pending, close the log."""
        self._stop_beat.set()
        if self._beat.is_alive():
            self._beat.join(timeout=5)
        self.flush()
        try:
            self._log.close()
        except OSError:
            pass

    def _heartbeat(self):
        beats = 0
        while not self._stop_beat.wait(1.0):
            if CJ.stop_requested(self.id) and not self.control.cancelled():
                self.line("\n■ Stop requested — terminating the current step…", "warn")
                self.control.cancel()
            beats += 1
            if beats >= CJ.HEARTBEAT_EVERY or self._pending:
                beats = 0
                self.flush()

    # ----- record -----
    def set(self, **fields):
        with self._lock:
            self._pending.update(fields)

    def flush(self):
        with self._lock:
            fields, self._pending = self._pending, {}
            try:
                CJ.update(self.id, **fields)
            except OSError:
                for k, v in fields.items():    # try again on the next beat,
                    self._pending.setdefault(k, v)   # without undoing newer values

    # ----- callbacks handed to the pipeline -----
    def line(self, text, tag=None):
        tag = tag or classify_line(text.strip())
        with self._lock:
            try:
                self._log.write(text + "\n")
                self._log.flush()
            except (OSError, ValueError):
                pass
            if not self.quiet:
                try:
                    print(paint(text, tag, self.out), file=self.out, flush=True)
                except (OSError, ValueError):
                    pass

    def emit(self, text, tag=None):
        if tag == "tokens":
            try:
                tokens = json.loads(text)
            except (TypeError, ValueError):
                return
            self.set(tokens=tokens)
            return
        if tag == "session":
            self.set(session_id=(text or "").strip())
            return
        if tag == "prmeta":
            try:
                meta = json.loads(text)
            except (TypeError, ValueError):
                return
            src, dest = meta.get("source") or "", meta.get("dest") or ""
            self.set(branches=f"{src} → {dest}" if src and dest else "",
                     author=meta.get("author") or "")
            return
        stripped = (text or "").strip()
        self.line(text, tag)
        if stripped.startswith("◆ Verdict:"):
            m = re.match(r"◆ Verdict:\s*(.*?)\s{2,}Impact:\s*(.*)", stripped)
            raw = m.group(1).strip() if m else stripped.replace("◆ ", "").strip()
            self.set(verdict=raw, impact=m.group(2).strip() if m else "")

    def progress(self, stage, state):
        if state == "active":
            self.set(stage=STAGE_NAMES.get(stage, stage))

    def ask(self, question, choices=None):
        """Put a question to whoever is at the terminal.

        With nobody there (a detached run, or no tty) the answer is "no
        answer", which every caller already treats as the cautious choice: a
        conflict is not resolved and a high-impact merge is not confirmed.
        """
        self.set(status=J.NEEDS_INPUT)
        self.flush()
        try:
            if not self.interactive:
                first = (question or "").strip().splitlines()[0] if question else ""
                self.line(f"⚠ PRISM needs an answer but no terminal is attached; "
                          f"taking the cautious option. ({first[:160]})", "warn")
                return None
            self.line("")
            self.line("? " + question.strip(), "warn")
            if choices:
                for i, (_token, label) in enumerate(choices, 1):
                    self.line(f"   {i}) {label}")
                while True:
                    answer = read_line(f"Choose 1-{len(choices)}: ", self.control)
                    if answer is None:
                        return None
                    answer = answer.strip()
                    if answer.isdigit() and 1 <= int(answer) <= len(choices):
                        return choices[int(answer) - 1][0]
                    for token, label in choices:
                        if answer.lower() in (str(token).lower(), label.lower()):
                            return token
                    self.line(f"   Enter a number from 1 to {len(choices)}.", "warn")
            answer = read_line("Your answer (blank to skip): ", self.control)
            return (answer or "").strip() or None
        finally:
            self.set(status=J.RUNNING)
            self.flush()


def _install_signals(control, reporter):
    """Ctrl-C (or a plain kill) stops the run cleanly; a second Ctrl-C leaves now."""
    def handler(signum, _frame):
        if control.cancelled():
            raise KeyboardInterrupt
        reporter.line("\n■ Stopping — terminating the current step… "
                      "(press Ctrl-C again to leave immediately)", "warn")
        control.cancel()
    for name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass        # not the main thread; the stop file still works


def execute(spec, job_id=None, quiet=False, runner=None, interactive=None):
    """Run one job in this process and return an exit code."""
    job_id = job_id or CJ.new_id()
    # A document job brings its own pipeline; a pull request uses full_pipeline.
    runner = runner or getattr(spec, "runner", None) or full_pipeline
    dup = CJ.find_duplicate(spec)
    if dup is not None and dup.get("id") != job_id:
        print(f"{spec.label} is already being reviewed (job {dup['id']}).", file=sys.stderr)
        return EXIT_USAGE
    record = CJ.create(spec, job_id)
    # The job ID on its chat cards, in `prism jobs` and found by `prism jobs --find`: the
    # job's own id (kept by a retry).
    card_id = record.get("job_id") or job_id
    CJ.prune()
    control = RunControl()
    if interactive is None:
        interactive = (not quiet) and sys.stdin.isatty()
    reporter = Reporter(job_id, control, quiet=quiet, interactive=interactive)
    reporter.start()
    _install_signals(control, reporter)
    reporter.line(f"▸ PRISM job {job_id}"
                  + (f" (job ID {card_id})" if card_id != job_id else "")
                  + f"  ·  {spec.label}  ·  follow it from anywhere with: "
                  f"{PROG} logs {job_id} -f")
    status, fields, code = J.DONE, {}, EXIT_OK
    try:
        summary = runner(emit=reporter.emit, progress=reporter.progress, control=control,
                         ask=reporter.ask, get_note=None, job_id=card_id,
                         **spec.pipeline_kwargs())
        summary = summary or {}
        fields = {"merged": summary.get("merged") is True,
                  "stopped": str(summary.get("stopped") or ""),
                  "tokens": summary.get("tokens") or None,
                  "stage": ""}
        if getattr(spec, "kind", "pr") == "docs":
            fields.update(committed=summary.get("committed") is True,
                          pushed=summary.get("pushed") is True)
            # A document's verdict is one of exactly two values (FR-X-6).
            if summary.get("decision") in DR.DECISIONS:
                fields["verdict"] = summary["decision"]
        if summary.get("scale") in ("normal", "large"):
            fields["scale"] = summary["scale"]
        elif getattr(summary.get("review"), "scale", "") in ("normal", "large"):
            fields["scale"] = summary["review"].scale
        if not fields["tokens"]:
            fields.pop("tokens")
        # What the review found, as data (see findings.py) - read by `prism jobs --json`.
        fields["findings"] = summary.get("findings") if isinstance(summary.get("findings"),
                                                                    list) else []
        shown = {k: v for k, v in summary.items() if k not in INTERNAL_SUMMARY_KEYS}
        reporter.line(f"\n—— finished: {shown} ——", "ok")
    except KeyboardInterrupt:
        status, code = J.STOPPED, EXIT_STOPPED
        reporter.line("\n■ Run stopped.", "warn")
    except Exception as e:  # noqa: BLE001
        if control.cancelled() or isinstance(e, Cancelled):
            status, code = J.STOPPED, EXIT_STOPPED
            reporter.line("\n■ Run stopped.", "warn")
        else:
            status, code = J.ERROR, EXIT_ERROR
            fields = {"error": str(e)[:3000], "stage": ""}
            reporter.line(f"\n⛔ ERROR: {e}", "err")
    reporter.close()
    CJ.finish(job_id, status, **fields)
    CJ.clear_stop(job_id)
    return code


# ------------------------------------------------------------ commands --
def _add_run_options(p):
    p.add_argument("--path", help="project folder (default: the git repository you are in)")
    p.add_argument("--repo", help="CodeCommit repository name, or GitLab project path "
                                  "(group/project) when the remote's does not match the "
                                  "server (default: from the clone's remote)")
    p.add_argument("--clone", help="local clone to use, when not the folder itself")
    p.add_argument("--region", help=f"AWS region (default: {REGION_DEFAULT} or your saved default)")
    p.add_argument("--model", help="reviewer model, e.g. anthropic/claude-sonnet-5-5")
    for flag, dest, what in (("review", "review", "run the review"),
                             ("describe", "describe", "write the findings into the PR description"),
                             ("merge", "merge", "merge on approval"),
                             ("sync", "sync", "sync the destination branch into the source first")):
        p.add_argument(f"--{flag}", dest=dest, action="store_true", default=None, help=what)
        p.add_argument(f"--no-{flag}", dest=dest, action="store_false", help=f"do not {what}")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                   help="review but change nothing in AWS or git")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false", help=argparse.SUPPRESS)
    p.add_argument("-i", "--instructions", help="extra instructions for the reviewer")
    p.add_argument("--instructions-file", help="read the extra instructions from a file")
    p.add_argument("-d", "--detach", action="store_true",
                   help="run in the background and return; survives logging out")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print nothing (the log is still kept)")
    p.add_argument("--job-id", help=argparse.SUPPRESS)


def _add_docs_options(p):
    p.add_argument("files", nargs="*", metavar="file",
                   help="document(s) to review: paths in the repository you are in, or "
                        "files elsewhere on this machine (reviewed as uploaded documents; "
                        "with no repository at all, reviewed without a codebase)")
    p.add_argument("--path", help="project folder (default: the git repository you are in)")
    p.add_argument("--clone", help="the repository's clone, when it is not the folder itself")
    p.add_argument("--branch", help="branch the documents are on (default: the one checked out)")
    p.add_argument("--be", nargs="+", metavar="FILE", help="documents in the backend repository")
    p.add_argument("--be-clone", metavar="DIR", help="the backend clone (default: guessed from the "
                                                      "folder names)")
    p.add_argument("--be-branch", metavar="NAME", help="backend branch (default: checked out)")
    p.add_argument("--fe", nargs="+", metavar="FILE", help="documents in the frontend repository")
    p.add_argument("--fe-clone", metavar="DIR", help="the frontend clone (default: guessed from "
                                                      "the folder names)")
    p.add_argument("--fe-branch", metavar="NAME", help="frontend branch (default: checked out)")
    p.add_argument("--owner", metavar="NAME",
                   help="who owns this review request: the person responsible for the "
                        "document and for answering the review (required)")
    p.add_argument("--dest", action="append", metavar="FILE=PATH",
                   help="commit an uploaded document (a file outside the repository) to PATH "
                        "in the repository, with the review in it; repeat per file. Without "
                        "it the review goes into a local copy only")
    p.add_argument("--persona", metavar="NAME",
                   help="who reviews: ba (Business Analyst, functional; the default), "
                        "architect (technical) or qa (quality)")
    p.add_argument("--requirements", metavar="DIR",
                   help="optional: the folder holding the requirements the documents were "
                        "decomposed from. With it the review checks the decomposition "
                        "satisfies all of them; without it, the documents against the "
                        "codebase alone")
    p.add_argument("--model", help="reviewer model, e.g. anthropic/claude-sonnet-5-5")
    for flag, dest, what in (("commit", "commit", "commit the review comments to the branch"),
                             ("push", "push", "push the commit to the remote")):
        p.add_argument(f"--{flag}", dest=dest, action="store_true", default=None, help=what)
        p.add_argument(f"--no-{flag}", dest=dest, action="store_false", help=f"do not {what}")
    p.add_argument("--dry-run", dest="dry_run", action="store_true", default=None,
                   help="review, but write, commit and push nothing")
    p.add_argument("--no-dry-run", dest="dry_run", action="store_false", help=argparse.SUPPRESS)
    p.add_argument("-i", "--instructions", help="extra instructions for the reviewer")
    p.add_argument("--instructions-file", help="read the extra instructions from a file")
    p.add_argument("-d", "--detach", action="store_true",
                   help="run in the background and return; survives logging out")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="print nothing (the log is still kept)")
    p.add_argument("--job-id", help=argparse.SUPPRESS)
    p.add_argument("--spec-json", help=argparse.SUPPRESS)


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description="PRISM - review and safely merge AWS CodeCommit pull requests "
                               "and GitLab merge requests, and review spec documents.",
        epilog=f"Run '{PROG} <command> --help' for a command's options. Defaults live in "
               f"{CFG.CONFIG_PATH} (see '{PROG} config').")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    r = sub.add_parser("run", help="review a pull request (CodeCommit) or merge request (GitLab)",
                       description="Review CodeCommit pull request <pr_id>, or GitLab merge "
                                   "request <pr_id> (also written !<id>), of the repository in "
                                   "the current folder. The host is read from the clone's "
                                   "origin remote.")
    r.add_argument("pr_id", metavar="pr_id")
    _add_run_options(r)

    d = sub.add_parser(
        "docs", help="review a spec or requirement document",
        description="Review requirement-decomposition documents or specs against the "
                    "codebase and, if you give them, the requirements they came from, as a "
                    "Business Analyst, Architect or QA (--persona), always checking security, "
                    "stability and scalability. The review is written "
                    "into each document as a 'PRISM review comments' section, committed as "
                    "PRISM-<run id>: <message> and pushed - an aid to your own review, not a "
                    "replacement for it.")
    _add_docs_options(d)

    j = sub.add_parser("jobs", help="list jobs started from the command line")
    j.add_argument("-a", "--all", action="store_true", help="include finished jobs")
    j.add_argument("-f", "--find", metavar="JOB_ID",
                   help="only the jobs whose job ID (from a chat card) contains this - "
                        "finished ones included; also matches the job's name")
    j.add_argument("--json", action="store_true", help="machine-readable output")

    lg = sub.add_parser("logs", help="show a job's transcript")
    lg.add_argument("job_id")
    lg.add_argument("-f", "--follow", action="store_true", help="keep following until it ends")
    lg.add_argument("-n", "--lines", type=int, help="only the last N lines")

    s = sub.add_parser("stop", help="stop a running job")
    s.add_argument("job_id", nargs="?")
    s.add_argument("--all", action="store_true", help="stop every running CLI job")

    rt = sub.add_parser("retry", help="run a finished job again with the same settings")
    rt.add_argument("job_id")
    rt.add_argument("-d", "--detach", action="store_true")
    rt.add_argument("-q", "--quiet", action="store_true")
    rt.add_argument("-m", "--model", default=None,
                    help="use this model for the rerun (default: the job's own; "
                         "'default' selects the engine's default model)")
    rt.add_argument("--persona", metavar="NAME", default=None,
                    help="a document review only: rerun it as another reviewer role - "
                         "ba, architect or qa (default: the job's own)")

    a = sub.add_parser("ask", help="ask the reviewer about a finished job (read-only)")
    a.add_argument("job_id")
    a.add_argument("question", nargs="+")

    ch = sub.add_parser(
        "chat", help="ask about a job, in any state (read-only)",
        description="Ask the live chat about a job - running, queued, held, failed or "
                    "finished, pull request or document review. The answer comes from the "
                    "job's details and, for a document review, the documents and "
                    "requirements themselves, and is printed; it never steers the review, "
                    "uses a steering round or changes anything. Each question follows on "
                    "from the earlier ones about the same job (--new starts over). "
                    "Questions that are not about the job are declined.")
    ch.add_argument("job_id")
    ch.add_argument("question", nargs="*")
    ch.add_argument("--new", action="store_true",
                    help="forget the earlier questions about this job and start over")

    rm = sub.add_parser("rm", help="delete a finished job's record and log")
    rm.add_argument("job_id", nargs="*")
    rm.add_argument("--all", action="store_true", help="delete every finished job")

    m = sub.add_parser("models", help="list the reviewer models available")
    m.add_argument("--refresh", action="store_true", help="ask the engine again")

    dr = sub.add_parser("doctor", help="check this machine's setup (git, aws, engine, access)",
                        description="Checks that everything PRISM needs is installed and "
                                    "signed in, and says how to fix what is not. Add --repo, "
                                    "--pr and --model to test the exact job that failed.")
    dr.add_argument("--repo", help="a local clone: checks it can fetch from origin")
    dr.add_argument("--pr", help="a pull request id: checks PRISM can read it")
    dr.add_argument("--region", default=None, help=f"AWS region (default {REGION_DEFAULT})")
    dr.add_argument("-m", "--model", help="check this model id is available")
    dr.add_argument("--quick", action="store_true",
                    help="only look at this machine (no network or engine calls)")
    dr.add_argument("--json", action="store_true", help="machine-readable output")
    dr.add_argument("-o", "--output", metavar="FILE",
                    help="also save the report to FILE (a .zip with the checks, ready to send)")

    ex = sub.add_parser("export", help="save a job's log as a .zip to send for debugging",
                        description="Packs a job's transcript, settings and a setup check into "
                                    "one .zip. Passwords, tokens and webhook URLs are removed "
                                    "first; read log.txt before sending it outside your "
                                    "organisation.")
    ex.add_argument("job_id")
    ex.add_argument("-o", "--output", metavar="FILE",
                    help="where to save it (default: the current folder)")
    ex.add_argument("--no-checks", action="store_true",
                    help="skip the network setup checks (faster, less useful)")

    rp = sub.add_parser("repos", help="show the git clones PRISM finds in a folder")
    rp.add_argument("path", nargs="?", default=".")

    c = sub.add_parser("config", help="show or change saved defaults",
                       description="With no arguments, show the saved defaults. "
                                   "Keys: " + ", ".join(CONFIG_KEYS_HELP))
    c.add_argument("action", nargs="?", choices=("show", "set", "unset", "path"), default="show")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="*")

    mp = sub.add_parser("man", help="install the manual page (then: man prism)")
    mp.add_argument("--install", action="store_true",
                    help="copy it into your man directory (the default action)")
    mp.add_argument("--dir", help="man1 directory to install into "
                                  "(default: ~/.local/share/man/man1)")
    mp.add_argument("--path", action="store_true", help="only print where the page is")
    sub.add_parser("version", help="print the version")
    u = sub.add_parser("update", help="check for (and optionally install) a newer release")
    u.add_argument("--install", action="store_true", help="download and install it")
    sub.add_parser("help", help="show this help")
    return p


CONFIG_KEYS_HELP = list(CFG.CLI_TEXT_KEYS) + list(CFG.CLI_FLAG_KEYS) + [
    "webhook", "docs_webhook", "username", "gitlab_token", "gitlab_hosts"]


def cmd_run(args):
    spec = build_spec(args)
    dup = CJ.find_duplicate(spec)
    if dup is not None and dup.get("id") != args.job_id:
        raise UsageError(f"{spec.label} is already being reviewed (job {dup['id']}).")
    job_id = args.job_id or CJ.new_id()
    if args.detach:
        return detach(["run", str(args.pr_id)] + _forward_run_args(args), job_id, spec.label)
    return execute(spec, job_id, quiet=args.quiet)


def cmd_docs(args):
    if args.spec_json:                       # a detached run: the spec was resolved already
        try:
            spec = with_identity(CJ.spec_from_dict(json.loads(args.spec_json)))
        except (ValueError, TypeError) as e:
            raise UsageError(f"Bad --spec-json: {e}")
    else:
        spec = build_docs_spec(args)
    dup = CJ.find_duplicate(spec)
    if dup is not None and dup.get("id") != args.job_id:
        raise UsageError(f"{spec.label} is already being reviewed (job {dup['id']}).")
    job_id = args.job_id or CJ.new_id()
    if args.detach:
        # The resolved spec travels as-is, so the background process does not depend on
        # its own working directory or on the saved defaults changing meanwhile.
        return detach(["docs", "--spec-json", json.dumps(CJ.spec_to_dict(spec))],
                      job_id, spec.label)
    return execute(spec, job_id, quiet=args.quiet)


def _forward_run_args(args):
    """Re-create the options of a detached run, from the parsed values.

    Resolved values are passed explicitly, so the background process does not
    depend on its own working directory or on the config changing meanwhile.
    """
    spec = build_spec(args)
    out = ["--path", spec.project_dir, "--repo", spec.repo_name, "--region", spec.region]
    if spec.local_repo:
        out += ["--clone", spec.local_repo]
    if spec.model:
        out += ["--model", spec.model]
    for flag, value in (("review", spec.do_review), ("describe", spec.do_update_desc),
                        ("merge", spec.do_merge), ("sync", spec.do_sync)):
        out.append(f"--{flag}" if value else f"--no-{flag}")
    if spec.dry_run:
        out.append("--dry-run")
    if spec.custom_instructions:
        out += ["--instructions", spec.custom_instructions]
    return out


def detach(argv_tail, job_id, label):
    """Start the command in the background and wait for it to show up."""
    CJ.spawn_detached(argv_tail + ["--quiet", "--job-id", job_id])
    deadline = time.time() + 15
    while time.time() < deadline:
        if CJ.load(job_id) is not None:
            break
        time.sleep(0.2)
    else:
        print(f"Started {label} in the background, but it has not registered yet. "
              f"Check with: {PROG} jobs", file=sys.stderr)
        return EXIT_OK
    print(f"Started {label} in the background as job {job_id}.")
    print(f"  follow it:  {PROG} logs {job_id} -f")
    print(f"  stop it:    {PROG} stop {job_id}")
    return EXIT_OK


def _row_status(record):
    status = CJ.status_of(record)
    return {J.NEEDS_INPUT: "needs input"}.get(status, status)


def cmd_jobs(args):
    records = CJ.list_jobs()
    find = getattr(args, "find", None)
    if find is not None:
        # A search looks at every job, finished ones included: that is what it is for.
        if not jobid.normalise_query(find):
            raise UsageError("Give the job ID (or part of it) to find.")
        records = [r for r in records if CJ.matches(r, find)]
    elif not args.all:
        records = [r for r in records if CJ.is_active(r)]
    if args.json:
        for r in records:
            r["status"] = CJ.status_of(r)
            r["job_id"] = CJ.job_id_of(r)
            # Always present, so a reader never has to test for them: a record written
            # before they existed (or by a job that has not delivered a review) has none.
            if not isinstance(r.get("findings"), list):
                r["findings"] = []
            r.setdefault("reviewer", "")
            r.setdefault("scale", None)
            if (r.get("spec") or {}).get("kind") == "docs":
                r.setdefault("spec", {}).setdefault("owner", "")
        print(json.dumps(records, indent=1))
        return EXIT_OK
    if not records and find is not None:
        print(f"No job matches {find!r}.")
        return EXIT_OK
    if not records:
        print("No running jobs." if not args.all else "No jobs yet.")
        if not args.all and any(True for _ in CJ.list_jobs()):
            print(f"(see finished ones with: {PROG} jobs --all)")
        return EXIT_OK
    now = time.time()
    has_docs = any((r.get("spec") or {}).get("kind") == "docs" for r in records)
    rows = [("ID", "STATUS", "TARGET" if has_docs else "PULL REQUEST", "STAGE / RESULT", "AGE")]
    for r in records:
        active = CJ.is_active(r)
        verdict = r.get("verdict") or ""
        if (r.get("spec") or {}).get("kind") == "docs" and verdict in DR.DECISIONS:
            verdict = DR.decision_words(verdict)
        if r.get("scale") == "large" and verdict:
            verdict += " (large change)"
        detail = r.get("stage") if active else (verdict or r.get("error") or "")
        if not active and r.get("merged"):
            detail = (detail + "  ·  merged").strip(" ·")
        elif not active and r.get("pushed"):
            detail = (detail + "  ·  pushed").strip(" ·")
        elif not active and r.get("committed"):
            detail = (detail + "  ·  committed").strip(" ·")
        # Since v7.2 a job's id is its job ID; an older job run again shows both.
        card_id = CJ.job_id_of(r)
        shown_id = r["id"] if card_id in ("", r["id"]) else f"{r['id']} ({card_id})"
        rows.append((shown_id, _row_status(r), CJ.label(r), (detail or "")[:48],
                     human_age(now - (r.get("started") or now))))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for n, row in enumerate(rows):
        text = "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip()
        print(paint(text, "dim") if n == 0 else text)
    return EXIT_OK


def _need_record(job_id):
    record = CJ.load(job_id)
    if record is None:
        raise UsageError(f"No job {job_id}. See: {PROG} jobs --all")
    return record


def cmd_logs(args):
    _need_record(args.job_id)
    if not args.follow:
        text = CJ.read_log(args.job_id, tail=args.lines)
        if text:
            print(text)
        return EXIT_OK
    offset = 0
    if args.lines:
        tail = CJ.read_log(args.job_id, tail=args.lines)
        if tail:
            print(tail)
        offset = len(CJ.log_path(args.job_id).read_bytes()) if CJ.log_path(args.job_id).exists() else 0
    try:
        while True:
            try:
                with open(str(CJ.log_path(args.job_id)), "rb") as fh:
                    fh.seek(offset)
                    chunk = fh.read()
                    offset += len(chunk)
            except OSError:
                chunk = b""
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
            else:
                record = CJ.load(args.job_id)
                if record is None or CJ.is_finished(record):
                    break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    return EXIT_OK


def cmd_stop(args):
    if args.all:
        targets = [r["id"] for r in CJ.active_jobs()]
        if not targets:
            print("Nothing is running.")
            return EXIT_OK
    elif args.job_id:
        targets = [args.job_id]
    else:
        raise UsageError(f"Say which job: {PROG} stop <id>   (or --all)")
    code = EXIT_OK
    for job_id in targets:
        _need_record(job_id)
        if CJ.request_stop(job_id):
            print(f"Stopping {job_id}…")
        else:
            print(f"{job_id} is not running.")
            code = EXIT_USAGE
    return code


def cmd_retry(args):
    record = _need_record(args.job_id)
    if CJ.is_active(record):
        raise UsageError(f"{args.job_id} is still running. Stop it first: {PROG} stop {args.job_id}")
    try:
        spec = CJ.spec_from_dict(record.get("spec"))
    except ValueError as e:
        raise UsageError(f"Cannot retry {args.job_id}: {e}")
    # The webhook and the reviewer's name are machine-wide settings, read fresh as the
    # desktop does.
    if getattr(spec, "kind", "pr") != "docs":
        spec = J.JobSpec(**dict(CJ.spec_to_dict(spec), webhook_url=CFG.get_webhook_url() or None,
                                origin="cli"))
    spec = with_identity(spec)
    if args.model:
        spec = dataclasses.replace(spec, model=None if args.model == "default" else args.model)
    persona = getattr(args, "persona", None)
    if persona:
        if getattr(spec, "kind", "pr") != "docs":
            raise UsageError("--persona is for a document review; a pull request has no "
                             "reviewer role.")
        try:
            spec = dataclasses.replace(spec, persona=DR.normalise_persona(persona))
        except ValueError as e:
            raise UsageError(str(e))
    dup = CJ.find_duplicate(spec)
    if dup is not None and dup.get("id") != args.job_id:
        raise UsageError(f"{spec.label} is already being reviewed (job {dup['id']}).")
    if args.detach:
        return detach(["retry", args.job_id] + (["--model", args.model] if args.model else [])
                      + (["--persona", persona] if persona else []), args.job_id, spec.label)
    return execute(spec, args.job_id, quiet=args.quiet)


def cmd_ask(args):
    record = _need_record(args.job_id)
    if CJ.is_active(record):
        raise UsageError("That job is still running; ask once it has finished.")
    spec = CJ.spec_from_dict(record.get("spec"))
    control = RunControl()
    reporter = Reporter(args.job_id, control, quiet=False, interactive=False)
    _install_signals(control, reporter)
    question = " ".join(args.question)
    reporter.line(f"\n› {question}", "dim")
    outcome = ("merged" if record.get("merged") is True
               else f"not merged ({record.get('stopped') or record.get('status')})")

    def emit(text, tag=None):
        if tag == "session":
            reporter.set(session_id=(text or "").strip())
        elif tag not in ("tokens", "prmeta"):
            reporter.line(text, tag)

    code = EXIT_OK
    try:
        if getattr(spec, "kind", "pr") == "docs":
            reply, sid = DR.run_followup(
                question, spec.project_dir, model=spec.model,
                session_id=record.get("session_id") or None, emit=emit, control=control)
        else:
            reply, sid = run_followup(
                question, spec.project_dir, spec.repo_name, spec.pr_id,
                local_repo=spec.local_repo, region=spec.region, model=spec.model,
                session_id=record.get("session_id") or None, outcome=outcome,
                emit=emit, control=control,
                **({"host": spec.host, "host_url": spec.host_url}
                   if getattr(spec, "is_gitlab", False) else {}))
        reason = followup_declined(reply) if getattr(spec, "kind", "pr") != "docs" else None
        reporter.line(f"⛔ Not actioned — {reason}" if reason else "✔ Follow-up answered.",
                      "warn" if reason else "ok")
        if sid:
            reporter.set(session_id=sid)
    except Cancelled:
        reporter.line("■ Follow-up stopped.", "warn")
        code = EXIT_STOPPED
    except Exception as e:  # noqa: BLE001
        reporter.line(f"⚠ Follow-up failed: {str(e)[:500]}", "err")
        code = EXIT_ERROR
    finally:
        reporter.close()     # the job is finished, so this is the only writer
    return code


def record_details(record, log_text=None):
    """A CLI job's details for the live chat (chat.snapshot), from its record and log."""
    spec_data = record.get("spec") or {}
    docs = spec_data.get("kind") == "docs"
    try:
        spec = CJ.spec_from_dict(spec_data)
    except ValueError:
        spec = None
    verdict = record.get("verdict") or ""
    if docs and verdict in DR.DECISIONS:
        verdict = DR.verdict_label(verdict)
    if spec is None:
        settings = target = host = ""
    elif docs:
        settings = spec.summary_bits()
        target = "; ".join(
            (f"{t.label}: {t.repo_name} @ {t.branch}" if t.has_repo else t.label) + " — "
            + ", ".join(list(t.files) + [f"{u.name} (uploaded)" for u in t.uploads])
            for t in spec.targets)
        host = ""
    else:
        settings, target = spec.summary_bits(), spec.label
        host = ("GitLab " + spec.host_url) if spec.is_gitlab else f"CodeCommit ({spec.region})"
    stage = record.get("stage") or ""
    if log_text is None:
        log_text = CJ.read_log(record.get("id"), tail=CHAT.OUTPUT_TAIL_LINES) or ""
    return {
        "label": CJ.label(record), "kind": "docs" if docs else "pr",
        "job_id": CJ.job_id_of(record) or None, "status": CJ.status_of(record), "stages": {"current stage": stage} if stage else {},
        "settings": settings, "host": host or None, "target": target,
        "branches": record.get("branches") or None,
        "owner": spec_data.get("owner") or None, "reviewer": record.get("reviewer") or None,
        "verdict": verdict, "impact": record.get("impact") or None,
        "scale": record.get("scale") or None,
        "merged": record.get("merged") if record.get("merged") is not None else None,
        "stopped": record.get("stopped") or None, "error": record.get("error") or None,
        "findings": record.get("findings") if isinstance(record.get("findings"), list)
        else None,
        "output": (log_text or "").splitlines(),
    }


def chat_reply(record, question, control=None, history=None):
    """(reply, how) for a question about a CLI job. The reviewer's session is continued
    only once the job is finished; a running job is answered from its record.

    `history` is the conversation so far ([(role, text)], oldest first); None reads the
    one `prism chat` keeps for the job. The job's documents go with the question."""
    spec = CJ.spec_from_dict(record.get("spec"))
    details_data = record_details(record)
    details = CHAT.snapshot(details_data)
    session = record.get("session_id") if CJ.is_finished(record) else None
    if history is None:
        history = CJ.read_chat(record.get("id"))
    sources = CHS.for_job(spec, details_data.get("findings"),
                          details_data.get("job_id") or record.get("id") or "")
    return CHAT.ask(question, details, CJ.label(record), getattr(spec, "kind", "pr"),
                    spec.project_dir, model=spec.model, session_id=session or None,
                    control=control, history=history, sources=sources)


def cmd_chat(args):
    record = _need_record(args.job_id)
    try:
        CJ.spec_from_dict(record.get("spec"))
    except ValueError as e:
        raise UsageError(f"Cannot chat about {args.job_id}: {e}")
    if args.new:
        CJ.clear_chat(record.get("id"))
    question = " ".join(args.question).strip()
    if not question:
        if args.new:
            print(f"Started a new conversation about {args.job_id}.")
            return EXIT_OK
        raise UsageError(f"Ask a question: {PROG} chat {args.job_id} <question>")
    control = RunControl()
    try:
        reply, _how = chat_reply(record, question, control=control)
    except Cancelled:
        return EXIT_STOPPED
    except Exception as e:  # noqa: BLE001
        print(f"Could not answer: {str(e)[:500]}", file=sys.stderr)
        return EXIT_ERROR
    # Remembered, so the next `prism chat` about this job follows on from this one.
    CJ.append_chat(record.get("id"), ("you", question), ("chat", reply))
    print(reply)
    return EXIT_OK


def cmd_rm(args):
    targets = args.job_id
    if args.all:
        targets = [r["id"] for r in CJ.list_jobs() if CJ.is_finished(r)]
    if not targets:
        raise UsageError(f"Say which job: {PROG} rm <id>   (or --all)")
    code = EXIT_OK
    for job_id in targets:
        if CJ.load(job_id) is None:
            print(f"No job {job_id}.")
            code = EXIT_USAGE
        elif CJ.remove(job_id):
            print(f"Removed {job_id}.")
        else:
            print(f"{job_id} is still running; stop it first.")
            code = EXIT_USAGE
    return code


def cmd_models(args):
    models = list_available_models(force=args.refresh)
    if not models:
        print("Could not list models (is the reviewer engine installed?).", file=sys.stderr)
        return EXIT_ERROR
    print("\n".join(models))
    return EXIT_OK


def cmd_doctor(args):
    checks = DG.run_checks(region=args.region, repo=args.repo, pr=args.pr,
                           model=args.model, deep=not args.quick)
    if args.json:
        print(json.dumps([dataclasses.asdict(c) for c in checks], indent=2))
    else:
        print(DG.render_text(checks, paint=paint))
    if args.output:
        path = DG.export_bundle(args.output, "doctor", "", checks=checks,
                                note="This bundle holds only the setup checks (no job).")
        print(f"\nSaved to {path}")
    return EXIT_ERROR if any(c.status == DG.FAIL for c in checks) else EXIT_OK


def cmd_export(args):
    record = _need_record(args.job_id)
    log = CJ.read_log(args.job_id)
    if not log:
        raise UsageError(f"Job {args.job_id} has no log to export.")
    checks = DG.run_checks(deep=not args.no_checks, region=(record.get("spec") or {}).get("region"),
                           repo=(record.get("spec") or {}).get("local_repo"),
                           model=(record.get("spec") or {}).get("model"))
    dest = args.output or DG.default_name(args.job_id)
    path = DG.export_bundle(dest, args.job_id, log, record=record, checks=checks)
    print(f"Saved to {Path(path).resolve()}")
    print("Credentials were removed; read log.txt before sending it outside your organisation.")
    return EXIT_OK


def cmd_repos(args):
    base = Path(native_path(args.path))
    found = detect_local_repos(str(base))
    if not found:
        print(f"No git clones in {base.resolve()}.")
        return EXIT_OK
    for name, path in found:
        print(f"{name:<30} {path}")
    return EXIT_OK


def _parse_bool(text):
    value = (text or "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise UsageError(f"Expected yes or no, got {text!r}.")


def _mask(url):
    return url if len(url) < 30 else url[:24] + "…" + url[-4:]


def cmd_config(args):
    if args.action == "path":
        print(CFG.CONFIG_PATH)
        return EXIT_OK
    if args.action == "show":
        cfg = CFG.get_cli()
        print(f"# {CFG.CONFIG_PATH}")
        for key in CFG.CLI_TEXT_KEYS:
            print(f"{key:<13} {cfg[key] or '-'}")
        for key in CFG.CLI_FLAG_KEYS:
            print(f"{key:<13} {'yes' if cfg[key] else 'no'}")
        hook = CFG.get_webhook_url()
        print(f"{'webhook':<13} {_mask(hook) if hook else '-'}")
        docs_hook = CFG.get_docs_webhook_url()
        print(f"{'docs_webhook':<13} {_mask(docs_hook) if docs_hook else '-'}")
        print(f"{'username':<13} {CFG.get_username() or '-'}")
        saved = CFG.load().get("gitlab_token")
        token = saved.strip() if isinstance(saved, str) else ""
        print(f"{'gitlab_token':<13} {GL.mask(token) if token else '-'}")
        hosts = CFG.load().get("gitlab_hosts")
        hosts = [hosts] if isinstance(hosts, str) else (hosts if isinstance(hosts, list) else [])
        print(f"{'gitlab_hosts':<13} {', '.join(str(h) for h in hosts) or '-'}")
        return EXIT_OK
    key = (args.key or "").replace("-", "_")
    if key not in CONFIG_KEYS_HELP:
        raise UsageError(f"Unknown setting {args.key!r}. Choose from: {', '.join(CONFIG_KEYS_HELP)}")
    if args.action == "unset":
        if key == "webhook":
            CFG.set_webhook_url("")
        elif key == "docs_webhook":
            CFG.set_docs_webhook_url("")
        elif key == "username":
            CFG.set_username("")
        elif key == "gitlab_token":
            GL.set_token("")
        elif key == "gitlab_hosts":
            cfg = CFG.load()
            cfg.pop("gitlab_hosts", None)
            CFG.save(cfg)
        else:
            CFG.unset_cli(key)
        print(f"{key} unset.")
        return EXIT_OK
    if not args.value:
        raise UsageError(f"Give a value: {PROG} config set {key} <value>")
    text = " ".join(args.value)
    if key == "webhook":
        CFG.set_webhook_url(text)
    elif key == "docs_webhook":
        CFG.set_docs_webhook_url(text)
    elif key == "username":
        name = CFG.clean_username(text)
        if not name:
            raise UsageError("The name is empty once markup and control characters are removed.")
        CFG.set_username(name)
        text = name
    elif key == "gitlab_token":
        GL.set_token(text)
        text = GL.mask(text.strip())         # never echoed in full
    elif key == "gitlab_hosts":
        cfg = CFG.load()
        cfg["gitlab_hosts"] = [h.strip() for h in re.split(r"[,\s]+", text) if h.strip()]
        CFG.save(cfg)
    elif key in CFG.CLI_FLAG_KEYS:
        CFG.set_cli(**{key: _parse_bool(text)})
    elif key == "project_dir":
        folder = Path(native_path(text)).resolve()
        if not folder.is_dir():
            raise UsageError(f"{folder} is not a folder.")
        CFG.set_cli(project_dir=str(folder))
    elif key == "persona":
        try:
            CFG.set_cli(persona=DR.normalise_persona(text))     # saved as the canonical key
        except ValueError as e:
            raise UsageError(str(e))
    else:
        CFG.set_cli(**{key: text})
    print(f"{key} saved to {CFG.CONFIG_PATH}")
    return EXIT_OK


MAN_PAGE = TOOL_DIR / "man" / "prism.1"


def cmd_man(args):
    if not MAN_PAGE.is_file():
        print(f"The manual page is missing from this install ({MAN_PAGE}).", file=sys.stderr)
        return EXIT_ERROR
    if args.path:
        print(MAN_PAGE)
        return EXIT_OK
    target = Path(native_path(args.dir)) if args.dir else (
        Path.home() / ".local" / "share" / "man" / "man1")
    try:
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(MAN_PAGE), str(target / "prism.1"))
    except OSError as e:
        print(f"Could not install into {target}: {e.strerror or e}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Installed {target / 'prism.1'}")
    if not args.dir:
        print("Try:  man prism    (if it is not found, add ~/.local/share/man to MANPATH)")
    return EXIT_OK


def cmd_update(args):
    import updater as U
    try:
        release = U.check()
    except U.UpdateError as e:
        print(f"Could not check for updates: {e}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as e:
        print(f"Could not reach GitHub: {e}", file=sys.stderr)
        return EXIT_ERROR
    if release is None:
        print(f"PRISM {__version__} is the latest version.")
        return EXIT_OK
    print(f"PRISM {release.version} is available (you have {__version__}).")
    if release.notes:
        print("\n" + release.notes.strip() + "\n")
    if not args.install:
        print(f"Install it with: {PROG} update --install   ({release.page_url})")
        return EXIT_OK
    ok, reason = U.can_self_update()
    if not ok:
        print(reason, file=sys.stderr)
        return EXIT_ERROR
    try:
        U.apply_update(release, progress=None)
    except U.UpdateError as e:
        print(f"Update failed: {e}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Updated to {release.version}. It takes effect the next time PRISM starts.")
    return EXIT_OK


HANDLERS = {"run": cmd_run, "jobs": cmd_jobs, "logs": cmd_logs, "stop": cmd_stop,
            "docs": cmd_docs, "retry": cmd_retry, "ask": cmd_ask, "chat": cmd_chat, "rm": cmd_rm,
            "models": cmd_models,
            "doctor": cmd_doctor, "export": cmd_export,
            "repos": cmd_repos, "config": cmd_config, "man": cmd_man,
            "update": cmd_update}


def ensure_username():
    """Ask once, at a terminal, whose name reviews are to carry.

    The desktop app asks the same question the first time it starts. Here it is
    asked only when a person is actually there to answer: a background job, a
    script or a pipe is never held up by it (such a run is signed with the
    machine login instead - see config.effective_username). Enter accepts the
    suggestion; an interrupted or empty answer saves nothing, so it is asked
    again next time.
    """
    if CFG.get_username():
        return
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        suggestion = CFG.system_username()
        print("PRISM signs every review it produces - the PR description, the chat card, "
              "the document comments - with your name, so a review can be traced to who "
              "ran it. Asked once; change it any time with: "
              f"{PROG} config set username <name>")
        shown = f" [{suggestion}]" if suggestion else ""
        name = CFG.clean_username(input(f"Your name or username{shown}: ")) or suggestion
        if name:
            CFG.set_username(name)
            print(f"Saved: reviews will be signed {name}.\n")
    except (EOFError, KeyboardInterrupt, OSError):
        print()


# Commands that name a job: each takes its id or the job ID on its chat cards.
_JOB_COMMANDS = ("logs", "stop", "retry", "ask", "chat", "rm", "export")


def resolve_job_id(given):
    """A job id for what was typed: a job id as it is, or the id of the job with this job
    ID (as shown on its chat card, also pasted as "Job ID …" or "PRISM-…"). Unknown values
    come back as typed, so the command reports them as it always has."""
    if not given or CJ.load(given) is not None:
        return given
    record = CJ.find_by_job_id(given)
    return record["id"] if record is not None else given


def _resolve_job_ids(args):
    if args.command not in _JOB_COMMANDS:
        return
    ids = getattr(args, "job_id", None)
    if isinstance(ids, list):
        args.job_id = [resolve_job_id(i) for i in ids]
    elif ids:
        args.job_id = resolve_job_id(ids)


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "help"):
        parser.print_help()
        return EXIT_OK
    if args.command == "version":
        print(f"PRISM {__version__}")
        return EXIT_OK
    try:
        _resolve_job_ids(args)
        if args.command in ("run", "docs", "retry") and not getattr(args, "quiet", False):
            ensure_username()
        return HANDLERS[args.command](args)
    except UsageError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main())
