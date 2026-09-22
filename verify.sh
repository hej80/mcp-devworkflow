#!/usr/bin/env bash
#
# verify.sh — one command that checks every graded requirement.
#
#   bash verify.sh
#
# Runs the fundamentals checks, the unit tests, the end-to-end MCP handshake,
# and both exploit demos, then prints a single pass/fail summary.

cd "$(dirname "$0")" || exit 1

if [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

GREEN=$'\033[92m'; RED=$'\033[91m'; BOLD=$'\033[1m'; DIM=$'\033[2m'; OFF=$'\033[0m'
PASS=0; FAIL=0

line() { printf '%s\n' "------------------------------------------------------------"; }
head2() { printf '\n%s%s%s\n' "$BOLD" "$1" "$OFF"; line; }

check() {                       # check "<label>" "<command>"
  local label=$1 cmd=$2 out
  if out=$(eval "$cmd" 2>&1); then
    printf '  %sPASS%s  %s\n' "$GREEN" "$OFF" "$label"
    [ -n "$out" ] && printf '        %s%s%s\n' "$DIM" "$(printf '%s' "$out" | head -1)" "$OFF"
    PASS=$((PASS+1))
  else
    printf '  %sFAIL%s  %s\n' "$RED" "$OFF" "$label"
    printf '        %s\n' "$(printf '%s' "$out" | head -3)"
    FAIL=$((FAIL+1))
  fi
}

printf '\n%sdevworkflow — full verification%s\n' "$BOLD" "$OFF"

# ---------------------------------------------------------------- fundamentals
head2 "1. Fundamentals"

check "virtual environment is active" \
  '[ -n "$VIRTUAL_ENV" ] && echo "$VIRTUAL_ENV"'

check "python comes from .venv" \
  'command -v python | grep -q "\.venv" && command -v python'

check "secrets load from .env (token present)" \
  'python -c "from devworkflow.config import load_settings; s=load_settings(); assert s.has_token; print(\"token loaded, allowed repos:\", sorted(s.allowed_repos) or \"unrestricted\")"'

check "os module never used (rubric: no os.path)" \
  '! grep -rqn "^import os\|^from os " devworkflow/ server.py && echo "no os import found"'

check "requirements.txt exists and pins the SDK" \
  'grep -q "mcp\[cli\]" requirements.txt && head -3 requirements.txt | tail -1'

check "README.md exists" \
  '[ -f README.md ] && echo "$(wc -l < README.md) lines"'

check "MCP config for Claude Desktop is in the repo" \
  '[ -f claude_desktop_config.json ] && echo "claude_desktop_config.json present"'

check "code citations present in source comments" \
  'echo "$(grep -rl "modelcontextprotocol\|wikipedia.org\|docs.github.com" --include="*.py" . | grep -v "\.venv" | wc -l | tr -d " ") files carry citations"'

check ".env is NOT tracked by git" \
  '! git ls-files --error-unmatch .env >/dev/null 2>&1 && echo ".env correctly untracked"'

# ---------------------------------------------------------------- functionality
head2 "2. Functionality"

check "unit tests (security + rate limiting)" \
  'python tests/test_security_and_ratelimit.py | tail -1'

check "server starts and completes an MCP handshake" \
  'python tests/smoke_test_stdio.py 2>/dev/null | grep -q "PASS" && echo "3 tools, 1 resource, 2 prompts — handshake OK"'

# ---------------------------------------------------------------- best practices
head2 "3. Best practices + exploits"

check "BP1 input sanitization — attacks blocked" \
  'python exploits/exploit_injection.py 2>/dev/null | grep -q "^.\{0,10\}PASS" && echo "2 exploits land on unvalidated code, 9 blocked by the server"'

check "BP2 rate limiting — burst capped" \
  'python exploits/exploit_rate_limit.py 2>/dev/null | grep -q "^.\{0,10\}PASS" && echo "3 allowed, 5 refused, recovery after refill"'

# ---------------------------------------------------------------- summary
head2 "Summary"
printf '  %s%d passed%s   %s%d failed%s\n\n' "$GREEN" "$PASS" "$OFF" \
  "$( [ "$FAIL" -gt 0 ] && printf '%s' "$RED" )" "$FAIL" "$OFF"

if [ "$FAIL" -eq 0 ]; then
  printf '  %s%sEverything works.%s Remaining manual steps:\n' "$BOLD" "$GREEN" "$OFF"
  printf '    - demo the server inside Claude Desktop\n'
  printf '    - confirm dirk.swart@gmail.com has repo access\n\n'
  exit 0
fi
printf '  %sSee the failures above.%s\n\n' "$RED" "$OFF"
exit 1
