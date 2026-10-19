"""Read-only backend for the dashboard. Every response is read from the database; no model calls,
no API keys. The AI recommendation was generated once by the pipeline's memo stage and stored."""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from _db import connect, q  # noqa: E402

api = FastAPI(title="Spotify review pipeline API")


async def app(scope, receive, send):
    """Vercel rewrites /api/<path> to this one function as /api/index?__path=<path>; restore the original path."""
    if scope["type"] == "http" and scope.get("path") == "/api/index":
        from urllib.parse import parse_qsl, urlencode
        params = parse_qsl(scope.get("query_string", b"").decode())
        orig = next((v for k, v in params if k == "__path"), None)
        if orig is not None:
            scope = {**scope, "path": "/api/" + orig, "raw_path": ("/api/" + orig).encode(),
                     "query_string": urlencode([(k, v) for k, v in params if k != "__path"]).encode()}
    await api(scope, receive, send)
_conn = None
PUBLIC = os.path.join(os.path.dirname(__file__), "..", "public")


def db():
    global _conn
    if _conn is None:
        _conn = connect()
    return _conn


def metrics():
    return {r["key"]: json.loads(r["value"]) for r in q(db(), "SELECT key, value FROM metrics")}


@api.get("/api/health")
def health():
    return {"ok": True, "reviews": q(db(), "SELECT COUNT(*) AS n FROM reviews")[0]["n"]}


@api.get("/api/overview")
def overview():
    return {"metrics": metrics(),
            "topics": q(db(), "SELECT * FROM topic_aggregates ORDER BY complaint_count DESC"),
            "monthly": q(db(), "SELECT * FROM monthly ORDER BY month")}


@api.get("/api/ranking")
def ranking(limit: int = Query(25, ge=1, le=200)):
    return q(db(), "SELECT * FROM issues ORDER BY rank LIMIT %s", (limit,))


@api.get("/api/issues/{issue_id}")
def issue(issue_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    rows = q(db(), "SELECT * FROM issues WHERE issue_id = %s", (issue_id,))
    if not rows:
        raise HTTPException(404, "unknown issue")
    members = q(db(), """SELECT review_id, review_text, evidence_quote, severity, intent, sentiment, review_likes,
                         review_rating, review_timestamp FROM reviews WHERE issue_id = %s
                         ORDER BY review_likes DESC, severity DESC, review_id LIMIT %s OFFSET %s""",
                (issue_id, limit, offset))
    sev = q(db(), "SELECT severity, COUNT(*) AS n FROM reviews WHERE issue_id = %s GROUP BY severity ORDER BY severity",
            (issue_id,))
    return {"issue": rows[0], "severity_mix": sev, "members": members, "offset": offset}


@api.get("/api/recommendation")
def recommendation():
    rows = q(db(), "SELECT * FROM recommendation WHERE id = 1")
    if not rows:
        raise HTTPException(404, "no recommendation stored")
    r = rows[0]
    for k in ("claims_json", "check_json", "evidence_json"):
        r[k.removesuffix("_json")] = json.loads(r.pop(k) or "null")
    return r


@api.get("/api/reviews/{review_id}")
def review(review_id: str):
    rows = q(db(), "SELECT * FROM reviews WHERE review_id = %s", (review_id,))
    if not rows:
        raise HTTPException(404, "unknown review")
    r = rows[0]
    r["entities"] = json.loads(r["entities"] or "[]")
    r["issue"] = q(db(), "SELECT * FROM issues WHERE issue_id = %s", (r["issue_id"],))[0] if r["issue_id"] else None
    r["memo_claims"] = q(db(), "SELECT * FROM claims WHERE issue_id = %s", (r["issue_id"],)) if r["issue_id"] else []
    return r


@api.get("/")
def index():
    return FileResponse(os.path.join(PUBLIC, "index.html"))
