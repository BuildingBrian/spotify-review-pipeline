"""The only module that talks to a model API. Code-side controls live here:

- explicit opt-in: nothing in this file runs unless a command was given --paid
- spend ledger: reserve worst-case cost before each call, settle with real usage after;
  refuse to dispatch when spent + reserved + next reservation would exceed the cap
- bounded retries for transient errors (429, 5xx, overload, timeout, connection) with backoff + jitter
- every attempt (success or failure) is logged with role, phase, review IDs, model, usage, latency
"""

import csv
import json
import os
import random
import threading
import time
import uuid
from datetime import datetime, timezone

from . import common as c

RATES_CSV = c.ROOT / "cost" / "rates.csv"
TRANSIENT_MAX_ATTEMPTS = 4


class BudgetExceeded(Exception):
    pass


class CallFailed(Exception):
    pass


def load_rates(path=RATES_CSV):
    """{(model, item): usd_per_unit}. Items: input_tokens, output_tokens, cache_read_input_tokens, cache_write_input_tokens."""
    rates = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rates[(row["model"], row["item"])] = float(row["usd_per_unit"])
    return rates


def cost_of(usage, model, rates):
    """Mutually exclusive billing items: uncached input, cache reads, cache writes, output (output already includes any thinking)."""
    total = 0.0
    for item in ("input_tokens", "cache_read_input_tokens", "cache_write_input_tokens", "output_tokens"):
        n = usage.get(item) or 0
        if n:
            total += n * rates[(model, item)]
    return total


def spent_from_log(path):
    """Total recorded spend across every run (the ledger's starting point)."""
    total = 0.0
    if path.exists():
        for line in path.open(encoding="utf-8"):
            total += json.loads(line).get("cost_usd") or 0.0
    return total


class Ledger:
    """One shared spend ledger for all workers (thread-safe)."""

    def __init__(self, cap_usd, rates, spent_usd=0.0):
        self.cap, self.rates, self.spent, self.reserved = cap_usd, rates, spent_usd, 0.0
        self.lock = threading.Lock()

    def reserve(self, model, max_input_tokens, max_output_tokens):
        worst = cost_of({"input_tokens": max_input_tokens, "output_tokens": max_output_tokens}, model, self.rates)
        with self.lock:
            if self.spent + self.reserved + worst > self.cap:
                raise BudgetExceeded(f"spent ${self.spent:.4f} + reserved ${self.reserved:.4f} + next ${worst:.4f} "
                                     f"> cap ${self.cap:.2f}")
            self.reserved += worst
        return worst

    def settle(self, reservation, actual):
        with self.lock:
            self.reserved -= reservation
            self.spent += actual


_client = None


def client():
    """Created lazily so importing this module never needs a key or makes a call."""
    global _client
    if _client is None:
        from dotenv import load_dotenv
        import anthropic
        load_dotenv(c.ROOT / ".env")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise SystemExit("ANTHROPIC_API_KEY is not set in .env (paid commands need it; offline commands don't).")
        _client = anthropic.Anthropic(max_retries=0, timeout=120)  # we own retries so every attempt is logged
    return _client


def call(*, role, model, system, user, schema, max_tokens, ledger, log, review_ids=(), phase=None,
         label_config=None, run_id=None, est_input_tokens=None, effort=None):
    """One logical model request with bounded transient retries. Returns (parsed_json, attempt_records)."""
    import anthropic

    est_in = est_input_tokens or (len(system) + len(user)) // 3 + 50  # generous chars->tokens upper bound
    attempts = []
    for attempt in range(1, TRANSIENT_MAX_ATTEMPTS + 1):
        reservation = ledger.reserve(model, est_in, max_tokens)
        started = time.time()
        rec = {"request_id": None, "local_call_id": uuid.uuid4().hex, "run_id": run_id, "role": role, "model": model,
               "review_ids": list(review_ids), "phase": phase, "label_config": label_config, "attempt": attempt,
               "settings": {"thinking": "adaptive" if effort else "disabled", "effort": effort,
                            "max_tokens": max_tokens, "structured_output": True},
               "started_at": datetime.now(timezone.utc).isoformat()}
        try:
            # The fixed instructions are cached by the provider (cache hits bill at 10% of input);
            # the per-batch reviews come after the cache marker so they never invalidate it.
            resp = client().messages.create(
                model=model, max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                # Haiku: thinking off (cheapest). Stronger models (memo) can't disable it; use adaptive + low effort.
                **({"thinking": {"type": "disabled"}} if not effort else {}),
                output_config={"format": {"type": "json_schema", "schema": schema}, **({"effort": effort} if effort else {})},
            )
            u = resp.usage
            usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                     "cache_read_input_tokens": u.cache_read_input_tokens or 0,
                     "cache_write_input_tokens": u.cache_creation_input_tokens or 0}
            cost = cost_of(usage, model, ledger.rates)
            ledger.settle(reservation, cost)
            rec.update(request_id=resp.id, **usage, cost_usd=round(cost, 8), latency_s=round(time.time() - started, 3),
                       stop_reason=resp.stop_reason)
            if resp.stop_reason != "end_turn":
                rec.update(outcome="failed", error=f"stop_reason={resp.stop_reason}")
                log(rec)
                attempts.append(rec)
                raise CallFailed(rec["error"])  # truncated/refused output: not retried here; caller decides
            text = "".join(b.text for b in resp.content if b.type == "text")
            parsed = json.loads(text)
            rec["outcome"] = "succeeded"
            log(rec)
            attempts.append(rec)
            return parsed, attempts
        except (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError,
                anthropic.APITimeoutError, getattr(anthropic, "OverloadedError", anthropic.InternalServerError),
                getattr(anthropic, "ServiceUnavailableError", anthropic.InternalServerError)) as e:
            # Unknown charge on timeouts: keep the reservation as spent (conservative) and flag it.
            unknown = isinstance(e, (anthropic.APITimeoutError, anthropic.APIConnectionError))
            ledger.settle(reservation, reservation if unknown else 0.0)
            rec.update(request_id="local-" + rec["local_call_id"], outcome="failed", error=f"{type(e).__name__}: {e}"[:300],
                       input_tokens=0, output_tokens=0, cost_usd=round(reservation, 8) if unknown else 0.0,
                       cost_is_estimate=unknown, latency_s=round(time.time() - started, 3))
            log(rec)
            attempts.append(rec)
            if attempt == TRANSIENT_MAX_ATTEMPTS:
                raise CallFailed(f"{role}: gave up after {attempt} attempts: {rec['error']}")
            retry_after = getattr(getattr(e, "response", None), "headers", {}).get("retry-after") if hasattr(e, "response") else None
            delay = float(retry_after) if retry_after else min(60, 2 ** attempt) + random.uniform(0, 1.5)
            time.sleep(delay)
        except (json.JSONDecodeError, anthropic.BadRequestError, anthropic.AuthenticationError,
                anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
            if rec.get("request_id") is None:
                ledger.settle(reservation, 0.0)
                rec.update(request_id="local-" + rec["local_call_id"], input_tokens=0, output_tokens=0, cost_usd=0.0)
            rec.update(outcome="failed", error=f"{type(e).__name__}: {e}"[:300], latency_s=round(time.time() - started, 3))
            log(rec)
            attempts.append(rec)
            if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                raise SystemExit(f"API key problem: {e}")
            raise CallFailed(rec["error"])
