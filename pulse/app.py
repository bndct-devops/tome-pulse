"""tome-pulse: the receiving end of Tome's opt-in telemetry.

One small anonymous report per instance per month comes in, aggregates go
out. The whole service is this file.

What is stored: the report as sent, keyed by instance id and month, and the
day it was first seen. What is not stored: the sender's IP address, any
header, anything else. Rows older than RETENTION_MONTHS are pruned.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from typing_extensions import Annotated

DB_PATH = Path(os.environ.get("PULSE_DB", "/data/pulse.db"))
# Reports are a few hundred bytes. Anything bigger is not a Tome instance.
MAX_BODY_BYTES = int(os.environ.get("PULSE_MAX_BODY", "8192"))
# Ceiling on instances seen for the first time in one UTC day. A flood of
# fake ids hits this and is dropped; real growth never comes close.
NEW_INSTANCES_PER_DAY = int(os.environ.get("PULSE_NEW_PER_DAY", "500"))
RETENTION_MONTHS = int(os.environ.get("PULSE_RETENTION_MONTHS", "12"))
# An instance counts as active while it has reported within this window.
ACTIVE_DAYS = 90
AGGREGATE_TTL_SECONDS = 300

SCHEMAS_ACCEPTED = {1}

Bucket = Annotated[str, StringConstraints(pattern=r"^(0|\d+|\d+-\d+|\d+\+)$", max_length=16)]
Short = Annotated[str, StringConstraints(min_length=1, max_length=64)]

FEATURE_KEYS = (
    "koreader_sync", "web_reader", "kindle_sync_code", "send_to_device", "bindery", "wishlist",
    "hardcover", "opds", "sso_users", "api_tokens", "reading_goals", "series_ratings", "arcs",
)
Share = dict[Annotated[str, StringConstraints(min_length=1, max_length=24)], Annotated[float, Field(ge=0, le=1)]]


class Features(BaseModel):
    """Which features were alive in the last 30 days. One boolean each."""
    model_config = ConfigDict(extra="forbid")
    koreader_sync: bool
    web_reader: bool
    kindle_sync_code: bool
    send_to_device: bool
    bindery: bool
    wishlist: bool
    hardcover: bool
    opds: bool
    sso_users: bool
    api_tokens: bool
    reading_goals: bool
    series_ratings: bool
    arcs: bool


class Reading(BaseModel):
    """Reading activity in the last 30 days, instance-wide, bucketed."""
    model_config = ConfigDict(extra="forbid")
    sessions: Bucket
    hours: Bucket
    pages: Bucket
    finished: Bucket


class Report(BaseModel):
    """Schema 1. Mirrors backend/services/telemetry.py in Tome exactly;
    anything outside it is rejected rather than stored."""
    model_config = ConfigDict(extra="forbid")
    schema_: Literal[1] = Field(alias="schema")
    instance: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]
    version: Short
    plugin_build: int = Field(ge=0, le=100000)
    platform: Short
    docker: bool
    install_age: Bucket
    users: Bucket
    readers_30d: Bucket
    books: Bucket
    libraries: Bucket
    book_types: Share = Field(max_length=8)
    formats: Share = Field(max_length=8)
    languages: Share = Field(max_length=5)
    reading_30d: Reading
    highlights: Bucket
    features_30d: Features


_lock = threading.Lock()
_aggregate_cache: tuple[float, dict] | None = None


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reports (
                instance TEXT NOT NULL,
                month    TEXT NOT NULL,          -- YYYY-MM, UTC, of receipt
                received TEXT NOT NULL,          -- ISO timestamp, UTC
                body     TEXT NOT NULL,          -- the report as JSON
                PRIMARY KEY (instance, month)
            );
            CREATE TABLE IF NOT EXISTS instances (
                instance   TEXT PRIMARY KEY,
                first_seen TEXT NOT NULL         -- YYYY-MM-DD, UTC
            );
            CREATE INDEX IF NOT EXISTS reports_month ON reports(month);
            """
        )


def prune(conn: sqlite3.Connection, today: date) -> None:
    cutoff = (today.replace(day=1) - timedelta(days=RETENTION_MONTHS * 31)).strftime("%Y-%m")
    conn.execute("DELETE FROM reports WHERE month < ?", (cutoff,))


@asynccontextmanager
async def _lifespan(_: FastAPI):
    init_db()
    with db() as conn:
        prune(conn, datetime.now(timezone.utc).date())
    yield


app = FastAPI(title="tome-pulse", docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/v1/report", status_code=204)
async def receive(request: Request) -> Response:
    length = request.headers.get("content-length")
    if length and int(length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="report too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="report too large")
    try:
        report = Report.model_validate_json(raw)
    except ValueError as e:
        raise HTTPException(status_code=422, detail="not a Tome report") from e
    if report.schema_ not in SCHEMAS_ACCEPTED:
        raise HTTPException(status_code=422, detail="unknown schema")

    now = datetime.now(timezone.utc)
    today, month = now.date().isoformat(), now.strftime("%Y-%m")
    body = json.dumps(report.model_dump(by_alias=True), separators=(",", ":"), sort_keys=True)

    with _lock, db() as conn:
        known = conn.execute("SELECT 1 FROM instances WHERE instance = ?", (report.instance,)).fetchone()
        if not known:
            new_today = conn.execute("SELECT COUNT(*) FROM instances WHERE first_seen = ?", (today,)).fetchone()[0]
            if new_today >= NEW_INSTANCES_PER_DAY:
                # Dropped silently: the sender gets a 204 either way, so a
                # flood learns nothing about the ceiling.
                return Response(status_code=204)
            conn.execute("INSERT INTO instances (instance, first_seen) VALUES (?, ?)", (report.instance, today))
        conn.execute(
            "INSERT INTO reports (instance, month, received, body) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(instance, month) DO UPDATE SET received = excluded.received, body = excluded.body",
            (report.instance, month, now.isoformat(timespec="seconds"), body),
        )
    return Response(status_code=204)


def _share(counter: dict[str, int], total: int, top: int | None = None) -> dict[str, float]:
    items = sorted(counter.items(), key=lambda kv: -kv[1])
    if top:
        items = items[:top]
    return {k: round(v / total, 3) for k, v in items} if total else {}


def compute_aggregate(now: datetime) -> dict:
    """Latest report per active instance, counted. No per-instance data leaves."""
    since = (now - timedelta(days=ACTIVE_DAYS)).isoformat(timespec="seconds")
    with db() as conn:
        rows = conn.execute(
            "SELECT r.instance, r.body FROM reports r "
            "JOIN (SELECT instance, MAX(received) AS latest FROM reports WHERE received >= ? GROUP BY instance) l "
            "ON l.instance = r.instance AND l.latest = r.received",
            (since,),
        ).fetchall()
        this_month = conn.execute("SELECT COUNT(DISTINCT instance) FROM reports WHERE month = ?", (now.strftime("%Y-%m"),)).fetchone()[0]
        new_30d = conn.execute("SELECT COUNT(*) FROM instances WHERE first_seen >= ?", ((now - timedelta(days=30)).date().isoformat(),)).fetchone()[0]

    total = len(rows)
    share_counters: dict[str, dict[str, int]] = {k: {} for k in (
        "versions", "platforms", "plugin_builds", "install_age", "users", "readers_30d", "books",
        "sessions_30d", "hours_30d", "pages_30d", "finished_30d", "highlights",
    )}
    docker = 0
    features: dict[str, int] = {k: 0 for k in FEATURE_KEYS}
    mix_sums: dict[str, dict[str, float]] = {"book_types": {}, "formats": {}, "languages": {}}
    hours_sum = 0.0

    def count(key: str, value: str) -> None:
        share_counters[key][value] = share_counters[key].get(value, 0) + 1

    for _, body in rows:
        r = json.loads(body)
        count("versions", r["version"])
        count("platforms", r["platform"])
        count("plugin_builds", str(r["plugin_build"]))
        count("install_age", r["install_age"])
        count("users", r["users"])
        count("readers_30d", r["readers_30d"])
        count("books", r["books"])
        rd = r["reading_30d"]
        count("sessions_30d", rd["sessions"])
        count("hours_30d", rd["hours"])
        count("pages_30d", rd["pages"])
        count("finished_30d", rd["finished"])
        count("highlights", r["highlights"])
        hours_sum += bucket_midpoint(rd["hours"])
        docker += 1 if r["docker"] else 0
        for k, v in r["features_30d"].items():
            if v and k in features:
                features[k] += 1
        for mix in mix_sums:
            for name, share in r[mix].items():
                mix_sums[mix][name] = mix_sums[mix].get(name, 0.0) + float(share)

    out = {
        "generated": now.isoformat(timespec="seconds"),
        "active_instances": total,
        "reported_this_month": this_month,
        "new_instances_30d": new_30d,
        # Hours are buckets, so this is the sum of bucket midpoints: a fair
        # estimate, labelled as one on the public page.
        "hours_read_30d_estimate": int(hours_sum),
        "docker": round(docker / total, 3) if total else 0.0,
        "features_30d": {k: (round(v / total, 3) if total else 0.0) for k, v in sorted(features.items())},
    }
    for key, counter in share_counters.items():
        out[key] = _share(counter, total, top=10 if key in ("versions", "plugin_builds") else None)
    for mix, sums in mix_sums.items():
        out[mix] = {k: round(v / total, 3) for k, v in sorted(sums.items(), key=lambda kv: -kv[1])[:8]} if total else {}
    return out


def bucket_midpoint(b: str) -> float:
    """"26-50" -> 38, "5" -> 5, "10000+" -> 10000, "0" -> 0."""
    if b.endswith("+"):
        return float(b[:-1])
    if "-" in b:
        lo, hi = b.split("-", 1)
        return (int(lo) + int(hi)) / 2
    return float(b)


@app.get("/v1/aggregate")
def aggregate(response: Response) -> dict:
    global _aggregate_cache
    now = time.time()
    if _aggregate_cache and now - _aggregate_cache[0] < AGGREGATE_TTL_SECONDS:
        data = _aggregate_cache[1]
    else:
        data = compute_aggregate(datetime.now(timezone.utc))
        _aggregate_cache = (now, data)
    response.headers["Cache-Control"] = f"public, max-age={AGGREGATE_TTL_SECONDS}"
    response.headers["Access-Control-Allow-Origin"] = "*"
    return data
