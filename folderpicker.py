"""Choose a folder with the platform's own dialog.

Tk's built-in directory chooser is the old Motif-style one on Linux: a bare
two-pane list with no places, no search and no typing of a path, which makes
moving between project folders slow. Linux desktops already ship a modern
chooser (GTK's via zenity or yad, Qt's via kdialog), so that is what is used
there, and Tk's own dialog is the fallback when none is installed. macOS and
Windows already get their native dialog from Tk and are left alone.

Tk-free, so it is testable: `pick_folder` returns a path, "" for cancel, or None
when no native chooser exists and the caller should use its own.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Variables a PyInstaller build points at its own bundled libraries. Handed to
# zenity they make it load the wrong GTK, so the originals are restored.
_BUNDLE_LIB_VARS = ("LD_LIBRARY_PATH", "LD_PRELOAD")


def _clean_env():
    env = dict(os.environ)
    for var in _BUNDLE_LIB_VARS:
        original = env.pop(var + "_ORIG", None)
        if original is not None:
            env[var] = original
        elif getattr(sys, "frozen", False):
            env.pop(var, None)
    return env


def _start_dir(initial):
    try:
        path = Path(initial).expanduser() if initial else Path.home()
        while not path.is_dir() and path != path.parent:
            path = path.parent
        return str(path if path.is_dir() else Path.home())
    except OSError:
        return str(Path.home())


def chooser_command(title, initial, which=shutil.which):
    """The command line of the first native chooser found, or None."""
    start = _start_dir(initial)
    if not start.endswith("/"):
        start += "/"          # zenity treats a trailing slash as "open this folder"
    if which("zenity"):
        return ["zenity", "--file-selection", "--directory", "--title", title,
                "--filename", start]
    if which("kdialog"):
        return ["kdialog", "--title", title, "--getexistingdirectory", start]
    if which("yad"):
        return ["yad", "--file", "--directory", "--title", title, "--filename", start]
    return None


def native_available(platform=sys.platform, which=shutil.which):
    return platform.startswith("linux") and chooser_command("", "", which) is not None


def pick_folder(title="Select a folder", initial=None, platform=sys.platform,
                which=shutil.which, run=subprocess.run):
    """Ask for a folder. Path on success, "" if cancelled, None if unavailable.

    Blocks until the dialog closes - call it from a worker thread when the UI
    must keep painting.
    """
    if not platform.startswith("linux"):
        return None
    cmd = chooser_command(title, initial, which)
    if cmd is None:
        return None
    try:
        proc = run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                   universal_newlines=True, env=_clean_env())
    except OSError:
        return None
    if proc.returncode == 0:
        return (proc.stdout or "").strip().rstrip("\n") or ""
    if proc.returncode in (1, 252):   # the person pressed Cancel or closed it
        return ""
    return None                       # it failed to start (no display, bad GTK...)
