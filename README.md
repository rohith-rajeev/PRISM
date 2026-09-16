# PRISM — Pull Request Inspection & Safety Manager

Lightweight, project-agnostic desktop UI (Python **stdlib only** — Tkinter,
no pip packages) that reviews AWS CodeCommit pull requests end-to-end for
**any** repository.

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
2. A bundled reviewer agent runs behind the scenes and reports a
   **Verdict** (Approve / Approve with comments / Request changes / Block)
   plus an **impact score**.
3. PRISM asks the reviewer to append its findings to the **PR description**
   (marked block, stale blocks replaced), with a direct AWS-CLI fallback.
4. On ✅ Approve / ⚠️ Approve with comments it merges with
   **fast-forward only** (`merge-pull-request-by-fast-forward`).
5. If the PR is not fast-forward mergeable, it **syncs destination →
   source** (`git fetch + merge origin/<dest> + push`) and retries the merge.
6. 🔴 Request-changes / ⛔ Block verdicts are **never merged**.

A segmented progress control (Review / Describe / Merge check / Merge)
tracks the run: the active segment is the current task, checked segments are
done, dimmed ones are pending or skipped. The plan is dynamic per PR —
describe/merge stages show as skipped when disabled, blocked by verdict, or
dry-run, and the Merge segment switches to "Syncing…" when a fast-forward
needs a destination→source sync first.

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
Header with status pill, Project card, Model & behavior card, segmented
progress (Review / Describe / Merge check / Merge), full-width run button,
Verdict + Impact cards, dark log console, and split Copy-verdict / Clear-log
buttons. The shipped theme is dark only — there is no theme toggle. All
custom widgets are hand-drawn stdlib Tkinter canvas — still zero
dependencies.

## Model selection
The Model field is a custom dark-themed picker (no native widget styling
clashes). It lists the same `provider/model` ids the engine offers,
**grouped under provider headers**, with type-to-filter search, mouse-wheel
scrolling, and Esc / ✕ / click-outside to dismiss. It is fixed-width (never
stretches full-width); the live count sits inline next to it. Leave it empty
to use the default model. The choice is passed as `--model …` to both
reviewer runs (review + description update).

## Self-contained reviewer bundle
The tool ships its own copy of the reviewer agent at
`agents/pr-reviewer.md` — a project-agnostic version that takes the repo
name, PR id, region, and clone path from each run's prompt. Before every run
PRISM installs that file into `<project>/.opencode/agents/pr-reviewer.md`,
so it works on any machine with the engine + `aws` installed — no dependency
on any other skill, agent, or checkout outside this folder.

## Prerequisites
- `opencode` on PATH (provides the agent runtime + model list). PRISM also
  looks in `~/.opencode/bin` and `~/.local/bin`, so launching from a desktop
  shortcut (which doesn't source your shell profile) still works.
- `aws` CLI (CodeCommit access) + `git`

## Files
- `app.py` — PRISM UI (threaded, live log, dynamic progress bar, dry-run toggle)
- `orchestrator.py` — backend: reviewer runs, verdict parsing, CodeCommit merge/sync
- `agents/pr-reviewer.md` — bundled project-agnostic reviewer agent
- `desktop/` — packaging into a standalone executable (build-time only)
- `docs/requirements/PRISM.md` — full tool documentation
- No runtime dependencies: `requirements.txt` intentionally empty.

## Safety
- Dry-run toggle previews without any writes/merges.
- Merge only on Approve verdicts + PR status OPEN.
- Sync conflicts abort cleanly (`git merge --abort`) and are reported, never forced.
- A sync refuses to start on a dirty working tree, refuses to push commits that
  exist only in your clone, and returns the clone to the branch you had checked
  out when it finishes.
- The PR description write is verified against the live PR, not the engine's
  exit code; if the reviewer didn't write the marker block, the direct AWS-CLI
  fallback does.
