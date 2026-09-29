#!/usr/bin/env bash
# The script half of the "coding" check (CHECKS.md, the first canonical
# setup): lint and type-check the files the turn wrote, where the session
# runs, with the session's own folders. Exit 0 = pass; anything printed
# before a non-zero exit becomes the finding the agent reads.
set -u
INPUT="${OTODOCK_CHECK_INPUT:-${OTODOCK_STEP_PAYLOAD:-}}"
[ -n "$INPUT" ] && [ -f "$INPUT" ] || { echo "no changed set"; exit 2; }

# The written files, one per line, as the session named them.
FILES=$(python3 - "$INPUT" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
for p in doc.get("paths") or []:
    if p.get("writes") and p.get("path"):
        print(p["path"])
PY
)
[ -n "$FILES" ] || { echo "nothing written"; exit 0; }

status=0
py=$(echo "$FILES" | grep -E '\.py$' || true)
ts=$(echo "$FILES" | grep -E '\.(ts|tsx)$' || true)

if [ -n "$py" ] && command -v ruff >/dev/null 2>&1; then
  echo "$py" | xargs ruff check --quiet || status=1
fi
if [ -n "$ts" ] && [ -f package.json ] && command -v npx >/dev/null 2>&1; then
  npx --no-install tsc --noEmit -p . 2>&1 | tail -40 || status=1
fi
exit $status
