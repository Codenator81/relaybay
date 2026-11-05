"""Logging setup for `relaybay serve` and the desktop program: uvicorn's defaults plus token
redaction."""

import copy
import logging
import sys
from pathlib import Path
from typing import Any

from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter

REDACTED = "<redacted>"


class RedactLoginFilter(logging.Filter):
    """Drop the query string of `/login` from access-log lines: it carries the token."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn.access records: (client_addr, method, full_path, http_version, status_code)
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            if sep and path.rstrip("/") == "/login":
                record.args = (*args[:2], f"{path}?{REDACTED}", *args[3:])
        return True


def uvicorn_log_config() -> dict[str, Any]:
    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("filters", {})["redact_login"] = {"()": RedactLoginFilter}
    config["handlers"]["access"]["filters"] = ["redact_login"]
    return config


LOG_FILE_BYTES = 1024 * 1024
LOG_FILE_BACKUPS = 4  # five files in all


class FileFormatter(logging.Formatter):
    """One format for the log file: access lines as uvicorn prints them, the rest with the
    logger name. One handler writes the file, so rotation never fights a second handle."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._access = AccessFormatter(
            '%(asctime)s %(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            use_colors=False,
        )

    def format(self, record: logging.LogRecord) -> str:
        if record.name == "uvicorn.access":
            return self._access.format(record)
        return super().format(record)


def desktop_log_config(path: Path) -> dict[str, Any]:
    """`uvicorn_log_config()` plus a rotating file at `path` (5 x 1 MB) for uvicorn and every
    other logger. Console handlers stay only when the process has a console: a GUI start
    (pythonw, the windowed executable) has `sys.stderr = None`."""
    config = uvicorn_log_config()
    config["formatters"]["file"] = {"()": FileFormatter}
    config["handlers"]["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "formatter": "file",
        "filename": str(path),
        "maxBytes": LOG_FILE_BYTES,
        "backupCount": LOG_FILE_BACKUPS,
        "encoding": "utf-8",
        "filters": ["redact_login"],
    }
    console = sys.stderr is not None and sys.stdout is not None
    if console:
        config["loggers"]["uvicorn"]["handlers"] = ["default", "file"]
        config["loggers"]["uvicorn.access"]["handlers"] = ["access", "file"]
    else:
        del config["handlers"]["default"], config["handlers"]["access"]
        del config["formatters"]["default"], config["formatters"]["access"]
        config["loggers"]["uvicorn"]["handlers"] = ["file"]
        config["loggers"]["uvicorn.access"]["handlers"] = ["file"]
    config["root"] = {"handlers": ["file"], "level": "INFO"}
    path.parent.mkdir(parents=True, exist_ok=True)
    return config
