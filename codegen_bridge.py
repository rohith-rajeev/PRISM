"""Local bridge between PRISM and an external code-generation tool ("codegen").

The tool builds a change end to end and opens the pull request; PRISM reviews it.
This module is the whole conversation between the two, over loopback HTTP:

    codegen ──POST /v1/reviews──────────▶ PRISM   "PR #214 is ready, please review"
    codegen ◀──POST <callback.url>─────── PRISM   verdict + findings, or "merged"

and when the verdict is "make changes", codegen fixes the PR and submits it
again (iteration + 1) until PRISM approves and merges it — or a ceiling is hit
and a person takes over. See docs/CODEGEN_INTEGRATION.md for the full contract.

Design rules, all of them about not disturbing the rest of PRISM:

* Off by default. Nothing here runs, and no port is opened, unless the person
  enabled it (config.get_codegen()["enabled"]).
* Loopback only. The server binds 127.0.0.1, every request needs a bearer
  token that only the local user can read, and callbacks may only go to a
  loopback address — a request can never make PRISM call out to the network.
* Not a second pipeline. A request becomes an ordinary job (jobs.JobSpec) and
  runs through exactly the same review → describe → merge pipeline, gates
  included — the high-impact human confirmation and the "source moved since
  review" refusal still apply. The bridge only decides *whether to queue* and
  *how to report back*.
* Tk-free and stdlib only, like jobs.py, so it is unit-testable headlessly.

Threading contract (inherited from jobs.JobManager): only the UI thread may
create or mutate a Job. HTTP handler threads therefore never touch the manager;
they hand a Submission to `inbox` and wait for the UI thread to answer it via
`CodegenBridge.admit()` (see app.App._drain_codegen).
"""
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import config
import jobs as J
from orchestrator import REGION_DEFAULT
from version import __version__

PROTOCOL = 1
DISCOVERY_PATH = config.CONFIG_DIR / "codegen-bridge.json"

# How long an HTTP thread waits for the UI thread to accept a submission. The
# UI pumps every ~80 ms, so this only elapses if the window is wedged — better
# a clear 503 than codegen hanging on a request.
ADMIT_TIMEOUT = 8
SETTLE_WAIT = 10              # max wait for the UI to mark a job finished
CALLBACK_TIMEOUT = 10
CALLBACK_ATTEMPTS = 4
CALLBACK_BACKOFF = 2          # seconds, multiplied by the attempt number
MAX_BODY = 256 * 1024
MAX_REMEMBERED = 200          # finished reviews kept for polling (memory only)

# Free text from codegen is shown to the reviewer; bound it so a runaway request
# can't swamp the prompt.
MAX_CONTEXT_CHARS = 6000
MAX_FEEDBACK_FINDINGS = 60

_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_REGION_RE = re.compile(r"^[a-z]{2}(?:-[a-z]+)+-\d$")
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# Decisions codegen acts on. The mapping from PRISM's internal outcome to these is
# in outcome_from_summary(); the meaning for codegen is in the integration doc.
DECISION_MERGED = "merged"                    # done — nothing more to do
DECISION_APPROVED = "approved"                # approved but not merged (see reason)
DECISION_CHANGES = "changes-requested"        # fix the findings, then resubmit
DECISION_BLOCKED = "blocked"                  # serious; fix and resubmit, or escalate
DECISION_STALE = "stale"                      # PR moved during review; resubmit as-is
DECISION_NEEDS_HUMAN = "needs-human"          # a person must act in PRISM
DECISION_FAILED = "failed"                    # PRISM could not complete the review
DECISION_STOPPED = "stopped"                  # a person stopped the job in PRISM


class BridgeError(Exception):
    """A request that must be refused; carries the HTTP status and a code."""

    def __init__(self, status, code, message, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def body(self):
        return dict({"error": self.code, "message": self.message}, **self.extra)


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------
def is_loopback_url(url):
    """True only for http(s) URLs whose host is a loopback address.

    Callbacks are the one place PRISM makes an outbound request on a stranger's
    say-so, so they are confined to this machine: no SSRF into the LAN, cloud
    metadata endpoints, or anywhere else.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class ReviewRequest:
    """A validated "please review this PR" request from codegen."""

    request_id: str
    repo_name: str
    pr_id: str
    region: str
    local_repo: str
    project_dir: str
    iteration: int
    callback_url: str
    callback_token: str
    context: str            # requirement / change summary, already length-bounded
    auto_merge: bool        # codegen may only ask for *less* than PRISM allows
    run_ref: str = ""       # codegen's own handle for the run, echoed back verbatim

    @property
    def key(self):
        return f"{self.repo_name}#{self.pr_id}"


def _text(data, key, limit=200):
    value = data.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def parse_request(data, settings=None):
    """Validate a decoded JSON body into a ReviewRequest, or raise BridgeError.

    Everything that ends up in a command line, a path or a prompt is checked
    here, once, so the rest of the pipeline can keep trusting its inputs.
    """
    settings = settings or config.get_codegen()
    if not isinstance(data, dict):
        raise BridgeError(400, "bad-request", "Body must be a JSON object.")
    if data.get("protocol", PROTOCOL) != PROTOCOL:
        raise BridgeError(400, "unsupported-protocol",
                          f"This PRISM speaks protocol {PROTOCOL}.")

    request_id = _text(data, "request_id", 128)
    if not _ID_RE.match(request_id):
        raise BridgeError(400, "bad-request", "request_id is required "
                          "(letters, digits, . _ : - only, up to 128 characters).")
    repo = _text(data, "repo_name", 100)
    if not _REPO_RE.match(repo):
        raise BridgeError(400, "bad-request", "repo_name is missing or has "
                          "characters a CodeCommit repository name cannot have.")
    pr_id = str(data.get("pr_id", "")).strip()
    if not pr_id.isdigit() or len(pr_id) > 12:
        raise BridgeError(400, "bad-request", "pr_id must be a numeric CodeCommit "
                          "pull request id.")
    region = _text(data, "region", 40) or REGION_DEFAULT
    if not _REGION_RE.match(region):
        raise BridgeError(400, "bad-request", "region is not a valid AWS region.")

    local = _text(data, "local_repo", 1024)
    project = _text(data, "project_dir", 1024) or local
    if not project:
        raise BridgeError(400, "bad-request", "local_repo (or project_dir) is "
                          "required so the reviewer can read the code.")
    for label, path in (("local_repo", local or project), ("project_dir", project)):
        p = Path(path).expanduser()
        if not p.is_absolute() or not p.is_dir():
            raise BridgeError(400, "bad-request",
                              f"{label} must be an absolute path to an existing directory.")
    if not (Path(local or project).expanduser() / ".git").exists():
        raise BridgeError(400, "bad-request", "local_repo is not a git clone.")

    try:
        iteration = int(data.get("iteration", 1))
    except (TypeError, ValueError):
        iteration = 0
    if iteration < 1:
        raise BridgeError(400, "bad-request", "iteration must be a whole number >= 1.")

    callback = data.get("callback")
    cb_url = cb_token = ""
    if callback is not None:
        if not isinstance(callback, dict):
            raise BridgeError(400, "bad-request", "callback must be an object.")
        cb_url = _text(callback, "url", 500)
        cb_token = _text(callback, "token", 256)
        if cb_url and not is_loopback_url(cb_url):
            raise BridgeError(400, "bad-callback", "callback.url must be a loopback "
                              "address (127.0.0.1, ::1 or localhost).")

    context = ""
    ctx = data.get("context")
    if isinstance(ctx, dict):
        bits = [f"{k}: {ctx[k].strip()}" for k in ("change", "title", "requirements")
                if isinstance(ctx.get(k), str) and ctx[k].strip()]
        context = "\n".join(bits)[:MAX_CONTEXT_CHARS]
    elif isinstance(ctx, str):
        context = ctx.strip()[:MAX_CONTEXT_CHARS]

    options = data.get("options") if isinstance(data.get("options"), dict) else {}
    # codegen can ask for review-only, never for more than the person allowed.
    auto_merge = bool(settings["auto_merge"]) and options.get("auto_merge") is not False

    return ReviewRequest(
        request_id=request_id, repo_name=repo, pr_id=pr_id, region=region,
        local_repo=str(Path(local or project).expanduser()),
        project_dir=str(Path(project).expanduser()), iteration=iteration,
        callback_url=cb_url, callback_token=cb_token, context=context,
        auto_merge=auto_merge, run_ref=_text(data, "run_ref", 200))


def instructions_for(req):
    """The custom-instructions block handed to the reviewer for a codegen PR.

    Marked as coming from codegen and as *context*, never as a way to change the
    verdict rules: the reviewer's own contract (agents/pr-reviewer.md) already
    says custom instructions can't alter the report format, its read-only
    rules or the honesty of its verdict.
    """
    lines = [f"This pull request was opened by an automated code-generation tool "
             f"(review round {req.iteration}). Review it exactly as you would a "
             f"human's — the verdict must reflect the code, not who wrote it."]
    if req.context:
        lines.append("What codegen was asked to build (check the PR actually delivers it):\n"
                     + req.context)
    if req.iteration > 1:
        lines.append("This is a resubmission: confirm each earlier finding was truly "
                     "fixed rather than worked around, and say so per finding.")
    return "\n\n".join(lines)


# --------------------------------------------------------------------------
# Outcome shaping
# --------------------------------------------------------------------------
def outcome_from_summary(summary, review):
    """(decision, reason) for a finished pipeline run.

    `summary` is the dict full_pipeline returned; `review` is its ReviewResult
    (or None). Anything unrecognised falls to the safest reading — a person
    looks — rather than guessing that codegen should loop or that it is finished.
    """
    if summary.get("merged") is True:
        return DECISION_MERGED, ""
    verdict = getattr(review, "verdict_key", "") or ""
    stopped = summary.get("stopped") or ""
    if stopped == "pr-changed-since-review":
        return DECISION_STALE, "The PR changed while it was being reviewed."
    if stopped == "high-impact-not-confirmed":
        return DECISION_NEEDS_HUMAN, ("High-impact change: a person has to confirm "
                                      "the merge in PRISM.")
    if verdict == "request-changes":
        return DECISION_CHANGES, ""
    if verdict == "block":
        return DECISION_BLOCKED, ""
    if verdict in ("approve", "approve-with-comments"):
        if stopped in ("merge-disabled", "dry-run"):
            return DECISION_APPROVED, "Approved; PRISM was not allowed to merge."
        if stopped == "not-fast-forwardable":
            return DECISION_NEEDS_HUMAN, "Approved, but not fast-forward mergeable."
        if stopped.startswith("status-"):
            return DECISION_NEEDS_HUMAN, f"Approved, but PR status is {stopped[7:]}."
        return DECISION_NEEDS_HUMAN, "Approved, but PRISM did not merge it."
    if stopped == "unparsed-verdict":
        return DECISION_NEEDS_HUMAN, "The reviewer's verdict could not be read."
    return DECISION_NEEDS_HUMAN, "PRISM finished without a usable verdict."


def feedback_text(review, decision):
    """The findings as a message codegen can hand straight to its agent.

    Empty unless there is something to act on. Findings are PRISM's own parsed
    one-line bullets, so codegen gets the same text a human sees on the PR.
    """
    if decision not in (DECISION_CHANGES, DECISION_BLOCKED):
        return ""
    findings = list(getattr(review, "findings", None) or [])[:MAX_FEEDBACK_FINDINGS]
    head = ("PRISM reviewed this pull request and it cannot be merged yet "
            f"({getattr(review, 'verdict_raw', '') or decision}).")
    body = "\n".join(findings) if findings else (
        getattr(review, "summary", "") or "See the PR description for the findings.")
    return (f"{head}\nFix every finding below — including the same defect wherever a "
            f"finding says it recurs — without changing what the change is meant to do, "
            f"push to the same branch, then resubmit.\n\n{body}")


def build_outcome(req, review_id, summary=None, error=None, stopped=False):
    """The JSON delivered to codegen (callback body and GET /v1/reviews/{id})."""
    out = {"protocol": PROTOCOL, "review_id": review_id, "request_id": req.request_id,
           "run_ref": req.run_ref, "repo_name": req.repo_name, "pr_id": req.pr_id,
           "iteration": req.iteration, "state": "finished", "merged": False,
           "verdict": "", "verdict_raw": "", "impact_score": "", "impact_reason": "",
           "findings": [], "feedback": "", "reason": "", "decision": DECISION_FAILED,
           "finished_at": int(time.time())}
    if stopped:
        out.update(state="stopped", decision=DECISION_STOPPED,
                   reason="The job was stopped in PRISM.")
        return out
    if error is not None:
        out.update(state="error", decision=DECISION_FAILED, reason=str(error)[:1000])
        return out
    review = summary.get("review")
    decision, reason = outcome_from_summary(summary, review)
    out.update(
        decision=decision, reason=reason, merged=summary.get("merged") is True,
        verdict=getattr(review, "verdict_key", "") or "",
        verdict_raw=getattr(review, "verdict_raw", "") or "",
        impact_score=getattr(review, "impact_score", "") or "",
        impact_reason=getattr(review, "impact_reason", "") or "",
        findings=list(getattr(review, "findings", None) or [])[:MAX_FEEDBACK_FINDINGS],
        feedback=feedback_text(review, decision))
    return out


def deliver(url, token, payload, opener=urllib.request.urlopen, sleep=time.sleep):
    """POST `payload` to codegen's callback, retrying briefly. True on success.

    Best-effort by design: codegen may be restarting, and the result stays
    available to poll either way, so a failure is reported to the caller to log
    — it never affects the job it describes.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": f"PRISM/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(1, CALLBACK_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with opener(request, timeout=CALLBACK_TIMEOUT) as resp:
                if 200 <= getattr(resp, "status", 200) < 300:
                    return True
        except urllib.error.HTTPError as e:
            # A 4xx means codegen understood and refused; retrying can't help.
            if 400 <= e.code < 500:
                return False
        except Exception:  # noqa: BLE001
            pass
        if attempt < CALLBACK_ATTEMPTS:
            sleep(CALLBACK_BACKOFF * attempt)
    return False


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------
@dataclass
class Submission:
    """Work for the UI thread: a validated review request, or a cancel."""

    request: ReviewRequest = None
    cancel_id: str = ""          # set instead of `request` for a cancel
    done: threading.Event = field(default_factory=threading.Event)
    result: dict = None          # answer for the HTTP thread
    error: BridgeError = None


@dataclass
class _Review:
    review_id: str
    request: ReviewRequest
    job_id: int
    outcome: dict = None
    callback: str = "none"       # none | pending | delivered | failed
    revision: int = 0            # bumps if a person retries the job in PRISM


class CodegenBridge:
    """Owns the server, the review registry and the loopback callback.

    `manager` is the app's JobManager. It is only ever touched from the UI
    thread (admit/attach); handler threads see it through `inbox` alone, except
    for read-only status lookups of a job's `status` string.
    """

    def __init__(self, manager, settings=None, log=None, deliver_fn=deliver,
                 commit_lookup=None):
        self.manager = manager
        # (request) -> (current source commit, commit PRISM last reviewed) or
        # None when unknown. Injectable so tests need no AWS.
        self._commit_lookup = commit_lookup or _lookup_commits
        self.settings = settings or config.get_codegen()
        self.inbox = queue.Queue()
        self._log = log or (lambda msg: None)
        self._deliver = deliver_fn
        self._lock = threading.Lock()
        self._reviews = {}            # review_id -> _Review (insertion ordered)
        self._by_request = {}         # request_id -> review_id (idempotency)
        self._by_job = {}             # job id -> review_id
        self._rounds = {}             # "repo#pr" -> highest iteration admitted
        self.token = ""
        self.server = None
        self.port = 0

    # ----- lifecycle -----
    def start(self):
        """Bind loopback, publish the discovery file. Returns the port."""
        self.token = secrets.token_urlsafe(32)
        handler = _make_handler(self)
        self.server = ThreadingHTTPServer(("127.0.0.1", self.settings["port"]), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="prism-codegen-bridge",
                         daemon=True).start()
        self._write_discovery()
        return self.port

    def stop(self):
        server, self.server = self.server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
        self._remove_discovery()

    def _write_discovery(self):
        """Tell codegen where PRISM is. 0600: the file carries the bearer token."""
        info = {"app": "prism", "protocol": PROTOCOL, "version": __version__,
                "url": f"http://127.0.0.1:{self.port}", "token": self.token,
                "pid": os.getpid(), "started_at": int(time.time())}
        DISCOVERY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DISCOVERY_PATH.with_suffix(".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        os.replace(str(tmp), str(DISCOVERY_PATH))

    def _remove_discovery(self):
        # Only remove our own file: a second PRISM that started after us must
        # not lose its entry because we are exiting.
        try:
            info = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
            if info.get("pid") == os.getpid():
                DISCOVERY_PATH.unlink()
        except Exception:  # noqa: BLE001
            pass

    # ----- auth -----
    def authorised(self, header):
        header = header or ""
        presented = header[7:] if header.lower().startswith("bearer ") else ""
        return bool(self.token) and hmac.compare_digest(
            hashlib.sha256(presented.encode()).digest(),
            hashlib.sha256(self.token.encode()).digest())

    # ----- handler-thread side -----
    def precheck(self, request):
        """Refuse a resubmission that carries nothing new to review.

        Runs on the handler thread (it makes an AWS call, which must never
        happen on the UI thread). Only for round 2 onwards: round 1 has nothing
        to compare with. Fails open — if the commits can't be looked up the
        review proceeds, because wasting one review is better than wrongly
        refusing a real fix.
        """
        if request.iteration <= 1:
            return
        with self._lock:
            if request.request_id in self._by_request:
                return          # a retried POST: admit() answers it idempotently
        try:
            commits = self._commit_lookup(request)
        except Exception:  # noqa: BLE001
            return
        if commits and commits[0] and commits[0] == commits[1]:
            raise BridgeError(
                409, "no-new-commits",
                f"{request.key} has no commits since PRISM last reviewed it "
                f"({commits[0][:8]}). Push the fixes to the PR's source branch, "
                f"then resubmit.")

    def submit(self, request):
        """Hand a request to the UI thread and wait for its answer."""
        self.precheck(request)
        return self._roundtrip(Submission(request=request))

    def request_cancel(self, review_id):
        return self._roundtrip(Submission(cancel_id=review_id))

    def _roundtrip(self, sub):
        self.inbox.put(sub)
        if not sub.done.wait(ADMIT_TIMEOUT):
            # Mark it abandoned so a late admit() skips it rather than queueing
            # a review nobody is waiting to hear about.
            sub.error = BridgeError(503, "busy", "PRISM did not respond in time.")
            sub.done.set()
            raise sub.error
        if sub.error is not None:
            raise sub.error
        return sub.result

    def status(self, review_id):
        with self._lock:
            rev = self._reviews.get(review_id)
            if rev is None:
                return None
            if rev.outcome is not None:
                return dict(rev.outcome, callback=rev.callback)
            job = self.manager.jobs.get(rev.job_id)
            state = job.status if job is not None else "unknown"
            return {"protocol": PROTOCOL, "review_id": review_id,
                    "request_id": rev.request.request_id, "pr_id": rev.request.pr_id,
                    "repo_name": rev.request.repo_name,
                    "iteration": rev.request.iteration, "state": state,
                    "callback": rev.callback}

    # ----- UI-thread side -----
    def admit(self, sub):
        """Act on a submission (queue a review or cancel one). UI thread only.

        Idempotent on request_id (codegen retrying a timed-out POST must not
        queue a second review) and guarded by the same duplicate rule as the
        New-job form: one active job per PR.
        """
        if sub.done.is_set():       # the handler already gave up on it
            return
        try:
            sub.result = (self._cancel(sub.cancel_id) if sub.cancel_id
                          else self._admit(sub.request))
        except BridgeError as e:
            sub.error = e
        except Exception as e:  # noqa: BLE001
            sub.error = BridgeError(500, "internal", f"Could not queue the review: {e}")
        sub.done.set()

    def _admit(self, req):
        with self._lock:
            known = self._by_request.get(req.request_id)
            if known is not None:
                rev = self._reviews[known]
                return {"review_id": known, "job_id": rev.job_id, "duplicate": True}
            seen = self._rounds.get(req.key, 0)
        # Authoritative round comes from codegen (it survives a PRISM restart); our
        # own count only catches a codegen that forgot to increment it.
        round_no = max(req.iteration, seen + 1)
        if round_no > self.settings["max_iterations"]:
            raise BridgeError(
                409, "iteration-limit",
                f"{req.key} has been through {self.settings['max_iterations']} review "
                f"rounds without being merged; a person needs to look at it.",
                max_iterations=self.settings["max_iterations"])
        if req.iteration < round_no:
            req = _with_iteration(req, round_no)
        spec = J.JobSpec(
            project_dir=req.project_dir, repo_name=req.repo_name, pr_id=req.pr_id,
            local_repo=req.local_repo, region=req.region, model=None,
            do_review=True, do_update_desc=True, do_merge=req.auto_merge,
            do_sync=True, dry_run=False,
            webhook_url=config.get_webhook_url() or None,
            custom_instructions=instructions_for(req),
            origin="codegen", origin_ref=req.request_id)
        try:
            job = self.manager.create(spec)
        except J.DuplicateJob as e:
            with self._lock:
                existing = next((r for r in self._reviews.values()
                                 if r.request.key == req.key and r.outcome is None), None)
            raise BridgeError(409, "already-reviewing", str(e),
                              review_id=existing.review_id if existing else None)
        review_id = "rv_" + secrets.token_hex(8)
        with self._lock:
            self._reviews[review_id] = _Review(review_id, req, job.id)
            self._by_request[req.request_id] = review_id
            self._by_job[job.id] = review_id
            self._rounds[req.key] = round_no
            self._trim()
        self.manager.pump()
        self._log(f"codegen asked for a review of {job.label} (round {req.iteration}).")
        return {"review_id": review_id, "job_id": job.id, "duplicate": False,
                "iteration": req.iteration}

    def _trim(self):
        # Caller holds the lock. Drop the oldest *finished* reviews first; an
        # unfinished one is still needed to route its outcome.
        for rid in [r for r, v in self._reviews.items() if v.outcome is not None]:
            if len(self._reviews) <= MAX_REMEMBERED:
                break
            rev = self._reviews.pop(rid)
            self._by_request.pop(rev.request.request_id, None)
            self._by_job.pop(rev.job_id, None)

    # ----- worker-thread side -----
    def job_finished(self, job, summary=None, error=None, stopped=False):
        """JobManager outcome hook. Runs on the job's worker thread.

        Records the outcome, then delivers the callback on its own thread so a
        slow codegen never keeps a review slot occupied.
        """
        with self._lock:
            rid = self._by_job.get(job.id)
            rev = self._reviews.get(rid) if rid else None
            if rev is None:
                return                      # not a codegen job — nothing to do
            outcome = build_outcome(rev.request, rid, summary=summary, error=error,
                                    stopped=stopped)
            # A person can retry a codegen job from the jobs list. The new result
            # is sent again, tagged with a higher revision so codegen can tell it
            # from a duplicate delivery of the first.
            rev.revision += 1
            outcome["revision"] = rev.revision
            rev.outcome = outcome
            rev.callback = "pending" if rev.request.callback_url else "none"
        if rev.request.callback_url:
            threading.Thread(target=self._callback, args=(rev, outcome),
                             daemon=True).start()
        self._log(f"codegen review of {job.label}: {outcome['decision']}.")

    def _callback(self, rev, outcome):
        # The worker finishes before the UI thread has applied "done" to the
        # job, and codegen may resubmit the moment it hears. Wait for the job to
        # read as finished first, or that resubmission would be refused as a
        # duplicate of a review that is, to codegen, already over. Bounded: a
        # wedged UI thread delays the callback, never loses it.
        job = self.manager.jobs.get(rev.job_id)
        deadline = time.time() + SETTLE_WAIT
        while job is not None and not job.is_terminal and time.time() < deadline:
            time.sleep(0.05)
        ok = self._deliver(rev.request.callback_url, rev.request.callback_token, outcome)
        with self._lock:
            rev.callback = "delivered" if ok else "failed"
        if not ok:
            self._log(f"Could not reach codegen at {rev.request.callback_url}; "
                      f"the result stays available at /v1/reviews/{rev.review_id}.")

    def _cancel(self, review_id):
        with self._lock:
            rev = self._reviews.get(review_id)
        if rev is None:
            raise BridgeError(404, "not-found", "No such review.")
        # stop() is None for a job that already finished — cancelling is then a
        # harmless no-op, reported as such rather than as an error.
        live = self.manager.stop(rev.job_id) is not None
        return {"review_id": review_id, "cancelled": live}


def _lookup_commits(req):
    """(PR source commit now, commit PRISM last reviewed) from live data."""
    import orchestrator as O
    pr = O.get_pr(req.pr_id, region=req.region)
    return (pr.get("sourceCommit") or "",
            O._locally_confirmed_reviewed_commit(req.repo_name, req.pr_id) or "")


def _with_iteration(req, iteration):
    from dataclasses import replace
    return replace(req, iteration=iteration)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def _make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"PRISM/{__version__}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):      # keep the console quiet
            pass

        def _send(self, status, payload):
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _guard(self):
            """Auth + DNS-rebinding defence. True when the request may proceed."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost", "::1"):
                self._send(403, {"error": "forbidden", "message": "Bad Host header."})
                return False
            if not bridge.authorised(self.headers.get("Authorization")):
                self._send(401, {"error": "unauthorised",
                                 "message": "Missing or wrong bearer token."})
                return False
            return True

        def _body(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                raise BridgeError(413, "too-large", "Request body is too large.")
            try:
                return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                raise BridgeError(400, "bad-request", "Body is not valid JSON.")

        def _route(self, method):
            path = urllib.parse.urlsplit(self.path).path.rstrip("/")
            try:
                if not self._guard():
                    return
                if method == "GET" and path == "/v1/health":
                    return self._send(200, {"ok": True, "app": "prism",
                                            "protocol": PROTOCOL, "version": __version__})
                if method == "POST" and path == "/v1/reviews":
                    result = bridge.submit(parse_request(self._body(), bridge.settings))
                    return self._send(200 if result.get("duplicate") else 202,
                                      dict(result, protocol=PROTOCOL, state="queued"))
                match = re.fullmatch(r"/v1/reviews/(rv_[0-9a-f]{16})(/cancel)?", path)
                if match and method == "GET" and not match.group(2):
                    status = bridge.status(match.group(1))
                    if status is None:
                        raise BridgeError(404, "not-found", "No such review.")
                    return self._send(200, status)
                if match and method == "POST" and match.group(2):
                    return self._send(202, dict(bridge.request_cancel(match.group(1)),
                                                protocol=PROTOCOL))
                raise BridgeError(404, "not-found", "No such endpoint.")
            except BridgeError as e:
                self._send(e.status, e.body())
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": "internal", "message": str(e)[:300]})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

    return Handler

