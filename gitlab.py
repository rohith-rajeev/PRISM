"""GitLab merge-request support: where a repository is hosted, the token to
talk to it with, and the handful of REST API v4 calls PRISM's pipeline needs.

PRISM began as a CodeCommit-only tool. A GitLab merge request is the same idea
(a source branch asking to land on a target branch), so rather than teach the
pipeline a second vocabulary, get_pr() below returns a merge request under the
very keys orchestrator.get_pr() uses for a CodeCommit pull request. The
pipeline can then branch on classify_remote()'s "host" in the few places that
truly differ (fetching, the merge call itself) and share everything else.

Stdlib only, and deliberately free of Tkinter, in the same spirit as
updater.py and notifier.py, so the pipeline can use it without the UI.

The token is a secret that ends up in a request header, never in a URL, and
every error message built here is passed through redact() before it leaves
this module: error text gets shown in the UI, written to job logs and posted
to chat, and none of those places should ever carry a credential.
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import config
from updater import _ssl_context

HOST_CODECOMMIT = "codecommit"
HOST_GITLAB = "gitlab"
HOST_UNSUPPORTED = "unsupported"

API_PREFIX = "/api/v4"
DEFAULT_TIMEOUT = 30

# Env var and config key naming extra self-hosted GitLab hostnames whose names
# give no hint (git.corp.example, code.example.org): there is no reliable way
# to recognise those from the URL alone, so the person has to say so.
HOSTS_ENV = "PRISM_GITLAB_HOSTS"
HOSTS_CONFIG_KEY = "gitlab_hosts"
TOKEN_CONFIG_KEY = "gitlab_token"
# GITLAB_TOKEN first because it is what glab and most CI images already set;
# the PRISM_-prefixed one lets someone use a different token for PRISM alone.
TOKEN_ENV_VARS = ("GITLAB_TOKEN", "PRISM_GITLAB_TOKEN")

REDACTED = "<redacted-token>"
# Personal access tokens. Newer GitLab releases append dot-separated routing
# parts ("glpat-xxxx.01.yyyy"), so those are swallowed too: leaving half a
# token in a log is still leaking it.
_PAT_RE = re.compile(r"glpat-[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)*")
# Too-short "tokens" are not replaced verbatim: redacting every "a" in an
# error message would make it unreadable and protect nothing real.
_MIN_REDACT_LEN = 4

# ---- CodeCommit remotes --------------------------------------------------------
# git-remote-codecommit's two forms, and the plain HTTPS/SSH endpoint.
_CC_REGION_RE = re.compile(r"^codecommit::([A-Za-z0-9-]+)://(?:[^@/]+@)?([^/@]+?)/*$")
_CC_PLAIN_RE = re.compile(r"^codecommit://(?:[^@/]+@)?([^/@]+?)/*$")
_CC_ENDPOINT_RE = re.compile(
    r"^(?:https|ssh)://(?:[^@/]+@)?git-codecommit\.([A-Za-z0-9-]+)\.amazonaws\.com(?:\.cn)?"
    r"(?::\d+)?/v1/repos/([^/]+?)/*$", re.IGNORECASE)

# scp-like ssh syntax: [user@]host:path. The (?!//) keeps "https://..." out,
# and requiring the host to be more than one character keeps a Windows drive
# path ("C:/src/repo") from being read as a host called "C".
_SCP_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s]{2,}):(?!//)(.+)$")

# Statuses GitLab reports while it is still working out mergeability. Asking
# again a moment later usually gets a real answer.
_PENDING_STATUSES = frozenset({
    "unchecked", "checking", "preparing", "approvals_syncing",
    "cannot_be_merged_recheck",
})
_POLL_ATTEMPTS = 5
_POLL_INTERVAL = 1.0


class GitLabError(RuntimeError):
    """A GitLab call failed. str() is a human-readable message that includes
    GitLab's own error text; .status is the HTTP status, or None when GitLab
    was never reached (DNS, TLS, timeout).
    """

    def __init__(self, message, status=None):
        # Defensive second pass: whoever built the message should already have
        # redacted it, but an exception is exactly the thing that gets logged.
        super().__init__(redact(str(message), token=""))
        self.status = status

    def __repr__(self):
        return f"GitLabError({str(self)!r}, status={self.status!r})"


# ---- host detection ------------------------------------------------------------

def _empty(host):
    return {"host": host, "base_url": "", "project": "", "region": ""}


def _normalise_host(entry):
    """A configured host entry reduced to a bare lowercase hostname.

    People paste whatever they have to hand: "gitlab.corp", "GitLab.Corp:8443",
    "https://gitlab.corp/". All of those mean the same host.
    """
    entry = (entry or "").strip().lower() if isinstance(entry, str) else ""
    if not entry:
        return ""
    if "://" in entry:
        return (urllib.parse.urlsplit(entry).hostname or "").lower()
    entry = entry.split("/", 1)[0].rsplit("@", 1)[-1]
    if entry.startswith("["):  # bracketed IPv6, maybe with a port
        return entry[1:].split("]", 1)[0]
    if entry.count(":") == 1:
        entry = entry.split(":", 1)[0]
    return entry


def _configured_hosts(extra_hosts):
    hosts = set()
    for entry in extra_hosts or ():
        hosts.add(_normalise_host(entry))
    for entry in os.environ.get(HOSTS_ENV, "").split(","):
        hosts.add(_normalise_host(entry))
    try:
        saved = config.load().get(HOSTS_CONFIG_KEY) or []
    except Exception:  # noqa: BLE001 - a bad config must not stop detection
        saved = []
    if isinstance(saved, str):  # tolerate a hand-edited single string
        saved = [saved]
    if isinstance(saved, list):
        for entry in saved:
            hosts.add(_normalise_host(entry))
    hosts.discard("")
    return hosts


def _is_gitlab_host(hostname, extra_hosts):
    hostname = (hostname or "").lower()
    if not hostname:
        return False
    if hostname == "gitlab.com":
        return True
    # gitlab.example.com, gitlab-ce.corp.io, code.gitlab.internal: a label
    # that names the product is as close to a self-description as a URL gets.
    if any(label.startswith("gitlab") for label in hostname.split(".")):
        return True
    return hostname in _configured_hosts(extra_hosts)


def _project_path(path):
    """"/group/sub/proj.git/" -> "group/sub/proj", or "" when not a project."""
    path = urllib.parse.unquote(path or "").strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    segments = [s for s in path.split("/") if s]
    # GitLab projects always live in a namespace, so "host/proj" is not one.
    if len(segments) < 2:
        return ""
    return "/".join(segments)


def _format_host(hostname):
    return f"[{hostname}]" if ":" in hostname else hostname


def classify_remote(url, extra_hosts=None):
    """Work out who hosts a git remote and how to address it.

    Returns {"host", "base_url", "project", "region"}. An empty remote counts
    as CodeCommit, because callers treat "no remote" as the legacy
    CodeCommit/API-only mode PRISM has always had. Credentials embedded in the
    URL (https://user:token@host/...) are never copied into the result.
    """
    url = (url or "").strip()
    if not url:
        return _empty(HOST_CODECOMMIT)

    m = _CC_REGION_RE.match(url)
    if m:
        return {"host": HOST_CODECOMMIT, "base_url": "", "project": m.group(2),
                "region": m.group(1)}
    m = _CC_PLAIN_RE.match(url)
    if m:
        return {"host": HOST_CODECOMMIT, "base_url": "", "project": m.group(1),
                "region": ""}
    m = _CC_ENDPOINT_RE.match(url)
    if m:
        return {"host": HOST_CODECOMMIT, "base_url": "", "project": m.group(2),
                "region": m.group(1).lower()}

    if "://" in url:
        try:
            parts = urllib.parse.urlsplit(url)
            hostname = parts.hostname or ""
            port = parts.port
        except ValueError:  # malformed port and the like
            return _empty(HOST_UNSUPPORTED)
        scheme = parts.scheme.lower()
        if scheme in ("http", "https"):
            base = f"{scheme}://{_format_host(hostname)}" + (f":{port}" if port else "")
        elif scheme in ("ssh", "git+ssh", "ssh+git"):
            # The ssh port says nothing about where the web API listens.
            base = f"https://{_format_host(hostname)}"
        else:  # file://, git://, ftp://...
            return _empty(HOST_UNSUPPORTED)
        path = parts.path
    else:
        m = _SCP_RE.match(url)
        if not m:  # a local path
            return _empty(HOST_UNSUPPORTED)
        hostname = m.group(1).lower()
        base = f"https://{hostname}"
        path = m.group(2)

    if not _is_gitlab_host(hostname, extra_hosts):
        return _empty(HOST_UNSUPPORTED)
    project = _project_path(path)
    if not project:
        return _empty(HOST_UNSUPPORTED)
    return {"host": HOST_GITLAB, "base_url": base, "project": project, "region": ""}


# ---- credentials ---------------------------------------------------------------

def get_token(base_url=""):
    """The GitLab token to use, or "" when none is set.

    base_url is accepted so per-host tokens can be added later without changing
    callers; today one token serves every GitLab host.
    """
    for name in TOKEN_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    try:
        value = config.load().get(TOKEN_CONFIG_KEY)
    except Exception:  # noqa: BLE001
        value = None
    return value.strip() if isinstance(value, str) else ""


def set_token(token):
    """Save the token in ~/.prism/config.json; "" removes it."""
    cfg = config.load()
    token = (token or "").strip()
    if token:
        cfg[TOKEN_CONFIG_KEY] = token
    else:
        cfg.pop(TOKEN_CONFIG_KEY, None)
    config.save(cfg)


def mask(token):
    """A token as it can safely be shown: "glpat-…abcd", or "****" when it is
    too short to reveal any of it without revealing most of it.
    """
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) >= 14:
        return f"{token[:6]}…{token[-4:]}"
    return "****"


def redact(text, token=None):
    """text with the token (given, or the configured one) and anything shaped
    like a GitLab personal access token replaced by "<redacted-token>".
    """
    text = "" if text is None else str(text)
    if token is None:
        token = get_token()
    token = (token or "").strip()
    if len(token) >= _MIN_REDACT_LEN:
        text = text.replace(token, REDACTED)
    return _PAT_RE.sub(REDACTED, text)


# ---- REST client ---------------------------------------------------------------

def _encode_id(project):
    """A project path as GitLab's :id path parameter: "a/b" -> "a%2Fb"."""
    return urllib.parse.quote(str(project), safe="")


def _flatten(value):
    """GitLab's error payloads come as a string, a list, or a dict of field
    -> [problems] (validation errors). Turn any of them into one line.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "; ".join(t for t in (_flatten(v) for v in value) if t)
    if isinstance(value, dict):
        parts = []
        for key, val in value.items():
            text = _flatten(val)
            parts.append(f"{key}: {text}" if text else str(key))
        return "; ".join(parts)
    return str(value)


def _error_text(raw):
    """GitLab's own explanation from an error response body."""
    if not raw:
        return ""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    try:
        data = json.loads(text)
    except ValueError:
        # An HTML error page from a proxy is not worth quoting in full.
        return " ".join(text.split())[:300]
    if isinstance(data, dict):
        parts = [_flatten(data.get("message")), _flatten(data.get("error")),
                 _flatten(data.get("error_description"))]
        joined = " - ".join(p for p in parts if p)
        return joined or _flatten(data)
    return _flatten(data)


# What the status means for the person reading the message, beyond GitLab's
# own (often terse) words. 405/406/409 are the merge endpoint's refusals.
_STATUS_HINTS = {
    401: "the GitLab token is missing, wrong, expired or lacks the api scope",
    403: "the GitLab token lacks permission for this (it needs the api scope and "
         "a role on the project that allows it)",
    404: "not found, or the token cannot see it",
    405: "GitLab will not merge it in its current state (draft, closed, blocked "
         "or unapproved)",
    406: "GitLab could not merge it (conflicts, or the branch cannot be "
         "fast-forwarded)",
    409: "the source branch no longer points at the reviewed commit",
}


class Client:
    """A thin GitLab REST API v4 client: one instance per GitLab host."""

    def __init__(self, base_url, token=None, opener=None, timeout=DEFAULT_TIMEOUT,
                 sleep=None):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.token = (get_token(self.base_url) if token is None else (token or "")).strip()
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        # Injectable so tests of ff_mergeable's polling do not really wait.
        self.sleep = sleep or time.sleep

    def __repr__(self):
        return f"Client(base_url={self.base_url!r}, token={mask(self.token)!r})"

    def _fail(self, method, path, status, message):
        hint = _STATUS_HINTS.get(status)
        if hint and message:
            message = f"{message} ({hint})"
        elif hint:
            message = hint
        text = f"GitLab {method} {path} failed ({status}): {message or 'no detail given'}"
        return GitLabError(redact(text, self.token), status=status)

    def request(self, method, path, params=None, body=None):
        """Call the API; returns (status, parsed JSON or None).

        path is relative to /api/v4, e.g. "/projects/a%2Fb/merge_requests/3".
        Raises GitLabError for any non-2xx answer or when GitLab is unreachable.
        """
        method = method.upper()
        bare_path = path.split("?", 1)[0]
        url = self.base_url + API_PREFIX + path
        if params:
            query = urllib.parse.urlencode(
                {k: (("true" if v else "false") if isinstance(v, bool) else v)
                 for k, v in params.items() if v is not None}, doseq=True)
            if query:
                url += ("&" if "?" in url else "?") + query
        headers = {"Accept": "application/json"}
        if self.token:
            headers["PRIVATE-TOKEN"] = self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            resp = self.opener(req, timeout=self.timeout, context=_ssl_context())
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                raw = b""
            message = _error_text(raw) or str(getattr(exc, "reason", "") or "")
            raise self._fail(method, bare_path, exc.code, message) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise GitLabError(redact(
                f"GitLab {method} {bare_path} failed: could not reach {self.base_url} "
                f"({reason})", self.token)) from None
        except OSError as exc:  # socket.timeout / TimeoutError, connection reset
            raise GitLabError(redact(
                f"GitLab {method} {bare_path} failed: no answer from {self.base_url} "
                f"({exc or type(exc).__name__})", self.token)) from None

        try:
            status = getattr(resp, "status", None) or resp.getcode()
            raw = resp.read()
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
        if not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return status, None

    # ---- endpoints -------------------------------------------------------------

    def _get(self, path, params=None):
        return self.request("GET", path, params=params)[1] or {}

    def project(self, project):
        return self._get(f"/projects/{_encode_id(project)}")

    def current_user(self):
        return self._get("/user")

    def merge_request(self, project, iid, recheck=False):
        """The raw MR. include_diverged_commits_count makes GitLab report how
        many target commits the source lacks, which is what decides whether a
        fast-forward is possible at all.
        """
        params = {"include_diverged_commits_count": "true"}
        if recheck:
            # Without this a stale "mergeable" can be served from cache after
            # the target branch has moved on.
            params["with_merge_status_recheck"] = "true"
        return self._get(f"/projects/{_encode_id(project)}/merge_requests/{int(iid)}",
                         params=params)

    def update_description(self, project, iid, description):
        return self.request(
            "PUT", f"/projects/{_encode_id(project)}/merge_requests/{int(iid)}",
            body={"description": description})[1] or {}

    def merge(self, project, iid, sha):
        """Merge (fast-forward, on an ff project) the MR at exactly `sha`.

        sha is mandatory: GitLab refuses the merge (409) unless the source
        branch still points at it, which is what guarantees that the commit
        landing is the commit PRISM reviewed and not something pushed since.
        """
        sha = (sha or "").strip()
        if not sha:
            raise ValueError("merge() needs the reviewed commit's sha")
        return self.request(
            "PUT", f"/projects/{_encode_id(project)}/merge_requests/{int(iid)}/merge",
            body={"sha": sha, "squash": False,
                  "should_remove_source_branch": False})[1] or {}

    def get_pr(self, project, iid):
        """The MR under the same keys orchestrator.get_pr() uses for a
        CodeCommit pull request, plus a few GitLab-only extras.
        """
        mr = self.merge_request(project, iid)
        diff_refs = mr.get("diff_refs") or {}
        state = (mr.get("state") or "").lower()
        diverged = mr.get("diverged_commits_count")
        return {
            "repositoryName": _mr_project_path(mr, project),
            "sourceReference": mr.get("source_branch") or "",
            "destinationReference": mr.get("target_branch") or "",
            "sourceCommit": mr.get("sha") or "",
            # start_sha is the target-branch commit the MR's diff starts from:
            # the counterpart of CodeCommit's destinationCommit.
            "destinationCommit": diff_refs.get("start_sha") or "",
            "mergeBase": diff_refs.get("base_sha") or "",
            "isMerged": state == "merged",
            "title": mr.get("title") or "",
            "description": mr.get("description") or "",
            "status": "OPEN" if state == "opened" else state.upper(),
            # PRISM shows the part after the last "/" of an ARN; a bare
            # username passes through that unchanged.
            "authorArn": (mr.get("author") or {}).get("username") or "",
            "detailedMergeStatus": mr.get("detailed_merge_status") or mr.get("merge_status") or "",
            "hasConflicts": bool(mr.get("has_conflicts")),
            "divergedCommitsCount": diverged if isinstance(diverged, int) else None,
            "sourceProjectId": mr.get("source_project_id"),
            "targetProjectId": mr.get("target_project_id"),
            "webUrl": mr.get("web_url") or "",
            "draft": bool(mr.get("draft") or mr.get("work_in_progress")),
            "raw": mr,
        }

    def merge_method_allows_ff(self, project):
        """(True, reason) when the project merges by fast-forward only.

        PRISM only ever fast-forwards: the commit that lands must be the
        commit that was reviewed, which a merge commit or a squash is not.
        """
        p = self.project(project)
        method = p.get("merge_method") or ""
        if method != "ff":
            label = {"merge": "merge commit",
                     "rebase_merge": "merge commit with semi-linear history"}.get(method)
            described = f"'{method}' ({label})" if label else repr(method or "unknown")
            return False, (f"the project's merge method is {described}; "
                           f"PRISM only fast-forwards")
        if (p.get("squash_option") or "") == "always":
            return False, ("the project's squash option is 'always', so GitLab would "
                           "squash instead of fast-forwarding the reviewed commit(s)")
        return True, "the project's merge method is fast-forward"

    def ff_mergeable(self, project, iid):
        """(True, "fast-forward possible") only when GitLab could fast-forward
        the target to the MR's source right now; otherwise (False, why).
        """
        ok, why = self.merge_method_allows_ff(project)
        if not ok:
            return False, why

        mr = self.merge_request(project, iid, recheck=True)
        state = (mr.get("state") or "").lower()
        if state != "opened":
            return False, f"the merge request is {state or 'not open'}"

        # The recheck above runs asynchronously; give GitLab a few seconds to
        # finish rather than reporting "checking" as a reason not to merge.
        status = _merge_status(mr)
        for _ in range(_POLL_ATTEMPTS):
            if status not in _PENDING_STATUSES:
                break
            self.sleep(_POLL_INTERVAL)
            mr = self.merge_request(project, iid)
            status = _merge_status(mr)

        if (mr.get("state") or "").lower() != "opened":
            return False, f"the merge request is {mr.get('state') or 'not open'}"
        if mr.get("has_conflicts"):
            return False, "the merge request has conflicts with the target branch"
        diverged = mr.get("diverged_commits_count")
        if not isinstance(diverged, int):
            # Fail closed: without this number nothing says the target has not
            # moved past the source.
            return False, ("GitLab did not report how far the source branch is behind "
                           "the target, so a fast-forward cannot be confirmed")
        if diverged > 0:
            return False, f"the source branch is {diverged} commit(s) behind the target"
        if status in _PENDING_STATUSES:
            return False, (f"GitLab is still checking whether it can be merged "
                           f"({status}); try again shortly")
        # Older GitLab only has merge_status, whose "yes" is can_be_merged.
        if status == "mergeable" or (status == "can_be_merged"
                                     and not mr.get("detailed_merge_status")):
            return True, "fast-forward possible"
        return False, f"GitLab says: {status or 'unknown merge status'}"


def _merge_status(mr):
    return (mr.get("detailed_merge_status") or mr.get("merge_status") or "").lower()


def _mr_project_path(mr, project):
    """The target project's path_with_namespace for an MR.

    references.full ("group/sub/proj!3") is exact; web_url is the fallback
    (it can carry a relative URL root, hence second); the caller's own
    `project` argument is last.
    """
    full = ((mr.get("references") or {}).get("full") or "").strip()
    if "!" in full:
        return full.rsplit("!", 1)[0]
    web = mr.get("web_url") or ""
    if "/-/merge_requests/" in web:
        path = urllib.parse.urlsplit(web).path.split("/-/merge_requests/", 1)[0]
        if path.strip("/"):
            return path.strip("/")
    return "" if project is None else str(project)
