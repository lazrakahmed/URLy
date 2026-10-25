from app.config import db_url


def test_url_built_from_five_variables(monkeypatch):
    monkeypatch.setenv("DB_HOST", "db.example")
    monkeypatch.setenv("DB_PORT", "5433")
    monkeypatch.setenv("DB_NAME", "urly")
    monkeypatch.setenv("DB_USER", "app")
    monkeypatch.setenv("DB_PASSWORD", "p@ss:w/ord#?%")
    url = db_url()
    assert (url.host, url.port, url.database, url.username) == ("db.example", 5433, "urly", "app")
    assert url.password == "p@ss:w/ord#?%"
    # special characters must be escaped in the rendered URL
    rendered = url.render_as_string(hide_password=False)
    assert "p@ss:w/ord#?%" not in rendered
    assert rendered.startswith("postgresql+psycopg://app:")
