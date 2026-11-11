"""Setup checks (`prism doctor`) and a shareable log bundle (`prism export`).

Both exist for the same reason: when a review fails on someone else's machine,
the person debugging it needs facts, not a screenshot. `run_checks` collects
the facts about this machine's setup, and `export_bundle` packs a job's
transcript with them into one .zip - a format every OS opens - with secrets
and the home folder's name scrubbed first.

Stdlib only and free of Tkinter, so the desktop and the command line share it.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
GLYPH = {OK: "✓", WARN: "⚠", FAIL: "✗", INFO: "·"}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""          # what to do about it; only for warn / fail


# ------------------------------------------------------------ redaction --
_SECRET_PATTERNS = (
    # Google Chat webhooks carry their key and token in the URL.
    (re.compile(r"https://chat\.googleapis\.com/\S+"), "<redacted-webhook-url>"),
    # user:password@host in any URL.
    (re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@"), "<redacted>@"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "<redacted-aws-key-id>"),
    (re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{16,}"), "<redacted-key>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "<redacted-token>"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "<redacted-token>"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer <redacted>"),
    # key = value / "key": "value" for the usual secret names.
    (re.compile(
        r"""(?ix)\b(api[_-]?key|access[_-]?key(?:[_-]?id)?|secret(?:[_-]?access[_-]?key)?|
        session[_-]?token|auth[_-]?token|token|password|passwd|authorization)\b
        (["']?\s*[=:]\s*["']?)[^\s"',&;]+"""), r"\1\2<redacted>"),
)


def redact(text):
    """`text` with credentials removed and the home folder shown as ~."""
    if not text:
        return text or ""
    text = str(text)
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    home = str(Path.home())
    if len(home) > 3:
        text = text.replace(home, "~")
        text = text.replace(home.replace("\\", "/"), "~")
    return text


def _scrub(value):
    """Redact every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: ("<redacted>" if "webhook" in str(k).lower() and v else _scrub(v))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


# --------------------------------------------------------------- running --
def _run(cmd, timeout=15, cwd=None):
    """(returncode, combined output). returncode is None when it could not run."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd,
                              stdin=subprocess.DEVNULL, env=_env())
    except FileNotFoundError:
        return None, "not found"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def _env():
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"        # never wait for a person
    env["PYTHONUTF8"] = "1"
    return env


def _first_line(text, limit=300):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


# ---------------------------------------------------------------- checks --
def _check_system():
    import version
    frozen = " (packaged app)" if getattr(sys, "frozen", False) else ""
    return [
        Check("PRISM", INFO, f"version {version.__version__}{frozen}"),
        Check("System", INFO, f"{platform.platform()}, Python {platform.python_version()}"),
        Check("Shell", INFO, os.environ.get("SHELL") or os.environ.get("COMSPEC") or "unknown"),
    ]


def _check_path():
    import orchestrator as O
    missing = O.ensure_tool_path()
    path = os.environ.get("PATH", "")
    detail = f"{len(path.split(os.pathsep))} folders: {path}"
    return [Check("PATH", INFO, detail)], missing


def _check_git():
    exe = shutil.which("git")
    if not exe:
        return [Check("git", FAIL, "not found on PATH",
                      "Install git (https://git-scm.com) and make sure `git --version` "
                      "works in a terminal.")]
    rc, out = _run([exe, "--version"], 10)
    checks = [Check("git", OK if rc == 0 else WARN, f"{exe} - {_first_line(out)}")]
    rc, out = _run([exe, "config", "--get-regexp", r"^credential\."], 10)
    helpers = [ln for ln in out.splitlines() if "helper" in ln] if rc == 0 else []
    checks.append(Check("git credentials", INFO,
                        "; ".join(helpers) if helpers else "no credential helper configured "
                        "(fine for SSH remotes; HTTPS CodeCommit remotes need one)"))
    return checks


def _check_aws(region, deep):
    exe = shutil.which("aws")
    if not exe:
        return [Check("aws CLI", FAIL, "not found on PATH",
                      "Install the AWS CLI "
                      "(https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), "
                      "then run `aws configure` or `aws login`. Without it PRISM cannot read "
                      "pull requests (the review shows branches as 'unknown').")]
    rc, out = _run([exe, "--version"], 15)
    checks = [Check("aws CLI", OK if rc == 0 else WARN, f"{exe} - {_first_line(out)}")]
    if not deep:
        return checks
    rc, out = _run([exe, "sts", "get-caller-identity", "--output", "json"], 25)
    if rc == 0:
        try:
            who = json.loads(out).get("Arn", "")
        except ValueError:
            who = ""
        checks.append(Check("aws sign-in", OK, who or "signed in"))
    else:
        checks.append(Check("aws sign-in", FAIL, _first_line(out) or "failed",
                            "Sign in again: `aws login` (or `aws sso login`, or check "
                            "`aws configure`). Expired or missing credentials are the usual cause."))
        return checks          # the checks below would only repeat the same failure
    rc, out = _run([exe, "codecommit", "list-repositories", "--max-items", "1",
                    "--region", region], 30)
    checks.append(Check(f"CodeCommit ({region})", OK if rc == 0 else FAIL,
                        "reachable" if rc == 0 else (_first_line(out) or "failed"),
                        "" if rc == 0 else "This identity cannot use CodeCommit in this region. "
                        "Check the region and that your IAM policy allows codecommit:*."))
    return checks


def _check_pr(pr_id, region):
    import orchestrator as O
    try:
        meta = O.get_pr(pr_id, region=region)
    except Exception as exc:  # noqa: BLE001
        return [Check(f"pull request {pr_id}", FAIL, _first_line(str(exc), 500),
                      "PRISM could not read this pull request: wrong id or region, no access, "
                      "or the aws CLI is not signed in.")]
    return [Check(f"pull request {pr_id}", OK,
                  f"{meta.get('repositoryName')}: {meta.get('sourceReference')} → "
                  f"{meta.get('destinationReference')} ({meta.get('status') or 'status unknown'})")]


def _check_repo(path):
    repo = Path(path).expanduser()
    if not repo.is_dir():
        return [Check("repository", FAIL, f"{repo} is not a folder", "Check the path.")]
    git = shutil.which("git")
    if not git:
        return []
    rc, out = _run([git, "rev-parse", "--is-inside-work-tree"], 10, cwd=str(repo))
    if rc != 0:
        return [Check("repository", FAIL, f"{repo} is not a git clone",
                      "Point --repo at the folder you cloned the repository into.")]
    checks = [Check("repository", OK, str(repo))]
    rc, out = _run([git, "remote", "get-url", "origin"], 10, cwd=str(repo))
    if rc != 0:
        checks.append(Check("remote 'origin'", FAIL, "this clone has no remote called origin",
                            "PRISM fetches the pull request's branches from origin."))
        return checks
    checks.append(Check("remote 'origin'", OK, out.strip()))
    rc, out = _run([git, "ls-remote", "--heads", "origin"], 40, cwd=str(repo))
    n = len(out.splitlines()) if rc == 0 else 0
    checks.append(Check("git fetch access", OK if rc == 0 else FAIL,
                        f"origin answered ({n} branches)" if rc == 0
                        else (_first_line(out, 500) or "failed"),
                        "" if rc == 0 else "PRISM must be able to `git fetch origin` here. "
                        "Run that in the clone yourself and fix what it says (credentials, "
                        "VPN, SSH key)."))
    return checks


def _check_engine(model, deep):
    import orchestrator as O
    exe = O.engine_path()
    if not exe:
        return [Check("review engine", FAIL, "opencode not found",
                      "Install the review engine (see the README's Prerequisites).")]
    rc, out = _run([exe, "--version"], 20)
    checks = [Check("review engine", OK if rc == 0 else WARN, f"{exe} - {_first_line(out)}")]
    agent = getattr(O, "BUNDLED_AGENT", None)
    if agent is not None:
        checks.append(Check("reviewer agent file", OK if Path(agent).is_file() else FAIL,
                            str(agent), "" if Path(agent).is_file() else "PRISM's install is "
                            "incomplete - reinstall or update PRISM."))
    if not deep:
        return checks
    try:
        models = O.list_available_models(timeout=40, attempts=1)
    except Exception as exc:  # noqa: BLE001
        models = []
        checks.append(Check("models", WARN, f"{type(exc).__name__}: {exc}"))
    if models:
        checks.append(Check("models", OK, f"{len(models)} available"))
    else:
        checks.append(Check("models", WARN, "the engine listed no models",
                            "Sign in to a provider in the engine (`opencode auth login`) and "
                            "check your network."))
    if model and models:
        if model in models:
            checks.append(Check(f"model {model}", OK, "available"))
        else:
            close = [m for m in models if model.split("/")[-1].split(".")[0] in m][:5]
            checks.append(Check(f"model {model}", FAIL, "not in the engine's model list",
                                "Pick one from `prism models`." +
                                (f" Similar: {', '.join(close)}" if close else "")))
    return checks


def _check_files():
    import config
    checks = []
    folder = config.CONFIG_DIR
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".doctor-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("settings folder", OK, f"{folder} (writable)"))
    except OSError as exc:
        checks.append(Check("settings folder", FAIL, f"{folder}: {exc}",
                            "PRISM keeps its settings and job logs here and needs to write to it."))
    try:
        hooked = bool(config.get_webhook_url())
    except Exception:  # noqa: BLE001
        hooked = False
    checks.append(Check("Google Chat webhook", INFO, "configured" if hooked else "not set"))
    return checks


def run_checks(region=None, repo=None, pr=None, model=None, deep=True):
    """Every check, in the order a person would debug them. Returns [Check].

    `deep` adds the ones that use the network or the engine (sign-in, CodeCommit,
    a fetch, the model list); without it only what is on this machine is read.
    """
    from orchestrator import REGION_DEFAULT
    region = region or REGION_DEFAULT
    checks = _check_system()
    path_checks, _missing = _check_path()
    checks += path_checks
    checks += _check_git()
    checks += _check_aws(region, deep)
    if deep and pr:
        checks += _check_pr(pr, region)
    if deep and repo:
        checks += _check_repo(repo)
    checks += _check_engine(model, deep)
    checks += _check_files()
    return checks


def render_text(checks, paint=None):
    """The checks as readable text. `paint(text, kind)` can add colour."""
    paint = paint or (lambda text, _kind: text)
    kind = {OK: "ok", WARN: "warn", FAIL: "err", INFO: "dim"}
    lines = []
    for c in checks:
        mark = paint(GLYPH[c.status], kind[c.status])
        lines.append(f"{mark} {c.name}" + (f" - {c.detail}" if c.detail else ""))
        if c.hint and c.status in (WARN, FAIL):
            lines.append(f"    → {c.hint}")
    bad = sum(c.status == FAIL for c in checks)
    warn = sum(c.status == WARN for c in checks)
    lines.append("")
    lines.append("Everything needed was found." if not (bad or warn) else
                 f"{bad} problem(s), {warn} warning(s).")
    return "\n".join(lines)


# ---------------------------------------------------------------- export --
README = """\
PRISM log bundle
================
Made {when} by PRISM {version} for job {job_id}.

  log.txt      the job's transcript, as PRISM showed it
  job.json     the job's settings and outcome
  doctor.txt   a check of this machine's setup (git, aws, review engine, ...)

Credentials (webhook URLs, tokens, keys, passwords) and the name of the home
folder were removed before this file was written, but the transcript can still
contain pull-request titles, branch names, file names and code from the diff.
Look through log.txt before sending it outside your organisation.
{note}"""


def default_name(job_id):
    return f"prism-job-{job_id}-{time.strftime('%Y%m%d-%H%M%S')}.zip"


def export_bundle(dest, job_id, log_text, record=None, checks=None, note=""):
    """Write a .zip with the (scrubbed) transcript, job record and setup checks.

    Written to a temporary file first, so a failure never leaves a half-made
    archive at `dest`. Returns the path written.
    """
    import version
    dest = Path(dest).expanduser()
    if dest.suffix.lower() != ".zip":
        dest = dest.with_name(dest.name + ".zip")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if checks is None:
        try:
            checks = run_checks(deep=False)
        except Exception as exc:  # noqa: BLE001 - the bundle matters more than the checks
            checks = [Check("doctor", WARN, f"could not run: {type(exc).__name__}: {exc}")]
    doctor = redact(render_text(checks))
    readme = README.format(when=time.strftime("%Y-%m-%d %H:%M:%S %Z"), version=version.__version__,
                           job_id=job_id, note=("\n" + note + "\n") if note else "")
    fd, tmp = tempfile.mkstemp(suffix=".zip", dir=str(dest.parent))
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("README.txt", readme)
            z.writestr("log.txt", redact(log_text) or "(the job has no log)\n")
            z.writestr("job.json", json.dumps(_scrub(record or {}), indent=2,
                                              default=str, ensure_ascii=False))
            z.writestr("doctor.txt", doctor + "\n")
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def record_from_job(job):
    """A desktop job's settings and outcome as plain data, for job.json."""
    from dataclasses import asdict
    try:
        spec = asdict(job.spec)
    except TypeError:
        spec = dict(getattr(job.spec, "__dict__", {}))
    return {
        "id": job.id, "origin": "desktop", "status": job.status, "spec": spec,
        "stages": getattr(job, "stages", {}), "verdict": job.verdict_raw,
        "impact": job.impact, "source": job.source, "dest": job.dest,
        "tokens": getattr(job, "tokens", {}), "error": job.error,
    }
