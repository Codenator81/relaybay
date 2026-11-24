"""The day time log: marks (start, switch, pause) on cards or free labels, and the spans of time
they make. Pure: no clock, no database. A non-pause mark opens a span that ends at the next mark
of any kind; a pause opens a gap. The day is the local date of the server machine; every function
takes `tz` (None: the machine's own zone) so tests can fix it."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from relaybay.domain.errors import ValidationError

START = "start"
SWITCH = "switch"
PAUSE = "pause"
KINDS = (START, SWITCH, PAUSE)
NOW = "now"  # the board's "Working on this now": start or switch, picked by the server
SAME_MARK_SECONDS = 60  # the same mark again within this is the first one
FUTURE_SECONDS = 60  # an `at` further ahead than this is refused
LABEL_MAX = 120
REASON_MAX = 300
REF_MAX = 200
WORKLOG_MINUTES_MAX = 1440  # one row; the migration's CHECK holds the same bound
# A span over local midnight counts only up to this long and only into the next day: longer, it
# is a day that was not closed (a forgotten pause), not night work.
MAX_OVERNIGHT_SECONDS = 4 * 3600
_CLOCK_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True, slots=True)
class TimeMark:
    id: int
    at: datetime  # aware, UTC
    kind: str
    task_id: int | None
    task_key: str | None
    project_id: int | None
    project_slug: str | None
    label: str | None
    actor: str
    created_at: str
    updated_at: str | None = None
    # The next mark of the whole log (any card), when the query read it: where this span ends.
    next_at: datetime | None = None
    next_kind: str | None = None

    @property
    def target(self) -> str:
        """What the mark is about, as the answers name it: the card key or the label."""
        if self.kind == PAUSE:
            return f"pause ({self.label})" if self.label else "pause"
        return self.task_key or self.label or "?"

    @property
    def project(self) -> str | None:
        """The card's project; a purged project's card keeps the slug its key starts with."""
        if self.project_slug:
            return self.project_slug
        if self.task_key and "-" in self.task_key:
            return self.task_key.rsplit("-", 1)[0]
        return None


@dataclass(frozen=True, slots=True)
class Span:
    """The time one non-pause mark opened. `end` None: unclosed (counts 0)."""

    mark: TimeMark
    start: datetime
    end: datetime | None
    running: bool = False  # today's last span, ending now

    @property
    def unclosed(self) -> bool:
        return self.end is None


@dataclass(frozen=True, slots=True)
class Segment:
    """A span's part inside one day, in local time: what the strip and the lists show."""

    mark_id: int
    kind: str
    start: datetime
    end: datetime | None  # None: unclosed
    seconds: int
    task_id: int | None
    key: str | None
    label: str | None
    project: str | None  # None for a label
    running: bool = False
    unclosed: bool = False

    @property
    def minutes(self) -> int:
        return self.seconds // 60

    @property
    def name(self) -> str:
        return self.key or self.label or "?"


@dataclass(slots=True)
class DayLog:
    day: date
    today: bool
    segments: list[Segment]  # chronological; unclosed ones with 0 seconds
    marks: list[TimeMark]  # the marks of the day, pauses included
    total: int  # seconds
    by_project: list[tuple[str | None, int]]  # None: the labels
    by_card: list[tuple[str, str | None, int]]  # key, project, seconds
    by_label: list[tuple[str, int]]
    current: Segment | None  # the running span, today only
    unclosed: list[Segment]
    offset: str  # the server's UTC offset on that day, e.g. "UTC+02:00"


@dataclass(slots=True)
class CardTime:
    """A card's time: today, all days, the minutes entered in the tracker, and whether it is
    the card being worked on now."""

    today: int = 0  # seconds
    total: int = 0  # seconds
    tracker: int = 0  # minutes (tracker_worklog)
    current: bool = False
    since: datetime | None = None  # when the running span started (local)
    days: list[tuple[date, int]] = field(default_factory=list)  # seconds per day, newest first


@dataclass(slots=True)
class MarkResult:
    mark: TimeMark
    created: bool  # False: the same mark again, nothing written
    closed: Segment | None  # the span this mark ended, today's part
    warnings: list[str]
    day: DayLog | None = None


def hm(seconds: int) -> str:
    """A duration floored to whole minutes, readable at a glance: `1h05`, `0h30`, `-0h30`."""
    sign = "-" if seconds < 0 else ""
    minutes = abs(int(seconds)) // 60
    return f"{sign}{minutes // 60}h{minutes % 60:02d}"


def hm_minutes(minutes: int) -> str:
    return hm(int(minutes) * 60)


def local(moment: datetime, tz: tzinfo | None) -> datetime:
    return moment.astimezone(tz)


def clock(moment: datetime, tz: tzinfo | None) -> str:
    return local(moment, tz).strftime("%H:%M")


def local_day(moment: datetime, tz: tzinfo | None) -> date:
    return local(moment, tz).date()


def day_start(day: date, tz: tzinfo | None) -> datetime:
    """Local midnight; with tz None the machine's zone on that date (its DST offset)."""
    if tz is None:
        return datetime.combine(day, time()).astimezone()
    return datetime.combine(day, time(), tzinfo=tz)


def day_bounds(day: date, tz: tzinfo | None) -> tuple[datetime, datetime]:
    return day_start(day, tz), day_start(day + timedelta(days=1), tz)


def utc_offset(day: date, tz: tzinfo | None) -> str:
    offset = day_start(day, tz).utcoffset() or timedelta()
    minutes = int(offset.total_seconds()) // 60
    sign = "-" if minutes < 0 else "+"
    return f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def parse_at(
    text: str | None, now: datetime, tz: tzinfo | None, day: date | None = None
) -> datetime:
    """`HH:MM` (local, on `day`, default today) or an ISO time (with an offset; without one it
    is local time), as an aware UTC time. Refused when unreadable or more than a minute ahead
    of `now`."""
    if text is None or not str(text).strip():
        return now.astimezone(UTC)
    value = str(text).strip()
    found = _CLOCK_RE.match(value)
    try:
        if found:
            clock_time = time(int(found.group(1)), int(found.group(2)))
            naive = datetime.combine(day or local_day(now, tz), clock_time)
            moment = naive.astimezone() if tz is None else naive.replace(tzinfo=tz)
        else:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.astimezone() if tz is None else moment.replace(tzinfo=tz)
    except ValueError:
        raise ValidationError(
            f"at must be HH:MM ({'today' if day is None else day.isoformat()}, server time)"
            f" or an ISO time like"
            f" 2026-11-24T09:00+02:00, got {value!r}. Nothing changed."
        ) from None
    if (moment - now).total_seconds() > FUTURE_SECONDS:
        raise ValidationError(
            f"at {value} is in the future (server time now {clock(now, tz)}). Nothing changed."
        )
    return moment.astimezone(UTC)


def check_target(kind: str, task_key: str | None, label: str | None) -> None:
    """start and switch name exactly one of a card and a label; a pause names no card (its
    label is the reason)."""
    if kind not in KINDS:
        raise ValidationError(f"kind must be one of {', '.join(KINDS)}, got {kind!r}.")
    if kind == PAUSE:
        if task_key:
            raise ValidationError("A pause names no card (label may say why). Nothing changed.")
        return
    if bool(task_key) == bool(label):
        raise ValidationError(
            f"{kind} needs exactly one of task (a card key) or label (work without a card)."
            " Nothing changed."
        )


def span_of(mark: TimeMark, now: datetime, tz: tzinfo | None) -> Span | None:
    """The span a mark opens, from its `next_at` / `next_kind`; None for a pause.

    Unclosed (0): the next mark (any kind) is on a later local day and the span is not a short
    night (it ends on the very next day and lasts at most MAX_OVERNIGHT_SECONDS), or there is
    no next mark and the mark's day is past. Today's last span runs until now."""
    if mark.kind == PAUSE:
        return None
    if mark.next_at is None:
        if local_day(mark.at, tz) < local_day(now, tz):
            return Span(mark, mark.at, None)
        return Span(mark, mark.at, max(now, mark.at), running=True)
    days = (local_day(mark.next_at, tz) - local_day(mark.at, tz)).days
    long = (mark.next_at - mark.at).total_seconds() > MAX_OVERNIGHT_SECONDS
    if days > 1 or (days == 1 and long):
        return Span(mark, mark.at, None)
    return Span(mark, mark.at, mark.next_at)


def chain(marks: Sequence[TimeMark]) -> list[TimeMark]:
    """Consecutive marks (sorted by at, id) with each one's next mark filled in from the list;
    the last keeps the `next_at` the query gave it (None when it is the newest)."""
    ordered = sorted(marks, key=lambda m: (m.at, m.id))
    out = []
    for i, mark in enumerate(ordered):
        if i + 1 < len(ordered):
            following = ordered[i + 1]
            out.append(_with_next(mark, following.at, following.kind))
        else:
            out.append(mark)
    return out


def _with_next(mark: TimeMark, at: datetime | None, kind: str | None) -> TimeMark:
    return TimeMark(
        id=mark.id,
        at=mark.at,
        kind=mark.kind,
        task_id=mark.task_id,
        task_key=mark.task_key,
        project_id=mark.project_id,
        project_slug=mark.project_slug,
        label=mark.label,
        actor=mark.actor,
        created_at=mark.created_at,
        updated_at=mark.updated_at,
        next_at=at,
        next_kind=kind,
    )


def segment(span: Span, start: datetime, end: datetime, tz: tzinfo | None) -> Segment | None:
    """The span's part in [start, end) (a day), or None when it has none there. An unclosed
    span belongs to the day it started, with 0 seconds."""
    mark = span.mark
    project = mark.project if mark.task_key else None
    if span.end is None:
        if not start <= span.start < end:
            return None
        return Segment(
            mark.id,
            mark.kind,
            local(span.start, tz),
            None,
            0,
            mark.task_id,
            mark.task_key,
            mark.label,
            project,
            unclosed=True,
        )
    lo, hi = max(span.start, start), min(span.end, end)
    if hi <= lo and not (span.running and start <= span.start < end):
        return None
    return Segment(
        mark.id,
        mark.kind,
        local(lo, tz),
        local(max(hi, lo), tz),
        max(0, int((hi - lo).total_seconds())),
        mark.task_id,
        mark.task_key,
        mark.label,
        project,
        running=span.running and hi >= span.end,
    )


def day_log(
    marks: Sequence[TimeMark],
    day: date,
    now: datetime,
    tz: tzinfo | None,
    project: str | None = None,
) -> DayLog:
    """The day from the marks around it (the one before the day and the day's; the last one
    carries its next mark from the query). Totals are summed in seconds and shown floored to
    minutes. `project`: only that project's cards (and no pauses or labels)."""
    start, end = day_bounds(day, tz)
    linked = chain(marks)
    segments: list[Segment] = []
    for mark in linked:
        span = span_of(mark, now, tz)
        part = segment(span, start, end, tz) if span else None
        if part is not None and (project is None or part.project == project):
            segments.append(part)
    inside = [
        m
        for m in linked
        if start <= m.at < end and (project is None or (m.task_key and m.project == project))
    ]
    return _summary(day, local_day(now, tz) == day, segments, inside, utc_offset(day, tz))


def _summary(
    day: date, today: bool, segments: list[Segment], marks: list[TimeMark], offset: str
) -> DayLog:
    projects: dict[str | None, int] = {}
    cards: dict[str, tuple[str | None, int]] = {}
    labels: dict[str, int] = {}
    for seg in segments:
        projects[seg.project] = projects.get(seg.project, 0) + seg.seconds
        if seg.key:
            project, seconds = cards.get(seg.key, (seg.project, 0))
            cards[seg.key] = (project, seconds + seg.seconds)
        elif seg.label:
            labels[seg.label] = labels.get(seg.label, 0) + seg.seconds
    current = next((s for s in segments if s.running), None) if today else None
    return DayLog(
        day=day,
        today=today,
        segments=segments,
        marks=marks,
        total=sum(s.seconds for s in segments),
        by_project=_largest((p, s) for p, s in projects.items() if s),
        # A card or label with no time on the day (unclosed, just started) is only in the list.
        by_card=sorted(
            ((k, p, s) for k, (p, s) in cards.items() if s), key=lambda r: (-r[2], r[0])
        ),
        by_label=_largest((k, s) for k, s in labels.items() if s),
        current=current,
        unclosed=[s for s in segments if s.unclosed],
        offset=offset,
    )


def _largest[K](items: Iterable[tuple[K, int]]) -> list[tuple[K, int]]:
    return sorted(items, key=lambda r: (-r[1], str(r[0] or "")))


def card_times(
    marks: Iterable[TimeMark], now: datetime, tz: tzinfo | None, days: int = 0
) -> CardTime:
    """One card's (or project's) time from its marks, each carrying its own next mark of the
    whole log (repo `time_marks_for_tasks`). `days`: also the seconds of each of that many
    latest days with time, newest first."""
    today = local_day(now, tz)
    start, end = day_bounds(today, tz)
    out = CardTime()
    per_day: dict[date, int] = {}
    for mark in marks:
        span = span_of(mark, now, tz)
        if span is None or span.end is None:
            continue
        out.total += max(0, int((span.end - span.start).total_seconds()))
        part = segment(span, start, end, tz)
        if part is not None:
            out.today += part.seconds
            if part.running:
                out.current, out.since = True, part.start
        if days:
            for day, seconds in split_days(span, tz):
                per_day[day] = per_day.get(day, 0) + seconds
    out.days = sorted(per_day.items(), reverse=True)[:days]
    return out


def split_days(span: Span, tz: tzinfo | None) -> list[tuple[date, int]]:
    """A closed span's seconds per local day (a span over midnight is split)."""
    if span.end is None:
        return []
    out = []
    day = local_day(span.start, tz)
    while True:
        lo, hi = day_bounds(day, tz)
        part = min(span.end, hi) - max(span.start, lo)
        if part.total_seconds() > 0:
            out.append((day, int(part.total_seconds())))
        if span.end <= hi:
            return out
        day += timedelta(days=1)


def running_at(
    before: TimeMark | None, at: datetime, kind: str, tz: tzinfo | None
) -> TimeMark | None:
    """`before` when a new mark at `at` ends its span (a short night included); None when it
    is a pause or its span would stay unclosed."""
    if before is None or before.kind == PAUSE:
        return None
    span = span_of(_with_next(before, at, kind), at, tz)
    return before if span is not None and not span.unclosed else None


def mark_warnings(
    kind: str,
    at: datetime,
    running: TimeMark | None,
    newest: TimeMark | None,
    tz: tzinfo | None,
    before: TimeMark | None = None,
) -> list[str]:
    """Odd sequences are written and named, never refused. `before`: the mark just before the
    new one, of any day; a span of an earlier day the new mark leaves unclosed is named."""
    out = []
    if before is not None and before.kind != PAUSE and local_day(before.at, tz) < local_day(at, tz):
        span = span_of(_with_next(before, at, kind), at, tz)
        if span is not None and span.unclosed:
            when = local(before.at, tz)
            pause = f"timelog_mark(kind='pause', at='{when.date().isoformat()}THH:MM')"
            out.append(
                f"Note: {before.target} since {when.strftime('%a %H:%M')} stays unclosed (0h00):"
                f" close it with {pause}."
            )
    if newest is not None and at < newest.at:
        out.append(
            f"Warning: this mark is before the latest one (#{newest.id} at"
            f" {clock(newest.at, tz)}): the spans around it changed."
        )
    if kind == START and running is not None:
        out.append(
            f"Note: {running.target} was running since {clock(running.at, tz)}; this start ends"
            " it (switch says the same)."
        )
    if kind == SWITCH and running is None:
        out.append("Note: nothing was running; this switch opens a span like a start.")
    if kind == PAUSE and running is None:
        out.append("Note: nothing was running; this pause ends no span.")
    return out
