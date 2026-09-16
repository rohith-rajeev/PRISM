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


def ensure_bundled_agent(project_dir: str, emit=print) -> str:
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


def _run_stream(cmd, cwd, emit, timeout=1200):
    """Run cmd, streaming stdout lines to emit. Returns (rc, full_output)."""
    emit(f"$ {_display_cmd(cmd)}")
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
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
    try:
        for line in proc.stdout:
            out_lines.append(line)
            emit(line.rstrip())
        proc.wait(timeout=60)
    except Exception as e:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        out_lines.append(f"\n[process error: {e}]\n")
        return 1, "".join(out_lines)
    finally:
        watchdog.cancel()
    if timed_out.is_set():
        out_lines.append("\n[TIMEOUT after %ss]\n" % timeout)
        return 1, "".join(out_lines)
    rc = proc.returncode
    return (1 if rc is None else rc), "".join(out_lines)


def run_opencode_review(pr_id, repo_name, local_repo, project_dir,
                        region=REGION_DEFAULT, emit=print, model=None):
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
    cmd = [exe, "run", "--agent", agent, "--dir", project_dir, "--auto"]
    if model:
        cmd += ["--model", model]
    cmd += [prompt]
    rc, out = _run_stream(cmd, cwd=project_dir, emit=emit)
    if rc != 0 and "Verdict" not in out:
        raise RuntimeError(f"Review run failed (exit {rc}). See log.")
    return out


def run_opencode_approve_description(pr_id, project_dir, emit=print, model=None,
                                     repo_name=None, region=REGION_DEFAULT):
    """Step 2: tell the agent 'yes' — append findings to the PR description.

    Uses a fresh non-interactive run that continues the last session when
    possible, falling back to a new session. The agent already knows the
    exact marker format (<!-- pr-reviewer:start --> ... ).
    """
    prompt = (
        f"Yes — append your findings as bullet points to the description of "
        f"pull request {pr_id}"
        + (f" (CodeCommit repository '{repo_name}', AWS region {region})" if repo_name else "")
        + " now (I approve this specific update)."
    )
    exe = require_engine()
    # Try continuing the previous review session first.
    for args in (["--continue"], []):
        cmd = [exe, "run", "--agent", AGENT_NAME,
               "--dir", project_dir, "--auto"]
        if model:
            cmd += ["--model", model]
        cmd += args + [prompt]
        rc, out = _run_stream(cmd, cwd=project_dir, emit=emit)
        if rc == 0:
            return out
    raise RuntimeError("Agent description-update runs failed (tried --continue and fresh).")


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


def sync_destination_into_source(local_repo, dest, src, pr_id, emit=print):
    """Sync destination commits onto the source branch locally, then push.

    Equivalent of: git fetch, checkout source, merge origin/destination, push.
    Raises on merge conflicts (aborts the merge first).
    """
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


def full_pipeline(project_dir, repo_name, pr_id, local_repo=None,
                  region=REGION_DEFAULT,
                  do_update_desc=True, do_merge=True, do_sync=True,
                  dry_run=False, model=None, progress=None, emit=print):
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

    emit(f"▸ Target: CodeCommit repo '{repo_name}'  |  PR #{pr_id}  |  region {region}"
         + (f"  |  model {model}" if model else "  |  model (default)"))
    repo_name, local_clone = resolve_repo(repo_name, project_dir, local_repo)
    emit(f"▸ Local clone: {local_clone}")
    ensure_bundled_agent(project_dir, emit=emit)

    if not Path(local_clone).is_dir():
        raise RuntimeError(f"Local clone path not found: {local_clone}")

    # ---- Step 1: review ----
    pg(STAGE_REVIEW, "active")
    emit("\n═══ STEP 1 — AI review ═══")
    raw = run_opencode_review(pr_id, repo_name, local_clone, project_dir,
                              region=region, emit=emit, model=model)
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
    if do_update_desc:
        pg(STAGE_DESCRIBE, "active")
        emit("\n═══ STEP 2 — Update PR description ═══")
        if dry_run:
            emit("(dry-run) would ask the reviewer to append findings + fallback direct update.")
            pg(STAGE_DESCRIBE, "skipped")
        else:
            try:
                run_opencode_approve_description(pr_id, project_dir, emit=emit,
                                                 model=model, repo_name=repo_name,
                                                 region=region)
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
