# 100-review cost and runtime report (offline replay)

Generated from saved measurements; no API calls. Rates multiplier: **1.0×**. Input `cost_100.csv` sha256 `c884ac3b9be5066995d5063f96ad9af6e5e082975788c1684c4f6b6ea661dd0e` (100 IDs, 100 unique texts).

## Measured on the real pilot (cold = empty result cache, warm = saved results)

| Run | Wall-clock s | API cost | Enrichment calls | Completed | Failed/quarantined | Result-cache hits |
|---|---|---|---|---|---|---|
| cold | 61.087 | $0.0332 | 3 | 100 | 0 | 0 |
| warm | 0.023 | $0.0000 | 0 | 100 | 0 | 100 (all completed IDs reused; downstream stages cached by input fingerprint) |

### By stage (cold run)

| Stage | Provider / exact model | Settings | Prompt/schema | Batch | Workers | Calls (failed) | Reviews sent | Input | Cache write | Cache read | Output | Cost | Stage s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| enrich | anthropic / `claude-haiku-5-5` | thinking disabled, effort —, max_tokens 7800 | `claude-haiku-5-5+thinking-disabled+enrich-v1-a43a4634+schema-v1` | ≤50 | 1 | 3 (0) | 102 | 4591 | 3893 | 7786 | 9512 | $0.0058 | 37.585 |
| verify | anthropic / `claude-haiku-5-5` | thinking disabled, effort —, max_tokens 1400 | `claude-haiku-5-5+verify-v1` | ≤50 | 1 | 1 (0) | 20 | 547 | 3129 | 0 | 688 | $0.0008 | 3.952 |
| group | anthropic / `claude-haiku-5-5` | thinking disabled, effort —, max_tokens 1740 | `claude-haiku-5-5+group-v1` | bounded pack | 1 | 1 (0) | 0 | 1615 | 537 | 0 | 1149 | $0.0008 | 7.293 |
| memo | anthropic / `claude-sonnet-5-5` | thinking adaptive, effort low, max_tokens 6000 | `claude-sonnet-5-5+effort-low+memo-v1` | bounded pack | 1 | 1 (0) | 0 | 3885 | 1099 | 0 | 1532 | $0.0258 | 12.12 |

Prepare (code) 0.126 s; rank (code) 0.005 s. Per-call latencies sum to more than stage time only when calls overlap; the pilot used 1 worker.

- Cost per 1,000 input rows (cold, all stages): **$0.3321**; enrichment only: $0.0578
- Cost per completed record (cold, all stages): $0.0003
- Enrichment throughput: 2.71 reviews/s at 1 worker; 11.7 s per 50-review request
- Enrichment tokens per review: input 45 uncached + 76 cache-read + 38 cache-write; output 93
- Local compute: Intel i7 MacBook, stdlib Python + SQLite; prepare reads 660,622 rows in ~27 s at ~360 MB RAM. Electricity/hardware cost: **unknown (not measured)**, shown separately from API spend.

## Projection for the declared final scope

Scope: 150,013 rows = 150,000 nonempty to classify + 13 empty-text quarantines. Assumed distinct-text fraction 0.7703 (exact-text reuse), verification 2% (min 20, max 3000), fixed overhead (group + memo) counted once, 4 effective workers.

| Scenario | Texts sent | Retry rate | Enrich | Verify | Fixed | **Total API** | Est. hours | Budget |
|---|---|---|---|---|---|---|---|---|
| base (exact-text reuse) | 115,545 | 3% | $6.7435 | $0.0913 | $0.0266 | **$6.8614** | 1.9 | within cap |
| conservative (reuse) | 115,545 | 10% | $7.2018 | $0.1185 | $0.0266 | **$7.3469** | 4.1 | within cap |
| no-reuse comparison | 150,000 | 3% | $8.7544 | $0.1185 | $0.0266 | **$8.8995** | 2.5 | within cap |

Reference, full corpus (not run): 484,189 distinct texts ≈ $28.29; no reuse (660,609) ≈ $38.58.

## Controls (editable in `cost/assumptions.json`; enforced in `pipeline/llm.py`)

- Spend cap: **$15** (ledger reserves worst-case cost before each call; refuses dispatch past the cap)
- Output-token cap: 7800 per enrichment request (150 × reviews + 300)
- Max workers: 6; batch ≤ 50 reviews; transient retries ≤ 4 attempts with exponential backoff + jitter
- Invalid-output retry: once, then quarantine. Fallback model fraction: 0.0 (no stronger-model fallback used)

## Rates used (dated sources)

| Model | Item | USD per 1M | Source | Checked |
|---|---|---|---|---|
| `claude-haiku-5-5` | cache_read_input_tokens | 0.010 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |
| `claude-haiku-5-5` | cache_write_input_tokens | 0.125 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |
| `claude-haiku-5-5` | input_tokens | 0.100 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |
| `claude-haiku-5-5` | output_tokens | 0.500 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |
| `claude-sonnet-5-5` | cache_read_input_tokens | 0.100 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |
| `claude-sonnet-5-5` | cache_write_input_tokens | 2.500 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |
| `claude-sonnet-5-5` | input_tokens | 2.000 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |
| `claude-sonnet-5-5` | output_tokens | 10.000 | https://platform.claude.com/docs/en/about-claude/pricing | 2026-10-18 |

Formula: item_cost = billed_units × price_per_unit; categories (uncached input, cache write, cache read, output) are mutually exclusive; thinking was disabled on Haiku and is included in billed output on Sonnet. Usage per call: `cost/usage.csv`.
