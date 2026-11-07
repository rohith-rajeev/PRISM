"""Keeping a packaged build's own files alive.

A PyInstaller one-file build unpacks itself into a temporary folder (/tmp/_MEIxxxx)
and deletes it when it exits. Two things have taken that folder away from a PRISM
that was still running, and each shows up as "Bundled agents missing: /tmp/_MEI...":

* it was started by a process that has since exited (the restart after an update,
  `prism run --detach`): a child that inherits the parent's environment is told to
  reuse the parent's folder, which goes when the parent does;
* something cleared the temp folder while PRISM was open (a tmp cleaner, a person).

This module answers both. `restart_if_bundle_gone` is for the first: at start-up a
build that finds its files already missing restarts itself once, from a clean
environment, so it unpacks its own. `preserve` and `agents_dir` are for the second:
the reviewer agents are kept in a durable per-version copy under ~/.prism, and used
when the unpacked ones are gone.

Stdlib only and free of Tk, so it can run before anything else is imported.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Set on the restarted process, so a build that is still broken after one restart
# reports the problem instead of restarting forever.
RESTARTED = "PRISM_BUNDLE_RESTARTED"


def clean_env(environ=None, frozen=None):
    """The environment for a child that must not share this process's unpack folder.

    Only a packaged build has anything to undo; anything else is returned as it is.
    Newer bootloaders honour PYINSTALLER_RESET_ENVIRONMENT; older ones key off the
    variables, so those go too.
    """
    env = dict(os.environ if environ is None else environ)
    if getattr(sys, "frozen", False) if frozen is None else frozen:
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        for key in [k for k in env if k == "_MEIPASS2" or k.startswith("_PYI_")]:
            del env[key]
    return env


def bundle_gone(tool_dir, frozen=None):
    """True when this is a packaged build whose unpacked agents folder is missing."""
    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return False
    return not (Path(tool_dir) / "agents").is_dir()


def restart_if_bundle_gone(tool_dir=None, frozen=None, argv=None, environ=None,
                           execve=os.execve, call=subprocess.call, exit_=sys.exit,
                           windows=None):
    """Restart this program once, from a clean environment, if its files are gone.

    Returns False when nothing was needed (or a restart was already tried), so the
    caller carries on. On POSIX the process is replaced (same pid, same terminal); on
    Windows the program is run again and this one exits with its status.
    """
    tool_dir = tool_dir if tool_dir is not None else getattr(sys, "_MEIPASS", "")
    if not tool_dir or not bundle_gone(tool_dir, frozen):
        return False
    env = clean_env(environ, True)
    if env.get(RESTARTED):
        return False
    env[RESTARTED] = "1"
    args = [sys.executable] + list(sys.argv[1:] if argv is None else argv)
    try:
        if (os.name == "nt") if windows is None else windows:
            exit_(call(args, env=env))
        else:
            execve(sys.executable, args, env)
    except OSError:
        return False
    return True


# ---- a durable copy of the agents ---------------------------------------------

def durable_dir(version=None):
    """Where the agents are kept between runs: ~/.prism/bundle/<version>/agents."""
    if version is None:
        try:
            from version import __version__ as version
        except Exception:  # noqa: BLE001
            version = "current"
    return Path.home() / ".prism" / "bundle" / str(version) / "agents"


def preserve(tool_dir, frozen=None, dest=None):
    """Copy the unpacked agents somewhere that outlives the temp folder.

    Cheap (a handful of small files), skips what is already identical, and never
    raises: a build that cannot write there simply has no fallback.
    """
    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return False
    try:
        src = Path(tool_dir) / "agents"
        dest = Path(dest) if dest is not None else durable_dir()
        if not src.is_dir():
            return False
        dest.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.md"):
            target = dest / f.name
            if not target.exists() or target.read_bytes() != f.read_bytes():
                shutil.copyfile(str(f), str(target))
        return True
    except OSError:
        return False


def agents_dir(unpacked, durable=None):
    """The folder to read the agents from: the unpacked one, else the durable copy,
    else None."""
    unpacked = Path(unpacked)
    if unpacked.is_dir():
        return unpacked
    durable = Path(durable) if durable is not None else durable_dir()
    return durable if durable.is_dir() and any(durable.glob("*.md")) else None
