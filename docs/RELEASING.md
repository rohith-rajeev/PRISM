# Releasing PRISM

A checklist for whoever (person or agent) ships a version. Follow it in order; the
reasons are given because several steps look optional and are not.

## How a release works

- Work happens on `develop`. **A merge into `main` is the release**: the workflow
  `.github/workflows/build-desktop.yml` tags the next version, rewrites
  `version.py`, builds Linux/macOS/Windows binaries, and publishes the GitHub
  release with the notes taken from `CHANGELOG.md`.
- **Never tag by hand and never push a tag yourself.** CI does it. A hand-made tag
  either collides with the one CI is about to make or is rejected by the job
  "Refuse a tag that disagrees with version.py".
- **Never bump `version.py` for a normal (minor) release.** CI works the number out
  (`version.next_version`: the highest of every `v*` tag and `version.py`, plus one
  tenth; 6.9 is followed by 7.0, there is no 6.10). Setting `6.5` or `6.5.0` by hand
  makes CI tag 6.6 and skip a version. "v6.5.0" in a request means the release
  `v6.5`; the third number is only for fix-ups (below).
- **Fix-up releases (6.4.1, 6.4.2)** are the exception: edit `version.py` yourself
  to the fix-up number, and merge with `[skip release]` in the merge message so CI
  does not also cut a normal release. (These publish no binaries; the next minor
  release does.)

## Before you start

1. `git status` - note any uncommitted change that is not yours (for example a
   `.gitignore` edit) and leave it out of the release commit.
2. `git fetch origin --tags`, then confirm `develop` contains `origin/develop` and
   `origin/main`'s history (`git merge-base --is-ancestor origin/main develop`).
   If main is ahead (CI's "Release vX" commit, or a `[skip release]` merge), merge
   `origin/main` into `develop` first.
3. Local `main` is often stale: always `git checkout main && git pull --ff-only`
   before merging into it.

## The change itself

4. **Tests green**: `python3 -m unittest discover -s tests -q` (takes ~2 minutes;
   needs `git`, no network, no display). Add tests for what you built.
5. **Compile check** (what CI runs): `python3 -m compileall -q app.py
   orchestrator.py jobs.py updater.py version.py prism_cli.py cli_jobs.py
   folderpicker.py desktop/`.
6. **New module?** Add it to `desktop/build.py` twice: the `preflight()` file list
   and a `--hidden-import`. PyInstaller will not always find it.
7. **New setting?** It lives in `~/.prism/config.json` (`config.py`). Upgrades never
   touch that folder, so a setting survives releases. Keep old files readable (new
   keys optional, wrong types ignored). If it is a CLI-visible key, add it to
   `prism_cli.CONFIG_KEYS_HELP` and to `man/prism.1` (a test requires every key to be
   documented there).
8. **A secret (webhook URL, token) never goes into a job record or onto a command
   line.** Read it from config in the process that uses it.
9. **UI change?** Check the Help screen at the minimum window size (900x620): the
   manual pane must keep usable height. `tests/render_scene.py` renders screens.

## Documentation (all of it, every release)

10. `CHANGELOG.md`: add `## vX.Y` **at the top, in the form `## v6.5`**. The release
    notes step matches the heading against the tag with `^## <tag>( |\(|$)`, so
    `## v6.5.0` would find nothing and publish "No changelog entry". Write what a
    user would notice, short; this text is shown in the in-app update screen.
11. `docs/MANUAL.md` (built into the app, also shown under Help), `README.md`, and
    `man/prism.1`: anything user-visible, including CLI flags, config keys and
    output formats.
12. `agents/*.md` and `agents/_shared-contract.md` if reviewer behaviour or report
    formats changed (PRISM parses those formats; change both sides together).

## Ship it

13. Commit on `develop`: message `vX.Y: <one-line summary>`, with the
    Co-Authored-By trailer the session requires. One commit per release is the
    convention. Do not commit unrelated local edits.
14. `git push origin develop`.
15. Merge into main:
    ```
    git checkout main && git pull --ff-only origin main
    git merge --no-ff develop -m "Merge develop (vX.Y) into main"
    git push origin main
    ```
    (add `[skip release]` to that message only for a fix-up or a no-ship change.)
16. **Watch CI**: `gh run list --branch main --limit 3`, then `gh run watch <id>`.
    It must produce: a `Release vX.Y` commit on main (the `version.py` bump), the tag
    `vX.Y`, three platform builds, and a GitHub release with `SHA256SUMS` and notes.
    If a step fails, fix forward on `develop` (a new fix-up commit), never delete or
    move a published tag.
17. **Sync back**: `git checkout develop && git pull origin main` (fast-forward or a
    merge) so develop carries CI's `version.py` bump. Skipping this makes the next
    release's version file diverge.
18. Verify: `gh release view vX.Y` shows the notes and the assets; `git tag -l 'vX.*'`
    lists the tag; optionally run `prism update` from an older build.

## Things that have gone wrong before

- Heading `## v6.5.0` in the changelog (notes not found).
- Hand-edited `version.py` on a minor release (skipped a version).
- A tag pushed by hand racing CI.
- A webhook URL written into a job record that `prism jobs --json` then printed.
- A new Help card pushing the manual pane to zero height on a small window.
- A new module missing from `desktop/build.py`, so the packaged app could not import it.
