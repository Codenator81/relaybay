"""Schedules (design 0023): a clock in the program for coordinator turns, read-only checks and
reminders.

Pure rules only: what a form or a tool may set, when a schedule is due next in the machine's
local time, the missed rule (K6), and the texts of a check's turn and of its report.
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from relaybay.domain.errors import ValidationError

KINDS = ("coordinator", "check", "reminder")
COORDINATOR_TURN, CHECK, REMINDER = KINDS
REPEATS = ("once", "daily", "every")
ONCE, DAILY, EVERY = REPEATS
OUTCOMES = ("ran", "missed", "skipped", "failed", "stopped")
RAN, MISSED, SKIPPED, FAILED, STOPPED = OUTCOMES
CREATORS = ("owner", "coordinator")
# K6: a run missed by less than this runs once when the program is back; older ones are missed.
MISSED_AFTER = timedelta(hours=1)
EVERY_MIN, EVERY_MAX = 5, 1440
IN_MINUTES_MAX = 7 * 24 * 60  # schedule_add: `in_minutes` and `at` within a week
COORDINATOR_OPEN_MAX = 10  # schedules a coordinator made that may still run
MAX_TIMES = 24
PROMPT_MAX = 4000
TITLE_MAX = 200
GIST_MAX = 120
# A check (probe 20): one turn without a transcript, capped in steps.
CHECK_MAX_TURNS = 8
CHECK_PROFILE = "read-only"
STEP_LIMIT = "check stopped at its step limit"
# The clock loop's longest sleep: after the machine slept, the next pass comes within it.
CHECK_SECONDS = 30.0
# The journal's schedule events (the Activity filter and the Audit page's changes).
EVENT_KINDS = (
    "schedule.created",
    "schedule.updated",
    "schedule.deleted",
    "schedule.fired",
    "schedule.missed",
    "schedule.skipped",
    "schedule.ended",
)
_DONE = re.compile(r"^\W*done\b", re.IGNORECASE)
_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


class LocalTime(Protocol):
    """The machine's local time: the conversions the clock needs, injectable for tests."""

    def to_utc(self, local: datetime) -> datetime:
        """A naive local date and time as an aware UTC moment."""
        ...

    def to_local(self, moment: datetime) -> datetime:
        """An aware moment as a naive local date and time."""
        ...

    def name(self) -> str: ...


class SystemTime:
    """The operating system's own local time. Python reads a naive datetime as local time with
    the system's rules, daylight saving time included, so a daily time stays at the same clock
    time across a change, without a time zone database."""

    def to_utc(self, local: datetime) -> datetime:
        return local.astimezone(UTC)

    def to_local(self, moment: datetime) -> datetime:
        return moment.astimezone().replace(tzinfo=None)

    def name(self) -> str:
        return datetime.now().astimezone().tzname() or "local"


@dataclass(slots=True)
class ScheduleRun:
    id: int
    schedule_id: int
    due_at: str
    started_at: str | None
    ended_at: str | None
    outcome: str | None
    reason: str | None
    tokens: int
    run_id: int | None
    turn_id: int | None
    report_id: int | None
    notice: str | None
    seen_at: str | None
    schedule_kind: str = ""
    schedule_title: str = ""
    task_key: str = ""
    project_slug: str = ""

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


@dataclass(slots=True)
class Schedule:
    id: int
    kind: str
    repeat: str
    at: str | None
    time_of_day: str | None
    weekdays_only: bool
    every_minutes: int | None
    max_times: int
    timezone: str
    project_id: int | None
    task_id: int | None
    profile: str | None
    prompt: str
    created_by: str
    created_by_kind: str
    enabled: bool
    next_at: str | None
    times_run: int
    done_at: str | None
    created_at: str
    updated_at: str
    updated_by: str
    project_slug: str = ""
    task_key: str = ""
    last: ScheduleRun | None = None  # its latest run
    running: bool = False  # a run of it is open (a check under way)

    @property
    def title(self) -> str:
        return flat(self.prompt, TITLE_MAX)

    @property
    def target(self) -> str:
        """Where it works: its card, its project, or the coordinator for a turn of it."""
        if self.task_key:
            return self.task_key
        if self.project_slug:
            return self.project_slug
        return "the coordinator" if self.kind == COORDINATOR_TURN else ""


def flat(value: Any, limit: int) -> str:
    """One line of at most `limit` characters."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _choice(value: Any, name: str, choices: tuple[str, ...]) -> str:
    text = str(value or "").strip()
    if text not in choices:
        raise ValidationError(f"{name} must be one of: {', '.join(choices)}; got {text!r}.")
    return text


def clean_kind(value: Any) -> str:
    return _choice(value, "kind", KINDS)


def clean_repeat(value: Any) -> str:
    return _choice(value, "repeat", REPEATS)


def clean_prompt(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError("The schedule's text is empty: say what to do, check or remind.")
    if len(text) > PROMPT_MAX:
        raise ValidationError(f"The schedule's text is longer than {PROMPT_MAX} characters.")
    return text


def _whole(value: Any, name: str, low: int, high: int, default: int | None) -> int:
    text = str(value if value is not None else "").strip()
    if text in ("", "-"):
        if default is None:
            raise ValidationError(f"{name} is required.")
        return default
    try:
        number = int(text)
    except ValueError:
        raise ValidationError(f"{name} must be a whole number; got {text!r}.") from None
    if not low <= number <= high:
        raise ValidationError(f"{name} must be from {low} to {high}; got {number}.")
    return number


def clean_every(value: Any) -> int:
    return _whole(value, "Every N minutes", EVERY_MIN, EVERY_MAX, None)


def clean_max_times(value: Any) -> int:
    return _whole(value, "At most N times", 1, MAX_TIMES, MAX_TIMES)


def clean_time_of_day(value: Any) -> str:
    """`HH:MM` of the local day."""
    text = str(value or "").strip()
    found = _TIME.match(text)
    if not found or int(found[1]) > 23 or int(found[2]) > 59:
        raise ValidationError(f"The time of day must be HH:MM (00:00 to 23:59); got {text!r}.")
    return f"{int(found[1]):02d}:{found[2]}"


def clean_at(value: Any, local: LocalTime) -> datetime:
    """A date and time: `YYYY-MM-DD HH:MM` (or with a `T`) is the machine's local time; one with
    an offset or `Z` is that moment. Returned as an aware UTC moment."""
    text = str(value or "").strip()
    if not text:
        raise ValidationError("A once schedule needs its date and time (YYYY-MM-DD HH:MM).")
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T", 1).replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(
            f"The date and time must be YYYY-MM-DD HH:MM (local time); got {text!r}."
        ) from None
    if parsed.tzinfo is None:
        return local.to_utc(parsed.replace(second=0, microsecond=0))
    return parsed.astimezone(UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _hm(time_of_day: str) -> time:
    hours, minutes = time_of_day.split(":")
    return time(int(hours), int(minutes))


def next_daily(
    after: datetime, time_of_day: str, weekdays_only: bool, local: LocalTime
) -> datetime:
    """The first local `time_of_day` strictly after `after`, on a weekday when asked. Each day's
    time is turned into UTC with that day's rules, so it stays at the same clock time across a
    daylight saving change; a time the change skips comes out as the moment the clock reads it
    an hour later (the system's reading of it)."""
    start: date = local.to_local(after).date()
    for offset in range(9):
        day = start + timedelta(days=offset)
        if weekdays_only and day.weekday() >= 5:
            continue
        moment = local.to_utc(datetime.combine(day, _hm(time_of_day)))
        if moment > after:
            return moment
    raise AssertionError("a daily schedule is due within nine days")  # unreachable


def first_due(
    repeat: str,
    now: datetime,
    local: LocalTime,
    *,
    at: datetime | None = None,
    time_of_day: str | None = None,
    weekdays_only: bool = False,
    every_minutes: int | None = None,
) -> datetime:
    """When a schedule saved now is due first: its date and time, its next daily time, or one
    interval from now."""
    if repeat == ONCE:
        assert at is not None
        return at
    if repeat == DAILY:
        assert time_of_day is not None
        return next_daily(now, time_of_day, weekdays_only, local)
    assert every_minutes is not None
    return now + timedelta(minutes=every_minutes)


def next_after(schedule: Schedule, now: datetime, local: LocalTime) -> datetime | None:
    """When it is due after a due time was handled now (ran, missed or skipped), with
    `times_run` already counting it; None: it is finished (a once, or an every at its count)."""
    if schedule.repeat == ONCE:
        return None
    if schedule.repeat == DAILY:
        assert schedule.time_of_day is not None
        return next_daily(now, schedule.time_of_day, schedule.weekdays_only, local)
    if schedule.times_run >= schedule.max_times:
        return None
    assert schedule.every_minutes is not None
    return now + timedelta(minutes=schedule.every_minutes)


def is_missed(due: datetime, now: datetime) -> bool:
    """K6: late by an hour or more (the program was closed or the machine asleep)."""
    return now - due >= MISSED_AFTER


def is_done_answer(text: str) -> bool:
    """A check's answer that starts with `done` (case ignored, after any markdown marks)."""
    return bool(_DONE.match(text.strip()))


def when_text(schedule: Schedule, local: LocalTime) -> str:
    if schedule.repeat == ONCE and schedule.at:
        return f"once at {local_text(schedule.at, local)}"
    if schedule.repeat == DAILY:
        days = " on weekdays" if schedule.weekdays_only else ""
        return f"daily at {schedule.time_of_day}{days}"
    until = ", or until the answer starts with done" if schedule.kind == CHECK else ""
    return f"every {schedule.every_minutes} min, at most {schedule.max_times} times{until}"


def local_text(value: str | None, local: LocalTime) -> str:
    """A stored UTC time as the machine's local `YYYY-MM-DD HH:MM`."""
    if not value:
        return ""
    return local.to_local(parse(value)).strftime("%Y-%m-%d %H:%M")


def gist(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return flat(first.lstrip("#").strip(), GIST_MAX) or "(no answer text)"


def check_prompt(schedule_id: int, where: str, cwd: str, text: str, until_done: bool) -> str:
    """A check's one turn: what it is, then the schedule's text."""
    head = (
        f"relaybay check (schedule {schedule_id}) for {where}, in {cwd}: one turn, read-only,"
        f" at most {CHECK_MAX_TURNS} steps, no session kept. Answer in a few lines; relaybay"
        " writes your answer down."
    )
    if until_done:
        head += ' Start the answer with "done" once what you check is finished: the schedule stops.'
    return f"{head}\n\n{text}"


def check_report(schedule_id: int, run_id: int, text: str, step_limit: bool) -> str:
    """The report a check's answer becomes on its card, first line written by relaybay."""
    answer = text.strip()
    first = STEP_LIMIT if step_limit else gist(answer)
    lines = [f"inspected · S · {first}", "", f"Check of schedule {schedule_id} (run {run_id})."]
    if answer:
        lines += ["", answer]
    return "\n".join(lines)
