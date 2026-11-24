"""Post a brief outcome summary to a Google Chat webhook, if one is
configured: one for pull-request reviews, and a separate one for document
reviews.

Stdlib only, and PRISM composes and sends this itself — no agent involved.
The content (verdict, impact, merged status) is already fully known by the
time the pipeline finishes, so an agent call here would only be one more
chance to hit a free-tier rate limit for zero benefit, the same reasoning
behind the PR-description write in orchestrator.py being direct too.

Deliberately free of Tkinter, in the same spirit as updater.py, so
orchestrator.py can use it without pulling in the UI.
"""
import html
import json
import re
import urllib.error
import urllib.request

from updater import _ssl_context

POST_TIMEOUT = 10

_VERDICT_GLYPH = {
    "approve": "✅", "approve-with-comments": "⚠️",
    "request-changes": "🔴", "block": "⛔",
}

# The reviewer's own report already opens the verdict line with an emoji
# (see agents/pr-reviewer.md's "**Verdict:** ✅ Approve | ..." template), so
# verdict_raw arrives pre-glyphed. Strip it before prepending our own
# canonical glyph, or the card doubles up ("✅ ✅ Approve").
_LEADING_SYMBOL_RE = re.compile(r"^[^\w(]+\s*")


def _display_author(author_arn):
    """CodeCommit only gives us the author's IAM ARN — show the trailing
    user/role name rather than the full `arn:aws:iam::...` noise.
    """
    return author_arn.rsplit("/", 1)[-1] if author_arn else author_arn


def build_card(repo_name, pr_id, do_review, verdict_raw=None, verdict_key=None,
              impact_score=None, merged=None, reason=None, author=None,
              source=None, dest=None, reviewer=None):
    """A small Google Chat card: one header line, a few widget rows.

    Kept to this shape deliberately — a card that scrolls off a phone screen
    defeats the point of a *brief* summary. The header identifies the PR
    itself (id, then repo) — outcome is a widget row, not the subtitle, so
    the header stays stable across a run's lifetime.
    """
    if merged:
        outcome = "✅ Merged"
    elif merged is False:
        outcome = "⛔ Not merged" + (f" — {reason}" if reason else "")
    else:
        outcome = "⚠️ Unknown outcome" + (f" — {reason}" if reason else "")

    widgets = [{"decoratedText": {"topLabel": "Outcome", "text": outcome}}]
    if source and dest:
        widgets.append({"decoratedText": {
            "topLabel": "Branches", "text": f"{source} → {dest}"}})
    if author:
        widgets.append({"decoratedText": {
            "topLabel": "Author", "text": _display_author(author)}})
    if reviewer:
        # Who ran the review, so the verdict below has a name behind it.
        widgets.append({"decoratedText": {"topLabel": "Reviewed by", "text": reviewer}})
    if not do_review:
        widgets.append({"decoratedText": {
            "topLabel": "Review", "text": "Skipped by configuration"}})
    elif verdict_raw:
        glyph = _VERDICT_GLYPH.get(verdict_key, "")
        clean = _LEADING_SYMBOL_RE.sub("", verdict_raw)
        widgets.append({"decoratedText": {
            "topLabel": "Verdict", "text": f"{glyph} {clean}".strip()}})
        if impact_score:
            widgets.append({"decoratedText": {
                "topLabel": "Impact", "text": f"{impact_score}/10"}})
    else:
        widgets.append({"decoratedText": {
            "topLabel": "Review", "text": "No verdict parsed"}})

    return {
        "header": {"title": f"PR #{pr_id}", "subtitle": repo_name},
        "sections": [{"widgets": widgets}],
    }


def _post(webhook_url, card, card_id, emit, what):
    """POST one card. Best-effort, never raises; True when Google Chat accepted it."""
    body = json.dumps({"cardsV2": [{"cardId": card_id, "card": card}]})
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
        emit(f"▸ Posted {what} to Google Chat.")
    return True


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
    return _post(webhook_url, card, "prism-summary", emit, "summary")


# ---- document reviews ----------------------------------------------------------

# A card that scrolls off a phone screen defeats the point of a brief summary, so
# a long list of documents is cut with a count of what is not shown.
DOC_LINES_PER_REPO = 10

_DOC_GLYPH = dict(_VERDICT_GLYPH)


def _doc_verdict_text(key, label):
    glyph = _DOC_GLYPH.get(key, "")
    clean = _LEADING_SYMBOL_RE.sub("", label or "") or (key or "").replace("-", " ").capitalize()
    return f"{glyph} {clean}".strip() or "No verdict"


def _esc(text):
    """Google Chat reads a small HTML subset in card text; file names must not be taken as tags."""
    return html.escape(str(text), quote=False)


def build_doc_card(persona, verdict_key=None, verdict_label=None, impact=None,
                   comments=None, targets=(), reviewer=None, run_id=None,
                   outcome=None, not_reviewed=0, project=None):
    """The card for one document review.

    Says who reviewed (the persona - Business Analyst, Architect or QA - and the
    person who ran it), the verdict and how many comments of what severity it
    carries, which branch(es) were looked at, and exactly which documents - by
    their path from the repository root - each with its own verdict.

    `targets` is a list of dicts: {"label", "repo", "branch", "documents": [
    {"file", "verdict_key", "verdict_label", "counts_text"}]}.
    """
    widgets = [{"decoratedText": {"topLabel": "Reviewer persona", "text": _esc(persona or "?")}}]
    if reviewer:
        widgets.append({"decoratedText": {"topLabel": "Reviewed by", "text": _esc(reviewer)}})
    widgets.append({"decoratedText": {
        "topLabel": "Verdict", "text": _esc(_doc_verdict_text(verdict_key, verdict_label))}})
    if impact:
        widgets.append({"decoratedText": {"topLabel": "Impact", "text": _esc(f"{impact}/10")}})
    if comments:
        widgets.append({"decoratedText": {"topLabel": "Comments", "text": _esc(comments)}})
    if outcome:
        widgets.append({"decoratedText": {"topLabel": "Outcome", "text": _esc(outcome)}})
    if not_reviewed:
        widgets.append({"decoratedText": {
            "topLabel": "Not reviewed",
            "text": f"⚠ {not_reviewed} document(s) could not be reviewed"}})
    for t in targets:
        # Several repositories can share a branch name; the repo is in every label.
        name = t.get("repo") or t.get("label") or "repository"
        label = t.get("label")
        who = f"{label} · {name}" if label and label != name else name
        widgets.append({"decoratedText": {
            "topLabel": "Branch", "text": _esc(f"{who} → {t.get('branch') or '?'}")}})
        docs = list(t.get("documents") or [])
        lines = []
        for d in docs[:DOC_LINES_PER_REPO]:
            glyph = _VERDICT_GLYPH.get(d.get("verdict_key"), "")
            tail = " — ".join(x for x in (
                _LEADING_SYMBOL_RE.sub("", d.get("verdict_label") or ""),
                d.get("counts_text") or "") if x)
            lines.append(f"{glyph} {_esc(d.get('file', '?'))}"
                         + (f" — {_esc(tail)}" if tail else ""))
        if len(docs) > DOC_LINES_PER_REPO:
            lines.append(f"… and {len(docs) - DOC_LINES_PER_REPO} more")
        if lines:
            widgets.append({"decoratedText": {
                "topLabel": f"Documents ({len(docs)})", "wrapText": True,
                "text": "<br>".join(x.strip() for x in lines)}})
    if run_id:
        widgets.append({"decoratedText": {"topLabel": "Run", "text": _esc(run_id)}})
    header = {"title": "Document review"}
    if project:
        header["subtitle"] = project
    return {"header": header, "sections": [{"widgets": widgets}]}


def post_doc_summary(webhook_url, emit=None, **card_kwargs):
    """Best-effort POST of the document-review card. Never raises, like post_summary."""
    if not webhook_url:
        return False
    try:
        card = build_doc_card(**card_kwargs)
    except Exception as exc:  # noqa: BLE001 - a malformed notice must not fail a finished review
        if emit:
            emit(f"⚠ Could not build the Google Chat notification: {exc}")
        return False
    return _post(webhook_url, card, "prism-doc-summary", emit, "document review summary")
