"""Load the pipeline's saved outputs into the dashboard database (Neon Postgres via DATABASE_URL, or SQLite locally).
Offline with respect to models: it only copies saved results. Re-running replaces the tables' contents."""

import csv
import json
import os
import sys
from datetime import datetime, timezone

from . import common as c


def load(database_url=None):
    from dotenv import load_dotenv
    load_dotenv(c.ROOT / ".env")
    if database_url:
        os.environ["DATABASE_URL"] = database_url
    sys.path.insert(0, str(c.ROOT / "dashboard" / "api"))
    import _db as dash  # dashboard/api/_db.py (same adapter the backend uses)
    dash.URL = os.environ.get("DATABASE_URL", "")
    conn = dash.connect()
    pg = dash.is_pg(conn)
    ph = "%s" if pg else "?"
    cur = conn.cursor()
    for stmt in dash.SCHEMA:
        cur.execute(stmt)
    for t in ("reviews", "issues", "topic_aggregates", "monthly", "recommendation", "claims", "metrics"):
        cur.execute(f"DELETE FROM {t}")

    member = {}
    for row in csv.DictReader((c.OUTPUTS / "membership.csv").open(encoding="utf-8")):
        member[row["review_id"]] = row["issue_id"]
    src = c.connect()
    cols = ["review_id", "scope_part", "status", "reason", "review_text", "review_rating", "review_likes",
            "review_timestamp", "app_version", "topic", "subtopic", "intent", "severity", "sentiment", "evidence_quote",
            "needs_review", "entities", "cache_source_id", "label_config", "issue_id"]
    rows = []
    for r in src.execute("SELECT * FROM records"):
        l = json.loads(r["labels_json"]) if r["labels_json"] else {}
        rows.append((r["review_id"], r["scope_part"], r["status"], r["reason"], r["review_text"], r["review_rating"],
                     int(r["review_likes"] or 0), r["review_timestamp"], r["app_version"], l.get("topic"), l.get("subtopic"),
                     l.get("intent"), l.get("severity"), l.get("sentiment"), l.get("evidence_quote"), l.get("needs_review"),
                     json.dumps(l.get("entities", []), ensure_ascii=False), r["cache_source_id"], r["label_config"],
                     member.get(r["review_id"])))
    if pg:
        with cur.copy(f"COPY reviews ({', '.join(cols)}) FROM STDIN") as cp:
            for row in rows:
                cp.write_row(row)
    else:
        cur.executemany(f"INSERT INTO reviews VALUES ({', '.join([ph] * len(cols))})", rows)

    names = c.read_json(c.OUTPUTS / "issue_names.json")
    for r in csv.DictReader((c.OUTPUTS / "ranking.csv").open(encoding="utf-8")):
        n = names.get(r["issue_id"], {})
        cur.execute(f"INSERT INTO issues VALUES ({', '.join([ph] * 9)})",
                    (r["issue_id"], int(r["rank"]), n.get("name", r["issue_id"]), n.get("description", ""),
                     r["issue_id"].split(".")[0], int(r["complaint_count"]), int(r["severity_sum"]), r["mean_severity"],
                     int(r["priority_score"])))
    for r in csv.DictReader((c.OUTPUTS / "aggregates.csv").open(encoding="utf-8")):
        cur.execute(f"INSERT INTO topic_aggregates VALUES ({', '.join([ph] * 8)})",
                    (r["topic"], int(r["completed"]), int(r["complaint_count"]), int(r["cancellation_count"]),
                     int(r["severity_sum"]), r["mean_severity"], int(r["severe_4_or_5"]), r["share_of_complaints"]))
    for month, m in c.read_json(c.OUTPUTS / "monthly.json").items():
        topics = {k[6:]: v for k, v in m.items() if k.startswith("topic_")}
        cur.execute(f"INSERT INTO monthly VALUES ({ph}, {ph}, {ph}, {ph})",
                    (month, m.get("completed", 0), m.get("complaints", 0), json.dumps(topics)))
    memo = c.read_json(c.OUTPUTS / "memo_output.json")
    check = c.read_json(c.OUTPUTS / "memo_check.json")
    pack = c.read_json(c.OUTPUTS / "memo_input_pack.json")
    calls = [json.loads(l) for l in (c.STATE / "calls.jsonl").open(encoding="utf-8")]
    memo_call = next((x for x in reversed(calls) if x.get("role") == "memo" and x.get("outcome") == "succeeded"), {})
    cur.execute(f"INSERT INTO recommendation VALUES ({', '.join([ph] * 9)})",
                (1, memo["recommended_area"], memo["headline"], memo["memo_markdown"], json.dumps(memo["claims"]),
                 json.dumps(check), memo_call.get("label_config", ""), json.dumps(pack["evidence"]),
                 memo_call.get("started_at", "")))
    for cl in memo["claims"]:
        cur.execute(f"INSERT INTO claims VALUES ({ph}, {ph}, {ph}, {ph})",
                    (cl["claim_id"], cl["issue_id"], cl["metric"], cl["value"]))

    counts = dict(src.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())
    enrich_calls = [x for x in calls if x.get("role") == "enrich"]
    ingestion = c.read_json(c.OUTPUTS / "ingestion_report.json")
    verify = c.read_json(c.OUTPUTS / "verification_report.json") if (c.OUTPUTS / "verification_report.json").exists() else {}
    golden = c.read_json(c.ROOT / "evals" / "golden_eval.json") if (c.ROOT / "evals" / "golden_eval.json").exists() else {}
    m = {
        "rows_ingested": ingestion["profile"]["counts"]["records"],
        "scope_rows": sum(counts.values()), "completed": counts.get("completed", 0),
        "quarantined": counts.get("quarantined", 0), "pending": counts.get("pending", 0),
        "cache_reuses": src.execute("SELECT COUNT(*) FROM records WHERE cache_source_id IS NOT NULL").fetchone()[0],
        "complaints": c.read_json(c.OUTPUTS / "summary.json")["complaints_and_cancellations"],
        "issues": len(names),
        "api_spend_usd": round(sum(x.get("cost_usd") or 0 for x in calls), 4),
        "model_calls": len(calls), "failed_calls": sum(x.get("outcome") == "failed" for x in calls),
        "enrichment_calls": len(enrich_calls),
        "models": sorted({x["model"] for x in calls if x.get("model")}),
        "verifier_agreement": verify.get("agreement"), "verifier_sample": verify.get("sample_size"),
        "golden_agreement": golden.get("agreement_with_alternatives"), "golden_cases": golden.get("labeled_cases"),
        "loaded_at": datetime.now(timezone.utc).isoformat(),
        "repo_url": os.environ.get("REPO_URL", "https://github.com/BuildingBrian/spotify-review-pipeline"),
    }
    for k, v in m.items():
        cur.execute(f"INSERT INTO metrics VALUES ({ph}, {ph})", (k, json.dumps(v)))
    if not pg:
        conn.commit()
    print(f"Loaded into {'Postgres' if pg else 'SQLite'}: {len(rows):,} reviews, {len(names)} issues, "
          f"{len(memo['claims'])} claims, {len(m)} metrics")
