"""The coordinator run (design 0023): one permanent run without a card, in a folder of its own.

Pure rules only: the owner's settings, the folder's permission rule, the texts of its turns. The
folder on disk lives in `relaybay.runner.coordinator`, the orchestration in the service.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from relaybay.domain.errors import ValidationError
from relaybay.domain.runs import FOLDER_MAX

COMPACT_KEY = "coordinator_compact_at_tokens"
DEFAULT_PROFILE = "coordinator"
DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "medium"
STARTER_NAME = "CLAUDE.md"
DEFAULT_GAP_SECONDS = 120  # a burst of events becomes one wake
DEFAULT_COMPACT_AT = 120_000  # context tokens (input + cache read + cache write), probe 18
COMPACT_MIN, COMPACT_MAX = 1_000, 1_000_000
# Probe 19: a wake reads about its whole context from the cache, and compaction keeps that under
# DEFAULT_COMPACT_AT, so a wake costs some 60 000 tokens on average: 3 000 000 is a busy day of
# about 50 wakes, and the warning comes at 80% of it. The owner tunes it after a measured week.
DEFAULT_DAILY_TOKENS = 3_000_000
DAILY_MIN, DAILY_MAX = 10_000, 10**12
BUDGET_ACTIONS = ("warn", "stop")
COMPACT = "/compact"
WAKE_LINE_MAX = 300
SCHEDULE_FIRED = "schedule.fired"  # a schedule ran; only a coordinator turn's wakes it
# The events a wake carries. Filters on their payload (a plan draft, new intake, a message to
# the coordinator) and "never its own" live in `repo.coordinator_wake_events`.
WAKE_KINDS = (
    "report.added",
    "run.ended",
    "run.stopped",
    "run.lost",
    "question.overdue",
    "plan.set",
    "approval.decided",
    "intake.received",
    "intake.tied",
    "message.sent",
    "budget.warned",
    "budget.exceeded",
    SCHEDULE_FIRED,
)
# Gitignore pattern characters and parentheses: a folder holding one would make its Edit rule
# match other paths, or not parse as a tool rule at all.
_PATTERN_CHARS = re.compile(r"[*?\[\]()!#\\]")


@dataclass(frozen=True, slots=True)
class CoordinatorWake:
    """One wake: its turn and the range of events it carried."""

    id: int
    run_id: int
    turn_id: int | None
    first_event_id: int
    last_event_id: int
    count: int
    created_at: str
    returned_at: str | None = None  # its turn never ran: the range went into a later wake


@dataclass(frozen=True, slots=True)
class StartedRun:
    """A run the coordinator started with run_start, or a check of a schedule it made."""

    id: int
    kind: str
    status: str
    created_at: str
    ended_at: str | None
    tokens: int
    task_key: str | None
    task_title: str | None
    project_slug: str | None


def clean_folder(value: Any) -> str | None:
    """The coordinator's folder: an absolute path, normalised (`Path.resolve`), never a drive
    or filesystem root and never written with `..`; empty (or "-") is the default. Whether it
    lies inside a git repository is checked at save and at Start."""
    text = str(value or "").strip()
    if text in ("", "-"):
        return None
    if len(text) > FOLDER_MAX:
        raise ValidationError(f"The coordinator's folder is longer than {FOLDER_MAX} characters.")
    if any(c in text for c in "\x00\r\n"):
        raise ValidationError("The coordinator's folder holds a control character.")
    given = PurePath(text)
    if not given.is_absolute():
        raise ValidationError(f"The coordinator's folder must be an absolute path; got {text!r}.")
    if ".." in given.parts:
        raise ValidationError(f"Write the coordinator's folder without '..'; got {text!r}.")
    resolved = Path(text).resolve()
    if resolved.parent == resolved:
        raise ValidationError(
            f"The coordinator's folder cannot be a drive or filesystem root; got {text!r}."
        )
    return str(resolved)


def posix_form(folder: str, *, windows: bool) -> str:
    """The folder as Claude Code matches paths: on Windows `C:\\x\\y` becomes `/c/x/y`."""
    if windows:
        win = PureWindowsPath(folder)
        drive = win.drive.rstrip(":").lower()
        parts = [p for p in win.parts[1:] if p not in ("\\", "/")]
        return "/" + "/".join([drive, *parts]) if drive else "/" + "/".join(parts)
    return str(PurePosixPath(folder))


def edit_rule(folder: str, *, windows: bool) -> str:
    """The allow rule that lets Edit and Write work inside the folder only. `//` anchors at the
    filesystem root (a single `/` would anchor at the settings source); an `Edit` rule covers
    Write too. Refused for a folder whose name a gitignore pattern would read as a pattern."""
    path = posix_form(folder, windows=windows).rstrip("/")
    segments = [s for s in path.split("/") if s]
    if len(segments) < (2 if windows else 1) or ".." in segments:
        raise ValidationError(
            f"The coordinator's folder {folder} is a drive or filesystem root, or holds '..':"
            " pick a folder of its own in Settings -> Agents."
        )
    if _PATTERN_CHARS.search(path.replace("/", "")):
        raise ValidationError(
            f"The coordinator's folder {folder} holds a character a permission rule would read"
            " as a pattern (* ? [ ] ( ) ! # or a backslash in a name): pick another folder in"
            " Settings -> Agents."
        )
    return f"Edit(/{path}/**)"


def header(run_id: int, session_name: str, folder: str) -> str:
    """The first turn's head: who the run is and how its writes name themselves."""
    return "\n".join(
        (
            f"relaybay coordinator run {run_id}: one coordinator for all projects, no card.",
            f'In relaybay writes pass session "{session_name}" and the hook session your'
            " SessionStart gave you as hook_session.",
            f"Your folder: {folder}. Edit and Write work only there; runs on cards do the code.",
            "",
        )
    )


def local_day_start(now: datetime) -> str:
    """The start of the machine's local day of `now`, as an ISO time in UTC: the daily cap of
    run_start counts from it."""
    local = now.astimezone()
    start = datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)
    return start.astimezone(UTC).isoformat(timespec="milliseconds")


def _flat(value: Any, limit: int = WAKE_LINE_MAX) -> str:
    """One line: an agent's text in a wake never starts a line of its own."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _age(minutes: Any) -> str:
    number = minutes if isinstance(minutes, int) else 0
    return f"{number // 60} h" if number >= 120 else f"{number} min"
