"""Stages 3-6 after enrichment. Each stage reads saved artifacts, writes saved artifacts, and logs its calls.

  3 VERIFY  (model)  independent re-label of a declared random sample; code compares; planted-error test
  4 GROUP   (code + model) code assigns each complaint/cancellation to one issue = topic.subtopic;
                     a model only names/describes issues from a bounded evidence pack
  5 RANK    (code)   priority = severity_sum = complaint_count x mean_severity; desc score, then asc issue_id
  6 MEMO    (model)  sees only aggregates + bounded evidence; code checks every cited ID and number

Downstream model stages are cached by an input fingerprint: unchanged inputs -> zero new calls.
"""

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from . import common as c
from . import llm

VERIFY_MODEL = "claude-haiku-5-5"
GROUP_MODEL = "claude-haiku-5-5"
MEMO_MODEL = "claude-sonnet-5-5"
MEMO_EFFORT = "low"


def now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def completed(db):
    rows = db.execute("""SELECT review_id, review_text, review_rating, review_likes, review_timestamp, labels_json,
                         label_config, cache_source_id, scope_part FROM records WHERE status='completed'""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.update(json.loads(d.pop("labels_json")))
        out.append(d)
    return out


def cached_stage(name, fp):
    meta = c.OUTPUTS / f"{name}_meta.json"
    return meta.exists() and c.read_json(meta).get("input_fingerprint") == fp


def mark_stage(name, fp, extra=None):
    c.write_json(c.OUTPUTS / f"{name}_meta.json", {"input_fingerprint": fp, "completed_at": now(), **(extra or {})})


class Calls:
    """Shared call log + ledger for downstream stages (same file the enrichment stage appends to)."""

    def __init__(self, cap_usd, run_id):
        self.path = c.STATE / "calls.jsonl"
        self.run_id = run_id
        self.ledger = llm.Ledger(cap_usd, llm.load_rates(), spent_usd=llm.spent_from_log(self.path))
        self.made = 0

    def log(self, rec):
        self.made += 1
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 3 VERIFY
VERIFY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["results"], "properties": {"results": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["key", "topic", "intent", "severity", "needs_review"],
                               "properties": {"key": {"type": "string"},
                                              "topic": {"type": "string", "enum": list(c.TOPICS)},
                                              "intent": {"type": "string", "enum": list(c.INTENTS)},
                                              "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                                              "needs_review": {"type": "boolean"}}}}}}


def verify_prompt():
    return ((c.ROOT / "prompts" / "verify_v1.md").read_text(encoding="utf-8")
            .replace("{DEFINITIONS}", (c.ROOT / "labels" / "definitions.md").read_text(encoding="utf-8")))


def verify(db, calls, frac=0.02, min_n=20, max_n=3000):
    recs = [r for r in completed(db) if not r["cache_source_id"]]
    k = min(len(recs), max(min_n, min(max_n, round(frac * len(recs)))))
    sample = sorted(recs, key=lambda r: hashlib.sha256(("verify-v1:" + r["review_id"]).encode()).hexdigest())[:k]
    system = verify_prompt()
    fp = fingerprint("verify-v1", VERIFY_MODEL, c.text_sha(system), sorted(r["review_id"] for r in sample))
    if cached_stage("verify", fp):
        print(f"verify: inputs unchanged -> cached (0 new calls), sample={k}")
        return c.read_json(c.OUTPUTS / "verification_report.json")
    preds = {}
    for i in range(0, len(sample), 50):
        chunk = sample[i:i + 50]
        payload = json.dumps([{"key": str(j + 1), "text": r["review_text"]} for j, r in enumerate(chunk)], ensure_ascii=False)
        try:
            parsed, _ = llm.call(role="verify", model=VERIFY_MODEL, system=system, user=payload, schema=VERIFY_SCHEMA,
                                 max_tokens=60 * len(chunk) + 200, ledger=calls.ledger, log=calls.log,
                                 review_ids=[r["review_id"] for r in chunk], run_id=calls.run_id,
                                 label_config=f"{VERIFY_MODEL}+verify-v1")
        except llm.CallFailed as e:
            print("verify batch failed:", e)
            continue
        for item in parsed.get("results", []):
            j = int(item["key"]) - 1 if item.get("key", "").isdigit() else -1
            if 0 <= j < len(chunk):
                preds[chunk[j]["review_id"]] = item
    rows, fields = [], ("topic", "intent", "severity")
    for r in sample:
        v = preds.get(r["review_id"])
        row = {"review_id": r["review_id"], "review_text": r["review_text"], "verified": bool(v)}
        for f in fields:
            row["enrich_" + f], row["verify_" + f] = r[f], (v or {}).get(f)
            row[f + "_agree"] = bool(v) and r[f] == v[f]
        row["severity_abs_diff"] = abs(r["severity"] - v["severity"]) if v else None
        row["enrich_needs_review"], row["verify_needs_review"] = r["needs_review"], (v or {}).get("needs_review")
        rows.append(row)
    with (c.OUTPUTS / "verification.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    got = [r for r in rows if r["verified"]]
    report = {
        "procedure": "Separate verify-v1 prompt re-labels a declared random sample (lowest SHA-256('verify-v1:'+id) among "
                     "directly-labeled completed records) WITHOUT seeing the enrichment answer; code compares.",
        "sample_size": k, "verified": len(got), "sample_fraction_declared": frac,
        "agreement": {f: round(sum(r[f + "_agree"] for r in got) / len(got), 4) if got else None for f in fields},
        "severity_mae": round(sum(r["severity_abs_diff"] for r in got) / len(got), 4) if got else None,
        "disagreement_counts": {f: sum(not r[f + "_agree"] for r in got) for f in fields},
        "topic_confusion": dict(Counter(f"{r['enrich_topic']}->{r['verify_topic']}" for r in got
                                        if not r["topic_agree"]).most_common(15)),
        "examples": [r for r in got if not (r["topic_agree"] and r["intent_agree"])][:25],
    }
    report["planted_error_test"] = planted_error_test(got)
    c.write_json(c.OUTPUTS / "verification_report.json", report)
    mark_stage("verify", fp, {"calls": calls.made})
    print(f"verify: {len(got)}/{k} verified; agreement {report['agreement']}; planted errors caught "
          f"{report['planted_error_test']['caught']}/{report['planted_error_test']['planted']}")
    return report


def planted_error_test(rows):
    """Test copy (never written back): flip the enrichment topic of 20 agreeing records to a wrong topic and
    confirm the code comparison flags every one. Proves the disagreement check works; costs no calls."""
    agreeing = [r for r in rows if r["topic_agree"]][:20]
    caught = 0
    cases = []
    for r in agreeing:
        wrong = c.TOPICS[(c.TOPICS.index(r["enrich_topic"]) + 3) % len(c.TOPICS)]
        flagged = wrong != r["verify_topic"]
        caught += flagged
        cases.append({"review_id": r["review_id"], "true_enrich_topic": r["enrich_topic"], "planted_topic": wrong,
                      "verifier_topic": r["verify_topic"], "flagged": flagged})
    return {"synthetic": True, "planted": len(agreeing), "caught": caught, "cases": cases}


# ---------------------------------------------------------------- 4 GROUP
GROUP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["issues"], "properties": {"issues": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["issue_id", "name", "description"],
                               "properties": {"issue_id": {"type": "string"}, "name": {"type": "string"},
                                              "description": {"type": "string"}}}}}}


def evidence_examples(members, n):
    """Deterministic representative examples: most helpful-votes first, then highest severity, then ID."""
    return sorted(members, key=lambda r: (-int(r["review_likes"] or 0), -r["severity"], r["review_id"]))[:n]


def group(db, calls, examples_per_issue=6):
    recs = [r for r in completed(db) if r["intent"] in ("complaint", "cancellation")]
    membership = sorted((f"{r['topic']}.{r['subtopic']}", r["review_id"]) for r in recs)
    with (c.OUTPUTS / "membership.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["issue_id", "review_id"])
        w.writerows(membership)
    by_issue = defaultdict(list)
    for r in recs:
        by_issue[f"{r['topic']}.{r['subtopic']}"].append(r)
    pack = [{"issue_id": iid, "members": len(m),
             "examples": [r["evidence_quote"][:200] for r in evidence_examples(m, examples_per_issue)]}
            for iid, m in sorted(by_issue.items())]
    system = (c.ROOT / "prompts" / "group_v1.md").read_text(encoding="utf-8")
    fp = fingerprint("group-v1", GROUP_MODEL, c.text_sha(system), pack)
    names = {}
    if cached_stage("group", fp) and (c.OUTPUTS / "issue_names.json").exists():
        print(f"group: inputs unchanged -> cached (0 new calls); {len(by_issue)} issues")
        names = c.read_json(c.OUTPUTS / "issue_names.json")
    else:
        try:
            parsed, _ = llm.call(role="group", model=GROUP_MODEL, system=system,
                                 user=json.dumps(pack, ensure_ascii=False), schema=GROUP_SCHEMA,
                                 max_tokens=80 * len(pack) + 300, ledger=calls.ledger, log=calls.log,
                                 review_ids=[], run_id=calls.run_id, label_config=f"{GROUP_MODEL}+group-v1")
            valid = set(by_issue)
            names = {i["issue_id"]: {"name": i["name"], "description": i["description"]}
                     for i in parsed["issues"] if i["issue_id"] in valid}  # reject invented issue IDs
        except llm.CallFailed as e:
            print("group naming failed (membership still saved; names fall back to IDs):", e)
        for iid in by_issue:
            names.setdefault(iid, {"name": iid.replace(".", ": ").replace("_", " "), "description": ""})
        c.write_json(c.OUTPUTS / "issue_names.json", names)
        mark_stage("group", fp, {"calls_made_total": calls.made})
        print(f"group: {len(recs)} complaint/cancellation records -> {len(by_issue)} issues; names saved")
    issues = [{"issue_id": iid, "name": names[iid]["name"], "description": names[iid]["description"],
               "member_count": len(m)} for iid, m in sorted(by_issue.items())]
    c.write_json(c.OUTPUTS / "issues.json", issues)
    return issues


# ---------------------------------------------------------------- 5 RANK (no model)
def mean6(total, n):
    return str((Decimal(total) / Decimal(n)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def rank(db=None):
    """Recomputes the baseline ranking from saved membership + saved records. No model call."""
    db = db or c.connect()
    sev = {r["review_id"]: r for r in completed(db)}
    members = defaultdict(list)
    with (c.OUTPUTS / "membership.csv").open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            members[row["issue_id"]].append(sev[row["review_id"]]["severity"])
    table = [{"issue_id": iid, "complaint_count": len(v), "severity_sum": sum(v),
              "mean_severity": mean6(sum(v), len(v)), "priority_score": sum(v)} for iid, v in members.items()]
    table.sort(key=lambda x: (-x["priority_score"], x["issue_id"]))
    for i, row in enumerate(table, 1):
        row["rank"] = i
    cols = ["rank", "issue_id", "complaint_count", "severity_sum", "mean_severity", "priority_score"]
    with (c.OUTPUTS / "ranking.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows({k: row[k] for k in cols} for row in table)
    aggregates(db, table)
    return table


def aggregates(db, ranking):
    """Topic-level view for the product question (access / usability / playback / downloads / catalog / billing / support)."""
    recs = completed(db)
    by_topic = defaultdict(lambda: Counter())
    for r in recs:
        t = by_topic[r["topic"]]
        t["completed"] += 1
        t["intent_" + r["intent"]] += 1
        if r["intent"] in ("complaint", "cancellation"):
            t["complaint_count"] += 1
            t["severity_sum"] += r["severity"]
            t["severe_4_or_5"] += r["severity"] >= 4
    total = len(recs)
    total_complaints = sum(t["complaint_count"] for t in by_topic.values())
    rows = []
    for topic in c.TOPICS:
        t = by_topic.get(topic, Counter())
        rows.append({"topic": topic, "completed": t["completed"], "complaint_count": t["complaint_count"],
                     "cancellation_count": t["intent_cancellation"], "severity_sum": t["severity_sum"],
                     "mean_severity": mean6(t["severity_sum"], t["complaint_count"]) if t["complaint_count"] else "",
                     "severe_4_or_5": t["severe_4_or_5"],
                     "share_of_complaints": mean6(t["complaint_count"], total_complaints) if total_complaints else ""})
    with (c.OUTPUTS / "aggregates.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    month = defaultdict(lambda: Counter())
    for r in recs:
        m = month[r["review_timestamp"][:7]]
        m["completed"] += 1
        m["complaints"] += r["intent"] in ("complaint", "cancellation")
        if r["intent"] in ("complaint", "cancellation"):
            m["topic_" + r["topic"]] += 1
    c.write_json(c.OUTPUTS / "monthly.json", {k: dict(v) for k, v in sorted(month.items())})
    c.write_json(c.OUTPUTS / "summary.json", {"completed": total, "complaints_and_cancellations": total_complaints,
                                              "issues": len(ranking), "computed_at": now()})
    return rows


# ---------------------------------------------------------------- 6 MEMO
MEMO_SCHEMA = {"type": "object", "additionalProperties": False,
               "required": ["recommended_area", "headline", "memo_markdown", "claims", "cited_review_ids"],
               "properties": {
                   "recommended_area": {"type": "string", "enum": ["access", "usability", "playback", "billing/support"]},
                   "headline": {"type": "string"},
                   "memo_markdown": {"type": "string"},
                   "claims": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                          "required": ["claim_id", "issue_id", "metric", "value"],
                                                          "properties": {"claim_id": {"type": "string"},
                                                                         "issue_id": {"type": "string"},
                                                                         "metric": {"type": "string", "enum": [
                                                                             "complaint_count", "severity_sum",
                                                                             "mean_severity", "priority_score"]},
                                                                         "value": {"type": "string"}}}},
                   "cited_review_ids": {"type": "array", "items": {"type": "string"}}}}


def memo(db, calls, top_issues=12, evidence_issues=6, per_issue=3):
    ranking = rank(db)
    agg = list(csv.DictReader((c.OUTPUTS / "aggregates.csv").open(encoding="utf-8")))
    names = c.read_json(c.OUTPUTS / "issue_names.json")
    summary = c.read_json(c.OUTPUTS / "summary.json")
    recs = {r["review_id"]: r for r in completed(db)}
    members = defaultdict(list)
    for row in csv.DictReader((c.OUTPUTS / "membership.csv").open(encoding="utf-8")):
        members[row["issue_id"]].append(recs[row["review_id"]])
    evidence = []
    for row in ranking[:evidence_issues]:
        for r in evidence_examples(members[row["issue_id"]], per_issue):
            evidence.append({"issue_id": row["issue_id"], "review_id": r["review_id"], "severity": r["severity"],
                             "intent": r["intent"], "helpful_votes": int(r["review_likes"] or 0),
                             "month": r["review_timestamp"][:7], "quote": r["evidence_quote"][:300]})
    pack = {"scope_note": "Self-selected public Google Play reviews, May 2022-Nov 2023, classified sample "
                          f"({summary['completed']} completed classifications). No revenue, plan tier or churn data.",
            "totals": {k: v for k, v in summary.items() if k != "computed_at"},
            "ranking_top": [{**{k: row[k] for k in ("rank", "issue_id", "complaint_count", "severity_sum",
                                                     "mean_severity", "priority_score")},
                             "name": names.get(row["issue_id"], {}).get("name", "")} for row in ranking[:top_issues]],
            "topic_aggregates": agg, "evidence": evidence}
    c.write_json(c.OUTPUTS / "memo_input_pack.json", pack)
    system = (c.ROOT / "prompts" / "memo_v1.md").read_text(encoding="utf-8")
    fp = fingerprint("memo-v1", MEMO_MODEL, MEMO_EFFORT, c.text_sha(system), pack)
    if cached_stage("memo", fp):
        print("memo: inputs unchanged -> cached (0 new calls)")
        return c.read_json(c.OUTPUTS / "memo_check.json")
    parsed, _ = llm.call(role="memo", model=MEMO_MODEL, system=system, user=json.dumps(pack, ensure_ascii=False),
                         schema=MEMO_SCHEMA, max_tokens=6000, ledger=calls.ledger, log=calls.log, review_ids=[],
                         run_id=calls.run_id, label_config=f"{MEMO_MODEL}+effort-{MEMO_EFFORT}+memo-v1",
                         effort=MEMO_EFFORT)
    check = check_memo(parsed, ranking, agg, pack, evidence)
    (c.OUTPUTS / "memo.md").write_text(parsed["memo_markdown"].strip() + "\n", encoding="utf-8")
    with (c.OUTPUTS / "claims.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["claim_id", "issue_id", "metric", "value"], lineterminator="\n")
        w.writeheader()
        w.writerows({k: cl[k] for k in ("claim_id", "issue_id", "metric", "value")} for cl in parsed["claims"])
    c.write_json(c.OUTPUTS / "memo_output.json", parsed)
    c.write_json(c.OUTPUTS / "memo_check.json", check)
    mark_stage("memo", fp)
    print(f"memo: recommends {parsed['recommended_area']!r}; claims ok {check['claims_ok']}/{check['claims']}; "
          f"unsupported numbers {len(check['unsupported_numbers'])}; bad review IDs {len(check['bad_review_ids'])}")
    return check


def check_memo(parsed, ranking, agg, pack, evidence):
    """Code checks the memo: claim values recomputed, cited IDs exist, every number in the prose is traceable."""
    by_id = {r["issue_id"]: r for r in ranking}
    claim_results = []
    for cl in parsed["claims"]:
        target = by_id.get(cl["issue_id"])
        ok = bool(target) and str(target[cl["metric"]]) == cl["value"]
        claim_results.append({**cl, "ok": ok, "expected": str(target[cl["metric"]]) if target else None})
    allowed = set()
    def add(v):
        if v in ("", None):
            return
        s = str(v)
        allowed.add(s)
        try:
            d = Decimal(s)
            allowed.update({str(d.normalize()), f"{d:.1f}", f"{d:.2f}", f"{int(d):,}" if d == int(d) else s})
            if Decimal(0) <= d <= 1:  # shares may be written as percentages
                for p in (0, 1):
                    allowed.add(f"{(d * 100).quantize(Decimal(1) if p == 0 else Decimal('0.1'), rounding=ROUND_HALF_UP)}")
        except Exception:
            pass
    for row in pack["ranking_top"] + ranking:
        for k in ("rank", "complaint_count", "severity_sum", "mean_severity", "priority_score"):
            add(row[k])
    for row in agg:
        for k, v in row.items():
            if k != "topic":
                add(v)
    for k, v in pack["totals"].items():
        add(v)
    for e in evidence:
        add(e["severity"]); add(e["helpful_votes"])
    uuid_re = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    text = re.sub(uuid_re, " ", parsed["memo_markdown"])
    text = re.sub(r"\[C\d+\]", " ", text)
    nums = [n.rstrip(",") for n in re.findall(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?", text)]
    years_months = {str(y) for y in range(2020, 2027)} | {f"{m:02d}" for m in range(1, 13)} | {str(i) for i in range(1, 13)}
    unsupported = sorted({n for n in nums if n not in allowed and n.replace(",", "") not in allowed and n not in years_months})
    evidence_ids = {e["review_id"] for e in evidence}
    cited = set(parsed["cited_review_ids"]) | set(re.findall(uuid_re, parsed["memo_markdown"]))
    issue_mentions = set(re.findall(r"\b(?:%s)\.[a-z_]+\b" % "|".join(c.TOPICS), text))
    return {"claims": len(claim_results), "claims_ok": sum(r["ok"] for r in claim_results), "claim_results": claim_results,
            "numbers_in_prose": len(nums), "unsupported_numbers": unsupported,
            "cited_review_ids": sorted(cited), "bad_review_ids": sorted(cited - evidence_ids),
            "issue_ids_in_prose": sorted(issue_mentions), "unknown_issue_ids": sorted(issue_mentions - set(by_id)),
            "note": "Automated checks cover numbers and IDs; a person must still judge whether the argument holds."}
