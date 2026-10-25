import os
from pathlib import Path

# Tests run against a real Postgres. Defaults match the throwaway container:
#   docker run -d -e POSTGRES_USER=urly -e POSTGRES_PASSWORD=urly-test \
#     -e POSTGRES_DB=urly_test -p 5432:5432 postgres:17
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_PORT", "5432")
os.environ.setdefault("DB_NAME", "urly_test")
os.environ.setdefault("DB_USER", "urly")
os.environ.setdefault("DB_PASSWORD", "urly-test")
os.environ.setdefault("BASE_URL", "http://short.test")

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import get_engine
from app.main import app

# Safety: the tests wipe the links table.
assert os.environ["DB_NAME"].endswith("_test"), "refusing to run tests on a non-test database"


@pytest.fixture(scope="session", autouse=True)
def migrated():
    api_dir = Path(__file__).resolve().parent.parent  # independent of the working directory
    cfg = Config(str(api_dir / "alembic.ini"))
    cfg.set_main_option("script_location", str(api_dir / "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
def clean_table(migrated):
    with get_engine().begin() as conn:
        conn.execute(text("TRUNCATE links RESTART IDENTITY"))


@pytest.fixture
def client():
    # follow_redirects=False so we can assert on the 302 itself
    return TestClient(app, follow_redirects=False)
