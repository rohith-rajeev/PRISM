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

**2. Choose your working folder** with `Browse`.

This is the folder holding your code. PRISM looks inside it for git clones and
sets itself up accordingly:

- Finds **one** repository → fills in the repo name for you.
- Finds **several** → shows a *Repository mapping* card. Pick which clone is
  your backend and which is your frontend, then choose which one to review.
- Finds **none** → type the CodeCommit repository name yourself.

> The repository name box is editable on purpose: the name in AWS can differ
> from your local folder name.

**3. Enter the PR id** — just the number, e.g. `214`.

**4. Check the region** (defaults to `us-east-1`).

**5. Click `▶ Start Prisming`.**

That's it. PRISM opens the job's detail view and starts working.

---

## Watching it work

The **progress bar** fills through four stages:

```
Review  →  Describe  →  Merge check  →  Merge
```

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

---

## When the reviewer asks you something

Sometimes it needs your input — a missing detail, or two branches that could
both be the source. A **`💬 Reviewer needs your input`** panel appears with the
question and a text box.

Type a normal sentence and press **Enter** (or click `Send ▸`). The job carries
on with your answer.

Click **`Skip`** if you'd rather not answer — the job finishes with what it has.

**Merge conflicts appear here too.** If syncing your branch with its base hits a
conflict, PRISM does not resolve it. It undoes the merge, leaves your clone
exactly as it found it, and shows you each conflict in turn with both versions
and three buttons: **`Keep current`** (your branch), **`Take incoming`** (the
base branch), or **`Abort sync`**. Once you have chosen for every conflict,
PRISM replays the merge with your answers.

If this happens while you're looking at a different job, that job shows
**Needs input** in the list, and the header says so too. Nothing is missed.

---

## Running several PRs at once

Create as many jobs as you like. **Three run at a time**; the rest wait their
turn and start automatically.

The jobs list shows every job at a glance:

```
● example-service-be #12948   Running…   ⚠️ Approve with comments   ⚠️ 6/10   [Stop] [✕]
● example-portal-fe  #884                         Finished   ✅ Approve                 ✅ 2/10   [Stop] [✕]
```

Click a row to open it. `Stop` ends that job. `✕` removes it from the list.

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
- **Two jobs on the same clone are allowed, with a warning.** They take turns
  for git operations, so the second may wait a little.

The new-job form remembers your last job's settings, so reviewing five PRs in
one repo usually means changing only the PR id.

---

## Settings

Four checkboxes on the new-job screen:

| Setting | On means | Turn it off when |
|---|---|---|
| **Update PR description after review** | Findings are written onto the PR for other reviewers to see | You want the review kept to yourself |
| **Auto-merge once approved** | An approving verdict merges the PR | You want to merge by hand |
| **Sync with base branch when diverged** | If a fast-forward isn't possible, PRISM merges the base branch in and retries | You'd rather resolve that yourself |
| **Dry run — skip writes and merges** | Nothing is written or merged; you just get the verdict | — |

**Use Dry run for your first go.** It gives you the full review with zero risk.

The **Model** picker is optional — leave it alone to use the default.

---

## Stopping and closing

**`Stop`** ends a job at any point and returns it to idle. It's safe: PRISM
checks between steps, so it won't interrupt a merge halfway. (An AWS call
already sent may still complete — the conversation panel will show it.)

**Closing the window asks first**, because **PRISM saves nothing to disk**. Your
verdicts and conversations exist only while the window is open. The prompt tells
you exactly what would be lost, and defaults to staying open.

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

---

## If something goes wrong

| What you see | What it means |
|---|---|
| `Review engine is not installed or not on PATH` | Install `opencode`, or make sure it's on your PATH |
| `AccessDenied` or an expired token | Log in to AWS again, then re-run the job |
| `Could not parse a verdict` | The reviewer didn't produce a clear verdict. Check the conversation, try a different model, run it again |
| `Backend and Frontend point to the same clone` | Pick two different clones in Repository mapping |
| A conflict appears in the question panel | Choose `Keep current` or `Take incoming` for each one, or `Abort sync` to stop and leave your clone untouched |
| `… is already being reviewed` | That PR already has a job. Open it from the list |
| Working clone has uncommitted changes | Commit or stash them, then re-run |
| Model list is empty | PRISM couldn't list models; runs will use the default |

---

## Good to know

- Nothing is saved between sessions — no config file, no history.
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

Open **`?  Help`** and click **`Check for updates`**. PRISM asks GitHub whether
a newer release exists.

- **You are up to date** — nothing to do.
- **A newer version exists** — you will see the version number and its release
  notes, and can choose **`Download and install`** or ignore it.

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

Nothing is sent to GitHub except the request for the release list. PRISM only
checks when you ask it to — there is no background polling.

---

For the full technical specification, see
[`docs/requirements/PRISM.md`](requirements/PRISM.md).
