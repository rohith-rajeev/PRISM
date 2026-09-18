"""Check for, download and install a newer PRISM from the project's releases.

Deliberately free of Tkinter so the whole flow is unit-testable, in the same
spirit as `jobs.py`. The UI layer supplies progress and cancellation callbacks
and does the asking; nothing here touches the screen or decides on its own
that an update should happen.

Why releases rather than workflow artifacts: artifacts need a GitHub token even
on a public repository and expire on a retention clock, so every user would
need a credential and a build older than a week would simply vanish. Release
assets are public, permanent and carry the version in their name.
"""

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from version import REPO, RELEASES_URL, __version__

API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
SUMS_NAME = "SHA256SUMS"
CHUNK = 64 * 1024

# Generous, because a release asset is tens of megabytes on a hotel connection.
CONNECT_TIMEOUT = 20


class UpdateError(RuntimeError):
    """Anything that stops an update, phrased for the person reading it."""


class Release:
    """The subset of a GitHub release this tool cares about."""

    def __init__(self, version, tag, notes, asset_name, asset_url, asset_size,
                 sums_url=None, page_url=RELEASES_URL):
        self.version = version
        self.tag = tag
        self.notes = notes or ""
        self.asset_name = asset_name
        self.asset_url = asset_url
        self.asset_size = asset_size
        self.sums_url = sums_url
        self.page_url = page_url

    @property
    def has_asset_for_this_platform(self):
        return bool(self.asset_url)


# --------------------------------------------------------------------------
# versions
# --------------------------------------------------------------------------

def parse_version(text):
    """"v2.10.1" -> (2, 10, 1). Trailing pre-release junk stops the parse.

    Numeric comparison, so 2.10 correctly beats 2.9 - which a string compare
    gets backwards, and which is exactly the version pair a project reaches
    right after its tenth release.
    """
    nums = []
    for part in re.split(r"[.\-+_]", (text or "").strip().lstrip("vV")):
        if part.isdigit():
            nums.append(int(part))
        else:
            break
    return tuple(nums) or (0,)


def is_newer(candidate, current=__version__):
    """True only when `candidate` is strictly ahead of `current`."""
    return parse_version(candidate) > parse_version(current)


# --------------------------------------------------------------------------
# where this copy lives
# --------------------------------------------------------------------------

def platform_key():
    name = platform.system()
    return {"Darwin": "macos", "Windows": "windows"}.get(name, "linux")


def install_root():
    """The file or bundle on disk that an update replaces.

    On macOS the executable sits inside PRISM.app/Contents/MacOS, and replacing
    that alone would leave the bundle's Info.plist and resources stale, so the
    whole .app is the unit. Elsewhere it is the single executable.
    """
    exe = Path(sys.executable).resolve()
    if platform_key() == "macos":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
    return exe


def can_self_update():
    """(bool, reason). Only a frozen build can meaningfully replace itself."""
    if not getattr(sys, "frozen", False):
        return False, ("This is a source checkout, not a packaged build - "
                       "update it with git pull.")
    root = install_root()
    parent = root.parent
    if not os.access(parent, os.W_OK):
        return False, (f"No permission to write to {parent}. Reinstall from "
                       "the releases page, or move PRISM somewhere you own.")
    return True, ""


# --------------------------------------------------------------------------
# talking to GitHub
# --------------------------------------------------------------------------

def _open(url, timeout=CONNECT_TIMEOUT, accept="application/vnd.github+json"):
    # GitHub rejects requests without a User-Agent outright.
    req = urllib.request.Request(url, headers={
        "User-Agent": f"PRISM/{__version__}",
        "Accept": accept,
    })
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No published release to update to yet.") from exc
        if exc.code in (403, 429):
            raise UpdateError("GitHub is rate-limiting this machine. "
                              "Try again in a few minutes.") from exc
        raise UpdateError(f"GitHub returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError):
            raise UpdateError(f"Could not verify GitHub's certificate: {reason}") from exc
        raise UpdateError(f"Could not reach GitHub: {reason}") from exc
    except OSError as exc:
        raise UpdateError(f"Could not reach GitHub: {exc}") from exc


def _pick_asset(assets, key=None):
    """The release asset built for this platform.

    Matches on the platform token the build workflow puts in every asset name.
    The architecture is not required to match: there is one build per OS, and
    demanding an exact arch would refuse a perfectly good update.
    """
    key = key or platform_key()
    for asset in assets:
        name = (asset.get("name") or "").lower()
        if name == SUMS_NAME.lower():
            continue
        if key in name:
            return asset
    return None


def check(timeout=CONNECT_TIMEOUT, current=__version__, key=None):
    """The latest release if it is newer than `current`, else None."""
    with _open(API_LATEST, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    return release_from_payload(payload, current=current, key=key)


def release_from_payload(payload, current=__version__, key=None):
    """Split out from `check` so the parsing is testable without a network."""
    tag = payload.get("tag_name") or payload.get("name") or ""
    version = tag.lstrip("vV")
    if not version or not is_newer(version, current):
        return None
    assets = payload.get("assets") or []
    asset = _pick_asset(assets, key=key)
    sums = next((a for a in assets
                 if (a.get("name") or "").upper() == SUMS_NAME), None)
    return Release(
        version=version,
        tag=tag,
        notes=payload.get("body") or "",
        asset_name=(asset or {}).get("name"),
        asset_url=(asset or {}).get("browser_download_url"),
        asset_size=(asset or {}).get("size") or 0,
        sums_url=(sums or {}).get("browser_download_url"),
        page_url=payload.get("html_url") or RELEASES_URL,
    )


# --------------------------------------------------------------------------
# fetching and checking
# --------------------------------------------------------------------------

class Cancelled(UpdateError):
    """The person pressed Cancel; not a failure worth an error dialog."""


def download(release, dest_dir, progress=None, cancel=None,
             timeout=CONNECT_TIMEOUT):
    """Stream the asset to `dest_dir`, reporting (done, total) as it goes."""
    if not release.has_asset_for_this_platform:
        raise UpdateError(
            f"Release {release.tag} has no build for {platform_key()}.")
    dest = Path(dest_dir) / release.asset_name
    total = release.asset_size
    done = 0
    with _open(release.asset_url, timeout=timeout,
               accept="application/octet-stream") as resp, open(dest, "wb") as out:
        total = total or int(resp.headers.get("Content-Length") or 0)
        while True:
            if cancel is not None and cancel():
                raise Cancelled("Download cancelled.")
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    if total and done != total:
        raise UpdateError(
            f"Download was cut short: got {done} of {total} bytes.")
    return dest


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_sums(text):
    """A sha256sum file into {filename: digest}."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            out[parts[-1].lstrip("*")] = parts[0].lower()
    return out


def verify(path, release, timeout=CONNECT_TIMEOUT, sums_text=None):
    """Check the download against the release's SHA256SUMS.

    Returns True when verified, False when the release published no checksum
    for this asset. Raises when a checksum exists and does not match, because
    that is the one case where installing would be actively unsafe.
    """
    if sums_text is None:
        if not release.sums_url:
            return False
        with _open(release.sums_url, timeout=timeout,
                   accept="text/plain") as resp:
            sums_text = resp.read().decode("utf-8", "replace")
    expected = parse_sums(sums_text).get(release.asset_name)
    if not expected:
        return False
    actual = sha256(path)
    if actual != expected:
        raise UpdateError(
            "The downloaded file does not match the checksum GitHub "
            "published for it. Nothing has been installed.")
    return True


# --------------------------------------------------------------------------
# unpacking
# --------------------------------------------------------------------------

def _guard(dest, member_name):
    """Refuse any archive member that would land outside `dest`."""
    target = (dest / member_name).resolve()
    if not str(target).startswith(str(dest.resolve())):
        raise UpdateError(f"Refusing an archive that writes outside its "
                          f"folder: {member_name}")
    return target


def extract(archive, dest):
    """Unpack `archive` into `dest`, preserving the executable bit.

    A zip does not carry permissions the way tar does, and a bundle whose
    inner binary has lost +x will not launch - so on macOS this shells out to
    ditto, which is always present and round-trips the bundle exactly.
    """
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive, "r:gz") as tf:
            for member in tf.getmembers():
                _guard(dest, member.name)
            try:
                tf.extractall(dest, filter="data")     # 3.12+
            except TypeError:
                tf.extractall(dest)
    elif platform_key() == "macos" and shutil.which("ditto"):
        proc = subprocess.run(["ditto", "-x", "-k", str(archive), str(dest)],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=300)
        if proc.returncode != 0:
            raise UpdateError(f"Could not unpack the download: "
                              f"{proc.stderr.strip() or proc.stdout.strip()}")
    else:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                _guard(dest, info.filename)
                zf.extract(info, dest)
                mode = info.external_attr >> 16
                if mode:
                    os.chmod(dest / info.filename, mode)
    return dest


def payload_in(staged, root=None):
    """Find, inside an unpacked archive, the thing that replaces `root`."""
    root = root or install_root()
    staged = Path(staged)
    direct = staged / root.name
    if direct.exists():
        return direct
    matches = [p for p in staged.rglob(root.name) if p.exists()]
    if not matches:
        raise UpdateError(
            f"The download did not contain {root.name}; not installing.")
    return matches[0]


# --------------------------------------------------------------------------
# swapping it in
# --------------------------------------------------------------------------

def backup_path(root):
    return root.with_name(root.name + ".old")


def install(payload, root=None):
    """Move `payload` into place, keeping the previous copy alongside it.

    The old copy is renamed rather than deleted: a running executable cannot be
    overwritten on Windows but can be renamed out of the way, and on every
    platform keeping it means a failed swap can be undone. `cleanup_previous`
    clears it on the next launch, once nothing is holding it open.
    """
    root = (root or install_root())
    payload = Path(payload)
    backup = backup_path(root)
    if backup.exists():
        _remove(backup)
    os.rename(root, backup)                 # same directory, so never cross-device
    try:
        shutil.move(str(payload), str(root))
    except Exception as exc:                # noqa: BLE001
        os.rename(backup, root)             # put the working copy back
        raise UpdateError(f"Could not install the update: {exc}") from exc
    if root.is_file():
        os.chmod(root, os.stat(root).st_mode | 0o111)
    return root


def _remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            path.unlink()
        except OSError:
            pass


def cleanup_previous(root=None):
    """Delete the copy the last update displaced. Best effort, by design.

    On Windows the process that was replaced may still be exiting when its
    successor starts, and the file stays locked until it does; the attempt
    simply succeeds on a later launch instead.
    """
    try:
        backup = backup_path(root or install_root())
        if backup.exists():
            _remove(backup)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def relaunch(root=None):
    """Start the freshly installed copy and leave it running."""
    root = root or install_root()
    if platform_key() == "macos" and root.suffix == ".app":
        cmd = ["/usr/bin/open", "-n", str(root)]
    else:
        cmd = [str(root)]
    kwargs = {}
    if os.name == "nt":
        # Detached, so the new window does not die with this one.
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def apply_update(release, progress=None, cancel=None, root=None):
    """Download, verify, unpack and install. Returns (root, verified)."""
    root = root or install_root()
    with tempfile.TemporaryDirectory(prefix="prism-update-") as tmp:
        archive = download(release, tmp, progress=progress, cancel=cancel)
        verified = verify(archive, release)
        if cancel is not None and cancel():
            raise Cancelled("Cancelled before installing.")
        staged = extract(archive, Path(tmp) / "unpacked")
        payload = payload_in(staged, root)
        # Out of the temporary directory before it is swept away.
        holding = Path(tmp) / "ready"
        holding.mkdir()
        final = holding / payload.name
        shutil.move(str(payload), str(final))
        install(final, root)
    return root, verified
