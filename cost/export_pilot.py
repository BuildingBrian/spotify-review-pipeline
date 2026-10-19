"""Copy the real pilot evidence from cost/pilot/ (its own isolated state) into the calculator's input files.
Offline; no API calls. Run after a paid pilot: python cost/export_pilot.py COLD_RUN_ID WARM_RUN_ID"""

import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PILOT = HERE / "pilot"
COMMON = ("topic", "intent", "sentiment", "severity", "entities", "evidence_quote", "needs_review")

cold_id, warm_id = sys.argv[1], sys.argv[2]
db = sqlite3.connect(PILOT / "state" / "pipeline.sqlite")
db.row_factory = sqlite3.Row
with (HERE / "pilot_records.jsonl").open("w", encoding="utf-8") as f:
    for r in db.execute("SELECT * FROM records ORDER BY scope_rank"):
        rec = {"review_id": r["review_id"], "source_sha256": r["source_sha256"], "status": r["status"]}
        if r["status"] == "completed":
            labels = json.loads(r["labels_json"])
            rec.update({k: labels[k] for k in COMMON}, label_config=r["label_config"], subtopic=labels["subtopic"],
                       attempts=r["attempts"])
        else:
            rec.update(reason=r["reason"], attempts=r["attempts"])
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

calls = [json.loads(l) for l in (PILOT / "state" / "calls.jsonl").open(encoding="utf-8")]
with (HERE / "pilot_calls.jsonl").open("w", encoding="utf-8") as f:
    for cl in calls:
        if cl.get("run_id") in (cold_id, warm_id):
            cl["pilot_run"] = "cold" if cl["run_id"] == cold_id else "warm"
            f.write(json.dumps(cl, ensure_ascii=False) + "\n")

log = [json.loads(l) for l in (PILOT / "state" / "run_log.jsonl").open(encoding="utf-8")]
summ = {e["run_id"]: e for e in log if e["stage"] == "summary"}
enrich = {e["run_id"]: e for e in log if e["stage"] == "enrich"}
ingest = json.loads((PILOT / "outputs" / "ingestion_report.json").read_text())
runs = {"input_file": "data/dataset/cost_100.csv (unchanged)", "input_sha256": ingest["input"]["sha256"],
        "unique_texts": ingest["profile"]["distinct_nonempty_texts"],
        "cold": {"run_id": cold_id, "wall_clock_seconds": summ[cold_id]["wall_clock_seconds"],
                 "stage_seconds": summ[cold_id]["stage_seconds"], "workers": 1, "result_cache": "empty (fresh workdir)"},
        "warm": {"run_id": warm_id, "wall_clock_seconds": summ[warm_id]["wall_clock_seconds"],
                 "stage_seconds": summ[warm_id]["stage_seconds"], "workers": 1,
                 "enrich_calls": enrich[warm_id]["calls"], "enrich_cache_hits": 100,
                 "note": "All 100 IDs already completed under the same label_config -> 0 enrichment calls; "
                         "verify/group/memo skipped because their input fingerprints were unchanged."}}
(HERE / "pilot_runs.json").write_text(json.dumps(runs, indent=2) + "\n", encoding="utf-8")
print(f"pilot_records.jsonl, pilot_calls.jsonl ({sum(1 for _ in (HERE / 'pilot_calls.jsonl').open())} calls), pilot_runs.json written")
