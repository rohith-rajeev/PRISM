# Changelog

What changed in each release, for people using PRISM — not a commit log.
This file is what the in-app "check for updates" screen shows for a new
release, so keep entries short and about what you'd actually notice.

## v3.6

- **Reviews stay consistent from develop to QA to staging.** PRISM keeps a
  short-lived record (a temp file on this machine; nothing is ever written to
  your repositories) of what it concluded about each change, matched by the code
  that changed so it survives rebases and squashes, and gives the reviewer the
  earlier findings when the same change comes back in a later PR (or a Retry). A
  defect the earlier stages missed is flagged as a **late find** in the job log
  and the PR description, since a fix made that late hasn't been through the
  testing in between. Only a late Critical/High finding blocks the merge; a
  late Medium or lower is reported and approves with comments. Nothing changes
  if there is no earlier review of those changes.
- **Reviewers look harder the first time.** Every review now checks failure
  handling and edge cases, the impact on callers, and searches the repository
  for the same defect elsewhere: each finding lists the other places the pattern
  occurs (or says none were found), so one fix can cover them all. Findings can
  be longer as a result, and reviews use somewhat more tokens.
- **Codegen tool integration (optional).** In Help, tick *Accept reviews from a
  code-generation tool* and PRISM will review the pull requests a codegen tool
  opens, send "request changes" findings back to it, and merge once the PR is
  approved — repeating up to five rounds before it hands over to you. Off by
  default and local to your machine; a job from the tool is an ordinary job
  (marked *via codegen*) with every existing check, including the high-impact
  confirmation, still in force. Nothing changes if you never turn it on.
- **The manual is updated** for all of the above: how to read the new finding
  markers, the new safety notes and troubleshooting entries.

## v3.5

- **Fewer "could not parse a verdict" stops.** The verdict is now found
  however the reviewer formats it, and a follow-up reply that doesn't repeat
  it no longer wipes out the verdict it already gave. If a verdict still
  can't be read, PRISM asks the reviewer once to restate it before stopping.
  "Approve — no blocking issues" is no longer mistaken for a Block.
- **Custom instructions set before you start are now honoured.** They are
  handed to the reviewer up front as a clearly marked block (and logged),
  instead of a trailing sentence it could skim past.
- **Ask about a PR after its job has finished.** The instruction box stays
  enabled on finished and stopped jobs and becomes a read-only follow-up
  question to the reviewer; unrelated requests are declined with a reason.
- **PRISM AWS Alerts: deployment alerts are now cards** like the merge alert,
  with a colour-coded SUCCEEDED (green) / FAILED (red) status. Redeploy
  `prism-aws-alerts` to pick this up.
- **The jobs list is split into Running and Completed sections**, so live
  work is always at the top.

## v3.4

- **Sync + merge-only runs now notify Google Chat.** With review turned
  off, a merge used to post nothing; it now posts the card (outcome,
  branches, author, "Review: Skipped by configuration").
- **Source → destination branches are shown** on each job in the list, in
  the job's header, and on the Chat card.
- **The PR author is shown** in the job list and inside the job.
- **PRISM AWS Alerts: one author, both messages.** The merge alert now
  records the PR author and the deployment alert reuses it, instead of
  showing the git commit author — so the two no longer disagree. Redeploy
  `prism-aws-alerts` to pick this up.

## v3.3

- **The reviewer is now actually handed the source and destination
  branches** it resolves from the PR id, instead of being left to work
  them out on its own — closing the gap that occasionally had it stop and
  ask you which branch was which.
- **Custom instructions.** A new field on the new-job screen lets you steer
  a review before it starts ("focus on the auth changes", "skip the
  generated files"), and a matching box on the detail screen lets you add
  one while a job is running — the reviewer picks it up at its next turn.
- **Retry now reruns the same job** instead of creating a new one — same
  row, same id, log and verdict cleared, nothing extra added to the list.
- **Google Chat notifications now post only on a real, parsed verdict** —
  Approve, Approve with comments, Request changes, or Block. A skipped
  review, an unparsed verdict, or an internal error no longer post.
- **Two jobs run at a time now, not three** — the rest queue and start
  automatically as before, just with a smaller live window to stay clear
  of provider rate limits.

## v3.2

- Added `prism-aws-alerts`, a standalone Terraform sub-project bundled in
  this repo: Lambdas + EventBridge rules that post a Google Chat card when a
  pull request is merged into a watched branch, then reply in that same
  thread once the resulting commit is deployed. Independent of the desktop
  app — nothing here changes how PRISM itself runs.

## v3.1

- **Retry a finished job** with one click — same repo, PR id and settings,
  nothing to retype. A retry automatically picks up where PRISM's last
  review of that PR left off, focusing on what changed since instead of
  starting over.
- **PRISM now checks for a new version on startup**, quietly — it only
  interrupts you if one is actually available. Manual "Check for updates"
  in Help still works the same way.
- Release notes (this file) are now written by hand for each version.

## v3.0

- **Faster re-reviews.** When PRISM reviews a PR it has already reviewed
  before, it now focuses on what changed since — not the whole PR again —
  and calls out whether earlier findings were addressed.
- **Confirmation before high-impact merges.** A PR scoring 7/10 or higher
  on impact now pauses for your explicit go-ahead before PRISM merges it,
  even on an Approve verdict.
- **Faster concurrent reviews.** Different PRs no longer wait on each
  other's git operations — each job now works in its own isolated
  workspace instead of sharing one locked clone.
- **PR descriptions are no longer trimmed.** Every finding PRISM reports
  is written to the PR, not just the first several.
- Fixed the Google Chat notification card showing a doubled verdict emoji,
  and cleaned up its header (PR number as the title, repo as the subtitle).
- Execution errors (a crash, an AWS hiccup) no longer post to the group
  chat — only real review outcomes do.
- Increased font sizes on macOS, where the previous sizing was hard to
  read comfortably.
- Fixed a bug where pasting a PR id could silently fail to register,
  requiring a second attempt.
