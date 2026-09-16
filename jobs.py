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
import threading
from dataclasses import dataclass
from pathlib import Path

from orchestrator import REGION_DEFAULT, RunControl, full_pipeline

# Each job is a full agent run — model calls, AWS, git. Running an unbounded
# number at once invites provider rate limits and spawns unbounded child
# processes, so extra jobs queue and start as slots free.
MAX_PARALLEL_JOBS = 3

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
    do_update_desc: bool = True
    do_merge: bool = True
    do_sync: bool = True
    dry_run: bool = False

    @property
    def label(self):
        return f"{self.repo_name} #{self.pr_id}"

    @property
    def clone_key(self):
        """Resolved clone path, for spotting two jobs sharing one checkout."""
        target = self.local_repo or self.project_dir
        try:
            return str(Path(target).expanduser().resolve())
        except Exception:  # noqa: BLE001
            return str(target)

    def summary_bits(self):
        """Short descriptor for a jobs-list row."""
        bits = [self.region]
        if self.dry_run:
            bits.append("dry run")
        if not self.do_merge:
            bits.append("no auto-merge")
        return " · ".join(bits)

    def pipeline_kwargs(self):
        return dict(project_dir=self.project_dir, repo_name=self.repo_name,
                    pr_id=self.pr_id, local_repo=self.local_repo,
                    region=self.region, model=self.model,
                    do_update_desc=self.do_update_desc, do_merge=self.do_merge,
                    do_sync=self.do_sync, dry_run=self.dry_run)


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

    def ask(self, question):
        self._event.clear()
        self._answer = None
        self.job.emit_event("ask", question)
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
        self.pending_question = None
        self.result = None
        self.error = None
        self.log = collections.deque(maxlen=LOG_LINES_PER_JOB)
        self.control = RunControl()
        self.ask = AskBridge(self)
        self.thread = None

    # ----- plumbing -----
    def emit_event(self, kind, payload):
        """Post to the UI queue. Called from the worker thread only."""
        self._out_q.put((self.id, kind, payload))

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

    def summary_line(self):
        """One-line description for the detail screen header."""
        bits = [self.spec.label, self.spec.region]
        if self.spec.dry_run:
            bits.append("dry run")
        if not self.spec.do_merge:
            bits.append("no auto-merge")
        return "  ·  ".join(bits)


class DuplicateJob(Exception):
    """Raised when the same pull request is already being worked on."""


class JobManager:
    """Owns the jobs and decides which of them run.

    Threading contract: worker threads only ever put messages on the queue.
    Every mutation of a Job happens on the UI thread, driven by draining that
    queue — so the model can be read for repaint without locking.
    """

    def __init__(self, out_q, runner=full_pipeline, max_parallel=None):
        self.out_q = out_q
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
            if (job.is_active and job.spec.repo_name == spec.repo_name
                    and str(job.spec.pr_id) == str(spec.pr_id)):
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
            summary = self._runner(
                emit=lambda m: job.emit_event("log", m),
                progress=lambda stage, state: job.emit_event("progress", (stage, state)),
                control=job.control, ask=job.ask.ask,
                **job.spec.pipeline_kwargs())
            job.emit_event("done", {k: v if isinstance(v, bool) else str(v)[:160]
                                    for k, v in summary.items() if k != "review"})
        except Exception as e:  # noqa: BLE001
            # Stopping kills the child process, so the failure it provokes is
            # the stop, not a real error — report it as such.
            if job.control.cancelled():
                job.emit_event("stopped", None)
            else:
                job.emit_event("error", str(e)[:3000])

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

    def remove(self, job_id):
        """Drop a finished job. Active jobs are stopped first."""
        job = self.jobs.get(job_id)
        if job is None:
            return
        if job.is_active:
            self.stop(job_id)
        self.jobs.pop(job_id, None)

    def active_jobs(self):
        return [j for j in self.jobs.values() if j.is_active]
