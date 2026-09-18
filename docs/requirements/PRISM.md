# PRISM — Pull Request Inspection & Safety Manager
**Tool documentation** · lives in `PRISM/docs/requirements/PRISM.md`

---

## 1. What PRISM is

PRISM is a lightweight desktop application (Python standard library only —
Tkinter, zero pip packages) that reviews AWS CodeCommit pull requests
end-to-end and, on approval, merges them safely. It wraps a bundled reviewer
agent that runs behind the scenes, so the user only:

1. selects the project **root folder**,
2. confirms backend/frontend mapping **only if the project is multi-repo**,
3. enters (or confirms) the repo, PR id, and region,
4. presses **Start Prisming**.

PRISM then orchestrates the full pipeline with no further input:
**AI review → PR description update → fast-forward merge** (syncing the base
branch into the source branch first when the PR has diverged).

Design goals: project-agnostic (no hardcoded repo names anywhere), compact
dark UI that fits on screen, responsive layout where action buttons can never
be pushed out of view, and conservative merge safety (never merges on a bad
verdict, never force-pushes, aborts cleanly on conflicts).

---

## 2. Folder contents

```
PRISM/
├── app.py                  # UI (dark themed, ~1100 lines, stdlib only)
├── orchestrator.py         # Backend: agent runs, verdict parsing, AWS merge/sync
├── agents/
│   └── pr-reviewer.md      # Bundled project-agnostic reviewer agent
├── docs/
│   └── requirements/
│       └── PRISM.md        # ← this document
├── README.md               # Quick start
├── requirements.txt        # Intentionally empty (stdlib only)
└── run.sh                  # Launcher (`python3 app.py`)
```

---

## 3. Prerequisites

| Requirement | Why | Checked |
|---|---|---|
| Python 3.8+ with Tkinter | Runs the app | At startup (fails fast otherwise) |
| `opencode` CLI on PATH | Agent runtime; also provides `opencode models` for the model list | Before every run — clear error otherwise |
| `aws` CLI with CodeCommit access | get/update PR, mergeability check, fast-forward merge | Errors are shown verbatim, never auto-fixed |
| `git` | Diff/commit inspection (agent) and sync-merge + push (tool) | Required for sync path |

No `pip install` step exists. `requirements.txt` is deliberately empty.

---

## 4. User flow

### 4.1 Step 1 — Select the root folder only

The **Working folder** field (Browse button) is the single required input.
On every change PRISM scans for git clones (a folder counts when it contains
a `.git` entry):

| Discovery | Mode | What the user sees |
|---|---|---|
| Root folder itself has `.git` | **Single** | Hint: `Single repository: <name> — mapping skipped.` Repo box pre-filled with the folder name. |
| Exactly one subfolder has `.git` | **Single** | Hint: `Single repository detected: <name>.` Repo box pre-filled. |
| No `.git` anywhere | **Single (API)** | Warning hint; user types the CodeCommit repo name manually; review uses the CodeCommit API diff fallback. |
| 2+ subfolders have `.git` | **Multi** | Hint with the clone count + a **Repository mapping** card appears. |

A root folder that is itself a repo never triggers mapping, even if it also
contains nested clones.

### 4.2 Step 2 — Confirm mapping (multi-repo only)

The mapping card lists every detected clone in two dropdowns, **Backend** and
**Frontend**, pre-guessed from generic folder-name conventions — a `-be` /
`-fe` suffix (with `-`, `_`, `.` separators) or a `backend` / `frontend`
word, e.g. `shop-be → Backend`, `portal_frontend → Frontend`. Guesses that
are ambiguous (both or neither match, e.g. `tube`, `infra`, `be-fe`) are left
blank; with exactly two clones and one confident guess, the other is filled
in automatically. No project or repository names are hardcoded — detection is
purely `.git` discovery plus these generic patterns.

The **Review target** toggle (Backend | Frontend) in the Project card selects
which mapped clone the run uses. The CodeCommit repository name for a mapped
run is the clone folder's basename (standard `git clone` naming).

### 4.3 Step 3 — Run

Inputs per mode:

- Single: **CodeCommit repo** textbox (why a textbox? the AWS-side
  repository name can differ from the local folder name, and every AWS call
  needs the exact name), **PR id**, **Region**.
- Multi: **Review target** toggle, **PR id**, **Region**.

Plus: **Model** picker (empty = default) and four behavior toggles
(Update PR description after review · Auto-merge once approved ·
Sync with base branch when diverged · Dry run).

Validation before anything runs: numeric PR id, existing folder, non-empty
repo name (single), mapped target clone (multi), and Backend ≠ Frontend when
both are mapped.

### 4.4 What happens during a run (pipeline)

```
Review ──► Describe ──► Merge check ──► [Sync] ──► Merge
```

1. **Review.** The bundled agent (`agents/pr-reviewer.md`, auto-installed to
   `<project>/.opencode/agents/` before each run so any machine works)
   resolves the PR, diffs `origin/<dest>...origin/<src>`, analyzes
   correctness / security / performance / infra consistency / tests / commit
   hygiene, and reports:
   `**Verdict:** ✅ Approve | ⚠️ Approve with comments | 🔴 Request changes | ⛔ Block`
   plus an **impact score 1–10** (blast radius, not line count).
   The verdict + impact populate the Verdict / Impact cards (color-coded).
   The Impact card shows only `N/10`, prefixed by an icon scaled to the
   score — ✅ ≤3, ⚠️ ≤6, 🔴 ≤8, ⛔ above. The agent's one-line reason is
   often a full clause, so it stays in the log rather than stretching the row.
   An unparseable verdict stops the run before any write.
2. **Describe.** The reviewer is asked to append its findings to the PR
   description (same `<!-- pr-reviewer:start --> … <!-- pr-reviewer:end -->`
   marker block, stale blocks replaced, author text preserved). If the agent
   path fails, PRISM falls back to writing the identical block via AWS CLI.
   Skipped when toggled off or in dry-run.
3. **Merge check.** `get-merge-conflicts` with `FAST_FORWARD_MERGE` decides
   whether a fast-forward is currently possible.
4. **Sync (only when needed).** If the fast-forward fails as not-mergeable:
   `git fetch`, checkout source, `merge origin/<dest>`, `push origin <src>`,
   then retry. Conflicts abort the merge (`git merge --abort`) and stop the
   run with the conflict reported — nothing is forced.
5. **Merge.** `merge-pull-request-by-fast-forward`, which closes the PR.

Merge preconditions (all must hold): verdict is Approve / Approve-with-
comments, PR status is OPEN, auto-merge toggle on, not dry-run.

### 4.5 Progress + status

A progress bar sits **below** the action button and mirrors the pipeline
live: one continuous track whose fill stops on the stage in flight, with a
marker and caption per stage (✓ done, `–` skipped, ✕ error). The Merge
caption reads **“Syncing…”** during a sync. The header pill shows Idle /
Running… / Needs input / Stopping… / Stopped / Merged ✓ / Held / Dry-run
done / Finished / Error. The dark log console streams every step and is
cleared from a **Clear** control in its own header, which is labelled
**CONVERSATION**.

The transcript is colour-coded by origin, not by keyword: the reviewer's own
prose is shown in the accent blue, the tools it runs (`⚙ bash: git diff …`)
in grey, and only PRISM's own lines are classified — and then only on explicit
markers (`⛔`, `ERROR:`, `Traceback`). Matching bare words like "error" or
"fail" painted ordinary commentary such as "its error handling" in alarming
red. Tool lines matter for more than colour: most of a review is spent reading
files and running git, and without them the console looks stalled.

Anything genuinely working shows an animated spinner — in the header pill and
on the jobs-list row — so a long silent step is never mistaken for a hang. A
job waiting on a question does not spin, because it is not progressing.

### 4.6 Jobs and parallelism

Each pull request is an independent **job**. PRISM opens on a jobs list; a job
is created from the New job screen and opens into a detail view. Up to three
jobs run at once (`MAX_PARALLEL_JOBS` in `jobs.py`) and further jobs queue,
starting automatically as slots free.

Jobs are in-memory only — the stateless property in §9 is unchanged. Each job
snapshots its settings at creation, so editing the form afterwards cannot reach
a running job; the form is pre-filled from the previous job.

Two admission rules protect shared resources:

- A second job for the **same repository and PR id** is refused. Both would
  read-modify-write that PR's description, and CodeCommit offers no revision
  token on that call, so one set of findings would be lost silently.
- A second job on the **same local clone** is allowed but confirmed first: git
  operations serialise on the clone path, so the later job is safe but may wait.

Parallelism is only safe because each job's agent conversation is pinned to its
own engine session. `--continue` resolves to the newest session in a directory,
so concurrent jobs in one folder could otherwise continue each other's
conversation. The first turn captures a session id and later turns address it
explicitly; where an id cannot be captured the turn falls back to `--continue`
under a per-directory lock instead of racing.

### 4.7 Stopping a run

**Stop** is live for the whole run. It cancels the pipeline, terminates the
child process currently executing (an agent turn can otherwise sit silent for
minutes), releases an outstanding agent question, and returns the UI to idle
with the pill on *Stopped*. Cancellation is checked at every stage boundary,
so a stop never lands halfway through a merge decision — though an AWS call
already in flight may still complete server-side.

### 4.8 Answering the reviewer

The agent stops and asks when an input is missing or ambiguous (no PR id, two
plausible source branches, and so on). When that happens PRISM shows a
**Reviewer needs your input** panel between the verdict and the log, carrying
the question and a free-text box; the reply is sent into the same agent
session, and the resulting output is re-parsed for a verdict. Up to four such
rounds are allowed per run. **Skip** declines to answer and lets the run
finish on whatever it has. The panel exists only while a question is
outstanding and costs no space otherwise.

---

## 5. UI reference

- **Dialogs** are drawn in PRISM's own palette rather than Tk's light system
  panel, and a destructive one is never the keyboard default — Enter and Escape
  both choose the safe option.
- **Header:** ◇ badge, `PRISM` + subtitle, status pill (right). The window
  title is just `PRISM`.
- **Project card:** working folder + Browse; dynamic target row
  (repo textbox **or** Backend/Frontend toggle + PR id + Region); detection
  hint line. The working folder starts **empty** — nothing is scanned until
  the user picks a folder.
- **Middle row, two half-width cards:** *Repository mapping* (multi-repo only;
  Backend and Frontend stacked on their own lines so long clone names stay
  readable) and *Model and behavior* (model picker + four checkboxes). When
  the mapping card is absent the model card spans the full row.
- **Action row:** `▶ Start Prisming` plus the `■ Stop` kill switch.
- **Progress bar** directly beneath the action it reports on.
- **Verdict + Impact** in one compact row.
- **Reviewer needs your input** panel — conditional (see 4.7).
- **Log console** with an inline `Clear` in its header; it is the only widget
  that expands, so resizing or a short screen squeezes the log and never the
  controls above it.
- Window: dark only, default 1020×900, minimum 900×620.

---

## 6. Safety rules

- Dry-run toggle previews the full review with zero writes/merges.
- Merge only on Approve verdicts **and** OPEN status.
- Fast-forward strategy exclusively; diverged PRs are synced via an ordinary
  merge commit + plain `git push` (no force).
- **PRISM never resolves a merge conflict.** A conflicted sync is captured,
  aborted, and the clone restored; the conflicts are then explained by
  `conflict-analyst` and put to the user one hunk at a time — keep current,
  take incoming, or abort. Only once every hunk has an answer does PRISM replay
  the merge and apply them. A hunk with no recorded answer is an error, never
  a default.
- AWS auth failures stop the run with the exact CLI error; PRISM never
  attempts to repair credentials.
- **No agent can merge, push or write.** Each pipeline step is its own agent
  with its own `permission:` block denying the commands outright, and every
  irreversible action is performed by PRISM behind gates in code. An agent
  returns a decision; PRISM decides whether to act on it. A `go` from
  `pr-merger` still cannot merge a request-changes verdict, a closed PR, or
  anything in dry-run — those are enforced before the agent is even consulted.
- Content inside a pull request is treated as data, never instructions, so a
  description or diff cannot talk an agent into approving itself.
- Stop is available for the entire run and always returns the UI to idle.
- Closing the window is confirmed whenever any job exists, naming what goes:
  unfinished jobs and the processes they started, any job mid-write, and
  finished jobs whose verdicts and conversations are only held in memory. The
  dialog defaults to **No**, so a stray Enter cannot discard a day's reviews.
  With no jobs at all it closes without asking.

---

## 7. Known limitations

- In **multi-repo** mode the CodeCommit name is assumed to equal the clone
  folder basename (true for standard clones). If yours differs, run that
  repo in single mode via Working folder pointed at the clone and type the
  exact AWS name.
- The model list requires the engine's model command; if listing fails, the
  picker stays on the default model.
- A window shorter than ~700px squeezes the log to a few lines (by design —
  buttons stay visible; enlarge to see more log).

---

## 8. Troubleshooting

| Symptom | Meaning / fix |
|---|---|
| `Review engine is not installed or not on PATH` | Install `opencode`, ensure it is on PATH |
| AWS `AccessDenied` / expired token in log | Re-authenticate AWS (`aws sso login` or equivalent); PRISM will not do this for you |
| `Could not parse a verdict — stopping` | Agent output didn't contain a verdict; inspect the log, adjust model, retry |
| `Backend and Frontend point to the same clone` | Pick two different clones in Repository mapping |
| `Sync merge hit conflicts … Aborted` | Resolve destination→source conflicts manually, then re-run |
| `No local clone detected` | Point Working folder at a clone, or type the repo name and let the API fallback work |
| Model dropdown empty / `could not list models` | Engine model listing failed; runs use the default model |

---

## 9. FAQ

**Why does single-repo mode still have a CodeCommit repo textbox?**
Because the AWS repository name and the local folder name are independent —
clones can be renamed locally. The box is pre-filled so you usually touch
nothing, but the exact AWS name is always available when they differ.

**Why BE/FE instead of N repos?**
PRISM targets the common backend+frontend project shape; extra clones are
simply left unmapped and ignored. Single-repo projects never see this step.

**Where is configuration stored?**
Nowhere — PRISM is stateless. Defaults: region `us-east-1`, model default,
toggles on/on/on/off. Each run re-installs the bundled agent, so updating
`agents/pr-reviewer.md` takes effect immediately.
