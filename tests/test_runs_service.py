"""Runs in the service with a stub runner: no process starts, the test plays the turns."""

import asyncio
import json
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from relaybay.db import repo
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.domain.reports import parse_checks
from relaybay.mcp_server import format as fmt
from relaybay.runner.auth import LoginState
from relaybay.runner.claude_cli import TurnSpec
from relaybay.runner.layers import LayerState
from relaybay.runner.manager import RunnerConfig
from relaybay.runner.stream import Refusal, Result, read_line
from relaybay.service import _NOTES, Service
from tests.e2e.conftest import make_git_repo

SUBSCRIPTION = LoginState(True, "claude.ai", "max", 0, readable=True)
DESKTOP = f"local_{uuid.UUID(int=5)}"
CC_A = "11111111-2222-3333-4444-555555555555"
CC_B = "66666666-7777-8888-9999-000000000000"


class StubRuns:
    """The RunManager surface the service uses; turns end when the test says so."""

    def __init__(self, sink: Service, home: Path) -> None:
        self.sink = sink
        self.config = RunnerConfig(home, "http://127.0.0.1:8765", home / "token")
        self.state = SUBSCRIPTION
        self.started: list[tuple[int, int, TurnSpec]] = []
        self.active: dict[int, int] = {}
        self.checked_since_start: set[int] = set()
        self.fail: Exception | None = None
        self.reserved: set[int] = set()
        self.desktop_calls: list[tuple[str, str, dict[str, str], float]] = []
        self.desktop_answer: int | Exception = 0
        self.desktop_stderr = ""  # what the "CLI" writes into the stderr file of the command
        self.during_launch: Any = None  # a coroutine function run while the launch waits
        self.loop: Any = None
        self.desktop_launcher: Any = self.launch_desktop

    def launch_desktop(self, command: str, cwd: str, env: dict[str, str], timeout: float) -> int:
        self.desktop_calls.append((command, cwd, env, timeout))
        if self.desktop_stderr:
            target = re.search(r'2> "([^"]+)"', command)
            assert target is not None
            Path(target.group(1)).write_text(self.desktop_stderr, encoding="utf-8")
        if self.during_launch is not None:
            asyncio.run_coroutine_threadsafe(self.during_launch(), self.loop).result(10)
        if isinstance(self.desktop_answer, Exception):
            raise self.desktop_answer
        return self.desktop_answer

    def claude(self) -> str:
        return "/usr/local/bin/claude"

    async def login(self, force: bool = False) -> LoginState:
        return self.state

    closing = False

    def is_running(self, run_id: int) -> bool:
        return run_id in self.active or run_id in self.reserved

    def reserve(self, run_id: int) -> bool:
        if self.closing or self.is_running(run_id):
            return False
        self.reserved = self.reserved | {run_id}
        return True

    def release(self, run_id: int) -> None:
        self.reserved = self.reserved - {run_id}

    async def start_turn(
        self, run_id: int, turn_id: int, spec: TurnSpec, *, reserved: bool = False
    ) -> int:
        self.release(run_id)
        if self.fail is not None:
            raise self.fail
        self.started.append((run_id, turn_id, spec))
        self.active[run_id] = turn_id
        return 4242

    async def finish(self, run_id: int, outcome: str = "success", cc_id: str = CC_A) -> None:
        turn_id = self.active.pop(run_id)
        await self.sink.turn_init(turn_id, cc_id)
        result = Result("success", outcome == "error", 1, 10, 1, 2, 3, 4)
        await self.sink.turn_done(turn_id, outcome, result, 0, None)

    async def stop(self, run_id: int, reason: str, grace: float = 10.0) -> bool:
        if run_id not in self.active:
            return False
        turn_id = self.active.pop(run_id)
        await self.sink.turn_done(turn_id, "stopped", None, 1, reason)
        return True

    async def close(self) -> None:
        return None


@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    ok = LayerState("command", True, "command hooks from your user settings", "registered")
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: ok)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, tmp_path)  # type: ignore[assignment]
    stub(service).loop = asyncio.get_running_loop()
    repo = tmp_path / "acme-shop"
    repo.mkdir()
    await service.upsert_project("acme-shop", repo_path=str(repo))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    try:
        yield service
    finally:
        await service.close()


def stub(service: Service) -> StubRuns:
    assert isinstance(service.runs, StubRuns)
    return service.runs


async def test_start_writes_the_run_its_session_binding_and_first_turn(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1", brief="Build the checkout")
    assert (run.status, run.auth_method, run.started_by_kind) == ("running", "claude.ai", "owner")
    assert (run.model, run.effort, run.permission_mode, run.allowed_tools) == (
        "opus",
        "medium",
        "auto",
        "mcp__relaybay",
    )
    task = await svc.get_task("acme-shop-1")
    assert task.session_id == run.session_id and task.session_name == f"acme-shop-1 run {run.id}"
    [(run_id, _, spec)] = stub(svc).started
    assert run_id == run.id and spec.resume is None and spec.cwd == run.cwd
    assert spec.prompt.startswith(f"relaybay run {run.id} on card acme-shop-1 (acme-shop).")
    assert f'pass session "acme-shop-1 run {run.id}"' in spec.prompt
    assert spec.prompt.endswith("Build the checkout")
    await stub(svc).finish(run.id)
    view = await svc.run_view(task)
    assert view is not None
    assert (view.run.status, view.run.claude_session_id) == ("idle", CC_A)
    assert [(t.outcome, t.tokens, t.pid) for t in view.turns] == [("success", 10, 4242)]


async def test_a_text_during_a_turn_waits_and_then_resumes(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    queued = await svc.send_to_run(run.id, "Then the tests")
    assert queued.state == "queued" and len(stub(svc).started) == 1
    await stub(svc).finish(run.id)
    assert len(stub(svc).started) == 2
    second = stub(svc).started[1][2]
    assert (second.prompt, second.resume) == ("Then the tests", CC_A)
    assert (await svc.get_run(run.id)).status == "running"
    await stub(svc).finish(run.id)
    assert (await svc.get_run(run.id)).status == "idle"


async def test_one_open_run_per_card_and_per_repository(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    with pytest.raises(ConflictError, match="already has an open run"):
        await svc.start_run("acme-shop-1")
    await svc.create_task("acme-shop", "Payment", stage="dev")
    with pytest.raises(ConflictError, match=f"in use by run {run.id} on acme-shop-1"):
        await svc.start_run("acme-shop-2")
    await svc.end_run(run.id)
    assert (await svc.start_run("acme-shop-2")).status == "running"


async def test_stop_cancels_waiting_texts_end_is_final(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await svc.send_to_run(run.id, "Later")
    stopped = await svc.stop_run(run.id)
    assert (stopped.status, stopped.status_reason) == ("stopped", "stopped by owner")
    task = await svc.get_task("acme-shop-1")
    view = await svc.run_view(task)
    assert view is not None and [t.outcome for t in view.turns] == ["stopped", "cancelled"]
    session = await repo.get_session_by_name(svc._r, task.session_name or "")
    assert session is not None and session.status == "ended"
    with pytest.raises(ValidationError, match="No turn"):
        await svc.stop_run(run.id)
    await svc.send_to_run(run.id, "Carry on")
    assert stub(svc).started[-1][2].resume is None  # the stopped turn never gave an id
    ended = await svc.end_run(run.id)  # stops the running turn first
    assert (ended.status, ended.ended_by) == ("ended", "owner") and ended.ended_at
    with pytest.raises(ValidationError, match="has ended"):
        await svc.send_to_run(run.id, "More")


async def test_refusals_come_before_anything_is_written(svc: Service) -> None:
    stub(svc).state = LoginState(False, "none", None, 1, readable=True)
    with pytest.raises(ValidationError, match="not signed in"):
        await svc.start_run("acme-shop-1")
    stub(svc).state = LoginState(True, "api_key", None, 0, readable=True)
    with pytest.raises(ValidationError, match="API key"):
        await svc.start_run("acme-shop-1")
    assert await svc.run_view(await svc.get_task("acme-shop-1")) is None
    await svc.set_run_login_any(True)
    assert (await svc.start_run("acme-shop-1")).auth_method == "api_key"


async def test_a_failed_spawn_stops_the_run_visibly(svc: Service) -> None:
    stub(svc).fail = FileNotFoundError(2, "No such file", "claude")
    run = await svc.start_run("acme-shop-1")
    assert run.status == "stopped" and "claude did not start" in (run.status_reason or "")
    view = await svc.run_view(await svc.get_task("acme-shop-1"))
    assert view is not None and view.turns[0].outcome == "failed_to_start"


async def test_project_defaults_never_bypass_permissions(svc: Service) -> None:
    saved = await svc.set_run_defaults(
        "acme-shop",
        {"model": "sonnet", "effort": "high", "allowed_tools": "mcp__relaybay, Bash(git *)"},
    )
    assert (saved.model, saved.effort, saved.allowed_rules) == (
        "sonnet",
        "high",
        ["mcp__relaybay", "Bash(git *)"],
    )
    with pytest.raises(ValidationError, match="permission mode"):
        await svc.set_run_defaults("acme-shop", {"permission_mode": "bypassPermissions"})
    run = await svc.start_run("acme-shop-1", mode="bypassPermissions")  # the owner, for one run
    assert run.permission_mode == "bypassPermissions" and run.model == "sonnet"
    assert stub(svc).started[0][2].allowed == ("mcp__relaybay", "Bash(git *)")


async def test_a_running_run_is_lost_after_a_crash(tmp_path: Path, svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await svc.send_to_run(run.id, "Queued")
    again = Service(svc.db_path)  # a new server on the same data, the old one never stopped
    await again.start()
    try:
        lost = await again.get_run(run.id)
        assert (lost.status, lost.status_reason) == ("lost", "the server stopped while a turn ran")
        view = await again.run_view(await again.get_task("acme-shop-1"))
        assert view is not None and [t.outcome for t in view.turns] == ["lost", "cancelled"]
    finally:
        await again.close()


def start_hook(cc_id: str, cwd: str, run_id: int | None) -> dict[str, Any]:
    payload = {"hook_event_name": "SessionStart", "session_id": cc_id, "cwd": cwd}
    return payload | ({"relaybay_run": str(run_id)} if run_id is not None else {})


async def test_hooks_link_by_the_run_id_and_never_guess(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    # A desktop session bound to another card in the same project: a guess would take it.
    await svc.create_task("acme-shop", "Payment", stage="dev", session=DESKTOP)
    linked = await svc.record_hook(start_hook(CC_A, run.cwd, run.id))
    assert linked is not None and linked.id == run.session_id and linked.cc_exact
    await stub(svc).finish(run.id, cc_id=CC_A)
    # A claude started inside a later turn inherits the run id: its own row.
    await svc.send_to_run(run.id, "Next")
    nested = await svc.record_hook(start_hook(CC_B, run.cwd, run.id))
    assert nested is not None and nested.id != run.session_id and nested.kind == "hook"
    desktop = await repo.get_session_by_external_id(svc._r, DESKTOP)
    assert desktop is not None and desktop.cc_session_id is None
    # The same id again on the resumed turn stays on the run's row.
    again = await svc.record_hook(start_hook(CC_A, run.cwd, run.id))
    assert again is not None and again.id == run.session_id
    # No turn running: a forged run id links nothing.
    await stub(svc).finish(run.id, cc_id=CC_A)
    forged = await svc.record_hook(start_hook("cc-forged", run.cwd, run.id))
    assert forged is not None and forged.id != run.session_id


async def test_stop_after_the_result_cancels_the_waiting_text(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await svc.send_to_run(run.id, "Later")
    turn_id = stub(svc).active.pop(run.id)
    await svc.turn_init(turn_id, CC_A)
    result = Result("success", False, 1, 10, 1, 2, 3, 4)
    await svc.turn_done(turn_id, "success", result, 0, "stopped by owner", True)
    stopped = await svc.get_run(run.id)
    assert (stopped.status, stopped.status_reason) == ("stopped", "stopped by owner")
    view = await svc.run_view(await svc.get_task("acme-shop-1"))
    assert view is not None and [t.outcome for t in view.turns] == ["success", "cancelled"]
    assert len(stub(svc).started) == 1  # the waiting text never started


async def test_a_running_run_without_a_process_can_be_stopped_and_ended(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    stub(svc).active.clear()  # as if its end had not been stored
    lost = await svc.stop_run(run.id)
    assert (lost.status, lost.status_reason) == (
        "lost",
        "no claude process was running for this turn",
    )
    await svc.send_to_run(lost.id, "Again")
    run = lost
    stub(svc).active.clear()
    ended = await svc.end_run(run.id)
    assert ended.status == "ended"
    view = await svc.run_view(await svc.get_task("acme-shop-1"))
    assert view is not None and [t.outcome for t in view.turns] == ["lost", "lost"]


async def test_a_double_click_on_send_is_one_turn(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    first = await svc.send_to_run(run.id, "Then the tests")
    second = await svc.send_to_run(run.id, "Then the tests")
    assert first.id == second.id
    other = await svc.send_to_run(run.id, "And the docs")
    assert other.id != first.id


async def test_runs_refuse_a_claude_home_that_is_not_the_home_folder(
    svc: Service, tmp_path: Path
) -> None:
    stub(svc).config.claude_home = tmp_path / "other-home"
    with pytest.raises(ValidationError, match="runs need claude_home unset"):
        await svc.start_run("acme-shop-1")
    form = await svc.run_form(await svc.get_task("acme-shop-1"))
    assert any("claude_home" in why for why in form.refusals)
    stub(svc).config.claude_home = Path.home()
    assert (await svc.start_run("acme-shop-1")).status == "running"


async def test_the_brief_fits_whatever_the_digest_budget(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaybay.domain.runs import BRIEF_MAX

    asked: list[int] = []

    async def digest(task: Any, reader: Any, since: Any, budget: int) -> tuple[str, bool]:
        asked.append(budget)
        return "x" * budget, True

    monkeypatch.setattr(svc, "_task_context", digest)
    svc.digest_budget_chars = 40000  # the largest digest budget the config allows
    brief = await svc.run_brief(await svc.get_task("acme-shop-1"))
    assert asked == [BRIEF_MAX - 500] and len(brief) <= BRIEF_MAX
    run = await svc.start_run("acme-shop-1", brief=brief)  # the form sends it back as is
    assert run.status == "running"


async def test_a_plan_accepted_on_the_board_never_wakes_a_run_session(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await svc.record_hook(start_hook(CC_A, run.cwd, run.id))
    await svc.set_plan("acme-shop-1", [{"title": "Write it"}])
    await stub(svc).finish(run.id)
    await svc.record_hook({"hook_event_name": "Stop", "session_id": CC_A, "cwd": run.cwd})
    await svc.accept_plan("acme-shop-1", actor="owner")
    assert await svc.wait_for_comments({"session_id": CC_A}, hold=0.3, poll=0.02) is None
    # The next turn's own hooks tell it.
    await svc.send_to_run(run.id, "Go on")
    answer = await svc.hook_answer(
        {"hook_event_name": "UserPromptSubmit", "session_id": CC_A, "cwd": run.cwd}
    )
    assert answer is not None and "accepted the plan of acme-shop-1" in json.dumps(answer)


async def test_a_missing_claude_path_is_named(svc: Service, tmp_path: Path) -> None:
    stub(svc).config.claude_path = tmp_path / "bin" / "claude"
    stub(svc).claude = lambda: None  # type: ignore[method-assign]
    with pytest.raises(ValidationError, match=r"claude_path is .*claude, which is not a file"):
        await svc.start_run("acme-shop-1")


async def test_a_send_again_after_a_failed_turn_is_a_new_turn(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await stub(svc).finish(run.id)
    stub(svc).fail = OSError("spawn failed")
    first = await svc.send_to_run(run.id, "continue")
    assert (await svc.get_run(run.id)).status == "stopped"
    stub(svc).fail = None
    again = await svc.send_to_run(run.id, "continue")
    assert again.id != first.id and (await svc.get_run(run.id)).status == "running"


async def test_a_turn_stopped_before_its_process_exists_never_starts(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await stub(svc).finish(run.id)
    spawn = svc._spawn_turn
    lost: list[str] = []

    async def stop_first(run_id: int, turn_id: int) -> None:
        # Stop arrives between the commit of the started turn and the spawn.
        await svc.stop_run(run_id)
        lost.append((await svc.get_run(run_id)).status)
        await spawn(run_id, turn_id)

    svc._spawn_turn = stop_first  # type: ignore[method-assign]
    started = len(stub(svc).started)
    await svc.send_to_run(run.id, "Then the tests")
    assert lost == ["lost"]
    assert len(stub(svc).started) == started and not stub(svc).is_running(run.id)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@needs_git
async def test_a_repository_inside_an_open_runs_project_folder_is_refused(
    svc: Service, tmp_path: Path
) -> None:
    folder = tmp_path / "acme"
    api = make_git_repo(folder / "api")
    await svc.upsert_project("acme-shop", repos=[{"name": "api", "path": str(api)}])
    await svc.set_run_defaults("acme-shop", {"folder": str(folder)})
    [api_repo] = await repo.list_project_repos(svc._r, (await svc.get_project("acme-shop")).id)
    first = await svc.start_run("acme-shop-1", cwd="folder", repos=[(api_repo.id, True)])
    await svc.create_task("acme-shop", "Payment", stage="dev")
    with pytest.raises(ConflictError, match=f"in use by run {first.id} on acme-shop-1"):
        await svc.start_run("acme-shop-2", cwd=str(api_repo.id), repos=[(api_repo.id, False)])
    # Another run in the same project folder with worktrees of its own: in parallel.
    second = await svc.start_run("acme-shop-2", cwd="folder", repos=[(api_repo.id, True)])
    assert second.status == "running" and second.cwd == str(folder)


@needs_git
async def test_worktrees_under_a_data_folder_inside_the_repository_do_not_conflict(
    svc: Service, tmp_path: Path
) -> None:
    api = make_git_repo(tmp_path / "api")
    stub(svc).config.home = api / ".relaybay-data"
    await svc.upsert_project("acme-shop", repos=[{"name": "api", "path": str(api)}])
    [api_repo] = await repo.list_project_repos(svc._r, (await svc.get_project("acme-shop")).id)
    in_place = await svc.start_run("acme-shop-1", repos=[(api_repo.id, False)])
    assert in_place.cwd == str(api)
    await svc.create_task("acme-shop", "Payment", stage="dev")
    other = await svc.start_run("acme-shop-2", repos=[(api_repo.id, True)])
    assert Path(other.cwd).parent.parent == api / ".relaybay-data" / "worktrees"


async def test_a_cancelled_start_removes_the_worktrees_made_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from relaybay import service as service_module
    from relaybay.runner import worktrees
    from relaybay.runner.worktrees import WorktreeSpec

    release = threading.Event()
    removed: list[list[WorktreeSpec]] = []
    spec = WorktreeSpec(1, "api", "/code/api", str(tmp_path / "wt" / "api"), True, "relaybay/k-1")

    def slow_add(specs: Any, git: Any, key: str, run_id: int, made: list) -> list:
        made.append(spec)  # the worktree exists from here on
        release.wait(30)
        return list(made)

    monkeypatch.setattr(worktrees, "add_all", slow_add)
    undone = threading.Event()

    def remove(specs: list[WorktreeSpec], git: Any) -> None:
        removed.append(specs)
        undone.set()

    monkeypatch.setattr(worktrees, "remove_created", remove)
    task = asyncio.ensure_future(service_module._make_worktrees([spec], "git", "k-1", 1))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await asyncio.to_thread(undone.wait, 30)
    assert removed == [[spec]]


# --- the desktop app and the settings between turns -----------------------------------------


async def idle_run(service: Service) -> Any:
    run = await service.start_run("acme-shop-1")
    await stub(service).finish(run.id)
    return await service.get_run(run.id)


async def run_events(service: Service, kind: str) -> list[Any]:
    return [e for e in await repo.events_after(service._r, 0, None, 1000) if e.kind == kind]


async def test_open_in_desktop_hands_the_session_to_the_owner_and_back(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    run = await idle_run(svc)
    opened = await svc.open_in_desktop(run.id)
    [(command, cwd, env, timeout)] = stub(svc).desktop_calls
    stderr_file = svc._run_folder(run.id) / "desktop-stderr.txt"
    assert command == (
        f'cmd.exe /d /c ""/usr/local/bin/claude" --desktop --resume {CC_A} 2> "{stderr_file}""'
    )
    assert "--add-dir" not in command and not stderr_file.exists()
    assert cwd == run.cwd and timeout == 60.0
    assert "CLAUDECODE" not in env and "RELAYBAY_RUN_ID" not in env
    assert opened.with_owner and opened.with_owner_by == "owner" and opened.desktop_opened_at
    assert opened.desktop_link == f"claude://claude.ai/epitaxy/local_{CC_A}"
    [event] = await run_events(svc, "run.opened_in_desktop")
    assert (event.actor, event.payload["run_id"]) == ("owner", run.id)
    with pytest.raises(ValidationError, match="already with the owner"):
        await svc.open_in_desktop(run.id)
    waiting = await svc.send_to_run(run.id, "After the desktop")
    assert waiting.state == "queued" and len(stub(svc).started) == 1
    assert (await svc.get_run(run.id)).status == "idle"
    task, view = await svc.run_status("acme-shop-1")
    status = fmt.run_status(task, view)
    assert "With the owner in the Claude desktop app since" in status
    assert f"claude://claude.ai/epitaxy/local_{CC_A}" in status
    back = await svc.return_run(run.id)
    assert not back.with_owner and back.status == "running"
    assert back.desktop_link  # the link stays
    spec = stub(svc).started[-1][2]
    assert (spec.prompt, spec.resume) == ("After the desktop", CC_A)
    [returned] = await run_events(svc, "run.returned")
    assert returned.actor == "owner"
    with pytest.raises(ValidationError, match="not with the owner"):
        await svc.return_run(run.id)


async def test_open_in_desktop_is_refused_while_a_turn_runs_or_without_a_session(
    svc: Service,
) -> None:
    run = await svc.start_run("acme-shop-1")
    with pytest.raises(ValidationError, match="is running"):
        await svc.open_in_desktop(run.id)
    await svc.stop_run(run.id)
    with pytest.raises(ValidationError, match="no Claude session yet"):
        await svc.open_in_desktop(run.id)
    assert stub(svc).desktop_calls == []


async def test_without_a_console_the_terminal_command_is_named(svc: Service) -> None:
    run = await idle_run(svc)
    stub(svc).desktop_launcher = None
    with pytest.raises(ValidationError, match=f"claude --resume {CC_A}"):
        await svc.open_in_desktop(run.id)
    assert not (await svc.get_run(run.id)).with_owner


@pytest.mark.parametrize(
    ("answer", "why"),
    [
        (1, "exited with code 1"),
        (subprocess.TimeoutExpired("claude", 60), "did not finish in 60 s"),
        (FileNotFoundError(2, "No such file"), "did not start"),
    ],
)
async def test_a_failed_desktop_launch_keeps_the_run_with_relaybay(
    svc: Service, answer: Any, why: str
) -> None:
    run = await idle_run(svc)
    stub(svc).desktop_answer = answer
    with pytest.raises(ValidationError, match=why) as caught:
        await svc.open_in_desktop(run.id)
    assert "the run stays with relaybay" in str(caught.value)
    after = await svc.get_run(run.id)
    assert not after.with_owner and after.desktop_opened_at is None
    assert await run_events(svc, "run.opened_in_desktop") == []
    await svc.send_to_run(run.id, "Go on")
    assert (await svc.get_run(run.id)).status == "running"


async def test_end_works_while_the_run_is_with_the_owner(svc: Service) -> None:
    run = await idle_run(svc)
    await svc.open_in_desktop(run.id)
    await svc.send_to_run(run.id, "Never runs")
    ended = await svc.end_run(run.id)
    assert ended.status == "ended" and not ended.with_owner
    view = await svc.run_view(await svc.get_task("acme-shop-1"))
    assert view is not None and view.turns[-1].outcome == "cancelled"
    assert len(stub(svc).started) == 1


async def test_settings_apply_from_the_next_turn_and_are_journaled(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    with pytest.raises(ValidationError, match="is running"):
        await svc.set_run_settings(run.id, mode="plan")
    await stub(svc).finish(run.id)
    changed = await svc.set_run_settings(
        run.id, mode="bypassPermissions", model="sonnet", effort="-"
    )
    assert (changed.permission_mode, changed.model, changed.effort) == (
        "bypassPermissions",
        "sonnet",
        "medium",
    )
    [event] = await run_events(svc, "run.settings_changed")
    assert event.actor == "owner"
    assert event.payload["changed"] == {
        "permission_mode": {"from": "auto", "to": "bypassPermissions"},
        "model": {"from": "opus", "to": "sonnet"},
    }
    await svc.set_run_settings(run.id, mode="bypassPermissions")  # no change: no event
    assert len(await run_events(svc, "run.settings_changed")) == 1
    with pytest.raises(ValidationError, match="model must be one of"):
        await svc.set_run_settings(run.id, model="gpt")
    await svc.send_to_run(run.id, "Next")
    spec = stub(svc).started[-1][2]
    assert (spec.mode, spec.model, spec.effort) == ("bypassPermissions", "sonnet", "medium")


async def test_a_coordinator_may_only_lower_the_mode(svc: Service) -> None:
    svc.coordinators = ["lead"]
    run = await idle_run(svc)
    with pytest.raises(ValidationError, match="only for the coordinator"):
        await svc.run_settings_mcp("acme-shop-1", permission_mode="plan", session="worker")
    with pytest.raises(ValidationError, match="alone, picked on the card"):
        await svc.run_settings_mcp(
            "acme-shop-1", permission_mode="bypassPermissions", session="lead"
        )
    lowered = await svc.run_settings_mcp(
        "acme-shop-1", permission_mode="acceptEdits", effort="low", session="lead"
    )
    assert (lowered.permission_mode, lowered.effort) == ("acceptEdits", "low")
    same_rank = await svc.run_settings_mcp("acme-shop-1", permission_mode="dontAsk", session="lead")
    assert same_rank.permission_mode == "dontAsk"
    with pytest.raises(ValidationError, match="raises the permission mode"):
        await svc.run_settings_mcp("acme-shop-1", permission_mode="auto", session="lead")
    [*_, event] = await run_events(svc, "run.settings_changed")
    assert event.actor == "lead"
    assert event.payload["changed"] == {"permission_mode": {"from": "acceptEdits", "to": "dontAsk"}}
    await svc.set_board_brake("project", project="acme-shop")
    with pytest.raises(ValidationError, match="under the brake"):
        await svc.run_settings_mcp("acme-shop-1", model="haiku", session="lead")
    assert (await svc.get_run(run.id)).model == "opus"


async def test_a_coordinator_lowers_the_owners_bypass(svc: Service) -> None:
    svc.coordinators = ["lead"]
    run = await idle_run(svc)
    await svc.set_run_settings(run.id, mode="bypassPermissions")
    lowered = await svc.run_settings_mcp("acme-shop-1", permission_mode="plan", session="lead")
    assert lowered.permission_mode == "plan"
    await svc.end_run(run.id)
    with pytest.raises(NotFoundError, match="no open run"):
        await svc.run_settings_mcp("acme-shop-1", model="haiku", session="lead")


async def test_a_refusal_of_the_cli_is_shown_and_an_opened_session_offers_its_link(
    svc: Service,
) -> None:
    run = await idle_run(svc)
    red, reset = "\x1b[31m", "\x1b[0m"
    stub(
        svc
    ).desktop_stderr = (
        f"{red}Session {CC_A} is open in another terminal.{reset}\n Run /desktop there instead.\n"
    )
    stub(svc).desktop_answer = 1
    with pytest.raises(ValidationError) as first:
        await svc.open_in_desktop(run.id)
    text = str(first.value)
    assert f"exited with code 1: Session {CC_A} is open in another terminal. Run /desktop" in text
    assert "\x1b" not in text and f"claude --resume {CC_A}" in text
    assert not (svc._run_folder(run.id) / "desktop-stderr.txt").exists()
    stub(svc).desktop_stderr, stub(svc).desktop_answer = "", 0
    await svc.open_in_desktop(run.id)
    await svc.return_run(run.id)
    stub(svc).desktop_answer = 1
    with pytest.raises(ValidationError) as second:
        await svc.open_in_desktop(run.id)
    assert f"claude://claude.ai/epitaxy/local_{CC_A}" in str(second.value)
    assert "claude --resume" not in str(second.value)


async def test_back_waits_for_the_launch_and_end_meanwhile_records_no_event(
    svc: Service,
) -> None:
    run = await idle_run(svc)
    refusals: list[str] = []

    async def press_back_then_end() -> None:
        assert run.id in svc.desktop_launching
        with pytest.raises(ValidationError, match="still being opened") as caught:
            await svc.return_run(run.id)
        refusals.append(str(caught.value))
        await svc.end_run(run.id)

    stub(svc).during_launch = press_back_then_end
    after = await svc.open_in_desktop(run.id)
    assert refusals and run.id not in svc.desktop_launching
    assert after.status == "ended" and not after.with_owner and after.desktop_opened_at
    assert await run_events(svc, "run.opened_in_desktop") == []


# --- a turn's report, its lines and the verified marks --------------------------------------

MARKS_WARNING = "Warning (rules): an M or L report has no verified marks"


def result(text: str, refusals: list[Refusal] | None = None) -> Result:
    refusals = refusals or []
    return Result(
        "success",
        False,
        1,
        10,
        1,
        2,
        3,
        4,
        len(refusals),
        [r.tool for r in refusals],
        text,
        refusals,
    )


async def finish(svc: Service, run_id: int, res: Result, lines: list[dict[str, Any]] = ()) -> int:
    turn_id = stub(svc).active.pop(run_id)
    await svc.turn_init(turn_id, CC_A)
    events = [read_line(json.dumps(line), n)[0] for n, line in enumerate(lines, start=1)]
    if events:
        await svc.turn_events(turn_id, events)
    await svc.turn_done(turn_id, "success", res, 0, None)
    return turn_id


async def report_events(svc: Service) -> list[dict[str, Any]]:
    async with svc._r.execute(
        "SELECT payload FROM event WHERE kind = 'report.added' ORDER BY id"
    ) as cur:
        return [json.loads(r[0]) for r in await cur.fetchall()]


async def test_the_turns_result_lands_in_full_with_its_lines_nested(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    text = "verified · M · checkout fixed\n\n" + "\n".join(f"line {n}" for n in range(300))
    text += "\n- tests pass (verified: pytest on Windows)"
    lines = [
        {"type": "system", "subtype": "init", "session_id": CC_A, "model": "demo"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_A", "name": "Agent", "input": {"x": 1}}
                ]
            },
        },
        {
            "type": "assistant",
            "parent_tool_use_id": "toolu_A",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_B", "name": "Grep", "input": {"q": "x"}}
                ]
            },
        },
        {
            "type": "user",
            "parent_tool_use_id": "toolu_A",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "toolu_B", "content": "hit"}]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "toolu_A", "content": "done"}]
            },
        },
        {"type": "result", "subtype": "success", "result": text},
    ]
    turn_id = await finish(svc, run.id, result(text), lines)
    task = await svc.get_task("acme-shop-1")
    detail = await svc.task_detail(task.key)
    [report] = detail.reports
    assert report.body_md == text  # in full, the agent's own first line kept
    assert (report.state, report.size, report.summary) == ("verified", "M", "checkout fixed")
    assert report.author == f"acme-shop-1 run {run.id}"
    assert [(c.line, c.where) for c in detail.checks[report.id]] == [(303, "pytest on Windows")]
    assert detail.marks_warning(report) is None
    _, turn, shown, checks, items = await svc.run_turn_detail(run.id, 1)
    assert turn.id == turn_id and turn.report_id == report.id and turn.events == 6
    assert shown is not None and shown.id == report.id and len(checks) == 1
    [init, agent, final] = items
    assert (init.kind, agent.kind, agent.name, agent.result) == ("init", "tool", "Agent", "done")
    [grep] = agent.children
    assert (grep.name, grep.result, grep.parent) == ("Grep", "hit", "toolu_A")
    assert final.kind == "result"
    view = await svc.run_view(task)
    assert view is not None and view.last_report == "verified · M · checkout fixed"
    assert view.last_counts == {
        "all": 6,
        "assistant": 2,
        "result": 1,
        "subagent": 2,
        "system": 1,
        "user": 2,
    }
    status = fmt.run_status(task, view)
    assert "Last turn's report: verified · M · checkout fixed" in status
    assert "Lines of the latest turn: 6 (" in status and "2 from subagents" in status


async def test_refusals_show_in_the_report_and_a_neutral_first_line_without_one(
    svc: Service,
) -> None:
    run = await svc.start_run("acme-shop-1")
    refusals = [Refusal("Bash", "git push origin `main`"), Refusal("Write", "x")]
    await finish(svc, run.id, result("Pushed nothing.", refusals))
    [report] = (await svc.task_detail("acme-shop-1")).reports
    first, rest = report.body_md.split("\n\n", 1)
    assert first == f"Run {run.id}, turn 1: success" and report.state is None
    assert rest.startswith("Pushed nothing.")
    assert "**Refusals** (2, permission denials of the turn):" in rest
    assert "- Bash: `` git push origin 'main' ``" in rest
    await svc.send_to_run(run.id, "Again")
    await finish(svc, run.id, result(""))
    latest = (await svc.task_detail("acme-shop-1")).reports[0]
    assert (
        latest.body_md == f"Run {run.id}, turn 2: success\n\n(the turn ended without a final text)"
    )


async def test_marks_warning_for_m_and_l_never_for_s(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await finish(svc, run.id, result("changed · M · half done\n\n- the form works"))
    await svc.send_to_run(run.id, "Again")
    await finish(svc, run.id, result("changed · L · done\n\n- form works `verified: browser`"))
    await svc.send_to_run(run.id, "Again")
    await finish(svc, run.id, result("changed · S · typo\n\n- fixed"))
    payloads = await report_events(svc)
    assert [any(MARKS_WARNING in w for w in p.get("warnings", [])) for p in payloads] == [
        True,
        False,
        False,
    ]
    detail = await svc.task_detail("acme-shop-1")
    s_report, l_report, m_report = detail.reports
    assert detail.marks_warning(m_report).startswith(MARKS_WARNING)
    assert detail.marks_warning(l_report) is None and detail.marks_warning(s_report) is None
    assert [c.where for c in detail.checks[l_report.id]] == ["browser"]


async def test_report_add_parses_marks_and_warns_over_mcp(svc: Service) -> None:
    notes: list[str] = []
    token = _NOTES.set(notes)
    try:
        await svc.add_report("acme-shop-1", "verified · M · login\n\n- works")
        assert any(n.startswith(MARKS_WARNING) for n in notes)
        notes.clear()
        body = "verified · M · login\n\n- works (verified: pytest on Windows)\n- page ok"
        body += " verified: browser at /p/acme-shop; and more"
        _, report = await svc.add_report("acme-shop-1", body)
        assert not [n for n in notes if MARKS_WARNING in n]
    finally:
        _NOTES.reset(token)
    checks = (await repo.report_checks(svc._r, [report.id]))[report.id]
    assert [(c.line, c.where) for c in checks] == [
        (3, "pytest on Windows"),
        (4, "browser at /p/acme-shop"),
    ]
    assert checks[0].claim == "works"


def test_parse_checks_takes_the_where_up_to_a_bracket_or_the_line_end() -> None:
    body = "x · M · y\n- a (verified: pytest)\n- b [verified: CI on 3 OSes]\n- c verified:"
    body += "\nverified: d."
    assert [(c.line, c.where) for c in parse_checks(body)] == [
        (2, "pytest"),
        (3, "CI on 3 OSes"),
        (5, "d"),
    ]


async def test_run_events_is_short_masked_and_bounded(svc: Service) -> None:
    secret = "ghp_" + "Q1w2E3r4T5y6" * 3
    run = await svc.start_run("acme-shop-1")
    lines = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": f"step {n} {secret} " + "z" * 500}]},
        }
        for n in range(150)
    ]
    await finish(svc, run.id, result("done"), lines)
    found, turn, items = await svc.run_events(run.id, None, limit=40)
    assert [i.seq for i in items] == list(range(111, 151))
    assert all(len(i.text) <= 160 for i in items)
    text = fmt.run_events(found, turn, items)
    assert secret not in text and "ghp_***" in text
    assert text.startswith(f"Run {run.id} turn 1 (success): 150 lines stored, 40 item(s)")
    _, _, first = await svc.run_events(run.id, 1, limit=200, from_seq=1)
    assert len(first) == 150
    assert len(fmt.run_events(found, turn, first)) <= fmt.RUN_EVENTS_OUTPUT_MAX + 200
    async with svc._r.execute("SELECT data FROM run_event") as cur:
        blobs = [r[0] for r in await cur.fetchall()]
    assert all(secret.encode() not in b for b in blobs)


async def test_a_turn_the_agent_reported_itself_links_that_report(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    session = f"acme-shop-1 run {run.id}"
    _, own = await svc.add_report("acme-shop-1", "verified · S · done", session=session)
    await finish(svc, run.id, result("verified · S · done\n\nthe same, said again"))
    [turn] = await repo.list_turns(svc._r, run.id)
    assert turn.report_id == own.id
    assert len((await svc.task_detail("acme-shop-1")).reports) == 1
    await svc.send_to_run(run.id, "Again")
    await finish(svc, run.id, result("Something else entirely."))
    detail = await svc.task_detail("acme-shop-1")
    assert len(detail.reports) == 2 and detail.reports[0].author == session


async def test_a_turn_report_never_gets_the_first_line_warning(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    await finish(svc, run.id, result("Just text, no state line."))
    [payload] = await report_events(svc)
    assert "warnings" not in payload


async def test_strict_refuses_a_bad_first_line_before_the_session_is_written(
    svc: Service,
) -> None:
    svc.rules_mode = "strict"
    with pytest.raises(ValidationError, match="Rules:"):
        await svc.add_report("acme-shop-1", "no state line here", session="brand-new-session")
    assert await repo.find_sessions(svc._r, "brand-new-session") == []


async def test_reports_before_the_marks_get_their_marks_and_never_the_warning(
    tmp_path, svc: Service
) -> None:
    _, old = await svc.add_report("acme-shop-1", "changed · M · old\n\n- ok (verified: CI)")
    _, bare = await svc.add_report("acme-shop-1", "changed · M · old, unmarked")
    async with svc._tx() as tx:
        await tx.conn.execute("DELETE FROM report_check")
        await tx.conn.execute("DELETE FROM app_setting WHERE key = 'report_marks_from'")
    await svc._backfill_report_marks()
    assert svc.report_marks_from == bare.id + 1
    _, new = await svc.add_report("acme-shop-1", "changed · M · new, unmarked")
    detail = await svc.task_detail("acme-shop-1")
    assert [c.where for c in detail.checks[old.id]] == ["CI"]
    by_id = {r.id: r for r in detail.reports}
    assert detail.marks_warning(by_id[bare.id]) is None
    assert detail.marks_warning(by_id[new.id]).startswith(MARKS_WARNING)
    await svc._backfill_report_marks()  # once: a second start reads nothing again
    assert len((await repo.report_checks(svc._r, [old.id]))[old.id]) == 1


def test_negated_marks_urls_flags_and_code_are_no_marks() -> None:
    body = "\n".join(
        [
            "Not verified: the browser on macOS",
            "- not verified: Windows (no machine)",
            "Unverified: macOS",
            "status: verified: true",
            "See https://x/verified:abc",
            "```",
            "assert x  # verified: by hand",
            "```",
            "- tests pass (verified: pytest on Windows (CI)); docs updated",
        ]
    )
    [mark] = parse_checks(body)
    assert (mark.line, mark.where, mark.claim) == (
        9,
        "pytest on Windows (CI)",
        "tests pass ; docs updated",
    )


async def test_the_board_view_of_a_long_turn_shows_its_last_lines(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1")
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "w" * 3000}]}}
        for _ in range(400)
    ]
    await finish(svc, run.id, result("done"), lines)
    _, turn, _, _, items = await svc.run_turn_detail(run.id, 1)
    assert turn.events == 400 and len(items) == 300 and items[0].seq == 101
    assert sum(len(i.text) for i in items) <= 200_000 + 300 * 160


def test_marks_in_markdown_emphasis_count_and_soft_negations_do_not() -> None:
    from relaybay.domain.reports import parse_checks

    found = [c.where for c in parse_checks("**Verified:** pytest on Windows\n- **verified: CI**")]
    assert found == ["pytest on Windows", "CI"]
    body = "not yet verified: macOS\nnever verified: Linux\nverified: n/a"
    assert parse_checks(body) == []
