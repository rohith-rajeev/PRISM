# Desktop builds

Packages PRISM into a standalone executable so it can be launched without a
Python checkout. The app itself stays stdlib-only — [PyInstaller](https://pyinstaller.org)
is a **build-time** dependency, never a runtime one.

| Platform | Output | How it's launched |
|---|---|---|
| Linux | `dist/PRISM` + `dist/PRISM.desktop` + `dist/PRISM.png` | double-click, or install the `.desktop` entry |
| macOS | `dist/PRISM.app` (and a bare `dist/PRISM` folder) | double-click the `.app` |
| Windows | `dist/PRISM.exe` | double-click |

## Build

```bash
python3 -m pip install -r desktop/requirements-build.txt
python3 desktop/build.py
```

Flags: `--onedir` (folder layout — slower to copy, faster to start),
`--no-icon`, `--keep-build` (keeps intermediates for debugging).

macOS always builds `--onedir` regardless of this flag: a `--onefile` build
re-extracts itself into a fresh `$TMPDIR` directory on every launch, and if
that extraction is incomplete or gets swept mid-run (a disk-cleanup tool, low
disk space, a cached extraction from days ago getting reaped), the app fails
with errors like "bundled agents missing" that a user can't fix from inside
it. An `.app` bundle from `--onedir` runs its files straight out of
`Contents/`, with no such step. Linux and Windows keep `--onefile`.

The build needs a Python with **Tkinter**. If `build.py` stops with a tkinter
error: `sudo apt install python3-tk` on Debian/Ubuntu, `brew install python-tk`
(or a python.org build) on macOS, or re-run the Windows installer with the
*tcl/tk and IDLE* option ticked.

## One platform per machine

PyInstaller does not cross-compile — it wraps the interpreter it is running
under, so a macOS app must be built on macOS, a `.exe` on Windows, and so on.
There is no flag that changes this.

To produce all three from one push, use the bundled GitHub Actions workflow:

```bash
git push origin develop      # builds all three, artifacts on the run page
```

`.github/workflows/build-desktop.yml` runs this same `build.py` on
`ubuntu-latest`, `macos-latest` and `windows-latest`, and uploads each result
as a downloadable artifact. Pushing a `v*` tag additionally attaches them to a
GitHub Release.

## Runtime prerequisites

The bundle contains the PRISM UI and its reviewer agent — **not** the external
tools it drives. The machine still needs, on `PATH`:

- `opencode` — the agent runtime (also probed at `~/.opencode/bin`, `~/.local/bin`)
- `aws` — CodeCommit access, already authenticated
- `git` — diff inspection and the destination→source sync

## Installing the Linux desktop entry

Copy the binary somewhere stable first — a launcher pointing into a source
checkout breaks the moment the repo moves:

```bash
install -Dm755 dist/PRISM     ~/.local/opt/prism/PRISM
ln -sfn ~/.local/opt/prism/PRISM ~/.local/bin/prism
install -Dm644 dist/PRISM.png ~/.local/share/icons/hicolor/256x256/apps/prism.png

sed -e "s|^Exec=.*|Exec=$HOME/.local/opt/prism/PRISM|" \
    -e "s|^Icon=.*|Icon=prism|" \
    dist/PRISM.desktop > ~/.local/share/applications/prism.desktop

update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor 2>/dev/null || true
```

For a system-wide install use `/opt/prism` and `/usr/share/...` instead (needs
root). The generated entry sets `StartupWMClass=Prism`, which matches the
`WM_CLASS` the app sets via Tk's `className` — that is what makes a running
window group under its own dock icon instead of a generic "Tk" one.

## Code signing

The workflow produces **unsigned** binaries. Unsigned apps are blocked by
default on recent macOS (Gatekeeper) and warned about by Windows SmartScreen:

- macOS: `xattr -dr com.apple.quarantine /Applications/PRISM.app`, or
  right-click → Open the first time. For distribution, sign and notarise with
  an Apple Developer ID.
- Windows: *More info → Run anyway*, or sign with an Authenticode certificate.

Signing needs paid certificates and secrets, so it is deliberately left out of
the public workflow.

## Files

- `build.py` — preflight checks, icon generation, PyInstaller invocation
- `make_icon.py` — stdlib-only PNG/ICO/ICNS generator for the ◇ badge
- `requirements-build.txt` — pinned PyInstaller range
