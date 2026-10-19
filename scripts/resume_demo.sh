#!/bin/bash
# Interrupt/resume demonstration on the 500-review checkpoint (plus the golden 50): scope ranks 0..549.
# Step 1 runs 4 batches then stops on purpose (planned interruption). Step 2 resumes the SAME command.
# Already-completed reviews are never re-sent: phase=resume calls contain only pending IDs.
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
say() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$1"; sleep 2; }

say "BEFORE: record counts"
$PY -m pipeline status | head -8

say "STEP 1: start enrichment of 550 reviews, interrupted after 4 batches (200 reviews)"
$PY -m pipeline enrich --paid --upto 550 --workers 1 --stop-after 4

say "AFTER INTERRUPTION: saved progress"
$PY -m pipeline status | head -8

say "STEP 2: resume with the same command (no --stop-after)"
$PY -m pipeline enrich --paid --upto 550 --workers 1

say "AFTER RESUME: checkpoints and proof nothing was re-sent"
$PY - <<'PY'
import json, glob
snaps = sorted(glob.glob("state/checkpoints/enrich-*-start.json"))
before = set(json.load(open(snaps[-1]))["completed_ids"])
after = set(json.load(open(snaps[-1].replace("-start", "-end")))["completed_ids"])
calls = [json.loads(l) for l in open("state/calls.jsonl")]
resumed = [c for c in calls if c.get("role") == "enrich" and c.get("phase") == "resume"]
resent = sum(len(before & set(c["review_ids"])) for c in resumed)
print(f"completed before resume: {len(before)}   after resume: {len(after)}   new: {len(after - before)}")
print(f"resume-phase enrichment calls: {len(resumed)}   already-completed IDs re-sent: {resent}")
PY
