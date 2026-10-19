"""Shared helpers: paths, source hashing, CSV reading, atomic writes, the SQLite state store.

Code owns record accounting. Every source review gets one row in the `records` table,
and its status only moves pending -> completed or pending -> quarantined.
"""

import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "data" / "dataset"
FULL_CSV = DATASET / "spotify_reviews_18months.csv"
CHECKER = DATASET / "check_submission.py"
OUTPUTS = ROOT / "outputs"
STATE = ROOT / "state"
DB_PATH = STATE / "pipeline.sqlite"


def configure(workdir):
    """Point state/ and outputs/ at another folder (used for the isolated 100-review pilot)."""
    global OUTPUTS, STATE, DB_PATH
    workdir = Path(workdir).resolve()
    OUTPUTS, STATE = workdir / "outputs", workdir / "state"
    DB_PATH = STATE / "pipeline.sqlite"

FIELDS = ("review_id", "review_text", "review_rating", "review_likes", "app_version", "review_timestamp")
TOPICS = ("access", "usability", "playback", "downloads", "catalog", "billing", "support", "other")
SUBTOPICS = {
    "access": ("login_failure", "signup", "password_reset", "account_locked_or_hacked", "third_party_login", "other"),
    "usability": ("ads_frequency", "navigation_layout", "playlist_queue_management", "ui_redesign", "controls_widgets", "other"),
    "playback": ("crashes", "stops_or_pauses", "skipping_glitches", "buffering_lag", "connection_errors", "audio_quality",
                 "battery_resources", "device_integration", "other"),
    "downloads": ("downloads_disappearing", "download_failure", "offline_mode", "storage", "other"),
    "catalog": ("missing_content", "search", "recommendations_shuffle", "lyrics", "podcasts", "other"),
    "billing": ("price", "charges_refunds", "premium_only_restrictions", "subscription_activation", "plans_family_student", "other"),
    "support": ("no_response", "unhelpful_response", "other"),
    "other": ("general_praise", "general_criticism", "unrelated_or_political", "other"),
}
ALL_SUBTOPICS = sorted({s for subs in SUBTOPICS.values() for s in subs})
INTENTS = ("cancellation", "complaint", "request", "praise", "unclear")  # precedence order

# The course's own sampling key (manifest.json "samples"): lowest SHA-256(seed:review_id) first.
COURSE_SEED = "berkeley-fall-2026-assignment-5-v1"


def canonical(value):
    """Same serialization as the grading checker, so hashes match exactly."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def row_sha(row):
    """SHA-256 of the six original field strings, untouched (GRADING_CONTRACT.md)."""
    return hashlib.sha256(canonical([row[k] for k in FIELDS]).encode("utf-8")).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def text_sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def course_rank(review_id):
    return int.from_bytes(hashlib.sha256((COURSE_SEED + ":" + review_id).encode()).digest(), "big")


def csv_rows(path):
    """Strict reader that keeps multiline review text intact (same rules as the checker)."""
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, strict=True)
        if tuple(reader.fieldnames or ()) != FIELDS:
            raise ValueError(f"Unexpected CSV header in {path}: {reader.fieldnames}")
        for row in reader:
            if None in row or any(v is None for v in row.values()):
                raise ValueError("Malformed CSV row")
            yield row


def write_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_json(path, value):
    """Write to a temp file, then rename: a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.replace(tmp, path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def connect(db_path=None):
    db_path = db_path or DB_PATH
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    review_id       TEXT PRIMARY KEY,
    scope_rank      INTEGER NOT NULL,      -- position in the course sampling order; empties at the end
    scope_part      TEXT NOT NULL,         -- golden | core | extension | empty
    source_sha256   TEXT NOT NULL,
    text_sha256     TEXT NOT NULL,
    review_text     TEXT NOT NULL,
    review_rating   TEXT NOT NULL,
    review_likes    TEXT NOT NULL,
    app_version     TEXT NOT NULL,
    review_timestamp TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'quarantined')),
    reason          TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    labels_json     TEXT,                  -- validated labels once completed
    label_config    TEXT,
    cache_source_id TEXT,
    phase           TEXT,                  -- initial | resume (enrichment run phase)
    updated_at      TEXT,
    recovery_tried  INTEGER
);
CREATE INDEX IF NOT EXISTS records_status ON records(status, scope_rank);
CREATE INDEX IF NOT EXISTS records_text ON records(text_sha256);
"""
