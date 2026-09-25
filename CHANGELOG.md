# Changelog

What changed in each release, for people using PRISM — not a commit log.
This file is what the in-app "check for updates" screen shows for a new
release, so keep entries short and about what you'd actually notice.

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
