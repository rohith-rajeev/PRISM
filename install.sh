#!/bin/sh
# PRISM installer for Linux (x86_64) and macOS (Apple silicon).
#
#   curl -fsSL https://raw.githubusercontent.com/rohith-rajeev/PRISM/main/install.sh | sh
#
# Downloads the latest release, checks it against the SHA256SUMS published
# beside it, installs it under your home directory (no sudo) and puts a `prism`
# command on your PATH. Needs only curl or wget plus tar (Linux) / ditto (macOS).
#
# Options (pass after `sh -s --` when piping):
#   --version vX.Y   install that release instead of the latest
#   --uninstall      remove what this script installed (keeps ~/.prism settings)
#   -h, --help       show this help
# Environment: PRISM_VERSION (same as --version), PRISM_BIN_DIR (default
# ~/.local/bin), PRISM_PREFIX (Linux only; default ~/.local/opt/prism).
set -eu

REPO="rohith-rajeev/PRISM"
VERSION="${PRISM_VERSION:-latest}"
BIN_DIR="${PRISM_BIN_DIR:-$HOME/.local/bin}"
UNINSTALL=0

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --version)   [ $# -ge 2 ] || die "--version needs a value, e.g. --version v4.5"
                     VERSION="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help)   sed -n '2,/^set -eu/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)           die "unknown option: $1 (try --help)" ;;
    esac
done

OS=$(uname -s)
ARCH=$(uname -m)
case "$OS/$ARCH" in
    Linux/x86_64)
        ASSET="PRISM-linux-x86_64.tar.gz"
        PREFIX="${PRISM_PREFIX:-$HOME/.local/opt/prism}" ;;
    Darwin/arm64)
        ASSET="PRISM-macos-arm64.zip"
        APP_DIR="$HOME/Applications" ;;
    *)  die "no prebuilt PRISM for $OS/$ARCH. Run it from source instead: git clone https://github.com/$REPO && cd PRISM && ./run.sh" ;;
esac

# ---------------------------------------------------------------- uninstall
if [ "$UNINSTALL" = 1 ]; then
    rm -f "$BIN_DIR/prism"
    if [ "$OS" = Linux ]; then
        rm -rf "$PREFIX"
        rm -f "$HOME/.local/share/applications/PRISM.desktop" "$HOME/.local/share/man/man1/prism.1"
    else
        rm -rf "$APP_DIR/PRISM.app"
        rm -f "$HOME/.local/share/man/man1/prism.1"
    fi
    say "PRISM removed. Your settings in ~/.prism were left alone (delete that folder to remove them too)."
    exit 0
fi

# ----------------------------------------------------------------- download
if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL --retry 3 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q -O "$2" "$1"; }
else
    die "need curl or wget to download PRISM"
fi

if [ "$VERSION" = latest ]; then
    BASE="https://github.com/$REPO/releases/latest/download"
else
    case "$VERSION" in v*) ;; *) VERSION="v$VERSION" ;; esac
    BASE="https://github.com/$REPO/releases/download/$VERSION"
fi

TMP=$(mktemp -d "${TMPDIR:-/tmp}/prism-install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM

say "Downloading $ASSET ($VERSION) ..."
fetch "$BASE/$ASSET" "$TMP/$ASSET"      || die "download failed — is '$VERSION' a real release? See https://github.com/$REPO/releases"
fetch "$BASE/SHA256SUMS" "$TMP/SHA256SUMS" || die "could not download SHA256SUMS"

if command -v sha256sum >/dev/null 2>&1; then
    sha() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
    sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
    die "need sha256sum or shasum to verify the download"
fi
WANT=$(grep " $ASSET\$" "$TMP/SHA256SUMS" | cut -d' ' -f1)
[ -n "$WANT" ] || die "$ASSET is not listed in SHA256SUMS"
[ "$(sha "$TMP/$ASSET")" = "$WANT" ] || die "checksum mismatch — the download is corrupt or has been tampered with; nothing was installed"
say "Checksum OK."

mkdir -p "$BIN_DIR"

# ------------------------------------------------------------------- Linux
if [ "$OS" = Linux ]; then
    command -v tar >/dev/null 2>&1 || die "need tar"
    mkdir -p "$TMP/x" "$PREFIX"
    tar -xzf "$TMP/$ASSET" -C "$TMP/x"
    [ -f "$TMP/x/PRISM" ] || die "the archive has no PRISM executable"
    # The archive does not carry the executable bit, which is why a plain
    # extract can't be run.
    chmod 755 "$TMP/x/PRISM"
    mv -f "$TMP/x/PRISM" "$PREFIX/PRISM"
    [ -f "$TMP/x/PRISM.png" ] && cp -f "$TMP/x/PRISM.png" "$PREFIX/PRISM.png"
    ln -sf "$PREFIX/PRISM" "$BIN_DIR/prism"

    # Launcher entry with absolute paths: a bare `Exec=PRISM` only works if
    # the desktop's PATH happens to include the install folder.
    APPS="$HOME/.local/share/applications"
    mkdir -p "$APPS"
    if [ -f "$TMP/x/PRISM.desktop" ]; then
        sed -e "s|^Exec=.*|Exec=$PREFIX/PRISM|" \
            -e "s|^Icon=.*|Icon=$PREFIX/PRISM.png|" \
            "$TMP/x/PRISM.desktop" > "$APPS/PRISM.desktop"
        command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true
    fi
    RUN="$PREFIX/PRISM"
    LAUNCH="Open PRISM from your applications menu, or run:  prism"

# ------------------------------------------------------------------- macOS
else
    command -v ditto >/dev/null 2>&1 || die "need ditto (it ships with macOS)"
    mkdir -p "$TMP/x" "$APP_DIR"
    # ditto, not unzip: it keeps the bundle's symlinks and permission bits.
    ditto -x -k "$TMP/$ASSET" "$TMP/x"
    [ -d "$TMP/x/PRISM.app" ] || die "the archive has no PRISM.app"
    rm -rf "$APP_DIR/PRISM.app"
    mv "$TMP/x/PRISM.app" "$APP_DIR/PRISM.app"
    # The app is not signed with an Apple developer certificate; clearing the
    # quarantine flag stops Gatekeeper refusing to open it.
    xattr -dr com.apple.quarantine "$APP_DIR/PRISM.app" 2>/dev/null || true
    ln -sf "$APP_DIR/PRISM.app/Contents/MacOS/PRISM" "$BIN_DIR/prism"
    RUN="$APP_DIR/PRISM.app/Contents/MacOS/PRISM"
    LAUNCH="Open PRISM from ~/Applications (or Spotlight), or run:  prism"
fi

"$RUN" man >/dev/null 2>&1 || true      # man page; best effort
INSTALLED=$("$RUN" version 2>/dev/null || true)
[ -n "$INSTALLED" ] || warn "installed, but '$RUN version' did not run — please report this"

say ""
say "${INSTALLED:-PRISM} installed.  $LAUNCH"

# ------------------------------------------------------------------- advice
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) say ""
       say "$BIN_DIR is not on your PATH, so the 'prism' command won't be found yet. Add it:"
       if [ "$OS" = Darwin ]; then
           say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.zshrc && exec zsh"
       else
           say "  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.profile   # then log in again (or: export PATH=\"$BIN_DIR:\$PATH\")"
       fi ;;
esac

MISSING=""
for tool in git aws opencode; do
    if ! command -v "$tool" >/dev/null 2>&1 && [ ! -x "$HOME/.opencode/bin/$tool" ]; then
        MISSING="$MISSING $tool"
    fi
done
if [ -n "$MISSING" ]; then
    say ""
    say "PRISM also needs these on your PATH, and they were not found:$MISSING"
    case "$MISSING" in *git*) say "  git       https://git-scm.com/downloads" ;; esac
    case "$MISSING" in *aws*) say "  aws       https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html  (then: aws configure)" ;; esac
    case "$MISSING" in *opencode*) say "  opencode  https://opencode.ai  (the reviewer engine)" ;; esac
fi
say ""
say "Next:  cd <a clone of your CodeCommit repo> && prism run <PR id>     (prism --help for everything)"
