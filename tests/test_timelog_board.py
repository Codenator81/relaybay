"""The day time log on the board (relaybay-42): the `/` day panel, the card chip, the task's Time
row, the board's writes (cookie + CSRF only) and the portfolio digest line."""

import zlib
from datetime import date, datetime, timedelta

from starlette.testclient import TestClient

from relaybay.security import csrf_token
from relaybay.service import Service, collect_notes
from tests.conftest import BASE, run_in_app

FORM = {"Origin": BASE}
HTMX = {**FORM, "HX-Request": "true"}


def yesterday() -> date:
    return datetime.now().astimezone().date() - timedelta(days=1)


def local(day: date, hour: int, minute: int = 0) -> str:
    return datetime(day.year, day.month, day.day, hour, minute).astimezone().isoformat()


async def seed_day(service: Service, day: date) -> None:
    """Three projects, two labels, a pause and an unclosed span; total 6h00."""
    for slug in ("acme-shop", "demo-api", "docs-site"):
        await service.upsert_project(slug)
    await service.create_task("acme-shop", "Checkout")
    await service.create_task("acme-shop", "Cart")
    await service.create_task("demo-api", "Webhooks")
    await service.create_task("docs-site", "Guide")
    for kind, task, label, hour, minute in (
        ("start", "acme-shop-1", None, 9, 0),
        ("switch", None, "call", 10, 15),
        ("switch", "demo-api-1", None, 10, 45),
        ("pause", None, "lunch", 12, 0),
        ("start", "docs-site-1", None, 13, 0),
        ("switch", "acme-shop-2", None, 14, 30),
        ("pause", None, None, 16, 0),
        ("start", "demo-api-1", None, 17, 10),
    ):
        await service.mark_time(kind, task=task, label=label, at=local(day, hour, minute))


def service_of(board: TestClient) -> Service:
    return board.app.state.service  # type: ignore[attr-defined]


def test_the_day_panel_shows_the_strip_the_totals_and_the_marks(board: TestClient) -> None:
    day = yesterday()
    run_in_app(board, seed_day, service_of(board), day)
    page = board.get(f"/?day={day.isoformat()}").text
    assert 'id="daylog"' in page and '<span class="daylog-total">6h00</span>' in page
    assert "<title>acme-shop-1 · 09:00-10:15 · 1h15</title>" in page
    assert 'class="tl-seg tl-label' in page and "<title>call · 10:15-10:45 · 0h30</title>" in page
    # One colour rule for every project: the CRC-32 of its slug.
    assert f'class="tl-chip tl-c{zlib.crc32(b"docs-site") % 8}">docs-site 1h30' in page
    assert "is-unclosed" in page and "demo-api-1 · since 17:10 · unclosed, counts 0h00" in page
    for chip in ("acme-shop 2h45", "docs-site 1h30", "demo-api 1h15", "labels 0h30"):
        assert chip in page, chip
    assert "demo-api-1 since 17:10 is unclosed and counts 0h00" in page
    assert f"(it goes on {day.isoformat()})" in page
    assert "Marks of the day (8)" in page and 'hx-post="/timelog/1/delete"' in page
    assert f'href="/?day={(day - timedelta(days=1)).isoformat()}"' in page
    fragment = board.get(f"/fragments/daylog?day={day.isoformat()}").text
    assert "6h00" in fragment and "<main" not in fragment
    # The projects list: only the numbers that are not 0.
    assert "total 1h15" in page and "today 0h00" not in page


def test_an_empty_day_is_one_muted_line(board: TestClient) -> None:
    page = board.get("/?day=2026-01-05").text
    assert "No time marked on this day." in page and "tl-strip" not in page
    assert "Today" in page  # the way back
    assert "Day must be" not in page
    bad = board.get("/fragments/daylog?day=soon").text
    assert "day must be a date" in bad


def test_the_card_chip_and_the_task_time_row(board: TestClient, token: str) -> None:
    service = service_of(board)
    run_in_app(board, seed_day, service, yesterday())
    plain = board.get("/p/acme-shop").text
    assert "time-chip" not in plain  # no time today on any card
    form = {"csrf_token": csrf_token(token), "kind": "now"}
    view = board.post("/p/acme-shop/t/acme-shop-1/timelog", data=form, headers=HTMX)
    assert view.status_code == 200 and "now since" in view.text and ">Pause</button>" in view.text
    again = board.post("/p/acme-shop/t/acme-shop-1/timelog", data=form, headers=HTMX)
    assert again.status_code == 200
    log = run_in_app(board, service.day_log)
    assert [m.kind for m in log.marks] == ["start"]  # "Working on this now" twice: one mark
    page = board.get("/p/acme-shop").text
    since = log.marks[0].at.astimezone().strftime("%H:%M")
    assert 'class="chip time-chip is-now"' in page
    assert f">now since {since} · today 0h00</span>" in page
    card = board.get("/p/acme-shop/t/acme-shop-1/card").text
    assert "time-chip is-now" in card
    assert "time-chip" not in board.get("/p/acme-shop/t/acme-shop-2/card").text
    task = board.get("/t/acme-shop-1").text
    assert "today 0h00 · total 1h15 · in tracker 0h00" in task
    paused = board.post(
        "/p/acme-shop/t/acme-shop-1/timelog", data={**form, "kind": "pause"}, headers=HTMX
    )
    assert "Working on this now" in paused.text
    assert [m.kind for m in run_in_app(board, service.day_log).marks] == ["start", "pause"]


def test_board_time_writes_need_the_cookie_and_the_csrf_token(
    board: TestClient, token: str
) -> None:
    service = service_of(board)
    day = yesterday()
    run_in_app(board, seed_day, service, day)
    form = {
        "csrf_token": csrf_token(token),
        "kind": "switch",
        "target": "label",
        "what": "email",
        "at": "16:30",
        "day": day.isoformat(),
    }
    bearer = board.post("/timelog", data=form, headers={**FORM, "Authorization": f"Bearer {token}"})
    assert bearer.status_code == 403
    assert board.post("/timelog", data={**form, "csrf_token": "x"}, headers=FORM).status_code == 403
    for path in ("/timelog/1/delete", "/p/acme-shop/t/acme-shop-1/timelog"):
        assert board.post(path, data={"csrf_token": "x"}, headers=FORM).status_code == 403
        headers = {**FORM, "Authorization": f"Bearer {token}"}
        assert board.post(path, data=form, headers=headers).status_code == 403
    assert len(run_in_app(board, service.day_log, day).marks) == 8
    # The form's HH:MM is on the day the panel shows.
    saved = board.post("/timelog", data=form, headers=HTMX)
    assert saved.status_code == 200 and "email" in saved.text
    log = run_in_app(board, service.day_log, day)
    assert [(m.kind, m.label) for m in log.marks if m.label == "email"] == [("switch", "email")]
    card = board.post(
        "/timelog",
        data={**form, "target": "card", "what": "docs-site-1", "at": "16:45"},
        headers=HTMX,
    )
    assert card.status_code == 200
    log = run_in_app(board, service.day_log, day)
    assert ("docs-site-1", "docs-site", 6900) in log.by_card  # 1h30 + 16:45-17:10
    missing = board.post(
        "/timelog", data={**form, "target": "card", "what": "acme-shop-99"}, headers=HTMX
    )
    assert "not found" in missing.text.lower()
    # A label that looks like a key stays a label: the owner chose it.
    keyish = board.post("/timelog", data={**form, "what": "ops-1", "at": "16:50"}, headers=HTMX)
    assert keyish.status_code == 200
    log = run_in_app(board, service.day_log, day)
    assert [m.label for m in log.marks if m.label == "ops-1"] == ["ops-1"]
    bad_time = board.post("/timelog", data={**form, "at": "25:99"}, headers=HTMX)
    assert f"({day.isoformat()}, server time)" in bad_time.text
    assert "&#39;25:99&#39;" in bad_time.text
    no_time = board.post("/timelog", data={**form, "at": ""}, headers=HTMX)
    assert f"A mark on {day.isoformat()} needs its time" in no_time.text
    # Delete on the board: the event keeps the mark and the reason, on the audit.
    deleted = board.post(
        "/timelog/1/delete",
        data={"csrf_token": csrf_token(token), "day": day.isoformat()},
        headers=HTMX,
    )
    assert deleted.status_code == 200
    events = run_in_app(board, service.events_since, 0)
    [removed] = [e for e in events if e.kind == "timelog.removed"]
    assert removed.payload["reason"] == "removed on the board" and removed.actor == "board"
    assert "removed on the board" in board.get("/audit?changes=1").text


async def test_the_portfolio_digest_has_a_line_for_today(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.create_task("acme-shop", "Checkout")
    assert "Time today" not in await service.portfolio_digest()
    await service.mark_time("start", task="acme-shop-1")
    text = await service.portfolio_digest()
    assert "Time today 0h00; now acme-shop-1 since " in text and "(timelog_list)" in text


async def test_a_fix_that_moves_a_mark_to_an_earlier_day_names_it(service: Service) -> None:
    day = yesterday()
    await service.upsert_project("acme-shop")
    await service.create_task("acme-shop", "Checkout")
    await service.create_task("acme-shop", "Cart")
    await service.mark_time("start", task="acme-shop-1", at=local(day, 9))
    later = await service.mark_time("start", task="acme-shop-2", at=local(day + timedelta(1), 0, 1))
    with collect_notes() as notes:
        await service.fix_time_mark(later.mark.id, "it was yesterday", at=local(day, 18))
    assert len(notes) == 1 and notes[0].startswith("Note: acme-shop-2 since ")
    assert "18:00 stays unclosed (0h00)" in notes[0]


async def test_a_fix_that_leaves_an_earlier_day_unclosed_says_so(service: Service) -> None:
    day = yesterday()
    await service.upsert_project("acme-shop")
    await service.create_task("acme-shop", "Checkout")
    await service.mark_time("start", task="acme-shop-1", at=local(day, 9))
    pause = await service.mark_time("pause", at=local(day, 11))
    with collect_notes() as notes:
        await service.fix_time_mark(pause.mark.id, "never paused", delete=True)
    assert notes == [
        f"Note: acme-shop-1 since {datetime.fromisoformat(local(day, 9)).strftime('%a')} 09:00"
        f" stays unclosed (0h00): close it with timelog_mark(kind='pause',"
        f" at='{day.isoformat()}THH:MM')."
    ]


def test_a_pause_on_the_task_needs_that_card_running(board: TestClient, token: str) -> None:
    service = service_of(board)
    run_in_app(board, seed_day, service, yesterday())
    form = {"csrf_token": csrf_token(token), "kind": "pause"}
    idle = board.post("/p/acme-shop/t/acme-shop-1/timelog", data=form, headers=HTMX)
    assert "Not paused: nothing is running, not acme-shop-1. Nothing changed." in idle.text
    run_in_app(board, service.mark_time, "start", task="demo-api-1")
    other = board.post("/p/acme-shop/t/acme-shop-1/timelog", data=form, headers=HTMX)
    assert "Not paused: demo-api-1 is running, not acme-shop-1." in other.text
    assert [m.kind for m in run_in_app(board, service.day_log).marks] == ["start"]


def test_no_page_carries_an_inline_style(board: TestClient) -> None:
    """The board's CSP drops inline styles: the strip is SVG geometry, never `style=`."""
    service = service_of(board)
    day = yesterday()
    run_in_app(board, seed_day, service, day)
    run_in_app(board, service.mark_time, "start", task="acme-shop-1")
    for path in (
        "/",
        f"/?day={day.isoformat()}",
        f"/fragments/daylog?day={day.isoformat()}",
        "/p/acme-shop",
        "/p/acme-shop/t/acme-shop-1/card",
        "/p/acme-shop/t/acme-shop-1/view",
        "/t/acme-shop-1",
    ):
        answer = board.get(path)
        assert answer.status_code == 200, path
        assert 'style="' not in answer.text, path
