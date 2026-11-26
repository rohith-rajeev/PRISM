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
              source=None, dest=None, reviewer=None, ref=None):
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
        # `ref` names a GitLab merge request ("MR !12"); a pull request is "PR #12".
        "header": {"title": ref or f"PR #{pr_id}", "subtitle": repo_name},
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

# The card is three parts and nothing else (FR-CARD): the decision and its owner, a
# short summary, and the documents. A card that fills the chat window defeats the
# point of an alert, so each repository shows at most this many document lines,
# followed by "and N more"; the job screen still lists every document.
DOC_LINES_PER_REPO = 5
REPO_NAME_MAX = 40          # longer names show their last path segment
BRANCH_MAX = 60             # longer branches keep their start and end
REASON_MAX = 90             # the impact reason is clipped to one line

_DOC_DECISION = {"approve": ("Approve", "work may start"),
                 "request-changes": ("Request changes", "work may not start")}
_SEVERITIES = ("Blocker", "Major", "Minor", "Suggestion")


def _esc(text):
    """Google Chat reads a small HTML subset in card text; file names, branches and the
    owner must be shown as text, never taken as tags or entities."""
    return html.escape(str(text), quote=False)


def doc_decision_text(key):
    """"Request changes — work may not start": the decision always says what it means.
    Anything that is not one of the two decisions reads as Request changes."""
    words, meaning = _DOC_DECISION.get(key, _DOC_DECISION["request-changes"])
    return f"{words} — {meaning}"


def short_repo(name):
    """A repository name of more than REPO_NAME_MAX characters, by its last path segment."""
    name = str(name or "")
    if len(name) > REPO_NAME_MAX:
        return name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or name
    return name


def short_branch(name):
    """A branch of more than BRANCH_MAX characters keeps its start and end."""
    name = str(name or "")
    if len(name) <= BRANCH_MAX:
        return name
    keep = (BRANCH_MAX - 1) // 2
    return name[:keep] + "…" + name[-keep:]


def comment_counts_text(counts):
    """"1 blocker, 2 major, 1 minor" - zero counts left out."""
    counts = counts or {}
    plural = {"Blocker": "blockers", "Suggestion": "suggestions"}
    bits = [f"{counts[s]} {plural.get(s, s.lower()) if counts[s] != 1 else s.lower()}"
            for s in _SEVERITIES if counts.get(s)]
    return ", ".join(bits) or "No comments"


def _clip_line(text, limit=REASON_MAX):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _fact(label, value, wrap=True):
    return {"decoratedText": {"topLabel": label, "text": _esc(value), "wrapText": wrap}}


def _pair(left, right):
    """Two facts side by side - one line of the summary where columns are available."""
    items = [w for w in (left, right) if w]
    if len(items) == 1:
        return items[0]
    return {"columns": {"columnItems": [
        {"horizontalSizeStyle": "FILL_AVAILABLE_SPACE", "horizontalAlignment": "START",
         "verticalAlignment": "TOP", "widgets": [w]} for w in items]}}


def build_doc_card(persona, owner="", reviewer="", decision="", impact=None,
                   impact_reason="", counts=None, targets=(), run_id=None, outcome=None,
                   not_reviewed=0):
    """The card for one document review.

    1. Decision - the title, and a subtitle with the decision in words and the owner.
    2. Summary - owner, reviewer role, who ran it, comment counts, impact.
    3. Documents - one block per repository: a one-line heading (role, short repository
       name, branch) and one line per document (file name and its decision), the parent
       folder added only where two documents in the card share a file name.
    A footer line carries the run id, what happened to the comments and how many
    documents could not be reviewed.

    `targets` is a list of dicts: {"label", "role", "repo", "branch", "documents": [
    {"file", "folder", "verdict_key"}]}.
    """
    words, meaning = _DOC_DECISION.get(decision, _DOC_DECISION["request-changes"])
    header = {"title": "Document review",
              # Header text is plain text in Chat, so it is not escaped.
              "subtitle": f"{words} — {meaning}" + (f" · Owner: {owner}" if owner else "")}

    summary = []
    if owner:
        summary.append(_fact("Owner", owner))
    summary.append(_pair(_fact("Reviewer role", persona or "?"),
                         _fact("Reviewed by", reviewer) if reviewer else None))
    impact_text = ""
    if impact:
        impact_text = f"{impact}/10" + (f" — {_clip_line(impact_reason)}"
                                        if (impact_reason or "").strip() else "")
    summary.append(_pair(_fact("Comments", comment_counts_text(counts)),
                         _fact("Impact", impact_text, wrap=False) if impact_text else None))

    targets = [t for t in (targets or []) if t.get("documents")]
    names = [d.get("file") or "" for t in targets for d in t["documents"]]
    shared = {n for n in names if names.count(n) > 1}
    blocks = []
    for t in targets:
        repo, branch = short_repo(t.get("repo")), short_branch(t.get("branch"))
        place = " · ".join(x for x in (repo, branch) if x) or t.get("label") or ""
        if len(targets) > 1:
            label = {"BE": "Backend", "FE": "Frontend"}.get(t.get("role"), t.get("label") or "")
            place = f"{label} · {place}" if label and label != place else place
        lines = [f"<b>{_esc(place)}</b>"]
        docs = list(t["documents"])
        for d in docs[:DOC_LINES_PER_REPO]:
            name = d.get("file") or "?"
            lines.append(f"{_esc(name)} — {_esc(_DOC_DECISION.get(d.get('verdict_key'), _DOC_DECISION['request-changes'])[0])}")
            if name in shared:
                folder = d.get("folder") or ""
                lines.append("&nbsp;&nbsp;in " + (_esc(folder) if folder else "the top folder"))
        if len(docs) > DOC_LINES_PER_REPO:
            lines.append(f"and {len(docs) - DOC_LINES_PER_REPO} more")
        blocks.append({"decoratedText": {"text": "<br>".join(lines), "wrapText": True}})

    foot = []
    if run_id:
        foot.append(f"Run {run_id}")
    if outcome:
        foot.append(str(outcome))
    if not_reviewed:
        foot.append(f"{not_reviewed} document{'' if not_reviewed == 1 else 's'} "
                    f"could not be reviewed")
    sections = [{"widgets": summary}]
    if blocks:
        sections.append({"header": "Documents", "widgets": blocks})
    if foot:
        sections.append({"widgets": [{"textParagraph": {
            "text": f'<font color="#5f6368">{_esc(" · ".join(foot))}</font>'}}]})
    return {"header": header, "sections": sections}


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
