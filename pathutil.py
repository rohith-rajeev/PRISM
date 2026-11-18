"""Paths typed by people, made valid for the machine PRISM is running on.

A colleague on Windows types ``docs\\decomposition\\feature.md``; one on a Mac
types ``docs/decomposition/feature.md``. Both mean the same file, so whatever
was typed is turned into the separator style of the current OS before it is
used. Git itself always wants forward slashes (``git show <rev>:<path>``), so
`posix_rel` gives that form back for the few places that talk to git.
"""
import os
import re
from pathlib import Path

_QUOTES = "'\"‘’“”"


def _sep(text, sep=None):
    sep = sep or os.sep
    other = "/" if sep == "\\" else "\\"
    return text.replace(other, sep)


def _tidy(raw):
    return str(raw if raw is not None else "").strip().strip(_QUOTES).strip()


def native_path(raw, sep=None):
    """A folder or file path as typed -> the current OS's form, `~` expanded.

    Empty stays empty. Collapses doubled separators but never resolves symlinks
    or requires the path to exist.
    """
    text = _tidy(raw)
    if not text:
        return ""
    sep = sep or os.sep
    if sep == "/" and re.match(r"^[A-Za-z]:[\\/]", text):
        pass                        # a Windows drive path on a Mac: leave it to fail clearly
    text = _sep(text, sep)
    if text.startswith("~"):
        text = os.path.expanduser(text)
    lead = ""
    if sep == "\\" and text.startswith("\\\\"):
        lead, text = "\\\\", text[2:]            # keep a UNC share's leading pair
    text = re.sub(re.escape(sep) + r"{2,}", lambda _m: sep, text)
    return lead + text


def native_rel(raw, sep=None):
    """A repository-relative path as typed -> the current OS's separators,
    without a leading ``./``."""
    text = _tidy(raw)
    if not text:
        return ""
    sep = sep or os.sep
    text = _sep(text, sep)
    text = re.sub(re.escape(sep) + r"{2,}", lambda _m: sep, text)
    while text.startswith("." + sep):
        text = text[2:]
    return text


def posix_rel(raw):
    """Forward-slash form, which is what git expects whatever the OS."""
    return _tidy(raw).replace("\\", "/")


def native_any(raw, sep=None):
    """Either kind: absolute / `~` paths are paths, anything else is relative."""
    text = _tidy(raw)
    if not text:
        return ""
    if text.startswith("~") or Path(_sep(text, sep)).is_absolute() \
            or re.match(r"^[A-Za-z]:[\\/]", text) or text.startswith(("\\\\", "/")):
        return native_path(text, sep)
    return native_rel(text, sep)
