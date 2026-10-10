"""Registry of jobs started from the command line.

A CLI job is a separate process, possibly started over ssh, so the desktop
application cannot hold it in memory the way it holds its own jobs. Instead
each CLI job keeps one small record under ``~/.prism/cli-jobs/`` and the
desktop reads those records:

    <id>.json   the job's state; written only by the process running the job
    <id>.log    its transcript, so ``prism logs`` and a later session can read it
    <id>.stop   created by anyone who wants the job stopped (desktop or CLI)

Nobody has to be signalled: the running job notices ``<id>.stop`` within a
second and cancels itself through the same RunControl the desktop uses, which
kills the reviewer and git children it started.

Liveness is a heartbeat, not a pid. A pid says nothing across machines sharing
a home directory and means something different on every OS; a record whose
heartbeat has stopped is simply reported as "interrupted", whatever happened to
the process.

Stdlib only and free of Tkinter, like jobs.py, so it is testable headlessly.
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import config
import jobs as J

INTERRUPTED = "interrupted"

# How often a running job proves it is alive, and how long silence is tolerated
# before the record is called interrupted. Several beats must be missed, so a
# machine that is merely busy is not mistaken for a dead job.
HEARTBEAT_EVERY = 3
STALE_AFTER = 20

# Finished records kept for `prism jobs` and the desktop list. Older ones are
# deleted when a new job starts, so the directory cannot grow forever.
KEEP_FINISHED = 40

# Fields of a JobSpec that survive a round trip through a record. Anything else
# in a record's "spec" is ignored, so an older or newer PRISM can read it.
_SPEC_FIELDS = ("project_dir", "repo_name", "pr_id", "local_repo", "region", "model",
                "do_review", "do_update_desc", "do_merge", "do_sync", "dry_run",
                "webhook_url", "custom_instructions")


def jobs_dir(create=False):
    path = config.CONFIG_DIR / "cli-jobs"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _file(job_id, ext):
    return jobs_dir() / f"{job_id}.{ext}"


def log_path(job_id):
    return _file(job_id, "log")


def new_id():
    return secrets.token_hex(3)


def valid_id(job_id):
    """Ids end up in file names, so only the ones we mint are accepted."""
    return (isinstance(job_id, str) and 1 <= len(job_id) <= 32
            and all(c in "0123456789abcdef" for c in job_id))


# ---------------------------------------------------------------- specs --
def spec_to_dict(spec):
    data = asdict(spec)
    return {k: data.get(k) for k in _SPEC_FIELDS}


def spec_from_dict(data):
    """A JobSpec from a record, or ValueError if it cannot describe a job."""
    data = data if isinstance(data, dict) else {}
    kwargs = {k: data[k] for k in _SPEC_FIELDS if k in data}
    for required in ("project_dir", "repo_name", "pr_id"):
        if not str(kwargs.get(required) or "").strip():
            raise ValueError(f"saved job has no {required}")
    kwargs["pr_id"] = str(kwargs["pr_id"])
    return J.JobSpec(origin="cli", **kwargs)


# -------------------------------------------------------------- records --
def _write(job_id, record):
    """Replace a record atomically, so a reader never sees half a file."""
    jobs_dir(create=True)
    target = _file(job_id, "json")
    tmp = target.with_name(f".{job_id}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(target))


def load(job_id):
    """The record for `job_id`, or None when absent or unreadable."""
    if not valid_id(job_id):
        return None
    try:
        record = json.loads(_file(job_id, "json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def create(spec, job_id=None, cwd=None):
    """Write a fresh record for a job this process is about to run."""
    job_id = job_id or new_id()
    now = time.time()
    record = {
        "id": job_id, "pid": os.getpid(), "host": socket.gethostname(),
        "cwd": str(cwd or os.getcwd()), "status": J.RUNNING, "stage": "",
        "started": now, "heartbeat": now, "finished": None,
        "spec": spec_to_dict(spec), "verdict": "", "impact": "",
        "branches": "", "author": "", "tokens": None, "merged": None,
        "stopped": "", "error": "", "session_id": "",
    }
    for ext in ("stop", "log"):          # a reused id must not inherit either
        try:
            _file(job_id, ext).unlink()
        except OSError:
            pass
    _write(job_id, record)
    return record


def update(job_id, **fields):
    """Merge `fields` into a record. Only the owning process should call this."""
    record = load(job_id)
    if record is None:
        return None
    record.update(fields)
    record["heartbeat"] = time.time()
    _write(job_id, record)
    return record


def finish(job_id, status, **fields):
    return update(job_id, status=status, finished=time.time(), **fields)


# ------------------------------------------------------------- liveness --
def status_of(record, now=None):
    """The record's status, with a silent running job reported as interrupted."""
    status = record.get("status") or J.ERROR
    if status in J.ACTIVE_STATES:
        now = time.time() if now is None else now
        if now - float(record.get("heartbeat") or 0) > STALE_AFTER:
            return INTERRUPTED
        # The stop request is a file, not a write to the record: only the
        # owning process writes the record, so two writers can never clobber
        # each other and a stop can never fake a heartbeat.
        if stop_requested(record.get("id")):
            return J.STOPPING
    return status


def is_active(record, now=None):
    return status_of(record, now) in J.ACTIVE_STATES


def is_finished(record, now=None):
    return not is_active(record, now)


def list_jobs():
    """Every readable record, newest first."""
    out = []
    try:
        names = sorted(jobs_dir().glob("*.json"))
    except OSError:
        return out
    for path in names:
        record = load(path.stem)
        if record is not None:
            out.append(record)
    out.sort(key=lambda r: r.get("started") or 0, reverse=True)
    return out


def active_jobs():
    return [r for r in list_jobs() if is_active(r)]


def find_active(repo_name, pr_id):
    """A live CLI job for the same pull request, if there is one.

    Two jobs on one PR would both rewrite its description (see
    JobManager.duplicate_of), and that holds across processes too.
    """
    for record in active_jobs():
        spec = record.get("spec") or {}
        if spec.get("repo_name") == repo_name and str(spec.get("pr_id")) == str(pr_id):
            return record
    return None


def label(record):
    spec = record.get("spec") or {}
    return f"{spec.get('repo_name', '?')} #{spec.get('pr_id', '?')}"


# ------------------------------------------------------------- control --
def request_stop(job_id):
    """Ask a running job to stop. False when there is nothing to stop."""
    record = load(job_id)
    if record is None or not is_active(record):
        return False
    _file(job_id, "stop").write_text(str(time.time()), encoding="utf-8")
    return True


def clear_stop(job_id):
    try:
        _file(job_id, "stop").unlink()
    except OSError:
        pass


def stop_requested(job_id):
    return valid_id(job_id) and _file(job_id, "stop").exists()


def remove(job_id):
    """Delete a finished job's files. Refuses a live one."""
    record = load(job_id)
    if record is None:
        return False
    if is_active(record):
        return False
    for ext in ("json", "log", "stop"):
        try:
            _file(job_id, ext).unlink()
        except OSError:
            pass
    return True


def prune(keep=KEEP_FINISHED):
    """Delete the oldest finished jobs beyond `keep`."""
    finished = [r for r in list_jobs() if is_finished(r)]
    for record in finished[keep:]:
        remove(record["id"])


def read_log(job_id, tail=None):
    try:
        text = log_path(job_id).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if tail:
        text = "\n".join(text.splitlines()[-tail:])
    return text


# -------------------------------------------------------------- summary --
def counts():
    """(running, needs_input) over live CLI jobs, for the desktop's status bar."""
    running = asking = 0
    for record in active_jobs():
        if record.get("status") == J.NEEDS_INPUT:
            asking += 1
        else:
            running += 1
    return running, asking


# ------------------------------------------------------------- spawning --
def cli_command():
    """The command that runs the CLI: the packaged binary, or this checkout."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).with_name("prism_cli.py"))]


def spawn_detached(args):
    """Start `prism <args>` in its own session, so it outlives whoever started it.

    That is what lets a job survive an ssh logout, and what lets the desktop
    retry a CLI job without owning its process.
    """
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200   # DETACHED | NEW_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cli_command() + list(args), **kwargs)
