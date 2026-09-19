# PRISM — Pull Request Inspection & Safety Manager

Lightweight, project-agnostic desktop UI (Python **stdlib only** — Tkinter,
no pip packages) that reviews AWS CodeCommit pull requests end-to-end for
**any** repository.

## Jobs
Each pull request runs as its own **job**, and several run at once. PRISM opens
on a jobs list showing every job's status, verdict and impact; clicking one
opens the detailed view (progress, conversation, agent questions, Stop). Three
jobs run concurrently by default and the rest queue, starting automatically as
slots free.

Jobs are in-memory only — PRISM stays stateless, so closing the window stops
everything. Each job snapshots its settings when created, so setting up the
next one never disturbs a job already running, and the new-job form is
pre-filled from the last job (usually only the PR id changes).

## What it does
1. You pick the project **root folder** — that's the only required input.
   PRISM scans it for `.git` clones:
   - **Single repo** (the folder itself is a repo, or holds exactly one
     clone): the mapping step is skipped entirely; the CodeCommit repo box
     is pre-filled from the folder name (editable, since the AWS-side repo
     name can differ from the local folder name).
   - **Multi repo** (2+ clones): a Repository mapping card appears where you
     confirm which clone is **Backend** vs **Frontend** (pre-guessed from
     generic `-be`/`-fe`/`backend`/`frontend` naming — no project names are
     hardcoded), then pick the review target, PR id, and region.
2. A specialised agent runs each step — `pr-context-resolver`, `pr-reviewer`,
   `fast-forward-merge-checker`, `pr-sync`, `conflict-analyst`, `pr-merger` —
   each with its own prompt and its own permissions. Agents decide; PRISM
   performs every write behind gates in code. The reviewer reports a
   **Verdict** (Approve / Approve with comments / Request changes / Block)
   plus an **impact score**.
3. PRISM composes the findings block itself from the review's own output —
   no agent call, so nothing here can be bounced by a rate limit or a
   provider outage — and writes it to the **PR description** (markers
   preserved, stale blocks replaced).
4. On ✅ Approve / ⚠️ Approve with comments it merges with
   **fast-forward only** (`merge-pull-request-by-fast-forward`).
5. If the PR is not fast-forward mergeable, it **syncs destination →
   source** (`git fetch + merge origin/<dest> + push`) and retries the merge.
6. 🔴 Request-changes / ⛔ Block verdicts are **never merged**.

The conversation pane shows the reviewer's prose in blue and the tools it runs
in grey, so you can watch it work; anything running carries a spinner so a long
step never looks like a hang. A progress bar under the **Start Prisming**
button tracks the run: the fill
stops on the stage in flight, with a marker and caption per stage. The plan is
dynamic per PR — describe/merge stages show as skipped when disabled, blocked
by verdict, or dry-run, and the Merge caption switches to "Syncing…" when a
fast-forward needs a destination→source sync first.

**Stop** cancels at any point — it terminates the running child process and
returns to idle. If the reviewer asks a question (missing PR id, ambiguous
source branch), a **Reviewer needs your input** panel appears with the
question and a free-text box, and your reply goes straight back into the same
agent session.

## Run

From a source checkout (no install step — stdlib only):

```bash
cd PRISM
./run.sh          # or: python3 app.py
```

Or build a standalone desktop executable for Linux, macOS or Windows:

```bash
python3 -m pip install -r desktop/requirements-build.txt
python3 desktop/build.py          # -> dist/PRISM, dist/PRISM.app or dist/PRISM.exe
```

PyInstaller can't cross-compile, so each OS builds on its own machine — push to
`main`/`develop` and the bundled GitHub Actions workflow builds all three and
uploads them as artifacts. See [`desktop/README.md`](desktop/README.md).

## UI
Four screens behind one header (badge, live job tally, Help, back to the list):

- **Jobs** — one row per job: status, target, verdict, impact, Stop, dismiss.
- **New job** — Project card plus a two-column row pairing Repository mapping
  with Model & behavior (the model card spans the row for single-repo
  projects), then `Start Prisming`.
- **Job detail** — a one-line target header with `Stop`, a progress bar, a
  compact Verdict + Impact row, the conditional reviewer-question panel, and
  the conversation console with an inline Clear.
- **Help** — the user manual, rendered from `docs/MANUAL.md` into the app's own
  palette, plus `Check for updates`.

The version sits quietly in the bottom-right corner and opens Help when
clicked. The shipped theme is dark only — there is no
theme toggle. All custom widgets are hand-drawn stdlib Tkinter canvas — still
zero dependencies.

## Model selection
The Model field is a custom dark-themed picker (no native widget styling
clashes). It lists the same `provider/model` ids the engine offers,
**grouped under provider headers**, with type-to-filter search (debounced and
capped to the first 60 matches, so a long list stays fast to type into),
mouse-wheel scrolling, and Esc / ✕ / click-outside to dismiss. It is
fixed-width (never stretches full-width); the live count sits inline next to
it. It defaults to `opencode/big-pickle`, so a first run works without having
to pick from the list — change it to anything the engine offers. The choice is
passed as `--model …` to every agent run in the pipeline.

## Self-contained agent bundle
The tool ships every agent it uses in `agents/`, one per pipeline step, each
project-agnostic and each with its own `permission:` block. Before every run
PRISM installs the whole folder into `<project>/.opencode/agents/`, so it works
on any machine with the engine + `aws` installed — no dependency on any other
skill, agent, or checkout outside this folder.

No agent's output goes to the pull request directly — PRISM parses the
reviewer's own report and composes the PR description itself (see above).
Every agent denies `edit`, `task`, `webfetch`, and — where it has a shell at
all — the specific `git push` and `aws codecommit merge/update` commands.
`agents/_shared-contract.md` documents the decision block they all answer
with.

## Updating
`?  Help` → `Check for updates` asks GitHub for the latest release. If it is
newer, PRISM shows the version and its notes, and can install it: the asset for
the running platform is downloaded, checked against the `SHA256SUMS` published
beside it, unpacked and swapped in, then PRISM offers to restart.

- The previous copy is renamed rather than deleted, so a failed swap rolls back
  and a locked executable on Windows still updates. It is removed at the next
  launch.
- It refuses while any job is running or queued — updating restarts a program
  that keeps nothing on disk.
- From a source checkout there is nothing to replace; it says to `git pull`.
- Checking happens only when asked. There is no background polling.

## Releasing
**A merge into `main` is a release.** The workflow works out the next version,
writes it to `version.py`, commits that, tags it and publishes — no manual
step.

Versions move a tenth at a time and carry into the whole number at `.9`:

    2.0 → 2.1 → … → 2.8 → 2.9 → 3.0 → 3.1 → …

There is deliberately no `2.10`. The rule lives in `version.next_version()`
beside the version it governs, so it is importable and unit-tested rather than
buried in YAML. The highest of the existing tags and `version.py` is the base,
so a tag made by hand is never handed out twice.

Each release attaches `PRISM-linux-x86_64.tar.gz`, `PRISM-macos-arm64.zip`,
`PRISM-windows-x86_64.zip` and `SHA256SUMS`. Workflow artifacts are not used:
they need a token even on a public repository and expire on a retention clock.

Two details that are easy to get wrong and are deliberate here:

- **The build runs from the bumped commit**, not from the merge that triggered
  it. Building the merge would ship a binary whose `version.py` still held the
  previous number, so it would announce itself as out of date the moment it
  started.
- **Tagging and publishing happen in one workflow run.** A push made with
  `GITHUB_TOKEN` does not start a new workflow, so tagging and waiting for the
  tag to trigger the release would publish nothing at all.

Pushing a `v*` tag by hand still works and still publishes; CI refuses it if it
disagrees with `version.py`.

## Prerequisites
- `opencode` on PATH (provides the agent runtime + model list). PRISM also
  looks in `~/.opencode/bin` and `~/.local/bin`, so launching from a desktop
  shortcut (which doesn't source your shell profile) still works.
- `aws` CLI (CodeCommit access) + `git`

## Files
- `app.py` — PRISM UI (four screens, threaded, live log, dry-run toggle)
- `jobs.py` — job model and scheduler (no Tkinter, unit-tested)
- `orchestrator.py` — backend: reviewer runs, verdict parsing, CodeCommit merge/sync
- `updater.py` — release check, download, checksum and in-place swap (no Tkinter, unit-tested)
- `version.py` — the version, in one place, plus the rule for what comes next
- `tests/` — `python3 -m unittest discover -s tests`
- `agents/` — one agent per pipeline step, plus `_shared-contract.md`
- `desktop/` — packaging into a standalone executable (build-time only)
- `docs/MANUAL.md` — the user manual, also shipped as the in-app Help screen
- `docs/requirements/PRISM.md` — full tool documentation
- No runtime dependencies: `requirements.txt` intentionally empty.

## License

[MIT](LICENSE).

## Safety
- Dry-run toggle previews without any writes/merges.
- Merge only on Approve verdicts + PR status OPEN.
- Sync conflicts are never resolved by PRISM: they are captured, the clone is
  restored, and each hunk is put to you — keep current, take incoming, or
  abort — before the merge is replayed with your answers.
- No agent can merge, push or write. Agents decide; PRISM acts, behind gates in
  code that a prompt cannot reach.
- Closing the window asks first whenever any job exists — PRISM keeps nothing
  on disk, so that is the only copy of your verdicts — and defaults to *No*.
- A sync refuses to start on a dirty working tree, refuses to push commits that
  exist only in your clone, and returns the clone to the branch you had checked
  out when it finishes.
- The PR description write is verified against the live PR, not the engine's
  exit code; if the reviewer didn't write the marker block, the direct AWS-CLI
  fallback does.
- Each job's agent conversation is pinned to its own engine session, so two
  jobs in one project folder can never continue each other's conversation.
- Git operations on a shared clone are serialised, and two jobs for the same
  pull request are refused outright.
