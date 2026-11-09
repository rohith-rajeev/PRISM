# Changelog

What changed in each release, for people using PRISM — not a commit log.
This file is what the in-app "check for updates" screen shows for a new
release, so keep entries short and about what you'd actually notice.

## v5.6

- **Re-reviewing a document now verifies the earlier comments instead of reviewing
  it all again.** The reviewer decides for each open comment whether it is resolved,
  partly resolved, still open or withdrawn, and looks for new problems only in what
  the author changed. A comment it does not report on stays open under "Not
  assessed" instead of silently disappearing, and the review header summarises how
  many earlier comments were resolved.

## v5.5

- **A failed fetch of a pull request's branches now says why.** Instead of
  "check the connection and credentials", PRISM shows git's own message with a
  hint: expired AWS credentials, a deleted branch, or a connection problem.
- **A slow fetch is retried once, with a longer limit.** On a large repository the
  first attempt could time out and stop the review. Git no longer waits for a
  credential prompt that nobody can answer.
- **Fixed: updating failed on a passing "update service returned an error (504)".**
  The updater now retries temporary errors and dropped connections a couple of
  times, and says plainly when the service is temporarily unavailable.

## v5.4

- **Fixed: a document could be skipped as "not reviewed"** (for example the
  frontend document of a two-repo review) when the reviewer's Coverage merely
  mentioned something it hadn't read. A review that carries comments is now kept,
  with a warning, and a document that gets no review is retried once on its own.
- **Checks before the review starts.** Every branch, document and requirements
  file is verified up front and all problems are reported together, with a
  "did you mean" for a mistyped file name.
- **Stop now really stops a run.** It ends the review engine and everything it
  started, so a stopped run no longer lingers or keeps using tokens.
- **Document review comments are formal and consistent.** Bullet lists only, no
  conversational wording ("I reviewed…"), the same fields on every comment, and
  fixed Summary and Coverage lines.

## v5.3

- **Fixed: on Windows, a review could finish but the PR description wasn't
  updated and the PR couldn't be merged** ("'charmap' codec can't encode
  character"). The AWS CLI was writing in the legacy codepage and choked on the
  review's ✅. PRISM now makes it use UTF-8.

## v5.2

- **Choose who reviews a document:** Business Analyst (functional), Architect
  (technical) or QA (quality), each through its own lens. Security, stability and
  scalability are always checked. `--persona` on the command line.
- **Requirements are optional.** With them, the review checks the decomposition
  satisfies all of them; without, it checks the documents against the codebase.
- **Fixed: reviewing an already-merged pull request failed as "review
  incomplete".** The reviewer was blocked from running `git merge-base`. PRISM now
  finds a PR's changes itself, including for merged PRs, and says plainly when
  there is nothing to review.
- **Fixed: "Bundled agents missing" when starting a review**, after an update or
  once `/tmp` was cleared. PRISM now restarts itself cleanly or uses a saved copy
  of its agents.

## v5.1

- **Document review.** A new tab, and `prism docs` on the command line, reviews
  specs and requirement documents against the requirements and the code. The
  findings are written into the document as a *PRISM review comments* section,
  committed and pushed. Running it again checks whether earlier comments were
  resolved. It aids your review; it doesn't replace it.
- **Fixed: a review that couldn't read the pull request could be approved and
  merged.** It now fails instead, with nothing written or merged, and can be
  retried.
- **Fixed: after an in-app update, restarting could fail with "Bundled agents
  missing".** The new copy now starts with a clean environment.
- The tagline under the PRISM name is gone.

## v4.9

- **Fixed: PRISM would not start on Ubuntu 22.04 (including WSL) after
  updating to v4.8.** The Linux build now targets glibc 2.35, so
  it runs on Ubuntu 22.04 and every newer release instead of only the newest.
  No workaround or system upgrade is needed.

## v4.8

- **Open a command-line job to see its conversation.** On the **CLI jobs**
  screen, click any job to see everything the reviewer is doing, live while it
  runs and in full once it has finished, together with its status, stage and
  verdict, and Stop or Retry from the same page. This is the same transcript
  `prism logs -f` prints; the list used to show only status.

## v4.7

- **Fixed: `prism run --detach` failed with "Bundled agents missing".** The
  background worker was started from the packaged binary but shared the
  starter's temporary unpack folder, which was deleted the moment the starter
  returned. The worker now unpacks its own copy.
- **Better behaviour on older opencode versions.** PRISM again passes the
  project folder to engines that support it (`--dir`, as it did before v4.4)
  and no longer lets the engine inherit PRISM's own input stream, which could
  leave a run silent until it timed out. Newer opencode 2.x is unaffected. The
  log now also states which opencode version a run used, which makes a stalled
  run on someone else's machine diagnosable.

## v4.6

- **Fixed: the live conversation showed `shell: shell`** instead of what the
  reviewer was doing. opencode 2.x labels its commands with just the tool name;
  PRISM now shows the actual command, for example `shell: git diff --stat`.

## v4.5

- **Start a new job from any job's page.** Every job's detail view now has a
  `+ New job` button, so you no longer have to go back to the list first. The
  form opens with that job's settings (usually only the PR id changes), and
  Cancel takes you back to the job you were on.
- **Ctrl+Enter starts the review.** Once the PR id, working folder, region and
  repository are filled in, press Ctrl+Enter instead of clicking
  `Start Prisming`. It does nothing while anything required is still missing.
- **Fixed: the model list could stay empty forever.** The engine sometimes
  can't answer for a few seconds after it starts, and PRISM gave up on the
  first failure, leaving "could not list models" and an endless "Loading
  models…". It now retries, remembers the last good list, keeps re-asking in
  the background, and says what it's doing.
- **Fixed: reviews failing with `Agent not found: "pr-reviewer"`** when PRISM
  was started from a different folder than the project, on opencode 2.x.

## v4.4

- Stability fixes

## v4.3

- **A warning before merging a PR that was never reviewed.** If a run skips the
  review (sync + merge only, or merge alone), PRISM now stops and asks before
  it merges: "Merge without a review?", with the option to abort. It asks every
  time, so a setting left over from an earlier run can no longer merge a PR
  unreviewed by accident. A background run, with nobody to ask, does not merge.

## v4.2

- PRISM CLI integration

## v4.1

- **PRISM on the command line.** `prism run 214` reviews PR 214 of the
  repository in the folder you are in, with everything the desktop app does:
  review, description update, sync, merge, dry run, model and region choice,
  custom instructions, and the same safety gates. It needs no display, so you
  can ssh into the machine that runs PRISM and review from anywhere. Add `-d`
  to run in the background and survive logging out; `prism jobs`, `logs -f`,
  `stop`, `retry` and `ask` manage the rest. Defaults live in
  `~/.prism/config.json` — set them once with `prism config set`, and the
  repository and region are read from the clone's CodeCommit remote.
- **A CLI jobs screen in the desktop app.** Jobs started from the command line
  are listed there with Stop and Retry. The header tally now counts desktop and
  CLI jobs together, with the split shown when you hover over it.
- **A modern folder chooser on Linux.** "Browse" now opens your desktop's own
  folder dialog (zenity, kdialog or yad) instead of the old Tk one, falling back
  to it only when none is installed.

## v3.9

- **Faster, steadier reviews.** A review no longer fails after a fixed 20
  minutes when the model is simply slow, and PRISM says it is still working
  while it waits. If a review does have to be stopped, you still get its
  findings, marked as a partial review.
- **The reviewer no longer runs a PR's tests or installs packages.** It reads
  the code only, which is faster and safer.

## v3.8

- **Stability and performance fixes.**

## v3.7

- **Works with any branching setup.** The reviewer's guidance and PRISM's
  messages about repeat reviews no longer assume particular branch names or a
  promotion order — a repeat can be the same PR, another PR or another branch.

## v3.6

- **Repeat reviews stay consistent.** PRISM remembers what it found when it
  reviews a change it has seen before, even after a rebase or squash, so
  reviews agree and anything an earlier review missed is flagged as a
  "late find". Nothing is ever written to your repositories.
- **Deeper first review.** Reviews now check error handling and edge cases and
  what the change breaks.
- **Optional codegen tool integration.** Let a code-generation tool hand PRISM
  the pull requests it opens and get the verdict back. Off by default.

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
