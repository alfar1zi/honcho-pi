#!/usr/bin/env bash
# secret-scan.sh — Scan text for potential secrets.
# Usage: secret-scan.sh "text to scan"
set -uo pipefail

TEXT="${1:-}"
if [[ -z "$TEXT" ]]; then
  echo "Usage: $0 \"text to scan\"" >&2
  exit 2
fi

FOUND=0
if echo "$TEXT" | grep -qiE '(sk-|sk_live_|sk_test_|pk_live_|pk_test_)'; then
  echo "WARNING: Possible API key pattern detected"
  FOUND=1
fi
if echo "$TEXT" | grep -qiE '(password|secret|api_key|token|credential)\s*[=:][^\s]+'; then
  echo "WARNING: Possible secret assignment detected"
  FOUND=1
fi
if echo "$TEXT" | grep -qiE '-----BEGIN [A-Z ]+PRIVATE KEY-----'; then
  echo "WARNING: Possible private key detected"
  FOUND=1
fi

if [[ $FOUND -eq 1 ]]; then
  exit 1
fi
exit 0