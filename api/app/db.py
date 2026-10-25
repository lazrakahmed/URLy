from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import db_url


@lru_cache
def get_engine():
    return create_engine(
        db_url(),
        pool_size=5,
        max_overflow=5,  # max 10 connections per container
        pool_timeout=5,  # wait at most 5s for a free connection
        hide_parameters=True,  # keep SQL parameters out of SQLAlchemy error text
        pool_pre_ping=True,  # drop dead connections (e.g. after a DB failover)
        connect_args={
            "connect_timeout": 2,
            "options": "-c statement_timeout=5000",
        },
    )


@lru_cache
def _sessionmaker() -> sessionmaker:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def get_session():
    """FastAPI dependency: one session per request."""
    with _sessionmaker()() as session:
        yield session
