#!/bin/sh
# Build the web UI and deploy it where the installed (venv) server looks.
# The pip package ships no dist (gitignored), so a UI change needs this step;
# without it the venv server keeps serving stale assets.
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${OMP_MEMORY_WEB_DIR:-$HOME/.omp/memory/web}"

npm --prefix "$ROOT/web" run build
mkdir -p "$DEST"
# Same directory (OMP_MEMORY_WEB_DIR pointing at the checkout): build output
# is already in place; rm+cp would delete the source before copying it.
if [ "$(cd "$ROOT/web" && pwd)" != "$(cd "$DEST" && pwd)" ]; then
  cp -r "$ROOT/web/dist" "$DEST/dist.new"
  rm -rf "$DEST/dist"
  mv "$DEST/dist.new" "$DEST/dist"
fi
# Seed the graph cache: without it the first GET rebuilds from the store
# (O(n^2) similarity, ~13s at 600 records) and the page sits on "loading".
if [ ! -f "$DEST/data/graph.json" ] && [ -f "$ROOT/web/data/graph.json" ]; then
  mkdir -p "$DEST/data"
  cp "$ROOT/web/data/graph.json" "$DEST/data/graph.json"
fi
echo "[web-deploy] dist -> $DEST/dist"