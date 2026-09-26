"""Secret scrubbing for logs, stored error strings and error responses (invariant 4).

Credentials are never logged on purpose; this module is the backstop for the
accidental cases: an exception whose message quotes a request body, a
library that logs headers, a validation error that echoes its input.

`redact(text)` replaces, in order:
1. PEM blocks (`-----BEGIN ... -----` through `-----END ... -----`, or to the
   end of the base64 body when the END line was cut off);
2. values of known secret field names (`secret=...`, `"password": "..."`,
   `Authorization: Bearer ...`, `api-sign: ...`);
3. long base64-ish runs (>= 32 chars mixing upper, lower and digits: Fernet
   keys/tokens, Kraken secrets, JWTs, OAuth tokens);
4. long hex runs (>= 40 hex digits: hex secrets, 0x private keys), except
   0x-prefixed 40-digit wallet addresses, which are public.

`install()` applies it to every log record the process creates (a record
factory, so it covers handlers and loggers configured later, e.g. uvicorn's)
and adds `RedactingFilter` to the root handlers. The same filter goes on the
error tracker's handler when one is added. `install_error_handlers(app)`
stops FastAPI's 422 responses from echoing request bodies and logs unhandled
errors (scrubbed) behind a generic 500.
"""

import logging
import re
from collections.abc import Callable
from typing import Any

REDACTED = "[REDACTED]"

_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]+-----"
    r"(?:[\s\S]*?-----END [A-Z0-9 ]+-----|(?:[A-Za-z0-9+/=\s]|\\n|\\r)*)"
)

_SECRET_NAMES = (
    r"[\w-]*(?:secret|passphrase|password|passwd|token|private[_-]?key(?:[_-]?pem)?"
    r"|api[_-]?key|signature)|authorization|api-sign|cookie|set-cookie"
)
# name, optional closing quote, separator, optional opening quote, value. A
# %-placeholder is not a value: "token=%s" must keep its arg slot.
_FIELD = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(" + _SECRET_NAMES + r")(['\"]?\s*[:=]\s*['\"]?)"
    r"((?:bearer|basic)\s+)?(?!\[REDACTED)(?!%(?:\(\w+\))?[-#0 +]*\d*(?:\.\d+)?[a-zA-Z%])"
    r"([^'\"\s,;&}\]]+)"
)
_B64_RUN = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")
_HEX_RUN = re.compile(r"(?<![0-9A-Za-z])(0x)?([0-9a-fA-F]{40,})(?![0-9A-Za-z])")

_extra: list[re.Pattern[str]] = []


def add_pattern(pattern: str | re.Pattern[str]) -> None:
    """Register an extra secret shape (e.g. the auth module's TOTP base32)."""
    _extra.append(re.compile(pattern) if isinstance(pattern, str) else pattern)


def _field_sub(m: re.Match[str]) -> str:
    return f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{REDACTED}"


def _plain_segment(seg: str) -> bool:
    """A word-like path segment ("koine", "Users"): letters only, at most a
    leading capital. Random base64 almost never yields one of 3+ chars."""
    return len(seg) >= 3 and seg.isalpha() and seg[1:].islower()


def _looks_like_path(body: str) -> bool:
    """File or URL path, not a key. Judged by its segments, not by a leading
    "/" or a slash count alone: 1 in 64 base64 secrets starts with "/", and
    short ones often hold several slashes."""
    segments = [s for s in body.split("/") if s]
    if body.count("/") < 2 or any(len(s) > 24 for s in segments):
        return False
    return 2 * sum(_plain_segment(s) for s in segments) >= len(segments)


def _b64_sub(m: re.Match[str]) -> str:
    s = m.group(0)
    body = s.strip("=")
    if _looks_like_path(body):
        return s  # a file path or URL path, not a key
    if (
        any(c.isupper() for c in body)
        and any(c.islower() for c in body)
        and any(c.isdigit() for c in body)
    ):
        return REDACTED
    return s


def _hex_sub(m: re.Match[str]) -> str:
    if m.group(1) and len(m.group(2)) == 40:
        return m.group(0)  # an EVM address: public by construction
    return REDACTED


def redact(text: str) -> str:
    if not text:
        return text
    out = _PEM.sub("[REDACTED PEM]", text)
    out = _FIELD.sub(_field_sub, out)
    out = _B64_RUN.sub(_b64_sub, out)
    out = _HEX_RUN.sub(_hex_sub, out)
    for pattern in _extra:
        out = pattern.sub(REDACTED, out)
    return out


def redact_value(value: Any) -> Any:
    """Redact a log argument while keeping its type when nothing changes
    (formatters such as uvicorn's access log unpack args positionally)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, int | float | bool) or value is None:
        return value
    text = str(value)
    cleaned = redact(text)
    return value if cleaned == text else cleaned


def redact_record(record: logging.LogRecord) -> logging.LogRecord:
    """Scrub one record in place: message, args, exception and stack text."""
    if isinstance(record.msg, str):
        record.msg = redact(record.msg)
    elif record.msg is not None:
        text = str(record.msg)
        cleaned = redact(text)
        if cleaned != text:
            record.msg = cleaned
    if record.args:
        if isinstance(record.args, dict):
            record.args = {k: redact_value(v) for k, v in record.args.items()}
        else:
            record.args = tuple(redact_value(a) for a in record.args)
        # A secret straddling the format string and an argument is only
        # visible once merged: flatten in that (rare) case.
        try:
            merged = record.getMessage()
        except Exception:  # noqa: BLE001 - malformed format; let logging report it
            merged = None
        if merged is not None and redact(merged) != merged:
            record.msg, record.args = redact(merged), ()
    if record.exc_info and record.exc_info[0] is not None:
        record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        record.exc_info = None  # handlers now print the scrubbed exc_text
    elif record.exc_text:
        record.exc_text = redact(record.exc_text)
    if record.stack_info:
        record.stack_info = redact(record.stack_info)
    return record


class RedactingFilter(logging.Filter):
    """Handler filter: scrubs and always lets the record through."""

    def filter(self, record: logging.LogRecord) -> bool:
        redact_record(record)
        return True


_installed_factory: Callable[..., logging.LogRecord] | None = None


def install() -> None:
    """Idempotent: scrub every record at creation, and filter root handlers."""
    global _installed_factory
    current = logging.getLogRecordFactory()
    if current is not _installed_factory:
        base = current

        def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            return redact_record(base(*args, **kwargs))

        _installed_factory = factory
        logging.setLogRecordFactory(factory)
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())


def install_error_handlers(app: Any) -> None:
    """422s list what failed and where, never the submitted values: FastAPI's
    default echoes `input`, which for a model-level error is the whole body
    (a connect request's PEM included)."""
    from fastapi.encoders import jsonable_encoder
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    async def validation_handler(request: Any, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for err in exc.errors():
            clean = {k: v for k, v in err.items() if k not in ("input", "ctx", "url")}
            if "msg" in clean:
                clean["msg"] = redact(str(clean["msg"]))
            errors.append(clean)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    async def unhandled_handler(request: Any, exc: Exception) -> JSONResponse:
        # Logged through the scrubbing record factory; the client only ever
        # gets a generic body. Starlette re-raises afterwards for the server.
        logging.getLogger("alphamarket.errors").error(
            "unhandled error on %s %s", request.method, request.url.path, exc_info=exc
        )
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

    app.add_exception_handler(RequestValidationError, validation_handler)
    app.add_exception_handler(Exception, unhandled_handler)
