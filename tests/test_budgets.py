"""Token budgets: which tokens count where, thresholds once per period, warn and stop, the new
UTC day, a session on several tasks, a file that shrank, concurrency, and the board."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.domain.errors import ValidationError
from relaybay.domain.limits import level_for
from relaybay.domain.usage import parse_line
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_usage import report_session, usage_line, write_lines

FORM = {"Origin": BASE}
# usage_line counts tokens * 10 in all.


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    await service.start()
    await service.upsert_project("acme-shop", repo_path="/d/acme")
    await service.upsert_project("demo-api", repo_path="/d/demo")
    try:
        yield service
    finally:
        await service.close()


def soon(seconds: float = 1) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def use(svc: Service, path: Path, *lines: str, cc: str = "cc-1", cwd: str = "/d/acme"):
    """Append transcript lines, read them and run the budget check."""
    await report_session(svc, path, cc=cc, cwd=cwd)
    with path.open("a", encoding="utf-8") as out:
        for line in lines:
            out.write(line + "\n")
    await svc.usage_pass()
    await svc.limits_pass()


async def kinds_of(svc: Service) -> list[str]:
    events = await svc.activity(limit=200)
    return [e.kind for e in reversed(events) if e.kind.startswith(("budget.", "brake."))]


def test_level_for() -> None:
    assert level_for(79, 100) is None
    assert level_for(80, 100) == "warned"
    assert level_for(99, 100) == "warned"
    assert level_for(100, 100) == "exceeded"


def test_a_line_keeps_its_utc_time() -> None:
    raw = usage_line().replace(
        usage_line().split('"timestamp": "')[1].split('"')[0], "2026-03-01T23:30:00+02:00"
    )
    line = parse_line(raw.encode())
    assert line.day == "2026-03-01"
    assert line.at == "2026-03-01T21:30:00.000+00:00"


async def test_warn_is_the_default_and_counts_once(svc: Service, tmp_path: Path) -> None:
    budget = await svc.set_budget("project", project="acme-shop", max_tokens=1000)
    assert (budget.action, budget.period) == ("warn", "day")
    path = tmp_path / "t.jsonl"
    await use(svc, path, usage_line("m1", tokens=79))  # 790
    assert await kinds_of(svc) == ["budget.set"]
    await use(svc, path, usage_line("m2", tokens=1))  # 800
    assert await kinds_of(svc) == ["budget.set", "budget.warned"]
    await use(svc, path, usage_line("m3", tokens=1))
    assert await kinds_of(svc) == ["budget.set", "budget.warned"]
    await use(svc, path, usage_line("m4", tokens=50))  # 1310
    assert await kinds_of(svc) == ["budget.set", "budget.warned", "budget.exceeded"]
    await svc.limits_pass()
    assert (await kinds_of(svc)).count("budget.exceeded") == 1
    # Warning only: no brake, tools still work.
    assert await svc.open_brakes() == []
    [state] = await svc.budget_states()
    assert (state.used, state.level, state.percent) == (1310, "exceeded", 131)
    inbox = await svc.inbox(["owner"])
    assert [s.budget.id for s in inbox.budgets] == [budget.id]


async def test_one_jump_past_both_thresholds_is_one_event(svc: Service, tmp_path: Path) -> None:
    await svc.set_budget("all", period="total", max_tokens=100)
    await use(svc, tmp_path / "t.jsonl", usage_line("m1", tokens=20))
    assert await kinds_of(svc) == ["budget.set", "budget.exceeded"]
    await svc.limits_pass()
    assert await kinds_of(svc) == ["budget.set", "budget.exceeded"]


async def test_the_total_is_the_usage_page_total(svc: Service, tmp_path: Path) -> None:
    await svc.set_budget("all", period="total", max_tokens=10**9)
    await svc.set_budget("project", project="acme-shop", period="total", max_tokens=10**9)
    await svc.set_budget("project", project="demo-api", period="total", max_tokens=10**9)
    await use(svc, tmp_path / "a.jsonl", usage_line("a1", tokens=7), cc="a")
    await use(svc, tmp_path / "d.jsonl", usage_line("d1", tokens=9), cc="d", cwd="/d/demo")
    report = await svc.usage_report(days=0)
    by_target = {s.budget.target: s.used for s in await svc.budget_states()}
    assert by_target["all projects"] == report.total.tokens == 160
    # Cache reads are in it: the page's total, not input plus output.
    acme = await svc.usage_report(project="acme-shop", days=0)
    assert by_target["project acme-shop"] == acme.total.tokens == 70
    assert by_target["project demo-api"] == 90


async def test_a_stop_budget_uses_the_brake_and_the_owner_releases(
    svc: Service, tmp_path: Path
) -> None:
    await svc.set_budget(
        "project", project="acme-shop", period="total", max_tokens=100, action="stop"
    )
    path = tmp_path / "t.jsonl"
    await use(svc, path, usage_line("m1", tokens=20))
    [brake] = await svc.open_brakes()
    assert (brake.source, brake.scope, brake.created_by) == ("budget", "project", "budget")
    assert "token budget of project acme-shop exceeded: 200 of 100 tokens in total" in brake.reason
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": "cc-1",
        "cwd": "/d/acme",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    answer = await svc.hook_answer(payload)
    assert answer["continue"] is False
    other = {**payload, "session_id": "cc-d", "cwd": "/d/demo"}
    await svc.record_hook(other)
    assert await svc.hook_answer(other) is None

    # A total budget's brake is released only by the owner, and released stays released.
    await svc.limits_pass()
    assert len(await svc.open_brakes()) == 1
    await svc.release_brake(brake.id)
    await use(svc, path, usage_line("m2", tokens=20))
    assert await svc.open_brakes() == []


async def test_mark_event_and_brake_commit_together(
    svc: Service, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    budget = await svc.set_budget("all", period="total", max_tokens=100, action="stop")
    await use(svc, tmp_path / "a.jsonl")
    write_lines(tmp_path / "a.jsonl", usage_line("m1", tokens=20))
    await svc.usage_pass()

    async def broken(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr("relaybay.db.repo.insert_brake", broken)
        with pytest.raises(RuntimeError):
            await svc.limits_pass()
    assert await kinds_of(svc) == ["budget.set"]
    [state] = await svc.budget_states()
    assert state.level is None and state.budget.id == budget.id
    await svc.limits_pass()
    assert await kinds_of(svc) == ["budget.set", "budget.exceeded", "brake.set"]
    assert len(await svc.open_brakes()) == 1


async def test_a_daily_brake_ends_with_the_utc_day(svc: Service, tmp_path: Path) -> None:
    await svc.set_budget("all", period="day", max_tokens=100, action="stop")
    await use(svc, tmp_path / "t.jsonl", usage_line("m1", tokens=20))
    [brake] = await svc.open_brakes()
    await svc.limits_pass()  # the same day: it stays
    assert len(await svc.open_brakes()) == 1

    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="milliseconds")
    async with svc._tx() as tx:
        await tx.conn.execute("UPDATE brake SET created_at = ?", (yesterday,))
        await tx.conn.execute("UPDATE usage_day SET day = ?", (yesterday[:10],))
        await tx.conn.execute("UPDATE budget_mark SET period_key = ?", (yesterday[:10],))
    await svc.limits_pass()
    assert await svc.open_brakes() == []
    released = await svc.activity(kind="brake.released")
    assert released[0].actor == "new day" and released[0].payload["brake_id"] == brake.id
    [state] = await svc.budget_states()
    assert (state.used, state.level) == (0, None)
    assert (await svc.inbox(["owner"])).budgets == []


async def test_a_new_day_leaves_the_owners_brake_alone(svc: Service) -> None:
    await svc.set_board_brake("all", reason="Mine")
    async with svc._tx() as tx:
        await tx.conn.execute("UPDATE brake SET created_at = '2020-01-01T00:00:00.000+00:00'")
    await svc.limits_pass()
    assert len(await svc.open_brakes()) == 1


async def test_changing_a_budget_counts_it_again(svc: Service, tmp_path: Path) -> None:
    budget = await svc.set_budget("all", period="total", max_tokens=100)
    await use(svc, tmp_path / "t.jsonl", usage_line("m1", tokens=20))
    assert (await svc.budget_states())[0].level == "exceeded"
    # The same numbers change nothing.
    same = await svc.set_budget("all", period="total", max_tokens=100)
    assert same.id == budget.id and (await kinds_of(svc)).count("budget.set") == 1
    assert (await svc.budget_states())[0].level == "exceeded"
    # Raised: the threshold is new again. One row per target.
    raised = await svc.set_budget("all", period="total", max_tokens=1000)
    assert raised.id == budget.id and len(await svc.budget_states()) == 1
    assert (await svc.budget_states())[0].level is None
    await svc.limits_pass()
    assert (await svc.budget_states())[0].level is None
    # Warn to stop while over: the brake comes at the next check.
    await svc.set_budget("all", period="total", max_tokens=100, action="stop")
    await svc.limits_pass()
    assert len(await svc.open_brakes()) == 1
    await svc.remove_budget(budget.id)
    assert await svc.budget_states() == []
    assert len(await svc.open_brakes()) == 1  # the brake stays until the owner releases it


async def test_budget_validation(svc: Service) -> None:
    for kwargs, text in (
        ({"max_tokens": "many"}, "whole number"),
        ({"max_tokens": 0}, "from 1"),
        ({"max_tokens": 5, "period": "week"}, "period"),
        ({"max_tokens": 5, "action": "kill"}, "action"),
    ):
        with pytest.raises(ValidationError, match=text):
            await svc.set_budget("all", **kwargs)
    with pytest.raises(ValidationError, match="scope"):
        await svc.set_budget("session", max_tokens=5)
    with pytest.raises(ValidationError, match="needs its project"):
        await svc.set_budget("project", max_tokens=5)
    with pytest.raises(ValidationError, match="needs its task"):
        await svc.set_budget("task", max_tokens=5)
    assert (await svc.set_budget("all", max_tokens="1,000 000")).max_tokens == 1000000


# --- tasks ------------------------------------------------------------------


async def test_task_budget_counts_only_the_bound_time(svc: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    await report_session(svc, path)
    task = await svc.create_task("acme-shop", "Work", session="worker", hook_session="cc-1")
    await svc.set_budget("task", task=task.key, period="total", max_tokens=10**6)
    before = datetime.now(UTC) - timedelta(minutes=5)
    await use(
        svc,
        path,
        usage_line("old", tokens=100, day=before),  # before the binding
        usage_line("new", tokens=3, day=soon()),
    )
    detail = await svc.task_detail(task.key)
    assert detail.budget is not None and detail.budget.used == 30
    await svc.unbind_session(task.key)
    await use(svc, path, usage_line("after", tokens=5, day=soon(5)))
    assert (await svc.task_detail(task.key)).budget.used == 30


async def test_one_session_on_two_tasks_counts_in_full_on_each(
    svc: Service, tmp_path: Path
) -> None:
    path = tmp_path / "t.jsonl"
    await report_session(svc, path)
    one = await svc.create_task("acme-shop", "One", session="worker", hook_session="cc-1")
    two = await svc.create_task("acme-shop", "Two", session="worker", hook_session="cc-1")
    await svc.set_budget("task", task=one.key, period="total", max_tokens=100, action="stop")
    await svc.set_budget("task", task=two.key, period="day", max_tokens=10**6)
    await use(svc, path, usage_line("m1", tokens=11, day=soon()))
    states = {s.budget.target: s.used for s in await svc.budget_states()}
    assert states == {f"task {one.key}": 110, f"task {two.key}": 110}
    [brake] = await svc.open_brakes()
    assert (brake.scope, brake.task_key) == ("task", one.key)
    # A repeated message id in a second file counts nowhere twice.
    await use(svc, tmp_path / "b.jsonl", usage_line("m1", tokens=11, day=soon()))
    states = {s.budget.target: s.used for s in await svc.budget_states()}
    assert states[f"task {two.key}"] == 110


async def test_a_shrunk_file_gives_up_its_task_rows(svc: Service, tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    await report_session(svc, path)
    task = await svc.create_task("acme-shop", "Work", session="worker", hook_session="cc-1")
    await svc.set_budget("task", task=task.key, period="total", max_tokens=10**6)
    await use(svc, path, usage_line("m1", tokens=5, day=soon()), usage_line("m2", day=soon()))
    assert (await svc.task_detail(task.key)).budget.used == 150
    write_lines(path, usage_line("m3", tokens=2, day=soon()))
    await svc.usage_pass()
    assert (await svc.task_detail(task.key)).budget.used == 20


# --- hook lines -------------------------------------------------------------


async def test_a_warning_reaches_each_covered_session_once(svc: Service, tmp_path: Path) -> None:
    await svc.set_budget("project", project="acme-shop", period="total", max_tokens=100)
    await use(svc, tmp_path / "t.jsonl", usage_line("m1", tokens=9))  # 90: warned

    async def post_tool(cc: str, cwd: str = "/d/acme") -> str | None:
        payload = {
            "hook_event_name": "PostToolUse",
            "session_id": cc,
            "cwd": cwd,
            "tool_name": "Read",
            "tool_input": {},
        }
        await svc.record_hook(payload)
        answer = await svc.hook_answer(payload)
        return answer["hookSpecificOutput"]["additionalContext"] if answer else None

    first = await post_tool("cc-1")
    assert first is not None and "token budget of project acme-shop: 90 of 100" in first
    assert "only warns" in first
    assert await post_tool("cc-1") is None
    assert await post_tool("cc-2") is not None  # another session hears it too
    assert await post_tool("cc-d", "/d/demo") is None  # outside the budget
    # The next threshold is news again.
    await use(svc, tmp_path / "t.jsonl", usage_line("m2", tokens=9))
    again = await post_tool("cc-1")
    assert again is not None and "180 of 100" in again
    assert await post_tool("cc-1") is None


# --- the loop, concurrency --------------------------------------------------


async def test_the_loop_checks_with_usage_off_and_reads_nothing(tmp_path: Path) -> None:
    service = Service(tmp_path / "relaybay.sqlite", usage_enabled=False)
    await service.start()
    checked = asyncio.Event()

    async def spy() -> None:
        checked.set()

    async def no_read() -> None:
        raise AssertionError("usage_enabled = false reads nothing")

    service.limits_pass = spy  # type: ignore[method-assign]
    service.usage_pass = no_read  # type: ignore[method-assign]
    loop = asyncio.create_task(service.usage_loop(0.01))
    try:
        await asyncio.wait_for(checked.wait(), 5)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await service.close()


async def test_a_failing_check_keeps_the_loop_running(svc: Service) -> None:
    calls = []
    again = asyncio.Event()

    async def broken() -> None:
        calls.append(1)
        if len(calls) == 2:
            again.set()
        raise RuntimeError("boom")

    svc.limits_pass = broken  # type: ignore[method-assign]
    loop = asyncio.create_task(svc.usage_loop(0.01))
    try:
        await asyncio.wait_for(again.wait(), 5)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


async def test_the_loop_looks_every_minute_with_a_budget(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits = []

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(svc, "_limits_sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await svc.usage_loop(300)
    assert waits[0] == 300
    await svc.set_budget("all", max_tokens=5)
    waits.clear()
    with pytest.raises(asyncio.CancelledError):
        await svc.usage_loop(300)
    assert waits[0] == 60


async def test_concurrency_warns_once_per_ten_minutes_and_stops_nothing(tmp_path: Path) -> None:
    service = Service(tmp_path / "relaybay.sqlite", max_concurrent_sessions=1)
    await service.start()
    try:
        for cc in ("a", "b"):
            await service.record_hook(
                {"hook_event_name": "UserPromptSubmit", "session_id": cc, "cwd": f"/d/{cc}"}
            )
        await service.limits_pass()
        await service.limits_pass()
        [event] = await service.activity(kind="concurrency.over")
        assert (event.payload["running"], event.payload["max_sessions"]) == (2, 1)
        inbox = await service.inbox(["owner"])
        assert (inbox.running_over, inbox.running_cap) == (2, 1)
        assert await service.open_brakes() == []
        old = (datetime.now(UTC) - timedelta(minutes=11)).isoformat(timespec="milliseconds")
        async with service._tx() as tx:
            await tx.conn.execute("UPDATE event SET ts = ? WHERE kind = 'concurrency.over'", (old,))
        await service.limits_pass()
        assert len(await service.activity(kind="concurrency.over")) == 2
        # Sessions quiet for ten minutes are not running.
        async with service._tx() as tx:
            await tx.conn.execute("UPDATE session SET last_seen_at = ?", (old,))
        assert (await service.inbox(["owner"])).running_over is None
    finally:
        await service.close()


async def test_concurrency_is_off_by_default(svc: Service) -> None:
    for cc in ("a", "b"):
        await svc.record_hook(
            {"hook_event_name": "UserPromptSubmit", "session_id": cc, "cwd": f"/d/{cc}"}
        )
    await svc.limits_pass()
    assert await svc.activity(kind="concurrency.over") == []
    assert (await svc.inbox(["owner"])).running_over is None


# --- board ------------------------------------------------------------------


def test_board_sets_shows_and_removes_a_budget(board: TestClient, token: str) -> None:
    csrf = {"csrf_token": csrf_token(token)}
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Work")
    page = board.get("/usage").text
    assert "No budgets yet." in page and '<option value="warn" selected>' in page

    denied = board.post("/usage/budgets", data={"scope": "all", "max_tokens": "5"}, headers=FORM)
    assert denied.status_code in (400, 403)
    assert run_in_app(board, service.budget_states) == []

    page = board.post(
        "/usage/budgets", data={**csrf, "scope": "all", "max_tokens": "nope"}, headers=FORM
    ).text
    assert "The limit must be a whole number of tokens." in page
    page = board.post(
        "/usage/budgets",
        data={**csrf, "scope": "task", "task": task.key, "period": "total", "max_tokens": "1,000"},
        headers=FORM,
    ).text
    assert f"Saved the budget of task {task.key}." in page
    assert f"task {task.key}" in page and "<progress" in page
    assert f"task {task.key}" in board.get("/fragments/budgets").text
    view = board.get(f"/p/acme-shop/t/{task.key}/view").text
    assert "0 of 1,000 tokens in total" in view

    [state] = run_in_app(board, service.budget_states)
    denied = board.post(f"/usage/budgets/{state.budget.id}/delete", data={}, headers=FORM)
    assert denied.status_code in (400, 403)
    page = board.post(f"/usage/budgets/{state.budget.id}/delete", data=csrf, headers=FORM).text
    assert "Budget removed." in page
    assert run_in_app(board, service.budget_states) == []


def test_usage_page_says_budgets_are_blind_when_reading_is_off(settings: Any, token: str) -> None:
    from relaybay.app import create_app

    settings.usage_enabled = False
    with TestClient(create_app(settings), base_url=BASE) as client:
        client.get(f"/login?token={token}")
        assert "Budgets are blind" in client.get("/usage").text


async def test_a_new_budget_wakes_the_sleeping_loop(svc: Service, tmp_path: Path) -> None:
    await use(svc, tmp_path / "t.jsonl", usage_line("m1", tokens=20))
    loop = asyncio.create_task(svc.usage_loop(300))
    try:
        await asyncio.sleep(0.2)  # the first pass is done: it sleeps for 300 s now
        await svc.set_budget("all", period="total", max_tokens=100)
        async with asyncio.timeout(10):
            while not (await svc.budget_states())[0].level:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)


async def test_a_session_past_utc_midnight_is_told_again(svc: Service, tmp_path: Path) -> None:
    await svc.set_budget("all", period="day", max_tokens=100)
    await use(svc, tmp_path / "t.jsonl", usage_line("m1", tokens=9))
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "cc-1",
        "cwd": "/d/acme",
        "tool_name": "Read",
        "tool_input": {},
    }
    assert await svc.hook_answer(payload) is not None
    assert await svc.hook_answer(payload) is None
    # The same budget's mark under another day: the row carries its period.
    budget_id = (await svc.budget_states())[0].budget.id
    [told] = svc._budget_told[next(iter(svc._budget_told))]
    assert told[0] == budget_id and told[1] == datetime.now(UTC).date().isoformat()
    async with svc._tx() as tx:
        await tx.conn.execute("DELETE FROM budget_mark")
    svc._budget_told[next(iter(svc._budget_told))] = {(budget_id, "2020-01-01", "warned")}
    await svc.limits_pass()
    assert await svc.hook_answer(payload) is not None


async def test_changing_a_budget_tells_the_session_again(svc: Service, tmp_path: Path) -> None:
    await svc.set_budget("all", period="total", max_tokens=100)
    await use(svc, tmp_path / "t.jsonl", usage_line("m1", tokens=9))
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "cc-1",
        "cwd": "/d/acme",
        "tool_name": "Read",
        "tool_input": {},
    }
    assert await svc.hook_answer(payload) is not None
    assert await svc.hook_answer(payload) is None
    await svc.set_budget("all", period="total", max_tokens=110)
    await svc.limits_pass()
    again = await svc.hook_answer(payload)
    assert again is not None and "90 of 110" in again["hookSpecificOutput"]["additionalContext"]
