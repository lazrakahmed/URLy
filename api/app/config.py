"""Configuration, read from environment variables only.

The database is described by five separate variables (not one DATABASE_URL)
because ECS can inject a secret as a whole variable but cannot build a URL
from pieces. The URL is assembled here.
"""

import os

from sqlalchemy.engine import URL


def db_url() -> URL:
    # URL.create escapes special characters in the password for us.
    return URL.create(
        "postgresql+psycopg",
        username=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        database=os.environ["DB_NAME"],
    )


def base_url() -> str:
    # Never derived from the Host header: a client could forge it.
    # No default on purpose: a forgotten value must fail loudly, not hand out
    # localhost links in production.
    return os.environ["BASE_URL"].rstrip("/")
