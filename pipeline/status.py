"""Offline status: record counts by scope part and status, total spend, latest progress."""

import json

from . import common as c


def show():
    db = c.connect()
    print("Records by scope part and status:")
    for r in db.execute("SELECT scope_part, status, COUNT(*) n FROM records GROUP BY 1, 2 ORDER BY 1, 2"):
        print(f"  {r['scope_part']:<10} {r['status']:<12} {r['n']:>8,}")
    reasons = db.execute("SELECT reason, COUNT(*) n FROM records WHERE status='quarantined' GROUP BY 1").fetchall()
    for r in reasons:
        print(f"  quarantine reason {r['reason']}: {r['n']}")
    log = c.STATE / "calls.jsonl"
    if log.exists():
        calls = [json.loads(l) for l in log.open(encoding="utf-8")]
        ok = sum(x.get("outcome") == "succeeded" for x in calls)
        print(f"Calls logged: {len(calls)} ({ok} succeeded, {len(calls) - ok} failed); "
              f"spend ${sum(x.get('cost_usd') or 0 for x in calls):.4f}")
    p = c.STATE / "progress.json"
    if p.exists():
        print("Latest progress:", json.dumps(c.read_json(p), indent=1)[:1200])
