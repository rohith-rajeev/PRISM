"""Job IDs: one short ID per job, shown on its chat card and in the job lists.

A job ID is 10 lowercase hex characters (40 random bits), e.g. ``3f9c2a71be``. A job
keeps its ID for life: a retry is the same job and carries the same ID, so the ID on any
chat card the job posted always leads back to it. It is also how a document review's
commits are found later (``git log --grep PRISM-<job id>``). An ID already handed out by
this process is never handed out again, and across processes a clash is about one in a
trillion.

Stdlib only and free of Tkinter, so it is testable headlessly.
"""
import re
import secrets
import threading

LENGTH = 10

_ISSUED = set()
_LOCK = threading.Lock()
_JOB_ID_RE = re.compile(r"[0-9a-f]{%d}" % LENGTH)
# What a person may paste around an ID: "Job ID 3f9c2a71be" from a card's footer, or
# "PRISM-3f9c2a71be" from a commit message.
# "PRISM-" is taken off only in front of an ID, so a search for a repository called
# "prism-api" still searches for that name.
_PREFIX_RE = re.compile(r"^\s*(?:job[\s-]*id[\s:#]*|job[\s:#]+|prism-(?=[0-9a-f]{%d}\b))"
                        % LENGTH, re.IGNORECASE)


def new():
    """A fresh job ID that this process has never handed out before."""
    with _LOCK:
        while True:
            job_id = secrets.token_hex(LENGTH // 2)
            if job_id not in _ISSUED:
                _ISSUED.add(job_id)
                return job_id


def valid(job_id):
    """True for a well-formed job ID (exactly 10 lowercase hex characters)."""
    return isinstance(job_id, str) and _JOB_ID_RE.fullmatch(job_id) is not None


def normalise_query(text):
    """What a search box was given, ready to compare: trimmed, lower-case, and without
    the "Job ID " or "PRISM-" a pasted ID may carry."""
    return _PREFIX_RE.sub("", str(text or "")).strip().lower()


def matches(query, job_ids, label=""):
    """True when a search `query` finds a job with these IDs and this label.

    An empty query matches everything. Otherwise the query matches when it is part of
    one of the job's IDs (so the first few characters are enough), or part of its label,
    ignoring case.
    """
    q = normalise_query(query)
    if not q:
        return True
    if any(q in str(i).lower() for i in (job_ids or ()) if i):
        return True
    return q in str(label or "").lower()
