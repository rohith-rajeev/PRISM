"""The one place the application's version is written down.

`app.py` shows it, `updater.py` compares against it, and CI rewrites it when a
merge reaches main, so a build can never claim a version nobody tagged.
"""
import re

__version__ = "7.2.1"

# The public home of the project. Releases are published here, and the updater
# reads them from the unauthenticated API - nothing here is private.
REPO = "rohith-rajeev/PRISM"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"


def tag() -> str:
    """The git tag that corresponds to this version."""
    return "v" + __version__


def parse(text) -> tuple:
    """"v2.9" -> (2, 9, 0); "v6.4.1" -> (6, 4, 1). Unreadable sorts lowest, never raises.

    The third number is a fix-up release of the same version (6.4.1 is the first
    correction to 6.4). It sorts between 6.4 and 6.5.
    """
    numbers = [int(n) for n in re.findall(r"\d+", (text or "").strip())[:3]]
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def next_version(*candidates) -> str:
    """The version after the highest of `candidates`.

    A step is one tenth, and the tenth step carries into the whole number, so
    the sequence runs 2.8, 2.9, 3.0, 3.1. There is deliberately no 2.10: the
    scheme is a counter with one decimal place, not semantic versioning, and
    the carry is what keeps it sortable both as text and as numbers. A fix-up
    number (6.4.1) is dropped by the next step: 6.4.1 is followed by 6.5.

    Candidates may be tags, bare versions, empty or nonsense - whichever reads
    highest wins, so a tag made by hand is never handed out twice and a
    hand-edited version.py is still honoured.
    """
    major, minor, _fix = max((parse(c) for c in candidates), default=(0, 0, 0))
    minor += 1
    if minor > 9:
        major, minor = major + 1, 0
    return f"{major}.{minor}"
