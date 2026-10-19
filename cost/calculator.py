"""100-review cost and runtime calculator (COST_CALCULATOR.md).

DEFAULT = OFFLINE REPLAY: recomputes every number from saved usage (cost/pilot_calls.jsonl) x editable rates
(cost/rates.csv) and assumptions (cost/assumptions.json). No API key, no network, no model calls.

  python cost/calculator.py                     # offline replay -> cost/report.md, cost/usage.csv
  python cost/calculator.py --rates-mult 2      # what-if: every API rate doubled (measured time unchanged)
  python cost/calculator.py --records 660622    # what-if: different projected volume (pilot results unchanged)

Paid pilot execution is a separate, explicit command (see cost/README.md):
  python -m pipeline --workdir cost/pilot run --paid --input data/dataset/cost_100.csv --all --workers 1 --verify-frac 0.2
"""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ITEMS = ("input_tokens", "cache_write_input_tokens", "cache_read_input_tokens", "output_tokens")


def load_rates(path, mult):
    rates, meta = {}, {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            rates[(r["model"], r["item"])] = float(r["usd_per_unit"]) * mult
            meta[(r["model"], r["item"])] = r
    return rates, meta


def money(x):
    return f"${x:,.4f}" if x < 10 else f"${x:,.2f}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rates", type=Path, default=HERE / "rates.csv")
    ap.add_argument("--rates-mult", type=float, default=1.0, help="Multiply every API rate (what-if)")
    ap.add_argument("--assumptions", type=Path, default=HERE / "assumptions.json")
    ap.add_argument("--records", type=int, help="Override projected scope rows (what-if)")
    ap.add_argument("--out", type=Path, default=HERE / "report.md")
    a = ap.parse_args()

    A = json.loads(a.assumptions.read_text(encoding="utf-8"))
    if a.records:
        A["projection"]["scope_rows"] = a.records
    rates, rate_meta = load_rates(a.rates, a.rates_mult)
    calls = [json.loads(l) for l in (HERE / "pilot_calls.jsonl").open(encoding="utf-8")]
    runs = json.loads((HERE / "pilot_runs.json").read_text(encoding="utf-8"))
    records = [json.loads(l) for l in (HERE / "pilot_records.jsonl").open(encoding="utf-8")]

    # ---- measured: cost per call = sum over mutually exclusive billing items of units x rate
    usage_rows, by_stage = [], defaultdict(lambda: defaultdict(float))
    for cl in calls:
        cost = sum((cl.get(i) or 0) * rates[(cl["model"], i)] for i in ITEMS) if cl["outcome"] == "succeeded" or cl.get("input_tokens") else 0.0
        usage_rows.append({"run": cl["pilot_run"], "request_id": cl["request_id"], "role": cl["role"], "model": cl["model"],
                           "outcome": cl["outcome"], "attempt": cl.get("attempt", 1), "reviews": len(cl["review_ids"]),
                           **{i: cl.get(i) or 0 for i in ITEMS}, "latency_s": cl.get("latency_s"), "cost_usd": round(cost, 8)})
        s = by_stage[(cl["pilot_run"], cl["role"])]
        s["calls"] += 1
        s["failed"] += cl["outcome"] != "succeeded"
        s["reviews_sent"] += len(cl["review_ids"])
        for i in ITEMS:
            s[i] += cl.get(i) or 0
        s["cost"] += cost
        s["latency"] += cl.get("latency_s") or 0
    with (HERE / "usage.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(usage_rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(usage_rows)

    cold = runs["cold"]
    warm = runs["warm"]
    cold_cost = sum(v["cost"] for (run, _), v in by_stage.items() if run == "cold")
    warm_cost = sum(v["cost"] for (run, _), v in by_stage.items() if run == "warm")
    enr = by_stage[("cold", "enrich")]
    completed = sum(r["status"] == "completed" for r in records)
    failed = len(records) - completed
    n_inputs = len(records)

    # ---- projection: per-stage unit economics from the pilot, scaled by each stage's own work count
    P = A["projection"]
    per_review_enrich = enr["cost"] / max(1, enr["reviews_sent"])
    tok = {i: enr[i] / max(1, enr["reviews_sent"]) for i in ITEMS}
    verify = by_stage[("cold", "verify")]
    per_review_verify = verify["cost"] / max(1, verify["reviews_sent"])
    fixed = by_stage[("cold", "group")]["cost"] + by_stage[("cold", "memo")]["cost"]
    sec_per_batch = enr["latency"] / max(1, enr["calls"])

    def scenario(name, retry_rate, verify_frac, distinct_frac, workers_eff):
        distinct = round(P["nonempty_rows"] * distinct_frac)
        work = distinct * (1 + retry_rate)
        enrich_cost = work * per_review_enrich
        verify_cost = min(P["verify_max"], max(P["verify_min"], round(verify_frac * distinct))) * per_review_verify
        total = enrich_cost + verify_cost + fixed
        hours = (work / P["batch_size"]) * sec_per_batch / workers_eff / 3600
        return {"scenario": name, "distinct_texts_sent": distinct, "retry_rate": retry_rate, "enrich_usd": enrich_cost,
                "verify_usd": verify_cost, "fixed_usd": fixed, "total_usd": total, "hours": hours,
                "over_budget": total > A["controls"]["spend_cap_usd"]}

    scen = [scenario("base (exact-text reuse)", P["base_retry_rate"], P["verify_frac"], P["distinct_text_fraction"], P["effective_workers"]),
            scenario("conservative (reuse)", P["conservative_retry_rate"], P["verify_frac"] * 2, P["distinct_text_fraction"], max(1, P["effective_workers"] / 2)),
            scenario("no-reuse comparison", P["base_retry_rate"], P["verify_frac"], 1.0, P["effective_workers"])]
    full = A["full_corpus_reference"]
    full_scen = {"distinct": full["distinct_nonempty_texts"], "no_reuse": full["nonempty_rows"]}
    full_cost = {k: v * (1 + P["base_retry_rate"]) * per_review_enrich + fixed for k, v in full_scen.items()}

    rate_lines = sorted({(m, i): rate_meta[(m, i)] for (m, i) in rate_meta}.items())
    L = []
    L.append("# 100-review cost and runtime report (offline replay)\n")
    L.append(f"Generated from saved measurements; no API calls. Rates multiplier: **{a.rates_mult}×**. "
             f"Input `cost_100.csv` sha256 `{runs['input_sha256']}` ({n_inputs} IDs, {runs['unique_texts']} unique texts).\n")
    L.append("## Measured on the real pilot (cold = empty result cache, warm = saved results)\n")
    L.append("| Run | Wall-clock s | API cost | Enrichment calls | Completed | Failed/quarantined | Result-cache hits |")
    L.append("|---|---|---|---|---|---|---|")
    L.append(f"| cold | {cold['wall_clock_seconds']} | {money(cold_cost)} | {by_stage[('cold', 'enrich')]['calls']:.0f} | {completed} | {failed} | 0 |")
    L.append(f"| warm | {warm['wall_clock_seconds']} | {money(warm_cost)} | {by_stage[('warm', 'enrich')]['calls']:.0f} | {completed} | {failed} | {warm['enrich_cache_hits']} (all completed IDs reused; downstream stages cached by input fingerprint) |\n")
    L.append("### By stage (cold run)\n")
    L.append("| Stage | Provider / exact model | Settings | Prompt/schema | Batch | Workers | Calls (failed) | Reviews sent | Input | Cache write | Cache read | Output | Cost | Stage s |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for role in ("enrich", "verify", "group", "memo"):
        s = by_stage[("cold", role)]
        model = next((cl["model"] for cl in calls if cl["role"] == role), "")
        settings = next((cl.get("settings") for cl in calls if cl["role"] == role), {})
        cfg = next((cl.get("label_config") for cl in calls if cl["role"] == role), "")
        L.append(f"| {role} | anthropic / `{model}` | thinking {settings.get('thinking')}, effort {settings.get('effort') or '—'}, max_tokens {settings.get('max_tokens')} "
                 f"| `{cfg}` | {'≤50' if role in ('enrich', 'verify') else 'bounded pack'} | 1 | {s['calls']:.0f} ({s['failed']:.0f}) | {s['reviews_sent']:.0f} | "
                 f"{s['input_tokens']:.0f} | {s['cache_write_input_tokens']:.0f} | {s['cache_read_input_tokens']:.0f} | {s['output_tokens']:.0f} | {money(s['cost'])} | {cold['stage_seconds'].get(role, '')} |")
    L.append(f"\nPrepare (code) {cold['stage_seconds'].get('prepare')} s; rank (code) {cold['stage_seconds'].get('rank')} s. "
             "Per-call latencies sum to more than stage time only when calls overlap; the pilot used 1 worker.\n")
    L.append(f"- Cost per 1,000 input rows (cold, all stages): **{money(cold_cost / n_inputs * 1000)}**; enrichment only: {money(enr['cost'] / n_inputs * 1000)}")
    L.append(f"- Cost per completed record (cold, all stages): {money(cold_cost / max(1, completed))}")
    L.append(f"- Enrichment throughput: {enr['reviews_sent'] / max(1e-9, cold['stage_seconds']['enrich']):.2f} reviews/s at 1 worker; {sec_per_batch:.1f} s per 50-review request")
    L.append(f"- Enrichment tokens per review: input {tok['input_tokens']:.0f} uncached + {tok['cache_read_input_tokens']:.0f} cache-read + {tok['cache_write_input_tokens']:.0f} cache-write; output {tok['output_tokens']:.0f}")
    L.append("- Local compute: Intel i7 MacBook, stdlib Python + SQLite; prepare reads 660,622 rows in ~27 s at ~360 MB RAM. "
             "Electricity/hardware cost: **unknown (not measured)**, shown separately from API spend.\n")
    L.append("## Projection for the declared final scope\n")
    L.append(f"Scope: {P['scope_rows']:,} rows = {P['nonempty_rows']:,} nonempty to classify + {P['scope_rows'] - P['nonempty_rows']} empty-text quarantines. "
             f"Assumed distinct-text fraction {P['distinct_text_fraction']} (exact-text reuse), verification {P['verify_frac']:.0%} "
             f"(min {P['verify_min']}, max {P['verify_max']}), fixed overhead (group + memo) counted once, {P['effective_workers']} effective workers.\n")
    L.append("| Scenario | Texts sent | Retry rate | Enrich | Verify | Fixed | **Total API** | Est. hours | Budget |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for s in scen:
        L.append(f"| {s['scenario']} | {s['distinct_texts_sent']:,} | {s['retry_rate']:.0%} | {money(s['enrich_usd'])} | {money(s['verify_usd'])} | "
                 f"{money(s['fixed_usd'])} | **{money(s['total_usd'])}** | {s['hours']:.1f} | {'⚠ EXCEEDS cap' if s['over_budget'] else 'within cap'} |")
    L.append(f"\nReference, full corpus (not run): 484,189 distinct texts ≈ {money(full_cost['distinct'])}; no reuse (660,609) ≈ {money(full_cost['no_reuse'])}.\n")
    L.append("## Controls (editable in `cost/assumptions.json`; enforced in `pipeline/llm.py`)\n")
    C = A["controls"]
    L.append(f"- Spend cap: **${C['spend_cap_usd']}** (ledger reserves worst-case cost before each call; refuses dispatch past the cap)")
    L.append(f"- Output-token cap: {C['output_token_cap_per_request']} per enrichment request (150 × reviews + 300)")
    L.append(f"- Max workers: {C['max_workers']}; batch ≤ 50 reviews; transient retries ≤ {C['transient_max_attempts']} attempts with exponential backoff + jitter")
    L.append(f"- Invalid-output retry: once, then quarantine. Fallback model fraction: {C['fallback_fraction']} (no stronger-model fallback used)\n")
    L.append("## Rates used (dated sources)\n")
    L.append("| Model | Item | USD per 1M | Source | Checked |")
    L.append("|---|---|---|---|---|")
    for (m, i), r in rate_lines:
        L.append(f"| `{m}` | {i} | {float(r['usd_per_million']) * a.rates_mult:.3f} | {r['source']} | {r['checked_on']} |")
    L.append("\nFormula: item_cost = billed_units × price_per_unit; categories (uncached input, cache write, cache read, output) are mutually exclusive; "
             "thinking was disabled on Haiku and is included in billed output on Sonnet. Usage per call: `cost/usage.csv`.")
    a.out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"Wrote {a.out.name}: cold {money(cold_cost)} / {cold['wall_clock_seconds']}s, warm {money(warm_cost)} / {warm['wall_clock_seconds']}s; "
          f"projection base {money(scen[0]['total_usd'])} ({scen[0]['hours']:.1f} h)")


if __name__ == "__main__":
    main()
