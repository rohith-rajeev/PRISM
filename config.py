"""Small persistent app-level settings — the person's name and the Google Chat
webhook URLs. One JSON file, stdlib only.

Deliberately separate from a Job: a Job snapshots its settings once, at
creation, and never changes again once running, but this is the opposite —
a machine-wide setting the user configures once and expects every future
job to just pick up, including ones from a session that hasn't started yet.
"""
import getpass
import json
import re
from pathlib import Path

CONFIG_DIR = Path.home() / ".prism"
CONFIG_PATH = CONFIG_DIR / "config.json"


def load():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def save(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def get_webhook_url():
    return (load().get("google_chat_webhook_url") or "").strip()


def set_webhook_url(url):
    cfg = load()
    cfg["google_chat_webhook_url"] = (url or "").strip()
    save(cfg)


# The webhook for document-review alerts is separate from the one above: a team
# usually wants pull-request outcomes and document reviews in different spaces.
# Empty means "no alerts" - nothing else changes.
def get_docs_webhook_url():
    return (load().get("google_chat_docs_webhook_url") or "").strip()


def set_docs_webhook_url(url):
    cfg = load()
    cfg["google_chat_docs_webhook_url"] = (url or "").strip()
    save(cfg)


# ---- who is reviewing ----------------------------------------------------------
# One machine-wide name, asked for the first time PRISM starts and kept in this
# file from then on - an upgrade never touches ~/.prism, so it survives every
# release. It labels the window and is written onto what a review produces (the
# PR description, the Chat cards, the comments in a reviewed document), so a
# review can always be traced to the person who ran it.
MAX_USERNAME = 60
# Anything that could break out of the places the name is written: markup (it ends
# up inside an HTML comment in a document and Markdown in a PR description) and
# control characters (a newline would start a new line of the description).
_USERNAME_STRIP = re.compile(r"[<>`\x00-\x1f\x7f]")


def clean_username(text):
    """The name as it will be stored and shown: one line, no markup, bounded."""
    text = _USERNAME_STRIP.sub(" ", text if isinstance(text, str) else "")
    return " ".join(text.split())[:MAX_USERNAME].strip()


def get_username():
    """The saved name, or "" when the person has not given one yet."""
    value = load().get("username")
    return clean_username(value) if isinstance(value, str) else ""


def set_username(name):
    cfg = load()
    cfg["username"] = clean_username(name)
    save(cfg)


def system_username():
    """The operating-system login, as a suggestion. "" when it cannot be read."""
    try:
        return clean_username(getpass.getuser())
    except Exception:  # noqa: BLE001 - no login name is a normal state in a container
        return ""


def effective_username():
    """The name to put on a review: the saved one, else the machine login.

    A run that starts before anyone has answered the first-start question (a
    command-line job over ssh, say) is still attributed to somebody rather than
    to no one. The desktop app asks on first start, so there it is always saved.
    """
    return get_username() or system_username()


# ---- the owner of a document review request -------------------------------------
# The person who answers a document review (not the person who ran it). Asked on
# every document review; the last one given pre-fills the next form (FR-OWN-2).
def get_last_owner():
    value = load().get("last_doc_owner")
    return clean_username(value) if isinstance(value, str) else ""


def set_last_owner(name):
    name = clean_username(name)
    if not name:
        return
    cfg = load()
    cfg["last_doc_owner"] = name
    save(cfg)


# ---- codegen integration -------------------------------------------------------
# Optional and OFF by default: PRISM opens no port and touches nothing unless
# the person turns this on. Stored under one "codegen" key so the file stays
# readable by older PRISM versions, which ignore keys they don't know.
CODEGEN_DEFAULTS = {
    "enabled": False,
    # 0 = let the OS pick a free port; it is published in the discovery file,
    # so nothing needs to know it in advance.
    "port": 0,
    # Ceiling on review rounds for one PR, so a PRISM <-> codegen disagreement can
    # never loop forever (and burn tokens) without a human looking at it.
    "max_iterations": 5,
    # Whether PRISM may merge an approved PR it was handed by codegen. Off means
    # "review only": codegen is told the PR is approved and a human merges.
    "auto_merge": True,
}


def get_codegen():
    """The codegen settings with defaults filled in and values coerced to safe types."""
    raw = load().get("codegen")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(CODEGEN_DEFAULTS)
    out["enabled"] = raw.get("enabled") is True
    out["auto_merge"] = raw.get("auto_merge") is not False
    for key in ("port", "max_iterations"):
        try:
            out[key] = int(raw.get(key, CODEGEN_DEFAULTS[key]))
        except (TypeError, ValueError):
            out[key] = CODEGEN_DEFAULTS[key]
    if not 0 <= out["port"] <= 65535:
        out["port"] = CODEGEN_DEFAULTS["port"]
    out["max_iterations"] = max(1, min(out["max_iterations"], 50))
    return out


def set_codegen(**changes):
    """Merge `changes` into the saved codegen settings (unknown keys are ignored)."""
    cfg = load()
    current = cfg.get("codegen") if isinstance(cfg.get("codegen"), dict) else {}
    current = dict(current)
    current.update({k: v for k, v in changes.items() if k in CODEGEN_DEFAULTS})
    cfg["codegen"] = current
    save(cfg)


# ---- command-line defaults -----------------------------------------------------
# What `prism run <pr>` assumes when a flag is not given, so the same few
# choices are not retyped on every run (or on every machine reached over ssh).
# Stored under one "cli" key in this same file — the desktop app reads the file
# too and ignores keys it does not use. Empty text means "not set": the CLI then
# works it out from the current folder (see prism_cli.resolve_target).
# `requirements`, `persona`, `commit` and `push` are for `prism docs` (document review);
# the rest are shared by both commands (`model`, `instructions`, `dry_run`) or for `prism run`.
CLI_TEXT_KEYS = ("project_dir", "repo", "region", "model", "instructions", "requirements",
                 "persona")
CLI_FLAG_KEYS = ("review", "describe", "merge", "sync", "dry_run", "commit", "push")
CLI_DEFAULTS = dict({k: "" for k in CLI_TEXT_KEYS},
                    review=True, describe=True, merge=True, sync=True, dry_run=False,
                    commit=True, push=True)


def get_cli():
    """The CLI defaults with every key present and coerced to a safe type."""
    raw = load().get("cli")
    raw = raw if isinstance(raw, dict) else {}
    out = dict(CLI_DEFAULTS)
    for key in CLI_TEXT_KEYS:
        value = raw.get(key)
        out[key] = value.strip() if isinstance(value, str) else ""
    for key in CLI_FLAG_KEYS:
        value = raw.get(key)
        out[key] = value if isinstance(value, bool) else CLI_DEFAULTS[key]
    return out


def set_cli(**changes):
    """Merge `changes` into the saved CLI defaults; unknown keys are ignored."""
    cfg = load()
    current = cfg.get("cli") if isinstance(cfg.get("cli"), dict) else {}
    current = dict(current)
    current.update({k: v for k, v in changes.items() if k in CLI_DEFAULTS})
    cfg["cli"] = current
    save(cfg)


def unset_cli(*keys):
    cfg = load()
    current = cfg.get("cli") if isinstance(cfg.get("cli"), dict) else {}
    cfg["cli"] = {k: v for k, v in current.items() if k not in keys}
    save(cfg)
