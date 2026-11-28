"""Job model and scheduler for PRISM.

One job is one pull request being reviewed. Jobs run concurrently, each with
its own agent conversation, cancellation token and log buffer, so the UI can
show a list of them and open any one for detail.

Deliberately free of Tkinter: everything here is a plain model plus threads,
which keeps the scheduler testable headlessly. The UI layer owns the queue this
module writes to and is the only thing that mutates a Job — see the threading
note on JobManager.
"""

import collections
import dataclasses
import threading
from dataclasses import dataclass
from pathlib import Path

import chat as chat_mod
import chatsources
import docreview
import jobid
from pathutil import native_path
from orchestrator import (INTERNAL_SUMMARY_KEYS, REGION_DEFAULT, Cancelled, RunControl,
                          followup_declined, full_pipeline, run_followup)

# Each job is a full agent run — model calls, AWS, git. Running an unbounded
# number at once invites provider rate limits and spawns unbounded child
# processes, so extra jobs queue and start as slots free. Three live jobs
# turned out to be enough to make model/API rate limits bite in practice —
# two stays comfortably under them, with the rest queued instead of stalling.
MAX_PARALLEL_JOBS = 2

# Per-job ring buffer. 5,000 lines renders in ~8 ms when switching jobs and
# costs roughly half a megabyte.
LOG_LINES_PER_JOB = 5000

QUEUED, RUNNING, NEEDS_INPUT = "queued", "running", "needs-input"
STOPPING, STOPPED, DONE, ERROR = "stopping", "stopped", "done", "error"
ACTIVE_STATES = (QUEUED, RUNNING, NEEDS_INPUT, STOPPING)
TERMINAL_STATES = (STOPPED, DONE, ERROR)


@dataclass(frozen=True)
class JobSpec:
    """Immutable snapshot of the form at the moment a job was created.

    Frozen on purpose: editing the form afterwards to set up the next job must
    never reach back into a job already running.
    """

    project_dir: str
    repo_name: str
    pr_id: str
    local_repo: str = None
    region: str = REGION_DEFAULT
    model: str = None
    do_review: bool = True
    do_update_desc: bool = True
    do_merge: bool = True
    do_sync: bool = True
    dry_run: bool = False
    webhook_url: str = None
    custom_instructions: str = ""
    # Where the job came from: "manual" for the New-job form, "codegen" for a PR
    # codegen handed over (see codegen_bridge). Display and routing only — these are
    # deliberately absent from pipeline_kwargs(), so the review/merge pipeline
    # behaves identically whoever asked for it.
    origin: str = "manual"
    origin_ref: str = ""
    # The person who started the review (config.get_username at creation). It is
    # written onto the PR description and the chat card so the review can be
    # traced to someone; "" leaves both exactly as they were before it existed.
    reviewer: str = ""
    # Where the request lives, fixed when the job is created (FR-GL-1, FR-X-1): a
    # CodeCommit pull request, or a GitLab merge request - then `repo_name` is the
    # GitLab project path ("group/sub/project"), `pr_id` the merge request's number
    # and `host_url` the GitLab address. Never the token: that is read when used.
    host: str = "codecommit"
    host_url: str = ""

    def __post_init__(self):
        # Paths typed with either kind of slash are made valid for this OS.
        object.__setattr__(self, "project_dir", native_path(self.project_dir))
        if self.local_repo:
            object.__setattr__(self, "local_repo", native_path(self.local_repo))

    # Which kind of job this is. A documentation review (docreview.DocJobSpec)
    # says "docs"; everything that reads `kind` treats a missing one as "pr".
    kind = "pr"

    @property
    def is_gitlab(self):
        return self.host == "gitlab"

    @property
    def label(self):
        if self.is_gitlab:
            return f"{self.repo_name} !{self.pr_id}"
        return f"{self.repo_name} #{self.pr_id}"

    @property
    def dup_key(self):
        """Two jobs on the same request would both write its description."""
        # GitLab project paths are case-insensitive; CodeCommit names are not.
        name = self.repo_name.lower() if self.is_gitlab else self.repo_name
        return (self.host or "codecommit", (self.host_url or "").rstrip("/").lower(),
                name, str(self.pr_id))

    @property
    def clone_key(self):
        """Resolved clone path, for spotting two jobs sharing one checkout."""
        target = self.local_repo or self.project_dir
        try:
            return str(Path(native_path(target)).resolve())
        except Exception:  # noqa: BLE001
            return str(target)

    def summary_bits(self):
        """Short descriptor for a jobs-list row."""
        bits = ["GitLab" if self.is_gitlab else self.region]
        if self.origin == "codegen":
            bits.insert(0, "via codegen")
        if not self.do_review:
            bits.append("no review")
        if self.dry_run:
            bits.append("dry run")
        if not self.do_merge:
            bits.append("no auto-merge")
        return " · ".join(bits)

    def pipeline_kwargs(self):
        return dict(project_dir=self.project_dir, repo_name=self.repo_name,
                    pr_id=self.pr_id, local_repo=self.local_repo,
                    region=self.region, model=self.model,
                    do_review=self.do_review, do_update_desc=self.do_update_desc,
                    do_merge=self.do_merge, do_sync=self.do_sync, dry_run=self.dry_run,
                    webhook_url=self.webhook_url,
                    custom_instructions=self.custom_instructions,
                    reviewer=self.reviewer, host=self.host, host_url=self.host_url)


class AskBridge:
    """Blocking ask() for a worker thread, answered on the UI thread.

    The pipeline runs off-thread and Tk is not thread-safe, so a question goes
    through the same queue as log lines and the worker parks on an Event until
    the UI resolves it. The wait is polled rather than indefinite so stopping a
    job while its question is on screen releases the worker instead of leaking
    a parked thread.
    """

    def __init__(self, job):
        self.job = job
        self._event = threading.Event()
        self._answer = None

    def ask(self, question, choices=None):
        """Ask the user. `choices` turns the panel into buttons.

        Used for merge conflicts, where a free-text answer would be ambiguous
        and the user must pick one side per hunk.
        """
        self._event.clear()
        self._answer = None
        payload = {"text": question, "choices": choices} if choices else question
        self.job.emit_event("ask", payload)
        while not self._event.wait(0.2):
            if self.job.control.cancelled():
                return None
        return self._answer

    def resolve(self, answer):
        self._answer = answer
        self._event.set()


class Job:
    """One pull request under review."""

    def __init__(self, job_id, spec, out_q):
        self.id = job_id
        self.spec = spec
        self._out_q = out_q
        self.status = QUEUED
        self.stages = {}
        self.verdict_raw = ""
        self.verdict_key = ""
        self.impact = ""
        self.source = ""
        self.dest = ""
        self.author = ""
        self.tokens = {}
        self.pending_question = None
        self.result = None
        self.error = None
        self.log = collections.deque(maxlen=LOG_LINES_PER_JOB)
        self.control = RunControl()
        self.ask = AskBridge(self)
        self.thread = None
        self._note_lock = threading.Lock()
        self._pending_note = None
        # The reviewer's conversation, once it has one — lets the person keep
        # asking about this PR after the job has finished (see
        # JobManager.start_followup). followup_control is that turn's own
        # token: the job's control is already spent by the time it is used.
        self.session_id = ""
        self.followup_busy = False
        self.followup_control = None
        # The live chat (FR-CHAT): its thread lives as long as the job does - kept across
        # a retry, dropped with the job - and is never part of the OUTPUT log.
        self.chat = []                  # [(role, text)]: role is "you" or "chat"
        self.chat_open = False
        self.chat_busy = False
        self.chat_control = None
        # What the finished run reported, as data (findings, per-document results,
        # scale...): what the chat answers from, next to the status and the log.
        self.data = {}
        # The job's ID (see jobid.py), on every chat card it posts and on its row. Fixed
        # for life: a retry is the same job and keeps it. (`id` is the list's own
        # number for the job; this is the one people see and search for.)
        self.job_id = jobid.new()

    def matches(self, query):
        """Whether a job-list search for `query` finds this job (see jobid.matches)."""
        return jobid.matches(query, [self.job_id], self.label)

    # ----- plumbing -----
    def emit_event(self, kind, payload):
        """Post to the UI queue. Called from the worker thread only."""
        self._out_q.put((self.id, kind, payload))

    def queue_note(self, text):
        """UI thread: leave a steering note for the worker to pick up.

        There is no live channel into an agent turn already in flight (see
        pop_note), so the note waits here until the pipeline next checks for
        one — between agent turns, not mid-turn.
        """
        text = (text or "").strip()
        if not text:
            return
        with self._note_lock:
            self._pending_note = text

    def pop_note(self):
        """Worker thread: take and clear the pending note, if any."""
        with self._note_lock:
            note, self._pending_note = self._pending_note, None
            return note

    def append_log(self, text, tag=None):
        """The single place log lines are recorded.

        Both the live tail and the replay-on-switch read this buffer, so the
        cap applies to both; capping only the buffer would let a displayed
        job's widget grow unbounded while the model behind it stayed bounded.
        """
        self.log.append((text, tag))

    # ----- state -----
    @property
    def is_active(self):
        return self.status in ACTIVE_STATES

    @property
    def is_terminal(self):
        return self.status in TERMINAL_STATES

    @property
    def label(self):
        return self.spec.label

    def chat_details(self):
        """The job's details right now, as chat.snapshot() reads them. UI thread only."""
        spec, data = self.spec, self.data or {}
        docs = getattr(spec, "kind", "pr") == "docs"
        result = self.result if isinstance(self.result, dict) else {}
        if docs:
            settings = spec.summary_bits()
            target = "; ".join(
                (f"{t.label}: {t.repo_name} @ {t.branch}" if t.has_repo else t.label)
                + " — " + ", ".join(list(t.files) + [f"{u.name} (uploaded"
                                                     + (f", to {u.dest}" if u.dest else "")
                                                     + ")" for u in t.uploads])
                for t in spec.targets)
            host = None
        else:
            settings = spec.summary_bits()
            target = spec.label
            host = "GitLab " + spec.host_url if getattr(spec, "is_gitlab", False) \
                else f"CodeCommit ({spec.region})"
        return {
            "label": spec.label, "kind": "docs" if docs else "pr", "job_id": self.job_id,
            "status": self.status + (f" (waiting for the person: {self.pending_question}"
                                     f")" if self.pending_question and
                                     isinstance(self.pending_question, str) else ""),
            "stages": dict(self.stages), "settings": settings, "host": host,
            "target": target, "branches": self.branches,
            "owner": getattr(spec, "owner", "") or None,
            "reviewer": getattr(spec, "reviewer", "") or None,
            "verdict": data.get("verdict") or self.verdict_raw,
            "impact": self.impact or data.get("impact"),
            "impact_reason": data.get("impact_reason"),
            "scale": data.get("scale"), "scale_reason": data.get("scale_reason"),
            "standards": data.get("standards"),
            "merged": result.get("merged") if "merged" in result else None,
            "stopped": result.get("stopped") or None, "error": self.error,
            "findings": data.get("findings") if data else None,
            "documents": data.get("documents"),
            "output": [t for t, tag in self.log if tag not in ("tool",)],
        }

    @property
    def branches(self):
        """"source → dest", or "" until the pipeline has looked the PR up."""
        if getattr(self.spec, "kind", "pr") == "docs":
            return self.spec.branches_text
        return f"{self.source} → {self.dest}" if self.source and self.dest else ""

    def summary_line(self):
        """One-line description for the detail screen header."""
        if getattr(self.spec, "kind", "pr") == "docs":
            return "  ·  ".join([self.spec.label, f"job ID {self.job_id}",
                                 self.spec.branches_text, self.spec.summary_bits()])
        bits = [self.spec.label, f"job ID {self.job_id}",
                "GitLab" if self.spec.is_gitlab else self.spec.region]
        if self.spec.origin == "codegen":
            bits.append("via codegen")
        if self.branches:
            bits.append(self.branches)
        if self.author:
            bits.append(f"by {self.author}")
        if not self.spec.do_review:
            bits.append("no review")
        if self.spec.dry_run:
            bits.append("dry run")
        if not self.spec.do_merge:
            bits.append("no auto-merge")
        return "  ·  ".join(bits)


# The key of the "done" payload that carries run_data(): data for the job, not text.
RUN_DATA_KEY = "_data"


def run_data(summary):
    """What a finished run reported, as plain JSON-safe data for the job (the live chat
    reads it): findings, per-document results, and the review's verdict, impact and
    scale. Never raises."""
    try:
        review = summary.get("review")
        out = {"findings": list(summary.get("findings") or [])}
        if review is not None:
            out.update(verdict=getattr(review, "verdict_raw", "") or "",
                       impact=getattr(review, "impact_score", "") or "",
                       impact_reason=getattr(review, "impact_reason", "") or "",
                       scale=getattr(review, "scale", "") or "",
                       scale_reason=getattr(review, "scale_reason", "") or "",
                       standards=getattr(review, "standards", "") or "")
        results = summary.get("results")
        if isinstance(results, list):
            out["documents"] = [{
                "file": r.get("path") or r.get("file"), "decision":
                docreview.decision_words(r.get("verdict_key")),
                "counts": docreview.counts_text(r.get("counts") or {}),
                "impact": r.get("impact"), "scale": r.get("scale"), "copy": r.get("copy"),
                "standards": r.get("standards"),
            } for r in results if isinstance(r, dict)]
            if summary.get("decision"):
                out["verdict"] = docreview.verdict_label(summary["decision"])
            out["scale"] = summary.get("scale") or ""
        return out
    except Exception:  # noqa: BLE001
        return {}


# Sentinel for retry(): "leave the job's model as it is" (None means the default model).
KEEP_MODEL = object()


def find_jobs(jobs, query):
    """The jobs a job-list search for `query` finds, in the order given."""
    return [j for j in jobs if j.matches(query)]


class DuplicateJob(Exception):
    """Raised when the same pull request is already being worked on."""


class JobManager:
    """Owns the jobs and decides which of them run.

    Threading contract: worker threads only ever put messages on the queue.
    Every mutation of a Job happens on the UI thread, driven by draining that
    queue — so the model can be read for repaint without locking.
    """

    def __init__(self, out_q, runner=full_pipeline, max_parallel=None,
                 followup_runner=run_followup, on_outcome=None,
                 doc_followup_runner=docreview.run_followup, chat_runner=None):
        self.out_q = out_q
        # Optional observer, called on the worker thread as
        # on_outcome(job, summary=..., error=..., stopped=...) once a run ends.
        # Observing only: it cannot change the job, and anything it raises is
        # swallowed. None (the default) leaves behaviour exactly as it was.
        self._on_outcome = on_outcome
        self._followup_runner = followup_runner
        self._doc_followup_runner = doc_followup_runner
        self._chat_runner = chat_runner or chat_mod.ask
        self._chat_sources = chatsources.for_job
        self.jobs = {}           # insertion-ordered; doubles as display order
        self._next_id = 1
        self._runner = runner
        # Explicit None check, not `or`: a limit of 0 is meaningful
        # (start nothing) and `or` would silently turn it into the default.
        self.max_parallel = (MAX_PARALLEL_JOBS if max_parallel is None
                             else max_parallel)

    # ----- admission -----
    def duplicate_of(self, spec):
        """An active job for the same PR, if there is one.

        Two jobs on one PR would both read-modify-write its description, and
        CodeCommit offers no revision token on that call, so one set of
        findings would be silently lost.
        """
        for job in self.jobs.values():
            if getattr(spec, "kind", "pr") == "docs" or \
                    getattr(job.spec, "kind", "pr") == "docs":
                # A documentation review is the same job only when it covers
                # the very same documents on the same branches.
                if job.is_active and getattr(job.spec, "kind", "pr") == \
                        getattr(spec, "kind", "pr") and \
                        job.spec.dup_key == spec.dup_key:
                    return job
                continue
            if job.is_active and job.spec.dup_key == spec.dup_key:
                return job
        return None

    def shares_clone_with(self, spec):
        """An active job using the same checkout — allowed, but worth saying.

        Safe because the git sync serialises on the clone path, but the second
        job may visibly wait, so the UI warns rather than letting it look hung.
        """
        for job in self.jobs.values():
            if job.is_active and job.spec.clone_key == spec.clone_key:
                return job
        return None

    def create(self, spec):
        dup = self.duplicate_of(spec)
        if dup is not None:
            raise DuplicateJob(
                f"{spec.label} is already being reviewed (job #{dup.id}).")
        job = Job(self._next_id, spec, self.out_q)
        self.jobs[job.id] = job
        self._next_id += 1
        return job

    def retry(self, job_id, model=KEEP_MODEL, persona=None):
        """Rerun a finished job in place — same id, row and spec.

        Same repo, PR id, flags and model — nothing to retype, and nothing
        new added to the jobs list either: the existing row just goes back
        to queued with its log and verdict cleared. The rerun automatically
        scopes itself to what changed since PRISM's last completed review of
        this PR (orchestrator._incremental_context), backed by PRISM's own
        local record of what it actually reviewed, so a retry costs no more
        context-setup than any other job.

        `persona` (a document review only) reruns it as another reviewer role - a key
        of docreview.PERSONAS; None keeps the job's own. ValueError for an unknown
        role, or a role given for a pull request, which has none.

        None for a job that isn't finished yet (nothing to retry) or doesn't
        exist. Raises DuplicateJob exactly like create() when another active
        job already covers this PR — retry is not a way around that guard.
        The job being retried is itself terminal at this point, so it can
        never be the one duplicate_of() finds.
        """
        job = self.jobs.get(job_id)
        if job is None or not job.is_terminal:
            return None
        spec = job.spec
        if persona is not None:
            if getattr(spec, "kind", "pr") != "docs":
                raise ValueError("Only a document review has a reviewer role.")
            persona = docreview.normalise_persona(persona)
            if persona != spec.persona:
                spec = dataclasses.replace(spec, persona=persona)
        if model is not KEEP_MODEL and (model or None) != spec.model:
            # The next iteration may use a different model; everything else
            # in the spec stays exactly as it was.
            spec = dataclasses.replace(spec, model=model or None)
        dup = self.duplicate_of(spec)
        if dup is not None:
            raise DuplicateJob(
                f"{spec.label} is already being reviewed (job #{dup.id}).")
        # Only now: a refused retry leaves the job exactly as it was. It keeps its job ID.
        job.spec = spec
        job.status = QUEUED
        job.stages = {}
        job.verdict_raw = ""
        job.verdict_key = ""
        job.impact = ""
        job.source = job.dest = job.author = ""
        job.session_id = ""
        self._cancel_followup(job)
        job.tokens = {}
        job.pending_question = None
        job.result = None
        job.error = None
        job.log.clear()
        job.control = RunControl()
        job.ask = AskBridge(job)
        job.thread = None
        job.pop_note()   # drop any note left over from the previous run
        job.data = {}    # the chat thread itself is kept: it lives as long as the job
        return job

    # ----- scheduling -----
    @property
    def running_count(self):
        return sum(1 for j in self.jobs.values()
                   if j.status in (RUNNING, NEEDS_INPUT, STOPPING))

    def pump(self):
        """Start queued jobs while there is room. Call from the UI thread."""
        started = []
        for job in self.jobs.values():
            if self.running_count >= self.max_parallel:
                break
            if job.status == QUEUED:
                self._launch(job)
                started.append(job)
        return started

    def _launch(self, job):
        job.status = RUNNING
        job.thread = threading.Thread(target=self._work, args=(job,), daemon=True)
        job.thread.start()

    def _work(self, job):
        """Worker body. Posts messages; never touches Job state directly."""
        try:
            # A documentation job brings its own pipeline; a pull request uses
            # the one this manager was built with, exactly as before.
            runner = getattr(job.spec, "runner", None) or self._runner
            summary = runner(
                emit=lambda m, tag=None: job.emit_event("log", (m, tag)),
                progress=lambda stage, state: job.emit_event("progress", (stage, state)),
                control=job.control, ask=job.ask.ask, get_note=job.pop_note,
                job_id=job.job_id, **job.spec.pipeline_kwargs())
            # "tokens" is a small dict for the UI to render directly; every
            # other field is a short display string, same as before. The
            # structured findings are data for `prism jobs --json`, not text.
            payload = {k: v if isinstance(v, bool) else str(v)[:160]
                      for k, v in summary.items() if k not in INTERNAL_SUMMARY_KEYS}
            payload["tokens"] = summary.get("tokens")
            # What the run reported, as data for the live chat - not display text.
            payload[RUN_DATA_KEY] = run_data(summary)
            job.emit_event("done", payload)
            self._notify(job, summary=summary)
        except Exception as e:  # noqa: BLE001
            # Stopping kills the child process, so the failure it provokes is
            # the stop, not a real error — report it as such.
            if job.control.cancelled():
                job.emit_event("stopped", None)
                self._notify(job, stopped=True)
            else:
                job.emit_event("error", str(e)[:3000])
                self._notify(job, error=e)

    def _notify(self, job, **outcome):
        if self._on_outcome is None:
            return
        try:
            self._on_outcome(job, **outcome)
        except Exception:  # noqa: BLE001
            pass

    # ----- follow-up questions after a job has finished -----
    def start_followup(self, job_id, text):
        """Put a question or request to the reviewer about a finished job.

        Returns the job when it was started, None when it can't be (unknown or
        still-active job — an active one takes a steering note instead — empty
        text, or a follow-up already in flight). The reviewer is read-only and
        nothing acts on its answer, so this can never merge or write anything;
        it only adds to the job's log. Call from the UI thread.
        """
        job = self.jobs.get(job_id)
        text = (text or "").strip()
        # A live-chat answer may be continuing the same reviewer session: never run two
        # turns on one session at once.
        if job is None or not job.is_terminal or job.followup_busy or job.chat_busy \
                or not text:
            return None
        job.followup_busy = True
        job.followup_control = RunControl()
        threading.Thread(target=self._followup_work, args=(job, text, job.followup_control),
                         daemon=True).start()
        return job

    def _cancel_followup(self, job):
        ctl, job.followup_control = job.followup_control, None
        job.followup_busy = False
        if ctl is not None:
            ctl.cancel()

    def _followup_work(self, job, text, control):
        """Worker body for one follow-up turn. Posts messages only."""
        def emit(m, tag=None):
            # A follow-up that was cancelled (job retried or removed) must not
            # write into whatever the job is doing now.
            # Token lines are dropped: they would replace the job's total with
            # this one turn's figure (there is no meter carrying the rest).
            if tag != "tokens" and not control.cancelled():
                job.emit_event("log", (m, tag))
        try:
            result = job.result if isinstance(job.result, dict) else {}
            outcome = ("merged" if result.get("merged") is True
                       else f"not merged ({result.get('stopped') or job.status})")
            spec = job.spec
            if getattr(spec, "kind", "pr") == "docs":
                reply, sid = self._doc_followup_runner(
                    text, spec.project_dir, model=spec.model,
                    session_id=job.session_id or None, emit=emit, control=control)
            else:
                reply, sid = self._followup_runner(
                    text, spec.project_dir, spec.repo_name, spec.pr_id,
                    local_repo=spec.local_repo, region=spec.region, model=spec.model,
                    session_id=job.session_id or None, outcome=outcome,
                    emit=emit, control=control,
                    **({"host": spec.host, "host_url": spec.host_url}
                       if spec.is_gitlab else {}))
            if sid:
                emit(sid, "session")
            reason = followup_declined(reply)
            if reason:
                emit(f"⛔ Not actioned — {reason}", "warn")
            else:
                emit("✔ Follow-up answered.", "ok")
        except Cancelled:
            emit("■ Follow-up stopped.", "warn")
        except Exception as e:  # noqa: BLE001
            emit(f"⚠ Follow-up failed: {str(e)[:500]}", "err")
        finally:
            if not control.cancelled():
                job.emit_event("followup_done", None)

    # ----- live chat, in any state -----
    def start_chat(self, job_id, text):
        """Ask the live chat a question about a job, whatever state it is in.

        Returns the job when the question was taken, None when it cannot be (unknown
        job, empty text, or an answer still on its way). It never queues a steering
        note, never counts as a steering round and never touches the running review:
        while the job is active the answer comes from a snapshot of its details in a
        session of its own. Call from the UI thread (the snapshot reads the Job).
        """
        job = self.jobs.get(job_id)
        text = (text or "").strip()
        if job is None or not text or job.chat_busy:
            return None
        # The conversation so far, before this question: what a follow-up refers to.
        history = list(job.chat)
        job.chat.append(("you", text))
        job.chat_busy = True
        job.chat_control = RunControl()
        info = job.chat_details()
        details = chat_mod.snapshot(info)
        # The reviewer's own session only once it is finished with and free.
        session = job.session_id if (job.is_terminal and not job.followup_busy) else None
        threading.Thread(target=self._chat_work,
                         args=(job, text, details, session, job.chat_control, history,
                               list(info.get("findings") or [])),
                         daemon=True).start()
        return job

    def _chat_work(self, job, text, details, session, control, history=(), findings=()):
        spec = job.spec
        try:
            # The documents are read here, off the UI thread: git and Word/PDF reads.
            sources = self._chat_sources(spec, findings, job.job_id)
            reply, _how = self._chat_runner(
                text, details, spec.label, getattr(spec, "kind", "pr"), spec.project_dir,
                model=spec.model, session_id=session or None, control=control,
                history=list(history), sources=sources)
        except Cancelled:
            reply = None
        except Exception as e:  # noqa: BLE001
            reply = f"I couldn't answer that just now ({str(e)[:300]})."
        if not control.cancelled():
            job.emit_event("chat", reply)

    def _cancel_chat(self, job):
        ctl, job.chat_control = job.chat_control, None
        job.chat_busy = False
        if ctl is not None:
            ctl.cancel()

    # ----- control -----
    def stop(self, job_id):
        job = self.jobs.get(job_id)
        if job is None or job.is_terminal:
            return None
        if job.status == QUEUED:
            # Never started: no thread to unwind.
            job.status = STOPPED
            return job
        job.status = STOPPING
        job.control.cancel()      # also releases a parked question
        return job

    def stop_all(self):
        for job_id in list(self.jobs):
            self.stop(job_id)
        for job in self.jobs.values():
            self._cancel_followup(job)
            self._cancel_chat(job)

    def remove(self, job_id):
        """Drop a finished job. Active jobs are stopped first."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        if job.is_active:
            self.stop(job_id)
        self._cancel_followup(job)
        self._cancel_chat(job)
        self.jobs.pop(job_id, None)

    def active_jobs(self):
        return [j for j in self.jobs.values() if j.is_active]
