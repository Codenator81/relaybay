"""Runs in the service with a stub runner: no process starts, the test plays the turns."""

import asyncio
import json
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from relaybay.db import repo
from relaybay.domain.errors import ConflictError, ValidationError
from relaybay.runner.auth import LoginState
from relaybay.runner.claude_cli import TurnSpec
from relaybay.runner.layers import LayerState
from relaybay.runner.manager import RunnerConfig
from relaybay.runner.stream import Result
from relaybay.service import Service
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
