"""Stage 1 - PREPARE (code only, no model).

Reads every row of the input CSV, profiles it, quarantines empty texts, and selects the
classification scope by extending the course's own deterministic sample order:
lowest SHA-256(seed:review_id) among unique, nonempty, valid-rating records.

  ranks 0..49            golden 50 (hand-labeled by a person; labels never sent to a model)
  ranks 50..core-1       core (contains cost_100, checkpoint_500, analysis_10000 exactly)
  ranks core..core+ext-1 extension
  + every empty-text row, quarantined with reason empty_review_text

Outputs: outputs/ingestion_report.json, outputs/data_manifest.json, outputs/scope.json,
state/analysis_scope.csv (the declared analysis file), grading/ingestion.json (course helper),
and one 'pending' or 'quarantined' row per scoped review in state/pipeline.sqlite.
"""

import heapq
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone

from . import common as c

GOLDEN = 50


def scan(path, keep):
    """One pass over the file: full profile + the `keep` lowest-ranked eligible rows."""
    counts = Counter(records=0, duplicate_review_ids=0, empty_review_text=0, missing_app_version=0)
    months, ratings, text_counts = Counter(), Counter(), Counter()
    ids, heap, empties = set(), [], []
    first = last = None
    for row in c.csv_rows(path):
        rid, text = row["review_id"], row["review_text"]
        counts["records"] += 1
        duplicate = rid in ids
        counts["duplicate_review_ids"] += duplicate
        ids.add(rid)
        empty = not text.strip()
        counts["empty_review_text"] += empty
        counts["missing_app_version"] += not row["app_version"].strip()
        months[row["review_timestamp"][:7]] += 1
        ratings[row["review_rating"]] += 1
        ts = row["review_timestamp"]
        first = min(first, ts) if first else ts
        last = max(last, ts) if last else ts
        if empty:
            empties.append(row)
            continue
        text_counts[c.text_sha(text)] += 1
        if duplicate or not rid or row["review_rating"] not in {"1", "2", "3", "4", "5"}:
            continue
        rank = c.course_rank(rid)
        entry = (-rank, rid, row)
        if len(heap) < keep:
            heapq.heappush(heap, entry)
        elif rank < -heap[0][0]:
            heapq.heapreplace(heap, entry)
    selected = [e[2] for e in sorted(heap, key=lambda e: (-e[0], e[1]))]
    profile = {
        "counts": dict(counts),
        "distinct_nonempty_texts": len(text_counts),
        "nonempty_rows_sharing_text_with_another_row": sum(n for n in text_counts.values() if n > 1),
        "reviews_by_month": dict(sorted(months.items())),
        "reviews_by_rating": dict(sorted(ratings.items())),
        "first_review": first,
        "last_review": last,
    }
    return profile, selected, empties


def part_of(rank, core_end):
    if rank < GOLDEN:
        return "golden"
    return "core" if rank < core_end else "extension"


def verify_course_nesting(selected):
    """The course's golden/dev files must be exactly our first ranks; proves we replicated the method."""
    ids = [r["review_id"] for r in selected]
    checks = {}
    for name, start in (("golden_50_to_label.csv", 0), ("analysis_10000.csv", GOLDEN),
                        ("checkpoint_500.csv", GOLDEN), ("cost_100.csv", GOLDEN)):
        with (c.DATASET / name).open(encoding="utf-8", newline="") as f:
            import csv as _csv
            theirs = [row["review_id"] for row in _csv.DictReader(f)]
        checks[name] = {"rows": len(theirs), "matches_our_ranks": ids[start:start + len(theirs)] == theirs,
                        "our_rank_range": [start, start + len(theirs) - 1]}
    return checks


def run(input_csv=c.FULL_CSV, core=100_000, extension=50_000):
    started = datetime.now(timezone.utc)
    keep = core + extension
    print(f"Reading every row of {input_csv} ...", flush=True)
    profile, selected, empties = scan(input_csv, keep)
    if keep >= 10**9:  # --all: every eligible row of an arbitrary input file
        keep = len(selected)
    elif len(selected) < keep:
        raise SystemExit(f"Only {len(selected)} eligible rows; asked for {keep}.")
    nesting = verify_course_nesting(selected) if input_csv == c.FULL_CSV else {}

    # Declared analysis file = scoped rows (sample order) + empty-text rows.
    scope_rows = selected + empties
    analysis_csv = c.STATE / "analysis_scope.csv"
    c.write_csv(analysis_csv, scope_rows)

    db = c.connect()
    now = started.isoformat()
    with db:  # one transaction; INSERT OR IGNORE never touches work already saved
        for rank, row in enumerate(scope_rows):
            empty = rank >= keep
            db.execute(
                """INSERT OR IGNORE INTO records (review_id, scope_rank, scope_part, source_sha256, text_sha256,
                   review_text, review_rating, review_likes, app_version, review_timestamp,
                   status, reason, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row["review_id"], rank, "empty" if empty else part_of(rank, core), c.row_sha(row),
                 c.text_sha(row["review_text"]), row["review_text"], row["review_rating"], row["review_likes"],
                 row["app_version"], row["review_timestamp"],
                 "quarantined" if empty else "pending", "empty_review_text" if empty else None, now))
    status = dict(db.execute("SELECT status, COUNT(*) FROM records GROUP BY status").fetchall())
    parts = dict(db.execute("SELECT scope_part, COUNT(*) FROM records GROUP BY scope_part").fetchall())
    scope_texts = db.execute("SELECT COUNT(DISTINCT text_sha256) FROM records WHERE scope_part != 'empty'").fetchone()[0]
    scope_months = dict(db.execute(
        "SELECT substr(review_timestamp,1,7), COUNT(*) FROM records WHERE scope_part != 'empty' GROUP BY 1 ORDER BY 1"
    ).fetchall())

    # The grading export's ingestion.json must come from the course helper itself.
    helper_out = (c.ROOT / "grading" if input_csv == c.FULL_CSV else c.OUTPUTS) / "ingestion.json"
    helper_out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-I", str(c.CHECKER), "profile", "--full", str(input_csv),
                    "--out", str(helper_out)], check=True)
    helper = c.read_json(helper_out)

    expected = {"records": 660622, "duplicate_review_ids": 0, "empty_review_text": 13,
                "missing_app_version": 159701, "distinct_nonempty_texts": 484189}
    observed = {**profile["counts"], "distinct_nonempty_texts": profile["distinct_nonempty_texts"]}
    report = {
        "input": {"path": str(input_csv.relative_to(c.ROOT)) if input_csv.is_relative_to(c.ROOT) else str(input_csv),
                  "sha256": c.file_sha(input_csv), "bytes": input_csv.stat().st_size},
        "profile": profile,
        "brief_expected_vs_observed": {k: {"expected": v, "observed": observed.get(k), "match": observed.get(k) == v}
                                       for k, v in expected.items()} if input_csv == c.FULL_CSV else {},
        "course_helper_profile_counts_match": helper["counts"] == profile["counts"],
        "every_row_accounted_for": {
            "rows_read": profile["counts"]["records"],
            "in_classification_scope": keep,
            "quarantined_empty_text": len(empties),
            "outside_declared_scope": profile["counts"]["records"] - keep - len(empties),
        },
        "scope": {"method": "Extend the course sample order: lowest SHA-256(seed + ':' + review_id) among unique "
                            "nonempty valid-rating records; take the first core+extension; add all empty-text rows.",
                  "seed": c.COURSE_SEED, "golden": GOLDEN, "core_end_rank": core, "extension": extension,
                  "parts": parts, "distinct_texts_in_scope": scope_texts, "scope_by_month": scope_months,
                  "course_file_nesting": nesting},
        "state_status_counts": status,
        "started_at": now,
        "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }
    c.write_json(c.OUTPUTS / "ingestion_report.json", report)
    c.write_json(c.OUTPUTS / "scope.json", {
        "analysis_csv": "state/analysis_scope.csv (regenerate with: python -m pipeline prepare)",
        "analysis_sha256": c.file_sha(analysis_csv), "analysis_count": len(scope_rows),
        "nonempty_in_scope": keep, "empty_quarantined": len(empties), "parts": parts, "seed": c.COURSE_SEED})
    c.write_json(c.OUTPUTS / "data_manifest.json", {
        "source": {"file": input_csv.name, "sha256": report["input"]["sha256"], "bytes": report["input"]["bytes"],
                   "dataset_link": "https://drive.google.com/file/d/1P0rUoAS_wVjp3BYKqXMEyD4u0uJP1Bvf/view",
                   "origin": "BwandoWando, 3.4 Million Spotify Google Store Reviews v2 (Kaggle, CC0)"},
        "analysis_scope": {"sha256": c.file_sha(analysis_csv), "rows": len(scope_rows)},
        "prepared_at": now})
    return report


def summary(report):
    p, acc = report["profile"]["counts"], report["every_row_accounted_for"]
    print(f"\nRows read: {p['records']:,}  (empty text {p['empty_review_text']}, missing app_version "
          f"{p['missing_app_version']:,}, duplicate IDs {p['duplicate_review_ids']})")
    print(f"Distinct nonempty texts: {report['profile']['distinct_nonempty_texts']:,}")
    for k, v in report["brief_expected_vs_observed"].items():
        print(f"  {'OK ' if v['match'] else 'XX '} {k}: expected {v['expected']:,}, observed {v['observed']:,}")
    print(f"Course helper profile matches: {report['course_helper_profile_counts_match']}")
    for name, chk in report["scope"]["course_file_nesting"].items():
        print(f"  {'OK ' if chk['matches_our_ranks'] else 'XX '} {name} = our ranks {chk['our_rank_range']}")
    print(f"Scope: {acc['in_classification_scope']:,} to classify + {acc['quarantined_empty_text']} quarantined; "
          f"{acc['outside_declared_scope']:,} outside declared scope")
    print(f"Parts: {report['scope']['parts']}  | state: {report['state_status_counts']}  | {report['seconds']}s")
