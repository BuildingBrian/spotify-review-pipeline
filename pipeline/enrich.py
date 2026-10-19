"""Stage 2 - ENRICH (model: Haiku, <=50 reviews per request; code validates and saves).

Loop: pick pending reviews in scope order -> reuse an exact-text result when one exists under the
same label_config -> send one representative per distinct text, in batches of <=50 -> validate
every returned key and field -> save the whole batch in one SQLite transaction.

Failure rules (from the brief):
- invalid output for a review: retried once (in a later batch); second failure -> quarantined with reason
- transient API errors: bounded backoff inside llm.call; if a request still fails, the run stops and saves
- spend cap / time cap / Ctrl+C / --stop-after: stop admitting work, finish in-flight batches, save, snapshot
"""

import json
import re
import signal
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from . import common as c
from . import llm

MODEL = "claude-haiku-5-5"
PROMPT_VERSION = "enrich-v1"
SCHEMA_VERSION = "schema-v1"
MAX_INVALID_ATTEMPTS = 2  # first try + one retry

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["key", "topic", "subtopic", "intent", "severity", "sentiment", "entities", "evidence_quote", "needs_review"],
        "properties": {
            "key": {"type": "string"},
            "topic": {"type": "string", "enum": list(c.TOPICS)},
            "subtopic": {"type": "string", "enum": c.ALL_SUBTOPICS},
            "intent": {"type": "string", "enum": list(c.INTENTS)},
            "severity": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
            "sentiment": {"type": "number"},
            "entities": {"type": "array", "items": {"type": "string"}},
            "evidence_quote": {"type": "string"},
            "needs_review": {"type": "boolean"},
        }}}},
}


def system_prompt():
    template = (c.ROOT / "prompts" / "enrich_v1.md").read_text(encoding="utf-8")
    return template.replace("{DEFINITIONS}", (c.ROOT / "labels" / "definitions.md").read_text(encoding="utf-8"))


def label_config():
    """Changes whenever the model, prompt text, definitions or schema change -> cache reuse is only exact."""
    fingerprint = c.text_sha(system_prompt() + json.dumps(SCHEMA, sort_keys=True))[:8]
    return f"{MODEL}+thinking-disabled+{PROMPT_VERSION}-{fingerprint}+{SCHEMA_VERSION}"


def exact_span(quote, text):
    """Return the exact source substring for a model quote, or None.
    Exact match first; otherwise the same words with different whitespace/case (deterministic repair)."""
    quote = quote.strip()
    if not quote:
        return None, False
    if quote in text:
        return quote, False
    words = quote.split()
    if not words:
        return None, False
    m = re.search(r"\s+".join(re.escape(w) for w in words), text, flags=re.IGNORECASE)
    return (m.group(0), True) if m else (None, False)


def fuzzy_span(quote, text, min_ratio=0.85):
    """Recovery only: the model 'corrected' typos while quoting. Find the source span that best matches and
    return that EXACT source text (never the model's wording). None if nothing is similar enough."""
    import difflib
    q = quote.strip()
    if not q:
        return None
    sm = difflib.SequenceMatcher(None, q.lower(), text.lower(), autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    if not blocks:
        return None
    start, end = blocks[0].b, blocks[-1].b + blocks[-1].size
    span = text[start:end].strip()
    ratio = difflib.SequenceMatcher(None, q.lower(), span.lower(), autojunk=False).ratio()
    return span if span and ratio >= min_ratio else None


def validate(item, text, recovery=False):
    """Returns (labels, problem). Code is the judge of validity; the model only proposes."""
    if item.get("topic") not in c.TOPICS:
        return None, "bad_topic"
    if item.get("intent") not in c.INTENTS:
        return None, "bad_intent"
    sev = item.get("severity")
    if type(sev) is not int or not 1 <= sev <= 5:
        return None, "bad_severity"
    sent = item.get("sentiment")
    if type(sent) not in (int, float) or not -1 <= sent <= 1:
        return None, "bad_sentiment"
    if type(item.get("needs_review")) is not bool:
        return None, "bad_needs_review"
    ents = item.get("entities")
    if not isinstance(ents, list) or not all(isinstance(e, str) for e in ents):
        return None, "bad_entities"
    quote, repaired = exact_span(item.get("evidence_quote") or "", text)
    if quote is None and recovery:
        quote, repaired = fuzzy_span(item.get("evidence_quote") or "", text), True
    if quote is None:
        return None, "quote_not_in_source"
    sub = item.get("subtopic")
    sub_fixed = sub not in c.SUBTOPICS[item["topic"]]  # optional extension: mismatch is repaired, not fatal
    return {"topic": item["topic"], "subtopic": "other" if sub_fixed else sub, "subtopic_repaired": sub_fixed,
            "intent": item["intent"], "severity": sev, "sentiment": round(float(sent), 2),
            "entities": [e.strip() for e in ents if e.strip()], "evidence_quote": quote,
            "needs_review": item["needs_review"], "quote_repaired": repaired}, None


class Enricher:
    def __init__(self, *, run_id, upto_rank, batch_size=50, workers=1, cap_usd=15.0, max_minutes=None,
                 stop_after_batches=None, calls_log=None, progress_path=None, recovery=False):
        self.db = c.connect()
        self.run_id, self.upto_rank, self.batch_size, self.workers = run_id, upto_rank, min(batch_size, 50), workers
        self.max_minutes, self.stop_after = max_minutes, stop_after_batches
        self.system, self.config = system_prompt(), label_config()
        self.recovery = recovery
        if recovery:  # declared recovery pass: own label tag so these records stay distinguishable
            self.config += "+recovery-fuzzyquote"
        self.calls_log = calls_log or (c.STATE / "calls.jsonl")
        self.progress_path = progress_path or (c.STATE / "progress.json")
        self.log_lock = threading.Lock()
        rates = llm.load_rates()
        self.ledger = llm.Ledger(cap_usd, rates, spent_usd=self._spent_so_far())
        self.stop_reason, self.batches_done, self.started = None, 0, time.time()
        self.run_calls = self.run_failures = 0
        self.run_spend = 0.0
        self.done_records = 0
        completed_before = self.db.execute(
            "SELECT COUNT(*) FROM records WHERE status='completed' AND label_config=?", (self.config,)).fetchone()[0]
        self.phase = "resume" if completed_before else "initial"

    # ---------- bookkeeping ----------
    def _spent_so_far(self):
        return llm.spent_from_log(self.calls_log)

    def log(self, rec):
        with self.log_lock:
            self.calls_log.parent.mkdir(parents=True, exist_ok=True)
            with self.calls_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self.run_calls += 1
            self.run_failures += rec.get("outcome") == "failed"
            self.run_spend += rec.get("cost_usd") or 0.0

    def counts(self):
        rows = self.db.execute("""SELECT status, COUNT(*) FROM records WHERE scope_rank < ? GROUP BY status""",
                               (self.upto_rank,)).fetchall()
        return {k: v for k, v in rows}

    def snapshot(self, name):
        ids = [r[0] for r in self.db.execute(
            "SELECT review_id FROM records WHERE status='completed' AND label_config=? ORDER BY review_id", (self.config,))]
        c.write_json(c.STATE / "checkpoints" / f"{self.run_id}-{name}.json",
                     {"run_id": self.run_id, "label_config": self.config, "taken_at": datetime.now(timezone.utc).isoformat(),
                      "completed_ids": ids})
        return len(ids)

    def progress(self, status):
        n = self.counts()
        elapsed = time.time() - self.started
        done = self.batches_done
        pending = n.get("pending", 0)
        rate = (self.done_records / elapsed * 60) if elapsed > 0 else 0
        c.write_json(self.progress_path, {
            "stage": "enrich", "run_id": self.run_id, "status": status, "stop_reason": self.stop_reason,
            "phase": self.phase, "label_config": self.config, "target_rank": self.upto_rank, "workers": self.workers,
            "batch_size": self.batch_size, "counts_in_target": n, "batches_done_this_run": done,
            "records_done_this_run": self.done_records, "calls_this_run": self.run_calls,
            "failed_calls_this_run": self.run_failures, "spend_this_run_usd": round(self.run_spend, 4),
            "spend_total_usd": round(self.ledger.spent, 4), "spend_cap_usd": self.ledger.cap,
            "records_per_minute": round(rate, 1), "eta_minutes": round(pending / rate, 1) if rate else None,
            "elapsed_minutes": round(elapsed / 60, 1), "updated_at": datetime.now(timezone.utc).isoformat()})

    # ---------- cache ----------
    def apply_cache(self):
        """Pending rows whose exact text already has a direct (non-cached) result under this config."""
        now = datetime.now(timezone.utc).isoformat()
        with self.db:
            cur = self.db.execute("""
                UPDATE records SET status='completed', labels_json=src.labels_json, label_config=src.label_config,
                       cache_source_id=src.review_id, phase=NULL, updated_at=?
                FROM (SELECT text_sha256, MIN(review_id) AS review_id, labels_json, label_config FROM records
                      WHERE status='completed' AND label_config=? AND cache_source_id IS NULL GROUP BY text_sha256) AS src
                WHERE records.status='pending' AND records.scope_rank < ? AND records.text_sha256 = src.text_sha256""",
                                  (now, self.config, self.upto_rank))
        return cur.rowcount

    # ---------- batches ----------
    def next_batch(self, in_flight_texts):
        rows, texts = [], set()
        where = ("status='quarantined' AND reason='quote_not_in_source' AND recovery_tried IS NULL" if self.recovery
                 else "status='pending'")
        for r in self.db.execute(f"""SELECT review_id, review_text, text_sha256, attempts FROM records
                                    WHERE {where} AND scope_rank < ? ORDER BY scope_rank""", (self.upto_rank,)):
            if r["text_sha256"] in in_flight_texts or r["text_sha256"] in texts:
                continue  # one representative per distinct text; duplicates reuse its result afterwards
            rows.append(dict(r))
            texts.add(r["text_sha256"])
            if len(rows) == self.batch_size:
                break
        return rows

    def run_batch(self, rows):
        payload = json.dumps([{"key": str(i + 1), "text": r["review_text"]} for i, r in enumerate(rows)], ensure_ascii=False)
        max_tokens = min(8000, 150 * len(rows) + 300)
        try:
            parsed, _ = llm.call(role="enrich", model=MODEL, system=self.system, user=payload, schema=SCHEMA,
                                 max_tokens=max_tokens, ledger=self.ledger, log=self.log,
                                 review_ids=[r["review_id"] for r in rows], phase=self.phase,
                                 label_config=self.config, run_id=self.run_id)
            return rows, parsed, None
        except llm.CallFailed as e:
            return rows, None, str(e)

    def save_batch(self, rows, parsed, error):
        now = datetime.now(timezone.utc).isoformat()
        by_key = {}
        if parsed:
            for item in parsed.get("results", []):
                k = item.get("key")
                by_key.setdefault(k, []).append(item)
        completed = quarantined = retry = 0
        if error and "gave up" in error:
            return 0, 0, 0  # API outage, not a bad answer: rows stay pending with attempts unchanged
        with self.db:  # atomic: the whole batch lands or none of it does
            for i, r in enumerate(rows):
                attempts = r["attempts"] + 1
                items = by_key.get(str(i + 1), [])
                if error:
                    labels, problem = None, "call_failed: " + error[:120]
                elif len(items) != 1:
                    labels, problem = None, "missing_key" if not items else "duplicate_key"
                else:
                    labels, problem = validate(items[0], r["review_text"], recovery=self.recovery)
                if self.recovery:
                    self.db.execute("UPDATE records SET recovery_tried=1 WHERE review_id=?", (r["review_id"],))
                    if labels:
                        self.db.execute("""UPDATE records SET status='completed', labels_json=?, label_config=?, attempts=?,
                                           phase=?, reason=NULL, updated_at=? WHERE review_id=?""",
                                        (json.dumps(labels, ensure_ascii=False), self.config, attempts, self.phase, now,
                                         r["review_id"]))
                        completed += 1
                    else:
                        self.db.execute("UPDATE records SET attempts=?, reason=?, updated_at=? WHERE review_id=?",
                                        (attempts, problem + " (after recovery pass)", now, r["review_id"]))
                        quarantined += 1
                    continue
                if labels:
                    self.db.execute("""UPDATE records SET status='completed', labels_json=?, label_config=?, attempts=?,
                                       phase=?, reason=NULL, updated_at=? WHERE review_id=? AND status='pending'""",
                                    (json.dumps(labels, ensure_ascii=False), self.config, attempts, self.phase, now,
                                     r["review_id"]))
                    completed += 1
                elif attempts >= MAX_INVALID_ATTEMPTS:
                    self.db.execute("""UPDATE records SET status='quarantined', reason=?, attempts=?, updated_at=?
                                       WHERE review_id=? AND status='pending'""", (problem, attempts, now, r["review_id"]))
                    quarantined += 1
                else:
                    self.db.execute("UPDATE records SET attempts=?, reason=?, updated_at=? WHERE review_id=?",
                                    (attempts, problem, now, r["review_id"]))
                    retry += 1
            extra = set(by_key) - {str(i + 1) for i in range(len(rows))}
            if extra:
                self.log({"run_id": self.run_id, "role": "enrich", "event": "unexpected_keys_ignored",
                          "keys": sorted(extra)[:20]})
        self.done_records += completed + quarantined
        return completed, quarantined, retry

    def should_stop(self):
        if self.stop_reason:
            return True
        if self.stop_after is not None and self.batches_done >= self.stop_after:
            self.stop_reason = f"stop_after_batches={self.stop_after} (planned interruption)"
        elif self.max_minutes and time.time() - self.started > self.max_minutes * 60:
            self.stop_reason = f"time cap {self.max_minutes} min"
        return bool(self.stop_reason)

    def run(self):
        self.done_records = 0
        before = self.snapshot("start") if self.phase == "resume" else 0
        print(f"[{self.run_id}] enrich {self.config}\n  phase={self.phase}, completed under this config at start={before}, "
              f"workers={self.workers}, batch={self.batch_size}, cap=${self.ledger.cap:.2f} (spent so far ${self.ledger.spent:.4f})")

        def on_sigint(signum, frame):
            if self.stop_reason:
                raise KeyboardInterrupt
            self.stop_reason = "interrupted (Ctrl+C): finishing in-flight batches, then saving"
            print("\n" + self.stop_reason + " — press Ctrl+C again to abort immediately (in-flight work stays pending)")
        old = signal.signal(signal.SIGINT, on_sigint)

        cached = self.apply_cache()
        in_flight = {}
        try:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                while True:
                    while len(in_flight) < self.workers and not self.should_stop():
                        texts = {t for rows in in_flight.values() for t in (r["text_sha256"] for r in rows)}
                        rows = self.next_batch(texts)
                        if not rows:
                            break
                        in_flight[pool.submit(self.run_batch, rows)] = rows
                    if not in_flight:
                        break
                    finished, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        rows = in_flight.pop(fut)
                        rows_, parsed, error = fut.result()
                        done, quar, retry = self.save_batch(rows_, parsed, error)
                        self.batches_done += 1
                        cached += self.apply_cache()
                        if error and "gave up" in error:
                            self.stop_reason = "API kept failing after bounded retries: " + error[:150]
                        n = self.counts()
                        print(f"  batch {self.batches_done}: +{done} done, {quar} quarantined, {retry} to retry | "
                              f"in target: {n.get('completed', 0)} completed, {n.get('pending', 0)} pending | "
                              f"spend ${self.ledger.spent:.4f}", flush=True)
                        self.progress("running")
        except llm.BudgetExceeded as e:
            self.stop_reason = f"spend cap: {e}"
        finally:
            signal.signal(signal.SIGINT, old)
            after = self.snapshot("end")
            final = "stopped" if self.stop_reason else "finished"
            self.progress(final)
            n = self.counts()
            print(f"[{self.run_id}] {final}{': ' + self.stop_reason if self.stop_reason else ''}\n"
                  f"  completed under config: {before} -> {after}; cache reuses this run: {cached}; "
                  f"in target: {n}; calls {self.run_calls} ({self.run_failures} failed); run spend ${self.run_spend:.4f}")
        return final
