import logging
import time
import traceback
import uuid
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app import codes
from app.config import base_url
from app.db import get_session
from app.logging_config import log, setup_logging
from app.models import Link

MAX_URL_LENGTH = 2048
MAX_CODE_ATTEMPTS = 5
RETRY_AFTER = {"Retry-After": "5"}

setup_logging()

app = FastAPI(title="URLy", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """One JSON log line per request. Never logs the body or query (the Original URL)."""
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        if request.url.path != "/healthz":  # the ALB probes every few seconds: pure noise
            fields = {
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                # the ALB adds X-Amzn-Trace-Id; locally we make one up
                "request_id": (request.headers.get("x-amzn-trace-id") or uuid.uuid4().hex)[:128],
            }
            for key in ("short_code", "event"):
                if hasattr(request.state, key):
                    fields[key] = getattr(request.state, key)
            # ERROR is reserved for the event lines (db_error...), so alarms count each event once
            level = logging.WARNING if status >= 500 else logging.INFO
            log(level, "request", **fields)


class CreateLink(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def valid_http_url(cls, value: str) -> str:
        value = value.strip()
        if len(value) > MAX_URL_LENGTH:
            raise ValueError(f"URL must be at most {MAX_URL_LENGTH} characters")
        if any(c.isspace() or not c.isprintable() for c in value):
            raise ValueError("URL must not contain spaces or control characters")
        try:
            parts = urlsplit(value)
            host = parts.hostname
            _ = parts.port  # raises ValueError on a non-numeric or out-of-range port
        except ValueError:
            raise ValueError("Not a valid URL")
        if parts.scheme not in ("http", "https") or not host:
            raise ValueError("URL must start with http:// or https:// and include a host")
        return value


def not_found_page() -> HTMLResponse:
    html = (
        "<!doctype html><html lang=en><meta charset=utf-8><title>Not found</title>"
        "<h1>Link not found</h1><p>This short link does not exist.</p>"
    )
    return HTMLResponse(html, status_code=404)


def unavailable(request: Request, message: str) -> JSONResponse | HTMLResponse:
    if request.url.path.startswith("/api/") or request.url.path == "/readyz":
        return JSONResponse({"detail": message}, status_code=503, headers=RETRY_AFTER)
    html = (
        "<!doctype html><html lang=en><meta charset=utf-8><title>Unavailable</title>"
        f"<h1>Temporarily unavailable</h1><p>{message} Please try again in a few seconds.</p>"
    )
    return HTMLResponse(html, status_code=503, headers=RETRY_AFTER)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    # Messages only: never echo the submitted input back (it may not even be encodable).
    errors = [{"field": ".".join(map(str, e["loc"][1:])), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
@app.exception_handler(PoolTimeoutError)
async def database_unavailable(request: Request, exc: Exception):
    # Covers DB down, connection/statement timeout, pool exhausted. Programming
    # and data errors are bugs, not outages: they stay 500.
    # Log the error class and the driver message only: SQLAlchemy's own message can
    # carry the SQL parameters (the Original URL), so str(exc) is never logged.
    driver_error = getattr(exc, "orig", None) or exc
    log(
        logging.ERROR,
        "database unavailable",
        event="db_error",
        error=type(exc).__name__,
        detail=str(driver_error)[:300],
    )
    # No stack trace or driver message goes to the client.
    return unavailable(request, "The service is temporarily unavailable.")


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # Stack frames only (no exception message, which could contain user data).
    frames = [
        f"{f.filename}:{f.lineno} in {f.name}" for f in traceback.extract_tb(exc.__traceback__)
    ]
    log(
        logging.ERROR,
        "unhandled error",
        event="unhandled_error",
        error=type(exc).__name__,
        frames=frames,
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return HTMLResponse("<h1>Something went wrong</h1>", status_code=500)


@app.api_route("/healthz", methods=["GET", "HEAD"])
def healthz():
    """Process is alive. No DB: a DB outage must not make the ALB kill healthy containers."""
    return {"status": "ok"}


@app.api_route("/readyz", methods=["GET", "HEAD"])
def readyz(session: Session = Depends(get_session)):
    """Can the API do its job? Touches the DB (2s limit). For humans and the smoke test."""
    session.execute(text("SET LOCAL statement_timeout = 2000"))
    session.execute(text("SELECT 1"))
    return {"status": "ready"}


@app.post("/api/links", status_code=201)
def create_link(body: CreateLink, request: Request, session: Session = Depends(get_session)):
    short_base = base_url()  # fail before touching the DB if BASE_URL is missing
    # The unique constraint is the arbiter: insert, and on a collision pick a
    # new code and retry. Never check-then-insert (two requests could race).
    for _ in range(MAX_CODE_ATTEMPTS):
        link = Link(short_code=codes.generate_code(), original_url=body.url)
        session.add(link)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            if getattr(exc.orig, "sqlstate", None) != "23505":  # only a duplicate code retries
                raise
            log(logging.WARNING, "short code collision", event="code_collision")
            continue
        request.state.event = "link_created"
        request.state.short_code = link.short_code
        return {
            "short_code": link.short_code,
            "short_url": f"{short_base}/{link.short_code}",
            "original_url": link.original_url,
            "created_at": link.created_at.isoformat(),
        }
    return unavailable(request, "Could not allocate a short code.")


@app.api_route("/{code}", methods=["GET", "HEAD"])
def redirect(code: str, request: Request, session: Session = Depends(get_session)):
    # Anything that cannot be one of our codes is a 404 without touching the DB.
    if not codes.CODE_RE.fullmatch(code):
        return not_found_page()
    original = session.scalar(select(Link.original_url).where(Link.short_code == code))
    if original is None:
        return not_found_page()
    request.state.short_code = code
    # 302, not 301: browsers must not cache it, so every click reaches us.
    return RedirectResponse(original, status_code=302)
