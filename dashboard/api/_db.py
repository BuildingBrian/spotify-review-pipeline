"""Tiny DB adapter: Postgres (Neon) in production via DATABASE_URL, a SQLite file for local testing.
All SQL is written once with %s placeholders and portable types."""

import os
import sqlite3

URL = os.environ.get("DATABASE_URL", "")


def connect():
    if URL.startswith(("postgres://", "postgresql://")):
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(URL, row_factory=dict_row, autocommit=True)
    path = URL.removeprefix("sqlite:///") or os.path.join(os.path.dirname(__file__), "..", "local.sqlite")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = lambda cur, row: {d[0]: v for d, v in zip(cur.description, row)}
    return conn


def is_pg(conn):
    return not isinstance(conn, sqlite3.Connection)


def q(conn, sql, params=()):
    if not is_pg(conn):
        sql = sql.replace("%s", "?")
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.fetchall()


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS reviews (
        review_id TEXT PRIMARY KEY, scope_part TEXT, status TEXT, reason TEXT, review_text TEXT,
        review_rating TEXT, review_likes INTEGER, review_timestamp TEXT, app_version TEXT,
        topic TEXT, subtopic TEXT, intent TEXT, severity INTEGER, sentiment REAL, evidence_quote TEXT,
        needs_review BOOLEAN, entities TEXT, cache_source_id TEXT, label_config TEXT, issue_id TEXT)""",
    "CREATE INDEX IF NOT EXISTS reviews_issue ON reviews(issue_id, review_likes)",
    "CREATE INDEX IF NOT EXISTS reviews_topic ON reviews(topic, intent)",
    """CREATE TABLE IF NOT EXISTS issues (
        issue_id TEXT PRIMARY KEY, rank INTEGER, name TEXT, description TEXT, topic TEXT,
        complaint_count INTEGER, severity_sum INTEGER, mean_severity TEXT, priority_score INTEGER)""",
    """CREATE TABLE IF NOT EXISTS topic_aggregates (
        topic TEXT PRIMARY KEY, completed INTEGER, complaint_count INTEGER, cancellation_count INTEGER,
        severity_sum INTEGER, mean_severity TEXT, severe_4_or_5 INTEGER, share_of_complaints TEXT)""",
    "CREATE TABLE IF NOT EXISTS monthly (month TEXT PRIMARY KEY, completed INTEGER, complaints INTEGER, topics_json TEXT)",
    """CREATE TABLE IF NOT EXISTS recommendation (
        id INTEGER PRIMARY KEY, recommended_area TEXT, headline TEXT, memo_markdown TEXT,
        claims_json TEXT, check_json TEXT, model TEXT, evidence_json TEXT, generated_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS claims (claim_id TEXT PRIMARY KEY, issue_id TEXT, metric TEXT, value TEXT)",
    "CREATE TABLE IF NOT EXISTS metrics (key TEXT PRIMARY KEY, value TEXT)",
]
