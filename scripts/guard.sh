#!/usr/bin/env bash
#
# Eval-isolation guard.
#
# Once configs/eval100.txt exists, the tests/, solution/, verifier, oracle and grader
# files of every task on that list are off limits: reading them would contaminate the
# harness we are measuring. This script fails when
#
#   1. a staged commit touches a denied path, or
#   2. a recorded shell/tool access log shows a denied path being read.
#
# Run standalone (`make guard`) and from .git/hooks/pre-commit.
#
# Exit codes: 0 clean, 1 violation, 2 misuse.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 2

EVAL_LIST="configs/eval100.txt"
TASKS_DIR="${ERP_TASKS_DIR:-vendor/erp-bench/tasks}"
ACCESS_LOG="${ERP_ACCESS_LOG:-.guard/access.log}"

# Directories that hold grader/oracle material inside a task.
DENIED_SUBDIRS=(tests solution)

fail=0

note() { printf '%s\n' "$*" >&2; }

if [[ ! -f "$EVAL_LIST" ]]; then
  echo "guard: $EVAL_LIST does not exist yet — eval isolation not armed (no eval set yet). OK."
  exit 0
fi

# Build one grep -F pattern file of denied path fragments. (Plain while-read, not
# mapfile: macOS ships bash 3.2.)
PATTERNS="$(mktemp)"
PATTERN_NAMES="$(mktemp)"
trap 'rm -f "$PATTERNS" "$PATTERN_NAMES"' EXIT

n_ids=0
while IFS= read -r id; do
  id="${id%$'\r'}"
  [[ -z "$id" || "$id" == \#* ]] && continue
  n_ids=$((n_ids + 1))
  for sub in "${DENIED_SUBDIRS[@]}"; do
    printf '%s/%s/\n' "$id" "$sub" >>"$PATTERNS"
  done
done <"$EVAL_LIST"

if [[ $n_ids -eq 0 ]]; then
  note "guard: $EVAL_LIST is empty — refusing to run unarmed."
  exit 2
fi

check_stream() {
  local label="$1"
  local hits
  hits="$(grep -F -f "$PATTERNS" - 2>/dev/null || true)"
  if [[ -n "$hits" ]]; then
    note "guard: eval-isolation violation in $label:"
    printf '%s\n' "$hits" | sed 's/^/  /' >&2
    fail=1
  fi
}

# 1. Staged and unstaged changes must not add eval task grader files to the repo.
if git rev-parse --git-dir >/dev/null 2>&1; then
  { git diff --cached --name-only; git diff --name-only; git ls-files; } 2>/dev/null \
    | sort -u | check_stream "tracked/staged paths"
fi

# 2. Recorded accesses (optional; written by whatever tooling we point at it).
if [[ -f "$ACCESS_LOG" ]]; then
  check_stream "$ACCESS_LOG" <"$ACCESS_LOG"
fi

# 3. Nothing under harness/ may embed a task pattern name or an eval task id: that would
#    be pattern-specific logic.
if [[ -d "$TASKS_DIR" ]]; then
  grep -h '^task_pattern' "$TASKS_DIR"/*/task.toml 2>/dev/null \
    | sed -E 's/task_pattern = "?([^"]*)"?/\1/' | sort -u >"$PATTERN_NAMES"
  if [[ -s "$PATTERN_NAMES" ]] && [[ -d harness ]]; then
    hits="$(grep -rnF -f "$PATTERN_NAMES" harness/ 2>/dev/null || true)"
    if [[ -n "$hits" ]]; then
      note "guard: pattern-specific logic found under harness/:"
      printf '%s\n' "$hits" | head -20 | sed 's/^/  /' >&2
      fail=1
    fi
  fi
fi

if [[ $fail -eq 0 ]]; then
  echo "guard: OK ($n_ids eval task ids protected)"
fi
exit $fail
