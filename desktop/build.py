#!/usr/bin/env python3
"""Build a standalone PRISM desktop executable for the current platform.

    python3 desktop/build.py                 # build for whatever OS you're on
    python3 desktop/build.py --onedir        # folder layout instead of one file
    python3 desktop/build.py --no-icon       # skip icon generation

PyInstaller cannot cross-compile: a macOS build must run on macOS, a Windows
build on Windows, a Linux build on Linux. To get all three from one push, use
the GitHub Actions workflow in .github/workflows/build-desktop.yml, which runs
this same script on each runner.

Output lands in dist/:
    Linux    dist/PRISM              (executable)
    Windows  dist/PRISM.exe
    macOS    dist/PRISM.app          (bundle, plus a bare dist/PRISM)
"""
import argparse
import importlib
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
APP_NAME = "PRISM"
MIN_PY = (3, 8)


def fail(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def preflight():
    if sys.version_info < MIN_PY:
        fail(f"Python {MIN_PY[0]}.{MIN_PY[1]}+ required, running {platform.python_version()}")
    try:
        importlib.import_module("tkinter")   # probe: must be a real, working tkinter
    except ImportError:
        fail("this Python has no tkinter — install it first "
             "(Debian/Ubuntu: sudo apt install python3-tk; macOS: use python.org "
             "or `brew install python-tk`; Windows: re-run the installer with tcl/tk)")
    try:
        importlib.import_module("PyInstaller")
    except ImportError:
        fail(f"PyInstaller is missing — install it with:\n"
             f"    {sys.executable} -m pip install -r {Path('desktop') / 'requirements-build.txt'}")
    for rel in ("app.py", "orchestrator.py", "jobs.py", "updater.py",
                "version.py", Path("agents") / "pr-reviewer.md",
                Path("docs") / "MANUAL.md"):
        if not (ROOT / rel).is_file():
            fail(f"missing {rel} — run this from the PRISM checkout")


def make_icon(enabled):
    """Return the platform's icon path, or None to let PyInstaller default."""
    if not enabled:
        return None
    sys.path.insert(0, str(ROOT / "desktop"))
    try:
        from make_icon import write_icons
        write_icons(BUILD / "icons")
    except Exception as e:  # noqa: BLE001 - an icon is never worth failing a build
        print(f"warning: could not generate icons ({e}); using the default")
        return None
    wanted = {"win32": "icon.ico", "darwin": "icon.icns"}.get(sys.platform, "icon.png")
    path = BUILD / "icons" / wanted
    if not path.is_file():  # e.g. iconutil unavailable on a mac runner
        return None
    return path


def write_linux_desktop_entry(dist, icon):
    """Drop a .desktop launcher beside the binary.

    On Linux the icon lives in the desktop entry rather than the executable, so
    without this the build is just a bare ELF file with no menu entry or icon.
    Exec/Icon are rewritten to absolute paths on install (see desktop/README.md).
    """
    if icon and icon.suffix == ".png":
        shutil.copyfile(icon, dist / "PRISM.png")
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=PRISM\n"
        "GenericName=Pull Request Reviewer\n"
        "Comment=Review and safely merge AWS CodeCommit pull requests\n"
        "Exec=PRISM\n"
        "Icon=prism\n"
        "Terminal=false\n"
        "Categories=Development;RevisionControl;\n"
        "Keywords=code review;pull request;codecommit;aws;git;merge;\n"
        "StartupNotify=true\n"
        # Must match the WM_CLASS App sets via Tk's className, or the shell
        # shows a second, generic icon for the running window.
        "StartupWMClass=Prism\n"
    )
    (dist / "PRISM.desktop").write_text(entry, encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Build the PRISM desktop executable.")
    ap.add_argument("--onedir", action="store_true",
                    help="emit a folder instead of a single file (starts faster)")
    ap.add_argument("--no-icon", action="store_true", help="skip icon generation")
    ap.add_argument("--keep-build", action="store_true",
                    help="keep intermediate build/ output for debugging")
    args = ap.parse_args()

    preflight()
    icon = make_icon(not args.no_icon)

    dist = ROOT / "dist"
    for stale in (dist / APP_NAME, dist / f"{APP_NAME}.exe", dist / f"{APP_NAME}.app"):
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        elif stale.exists():
            stale.unlink()

    # The whole agents folder rides along as data — one file per pipeline step —
    # and orchestrator.py finds it via sys._MEIPASS. PyInstaller's --add-data separator is ':' on POSIX
    # and ';' on Windows.
    agent_src = ROOT / "agents"
    # The manual is the in-app help, so it ships too and is found the same way.
    manual_src = ROOT / "docs" / "MANUAL.md"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir" if args.onedir else "--onefile",
        "--windowed",                       # no console window; makes a .app on macOS
        "--name", APP_NAME,
        "--distpath", str(dist),
        "--workpath", str(BUILD / "pyinstaller"),
        "--specpath", str(BUILD),
        "--paths", str(ROOT),
        "--add-data", f"{agent_src}{os.pathsep}agents",
        "--add-data", f"{manual_src}{os.pathsep}docs",
        "--hidden-import", "orchestrator",
        "--hidden-import", "jobs",
        "--hidden-import", "updater",
        "--hidden-import", "version",
        # Tkinter is the whole UI; everything else the stdlib drags in is dead
        # weight in a GUI binary.
        "--exclude-module", "test",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--exclude-module", "distutils",
    ]
    if icon and sys.platform in ("win32", "darwin"):
        # PyInstaller only embeds icons on Windows and macOS; on Linux the icon
        # is carried by the .desktop entry written below instead.
        cmd += ["--icon", str(icon)]
    if sys.platform == "darwin":
        cmd += ["--osx-bundle-identifier", "dev.prism.desktop"]
    cmd.append(str(ROOT / "app.py"))

    print("$ " + " ".join(str(c) for c in cmd))
    rc = subprocess.call(cmd, cwd=str(ROOT))
    if rc != 0:
        fail(f"PyInstaller exited {rc}")

    if not args.keep_build:
        shutil.rmtree(BUILD / "pyinstaller", ignore_errors=True)

    if sys.platform.startswith("linux"):
        write_linux_desktop_entry(dist, icon)

    produced = [p for p in (dist / APP_NAME, dist / f"{APP_NAME}.exe",
                            dist / f"{APP_NAME}.app") if p.exists()]
    if not produced:
        fail(f"build reported success but nothing appeared in {dist}")
    print("\nBuilt:")
    for p in produced:
        if p.is_dir():
            size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
        else:
            size = p.stat().st_size
        print(f"  {p}  ({size / 1048576:.1f} MiB)")
    print("\nPRISM still needs `opencode`, `aws` and `git` on PATH at run time — "
          "the bundle ships the UI, not those tools.")


if __name__ == "__main__":
    main()
