import json
import logging

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import codes
from app.db import get_session
from app.logging_config import JsonFormatter
from app.main import app

SECRET_URL = "https://example.com/reset?token=SUPER-SECRET-TOKEN-123"


@pytest.fixture
def db_down():
    from app.config import db_url

    engine = create_engine(
        db_url().set(port=1),
        connect_args={"connect_timeout": 1},
        hide_parameters=True,
    )

    def broken():
        with sessionmaker(bind=engine)() as s:
            yield s

    app.dependency_overrides[get_session] = broken
    yield
    app.dependency_overrides.clear()


def request_lines(caplog):
    return [r.fields for r in caplog.records if r.getMessage() == "request"]


def test_healthz_ok_without_database(client, db_down):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_readyz_ok_with_database(client):
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "ready"}


def test_readyz_503_json_when_database_down(client, db_down):
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert r.headers["content-type"].startswith("application/json")


def test_health_routes_are_not_swallowed_by_code_route(client):
    # "healthz" is 7 letters, so it also looks like a short code: route order must win
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_request_log_line_has_expected_fields(client, caplog):
    caplog.set_level(logging.INFO, logger="urly")
    code = client.post("/api/links", json={"url": SECRET_URL}).json()["short_code"]
    client.get(f"/{code}", headers={"X-Amzn-Trace-Id": "Root=1-abc"})
    created, redirected = request_lines(caplog)
    assert created["method"] == "POST" and created["status"] == 201
    assert created["event"] == "link_created" and created["short_code"] == code
    assert redirected["status"] == 302 and redirected["short_code"] == code
    assert redirected["path"] == f"/{code}" and redirected["request_id"] == "Root=1-abc"
    assert isinstance(redirected["duration_ms"], float)


def test_healthz_is_not_logged(client, caplog):
    caplog.set_level(logging.INFO, logger="urly")
    client.get("/healthz")
    assert request_lines(caplog) == []


def test_formatter_emits_valid_json(client, caplog):
    caplog.set_level(logging.INFO, logger="urly")
    client.get("/readyz")
    record = next(r for r in caplog.records if r.getMessage() == "request")
    parsed = json.loads(JsonFormatter().format(record))
    assert {"timestamp", "level", "message", "method", "path", "status"} <= set(parsed)


def test_original_url_never_logged(client, caplog):
    caplog.set_level(logging.DEBUG)
    code = client.post("/api/links", json={"url": SECRET_URL}).json()["short_code"]
    client.get(f"/{code}")
    assert "SUPER-SECRET" not in caplog.text


def test_db_outage_logs_event_without_url(client, db_down, caplog):
    caplog.set_level(logging.DEBUG)
    assert client.post("/api/links", json={"url": SECRET_URL}).status_code == 503
    events = [
        r.fields["event"] for r in caplog.records if hasattr(r, "fields") and "event" in r.fields
    ]
    assert "db_error" in events
    assert "SUPER-SECRET" not in caplog.text


def test_collision_logs_event(client, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="urly")
    first = client.post("/api/links", json={"url": "https://example.com"}).json()["short_code"]
    sequence = iter([first, "Zz9Zz9Z"])
    monkeypatch.setattr(codes, "generate_code", lambda: next(sequence))
    client.post("/api/links", json={"url": "https://example.com"})
    events = [r.fields.get("event") for r in caplog.records if hasattr(r, "fields")]
    assert "code_collision" in events


def test_unhandled_error_logs_without_message(monkeypatch, caplog):
    from fastapi.testclient import TestClient

    caplog.set_level(logging.INFO, logger="urly")
    monkeypatch.setattr(codes, "generate_code", lambda: None)  # NOT NULL violation
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/api/links", json={"url": SECRET_URL})
    assert r.status_code == 500
    assert "SUPER-SECRET" not in caplog.text


def test_uvicorn_traceback_message_is_scrubbed():
    from app.logging_config import ScrubTraceback

    try:
        raise ValueError(f"Failing row contains ({SECRET_URL})")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "uvicorn.error",
            logging.ERROR,
            __file__,
            1,
            "Exception in ASGI application",
            None,
            sys.exc_info(),
        )
    ScrubTraceback().filter(record)
    text = JsonFormatter().format(record)
    assert "SUPER-SECRET" not in text and "ValueError" in text


def test_health_routes_answer_head(client, caplog):
    caplog.set_level(logging.INFO, logger="urly")
    assert client.head("/healthz").status_code == 200
    assert client.head("/readyz").status_code == 200
    assert all(f["path"] != "/healthz" for f in request_lines(caplog))


def test_request_id_is_length_capped(client, caplog):
    caplog.set_level(logging.INFO, logger="urly")
    client.get("/readyz", headers={"X-Amzn-Trace-Id": "x" * 5000})
    assert len(request_lines(caplog)[0]["request_id"]) == 128
