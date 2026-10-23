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


def _resolved_path(value) -> Path:
    return Path(value).expanduser().resolve()


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

    if selected_mode == MODE_DEMO:
        chosen = database_path or os.environ.get(DEMO_DB_ENV, "").strip() or demo_path
        return AppSettings(
            mode=MODE_DEMO,
            database_path=_resolved_path(chosen),
            database_read_only=True,
            uploads_enabled=False,
            upload_dir=None,
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
        return AppSettings(
            mode=MODE_UPLOAD,
            database_path=upload_path,
            database_read_only=False,
            uploads_enabled=True,
            upload_dir=upload_dir_path,
        )

    raise ConfigurationError(
        f"Unknown database mode {selected_mode!r}; expected {MODE_DEMO!r} or "
        f"{MODE_UPLOAD!r}."
    )
