"""The day time log's spans (domain/timelog.py, pure, a fixed UTC+2 zone) and the service's
idempotency and `now` rules."""

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from relaybay.domain import timelog as tl
from relaybay.domain.errors import ValidationError
from relaybay.service import Service
from tests.test_policy import JWT, project_layer

TZ = timezone(timedelta(hours=2))
DAY = date(2026, 11, 23)
NEXT = date(2026, 11, 24)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ).astimezone(UTC)


def mark(
    n: int, when: datetime, kind: str, key: str | None = None, label: str | None = None
) -> tl.TimeMark:
    return tl.TimeMark(
        id=n,
        at=when,
        kind=kind,
        task_id=int(key.rsplit("-", 1)[1]) if key else None,
        task_key=key,
        project_id=1 if key else None,
        project_slug=key.rsplit("-", 1)[0] if key else None,
        label=label,
        actor="lead",
        created_at=when.isoformat(),
    )


def test_durations_read_at_a_glance() -> None:
    assert [tl.hm(s) for s in (3900, 1800, 59, 0, 36000, -1800)] == [
        "1h05",
        "0h30",
        "0h00",
        "0h00",
        "10h00",
        "-0h30",
    ]
    assert tl.hm_minutes(90) == "1h30"


def test_a_switch_and_a_pause_make_two_spans_and_no_third() -> None:
    marks = [
        mark(1, at(DAY, 9), tl.START, key="acme-shop-12"),
        mark(2, at(DAY, 10), tl.SWITCH, label="call"),
        mark(3, at(DAY, 10, 30), tl.PAUSE),
    ]
    log = tl.day_log(marks, DAY, at(NEXT, 12), TZ)
    assert tl.hm(log.total) == "1h30"
    assert log.by_card == [("acme-shop-12", "acme-shop", 3600)]
    assert log.by_label == [("call", 1800)]
    assert log.by_project == [("acme-shop", 3600), (None, 1800)]
    assert [(s.start.strftime("%H:%M"), s.minutes) for s in log.segments] == [
        ("09:00", 60),
        ("10:00", 30),
    ]
    assert log.unclosed == [] and log.current is None and not log.today
    assert log.offset == "UTC+02:00" and [m.id for m in log.marks] == [1, 2, 3]


def test_a_pause_is_a_gap_until_the_next_start() -> None:
    marks = [
        mark(1, at(DAY, 9), tl.START, key="acme-shop-1"),
        mark(2, at(DAY, 10), tl.PAUSE, label="lunch"),
        mark(3, at(DAY, 11), tl.START, key="demo-api-4"),
        mark(4, at(DAY, 11, 15), tl.PAUSE),
    ]
    log = tl.day_log(marks, DAY, at(NEXT, 9), TZ)
    assert tl.hm(log.total) == "1h15"
    assert log.by_project == [("acme-shop", 3600), ("demo-api", 900)]
    only = tl.day_log(marks, DAY, at(NEXT, 9), TZ, project="demo-api")
    assert only.total == 900 and [m.id for m in only.marks] == [3]


def test_a_span_over_midnight_is_split_between_the_days() -> None:
    marks = [
        mark(1, at(DAY, 23, 30), tl.START, key="acme-shop-12"),
        mark(2, at(NEXT, 0, 30), tl.PAUSE),
    ]
    now = at(NEXT, 9)
    first = tl.day_log(marks, DAY, now, TZ)
    # The second day reads the mark before it too (repo `time_marks_around`).
    second = tl.day_log(marks, NEXT, now, TZ)
    assert (first.total, second.total) == (1800, 1800)
    assert second.segments[0].start.strftime("%H:%M") == "00:00"
    linked = tl.chain(marks)
    card = tl.card_times([linked[0]], now, TZ, days=5)
    assert (card.total, card.today, card.days) == (3600, 1800, [(NEXT, 1800), (DAY, 1800)])


def test_a_past_days_last_span_counts_0_and_is_unclosed() -> None:
    marks = [mark(1, at(DAY, 17, 10), tl.START, key="acme-shop-3")]
    log = tl.day_log(marks, DAY, at(NEXT, 10), TZ)
    assert log.total == 0 and len(log.unclosed) == 1
    assert log.unclosed[0].start.strftime("%H:%M") == "17:10" and log.unclosed[0].end is None
    # It does not run into the next day either.
    assert tl.day_log(marks, NEXT, at(NEXT, 10), TZ).total == 0


def test_a_start_on_a_later_day_leaves_the_day_before_unclosed() -> None:
    marks = [
        mark(1, at(DAY, 17), tl.START, key="acme-shop-3"),
        mark(2, at(NEXT, 9), tl.START, key="acme-shop-3"),
    ]
    now = at(NEXT, 9, 45)
    assert tl.day_log(marks, DAY, now, TZ).total == 0
    today = tl.day_log(marks, NEXT, now, TZ)
    assert today.total == 45 * 60 and today.unclosed == []


def test_a_long_night_does_not_count_whatever_the_next_mark() -> None:
    """17:00 Mon -> switch Tue 09:00 books no night: Mon counts 0 and lists it unclosed."""
    now = at(NEXT, 9, 45)
    marks = [
        mark(1, at(DAY, 17), tl.START, key="acme-shop-1"),
        mark(2, at(NEXT, 9), tl.SWITCH, label="email"),
    ]
    monday = tl.day_log(marks, DAY, now, TZ)
    assert monday.total == 0 and [u.mark_id for u in monday.unclosed] == [1]
    assert monday.unclosed[0].start.strftime("%H:%M") == "17:00"
    assert tl.day_log(marks, NEXT, now, TZ).total == 45 * 60  # only the email since 09:00
    # Over two midnights it never counts, however short.
    later = NEXT + timedelta(days=1)
    two = [
        mark(1, at(DAY, 23, 59), tl.START, key="acme-shop-1"),
        mark(2, at(later, 0, 1), tl.PAUSE),
    ]
    assert [tl.day_log(two, d, at(later, 9), TZ).total for d in (DAY, NEXT, later)] == [0, 0, 0]
    # A short night counts up to MAX_OVERNIGHT_SECONDS, split between the days.
    short = [
        mark(1, at(DAY, 22), tl.START, key="acme-shop-1"),
        mark(2, at(NEXT, 2), tl.SWITCH, label="email"),
    ]
    assert tl.MAX_OVERNIGHT_SECONDS == 4 * 3600
    assert tl.day_log(short, DAY, now, TZ).total == 2 * 3600
    longer = [mark(1, at(DAY, 21, 59), tl.START, key="acme-shop-1"), short[1]]
    assert tl.day_log(longer, DAY, now, TZ).total == 0


def test_a_mark_that_leaves_an_earlier_day_unclosed_says_so() -> None:
    monday = mark(1, at(DAY, 17), tl.START, key="acme-shop-1")
    warned = tl.mark_warnings(tl.SWITCH, at(NEXT, 9), None, monday, TZ, monday)
    assert warned[0] == (
        "Note: acme-shop-1 since Mon 17:00 stays unclosed (0h00): close it with"
        " timelog_mark(kind='pause', at='2026-11-23THH:MM')."
    )
    night = mark(1, at(DAY, 23, 30), tl.START, key="acme-shop-1")
    running = tl.running_at(night, at(NEXT, 0, 30), tl.PAUSE, TZ)
    assert running is night
    assert not tl.mark_warnings(tl.PAUSE, at(NEXT, 0, 30), running, night, TZ, night)
    assert tl.running_at(monday, at(NEXT, 9), tl.SWITCH, TZ) is None


def test_todays_open_span_runs_until_now() -> None:
    marks = [mark(1, at(NEXT, 9), tl.START, key="acme-shop-12")]
    log = tl.day_log(marks, NEXT, at(NEXT, 9, 35), TZ)
    assert log.today and tl.hm(log.total) == "0h35"
    assert log.current is not None and log.current.key == "acme-shop-12" and log.current.running
    later = tl.day_log(marks, NEXT, at(NEXT, 11, 5), TZ)
    assert tl.hm(later.total) == "2h05"
    card = tl.card_times(marks, at(NEXT, 9, 35), TZ)
    assert card.current and card.since is not None and card.since.strftime("%H:%M") == "09:00"


def test_at_reads_a_clock_time_or_an_iso_time() -> None:
    now = at(NEXT, 12)
    assert tl.parse_at("09:15", now, TZ) == at(NEXT, 9, 15)
    assert tl.parse_at("9:05", now, TZ) == at(NEXT, 9, 5)
    assert tl.parse_at("2026-11-24T08:00+02:00", now, TZ) == at(NEXT, 8)
    assert tl.parse_at("2026-11-24T06:00Z", now, TZ) == at(NEXT, 8)
    assert tl.parse_at("2026-11-24T08:00", now, TZ) == at(NEXT, 8)  # no offset: local
    assert tl.parse_at(None, now, TZ) == now
    assert tl.parse_at("17:30", now, TZ, day=DAY) == at(DAY, 17, 30)  # a fix: the mark's day
    assert tl.parse_at("12:00", now + timedelta(seconds=30), TZ) == now
    with pytest.raises(ValidationError, match="in the future"):
        tl.parse_at("12:02", now, TZ)
    with pytest.raises(ValidationError, match="HH:MM"):
        tl.parse_at("half past nine", now, TZ)
    with pytest.raises(ValidationError, match="HH:MM"):
        tl.parse_at("25:00", now, TZ)


def test_a_mark_names_one_target() -> None:
    tl.check_target(tl.START, "acme-shop-1", None)
    tl.check_target(tl.SWITCH, None, "call")
    tl.check_target(tl.PAUSE, None, "lunch")
    for kind, key, label in (
        (tl.START, None, None),
        (tl.SWITCH, "acme-shop-1", "call"),
        (tl.PAUSE, "acme-shop-1", None),
        ("stop", None, None),
    ):
        with pytest.raises(ValidationError):
            tl.check_target(kind, key, label)


def test_odd_sequences_warn() -> None:
    running = mark(1, at(NEXT, 9), tl.START, key="acme-shop-1")
    warned = tl.mark_warnings(tl.START, at(NEXT, 10), running, running, TZ)
    assert any("acme-shop-1 was running since 09:00" in w for w in warned)
    assert tl.mark_warnings(tl.PAUSE, at(NEXT, 10), None, None, TZ) == [
        "Note: nothing was running; this pause ends no span."
    ]
    early = tl.mark_warnings(tl.SWITCH, at(NEXT, 8), None, running, TZ)
    assert early[0].startswith("Warning: this mark is before the latest one (#1 at 09:00)")


# --- the service: idempotency and "now" --------------------------------------------------


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    await service.upsert_project("acme-shop")
    await service.create_task("acme-shop", "Checkout")
    await service.create_task("acme-shop", "Cart")
    try:
        yield service
    finally:
        await service.close()


async def test_the_same_mark_within_a_minute_is_one_row(svc: Service) -> None:
    first = await svc.mark_time("start", task="acme-shop-1", session="lead")
    again = await svc.mark_time("start", task="acme-shop-1", session="lead")
    assert first.created and not again.created and again.mark.id == first.mark.id
    assert again.warnings[0].startswith("Already marked: #1")
    log = await svc.day_log()
    assert [m.id for m in log.marks] == [1]
    events = [e for e in await svc.events_since(0) if e.kind == "timelog.marked"]
    assert len(events) == 1
    assert events[0].payload["keys"] == ["acme-shop-1"]
    assert events[0].payload["mark_kind"] == "start"


async def test_working_on_this_now_starts_switches_or_does_nothing(svc: Service) -> None:
    started = await svc.mark_time("now", task="acme-shop-1", actor="board")
    assert started.created and started.mark.kind == "start"
    same = await svc.mark_time("now", task="acme-shop-1", actor="board")
    assert not same.created and same.warnings[0].startswith("Already on acme-shop-1 since")
    moved = await svc.mark_time("now", task="acme-shop-2", actor="board")
    assert moved.created and moved.mark.kind == "switch"
    assert moved.closed is not None and moved.closed.key == "acme-shop-1"
    [event] = [e for e in await svc.events_since(0) if e.payload.get("id") == moved.mark.id]
    assert event.payload["keys"] == ["acme-shop-2", "acme-shop-1"]
    times = await svc.task_times(1, [await svc.get_task("acme-shop-1")])
    assert not times[1].current


async def test_a_label_is_scanned_for_secrets(svc: Service) -> None:
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    with pytest.raises(ValidationError):
        await svc.mark_time("start", label=f"call {token}")
    assert (await svc.day_log()).marks == []


async def test_a_fix_is_scanned_under_the_marks_own_project(svc: Service) -> None:
    await project_layer(svc, "acme-shop", {"secrets": {"disable": ["jwt"]}})
    card = await svc.mark_time("start", task="acme-shop-1", at="2026-11-23T09:00+02:00")
    label = await svc.mark_time("start", label="call", at="2026-11-23T10:00+02:00")
    fixed, _, _ = await svc.fix_time_mark(card.mark.id, f"pasted {JWT}", at="08:00")
    assert fixed is not None and fixed.task_key == "acme-shop-1"
    with pytest.raises(ValidationError, match=r"\(jwt\)"):
        await svc.fix_time_mark(label.mark.id, f"pasted {JWT}", delete=True)
    gone, _, _ = await svc.fix_time_mark(card.mark.id, f"again {JWT}", delete=True)
    assert gone is None
