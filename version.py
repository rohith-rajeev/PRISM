"""The one place the application's version is written down.

`app.py` shows it, `updater.py` compares against it, and CI checks it matches
the tag being released, so a build can never claim a version nobody tagged.
"""

__version__ = "2.0"

# The public home of the project. Releases are published here, and the updater
# reads them from the unauthenticated API - nothing here is private.
REPO = "rohith-rajeev/PRISM"
RELEASES_URL = f"https://github.com/{REPO}/releases/latest"


def tag() -> str:
    """The git tag that corresponds to this version."""
    return "v" + __version__
