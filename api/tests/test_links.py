import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import codes
from app.db import get_engine, get_session
from app.main import app
from app.models import Link


def create(client, url="https://example.com/some/long/path?x=1"):
    return client.post("/api/links", json={"url": url})


def test_create_returns_201_with_fields(client):
    r = create(client)
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"short_code", "short_url", "original_url", "created_at"}
    assert len(body["short_code"]) == 7 and body["short_code"].isalnum()
    assert body["short_url"] == f"http://short.test/{body['short_code']}"
    assert body["original_url"] == "https://example.com/some/long/path?x=1"


def test_short_url_ignores_host_header(client):
    r = client.post(
        "/api/links", json={"url": "https://example.com"}, headers={"Host": "evil.test"}
    )
    assert r.json()["short_url"].startswith("http://short.test/")


def test_same_url_twice_gives_two_links(client):
    a, b = create(client).json(), create(client).json()
    assert a["short_code"] != b["short_code"]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "javascript:alert(1)",
        "example.com",
        "http://",
        "https:///nohost",
        "",
        "https://example.com/a\x00b",
        "https://exa mple.com",
        "https://example.com/" + "a" * 2048,
    ],
)
def test_invalid_urls_rejected(client, url):
    assert create(client, url).status_code == 422


def test_lone_surrogate_rejected(client):
    raw = b'{"url": "https://example.com/\\ud800"}'
    r = client.post("/api/links", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_url_of_exactly_max_length_accepted(client):
    url = "https://example.com/" + "a" * (2048 - len("https://example.com/"))
    assert len(url) == 2048
    assert create(client, url).status_code == 201


def test_redirect_302_with_location(client):
    code = create(client, "https://example.com/target").json()["short_code"]
    r = client.get(f"/{code}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/target"


def test_head_redirects_too(client):
    code = create(client, "https://example.com/target").json()["short_code"]
    r = client.head(f"/{code}")
    assert r.status_code == 302
    assert r.headers["location"] == "https://example.com/target"


def test_unknown_code_is_html_404(client):
    r = client.get("/abcdefg")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")


def test_malformed_code_is_404(client):
    assert client.get("/not-a-code!").status_code == 404
    assert client.get("/abcdefg%0A").status_code == 404


def test_collision_retries_and_succeeds(client, monkeypatch):
    first = create(client).json()["short_code"]
    sequence = iter([first, first, "Zz9Zz9Z"])  # two collisions, then a free code
    monkeypatch.setattr(codes, "generate_code", lambda: next(sequence))
    r = create(client)
    assert r.status_code == 201
    assert r.json()["short_code"] == "Zz9Zz9Z"


def test_collisions_exhausted_gives_503(client, monkeypatch):
    first = create(client).json()["short_code"]
    monkeypatch.setattr(codes, "generate_code", lambda: first)
    r = create(client)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"


def test_reserved_words_never_issued(monkeypatch):
    monkeypatch.setattr(codes, "RESERVED", {"aaaaaaa"})
    monkeypatch.setattr(codes, "ALPHABET", "Ab")
    seen = {codes.generate_code() for _ in range(200)}
    assert "aaaaaaa" not in {c.lower() for c in seen}


def test_row_persisted(client):
    code = create(client).json()["short_code"]
    with sessionmaker(bind=get_engine())() as s:
        assert s.scalar(select(Link.original_url).where(Link.short_code == code))


@pytest.fixture
def db_down():
    # a session bound to a port nothing listens on
    from app.config import db_url

    engine = create_engine(
        db_url().set(port=1), connect_args={"connect_timeout": 1}, pool_pre_ping=False
    )

    def broken():
        with sessionmaker(bind=engine)() as s:
            yield s

    app.dependency_overrides[get_session] = broken
    yield
    app.dependency_overrides.clear()


def test_db_down_api_gives_json_503(client, db_down):
    r = create(client)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert r.headers["content-type"].startswith("application/json")
    assert "psycopg" not in r.text and "Traceback" not in r.text


def test_db_down_redirect_gives_html_503(client, db_down):
    r = client.get("/abcdefg")
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
    assert r.headers["content-type"].startswith("text/html")


def test_missing_base_url_fails_before_any_insert(monkeypatch):
    monkeypatch.delenv("BASE_URL")
    c = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    assert c.post("/api/links", json={"url": "https://example.com"}).status_code == 500
    with get_engine().connect() as conn:
        assert conn.scalar(text("SELECT count(*) FROM links")) == 0


def test_non_duplicate_integrity_error_is_not_retried(monkeypatch):
    calls = []

    def bad_code():
        calls.append(1)
        # NOT NULL violation, not a duplicate

    monkeypatch.setattr(codes, "generate_code", bad_code)
    c = TestClient(app, follow_redirects=False, raise_server_exceptions=False)
    assert c.post("/api/links", json={"url": "https://example.com"}).status_code == 500
    assert len(calls) == 1


def test_pool_exhausted_gives_503(client):
    from app.config import db_url

    engine = create_engine(db_url(), pool_size=1, max_overflow=0, pool_timeout=0.5)
    held = engine.connect()  # takes the only connection

    def starved():
        with sessionmaker(bind=engine)() as s:
            yield s

    app.dependency_overrides[get_session] = starved
    try:
        r = client.get("/abcdefg")
    finally:
        app.dependency_overrides.clear()
        held.close()
    assert r.status_code == 503
    assert r.headers["retry-after"] == "5"
