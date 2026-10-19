"""The orchestrator: code decides the next step, enforces stop conditions, and times every stage.

  prepare -> enrich -> (only if enrichment finished for the target) verify -> group -> rank -> memo

A stage that stops early (spend cap, time cap, Ctrl+C, API outage) ends the run; downstream stages don't run
on incomplete inputs. Rerunning the same command resumes from saved state.
"""

import json
import time
from datetime import datetime, timezone

from . import common as c
from . import enrich, prepare, stages


def log_stage(run_id, stage, started, **extra):
    rec = {"run_id": run_id, "stage": stage, "started_at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
           "seconds": round(time.time() - started, 3), **extra}
    with (c.STATE / "run_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def run(*, input_csv, upto, workers=1, cap=15.0, verify_frac=0.02, verify_min=20, max_minutes=None,
        stop_after=None, all_rows=False, core=100_000, extension=50_000):
    run_id = "run-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    t0 = time.time()
    timings = {}
    db = c.connect()
    if not db.execute("SELECT COUNT(*) FROM records").fetchone()[0]:
        s = time.time()
        rep = prepare.run(input_csv, 10**9 if all_rows else core, 0 if all_rows else extension)
        prepare.summary(rep)
        timings["prepare"] = log_stage(run_id, "prepare", s)["seconds"]
        db = c.connect()

    s = time.time()
    e = enrich.Enricher(run_id=run_id, upto_rank=upto, workers=workers, cap_usd=cap, max_minutes=max_minutes,
                        stop_after_batches=stop_after)
    status = e.run()
    timings["enrich"] = log_stage(run_id, "enrich", s, status=status, stop_reason=e.stop_reason,
                                  calls=e.run_calls, failed_calls=e.run_failures, spend_usd=round(e.run_spend, 6),
                                  records_done=e.done_records)["seconds"]
    pending = db.execute("SELECT COUNT(*) FROM records WHERE status='pending' AND scope_rank < ?", (upto,)).fetchone()[0]
    if status != "finished" or pending:
        print(f"Stopping before downstream stages: enrichment {status}, {pending} pending in target. "
              "Rerun the same command to resume.")
        return summarize(run_id, t0, timings, "stopped")

    calls = stages.Calls(cap, run_id)
    for name, fn in (("verify", lambda: stages.verify(db, calls, frac=verify_frac, min_n=verify_min)),
                     ("group", lambda: stages.group(db, calls)),
                     ("rank", lambda: stages.rank(db)),
                     ("memo", lambda: stages.memo(db, calls))):
        s = time.time()
        before = calls.made
        fn()
        timings[name] = log_stage(run_id, name, s, calls=calls.made - before)["seconds"]
    return summarize(run_id, t0, timings, "finished")


def summarize(run_id, t0, timings, status):
    db = c.connect()
    counts = dict(db.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())
    out = {"run_id": run_id, "status": status, "wall_clock_seconds": round(time.time() - t0, 3),
           "stage_seconds": timings, "record_counts": counts, "finished_at": datetime.now(timezone.utc).isoformat()}
    c.write_json(c.OUTPUTS / "run_summary.json", out)
    with (c.STATE / "run_log.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"run_id": run_id, "stage": "summary", **out}) + "\n")
    print(f"\n[{run_id}] {status} in {out['wall_clock_seconds']}s; stages {timings}; records {counts}")
    return out
