#!/usr/bin/env bash
# clean-code.sh - repo quality gate: secrets, eval, markers, em dashes, long lines.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SELF_DIR/.." && pwd)"
cd "$ROOT_DIR"

FILES=()
while IFS= read -r f; do FILES+=("$f"); done < <(
  find src bin tests -type f \( -name '*.py' -o -name '*.sh' \) | sort
)

ERRORS=0
fail() { echo "  x $1"; ERRORS=$((ERRORS + 1)); }

# Built from fragments so this file does not match its own patterns.
MARKER_RE="T""ODO|F""IXME"
CRED_NAMES="password|passwd|api_key|apikey|access_token|refresh_token"
CRED_NAMES+="|client_secret|secret_key"
CRED_RE="(${CRED_NAMES})[[:space:]]*[=:][[:space:]]*[\"'][^\"']{16,}[\"']"
EMDASH=$'\u2014'

echo "=== Clean Code Verification ==="

echo "--- hardcoded credentials ---"
for f in "${FILES[@]}"; do
  if grep -qE "$CRED_RE" "$f"; then fail "$f: possible hardcoded credential"; fi
done

echo "--- eval/exec in Python ---"
for f in "${FILES[@]}"; do
  case "$f" in
    *.py) if grep -qE '\b(eval|exec)[[:space:]]*\(' "$f"; then fail "$f: uses eval()/exec()"; fi ;;
  esac
done

echo "--- unresolved markers ---"
for f in "${FILES[@]}"; do
  count=$(grep -cE "$MARKER_RE" "$f" || true)
  if [[ "${count:-0}" -gt 0 ]]; then fail "$f: $count marker(s)"; fi
done

echo "--- em dash ---"
for f in "${FILES[@]}"; do
  if grep -q "$EMDASH" "$f"; then fail "$f: contains an em dash"; fi
done

echo "--- line length (max 120) ---"
for f in "${FILES[@]}"; do
  # LC_ALL=C: gawk in a UTF-8 locale counts characters, CI mawk counts bytes;
  # pin the byte semantics so the gate agrees everywhere.
  if LC_ALL=C awk 'length > 120 {found=1} END {exit !found}' "$f"; then
    fail "$f: line longer than 120 chars"
  fi
done

echo "=== Verification Complete ==="
if [[ $ERRORS -eq 0 ]]; then
  echo "all checks passed"
  exit 0
fi
echo "$ERRORS error(s) found"
exit 1
