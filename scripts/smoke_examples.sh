#!/usr/bin/env bash
# Runs every example offline through the CLI, the way the README describes.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
cd "$work"
export REWINDABLE_DB="$work/runs.db"

latest() { rewindable runs | awk 'NR==2 {print $1}'; }

echo "--- 01 crash and resume"
if python "$root/examples/01_crash_and_resume.py" --crash; then
  echo "expected the simulated crash"; exit 1
fi
run="$(latest)"
rewindable resume "$run" | tee out.txt
grep -q "replayed 9 steps" out.txt

echo "--- 02 human approval"
python "$root/examples/02_human_approval.py"
run="$(latest)"
key="$(rewindable pending | awk 'NR==1 {print $2}')"
rewindable approve "$run" "$key" | tee out.txt
grep -q "Refund of €129.00 issued" out.txt

echo "--- 03 research team"
python "$root/examples/03_research_team.py" | grep -q "## Brief"
rewindable show "$(latest)" > /dev/null

echo "--- 04 rewind and fix"
python "$root/examples/04_rewind_and_fix.py"
run="$(latest)"
REWINDABLE_DEMO_FIXED=1 rewindable rewind "$run" --to 2 | tee out.txt
grep -q "270.0 USD" out.txt

echo "--- 05 durable timers and the worker"
export REWINDABLE_DEMO_FAST=1
python "$root/examples/05_follow_up_later.py"
run="$(latest)"
sleep 5                       # the 3 "days" pass
rewindable worker --once      # wakes it; it now waits for an approval with a 1-"day" timeout
sleep 2
rewindable worker --once      # nobody answered in time
rewindable show "$run" | tee out.txt
grep -q "no follow-up sent" out.txt

echo "--- queue a run, let a worker drive it"
queued="$(rewindable start "$root/examples/04_rewind_and_fix.py:answer" --input '"How much is 250 EUR in USD?"' --queue)"
rewindable worker --once
rewindable show "$queued" | grep -q "completed"

echo "all examples OK"
