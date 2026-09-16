#!/bin/sh
# Launch PRISM from a source checkout (stdlib only, no pip install needed).
# For a packaged build see desktop/README.md.
set -e
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for PY in python3 python; do
    if command -v "$PY" >/dev/null 2>&1; then
        exec "$PY" "$DIR/app.py" "$@"
    fi
done
echo "PRISM needs Python 3.8+ with Tkinter on PATH (tried: python3, python)." >&2
exit 1
