"""Compare the pipeline's labels on the golden 50 with Brian's hand labels (evals/golden_human_labels.json).
Offline. The human labels were never model inputs; the model labeled these 50 inside the main run.

  python evals/golden_eval.py   -> evals/golden_eval.json, evals/golden_eval.md
Declared before evaluation: sentiment agreement tolerance ±0.3; severity reported as exact agreement and MAE.
"""

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pipeline import common as c  # noqa: E402

SENT_TOL = 0.3


def accepted(human, field):
    """Primary human label plus any 'also acceptable' alternatives the labeler wrote (e.g. 'topic=billing; severity=3')."""
    vals = {human[field]}
    for part in (human.get("also_acceptable") or "").replace(",", ";").split(";"):
        if "=" in part:
            k, v = (x.strip() for x in part.split("=", 1))
            if k == field:
                vals.add(int(v) if field == "severity" and v.isdigit() else v)
    return vals


def main():
    human = c.read_json(ROOT / "evals" / "golden_human_labels.json")
    db = c.connect()
    rows = db.execute("SELECT review_id, review_text, status, labels_json, reason FROM records WHERE scope_part='golden'").fetchall()
    cases, conf = [], {f: Counter() for f in ("topic", "intent", "severity")}
    for r in rows:
        h = human.get(r["review_id"])
        if not h or not h.get("topic"):
            continue
        p = json.loads(r["labels_json"]) if r["labels_json"] else None
        case = {"review_id": r["review_id"], "text": r["review_text"], "human": {k: h.get(k) for k in (
            "topic", "intent", "severity", "sentiment", "evidence_quote", "entities", "needs_review")},
                "also_acceptable": h.get("also_acceptable", ""), "human_notes": h.get("notes", ""),
                "prediction": {k: p[k] for k in ("topic", "subtopic", "intent", "severity", "sentiment", "evidence_quote",
                                                 "entities", "needs_review")} if p else None,
                "status": r["status"], "reason": r["reason"]}
        if p:
            for f in ("topic", "intent", "severity"):
                case[f + "_ok"] = p[f] in accepted(h, f)
                case[f + "_strict_ok"] = p[f] == h[f]
                conf[f][f"{h[f]} -> {p[f]}"] += 1
            case["severity_abs_err"] = abs(p["severity"] - h["severity"])
            case["sentiment_abs_err"] = round(abs(p["sentiment"] - float(h["sentiment"])), 2)
            case["sentiment_ok"] = case["sentiment_abs_err"] <= SENT_TOL
            case["quote_is_substring"] = p["evidence_quote"] in r["review_text"]
            case["unsupported_entities"] = [e for e in p["entities"] if e.lower() not in r["review_text"].lower()]
        else:
            for f in ("topic", "intent", "severity", "sentiment"):
                case[f + "_ok"] = False  # missing/quarantined predictions count as incorrect
        cases.append(case)
    n = len(cases)
    got = [x for x in cases if x["prediction"]]
    def rate(key, pool=cases):
        return round(sum(bool(x.get(key)) for x in pool) / len(pool), 4) if pool else None
    nr_pred = [x for x in got if x["prediction"]["needs_review"]]
    nr_true = [x for x in got if x["human"]["needs_review"]]
    per_topic = {}
    for t in c.TOPICS:
        sup = [x for x in got if x["human"]["topic"] == t]
        pred = [x for x in got if x["prediction"]["topic"] == t]
        tp = sum(x["human"]["topic"] == t and x["prediction"]["topic"] == t for x in got)
        if sup or pred:
            per_topic[t] = {"human_count": len(sup), "predicted_count": len(pred), "both": tp}
    report = {
        "labeled_cases": n, "predictions_available": len(got), "missing_or_quarantined": n - len(got),
        "agreement_with_alternatives": {"topic": rate("topic_ok"), "intent": rate("intent_ok"), "severity_exact": rate("severity_ok")},
        "agreement_strict_primary_label": {"topic": rate("topic_strict_ok"), "intent": rate("intent_strict_ok"),
                                           "severity_exact": rate("severity_strict_ok")},
        "severity_mae": round(sum(x["severity_abs_err"] for x in got) / len(got), 4) if got else None,
        "sentiment_within_tolerance": rate("sentiment_ok"), "sentiment_tolerance": SENT_TOL,
        "sentiment_mae": round(sum(x["sentiment_abs_err"] for x in got) / len(got), 4) if got else None,
        "quote_exact_substring_rate": rate("quote_is_substring", got),
        "cases_with_unsupported_entities": sum(bool(x["unsupported_entities"]) for x in got),
        "ambiguous_cases_marked_by_human": sum(bool(x["human"]["needs_review"]) or bool(x["also_acceptable"]) for x in cases),
        "needs_review_as_prediction": {"predicted_true": len(nr_pred), "human_true": len(nr_true),
                                       "both_true": sum(x["human"]["needs_review"] for x in nr_pred)},
        "per_topic_counts": per_topic,
        "confusion": {f: dict(v.most_common()) for f, v in conf.items()},
        "disagreements": [x for x in cases if not (x.get("topic_ok") and x.get("intent_ok") and x.get("severity_ok"))],
        "note": "50 cases = small diagnostic sample, not a precise population accuracy estimate.",
    }
    adj_path = ROOT / "evals" / "golden_adjudication.json"
    if adj_path.exists():
        adj = c.read_json(adj_path)
        verdicts = Counter(adj.get(x["review_id"], {}).get("verdict", "undecided") for x in report["disagreements"])
        resolved_ok = sum(1 for x in cases if x.get("topic_ok") and x.get("intent_ok") and x.get("severity_ok")) + \
            verdicts.get("human_error", 0) + verdicts.get("both_ok", 0)
        report["post_hoc_adjudication"] = {
            "disclosure": "Made by the same labeler AFTER seeing model predictions; reported separately from the blind result.",
            "verdicts": dict(verdicts),
            "all_three_fields_acceptable_after_adjudication": round(resolved_ok / n, 4) if n else None,
            "blind_all_three_fields_agree": round(sum(1 for x in cases if x.get("topic_ok") and x.get("intent_ok")
                                                      and x.get("severity_ok")) / n, 4) if n else None}
        for x in report["disagreements"]:
            x["adjudication"] = adj.get(x["review_id"])
    c.write_json(ROOT / "evals" / "golden_eval.json", {**report, "cases": cases})
    L = ["# Golden-50 evaluation\n", f"Labeled by a person: {n}. Predictions available: {len(got)}.\n",
         "| Field | Agreement (incl. human-listed alternatives) | Strict (primary label only) |", "|---|---|---|"]
    for f, k in (("topic", "topic"), ("intent", "intent"), ("severity exact", "severity_exact")):
        L.append(f"| {f} | {report['agreement_with_alternatives'][k]} | {report['agreement_strict_primary_label'][k]} |")
    L += [f"\nSeverity MAE {report['severity_mae']}; sentiment within ±{SENT_TOL}: {report['sentiment_within_tolerance']} "
          f"(MAE {report['sentiment_mae']}); quotes exact substrings: {report['quote_exact_substring_rate']}; "
          f"cases with entities not in text: {report['cases_with_unsupported_entities']}.\n",
          *( [f"Post-hoc adjudication (labeler, after seeing predictions): {report['post_hoc_adjudication']['verdicts']}; "
              f"all-three-fields acceptable {report['post_hoc_adjudication']['all_three_fields_acceptable_after_adjudication']} "
              f"vs blind {report['post_hoc_adjudication']['blind_all_three_fields_agree']}.\n"] if "post_hoc_adjudication" in report else []),
          "## Disagreements\n", "| Review | Human (topic/intent/sev) | Model | Adjudication | Text |", "|---|---|---|---|---|"]
    for x in report["disagreements"]:
        h, p = x["human"], x["prediction"] or {}
        L.append(f"| `{x['review_id'][:8]}` | {h['topic']}/{h['intent']}/{h['severity']} | "
                 f"{p.get('topic')}/{p.get('intent')}/{p.get('severity')} | {(x.get('adjudication') or {}).get('verdict', '')} | "
                 f"{x['text'][:90].replace('|', '/').replace(chr(10), ' ')} |")
    (ROOT / "evals" / "golden_eval.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("labeled_cases", "agreement_with_alternatives", "severity_mae",
                                              "sentiment_within_tolerance")}, indent=1))


if __name__ == "__main__":
    main()
