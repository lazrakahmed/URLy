from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session

from app import codes
from app.config import base_url
from app.db import get_session
from app.models import Link

MAX_URL_LENGTH = 2048
MAX_CODE_ATTEMPTS = 5
RETRY_AFTER = {"Retry-After": "5"}

app = FastAPI(title="URLy", docs_url=None, redoc_url=None, openapi_url=None)


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
    if request.url.path.startswith("/api/"):
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
    # No stack trace or driver message goes to the client.
    return unavailable(request, "The service is temporarily unavailable.")


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
            continue
        return {
            "short_code": link.short_code,
            "short_url": f"{short_base}/{link.short_code}",
            "original_url": link.original_url,
            "created_at": link.created_at.isoformat(),
        }
    return unavailable(request, "Could not allocate a short code.")


@app.api_route("/{code}", methods=["GET", "HEAD"])
def redirect(code: str, session: Session = Depends(get_session)):
    # Anything that cannot be one of our codes is a 404 without touching the DB.
    if not codes.CODE_RE.fullmatch(code):
        return not_found_page()
    original = session.scalar(select(Link.original_url).where(Link.short_code == code))
    if original is None:
        return not_found_page()
    # 302, not 301: browsers must not cache it, so every click reaches us.
    return RedirectResponse(original, status_code=302)
