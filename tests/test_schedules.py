"""Schedules (design 0023, PR 4): the clock with the missed rule, reminders, read-only checks as
`check` runs, coordinator turns as wake lines, the coordinator's MCP tools, the board's page and
migration 0047. The clock and the local time are the test's own."""

import asyncio
import json
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.domain import coordinator as crules
from relaybay.domain import profiles as prules
from relaybay.domain import schedules as srules
from relaybay.domain.errors import ConflictError, ValidationError
from relaybay.domain.models import Event
from relaybay.runner.layers import LayerState
from relaybay.runner.stream import Result
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_coordinator_wakes import started
from tests.test_runs_service import CC_A, StubRuns, stub

START = datetime(2026, 11, 16, 9, 0, tzinfo=UTC)
READ_ONLY = ["Read", "Glob", "Grep", *prules._READ_ONLY_GIT]


@dataclass
class FixedLocal:
    """Local time at a fixed offset from UTC, or at `after` from `switch` on (one DST change)."""

    hours: int = 2
    switch: datetime | None = None  # the change, in UTC
    after: int = 3
    zone: str = "TEST"

    def _offset(self, moment: datetime) -> timedelta:
        late = self.switch is not None and moment >= self.switch
        return timedelta(hours=self.after if late else self.hours)

    def to_utc(self, local: datetime) -> datetime:
        guess = local.replace(tzinfo=UTC) - timedelta(hours=self.hours)
        return local.replace(tzinfo=UTC) - self._offset(guess)

    def to_local(self, moment: datetime) -> datetime:
        return (moment.astimezone(UTC) + self._offset(moment)).replace(tzinfo=None)

    def name(self) -> str:
        return self.zone


@dataclass
class Clock:
    now: datetime = START
    seen: list[datetime] = field(default_factory=list)

    def __call__(self) -> datetime:
        return self.now

    def move(self, **delta: float) -> datetime:
        self.now += timedelta(**delta)
        return self.now


async def make(path: Path, clock: Clock) -> Service:
    service = Service(path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, path / "home")  # type: ignore[assignment]
    service.memory_reader = lambda: None
    stub(service).loop = asyncio.get_running_loop()
    service.clock = clock
    service.local_time = FixedLocal()
    return service


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Any:
    ok = LayerState("command", True, "command hooks from your user settings", "registered")
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: ok)
    folder = tmp_path / "acme-shop"
    folder.mkdir()
    service = await make(tmp_path, clock)
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    try:
        yield service
    finally:
        await service.close()


def local(at: datetime) -> str:
    """`at` as the form's local `YYYY-MM-DD HH:MM` (FixedLocal: UTC+2)."""
    return (at + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")


async def kinds(svc: Service, kind: str) -> list[Event]:
    return [e for e in await repo.events_after(svc._r, 0, None, 5000) if e.kind == kind]


async def check(svc: Service, **values: Any) -> srules.Schedule:
    return await svc.create_schedule(
        {
            "kind": "check",
            "repeat": "once",
            "at": local(START + timedelta(minutes=10)),
            "task": "acme-shop-1",
            "prompt": "Is the CI of PR 51 green?",
            **values,
        }
    )


async def answer(svc: Service, run_id: int, text: str, subtype: str = "success") -> None:
    """The check's one turn ends with `text` as its answer."""
    turn_id = stub(svc).active.pop(run_id)
    await svc.turn_init(turn_id, str(uuid.uuid4()))  # each check its own session id
    error = subtype != "success"
    result = Result(subtype, error, 2, 10, 5, 6, 7, 8, text=text)
    await svc.turn_done(turn_id, "error" if error else "success", result, 0, None)


async def runs_of(svc: Service, schedule_id: int) -> list[srules.ScheduleRun]:
    return list(reversed(await svc.schedule_runs([schedule_id])))


# --- pure rules -------------------------------------------------------------------------------


def test_a_daily_time_stays_at_its_clock_time_across_a_dst_change() -> None:
    # The local clock goes from UTC+2 to UTC+3 at 01:00 UTC on 2027-03-28.
    zone = FixedLocal(switch=datetime(2027, 3, 28, 1, 0, tzinfo=UTC))
    before = srules.next_daily(datetime(2027, 3, 27, 5, 0, tzinfo=UTC), "08:30", False, zone)
    assert before == datetime(2027, 3, 27, 6, 30, tzinfo=UTC)  # 08:30 at UTC+2
    after = srules.next_daily(before, "08:30", False, zone)
    assert after == datetime(2027, 3, 28, 5, 30, tzinfo=UTC)  # 08:30 at UTC+3
    assert zone.to_local(after).strftime("%H:%M") == "08:30"


def test_weekdays_only_skips_the_weekend() -> None:
    friday_evening = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
    found = srules.next_daily(friday_evening, "08:30", True, FixedLocal())
    assert found == datetime(2026, 11, 23, 6, 30, tzinfo=UTC)  # Monday


def test_the_forms_values_are_checked() -> None:
    assert srules.clean_time_of_day("8:05") == "08:05"
    for bad in ("24:00", "8", "08:61"):
        with pytest.raises(ValidationError):
            srules.clean_time_of_day(bad)
    with pytest.raises(ValidationError, match="from 5 to 1440"):
        srules.clean_every("4")
    with pytest.raises(ValidationError, match="from 1 to 24"):
        srules.clean_max_times("25")
    assert srules.clean_max_times("") == 24
    assert srules.is_done_answer("**Done**: CI is green") and srules.is_done_answer("done")
    assert not srules.is_done_answer("Not done yet") and not srules.is_done_answer("doneness")


# --- reminders --------------------------------------------------------------------------------


async def test_a_reminder_fires_on_the_clock_without_claude(svc: Service, clock: Clock) -> None:
    made = await svc.create_schedule(
        {
            "kind": "reminder",
            "repeat": "once",
            "at": local(START + timedelta(minutes=10)),
            "prompt": "Question 7 waits",
        }
    )
    assert made.next_at == srules.iso(START + timedelta(minutes=10)) and made.timezone == "TEST"
    after = await svc.last_event_id()
    assert await svc.schedule_pass(clock.move(minutes=5)) is not None
    assert await runs_of(svc, made.id) == []
    await svc.schedule_pass(clock.move(minutes=5))
    [ran] = await runs_of(svc, made.id)
    assert (ran.outcome, ran.notice, ran.run_id) == ("ran", "Question 7 waits", None)
    assert stub(svc).started == []
    found = await svc.get_schedule(made.id)
    assert (found.next_at, found.times_run) == (None, 1) and found.done_at is not None
    _, notices = await svc.notifications(after)
    assert [(n.kind, n.title, n.text) for _, n in notices] == [
        ("reminder", "Reminder", "Question 7 waits")
    ]
    inbox = await svc.inbox([])
    assert [n.id for n in inbox.schedules] == [ran.id] and inbox.count >= 1
    await svc.mark_schedule_notice_seen(ran.id)
    assert (await svc.inbox([])).schedules == []
    [fired] = await kinds(svc, "schedule.fired")
    assert fired.payload["schedule_kind"] == "reminder" and fired.payload["outcome"] == "ran"
    # A reminder wakes no coordinator: only a coordinator turn's `schedule.fired` does.
    assert await repo.coordinator_wake_events(svc._r, 0, fired.id, 10) == ([], 0, 0)


# --- read-only checks -------------------------------------------------------------------------


async def test_a_check_runs_claude_once_read_only_and_reports_on_the_card(
    svc: Service, clock: Clock
) -> None:
    made = await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    [(run_id, _, spec)] = stub(svc).started
    run = await svc.get_run(run_id)
    assert (run.kind, run.task_id, run.project_slug, run.profile) == (
        "check",
        None,
        "acme-shop",
        "read-only",
    )
    assert spec.resume is None and spec.no_session_persistence and spec.max_turns == 8
    assert spec.mode == "dontAsk" and list(spec.allowed) == READ_ONLY
    assert set(prules.FIXED_DISALLOWED) <= set(spec.disallowed)
    assert spec.cwd == str(Path(svc.db_path).parent / "acme-shop")
    assert "relaybay check (schedule 1) for acme-shop-1" in spec.prompt
    assert spec.prompt.endswith("Is the CI of PR 51 green?") and '"done"' not in spec.prompt
    [running] = await runs_of(svc, made.id)
    assert running.outcome is None and running.run_id == run_id
    await answer(svc, run_id, "All green: 14 checks passed.")
    [ran] = await runs_of(svc, made.id)
    assert (ran.outcome, ran.reason, ran.tokens) == ("ran", "All green: 14 checks passed.", 26)
    task = await svc.get_task("acme-shop-1")
    [report] = await repo.list_reports(svc._r, task.id, 10)
    assert report.body_md.splitlines()[0] == "inspected · S · All green: 14 checks passed."
    assert "All green: 14 checks passed." in report.body_md.splitlines()[-1]
    assert ran.report_id == report.id
    assert task.size is None  # a check's S does not size the card
    ended = await svc.get_run(run_id)
    assert ended.status == "ended" and "check of schedule 1 ran" in (ended.status_reason or "")
    [line] = [e for e in await kinds(svc, "run.ended") if e.payload["run_id"] == run_id]
    assert line.payload["run_kind"] == "check" and line.payload["project"] == "acme-shop"


async def test_a_check_at_its_step_limit_says_so(svc: Service, clock: Clock) -> None:
    made = await check(svc, task="", project="acme-shop")
    await svc.schedule_pass(clock.move(minutes=10))
    [(run_id, _, _)] = stub(svc).started
    await answer(svc, run_id, "Looked at", subtype="error_max_turns")
    [ran] = await runs_of(svc, made.id)
    assert (ran.outcome, ran.reason) == ("stopped", "check stopped at its step limit")
    # No card: the answer is a line in the Inbox and the turn's result.
    assert ran.notice == "inspected · S · check stopped at its step limit"
    assert [n.id for n in (await svc.inbox([])).schedules] == [ran.id]
    turn = (await repo.list_turns(svc._r, run_id))[0]
    assert (turn.result_md or "").startswith("inspected · S · check stopped at its step limit")


async def test_an_every_check_stops_at_done(svc: Service, clock: Clock) -> None:
    made = await check(svc, repeat="every", every_minutes="5", max_times="10")
    assert made.next_at == srules.iso(START + timedelta(minutes=5))
    await svc.schedule_pass(clock.move(minutes=5))
    [(run_id, _, spec)] = stub(svc).started
    assert '"done"' in spec.prompt
    await answer(svc, run_id, "Still running: 3 of 14.")
    assert (await svc.get_schedule(made.id)).next_at == srules.iso(clock.now + timedelta(minutes=5))
    await svc.schedule_pass(clock.move(minutes=5))
    run_id = stub(svc).started[-1][0]
    await answer(svc, run_id, "Done: all 14 green.")
    found = await svc.get_schedule(made.id)
    assert (found.next_at, found.times_run) == (None, 2) and found.done_at is not None
    assert [r.outcome for r in await runs_of(svc, made.id)] == ["ran", "ran"]


async def test_an_every_check_stops_after_max_times(svc: Service, clock: Clock) -> None:
    made = await check(svc, repeat="every", every_minutes="5", max_times="2")
    for _ in range(3):
        await svc.schedule_pass(clock.move(minutes=5))
        if stub(svc).active:
            await answer(svc, stub(svc).started[-1][0], "Still running.")
    found = await svc.get_schedule(made.id)
    assert (found.times_run, found.next_at, len(stub(svc).started)) == (2, None, 2)


async def test_one_run_of_a_schedule_at_a_time(svc: Service, clock: Clock) -> None:
    made = await check(svc, repeat="every", every_minutes="5", max_times="5")
    await svc.schedule_pass(clock.move(minutes=5))
    await svc.schedule_pass(clock.move(minutes=5))  # the first check still runs
    assert [r.outcome for r in await runs_of(svc, made.id)] == [None, "skipped"]
    with pytest.raises(ConflictError, match="one at a time"):
        await svc.run_schedule_now(made.id)
    assert len(stub(svc).started) == 1


async def test_a_check_waits_for_a_slot(svc: Service, clock: Clock) -> None:
    await svc.set_run_limits(1, 0)
    card = await svc.start_run("acme-shop-1")  # takes the one slot
    made = await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    [check_run] = [r for r in await repo.open_runs(svc._r) if r.kind == "check"]
    assert check_run.status == "queued" and "slot" in (check_run.status_reason or "")
    assert [r.kind for r in await svc.queued_runs()] == ["check"]
    await stub(svc).finish(card.id)
    assert (await svc.get_run(check_run.id)).status == "running"
    await answer(svc, check_run.id, "Fine.")
    assert [r.outcome for r in await runs_of(svc, made.id)] == ["ran"]


async def test_a_project_brake_holds_a_check(svc: Service, clock: Clock) -> None:
    run = await started(svc)
    await svc.set_brake(
        "project",
        project="acme-shop",
        reason="Hold on",
        session=run.session_name,
        hook_session=CC_A,
    )
    await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    [held] = [r for r in await repo.open_runs(svc._r) if r.kind == "check"]
    assert held.status == "queued" and (held.status_reason or "").startswith("braked")
    assert [r for r, _, _ in stub(svc).started if r != run.id] == []


async def test_the_owners_stop_ends_a_queued_check(svc: Service, clock: Clock) -> None:
    await svc.set_run_limits(1, 0)
    await svc.start_run("acme-shop-1")
    made = await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    await svc.stop_schedule(made.id)
    [stopped] = await runs_of(svc, made.id)
    assert (stopped.outcome, stopped.reason) == ("stopped", "stopped by owner")
    assert not (await svc.get_schedule(made.id)).running


async def test_a_check_lost_with_the_server_is_closed_on_the_next_pass(
    tmp_path: Path, svc: Service, clock: Clock
) -> None:
    made = await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    [(run_id, _, _)] = stub(svc).started
    again = await make(tmp_path, clock)  # the restart: the running turn is lost
    try:
        assert (await again.get_run(run_id)).status == "lost"
        await again.schedule_pass()
        [lost] = await runs_of(again, made.id)
        assert lost.outcome == "failed" and "server stopped" in (lost.reason or "")
        assert (await again.get_run(run_id)).status == "ended"
    finally:
        await again.close()


async def test_a_card_stop_budget_and_its_brake_hold_an_every_check(
    svc: Service, clock: Clock
) -> None:
    await svc.set_budget("task", task="acme-shop-1", max_tokens=10, action="stop")
    await check(svc, repeat="every", every_minutes="5", max_times="5")
    await svc.schedule_pass(clock.move(minutes=5))
    await answer(svc, stub(svc).started[-1][0], "Still running.")  # 26 tokens, over 10
    await svc.limits_pass()
    [brake] = await repo.open_brakes(svc._r)
    assert (brake.scope, brake.task_key) == ("task", "acme-shop-1")
    await svc.schedule_pass(clock.move(minutes=5))
    [held] = [r for r in await repo.open_runs(svc._r) if r.kind == "check"]
    assert held.status == "queued" and (held.status_reason or "").startswith("braked")
    assert len(stub(svc).started) == 1


async def test_a_deleted_schedule_keeps_its_cards_check_tokens(svc: Service, clock: Clock) -> None:
    made = await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    await answer(svc, stub(svc).started[-1][0], "Fine.")
    await svc.delete_schedule(made.id)
    task = await svc.get_task("acme-shop-1")
    assert await repo.check_tokens(svc._r, task_id=task.id) == 26
    assert await svc.schedules() == [] and await svc.schedule_runs() == []


async def test_a_check_that_cannot_start_fails_with_the_real_error(
    svc: Service, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: Any, **__: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr("relaybay.service.run_files", broken)
    made = await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    [failed] = await runs_of(svc, made.id)
    assert failed.outcome == "failed" and "disk full" in (failed.reason or "")
    assert stub(svc).started == []


async def test_a_coordinator_turns_text_is_one_wake_line(svc: Service) -> None:
    with pytest.raises(ValidationError, match="at most 200 characters"):
        await svc.create_schedule(
            {"kind": "coordinator", "repeat": "daily", "time_of_day": "09:00", "prompt": "x" * 201}
        )


async def per_day(svc: Service, count: int) -> None:
    coordinator = await svc.get_profile("coordinator")
    values = {
        "description": coordinator.description,
        "permission_mode": coordinator.permission_mode,
        "allowed_tools": coordinator.allowed_tools,
        "disallowed_tools": coordinator.disallowed_tools,
        "rights": {**coordinator.rights, prules.PER_DAY: str(count)},
        "kinds": coordinator.kinds,
    }
    await svc.update_profile("coordinator", values)


async def test_the_coordinators_open_schedules_are_capped(svc: Service) -> None:
    await started(svc)
    for i in range(10):
        await svc.schedule_add_mcp("reminder", f"Look {i}", in_minutes=30, hook_session=CC_A)
    with pytest.raises(ValidationError, match="at most 10"):
        await svc.schedule_add_mcp("reminder", "Look 11", in_minutes=30, hook_session=CC_A)
    with pytest.raises(ValidationError, match="within a week"):
        await svc.schedule_add_mcp(
            "reminder", "Later", at=local(START + timedelta(days=8)), hook_session=CC_A
        )


async def test_each_run_of_a_coordinators_check_counts_in_its_daily_cap(
    svc: Service, clock: Clock
) -> None:
    run = await started(svc)
    await per_day(svc, 2)
    made = await svc.schedule_add_mcp(
        "check", "CI?", task="acme-shop-1", every_minutes=5, max_times=5, hook_session=CC_A
    )
    for _ in range(2):
        await svc.schedule_pass(clock.move(minutes=5))
        await answer(svc, stub(svc).started[-1][0], "Still running.")
    await svc.schedule_pass(clock.move(minutes=5))  # the third run is over the cap
    outcomes = [(r.outcome, r.reason or "") for r in await runs_of(svc, made.id)]
    assert [o for o, _ in outcomes] == ["ran", "ran", "failed"]
    assert "ran 2 check(s) of its schedules today" in outcomes[-1][1]
    assert "allows 2 a day" in outcomes[-1][1]
    with pytest.raises(ValidationError, match="allows 2 a day"):
        await svc.schedule_add_mcp(
            "check", "CI?", task="acme-shop-1", in_minutes=30, hook_session=CC_A
        )
    with pytest.raises(ValidationError, match="allows 2 a day"):
        await svc.start_run_mcp("acme-shop-1", session=run.session_name, hook_session=CC_A)


# --- the profile's allow list holds in PreToolUse ---------------------------------------------


async def pre_tool(svc: Service, cc: str, cwd: str, tool: str, **tool_input: Any) -> Any:
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": cc,
        "cwd": cwd,
        "tool_name": tool,
        "tool_input": tool_input,
    }
    await svc.record_hook(payload)
    return await svc.hook_answer(payload)


def denied(answer: Any) -> bool:
    out = (answer or {}).get("hookSpecificOutput") or {}
    return out.get("permissionDecision") == "deny"


async def test_a_checks_calls_outside_its_allow_list_are_denied(svc: Service, clock: Clock) -> None:
    await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    [(run_id, turn_id, spec)] = stub(svc).started
    for rule in prules.CHECK_DISALLOWED:
        assert rule in spec.disallowed
    cc = str(uuid.uuid4())
    await svc.turn_init(turn_id, cc)
    start = {"hook_event_name": "SessionStart", "session_id": cc, "cwd": spec.cwd}
    await svc.record_hook({**start, "relaybay_run": str(run_id)})
    allowed_calls = (
        "git log -1",
        "git diff HEAD~1",
        "git status",
        "git log",
        "git log -w",
        "gh pr view 51",
    )
    for allowed in allowed_calls:
        assert not denied(await pre_tool(svc, cc, spec.cwd, "Bash", command=allowed)), allowed
    assert not denied(await pre_tool(svc, cc, spec.cwd, "Read", file_path="README.md"))
    for tool, args in (
        ("Bash", {"command": "rm -rf x"}),
        ("Bash", {"command": "git log -1; rm -rf x"}),
        ("Bash", {"command": "git log -1 --output=x"}),
        ("Bash", {"command": "git log -1 --outp=x"}),
        ("Bash", {"command": "git diff --output ../e HEAD"}),
        ("Bash", {"command": "git diff --output=../e HEAD"}),
        ("Bash", {"command": "git diff --ext-diff"}),
        ("Bash", {"command": "git -c core.pager=x log"}),
        ("Bash", {"command": "git log -1 ${IFS}"}),
        ("Bash", {"command": "gh pr checks 51 --watch"}),
        ("Bash", {"command": "gh pr view --web"}),
        ("Bash", {"command": "gh pr view 51 -w"}),
        ("PowerShell", {"command": "Get-ChildItem"}),
        ("MultiEdit", {"file_path": "x.txt"}),
        ("Write", {"file_path": "x.txt", "content": "x"}),
        ("mcp__relaybay__report_add", {}),
    ):
        answer = await pre_tool(svc, cc, spec.cwd, tool, **args)
        assert denied(answer), tool
        reason = answer["hookSpecificOutput"]["permissionDecisionReason"]
        assert "outside the allow list of profile read-only" in reason


async def test_the_coordinators_writes_stay_in_its_folder(svc: Service) -> None:
    run = await started(svc)
    folder = Path(run.cwd)
    start = {"hook_event_name": "SessionStart", "session_id": CC_A, "cwd": run.cwd}
    await svc.coordinator_say("Note it down.")  # a turn runs: its SessionStart links the row
    await svc.record_hook({**start, "relaybay_run": str(run.id)})
    inside = str(folder / "notes.md")
    outside = str(folder.parent / "elsewhere.md")
    assert not denied(await pre_tool(svc, CC_A, run.cwd, "Write", file_path=inside, content="x"))
    assert not denied(await pre_tool(svc, CC_A, run.cwd, "Edit", file_path="notes.md"))
    assert denied(await pre_tool(svc, CC_A, run.cwd, "Write", file_path=outside, content="x"))
    sneaky = str(folder / ".." / "elsewhere.md")
    assert denied(await pre_tool(svc, CC_A, run.cwd, "Write", file_path=sneaky, content="x"))
    assert denied(await pre_tool(svc, CC_A, run.cwd, "Bash", command="git push"))
    assert not denied(await pre_tool(svc, CC_A, run.cwd, "mcp__relaybay__task_list"))


async def test_check_tokens_count_in_the_project_and_card_budgets(
    svc: Service, clock: Clock
) -> None:
    await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    await answer(svc, stub(svc).started[-1][0], "Fine.")
    task = await svc.get_task("acme-shop-1")
    assert await repo.check_tokens(svc._r, project_id=task.project_id) == 26
    assert await repo.check_tokens(svc._r, task_id=task.id) == 26
    assert await repo.check_tokens(svc._r, task_id=task.id + 1) == 0


# --- the missed rule (K6) ---------------------------------------------------------------------


async def test_a_missed_time_runs_once_after_a_restart_within_the_hour(
    tmp_path: Path, svc: Service, clock: Clock
) -> None:
    soon = await svc.create_schedule(
        {
            "kind": "reminder",
            "repeat": "once",
            "at": local(START + timedelta(minutes=10)),
            "prompt": "Soon",
        }
    )
    daily = await svc.create_schedule(
        {"kind": "reminder", "repeat": "daily", "time_of_day": "11:15", "prompt": "Daily"}
    )
    assert daily.next_at == srules.iso(START + timedelta(minutes=15))
    # The program was closed for 50 minutes: both are under an hour late. The restart is a new
    # service on the same database.
    clock.move(minutes=60)
    again = await make(tmp_path, clock)
    try:
        await again.schedule_pass()
        assert [r.outcome for r in await runs_of(again, soon.id)] == ["ran"]
        assert [r.outcome for r in await runs_of(again, daily.id)] == ["ran"]
        after = await again.get_schedule(daily.id)
        assert after.next_at == srules.iso(START + timedelta(days=1, minutes=15))
        await again.schedule_pass()
        assert len(await runs_of(again, soon.id)) == 1  # once only
    finally:
        await again.close()


async def test_a_time_missed_by_an_hour_or_more_is_logged_as_missed(
    tmp_path: Path, svc: Service, clock: Clock
) -> None:
    made = await check(svc)
    daily = await svc.create_schedule(
        {"kind": "reminder", "repeat": "daily", "time_of_day": "11:15", "prompt": "Daily"}
    )
    clock.move(minutes=10 + 66)  # the check 66 min late, the daily 61
    again = await make(tmp_path, clock)
    try:
        await again.schedule_pass()
        [missed] = await runs_of(again, made.id)
        assert (missed.outcome, missed.started_at, missed.run_id) == ("missed", None, None)
        assert stub(again).started == []
        assert (await again.get_schedule(made.id)).next_at is None
        [late] = await runs_of(again, daily.id)
        assert late.outcome == "missed" and late.notice is None
        assert (await again.get_schedule(daily.id)).next_at == srules.iso(
            START + timedelta(days=1, minutes=15)
        )
        assert len(await kinds(again, "schedule.missed")) == 2
    finally:
        await again.close()


# --- coordinator turns ------------------------------------------------------------------------


async def test_a_coordinator_turn_is_a_line_in_its_next_wake(svc: Service, clock: Clock) -> None:
    run = await started(svc)
    made = await svc.create_schedule(
        {
            "kind": "coordinator",
            "repeat": "daily",
            "time_of_day": "11:05",
            "prompt": "Morning summary",
        }
    )
    await svc.schedule_pass(clock.move(minutes=5))
    [ran] = await runs_of(svc, made.id)
    assert ran.outcome == "ran"
    await svc.coordinator_wake_pass()
    wake = (await repo.list_turns(svc._r, run.id))[-1]
    assert wake.sent_by == "relaybay" and wake.prompt.startswith("relaybay wake: 1 event")
    assert f"- schedule {made.id}: Morning summary (event " in wake.prompt
    page = await svc.coordinator_page()
    assert [s.id for s in page.schedules] == [made.id]


async def test_a_coordinator_turn_is_skipped_without_a_coordinator(
    svc: Service, clock: Clock
) -> None:
    made = await svc.create_schedule(
        {
            "kind": "coordinator",
            "repeat": "once",
            "at": local(START + timedelta(minutes=1)),
            "prompt": "Morning summary",
        }
    )
    await svc.schedule_pass(clock.move(minutes=1))
    [skipped] = await runs_of(svc, made.id)
    assert skipped.outcome == "skipped" and "no coordinator run is open" in (skipped.reason or "")
    assert await kinds(svc, "schedule.fired") == []


# --- MCP: the coordinator's tools -------------------------------------------------------------


async def test_schedule_add_is_the_coordinators_alone(svc: Service, clock: Clock) -> None:
    with pytest.raises(ValidationError, match="coordinator run only"):
        await svc.schedule_add_mcp("reminder", "Hi", in_minutes=5, session="acme-shop-1 dev")
    await started(svc)
    with pytest.raises(ValidationError, match="the owner's"):
        await svc.schedule_add_mcp("coordinator", "Summary", in_minutes=5, hook_session=CC_A)
    with pytest.raises(ValidationError, match="Only a check repeats"):
        await svc.schedule_add_mcp("reminder", "Hi", every_minutes=10, hook_session=CC_A)
    made = await svc.schedule_add_mcp(
        "check",
        "CI green?",
        task="acme-shop-1",
        every_minutes=10,
        max_times=3,
        hook_session=CC_A,
    )
    assert (made.created_by_kind, made.repeat, made.max_times) == ("coordinator", "every", 3)
    once = await svc.schedule_add_mcp("reminder", "Look", in_minutes=30, hook_session=CC_A)
    assert once.next_at == srules.iso(START + timedelta(minutes=30))
    listed = await svc.schedule_list_mcp(hook_session=CC_A)
    assert {s.id for s in listed} == {made.id, once.id}


async def test_schedule_cancel_takes_only_a_coordinators_schedule(
    svc: Service, clock: Clock
) -> None:
    owners = await svc.create_schedule(
        {"kind": "reminder", "repeat": "daily", "time_of_day": "09:00", "prompt": "Mine"}
    )
    await started(svc)
    own = await svc.schedule_add_mcp("reminder", "Look", in_minutes=30, hook_session=CC_A)
    with pytest.raises(ValidationError, match="the owner's"):
        await svc.schedule_cancel_mcp(owners.id, hook_session=CC_A)
    await svc.schedule_cancel_mcp(own.id, hook_session=CC_A)
    assert [s.id for s in await svc.schedules()] == [owners.id]
    [deleted] = await kinds(svc, "schedule.deleted")
    assert deleted.payload["schedule_id"] == own.id


async def test_a_right_off_refuses_schedule_add(svc: Service, clock: Clock) -> None:
    await started(svc)
    coordinator = await svc.get_profile("coordinator")
    values = {
        "description": coordinator.description,
        "permission_mode": coordinator.permission_mode,
        "allowed_tools": coordinator.allowed_tools,
        "disallowed_tools": coordinator.disallowed_tools,
        "rights": {**coordinator.rights, "schedule_add": False},
        "kinds": coordinator.kinds,
    }
    await svc.update_profile("coordinator", values)
    with pytest.raises(ValidationError, match="does not grant schedule_add"):
        await svc.schedule_add_mcp("reminder", "Hi", in_minutes=5, hook_session=CC_A)


# --- the board --------------------------------------------------------------------------------


@pytest.fixture
def board(settings: Any, token: str, tmp_path: Path) -> Any:
    from relaybay.app import create_app

    settings.claude_path = tmp_path / "no-such-claude"
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        yield client


def form(token: str, **fields: Any) -> dict[str, Any]:
    from relaybay.security import csrf_token

    return {"csrf_token": csrf_token(token), **fields}


def test_the_schedules_page_lists_creates_and_is_board_only(board: TestClient, token: str) -> None:
    fields = {"kind": "reminder", "repeat": "daily", "time_of_day": "08:30", "prompt": "Stand-up"}
    page = board.get("/schedules")
    assert page.status_code == 200 and "No schedules yet." in page.text
    assert 'href="/schedules"' in page.text  # in the sidebar
    no_csrf = board.post("/schedules", data=fields, headers={"Origin": BASE})
    assert no_csrf.status_code == 403
    bearer = board.post(
        "/schedules",
        data=form(token, **fields),
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
    made = board.post("/schedules", data=form(token, **fields), headers={"Origin": BASE})
    assert made.status_code == 200 and "Schedule 1 added." in made.text
    assert "daily at 08:30" in made.text and "Stand-up" in made.text
    for action in ("disable", "delete"):
        refused = board.post(f"/schedules/1/{action}", data={}, headers={"Origin": BASE})
        assert refused.status_code == 403
    disabled = board.post("/schedules/1/disable", data=form(token), headers={"Origin": BASE})
    assert "Schedule 1 disabled." in disabled.text
    service: Service = board.app.state.service  # type: ignore[attr-defined]
    found = run_in_app(board, service.get_schedule, 1)
    assert (found.enabled, found.next_at) == (False, None)
    bad = board.post(
        "/schedules", data=form(token, **{**fields, "kind": "check"}), headers={"Origin": BASE}
    )
    assert "A check needs a project or a card" in bad.text
    deleted = board.post("/schedules/1/delete", data=form(token), headers={"Origin": BASE})
    assert "Schedule 1 deleted." in deleted.text
    assert run_in_app(board, service.schedules) == []
    audit = board.get("/audit?changes=1&days=0").text
    assert "schedule.created" in audit and "schedule.deleted" in audit


def test_the_hook_client_adds_the_run_id_to_pre_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from relaybay import hook_client

    monkeypatch.delenv("CLAUDE_CODE_HOST_SESSION_ID", raising=False)
    monkeypatch.setenv("RELAYBAY_RUN_ID", "12")
    body = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash"}).encode()
    assert json.loads(hook_client.with_host_session(body))["relaybay_run"] == "12"
    stop = json.dumps({"hook_event_name": "Stop"}).encode()
    assert hook_client.with_host_session(stop) is stop


async def test_a_check_is_known_by_its_pre_tool_use_alone(svc: Service, clock: Clock) -> None:
    await check(svc)
    await svc.schedule_pass(clock.move(minutes=10))
    [(run_id, turn_id, spec)] = stub(svc).started
    cc = str(uuid.uuid4())
    await svc.turn_init(turn_id, cc)  # no SessionStart came through
    payload = {
        "hook_event_name": "PreToolUse",
        "session_id": cc,
        "cwd": spec.cwd,
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf x"},
        "relaybay_run": str(run_id),
    }
    await svc.record_hook(payload)
    assert denied(await svc.hook_answer(payload))


def link_folder(link: Path, target: Path) -> bool:
    """A directory link (a junction on Windows); False where the system refuses one."""
    try:
        if sys.platform == "win32":
            done = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
            return done.returncode == 0
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        return False


def test_a_folder_under_a_link_allows_writes_inside_once_resolved(tmp_path: Path) -> None:
    real = tmp_path / "real"
    (real / "coordinator").mkdir(parents=True)
    linked = tmp_path / "linked"
    if not link_folder(linked, real):
        pytest.skip("this system makes no directory link here")
    folder = linked / "coordinator"
    windows = sys.platform == "win32"
    rule = crules.edit_rule(str(folder.resolve()), windows=windows)
    for path in (folder / "notes.md", real / "coordinator" / "notes.md"):
        assert prules.call_allowed([rule], "Write", {"file_path": str(path)}, windows=windows)
    outside = linked / "elsewhere.md"
    assert not prules.call_allowed([rule], "Write", {"file_path": str(outside)}, windows=windows)
