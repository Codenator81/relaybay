"""Permission profiles: the migration, the fixed rules, edits read by the next turn, the board."""

import json
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import Settings
from relaybay.db import repo
from relaybay.db.connection import connect
from relaybay.domain import profiles
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.runner.layers import LayerState
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_migrations import NOW
from tests.test_runs_service import StubRuns, stub

FIXED = list(profiles.FIXED_DISALLOWED)
AGENT_VALUES = {
    "description": "Runs on cards.",
    "permission_mode": "acceptEdits",
    "allowed_tools": "mcp__relaybay\nBash(git diff *)",
    "disallowed_tools": "",
    "rights": {"messages": True},
    "kinds": ["card"],
}


@pytest.fixture
async def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    ok = LayerState("command", True, "command hooks from your user settings", "registered")
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: ok)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    service.runs = StubRuns(service, tmp_path)  # type: ignore[assignment]
    folder = tmp_path / "acme-shop"
    folder.mkdir()
    await service.upsert_project("acme-shop", repo_path=str(folder))
    await service.create_task("acme-shop", "Checkout", stage="dev")
    try:
        yield service
    finally:
        await service.close()


async def events(service: Service, kind: str) -> list[Any]:
    return [e for e in await repo.events_after(service._r, 0, None, 1000) if e.kind == kind]


# --- migration ---------------------------------------------------------------------------


async def test_upgrade_keeps_each_projects_pair_as_a_profile(tmp_path: Path) -> None:
    db = tmp_path / "old.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0043")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 42;\nCOMMIT;")
        for pid, slug in ((1, "acme-shop"), (2, "demo-api"), (3, "demo-web")):
            await conn.execute(
                "INSERT INTO project (id, slug, name, task_seq, created_at, updated_at)"
                " VALUES (?, ?, ?, 1, ?, ?)",
                (pid, slug, slug, NOW, NOW),
            )
        await conn.execute(
            "INSERT INTO task (id, project_id, key, title, stage, created_at, updated_at)"
            " VALUES (1, 2, 'demo-api-1', 'Old run', 'dev', ?, ?)",
            (NOW, NOW),
        )
        rows = (
            (1, "auto", "mcp__relaybay"),  # the shipped pair: stays on agent
            (2, "plan", "mcp__relaybay, Bash(git diff *)"),  # its own: agent-demo-api
        )
        for pid, mode, tools in rows:
            await conn.execute(
                "INSERT INTO project_run_defaults (project_id, permission_mode, allowed_tools,"
                " updated_at, updated_by) VALUES (?, ?, ?, ?, 'owner')",
                (pid, mode, tools, NOW),
            )
        await conn.execute(
            "INSERT INTO run (id, task_id, project_id, cwd, model, effort, permission_mode,"
            " allowed_tools, status, hooks_source, mcp_source, started_by, started_by_kind,"
            " created_at, updated_at, ended_at) VALUES (1, 1, 2, '/code/demo-api', 'opus',"
            " 'medium', 'acceptEdits', 'mcp__relaybay', 'ended', 'user', 'user', 'owner',"
            " 'owner', ?, ?, ?)",
            (NOW, NOW, NOW),
        )
        await conn.commit()
    finally:
        await conn.close()

    service = Service(db)
    await service.start()
    try:
        shipped = await service.get_profile("agent")
        assert (shipped.permission_mode, shipped.allowed_tools, shipped.version) == (
            "auto",
            "mcp__relaybay",
            1,
        )
        assert (await service.run_defaults("acme-shop")).profile == "agent"
        assert (await service.run_defaults("demo-web")).profile == "agent"  # no row
        own = await service.run_defaults("demo-api")
        assert (own.profile, own.permission_mode, own.allowed_tools) == (
            "agent-demo-api",
            "plan",
            "mcp__relaybay, Bash(git diff *)",
        )
        made = await service.get_profile("agent-demo-api")
        assert (made.builtin, made.kinds, made.rights) == (False, ["card"], shipped.rights)
        assert made.disallowed_tools == shipped.disallowed_tools
        old = await service.get_run(1)
        assert (old.profile, old.mode_set, old.permission_mode) == (None, True, "acceptEdits")
        names = [p.name for p in await service.profiles()]
        assert names == ["agent", "coordinator", "read-only", "agent-demo-api"]
    finally:
        await service.close()


async def test_the_seeded_rows_are_the_shipped_values(svc: Service) -> None:
    for name, shipped in profiles.BUILTIN.items():
        row = await svc.get_profile(name)
        assert row.builtin and row.version == 1
        assert row.snapshot() == shipped, name
    coordinator = await svc.get_profile("coordinator")
    assert coordinator.per_day == 10 and coordinator.rights["run_start"] == 1
    assert coordinator.permission_mode == "dontAsk"
    assert "Bash(git log *)" in coordinator.allowed_rules
    reader = await svc.get_profile("read-only")
    assert reader.kinds == ["check"] and not any(reader.rights.values())


# --- domain rules ------------------------------------------------------------------------


def test_the_fixed_tools_are_always_taken_away() -> None:
    assert list(profiles.turn_disallowed("")) == FIXED
    assert list(profiles.turn_disallowed(None)) == FIXED
    assert list(profiles.turn_disallowed("Bash, CronCreate")) == ["Bash", "CronCreate", *FIXED[1:]]


def test_rules_are_one_per_line_and_checked() -> None:
    text = "mcp__relaybay\r\nBash(git diff *), Read\n\nRead"
    assert profiles.clean_rules(text, "allowed tools", required=True) == (
        "mcp__relaybay, Bash(git diff *), Read"
    )
    assert profiles.clean_rules("", "taken-away tools", required=False) == ""
    with pytest.raises(ValidationError, match="at least one rule"):
        profiles.clean_rules(" \n", "allowed tools", required=True)
    with pytest.raises(ValidationError, match="is not a tool rule"):
        profiles.clean_rules("rm -rf /", "allowed tools", required=True)
    with pytest.raises(ValidationError, match="at least one run kind"):
        profiles.clean_kinds([])
    with pytest.raises(ValidationError, match="Invalid profile name"):
        profiles.clean_name("Agent Strict")


async def test_bypass_is_never_a_profiles_mode(svc: Service) -> None:
    bypass = {**AGENT_VALUES, "permission_mode": "bypassPermissions"}
    with pytest.raises(ValidationError, match="never a profile's mode"):
        await svc.create_profile("agent-wide", bypass)
    with pytest.raises(ValidationError, match="never a profile's mode"):
        await svc.update_profile("agent", bypass)
    assert (await svc.get_profile("agent")).version == 1


# --- turns read the profile --------------------------------------------------------------


async def test_a_turn_passes_the_fixed_list_even_when_the_profile_lacks_it(
    svc: Service,
) -> None:
    await svc.update_profile("agent", {**AGENT_VALUES, "disallowed_tools": "WebFetch"})
    run = await svc.start_run("acme-shop-1", brief="Build it")
    [(_, _, spec)] = stub(svc).started
    assert list(spec.disallowed) == ["WebFetch", *FIXED]
    assert run.profile == "agent" and not run.mode_set


async def test_an_edit_shows_on_the_next_turn_with_its_version(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1", brief="Build it")
    first = stub(svc).started[-1][2]
    assert (first.mode, first.allowed) == ("auto", ("mcp__relaybay",))
    await stub(svc).finish(run.id)
    edited = await svc.update_profile("agent", AGENT_VALUES)
    assert edited.version == 2
    shown = await svc.get_run(run.id)  # the open run shows what its next turn starts with
    assert (shown.permission_mode, shown.allowed_tools) == (
        "acceptEdits",
        "mcp__relaybay, Bash(git diff *)",
    )
    await svc.send_to_run(run.id, "Go on")
    second = stub(svc).started[-1][2]
    assert (second.mode, second.allowed) == ("acceptEdits", ("mcp__relaybay", "Bash(git diff *)"))
    assert list(second.disallowed) == FIXED
    turns = await repo.list_turns(svc._r, run.id)
    assert [(t.profile, t.profile_version) for t in turns] == [("agent", 1), ("agent", 2)]


async def test_a_mode_set_for_the_run_stays_while_its_tools_follow(
    svc: Service,
) -> None:
    run = await svc.start_run("acme-shop-1", brief="Build it", mode="plan")
    assert run.mode_set and run.permission_mode == "plan"
    await stub(svc).finish(run.id)
    await svc.update_profile("agent", AGENT_VALUES)
    await svc.send_to_run(run.id, "Go on")
    spec = stub(svc).started[-1][2]
    assert (spec.mode, spec.allowed) == ("plan", ("mcp__relaybay", "Bash(git diff *)"))
    await stub(svc).finish(run.id)
    followed = await svc.set_run_settings(run.id, mode="profile")
    assert (followed.mode_set, followed.permission_mode) == (False, "acceptEdits")
    [change] = await events(svc, "run.settings_changed")
    assert change.payload["changed"]["follows_profile"] == {"from": False, "to": True}
    lowered = await svc.set_run_settings(run.id, mode="dontAsk")
    assert lowered.mode_set and lowered.permission_mode == "dontAsk"


async def test_the_start_form_may_pick_another_profile_for_cards(
    svc: Service,
) -> None:
    await svc.create_profile("agent-strict", {**AGENT_VALUES, "permission_mode": "dontAsk"})
    run = await svc.start_run("acme-shop-1", brief="Build it", profile="agent-strict")
    assert (run.profile, run.permission_mode, run.mode_set) == ("agent-strict", "dontAsk", False)
    await stub(svc).finish(run.id)
    await svc.end_run(run.id)
    await svc.create_task("acme-shop", "Search", stage="dev")
    with pytest.raises(ValidationError, match="not for runs on cards"):
        await svc.start_run("acme-shop-2", brief="Build it", profile="coordinator")
    with pytest.raises(ValidationError, match="not for runs on cards"):
        await svc.set_run_defaults("acme-shop", {"profile": "read-only"})


# --- edits, delete, reset, Audit ---------------------------------------------------------


async def test_shipped_profiles_are_not_deleted_and_reset_restores_them(
    svc: Service,
) -> None:
    with pytest.raises(ValidationError, match="ships with relaybay"):
        await svc.delete_profile("agent")
    unchanged = await svc.reset_profile("coordinator")
    assert unchanged.version == 1  # nothing to restore: no new version
    await svc.update_profile("agent", AGENT_VALUES)
    restored = await svc.reset_profile("agent")
    assert restored.snapshot() == profiles.BUILTIN["agent"] and restored.version == 3
    [reset] = await events(svc, "profile.reset")
    assert reset.payload["before"]["permission_mode"] == "acceptEdits"
    assert reset.payload["after"]["permission_mode"] == "auto"
    with pytest.raises(NotFoundError):
        await svc.reset_profile("nope")


async def test_a_profile_in_use_is_not_deleted(svc: Service) -> None:
    await svc.create_profile("agent-strict", AGENT_VALUES)
    with pytest.raises(ConflictError, match="exists already"):
        await svc.create_profile("agent-strict", AGENT_VALUES)
    await svc.set_run_defaults("acme-shop", {"profile": "agent-strict"})
    with pytest.raises(ConflictError, match="profile of acme-shop"):
        await svc.delete_profile("agent-strict")
    with pytest.raises(ConflictError, match="taken off cards"):
        await svc.update_profile("agent-strict", {**AGENT_VALUES, "kinds": ["check"]})
    run = await svc.start_run("acme-shop-1", brief="Build it")
    await svc.set_run_defaults("acme-shop", {"profile": "agent"})
    with pytest.raises(ConflictError, match=f"open run {run.id}"):
        await svc.delete_profile("agent-strict")
    await stub(svc).finish(run.id)
    await svc.end_run(run.id)
    await svc.delete_profile("agent-strict")
    with pytest.raises(NotFoundError):
        await svc.get_profile("agent-strict")
    [gone] = await events(svc, "profile.deleted")
    assert gone.payload["before"]["permission_mode"] == "acceptEdits"


async def test_every_change_is_an_audit_line_with_before_and_after(
    svc: Service,
) -> None:
    await svc.create_profile("agent-strict", AGENT_VALUES)
    same = await svc.update_profile("agent-strict", AGENT_VALUES)
    assert same.version == 1  # nothing changed: no version, no event
    await svc.update_profile("agent-strict", {**AGENT_VALUES, "permission_mode": "plan"})
    audit = await svc.audit(changes=True)
    kinds = [e.kind for e in audit]
    assert kinds.count("profile.updated") == 1 and "profile.created" in kinds
    [updated] = [e for e in audit if e.kind == "profile.updated"]
    assert updated.payload["version"] == 2 and updated.payload["version_before"] == 1
    assert updated.payload["before"]["permission_mode"] == "acceptEdits"
    assert updated.payload["after"]["permission_mode"] == "plan"
    from relaybay.mcp_server.format import event_detail

    assert event_detail(updated) == "agent-strict v2: permission_mode acceptEdits to plan"


# --- the board ---------------------------------------------------------------------------


@pytest.fixture
def board(settings: Settings, token: str, tmp_path: Path) -> Iterator[TestClient]:
    settings.claude_path = tmp_path / "no-such-claude"
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        service: Service = client.app.state.service
        folder = tmp_path / "acme-shop"
        folder.mkdir()
        run_in_app(client, service.upsert_project, "acme-shop", repo_path=str(folder))
        yield client


def form(token: str, **fields: Any) -> dict[str, Any]:
    from relaybay.security import csrf_token

    return {"csrf_token": csrf_token(token), **fields}


def editor(**fields: Any) -> dict[str, Any]:
    return {
        "description": "Strict.",
        "permission_mode": "dontAsk",
        "allowed_tools": "mcp__relaybay\nRead",
        "disallowed_tools": "WebFetch",
        "run_start_per_day": "3",
        "right_run_start": "1",
        "kinds": ["card"],
        **fields,
    }


def test_profiles_page_lists_edits_and_is_board_only(board: TestClient, token: str) -> None:
    page = board.get("/settings/profiles")
    assert page.status_code == 200
    for text in ("agent", "coordinator", "read-only", "CronCreate", "Reset to shipped"):
        assert text in page.text
    assert 'href="/settings/profiles"' in board.get("/settings/agents").text
    assert ">bypassPermissions<" not in page.text
    no_csrf = board.post("/settings/profiles/agent/save", data=editor(), headers={"Origin": BASE})
    assert no_csrf.status_code == 403
    bearer = board.post(
        "/settings/profiles/agent/save",
        data=form(token, **editor()),
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
    saved = board.post(
        "/settings/profiles/agent/save", data=form(token, **editor()), headers={"Origin": BASE}
    )
    assert saved.status_code == 200 and "agent saved (version 2)." in saved.text
    service: Service = board.app.state.service
    agent = run_in_app(board, service.get_profile, "agent")
    assert (agent.permission_mode, agent.allowed_tools, agent.disallowed_tools) == (
        "dontAsk",
        "mcp__relaybay, Read",
        "WebFetch",
    )
    assert agent.rights["run_start"] == 1 and agent.per_day == 3 and not agent.rights["messages"]
    bypass = board.post(
        "/settings/profiles/agent/save",
        data=form(token, **editor(permission_mode="bypassPermissions")),
        headers={"Origin": BASE},
    )
    assert "never a profile&#39;s mode" in bypass.text
    reset = board.post("/settings/profiles/agent/reset", data=form(token), headers={"Origin": BASE})
    assert "agent reset to shipped (version 3)." in reset.text
    assert (
        board.post(
            "/settings/profiles/agent/rename", data=form(token), headers={"Origin": BASE}
        ).status_code
        == 404
    )


def test_new_profile_project_picks_it_and_delete(board: TestClient, token: str) -> None:
    made = board.post(
        "/settings/profiles",
        data=form(token, name="agent-strict", **editor()),
        headers={"Origin": BASE},
    )
    assert made.status_code == 200 and "agent-strict added." in made.text
    page = board.get("/p/acme-shop/environments")
    assert 'name="profile"' in page.text and "agent-strict (dontAsk)" in page.text
    assert 'name="allowed_tools"' not in page.text
    saved = board.post(
        "/p/acme-shop/run-defaults",
        data=form(token, model="opus", effort="medium", profile="agent-strict"),
        headers={"Origin": BASE},
    )
    assert saved.status_code == 200 and "Agent defaults saved." in saved.text
    in_use = board.post(
        "/settings/profiles/agent-strict/delete", data=form(token), headers={"Origin": BASE}
    )
    assert "profile of acme-shop" in in_use.text
    board.post(
        "/p/acme-shop/run-defaults",
        data=form(token, model="opus", effort="medium", profile="agent"),
        headers={"Origin": BASE},
    )
    gone = board.post(
        "/settings/profiles/agent-strict/delete", data=form(token), headers={"Origin": BASE}
    )
    assert "agent-strict deleted." in gone.text
    audit = board.get("/audit?changes=1&days=0")
    assert "profile.created" in audit.text and "profile.deleted" in audit.text
    service: Service = board.app.state.service
    [event] = [
        e for e in run_in_app(board, service.audit, changes=True) if e.kind == "profile.created"
    ]
    assert json.loads(json.dumps(event.payload))["after"]["permission_mode"] == "dontAsk"


def test_the_daily_cap_takes_ascii_digits_only() -> None:
    with pytest.raises(ValidationError, match="whole number"):
        profiles.clean_rights({"run_start_per_day": "\u00b2"})
    assert profiles.clean_rights({"run_start_per_day": "7"})["run_start_per_day"] == 7


async def test_a_batch_shim_refuses_rules_cmd_would_read(
    svc: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stub(svc), "claude", lambda: "C:/tools/claude.cmd")
    bad = {**AGENT_VALUES, "allowed_tools": "mcp__relaybay\nBash(echo %X%)"}
    with pytest.raises(ValidationError, match=r"claude.cmd shim"):
        await svc.create_profile("agent-shell", bad)
    with pytest.raises(ValidationError, match=r"claude.cmd shim"):
        await svc.update_profile("agent", {**AGENT_VALUES, "disallowed_tools": "Bash(a|b)"})
    assert (await svc.get_profile("agent")).version == 1
    await svc.update_profile("agent", AGENT_VALUES)  # nothing cmd.exe reads: saved


async def test_an_open_run_from_before_profiles_keeps_its_own(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1", brief="Build it")
    await stub(svc).finish(run.id)
    async with svc._tx() as tx:
        await tx.conn.execute(
            "UPDATE run SET profile = NULL, mode_set = 1, disallowed_tools = NULL,"
            " allowed_tools = 'Read, Bash(rm *)', permission_mode = 'acceptEdits' WHERE id = ?",
            (run.id,),
        )
    await svc.update_profile("agent", {**AGENT_VALUES, "permission_mode": "plan"})
    await svc.send_to_run(run.id, "Go on")
    spec = stub(svc).started[-1][2]
    assert (spec.mode, spec.allowed) == ("acceptEdits", ("Read", "Bash(rm *)"))
    assert list(spec.disallowed) == FIXED
    turns = await repo.list_turns(svc._r, run.id)
    assert (turns[-1].profile, turns[-1].profile_version) == (None, None)


async def test_a_coordinator_cannot_make_a_lowered_run_follow_again(svc: Service) -> None:
    run = await svc.start_run("acme-shop-1", brief="Build it")
    await stub(svc).finish(run.id)
    low = await svc.set_run_settings(run.id, mode="plan", caller_kind="coordinator")
    assert low.mode_set and low.permission_mode == "plan"
    for mode in ("profile", "Profile", " profile "):
        with pytest.raises(ValidationError):
            await svc.set_run_settings(run.id, mode=mode, caller_kind="coordinator")
    kept = await svc.get_run(run.id)
    assert kept.permission_mode == "plan" and kept.mode_set


async def test_the_recorded_version_is_the_one_spawned(svc: Service) -> None:
    spawn = svc._spawn_turn

    async def edit_first(run_id: int, turn_id: int, reserved: bool) -> None:
        # An edit after the turn was marked started and before its process starts.
        await svc.update_profile("agent", AGENT_VALUES)
        await spawn(run_id, turn_id, reserved)

    svc._spawn_turn = edit_first  # type: ignore[method-assign]
    run = await svc.start_run("acme-shop-1", brief="Build it")
    spec = stub(svc).started[-1][2]
    assert spec.mode == "acceptEdits"
    [turn] = await repo.list_turns(svc._r, run.id)
    assert (turn.profile, turn.profile_version) == ("agent", 2)
