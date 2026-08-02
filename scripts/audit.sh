#!/usr/bin/env bash
# ── PK Ninja Agent — Security & Dependency Audit ────────────────────────
# Runs security checks on the codebase. Suitable for CI and local dev.
#
# Usage:
#   ./scripts/audit.sh          # run all checks
#   ./scripts/audit.sh --quick  # skip slow checks
# ────────────────────────────────────────────────────────────────────────
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check() {
    local name="$1"
    shift
    echo -e "\n${YELLOW}[$name]${NC} Running..."
    if "$@"; then
        echo -e "${GREEN}[$name]${NC} PASSED"
        PASS=$((PASS + 1))
    else
        echo -e "${RED}[$name]${NC} FAILED"
        FAIL=$((FAIL + 1))
    fi
}

warn_check() {
    local name="$1"
    shift
    echo -e "\n${YELLOW}[$name]${NC} Running..."
    if "$@"; then
        echo -e "${GREEN}[$name]${NC} PASSED"
        PASS=$((PASS + 1))
    else
        echo -e "${YELLOW}[$name]${NC} WARNING (non-blocking)"
        WARN=$((WARN + 1))
    fi
}

# ── 1. Dependency audit ─────────────────────────────────────────────────
dep_audit() {
    pip-audit --strict --desc 2>&1
}

# ── 2. Security scan (bandit) ───────────────────────────────────────────
sec_scan() {
    bandit -r backend/ agents/ providers/ \
        --severity-level medium \
        --confidence-level medium \
        -q 2>&1
}

# ── 3. Check for hardcoded secrets ──────────────────────────────────────
secret_scan() {
    # Look for potential hardcoded secrets in Python files
    local found=0
    for pattern in 'ghp_' 'sk-' 'AKIA' 'password\s*=\s*["\x27][^"\x27]' 'secret\s*=\s*["\x27][^"\x27]'; do
        matches=$(grep -rn --include="*.py" -E "$pattern" backend/ agents/ providers/ 2>/dev/null | \
                  grep -v 'test' | grep -v 'example' | grep -v 'default=""' | grep -v 'your_' || true)
        if [ -n "$matches" ]; then
            echo "Potential secret found: $matches"
            found=1
        fi
    done
    return $found
}

# ── 4. Check .env is not committed ──────────────────────────────────────
env_check() {
    if git ls-files --error-unmatch .env 2>/dev/null; then
        echo ".env is tracked by git!"
        return 1
    fi
    return 0
}

# ── 5. Check for dangerous imports ──────────────────────────────────────
dangerous_imports() {
    local found=0
    for module in "pickle" "subprocess" "os.system" "eval(" "exec("; do
        matches=$(grep -rn --include="*.py" "$module" backend/ agents/ providers/ 2>/dev/null | \
                  grep -v "test" | grep -v "#" | head -5 || true)
        if [ -n "$matches" ]; then
            echo "Found '$module' usage:"
            echo "$matches"
            found=1
        fi
    done
    return $found
}

# ── Run checks ──────────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════"
echo " PK Ninja Agent — Security Audit"
echo "═══════════════════════════════════════════════════════"

check "Dependency Audit" dep_audit
check "Security Scan (bandit)" sec_scan
check "Hardcoded Secrets" secret_scan
check "Git .env Check" env_check
warn_check "Dangerous Imports" dangerous_imports

echo ""
echo "═══════════════════════════════════════════════════════"
echo -e " Results: ${GREEN}${PASS} passed${NC}, ${YELLOW}${WARN} warnings${NC}, ${RED}${FAIL} failed${NC}"
echo "═══════════════════════════════════════════════════════"

exit $FAIL
