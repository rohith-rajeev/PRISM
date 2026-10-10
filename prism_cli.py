#!/usr/bin/env python3
"""PRISM on the command line.

    prism run 214                 review PR 214 of the repository in this folder
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
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as CFG  # noqa: E402
import cli_jobs as CJ  # noqa: E402
import jobs as J  # noqa: E402
from orchestrator import (  # noqa: E402
    REGION_DEFAULT, Cancelled, RunControl, detect_local_repos, followup_declined,
    full_pipeline, list_available_models, normalise_verdict, run_followup,
    STAGE_REVIEW, STAGE_DESCRIBE, STAGE_MERGE_CHECK, STAGE_SYNC, STAGE_MERGE,
)
from version import __version__  # noqa: E402

PROG = "prism"

# Words the first argument can be. The desktop binary uses this to decide
# whether it was started as a command or as the application.
COMMANDS = ("run", "jobs", "logs", "stop", "retry", "ask", "rm", "models", "repos",
            "config", "version", "update", "help")

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_STOPPED = 0, 1, 2, 130

STAGE_NAMES = {STAGE_REVIEW: "Review", STAGE_DESCRIBE: "Describe",
               STAGE_MERGE_CHECK: "Merge check", STAGE_SYNC: "Sync", STAGE_MERGE: "Merge"}

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
    start = Path(path or cfg.get("project_dir") or cwd or os.getcwd()).expanduser()
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
    local = str(Path(clone).expanduser().resolve()) if clone else None

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


def build_spec(args, cfg=None, cwd=None):
    cfg = cfg if cfg is not None else CFG.get_cli()
    project_dir, repo, local, region_hint = resolve_target(
        args.path, args.repo, args.clone, cfg=cfg, cwd=cwd)
    pr = str(args.pr_id).strip().lstrip("#")
    if not pr.isdigit():
        raise UsageError("The pull request id must be a number, e.g. prism run 214")

    def flag(cli_value, key):
        return cfg[key] if cli_value is None else cli_value

    do_review = flag(args.review, "review")
    do_describe = flag(args.describe, "describe") and do_review
    instructions = args.instructions if args.instructions is not None else cfg["instructions"]
    if args.instructions_file:
        try:
            instructions = Path(args.instructions_file).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            raise UsageError(f"Cannot read {args.instructions_file}: {e.strerror or e}")
    return J.JobSpec(
        project_dir=project_dir, repo_name=repo, pr_id=pr, local_repo=local,
        region=args.region or cfg["region"] or region_hint or REGION_DEFAULT,
        model=args.model or cfg["model"] or None,
        do_review=do_review, do_update_desc=do_describe,
        do_merge=flag(args.merge, "merge"), do_sync=flag(args.sync, "sync"),
        dry_run=flag(args.dry_run, "dry_run"),
        webhook_url=CFG.get_webhook_url() or None,
        custom_instructions=(instructions or "").strip(), origin="cli")


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


def execute(spec, job_id=None, quiet=False, runner=full_pipeline, interactive=None):
    """Run one job in this process and return an exit code."""
    job_id = job_id or CJ.new_id()
    dup = CJ.find_active(spec.repo_name, spec.pr_id)
    if dup is not None and dup.get("id") != job_id:
        print(f"{spec.label} is already being reviewed (job {dup['id']}).", file=sys.stderr)
        return EXIT_USAGE
    CJ.create(spec, job_id)
    CJ.prune()
    control = RunControl()
    if interactive is None:
        interactive = (not quiet) and sys.stdin.isatty()
    reporter = Reporter(job_id, control, quiet=quiet, interactive=interactive)
    reporter.start()
    _install_signals(control, reporter)
    reporter.line(f"▸ PRISM job {job_id}  ·  {spec.label}  ·  follow it from anywhere with: "
                  f"{PROG} logs {job_id} -f")
    status, fields, code = J.DONE, {}, EXIT_OK
    try:
        summary = runner(emit=reporter.emit, progress=reporter.progress, control=control,
                         ask=reporter.ask, get_note=None, **spec.pipeline_kwargs())
        summary = summary or {}
        fields = {"merged": summary.get("merged") is True,
                  "stopped": str(summary.get("stopped") or ""),
                  "tokens": summary.get("tokens") or None,
                  "stage": ""}
        if not fields["tokens"]:
            fields.pop("tokens")
        shown = {k: v for k, v in summary.items() if k not in ("review", "tokens")}
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
    p.add_argument("--repo", help="CodeCommit repository name (default: from the clone's remote)")
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


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROG, description="PRISM - review and safely merge AWS CodeCommit pull requests.",
        epilog=f"Run '{PROG} <command> --help' for a command's options. Defaults live in "
               f"{CFG.CONFIG_PATH} (see '{PROG} config').")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    r = sub.add_parser("run", help="review a pull request",
                       description="Review PR <pr_id> of the repository in the current folder.")
    r.add_argument("pr_id", metavar="pr_id")
    _add_run_options(r)

    j = sub.add_parser("jobs", help="list jobs started from the command line")
    j.add_argument("-a", "--all", action="store_true", help="include finished jobs")
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

    a = sub.add_parser("ask", help="ask the reviewer about a finished job (read-only)")
    a.add_argument("job_id")
    a.add_argument("question", nargs="+")

    rm = sub.add_parser("rm", help="delete a finished job's record and log")
    rm.add_argument("job_id", nargs="*")
    rm.add_argument("--all", action="store_true", help="delete every finished job")

    m = sub.add_parser("models", help="list the reviewer models available")
    m.add_argument("--refresh", action="store_true", help="ask the engine again")

    rp = sub.add_parser("repos", help="show the git clones PRISM finds in a folder")
    rp.add_argument("path", nargs="?", default=".")

    c = sub.add_parser("config", help="show or change saved defaults",
                       description="With no arguments, show the saved defaults. "
                                   "Keys: " + ", ".join(CONFIG_KEYS_HELP))
    c.add_argument("action", nargs="?", choices=("show", "set", "unset", "path"), default="show")
    c.add_argument("key", nargs="?")
    c.add_argument("value", nargs="*")

    sub.add_parser("version", help="print the version")
    u = sub.add_parser("update", help="check for (and optionally install) a newer release")
    u.add_argument("--install", action="store_true", help="download and install it")
    sub.add_parser("help", help="show this help")
    return p


CONFIG_KEYS_HELP = list(CFG.CLI_TEXT_KEYS) + list(CFG.CLI_FLAG_KEYS) + ["webhook"]


def cmd_run(args):
    spec = build_spec(args)
    dup = CJ.find_active(spec.repo_name, spec.pr_id)
    if dup is not None and dup.get("id") != args.job_id:
        raise UsageError(f"{spec.label} is already being reviewed (job {dup['id']}).")
    job_id = args.job_id or CJ.new_id()
    if args.detach:
        return detach(["run", str(args.pr_id)] + _forward_run_args(args), job_id, spec.label)
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
    if not args.all:
        records = [r for r in records if CJ.is_active(r)]
    if args.json:
        for r in records:
            r["status"] = CJ.status_of(r)
        print(json.dumps(records, indent=1))
        return EXIT_OK
    if not records:
        print("No running jobs." if not args.all else "No jobs yet.")
        if not args.all and any(True for _ in CJ.list_jobs()):
            print(f"(see finished ones with: {PROG} jobs --all)")
        return EXIT_OK
    now = time.time()
    rows = [("ID", "STATUS", "PULL REQUEST", "STAGE / RESULT", "AGE")]
    for r in records:
        active = CJ.is_active(r)
        detail = r.get("stage") if active else (r.get("verdict") or r.get("error") or "")
        if not active and r.get("merged"):
            detail = (detail + "  ·  merged").strip(" ·")
        rows.append((r["id"], _row_status(r), CJ.label(r), (detail or "")[:48],
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
    # The webhook is a machine-wide setting, read fresh as the desktop does.
    spec = J.JobSpec(**dict(CJ.spec_to_dict(spec), webhook_url=CFG.get_webhook_url() or None,
                            origin="cli"))
    dup = CJ.find_active(spec.repo_name, spec.pr_id)
    if dup is not None and dup.get("id") != args.job_id:
        raise UsageError(f"{spec.label} is already being reviewed (job {dup['id']}).")
    if args.detach:
        return detach(["retry", args.job_id], args.job_id, spec.label)
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
        reply, sid = run_followup(
            question, spec.project_dir, spec.repo_name, spec.pr_id,
            local_repo=spec.local_repo, region=spec.region, model=spec.model,
            session_id=record.get("session_id") or None, outcome=outcome,
            emit=emit, control=control)
        reason = followup_declined(reply)
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


def cmd_repos(args):
    base = Path(args.path).expanduser()
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
        return EXIT_OK
    key = (args.key or "").replace("-", "_")
    if key not in CONFIG_KEYS_HELP:
        raise UsageError(f"Unknown setting {args.key!r}. Choose from: {', '.join(CONFIG_KEYS_HELP)}")
    if args.action == "unset":
        if key == "webhook":
            CFG.set_webhook_url("")
        else:
            CFG.unset_cli(key)
        print(f"{key} unset.")
        return EXIT_OK
    if not args.value:
        raise UsageError(f"Give a value: {PROG} config set {key} <value>")
    text = " ".join(args.value)
    if key == "webhook":
        CFG.set_webhook_url(text)
    elif key in CFG.CLI_FLAG_KEYS:
        CFG.set_cli(**{key: _parse_bool(text)})
    elif key == "project_dir":
        folder = Path(text).expanduser().resolve()
        if not folder.is_dir():
            raise UsageError(f"{folder} is not a folder.")
        CFG.set_cli(project_dir=str(folder))
    else:
        CFG.set_cli(**{key: text})
    print(f"{key} saved to {CFG.CONFIG_PATH}")
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
            "retry": cmd_retry, "ask": cmd_ask, "rm": cmd_rm, "models": cmd_models,
            "repos": cmd_repos, "config": cmd_config, "update": cmd_update}


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
        return HANDLERS[args.command](args)
    except UsageError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return EXIT_USAGE
    except KeyboardInterrupt:
        return EXIT_STOPPED


if __name__ == "__main__":
    sys.exit(main())
