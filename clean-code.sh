#!/usr/bin/env bash
# clean-code.sh — Clean code validation script for omp-memory project.
# Checks for code quality issues, AI-generated patterns, and common anti-patterns.

set -euo pipefail

MEMORY_DIR="$HOME/.omp/memory"
PYTHON="${PYTHON:-python3}"

CHECK_FILES=(
    "memory_db.py"
    "memory_provider.py"
    "memory_context.py"
    "capture-session.sh"
    "analyze-session.sh"
    "recap-service.sh"
    "memory-guard.sh"
    "disk-check.sh"
    "secret-scan.sh"
    "audit-log.sh"
    "health-check.sh"
)

echo "=== Clean Code Verification ==="
echo ""

ERRORS=0

echo "--- Checking for hardcoded credentials ---"
for f in "${CHECK_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        if grep -qiE '(password|secret|api_key|token|pwd)\s*[:=]\s*["\x27]?[A-Za-z0-9]{10,}' "$f" 2>/dev/null; then
            echo "  x $f: possible hardcoded credentials"
            ERRORS=$((ERRORS + 1))
        else
            echo "  ok $f: no hardcoded credentials"
        fi
    fi
done

echo ""
echo "--- Checking for console.log in Python files ---"
for f in "${CHECK_FILES[@]}"; do
    if [[ -f "$f" && "$f" == *.py ]]; then
        if grep -q 'console\.log\|console\.error' "$f" 2>/dev/null; then
            echo "  x $f: contains console.log"
            ERRORS=$((ERRORS + 1))
        else
            echo "  ok $f: no console.log"
        fi
    fi
done

echo ""
echo "--- Checking for eval() in Python files ---"
for f in "${CHECK_FILES[@]}"; do
    if [[ -f "$f" && "$f" == *.py ]]; then
        if grep -q '\beval\s*(' "$f" 2>/dev/null; then
            echo "  x $f: contains eval()"
            ERRORS=$((ERRORS + 1))
        else
            echo "  ok $f: no eval()"
        fi
    fi
done

echo ""
echo "--- Checking for TODO comments ---"
for f in "${CHECK_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        count=$(grep -ci 'TODO\|FIXME\|XXX' "$f" 2>/dev/null || echo "0")
        if [[ "$count" -gt 0 ]]; then
            echo "  ! $f: $count TODO/FIXME comments"
        else
            echo "  ok $f: no TODO/FIXME"
        fi
    fi
done

echo ""
echo "--- Checking line length (max 120 chars) ---"
for f in "${CHECK_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        long_lines=$(awk -v len=120 'length > len {print NR": "length}' "$f" 2>/dev/null || true)
        if [[ -n "$long_lines" ]]; then
            echo "  ! $f: long lines:"
            echo "$long_lines" | while read line; do echo "    $line"; done
        else
            echo "  ok $f: no long lines"
        fi
    fi
done

echo ""
echo "=== Verification Complete ==="
if [[ $ERRORS -eq 0 ]]; then
    echo "All checks passed!"
else
    echo "$ERRORS error(s) found."
    exit 1
fi
