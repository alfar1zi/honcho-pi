#!/usr/bin/env bash
# secret-scan.sh — Detect secrets in text before saving to memory DB.
# Detects: API keys, passwords, tokens, SSH keys, private keys, etc.
#
# Usage: secret-scan.sh <text>
#   Exits 0 if no secrets found, 1 if secrets detected.
#   Prints detected secret types to stderr.
#
# Patterns are designed to be specific to avoid false positives on normal text.

set -euo pipefail

TEXT="${1:-}"

if [[ -z "$TEXT" ]]; then
    exit 0
fi

FOUND_SECRETS=0

# AWS Access Key ID (AKIA + 16 chars)
if echo "$TEXT" | grep -qE 'AKIA[0-9A-Z]{16}'; then
    echo "SECRET_DETECTED: AWS Access Key ID" >&2
    FOUND_SECRETS=1
fi

# AWS Secret Access Key (40 chars)
if echo "$TEXT" | grep -qiE 'aws_secret_access_key\s*[:=]\s*[A-Za-z0-9/+=]{40}'; then
    echo "SECRET_DETECTED: AWS Secret Access Key" >&2
    FOUND_SECRETS=1
fi

# GitHub tokens (ghp_, gho_, ghu_, ghs_, ghv_)
if echo "$TEXT" | grep -qE '(ghp|gho|ghu|ghs|ghv)_[A-Za-z0-9]{36,}'; then
    echo "SECRET_DETECTED: GitHub Token" >&2
    FOUND_SECRETS=1
fi

# OpenAI API keys (sk- followed by 48+ chars)
if echo "$TEXT" | grep -qE 'sk-[A-Za-z0-9]{48,}'; then
    echo "SECRET_DETECTED: OpenAI API Key" >&2
    FOUND_SECRETS=1
fi

# Generic sk_ or pk_ with live/test/dev prefix
if echo "$TEXT" | grep -qiE '(sk|pk)_(live|test|dev)_[A-Za-z0-9]{24,}'; then
    echo "SECRET_DETECTED: API Key (sk_/pk_ prefix)" >&2
    FOUND_SECRETS=1
fi

# SSH Private Keys
if echo "$TEXT" | grep -q 'BEGIN [A-Z ]*PRIVATE KEY'; then
    echo "SECRET_DETECTED: SSH Private Key" >&2
    FOUND_SECRETS=1
fi

# PGP Private Keys
if echo "$TEXT" | grep -q 'BEGIN PGP PRIVATE KEY BLOCK'; then
    echo "SECRET_DETECTED: PGP Private Key" >&2
    FOUND_SECRETS=1
fi

# Generic password/secret assignment
if echo "$TEXT" | grep -qiE '(password|passwd|api_key|apikey|api_secret|access_token|refresh_token|client_secret|secret_key)\s*[:=]\s*[^\s]{12,}'; then
    echo "SECRET_DETECTED: Password/Secret/Token assignment" >&2
    FOUND_SECRETS=1
fi

# Generic "secret =" with 12+ char value
if echo "$TEXT" | grep -qiE '(^|\s)secret\s*[:=]\s*[^\s]{12,}'; then
    echo "SECRET_DETECTED: Secret assignment" >&2
    FOUND_SECRETS=1
fi

exit $FOUND_SECRETS
