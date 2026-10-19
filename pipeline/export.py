"""Write the standardized grading/ folder (GRADING_CONTRACT.md) from saved state, then run the course checker.
Offline; no model calls.

  grading/run.json, ingestion.json (written by prepare via the course helper), records.jsonl.gz,
  membership.csv, ranking.csv, claims.csv, calls.jsonl, checkpoint_before.json, checkpoint_after.json
"""

import gzip
import json
import shutil
import subprocess
import sys

from . import common as c

GRADING = c.ROOT / "grading"
COMMON = ("topic", "intent", "sentiment", "severity", "entities", "evidence_quote", "needs_review")
CALL_FIELDS = ("request_id", "role", "review_ids", "model", "phase", "outcome", "label_config", "input_tokens",
               "output_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "cost_usd", "latency_s",
               "attempt", "run_id", "error", "started_at")


def export(before_snapshot, after_snapshot):
    GRADING.mkdir(exist_ok=True)
    db = c.connect()
    scope = c.read_json(c.OUTPUTS / "scope.json")
    pending = db.execute("SELECT COUNT(*) FROM records WHERE status='pending'").fetchone()[0]
    if pending:
        print(f"WARNING: {pending} records still pending; they will be exported as quarantined 'not_completed_at_export'.")

    c.write_json(GRADING / "run.json", {
        "version": "a5-audit-v1", "analysis_count": scope["analysis_count"], "analysis_sha256": scope["analysis_sha256"],
        "classification_input_fields": ["review_text"], "allow_multi_issue": False})

    n = {"completed": 0, "quarantined": 0, "cached": 0}
    with gzip.open(GRADING / "records.jsonl.gz", "wt", encoding="utf-8") as f:
        for r in db.execute("SELECT * FROM records ORDER BY scope_rank, review_id"):
            rec = {"review_id": r["review_id"], "source_sha256": r["source_sha256"]}
            if r["status"] == "completed":
                labels = json.loads(r["labels_json"])
                rec.update(status="completed", **{k: labels[k] for k in COMMON}, label_config=r["label_config"],
                           subtopic=labels["subtopic"])
                if r["cache_source_id"]:
                    rec["cache_source_id"] = r["cache_source_id"]
                    n["cached"] += 1
                n["completed"] += 1
            else:
                rec.update(status="quarantined", reason=r["reason"] if r["status"] == "quarantined"
                           else "not_completed_at_export", attempts=r["attempts"])
                n["quarantined"] += 1
            f.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
    for name in ("membership.csv", "ranking.csv", "claims.csv"):
        shutil.copyfile(c.OUTPUTS / name, GRADING / name)

    seen = set()
    with (GRADING / "calls.jsonl").open("w", encoding="utf-8") as f:
        for line in (c.STATE / "calls.jsonl").open(encoding="utf-8"):
            cl = json.loads(line)
            if cl.get("event") or not cl.get("role"):
                continue
            out = {k: cl.get(k) for k in CALL_FIELDS if k in cl}
            out["review_ids"] = cl.get("review_ids") or []
            out["input_tokens"], out["output_tokens"] = int(cl.get("input_tokens") or 0), int(cl.get("output_tokens") or 0)
            if out["request_id"] in seen:  # never expected; keep IDs unique for the checker
                out["request_id"] = f"{out['request_id']}-{cl['local_call_id']}"
            seen.add(out["request_id"])
            f.write(json.dumps(out, ensure_ascii=False) + "\n")

    for name, src in (("checkpoint_before.json", before_snapshot), ("checkpoint_after.json", after_snapshot)):
        snap = c.read_json(c.STATE / "checkpoints" / src)
        c.write_json(GRADING / name, {"completed_ids": snap["completed_ids"], "run_id": snap["run_id"],
                                       "label_config": snap["label_config"], "taken_at": snap["taken_at"],
                                       "source_snapshot": src})
    print(f"grading/ written: {n['completed']} completed ({n['cached']} exact-text cache reuses), "
          f"{n['quarantined']} quarantined, {len(seen)} calls")
    return check()


def check():
    """Course checker, run exactly as GRADING_CONTRACT.md describes; reports stay outside grading/."""
    out = c.ROOT / "evals" / "checker"
    out.mkdir(parents=True, exist_ok=True)
    py = [sys.executable, "-I", str(c.CHECKER)]
    ref = out / "local-reference.json"
    analysis = c.STATE / "analysis_scope.csv"
    if not ref.exists() or c.read_json(ref).get("analysis_sha256") != c.file_sha(analysis):
        subprocess.run(py + ["reference", "--full", str(c.FULL_CSV), "--analysis", str(analysis), "--out", str(ref)], check=True)
    subprocess.run(py + ["check", "--reference", str(ref), "--submission", str(GRADING), "--out", str(out / "self-check.json")],
                   check=True)
    rep = c.read_json(out / "self-check.json")
    print(f"checker status: {rep['status']}; coverage {rep['coverage']}; issues {rep['issue_counts']}")
    return rep
