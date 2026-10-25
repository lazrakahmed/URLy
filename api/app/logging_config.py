"""One JSON object per line on stdout; Fargate's awslogs driver ships it to CloudWatch."""

import json
import logging
import sys
from datetime import UTC, datetime

logger = logging.getLogger("urly")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, default=str)


class ScrubTraceback(logging.Filter):
    """Drop exception text from uvicorn's own error log.

    Postgres puts the failing row (including the Original URL) in its error detail, so
    a traceback message can leak user data. Our `unhandled_error` event already records
    the exception class and stack frames without the message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            name = record.exc_info[0].__name__
            record.msg = f"{record.getMessage()} ({name}; message suppressed, see unhandled_error)"
            record.args = None
            record.exc_info = None
            record.exc_text = None
        return True


def setup_logging() -> None:
    if logger.handlers:
        return
    logging.getLogger("uvicorn.error").addFilter(ScrubTraceback())
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def log(level: int, message: str, **fields) -> None:
    logger.log(level, message, extra={"fields": fields})
