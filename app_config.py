"""Explicit startup configuration for the shared application factory.

Two database modes are supported:

    demo    Read-only synthetic data. This is the default because the demo
            dataset must never receive an upload or any other mutation.
    upload  A local, writable database selected by an explicit path. The path
            must be supplied by the caller or by REVENGG_UPLOAD_DB_PATH. There
            is no fallback to the demo database.

Importing this module or the application factory never creates or initialises
a database. Only the upload-mode lifespan opens a writable database.
"""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DEMO_DB = BASE_DIR / "demo.db"
DEFAULT_UPLOAD_DIR = BASE_DIR / "uploads"

MODE_DEMO = "demo"
MODE_UPLOAD = "upload"

DEMO_DB_ENV = "REVENGG_DEMO_DB_PATH"
UPLOAD_DB_ENV = "REVENGG_UPLOAD_DB_PATH"
UPLOAD_DIR_ENV = "REVENGG_UPLOAD_DIR"
MODE_ENV = "REVENGG_MODE"
SESSION_COOKIE_SECURE_ENV = "REVENGG_SESSION_COOKIE_SECURE"
SESSION_TTL_ENV = "REVENGG_SESSION_TTL_SECONDS"
UPLOAD_MAX_BYTES_ENV = "REVENGG_UPLOAD_MAX_BYTES"
UPLOAD_MAX_PAGES_ENV = "REVENGG_UPLOAD_MAX_PAGES"
UPLOAD_MAX_EXTRACTED_CHARS_ENV = "REVENGG_UPLOAD_MAX_EXTRACTED_CHARS"
UPLOAD_MAX_PROCESSING_SECONDS_ENV = "REVENGG_UPLOAD_MAX_PROCESSING_SECONDS"

DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60

# Local/test defaults only. Production upload limits remain an open policy
# decision (OQ-04); these values are configurable and must not be represented
# as approved production policy.
DEFAULT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_UPLOAD_MAX_PAGES = 200
DEFAULT_UPLOAD_MAX_EXTRACTED_CHARS = 1_000_000
DEFAULT_UPLOAD_MAX_PROCESSING_SECONDS = 30


class ConfigurationError(RuntimeError):
    """Raised when the requested database mode is not safely configured."""


@dataclass(frozen=True)
class AppSettings:
    """Immutable settings shared by every route and by the lifespan."""

    mode: str
    database_path: Path
    database_read_only: bool
    uploads_enabled: bool
    upload_dir: Path | None
    session_cookie_secure: bool = True
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    upload_max_bytes: int = DEFAULT_UPLOAD_MAX_BYTES
    upload_max_pages: int = DEFAULT_UPLOAD_MAX_PAGES
    upload_max_extracted_chars: int = DEFAULT_UPLOAD_MAX_EXTRACTED_CHARS
    upload_max_processing_seconds: int = DEFAULT_UPLOAD_MAX_PROCESSING_SECONDS


def _resolved_path(value) -> Path:
    return Path(value).expanduser().resolve()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"{name} must be a boolean (true/false), got {raw!r}."
    )


def _session_settings() -> tuple[bool, int]:
    secure = _env_bool(SESSION_COOKIE_SECURE_ENV, True)
    raw_ttl = os.environ.get(SESSION_TTL_ENV, "").strip()
    if not raw_ttl:
        ttl = DEFAULT_SESSION_TTL_SECONDS
    else:
        try:
            ttl = int(raw_ttl)
        except ValueError as exc:
            raise ConfigurationError(
                f"{SESSION_TTL_ENV} must be an integer number of seconds."
            ) from exc
        if ttl <= 0:
            raise ConfigurationError(
                f"{SESSION_TTL_ENV} must be greater than zero."
            )
    return secure, ttl


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            f"{name} must be an integer, got {raw!r}."
        ) from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be greater than zero.")
    return value


def _upload_limits() -> tuple[int, int, int, int]:
    return (
        _positive_int_env(UPLOAD_MAX_BYTES_ENV, DEFAULT_UPLOAD_MAX_BYTES),
        _positive_int_env(UPLOAD_MAX_PAGES_ENV, DEFAULT_UPLOAD_MAX_PAGES),
        _positive_int_env(
            UPLOAD_MAX_EXTRACTED_CHARS_ENV,
            DEFAULT_UPLOAD_MAX_EXTRACTED_CHARS,
        ),
        _positive_int_env(
            UPLOAD_MAX_PROCESSING_SECONDS_ENV,
            DEFAULT_UPLOAD_MAX_PROCESSING_SECONDS,
        ),
    )


def _demo_path() -> Path:
    configured = os.environ.get(DEMO_DB_ENV, "").strip()
    return _resolved_path(configured or DEFAULT_DEMO_DB)


def load_settings(mode=None, database_path=None, upload_dir=None) -> AppSettings:
    """Build settings from explicit arguments and environment variables.

    An explicit database_path with no mode selects upload mode; that path is
    therefore intentional rather than a silent fallback. Upload mode always
    requires such a path and always rejects the synthetic demo database.
    """
    selected_mode = (mode or os.environ.get(MODE_ENV, "")).strip().lower()
    if not selected_mode:
        selected_mode = MODE_UPLOAD if database_path is not None else MODE_DEMO

    demo_path = _demo_path()
    session_cookie_secure, session_ttl_seconds = _session_settings()

    if selected_mode == MODE_DEMO:
        chosen = database_path or os.environ.get(DEMO_DB_ENV, "").strip() or demo_path
        return AppSettings(
            mode=MODE_DEMO,
            database_path=_resolved_path(chosen),
            database_read_only=True,
            uploads_enabled=False,
            upload_dir=None,
            session_cookie_secure=session_cookie_secure,
            session_ttl_seconds=session_ttl_seconds,
        )

    if selected_mode == MODE_UPLOAD:
        chosen = database_path or os.environ.get(UPLOAD_DB_ENV, "").strip()
        if not chosen:
            raise ConfigurationError(
                "Upload mode requires an explicit database path "
                f"(pass database_path or set {UPLOAD_DB_ENV})."
            )
        upload_path = _resolved_path(chosen)
        if upload_path == demo_path:
            raise ConfigurationError(
                "The upload database must be separate from the synthetic demo "
                f"database ({DEMO_DB_ENV} or demo.db)."
            )

        chosen_upload_dir = upload_dir or os.environ.get(UPLOAD_DIR_ENV, "").strip()
        upload_dir_path = _resolved_path(chosen_upload_dir or DEFAULT_UPLOAD_DIR)
        (
            upload_max_bytes,
            upload_max_pages,
            upload_max_extracted_chars,
            upload_max_processing_seconds,
        ) = _upload_limits()
        return AppSettings(
            mode=MODE_UPLOAD,
            database_path=upload_path,
            database_read_only=False,
            uploads_enabled=True,
            upload_dir=upload_dir_path,
            session_cookie_secure=session_cookie_secure,
            session_ttl_seconds=session_ttl_seconds,
            upload_max_bytes=upload_max_bytes,
            upload_max_pages=upload_max_pages,
            upload_max_extracted_chars=upload_max_extracted_chars,
            upload_max_processing_seconds=upload_max_processing_seconds,
        )

    raise ConfigurationError(
        f"Unknown database mode {selected_mode!r}; expected {MODE_DEMO!r} or "
        f"{MODE_UPLOAD!r}."
    )
