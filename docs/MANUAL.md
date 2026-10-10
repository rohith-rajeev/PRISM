# PRISM — User Manual

PRISM reviews AWS CodeCommit pull requests for you. It reads the PR, gives a
verdict and an impact score, optionally writes its findings onto the PR, and —
if you let it — merges it.

You can review several pull requests at the same time. Each one is a **job**.

---

## Before you start

Three tools must be installed and working on your machine:

| Tool | What PRISM uses it for |
|---|---|
| `opencode` | Runs the reviewer |
| `aws` | Reading and merging the pull request |
| `git` | Reading the diff and commit history |

Your AWS credentials must already be valid — PRISM never logs you in. If your
session has expired, run your usual `aws sso login` first.

**Launch it** by clicking PRISM in your applications menu, or run `./run.sh`
from the project folder.

---

## Your first review

**1. Click `+ New job`.**

**2. Choose your working folder** with `Browse`. On Linux this opens your desktop's own folder dialog (it needs `zenity`, `kdialog` or `yad`; without one PRISM falls back to its basic built-in chooser). You can also type or paste the path.

This is the folder holding your code. PRISM looks inside it for git clones and
sets itself up accordingly:

- Finds **one** repository → fills in the repo name for you.
- Finds **several** → shows a *Repository mapping* card. Pick which clone is
  your backend and which is your frontend, then choose which one to review.
- Finds **none** → type the CodeCommit repository name yourself.

> The repository name box is editable on purpose: the name in AWS can differ
> from your local folder name.

**3. Enter the PR id** — just the number, e.g. `214`. That's the only thing
PRISM needs to find the PR: it looks up the source and destination branches
itself from the PR id and repository, so you never have to type a branch name.

**4. Check the region** (defaults to `us-east-1`).

**5. (Optional) Add custom instructions.** A free-text box under the model
picker lets you steer the review — "focus on the auth changes", "skip the
generated files", anything relevant. Leave it empty for an ordinary review.
The reviewer is given these instructions up front, before it starts
reading, and notes in its summary how it applied them.

**6. Click `▶ Start Prisming`.**

That's it. PRISM opens the job's detail view and starts working.

---

## Watching it work

The **progress bar** fills through up to four stages:

```
Review  →  Describe  →  Merge check  →  Merge
```

Only the stages this job could actually reach are shown — a job with
**Agentic PR review** off shows just `Merge check → Merge`, one with
**Merge PR** off shows just `Review → Describe`, and so on.

The **CONVERSATION** panel shows what the reviewer is doing:

- **Blue text** — the reviewer thinking out loud.
- **Grey `⚙` lines** — commands it is running (reading files, `git diff`, AWS calls).
- **Plain text** — PRISM's own progress messages.
- **Red** — an actual error.

A **spinning icon** means work is genuinely happening. Reviews take a few
minutes, and long quiet stretches are normal — the spinner is how you know it
hasn't hung.

Click `Clear` to empty the panel. `← Jobs` takes you back to the list.

---

## Reading the result

**Verdict** — what the reviewer concluded:

| | Meaning | Will PRISM merge it? |
|---|---|---|
| ✅ Approve | Good to go | Yes |
| ⚠️ Approve with comments | Fine, but read the notes | Yes |
| 🔴 Request changes | Needs work | **No** |
| ⛔ Block | Do not merge | **No** |

**Impact** — how much of the system this PR touches, 1 to 10. It's about blast
radius, not how big the diff is:

| Score | | Meaning |
|---|---|---|
| 1–3 | ✅ | Isolated change |
| 4–6 | ⚠️ | Moderate reach |
| 7–8 | 🔴 | High risk |
| 9–10 | ⛔ | Critical |

The reviewer's reasoning is in the conversation panel.

**Findings** are one line each, most severe first, and they are written to the PR
description too. A finding is about the lines **this PR** changes. It can also
say where else in the PR the same mistake repeats, so one fix covers them all:

- *Same pattern also at: …* — other places in this PR with the same problem.
- *(late find — missed in PR #n)* — a problem in code an earlier review (for
  example on an earlier PR) had already covered. See **Reviewing the same
  change more than once**, under Settings. When a review has any, the job log
  and the PR description carry a warning, because a fix made now has not been
  through the testing done since that earlier review.

The reviewer's closing summary has a **Coverage** line saying what it checked and
anything it could not verify.

---

## When the reviewer asks you something

Sometimes it needs your input — a genuinely missing detail the PR itself
doesn't make clear. (Source and destination branches are not one of these —
PRISM resolves them itself from the PR id, so you're never asked to pick
between two branches.) A **`💬 Reviewer needs your input`** panel appears with
the question and a text box.

Type a normal sentence and press **Enter** (or click `Send ▸`). The job carries
on with your answer.

Click **`Skip`** if you'd rather not answer — the job finishes with what it has.

**Steering a review while it runs.** You don't have to wait for the reviewer
to ask you something — the detail screen always has an **"Add an
instruction…"** box above the conversation panel, enabled for as long as the
job is active. Type a note and click `Send note` (or press Enter); the
reviewer picks it up at its next turn — right after the current step it's on
finishes, not mid-thought. It's handed over the same way a typed answer is,
so anything you'd say to steer the review works here too.

**Asking about a PR after the job has finished.** The same box stays usable
once a job is finished or stopped — it becomes **"Ask about this pull
request…"** and the button reads `Ask`. The reviewer answers from the job's
own context (it still has the whole review in mind). It is read-only: it can
explain a finding, look at another part of the diff or tell you what to do
next, but it cannot merge, edit or change the PR — use **`↻ Retry`** for
that. A request that isn't about this pull request is declined, and the log
says why. The box greys out only while an answer is on its way.

**Merge conflicts appear here too.** If syncing your branch with its base hits a
conflict, PRISM does not resolve it. It undoes the merge, leaves your clone
exactly as it found it, and shows you each conflict in turn with both versions
and three buttons: **`Keep current`** (your branch), **`Take incoming`** (the
base branch), or **`Abort sync`**. Once you have chosen for every conflict,
PRISM replays the merge with your answers.

**High-impact PRs pause for you too.** A PR the reviewer scores 7/10 or
higher on impact stops here even on an Approve verdict — **`Proceed with
merge`** or **`Abort — I'll handle this manually`**. A verdict on its own is
never enough to auto-merge something that risky; PRISM always waits for you
to say go.

If this happens while you're looking at a different job, that job shows
**Needs input** in the list, and the header says so too. Nothing is missed.

---

## Running several PRs at once

Create as many jobs as you like. **Two run at a time**; the rest wait their
turn and start automatically.

The jobs list shows every job at a glance, in two sections: **Running**
(queued jobs included) on top, and **Completed** below it, newest first.
Each row shows the pull request's source → destination branches and its
author.

```
● example-service-be #12948   Running…   ⚠️ Approve with comments   ⚠️ 6/10   [Stop]    [✕]
● example-portal-fe  #884                         Finished   ✅ Approve                 ✅ 2/10   [↻ Retry] [✕]
```

Click a row to open it. `✕` removes it from the list. The other button
changes with the job: **`Stop`** while it's running, **`↻ Retry`** once it's
finished (see **Retrying a job**, below).

What the status word means:

| Status | |
|---|---|
| **Queued** | Waiting for a free slot |
| **Running…** | Working (the icon spins) |
| **Needs input** | Waiting on your answer — open it |
| **Finished** | Done; read the verdict |
| **Merged ✓** | Done, and the PR was merged |
| **Stopped** | You stopped it |
| **Error** | Something failed — the conversation says what |

Two rules keep things safe:

- **The same PR twice is refused.** Both jobs would fight over the PR
  description and one set of findings would be lost.
- **Two jobs on the same clone are allowed, with a warning.** Each works in
  its own isolated copy, so different PRs (almost always different branches)
  run fully in parallel; only two jobs that happen to target the exact same
  branch still take turns.

The new-job form remembers your last job's settings, so reviewing five PRs in
one repo usually means changing only the PR id.

### Retrying a job

Once a job is finished, its `Stop` button becomes **`↻ Retry`** — in the jobs
list, and on the job's own detail screen. Click it to rerun that same job —
same row, same repo, PR id and settings; nothing to retype, and nothing new
added to the list. Its previous log and verdict are cleared and it goes
straight back to queued (or running, if a slot is free).

This is the normal way to handle "Request changes": fix the PR, come back,
click `↻ Retry`. PRISM recognizes it has already reviewed this PR and scopes
the new pass to what changed since — checking whether earlier findings were
addressed, and only skimming the rest for anything newly critical — instead
of reading the whole PR again from scratch. A PR PRISM hasn't reviewed
before (or a retry of a job that never got that far) just gets an ordinary
full review.

---

## Settings

Five checkboxes on the new-job screen, top to bottom:

| Setting | On means | Turn it off when |
|---|---|---|
| **Agentic PR review** | The reviewer agent runs and produces a verdict | You've already reviewed the PR yourself and just want PRISM to sync/merge it — the merge step then treats that as an explicit approval, same as an Approve verdict |
| **Update PR description after review** | Findings are written onto the PR for other reviewers to see | You want the review kept to yourself. Greyed out and forced off automatically when Agentic PR review is off — there's nothing to describe without one |
| **Sync with base branch when diverged** | If a fast-forward isn't possible, PRISM merges the base branch in and retries | You'd rather resolve that yourself |
| **Merge PR** | An approving (or skipped) verdict merges the PR | You want to merge by hand |
| **Dry run — skip writes and merges** | Nothing is written or merged; you just get the verdict | — |

Skipping the review never skips the final independent check before merging —
PRISM always confirms the PR is still open, unchanged, and fast-forwardable
right before it merges, review or no review.

**Use Dry run for your first go.** It gives you the full review with zero risk.

The **Model** picker is optional — leave it alone to use the default.

**Custom instructions**, just below it, is a free-text box — optional, and
empty by default. Anything you write there is handed to the reviewer
alongside its usual workflow, for that job only.

**Google Chat notifications**, on the Help screen, are also optional and off
by default. Paste in a webhook URL and click Save once — it applies to
every job from then on, not just the one you're about to run. A card is
posted **only when the reviewer produced a real, parseable verdict** —
Approve, Approve with comments, Request changes, or Block — with the PR
author, verdict, impact, and whether it merged. A skipped review, a run
whose verdict PRISM couldn't parse, or an internal error never posts —
those aren't review outcomes, and the group chat doesn't need to see them
next to real ones. Leave the webhook field empty to turn notifications off
entirely.

**Code-generation integration**, also on the Help screen, is optional and off by default.
Tick **Accept reviews from a code-generation tool** and PRISM listens (on this machine
only) for pull requests an external code-generation tool (*codegen*) opens. Each one shows up in the
Jobs list marked **via codegen** and is reviewed exactly like one you started
yourself — including the pause that asks you before merging a high-impact
change. If the verdict is *Request changes* or *Block*, PRISM sends the
findings back to codegen, which fixes the PR and resubmits; this repeats up to
five times, then stops for you. When the PR is approved PRISM merges it and
tells codegen. Untick **Merge approved pull requests from it** to
review only and merge by hand. Pressing **Stop** on a codegen job tells codegen it
was stopped. Nothing changes for jobs you start yourself.

**Reviewing the same change more than once.** When a PR carries changes PRISM
has already reviewed — a retry, another PR, or the same change on another
branch, even after a rebase or squash — PRISM hands the reviewer its earlier
findings, so the reviews agree. It doesn't matter how your branches are laid out. This memory is a small temporary file on this
machine (PRISM never writes to your repositories); if it has been cleared, or
the earlier review was on another machine, PRISM just reviews afresh.
The reviewer still checks the whole PR; the history is never an approval. If it
finds a problem the earlier reviews missed, it marks it as a **late find**, and
the job log and PR description say so: a fix made now was not covered by the
testing since that review, so test it again. A late Critical or High finding
still blocks the merge; a late Medium or lower one is reported but approves with
comments.

---

## The command line

Everything above also works without the window, which is how you review from
another machine: ssh in and run `prism`.

    cd ~/work/my-service
    prism run 214              review PR 214 of the repository you are in
    prism run 214 -d           the same, in the background (survives logout)
    prism jobs                 what is running      (--all for finished ones)
    prism logs <id> -f         follow a job's output
    prism stop <id>            stop it              (--all stops every one)
    prism retry <id>           run it again with the same settings
    prism ask <id> "…"         ask the reviewer about a finished job

PRISM reads the repository name and region from the clone's CodeCommit remote,
so the folder you are in is usually all it needs. Options mirror the New job
screen: `--no-review`, `--no-describe`, `--no-merge`, `--no-sync`, `--dry-run`,
`--model`, `--region`, `-i "extra instructions"`. If a folder holds several
clones, say which with `--repo <name>`.

**Saved defaults** live in `~/.prism/config.json`. Set them once:

    prism config set region eu-west-1
    prism config set merge no
    prism config                show them

A flag on the command always wins over a saved default.

**Questions.** In a terminal, PRISM asks you where the desktop app would show
its question box. A detached job has nobody to ask, so it takes the cautious
answer: it does not resolve a conflict and does not confirm a high-impact merge.

**In the desktop app.** The **CLI jobs** button in the header opens a list of
jobs started from the command line, with **Stop** and **↻ Retry**. The list
is for control — read a job's output with `prism logs`. The status pill counts
desktop and CLI jobs together; hover over it to see the split.

---

## Stopping and closing

**`Stop`** ends a job at any point and returns it to idle. It's safe: PRISM
checks between steps, so it won't interrupt a merge halfway. (An AWS call
already sent may still complete — the conversation panel will show it.)

**Closing the window asks first**, because **no job's verdict or conversation
is saved to disk** — they exist only while the window is open (see **Good to
know**, below, for the couple of small settings that *do* persist). The
prompt tells you exactly what would be lost, and defaults to staying open.

---

## Safety

PRISM is deliberately cautious:

- It merges **only** on ✅ or ⚠️ verdicts, and only if the PR is still open.
- It uses **fast-forward merges only** and never force-pushes.
- If a sync hits conflicts, PRISM never picks a side for you. It backs the
  merge out, shows you each conflict, and asks which version to keep.
- It refuses to sync when your working copy has uncommitted changes, and it puts
  you back on your original branch when it's done.
- AWS errors are shown exactly as AWS reported them. PRISM never tries to fix
  your credentials.
- **The reviewer reads your code; it never runs it.** It can't run a pull
  request's tests, builds or installers, can't create virtualenvs or containers,
  and can't commit, push or merge. (A PR's code is untrusted, and running it was
  what made some reviews very slow.)
- **PRISM only reviews — it never writes to a repository.** Its memory of earlier
  reviews is a small temporary file outside any repo, and the code-generation
  integration never adds a file, branch or commit.
- **The code-generation integration is off by default and local to your
  machine.** It can ask PRISM to merge *less* than you allow, never more; it can't
  skip the review, change the model or bypass a check, and the confirmation
  before merging a high-impact PR still asks **you**. It stops after a fixed
  number of rounds on one PR and hands over to you.

---

## If something goes wrong

| What you see | What it means |
|---|---|
| `Review engine is not installed or not on PATH` | Install `opencode`, or make sure it's on your PATH |
| `AccessDenied` or an expired token | Log in to AWS again, then re-run the job |
| `⏳ Still working — 3m10s since the reviewer last reported` | Normal. Models sometimes pause for minutes between steps. PRISM keeps waiting and only stops a review that has been silent for 10 minutes or has run for 45 |
| `(partial review)` next to a verdict | The review had to be stopped (the model stalled or ran out of time) and reported what it had covered. Its findings are written, but PRISM won't merge it automatically. Retry the job to finish it |
| `The review did not finish` | The model gave no usable answer even when asked to wrap up. Nothing was changed on the pull request. Retry — the model's provider may be slow right now |
| `Could not parse a verdict` | The reviewer didn't produce a clear verdict. Check the conversation, try a different model, run it again |
| `Backend and Frontend point to the same clone` | Pick two different clones in Repository mapping |
| A conflict appears in the question panel | Choose `Keep current` or `Take incoming` for each one, or `Abort sync` to stop and leave your clone untouched |
| `… is already being reviewed` | That PR already has a job. Open it from the list |
| Working clone has uncommitted changes | Commit or stash them, then re-run |
| Model list is empty | PRISM couldn't list models; runs will use the default |
| Help says the integration **could not start** | A fixed port is already in use or `~/.prism` isn't writable. Untick it and tick it again; the integration stays off, nothing else is affected |
| The code-generation tool never submits a PR | The integration is off, PRISM wasn't running, or the tool isn't set up for it. The tool retries for a while, then tells its own session |
| A job *via codegen* sits at **Needs input** | The reviewer or the high-impact merge confirmation is waiting for **you**, same as any job |
| "Review round limit" / PR handed back to you | The PR used all its automatic rounds without being approved. Review it by hand |
| A *late finding* warning | A problem an earlier review missed. The fix hasn't had the testing that followed that review, so test it again |

---

## Good to know

- **No job history survives a restart** — closing PRISM discards every job's
  conversation and verdict, same as always. Two small things do persist
  between sessions, both under `~/.prism/`: your settings (the Google Chat
  webhook and the code-generation integration), and a local record of which
  commit PRISM last reviewed on each PR (what makes retrying a job — see
  **Retrying a job**, above — faster instead of starting over). A third, short-lived
  thing lives in your system's temporary folder, not under `~/.prism/`: PRISM's
  memory of what it concluded about earlier reviews, used for **Reviewing the same
  change more than once**. The system may clear it at any time; PRISM then just
  reviews afresh.
- Each job keeps the last 5,000 lines of its conversation.
- Every job takes a snapshot of its settings when it starts, so changing the
  form afterwards never affects a job already running.
- The reviewer itself can't merge, approve or push. Every write goes through
  PRISM, under the settings you chose.

---

## Help and version

This manual is built into the application: click **`?  Help`** in the top
right to read it without leaving PRISM.

The version sits in the **bottom-right corner** of the window. Clicking it
opens this manual too.

---

## Keeping PRISM up to date

**PRISM checks once, quietly, when it starts.** If you're already on the
latest version, nothing happens — no popup, nothing to dismiss. If a newer
version exists, the same "update available" screen described below opens on
its own.

You can also check any time yourself: open **`?  Help`** and click **`Check
for updates`**.

- **You are up to date** — nothing to do.
- **A newer version exists** — you will see the version number and its release
  notes (written by hand for each release, not a raw commit list), and can
  choose **`Download and install`** or ignore it.

If you install, PRISM downloads the build for your operating system, checks it
against the checksum published alongside it, replaces itself, and offers to
restart. The previous version is kept next to the new one until the next
launch, so a failed update can never leave you without a working copy.

Two things to know:

- **Updating needs a restart**, and PRISM keeps nothing on disk. It will
  refuse to update while any job is running or queued — finish or stop them
  first.
- **If you run PRISM from source** (`./run.sh`), there is nothing to replace.
  Use `git pull` instead; the Help screen says so rather than offering a
  download.

Nothing is sent to GitHub except the request for the release list — whether
that check happens on its own at startup or because you asked for it.

---

For the full technical specification, see
[`docs/requirements/PRISM.md`](requirements/PRISM.md).
