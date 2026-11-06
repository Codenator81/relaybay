"""Token usage from Claude Code transcripts: which file may be read and what a line holds.

Only the `usage` numbers, the model name, the message id and the time of a line go further than
`parse_line`; prompt and output text are parsed away with the rest of the line.
"""

import ctypes
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 10
# One read of one file. A line longer than this cannot be parsed: it is skipped and counted (the
# long lines of a transcript are pasted images, never a usage line).
CHUNK_BYTES = 8 * 1024 * 1024
PASS_BYTES = 32 * 1024 * 1024
# Between passes while a file is still behind; the configured interval once all are caught up.
CATCH_UP_SECONDS = 5.0
UNKNOWN = "unknown"
DAYS_MAX = 3660
# One transaction stores at most this many usage lines, so hooks never wait long for the write lock.
LINES_PER_TRANSACTION = 2000
# More would not be a token count; JSON numbers above this stop being exact in many readers.
NUMBER_MAX = 2**53
FIRST_DAY = date(2020, 1, 1)
# The chart and `usage_summary` show at most this many periods; the totals count them all.
PERIODS_MAX = 400
# Subagent transcripts live in `<transcript stem>/subagents/*.jsonl` beside the transcript.
SUBAGENT_FILES_MAX = 200
SUBAGENT_SCAN_MAX = 10_000
_MESSAGE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")
_DRIVE_REMOTE = 4  # GetDriveTypeW
_WINDOWS_DEVICES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}")
# Line types that never carry usage, so they are not "unknown" either.
NO_USAGE_TYPES = frozenset(
    {
        "user",
        "system",
        "summary",
        "attachment",
        "progress",
        "file-history-snapshot",
        "queue-operation",
        "last-prompt",
        "custom-title",
        "agent-name",
        "permission-mode",
    }
)
_NUMBERS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
}


@dataclass(frozen=True, slots=True)
class UsageLine:
    message_id: str
    day: str
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    at: str = ""  # when, UTC and in the format of the database's times: tells the bound tasks


class _Unknown:
    """A line relaybay did not understand: skipped and counted, never guessed at."""


UNKNOWN_LINE = _Unknown()


def _remote_drive(drive: str) -> bool:
    """A drive letter mapped to a network share. Asks for the drive's type, which needs no
    connection to the share."""
    if os.name != "nt" or not drive:
        return False
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == _DRIVE_REMOTE


def _plainly_local(value: str) -> Path | None:
    """`value` as an absolute local path, or None. Touches no file: whatever is refused here never
    reaches a call that could wait on the network."""
    if value[:2].count("/") + value[:2].count("\\") == 2:  # //host, \\host, \/host, /\host
        return None
    if "\0" in value or not value.lower().endswith(".jsonl"):
        return None
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(("\\", "//")):
        return None
    if path.drive.startswith(("\\", "//")) or _remote_drive(path.drive):
        return None
    if os.name == "nt":
        if ":" in str(path)[len(path.drive) :]:  # an alternate data stream
            return None
        if any(part.split(".")[0].casefold() in _WINDOWS_DEVICES for part in path.parts[1:]):
            return None
    return path


def transcript_file(value: str) -> Path | None:
    """`value` as a path relaybay may read, or None: an absolute local `*.jsonl` that is not a link
    leaving its folder. A UNC or mapped network path is refused: reading makes no network call.
    Blocking: call it from a worker thread."""
    path = _plainly_local(value)
    if path is None:
        return None
    try:
        real, folder = path.resolve(), path.parent.resolve()
    except (OSError, RuntimeError):  # a link loop or an unreadable path: refused like a bad one
        return None
    if str(real).startswith(("\\", "//")) or real.parent != folder:
        return None
    return real if real.suffix.lower() == ".jsonl" else None


def subagent_files(value: str, known: set[str], room: int) -> tuple[list[str], int]:
    """Subagent transcripts of the transcript `value` that are not in `known`: at most `room` of
    them, and how many more there are. Only the real `<stem>/subagents` folder counts: a link in
    its place, or a file there that links elsewhere, is left out. Blocking: worker thread."""
    main = transcript_file(value)
    if main is None:
        return [], 0
    folder = main.parent / main.stem / "subagents"
    try:
        if os.path.normcase(folder.resolve()) != os.path.normcase(folder):
            return [], 0
        names = []
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= SUBAGENT_SCAN_MAX:
                    break
                if entry.name.lower().endswith(".jsonl"):
                    names.append(entry.name)
    except (FileNotFoundError, NotADirectoryError):
        return [], 0
    found = []
    over = 0
    for name in sorted(names):
        path = str(folder / name)
        if path in known or transcript_file(path) is None:
            continue
        if len(found) < room:
            found.append(path)
        else:
            over += 1
    return found, over


def _moment(value: Any, today: date) -> datetime | None:
    """A time within 2020-01-01 to tomorrow, in UTC; anything else is not believed."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        moment = moment.astimezone(UTC)
    except (ValueError, OverflowError):
        return None
    return moment if FIRST_DAY <= moment.date() <= today + timedelta(days=1) else None


def parse_line(raw: bytes, today: date | None = None) -> UsageLine | _Unknown | None:
    """One transcript line: its usage, `UNKNOWN_LINE`, or None for a line without usage."""
    today = today or datetime.now(UTC).date()
    try:
        line = json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return UNKNOWN_LINE
    if not isinstance(line, dict):
        return UNKNOWN_LINE
    if line.get("type") in NO_USAGE_TYPES:
        return None
    if line.get("type") != "assistant":
        return UNKNOWN_LINE
    message = line.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return UNKNOWN_LINE
    numbers: dict[str, int] = {}
    for name, key in _NUMBERS.items():
        value = usage.get(key, 0)
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= NUMBER_MAX:
            return UNKNOWN_LINE
        numbers[name] = value
    if not any(numbers.values()):
        return None  # e.g. a local error message: no tokens were used
    message_id, moment = message.get("id"), _moment(line.get("timestamp"), today)
    if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id) or moment is None:
        return UNKNOWN_LINE
    model = message.get("model")
    model = model if isinstance(model, str) and _MODEL.fullmatch(model) else UNKNOWN
    at = moment.isoformat(timespec="milliseconds")
    return UsageLine(message_id, moment.date().isoformat(), model, **numbers, at=at)


@dataclass(slots=True)
class Chunk:
    """What one read found. `offset` is where the next read starts."""

    offset: int
    skipping: bool = False
    shrunk: bool = False
    behind: bool = False  # the file holds more complete data than this read took
    read_bytes: int = 0
    unknown: int = 0
    lines: list[UsageLine] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class UsageFile:
    id: int
    path: str
    session_id: int | None
    read_offset: int
    skipping: bool
    error: str | None
    parent_id: int | None = None  # the transcript this subagent file belongs to


def read_file(
    value: str,
    offset: int,
    skipping: bool,
    limit: int = CHUNK_BYTES,
    max_lines: int = LINES_PER_TRANSACTION,
) -> Chunk:
    """The next chunk of a tracked file. The path is checked again here: what it points at may
    have changed since the hook reported it. A file that shrank is read from its start."""
    path = transcript_file(value)
    if path is None:
        raise OSError("not a transcript file relaybay may read")
    chunk = read_chunk(path, offset, skipping, limit, max_lines)
    if chunk.shrunk:
        chunk = read_chunk(path, 0, False, limit, max_lines)
        chunk.shrunk = True
    return chunk


def _read_regular(path: Path, offset: int, limit: int) -> tuple[bytes, int] | None:
    """Up to `limit` bytes at `offset` and the file's size, or None when it is shorter than
    `offset`. Opened without blocking and without following a link, and looked at through the
    open handle: a named pipe or a device is refused, whatever the path said a moment ago."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    handle = os.open(path, flags)
    try:
        info = os.fstat(handle)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size < offset:
            return None
        os.lseek(handle, offset, os.SEEK_SET)
        return os.read(handle, limit), info.st_size
    finally:
        os.close(handle)


def read_chunk(
    path: Path,
    offset: int,
    skipping: bool,
    limit: int = CHUNK_BYTES,
    max_lines: int = LINES_PER_TRANSACTION,
) -> Chunk:
    """Parse the complete lines after `offset`, at most `limit` bytes and `max_lines` usage lines;
    a partial last line waits for the next pass. Blocking: runs in a worker thread, never on the
    hook path. Raises OSError when the file cannot be read."""
    found = _read_regular(path, offset, limit)
    if found is None:
        return Chunk(offset=0, shrunk=True)
    data, size = found
    chunk = Chunk(offset=offset, skipping=skipping, read_bytes=len(data))
    start = 0
    if skipping:
        newline = data.find(b"\n")
        if newline < 0:
            chunk.offset = offset + len(data)
            chunk.behind = len(data) == limit
            return chunk
        start, chunk.skipping = newline + 1, False
    end = data.rfind(b"\n")
    if end < start:
        if start == 0 and len(data) == limit:  # no line end in a whole read: too long to parse
            chunk.offset, chunk.skipping, chunk.behind = offset + limit, True, True
            chunk.unknown = 1
        else:
            chunk.offset = offset + start
            chunk.behind = start > 0 and size - chunk.offset >= limit
        return chunk
    today = datetime.now(UTC).date()
    position = start
    while position <= end:
        newline = data.find(b"\n", position)
        raw = data[position:newline]
        position = newline + 1
        if not raw.strip():
            continue
        parsed = parse_line(raw, today)
        if isinstance(parsed, UsageLine):
            chunk.lines.append(parsed)
            if len(chunk.lines) >= max_lines:
                break
        elif parsed is UNKNOWN_LINE:
            chunk.unknown += 1
    chunk.offset = offset + position
    chunk.read_bytes = position
    chunk.behind = position <= end or len(data) == limit
    return chunk


# --- report ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageDayRow:
    """One row of the daily aggregate, summed over the files of a session."""

    day: str
    session_id: int | None
    session: str | None
    project: str | None
    model: str
    input: int
    output: int
    cache_read: int
    cache_write: int
    messages: int


@dataclass(slots=True)
class UsageTotal:
    """Summed usage under a label: a period, a project, a session or a model."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    messages: int = 0

    def add(self, row: "UsageDayRow | UsageTotal") -> None:
        self.input += row.input
        self.output += row.output
        self.cache_read += row.cache_read
        self.cache_write += row.cache_write
        self.messages += row.messages

    @property
    def tokens(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write


@dataclass(slots=True)
class UsageRow:
    period: str
    session_id: int | None
    session: str
    project: str
    model: str
    total: UsageTotal


@dataclass(slots=True)
class UsageReport:
    group: str  # day or week
    days: int  # 0: all of it
    rows: list[UsageRow]
    total: UsageTotal
    periods: list[tuple[str, UsageTotal]]  # oldest first, without gaps: the chart
    by_project: list[tuple[str, UsageTotal]]
    by_session: list[tuple[str, UsageTotal]]
    by_model: list[tuple[str, UsageTotal]]
    files: int
    unread_files: int
    unknown_lines: int
    capped_files: int = 0  # subagent files over the limit per session: not read


def period_of(day: str, group: str) -> str:
    """The period a day falls in: the day itself, or the Monday of its ISO week."""
    if group != "week":
        return day
    moment = date.fromisoformat(day)
    return (moment - timedelta(days=moment.weekday())).isoformat()


def _gapless(found: dict[str, UsageTotal], group: str, last: date) -> list[tuple[str, UsageTotal]]:
    """Periods from the first one found to the one of `last`; a period without usage is a real
    zero in the chart, not a missing bar."""
    if not found:
        return []
    step = timedelta(days=7 if group == "week" else 1)
    newest = min(max(found), (last + timedelta(days=1)).isoformat())
    end = max(
        date.fromisoformat(period_of(last.isoformat(), group)),
        date.fromisoformat(period_of(newest, group)),
    )
    moment = max(date.fromisoformat(min(found)), end - step * (PERIODS_MAX - 1))
    out = []
    while moment <= end:
        key = moment.isoformat()
        out.append((key, found.get(key, UsageTotal())))
        moment += step
    return out


def _ranked(found: dict[str, UsageTotal]) -> list[tuple[str, UsageTotal]]:
    return sorted(found.items(), key=lambda item: (-item[1].tokens, item[0]))


def build_report(
    day_rows: list[UsageDayRow],
    *,
    group: str,
    days: int,
    today: date,
    files: int,
    unread_files: int,
    unknown_lines: int,
    capped_files: int = 0,
) -> UsageReport:
    """Group daily rows by period, session and model. What the data does not name stays
    "unknown": a row of a removed session, a session in no project, a line without a model."""
    grouped: dict[tuple[str, int | None, str], UsageRow] = {}
    total = UsageTotal()
    periods: dict[str, UsageTotal] = {}
    projects: dict[str, UsageTotal] = {}
    sessions: dict[str, UsageTotal] = {}
    models: dict[str, UsageTotal] = {}
    for row in day_rows:
        period = period_of(row.day, group)
        name, project = row.session or UNKNOWN, row.project or UNKNOWN
        key = (period, row.session_id, row.model)
        if key not in grouped:
            grouped[key] = UsageRow(period, row.session_id, name, project, row.model, UsageTotal())
        grouped[key].total.add(row)
        total.add(row)
        for bucket, label in (
            (periods, period),
            (projects, project),
            (sessions, name),
            (models, row.model),
        ):
            bucket.setdefault(label, UsageTotal()).add(row)
    rows = sorted(grouped.values(), key=lambda r: (r.period, r.total.tokens), reverse=True)
    return UsageReport(
        group=group,
        days=days,
        rows=rows,
        total=total,
        periods=_gapless(periods, group, today),
        by_project=_ranked(projects),
        by_session=_ranked(sessions),
        by_model=_ranked(models),
        files=files,
        unread_files=unread_files,
        unknown_lines=unknown_lines,
        capped_files=capped_files,
    )
