# Desktop builds

Packages PRISM into a standalone executable so it can be launched without a
Python checkout. The app itself stays stdlib-only — [PyInstaller](https://pyinstaller.org)
is a **build-time** dependency, never a runtime one.

| Platform | Output | How it's launched |
|---|---|---|
| Linux | `dist/PRISM` + `dist/PRISM.desktop` + `dist/PRISM.png` | double-click, or install the `.desktop` entry |
| macOS | `dist/PRISM.app` (and a bare `dist/PRISM`) | double-click the `.app` |
| Windows | `dist/PRISM.exe` | double-click |

## Build

```bash
python3 -m pip install -r desktop/requirements-build.txt
python3 desktop/build.py
```

Flags: `--onedir` (folder layout — slower to copy, faster to start),
`--no-icon`, `--keep-build` (keeps intermediates for debugging).

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

`Exec=`/`Icon=` are bare names, so point them at wherever you put the binary:

```bash
install -Dm755 dist/PRISM               ~/.local/bin/PRISM
install -Dm644 dist/PRISM.png           ~/.local/share/icons/hicolor/256x256/apps/PRISM.png
install -Dm644 dist/PRISM.desktop       ~/.local/share/applications/PRISM.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

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
