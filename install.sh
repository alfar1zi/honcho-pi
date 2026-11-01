#!/usr/bin/env bash
# install.sh - install honcho for the harness you are using.
#
# The MCP server is harness-neutral; only the config file location differs.
# Claude Code, Codex, OpenCode, Cursor and friends each read a different file,
# so this detects what is present rather than assuming one.
#
# Usage:
#   ./install.sh                 # detect and install
#   ./install.sh --harness omp   # force a target
#   ./install.sh --dir DIR       # copy files only, print the config snippet

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=""
HARNESS=""
PY_FILES=(memory_db.py honcho_mcp.py migrate_md.py reclaim.py budget.py guard.py test_honcho.py)

while [ $# -gt 0 ]; do
  case "$1" in
    --harness) HARNESS="$2"; shift 2 ;;
    --dir)     DEST="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

detect_harness() {
  if [ -n "$HARNESS" ]; then echo "$HARNESS"; return; fi
  if [ -n "${PI_CODING_AGENT_DIR:-}" ] || [ -n "${OMP_PROFILE:-}" ]; then echo omp; return; fi
  if [ -d "$HOME/.claude" ]; then echo claude; return; fi
  if [ -d "$HOME/.codex" ]; then echo codex; return; fi
  if [ -d "$HOME/.config/opencode" ]; then echo opencode; return; fi
  if [ -d "$HOME/.cursor" ]; then echo cursor; return; fi
  echo none
}

config_path() {
  case "$1" in
    omp)      echo "${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}/mcp.json" ;;
    claude)   echo "$HOME/.claude.json" ;;
    codex)    echo "$HOME/.codex/config.toml" ;;
    opencode) echo "$HOME/.config/opencode/opencode.json" ;;
    cursor)   echo "$HOME/.cursor/mcp.json" ;;
    *)        echo "" ;;
  esac
}

default_dir() {
  case "$1" in
    omp) echo "${PI_CODING_AGENT_DIR:-$HOME/.omp/agent}/honcho" ;;
    *)   echo "$HOME/.honcho" ;;
  esac
}

H="$(detect_harness)"
[ -n "$DEST" ] || DEST="$(default_dir "$H")"

echo "harness : $H"
echo "install : $DEST"

mkdir -p "$DEST"
for f in "${PY_FILES[@]}"; do
  cp "$SRC/$f" "$DEST/"
done
chmod +x "$DEST"/*.py
echo "copied  : ${#PY_FILES[@]} files"

CFG="$(config_path "$H")"

if [ "$H" = "none" ] || [ -z "$CFG" ]; then
  cat <<EOF

Could not detect a harness. Files are installed; register them manually.

Any MCP-capable client accepts a stdio server:

  command : python3
  args    : ["$DEST/honcho_mcp.py"]
  env     : HONCHO_HOME=$DEST

Generic JSON (Claude Desktop, Cursor, Zed, OpenCode):

  {
    "mcpServers": {
      "mem": {
        "type": "stdio",
        "command": "python3",
        "args": ["$DEST/honcho_mcp.py"],
        "env": { "HONCHO_HOME": "$DEST" }
      }
    }
  }
EOF
  exit 0
fi

if [ "$H" = "codex" ]; then
  cat <<EOF

Codex reads TOML. Append to $CFG:

  [mcp_servers.mem]
  command = "python3"
  args = ["$DEST/honcho_mcp.py"]
  [mcp_servers.mem.env]
  HONCHO_HOME = "$DEST"
EOF
  exit 0
fi

python3 - "$CFG" "$DEST" <<'PY'
import json, os, sys
cfg, dest = sys.argv[1], sys.argv[2]
parent = os.path.dirname(cfg)
if parent:
    os.makedirs(parent, exist_ok=True)
data = {}
if os.path.exists(cfg):
    try:
        with open(cfg) as fh:
            data = json.load(fh) or {}
    except json.JSONDecodeError:
        sys.exit(f"{cfg} is not valid JSON; edit it by hand")
servers = data.setdefault("mcpServers", {})
if "mem" in servers:
    print(f"note    : {cfg} already has a 'mem' entry; leaving it alone")
else:
    servers["mem"] = {
        "type": "stdio",
        "command": "python3",
        "args": [os.path.join(dest, "honcho_mcp.py")],
        "env": {"HONCHO_HOME": dest},
    }
    with open(cfg, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    print(f"wrote   : {cfg}")
PY

cat <<EOF

Restart $H and the agent gains: recall, remember, reinforce, forget, stats.

Import memory that already exists as Markdown:
  python3 $DEST/migrate_md.py <file.md> --db $DEST/memory.db
EOF
