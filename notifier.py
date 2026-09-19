"""Post a brief PR-outcome summary to a Google Chat webhook, if one is
configured.

Stdlib only, and PRISM composes and sends this itself — no agent involved.
The content (verdict, impact, merged status) is already fully known by the
time the pipeline finishes, so an agent call here would only be one more
chance to hit a free-tier rate limit for zero benefit, the same reasoning
behind the PR-description write in orchestrator.py being direct too.

Deliberately free of Tkinter, in the same spirit as updater.py, so
orchestrator.py can use it without pulling in the UI.
"""
import json
import urllib.error
import urllib.request

from updater import _ssl_context

POST_TIMEOUT = 10

_VERDICT_GLYPH = {
    "approve": "✅", "approve-with-comments": "⚠️",
    "request-changes": "🔴", "block": "⛔",
}


def build_card(repo_name, pr_id, do_review, verdict_raw=None, verdict_key=None,
              impact_score=None, merged=None, reason=None):
    """A small Google Chat card: one header line, at most two widget rows.

    Kept to this shape deliberately — a card that scrolls off a phone screen
    defeats the point of a *brief* summary.
    """
    if merged:
        subtitle = "✅ Merged"
    elif merged is False:
        subtitle = "⛔ Not merged" + (f" — {reason}" if reason else "")
    else:
        subtitle = "⚠️ Unknown outcome" + (f" — {reason}" if reason else "")

    widgets = []
    if not do_review:
        widgets.append({"decoratedText": {
            "topLabel": "Review", "text": "Skipped by configuration"}})
    elif verdict_raw:
        glyph = _VERDICT_GLYPH.get(verdict_key, "")
        widgets.append({"decoratedText": {
            "topLabel": "Verdict", "text": f"{glyph} {verdict_raw}".strip()}})
        if impact_score:
            widgets.append({"decoratedText": {
                "topLabel": "Impact", "text": f"{impact_score}/10"}})
    else:
        widgets.append({"decoratedText": {
            "topLabel": "Review", "text": "No verdict parsed"}})

    return {
        "header": {"title": f"PRISM  ·  {repo_name} #{pr_id}", "subtitle": subtitle},
        "sections": [{"widgets": widgets}],
    }


def post_summary(webhook_url, emit=None, **card_kwargs):
    """Best-effort POST of the summary card. Never raises.

    A notification is a courtesy, not part of the review — a webhook that is
    misconfigured, offline, or rate-limited must never take down a run that
    otherwise completed (or fail one that hadn't), so every failure mode
    here is swallowed and only reported through `emit`.
    """
    if not webhook_url:
        return False
    card = build_card(repo_name=card_kwargs.pop("repo_name"),
                      pr_id=card_kwargs.pop("pr_id"),
                      do_review=card_kwargs.pop("do_review"), **card_kwargs)
    body = json.dumps({"cardsV2": [{"cardId": "prism-summary", "card": card}]})
    req = urllib.request.Request(
        webhook_url, data=body.encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json; charset=UTF-8"})
    try:
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT, context=_ssl_context()):
            pass
    except urllib.error.HTTPError as exc:
        if emit:
            emit(f"⚠ Google Chat rejected the notification "
                 f"({exc.code}: {exc.reason}).")
        return False
    except Exception as exc:  # noqa: BLE001
        if emit:
            emit(f"⚠ Could not post to Google Chat: {exc}")
        return False
    if emit:
        emit("▸ Posted summary to Google Chat.")
    return True
