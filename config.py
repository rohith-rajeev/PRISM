"""Small persistent app-level settings — currently just the Google Chat
webhook URL. One JSON file, stdlib only.

Deliberately separate from a Job: a Job snapshots its settings once, at
creation, and never changes again once running, but this is the opposite —
a machine-wide setting the user configures once and expects every future
job to just pick up, including ones from a session that hasn't started yet.
"""
import json
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
