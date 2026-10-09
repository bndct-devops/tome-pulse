import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_DB", str(tmp_path / "pulse.db"))
    monkeypatch.setenv("PULSE_NEW_PER_DAY", "3")
    import importlib
    import pulse.app as app_module
    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        yield c, app_module


def report(**over):
    base = {
        "schema": 1,
        "instance": str(uuid.uuid4()),
        "version": "2.6.0",
        "plugin_build": 47,
        "platform": "linux/x86_64",
        "docker": True,
        "install_age": "6-12",
        "users": "2-5",
        "readers_30d": "2-5",
        "books": "251-500",
        "libraries": "3-5",
        "book_types": {"manga": 0.55, "light_novels": 0.3, "novels": 0.15},
        "formats": {"epub": 0.55, "cbz": 0.45},
        "languages": {"en": 0.9, "de": 0.1},
        "reading_30d": {"sessions": "101-250", "hours": "26-50", "pages": "2501-5000", "finished": "2-5"},
        "highlights": "101-250",
        "features_30d": {
            "koreader_sync": True, "web_reader": True, "kindle_sync_code": False, "send_to_device": False,
            "bindery": True, "wishlist": True, "hardcover": True, "opds": False, "sso_users": False,
            "api_tokens": True, "reading_goals": False, "series_ratings": True, "arcs": True,
        },
    }
    base.update(over)
    return base


def test_accepts_a_report_and_counts_it(client):
    c, _ = client
    assert c.post("/v1/report", json=report()).status_code == 204
    agg = c.get("/v1/aggregate").json()
    assert agg["active_instances"] == 1
    assert agg["versions"] == {"2.6.0": 1.0}
    assert agg["features_30d"]["koreader_sync"] == 1.0
    assert agg["features_30d"]["opds"] == 0.0
    assert agg["docker"] == 1.0
    assert agg["hours_read_30d_estimate"] == 38      # midpoint of 26-50
    assert agg["book_types"]["manga"] == 0.55
    assert agg["hours_30d"] == {"26-50": 1.0}


def test_one_row_per_instance_per_month(client):
    c, mod = client
    inst = str(uuid.uuid4())
    c.post("/v1/report", json=report(instance=inst, version="2.5.0"))
    c.post("/v1/report", json=report(instance=inst, version="2.6.0"))
    mod._aggregate_cache = None
    agg = c.get("/v1/aggregate").json()
    assert agg["active_instances"] == 1
    assert agg["versions"] == {"2.6.0": 1.0}


def test_rejects_anything_that_is_not_the_schema(client):
    c, _ = client
    assert c.post("/v1/report", json=report(title="Berserk")).status_code == 422          # extra field
    assert c.post("/v1/report", json=report(instance="not-a-uuid")).status_code == 422
    assert c.post("/v1/report", json=report(users="lots")).status_code == 422
    assert c.post("/v1/report", json=report(schema=2)).status_code == 422
    bad = report(); bad["features_30d"]["calendar"] = True
    assert c.post("/v1/report", json=bad).status_code == 422
    bad = report(); bad["reading_30d"]["streak"] = "5"
    assert c.post("/v1/report", json=bad).status_code == 422
    bad = report(); bad["languages"] = {"en": 1.5}
    assert c.post("/v1/report", json=bad).status_code == 422
    missing = report(); del missing["highlights"]
    assert c.post("/v1/report", json=missing).status_code == 422
    assert c.post("/v1/report", content=b"x" * 9000, headers={"content-type": "application/json"}).status_code == 413


def test_new_instance_ceiling_drops_silently(client):
    c, mod = client
    for _ in range(5):
        assert c.post("/v1/report", json=report()).status_code == 204
    mod._aggregate_cache = None
    assert c.get("/v1/aggregate").json()["active_instances"] == 3


def test_stores_nothing_but_the_report(client):
    c, mod = client
    c.post("/v1/report", json=report(), headers={"x-forwarded-for": "203.0.113.9", "user-agent": "curl"})
    with mod.db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(reports)")]
        (body,) = conn.execute("SELECT body FROM reports").fetchone()
    assert cols == ["instance", "month", "received", "body"]
    assert "203.0.113.9" not in body and "curl" not in body
    assert set(json.loads(body)) == set(report())


def test_prunes_old_months(client):
    c, mod = client
    with mod.db() as conn:
        conn.execute("INSERT INTO reports VALUES (?, ?, ?, ?)", (str(uuid.uuid4()), "2024-01", "2024-01-15T00:00:00+00:00", "{}"))
        mod.prune(conn, datetime.now(timezone.utc).date())
        assert conn.execute("SELECT COUNT(*) FROM reports WHERE month = '2024-01'").fetchone()[0] == 0


def test_inactive_instances_drop_out_of_the_aggregate(client):
    c, mod = client
    old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat(timespec="seconds")
    with mod.db() as conn:
        conn.execute("INSERT INTO reports VALUES (?, ?, ?, ?)", (str(uuid.uuid4()), "2026-06", old, json.dumps(report())))
    mod._aggregate_cache = None
    assert c.get("/v1/aggregate").json()["active_instances"] == 0


def test_bucket_midpoint():
    from pulse.app import bucket_midpoint
    assert bucket_midpoint("0") == 0 and bucket_midpoint("1") == 1
    assert bucket_midpoint("26-50") == 38 and bucket_midpoint("10000+") == 10000
