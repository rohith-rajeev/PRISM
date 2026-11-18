"""Windows process-launching fixes. Does nothing on other systems.

1. **No flashing console windows.** The packaged PRISM is a windowed program with
   no console of its own, so every `git`, `aws` or review-engine process it starts
   makes Windows open (and close) a console window - dozens of them while it
   works. `install()` makes every child start without one.

2. **Prompts survive `.cmd` launchers.** The review engine installed with npm is
   `opencode.cmd`, a batch file. Passing it a multi-line prompt as an argument
   hands the prompt to `cmd.exe`, which stops reading at the first line break and
   caps the whole line at 8191 characters, so the reviewer got a fragment and
   answered nothing. `direct_argv()` finds what the launcher would have run (node
   plus a script, or an .exe) and runs that directly, which has neither problem.

Stdlib only.
"""
import os
import re
import shutil
import subprocess

_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008


def _has_console():
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:  # noqa: BLE001
        return False


def install(force=False):
    """Start every child process without a console window.

    Skipped when PRISM itself has a console (run from a terminal): there the
    children share it and nothing flashes. Safe to call more than once.
    """
    if os.name != "nt" or getattr(subprocess.Popen, "_prism_quiet", False):
        return False
    if not force and _has_console():
        return False
    base = subprocess.Popen

    class _QuietPopen(base):
        _prism_quiet = True

        def __init__(self, *args, **kwargs):
            flags = kwargs.get("creationflags") or 0
            if not flags & _DETACHED_PROCESS:     # a detached process has no console anyway
                kwargs["creationflags"] = flags | _CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _QuietPopen
    return True


_SHIM_TARGET = re.compile(r'"%dp0%\\([^"]+)"\s+%\*', re.IGNORECASE)
_SHIM_LOCAL_EXE = re.compile(r'"%~dp0\\([^"]+)"\s+%\*', re.IGNORECASE)


def shim_target(launcher):
    """What an npm/bun `.cmd` launcher runs: (interpreter or None, script/exe path),
    or None when it cannot be worked out."""
    try:
        with open(launcher, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    here = os.path.dirname(os.path.abspath(launcher))
    for pattern in (_SHIM_TARGET, _SHIM_LOCAL_EXE):
        found = pattern.findall(text)
        if not found:
            continue
        target = os.path.normpath(os.path.join(here, found[-1].replace("\\", os.sep)))
        if not os.path.isfile(target):
            continue
        if target.lower().endswith(".exe"):
            return None, target
        node = os.path.join(here, "node.exe")
        if not os.path.isfile(node):
            node = shutil.which("node")
        if node:
            return node, target
    return None


def direct_argv(argv, nt=None):
    """`argv` with a `.cmd`/`.bat` launcher swapped for what it runs, so arguments
    reach the program untouched. Unchanged for anything else or when unsure."""
    if not argv:
        return list(argv)
    if (os.name == "nt") if nt is None else nt:
        exe = str(argv[0])
        if exe.lower().endswith((".cmd", ".bat")):
            hit = shim_target(exe)
            if hit:
                interpreter, target = hit
                return ([interpreter, target] if interpreter else [target]) + list(argv[1:])
    return list(argv)


def is_batch_launcher(exe, nt=None):
    return ((os.name == "nt") if nt is None else nt) and str(exe).lower().endswith((".cmd", ".bat"))


def windows():
    return os.name == "nt"
