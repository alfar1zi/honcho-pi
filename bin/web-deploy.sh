#!/bin/sh
# Build the web UI and deploy it where the installed (venv) server looks.
# The pip package ships no dist (gitignored), so a UI change needs this step;
# without it the venv server keeps serving stale assets.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${OMP_MEMORY_WEB_DIR:-$HOME/.omp/memory/web}"

npm --prefix "$ROOT/web" run build
mkdir -p "$DEST"
rm -rf "$DEST/dist"
cp -r "$ROOT/web/dist" "$DEST/dist"
echo "[web-deploy] dist -> $DEST/dist"