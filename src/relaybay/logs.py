"""Logging setup for `relaybay serve`: uvicorn's defaults plus token redaction."""

import copy
import logging
from typing import Any

from uvicorn.config import LOGGING_CONFIG

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
