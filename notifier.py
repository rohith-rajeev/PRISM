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

# Flat Material Symbols, not emoji: the same 2D style as the rest of Google Chat, and
# each shape distinct on its own (a tick, a speech-bubble note, a cross, a bar).
_VERDICT_ICON = {
    "approve": "check_circle", "approve-with-comments": "rate_review",
    "request-changes": "cancel", "block": "block",
}


def _icon(name):
    return {"materialIcon": {"name": name}}


def _row(label, text, icon=None):
    """One labelled fact, with a Material icon in front when there is one."""
    row = {"decoratedText": {"topLabel": label, "text": text}}
    if icon:
        row["decoratedText"]["startIcon"] = _icon(icon)
    return row

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
        outcome, outcome_icon = "Merged", "merge"
    elif merged is False:
        outcome, outcome_icon = ("Not merged" + (f" — {reason}" if reason else "")), "block"
    else:
        outcome, outcome_icon = ("Unknown outcome" + (f" — {reason}" if reason else "")), \
            "help"

    widgets = [_row("Outcome", outcome, outcome_icon)]
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
        clean = _LEADING_SYMBOL_RE.sub("", verdict_raw).strip()
        widgets.append(_row("Verdict", clean, _VERDICT_ICON.get(verdict_key)))
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

# The card is three parts (FR-CARD) and stays compact: the decision and its owner, a
# short summary, and the documents - then a one-line note on why, and a footer. Each
# repository shows at most this many document lines, then "and N more"; the job screen
# still lists every document.
DOC_LINES_PER_REPO = 5
REPO_NAME_MAX = 40          # longer names show their last path segment
BRANCH_MAX = 60             # longer branches keep their start and end
NOTE_MAX = 160              # the note is a sentence or two, never cut mid-word

# The decision as a flat Material icon - a tick or a cross - and what it means.
_DOC_ICON = {"approve": "check_circle", "request-changes": "cancel"}
_DOC_MEANING = {"approve": "Work may start", "request-changes": "Work may not start"}
_SEVERITIES = ("Blocker", "Major", "Minor", "Suggestion")
_RAISED_PREFIX = "Raised to 8/10 because the change is large or expensive"


def _esc(text):
    """Google Chat reads a small HTML subset in card text; file names, branches and the
    owner must be shown as text, never taken as tags or entities."""
    return html.escape(str(text), quote=False)


def _decision(key):
    return key if key in _DOC_ICON else "request-changes"


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


def short_reason(text, limit=NOTE_MAX):
    """The impact reason in a sentence: PRISM's "raised" preamble dropped, the first
    sentence kept, and a long one shortened at a clause or word boundary - never cut
    off with an ellipsis."""
    text = " ".join(str(text or "").split())
    if text.startswith(_RAISED_PREFIX):
        text = text.split(": ", 1)[1] if ": " in text else ""
    text = text.strip(" .;:—-")
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0].rstrip(".")
    if len(first) <= limit:
        return first
    cut = first[:limit]
    for sep in ("; ", ", ", " — ", " - "):
        if sep in cut[limit // 2:]:
            return cut[:cut.rindex(sep)].rstrip(" ,;—-")
    return cut[:cut.rindex(" ")] if " " in cut else cut


def _fact(label, value):
    return {"decoratedText": {"topLabel": label, "text": _esc(value), "wrapText": True}}


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
                   not_reviewed=0, decision_reason=""):
    """The card for one document review.

    1. Decision - title, the owner as the subtitle, and the decision as an icon with
       what it means (a tick: work may start; a cross: work may not start).
    2. Summary - reviewer role and who ran it, comment counts, impact (N/10).
    3. Documents - per repository a one-line heading (role, short repository name,
       branch), then one line per document: its decision icon and file name, with the
       parent folder beneath only where two documents in the card share a name.
    Then a short note on why (the decision's reason and the impact's), and a footer line
    with the run id, what happened to the comments, and any document not reviewed.

    `targets` is a list of dicts: {"label", "role", "repo", "branch", "documents": [
    {"file", "folder", "verdict_key"}]}.
    """
    decision = _decision(decision)
    header = {"title": "Document review"}
    if owner:
        # Header text is plain text in Chat, so it is not escaped.
        header["subtitle"] = f"Owner: {owner}"

    summary = [{"decoratedText": {"startIcon": _icon(_DOC_ICON[decision]),
                                  "text": f"<b>{_DOC_MEANING[decision]}</b>"}},
               _pair(_fact("Reviewer role", persona or "?"),
                     _fact("Reviewed by", reviewer) if reviewer else None),
               _pair(_fact("Comments", comment_counts_text(counts)),
                     _fact("Impact", f"{impact}/10") if impact else None)]

    targets = [t for t in (targets or []) if t.get("documents")]
    names = [d.get("file") or "" for t in targets for d in t["documents"]]
    shared = {n for n in names if names.count(n) > 1}
    docs = []
    for t in targets:
        repo, branch = short_repo(t.get("repo")), short_branch(t.get("branch"))
        place = " · ".join(x for x in (repo, branch) if x) or t.get("label") or ""
        if len(targets) > 1:
            label = {"BE": "Backend", "FE": "Frontend"}.get(t.get("role"), t.get("label") or "")
            place = f"{label} · {place}" if label and label != place else place
        docs.append({"decoratedText": {"text": f"<b>{_esc(place)}</b>", "wrapText": True}})
        listed = list(t["documents"])
        for d in listed[:DOC_LINES_PER_REPO]:
            name = d.get("file") or "?"
            row = {"startIcon": _icon(_DOC_ICON[_decision(d.get("verdict_key"))]),
                   "text": _esc(name), "wrapText": True}
            if name in shared:
                # Plain text in Chat: not escaped.
                row["bottomLabel"] = "in " + (d.get("folder") or "the top folder")
            docs.append({"decoratedText": row})
        if len(listed) > DOC_LINES_PER_REPO:
            docs.append({"decoratedText": {
                "text": f"and {len(listed) - DOC_LINES_PER_REPO} more"}})

    note = ". ".join(x for x in (
        (decision_reason or "").strip().rstrip("."),
        (f"Impact {impact}/10: {short_reason(impact_reason)}"
         if impact and short_reason(impact_reason) else "")) if x)
    foot = []
    if run_id:
        foot.append(f"Run {run_id}")
    if outcome:
        foot.append(str(outcome))
    if not_reviewed:
        foot.append(f"{not_reviewed} document{'' if not_reviewed == 1 else 's'} "
                    f"could not be reviewed")
    sections = [{"widgets": summary}]
    if docs:
        sections.append({"header": "Documents", "widgets": docs})
    tail = []
    if note:
        tail.append({"textParagraph": {"text": _esc(note + ".")}})
    if foot:
        tail.append({"textParagraph": {
            "text": f'<font color="#5f6368">{_esc(" · ".join(foot))}</font>'}})
    if tail:
        sections.append({"widgets": tail})
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
