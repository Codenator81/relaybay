"""Whole scenarios: a real server, real hooks through the real forwarder, MCP through the SDK
client, and only the model replaced by a script."""

import json
from collections.abc import Callable
from typing import Any

import pytest

from tests.e2e.conftest import Stand, poll
from tests.e2e.stream_contract import check_stream

ME = {"session": "dev-1", "hook_session": "${session_id}"}
KEY = {"key": r"\*\*(acme-shop-\d+)\*\*"}
PROJECT = {"mcp": "project_upsert", "arguments": {"slug": "acme-shop", "repo_path": "${cwd}"}}
NEW_TASK = {
    "mcp": "task_create",
    "arguments": {"project": "acme-shop", "title": "Checkout", "stage": "dev", **ME},
    "capture": KEY,
}


def test_task_session_plan_report_review_inbox(stand: Stand) -> None:
    scenario = {
        "turn": [
            {"text": "Starting."},
            PROJECT,
            NEW_TASK,
            {
                "mcp": "plan_set",
                "arguments": {
                    "task": "${key}",
                    "steps": [{"title": "Write the code"}, {"title": "Run the tests"}],
                    **ME,
                },
            },
            {"tool": "Bash", "input": {"command": "npm test"}, "result": "12 passed"},
            {
                "mcp": "report_add",
                "arguments": {
                    "task": "${key}",
                    "body": "verified · S · checkout works\n\nAll tests pass.",
                    "progress": "Code and tests",
                    **ME,
                },
            },
            {
                "mcp": "review_steps_set",
                "arguments": {
                    "task": "${key}",
                    "steps": [{"title": "Open checkout", "details": "The total is right"}],
                    **ME,
                },
            },
            {
                "mcp": "task_set_stage",
                "arguments": {
                    "task": "${key}",
                    "stage": "review",
                    "next_step": "The owner checks the page",
                    "waiting_on": "owner",
                    **ME,
                },
            },
            {"text": "Ready for review."},
        ]
    }
    run = stand.run(scenario).finish()
    check_stream(run.messages)
    assert run.result["subtype"] == "success" and not run.result["permission_denials"]
    servers = {s["name"]: s["status"] for s in run.init["mcp_servers"]}
    assert servers == {"relaybay": "connected"}
    assert "mcp__relaybay__task_create" in run.init["tools"]

    session_id = run.init["session_id"]
    contexts = [e["text"] for e in run.trace("context") if e["event"] == "SessionStart"]
    assert f"Your relaybay hook session: {session_id}" in contexts[0]

    key = "acme-shop-1"
    task = stand.mcp("task_get", task=key)
    assert "review" in task and "checkout works" in task
    with stand.board() as board:
        inbox = board.get("/inbox").text
        assert key in inbox and "Checkout" in inbox
        page = board.get(f"/t/{key}").text
        assert "checkout works" in page and "Write the code" in page and "Open checkout" in page
        # The transcript the hooks pointed at is read in the background, never on the hook path.
        poll(lambda: "demo-model-large" in board.get("/usage").text, what="the usage reader")


def test_comment_is_delivered_blocks_the_stop_and_is_acknowledged(stand: Stand) -> None:
    go = stand.root / "go"
    scenario = {
        "turn": [
            PROJECT,
            NEW_TASK,
            {"wait_file": go.as_posix()},
            {"tool": "Bash", "input": {"command": "npm test"}, "result": "12 passed"},
            {"text": "All done."},
        ],
        "on_stop_block": [
            {"text": "The owner wants a green button."},
            {"mcp": "comment_ack", "arguments": {"task": "${key}", **ME}},
            {"text": "Green it is."},
        ],
    }
    run = stand.run(scenario)
    poll(
        lambda: run.tool_result_text("mcp__relaybay__task_create"),
        what="the task created by the session",
    )
    with stand.board() as board:
        stand.post_form(board, "/p/acme-shop/t/acme-shop-1/comments", body="Make the button green")
        go.touch()
        run.finish()
        thread = board.get("/p/acme-shop/t/acme-shop-1/comments").text
    assert "Make the button green" in thread
    assert run.result["subtype"] == "success" and run.result["result"] == "Green it is."
    # The Stop block repeats a comment that is not acknowledged, delivered before or not.
    blocks = run.trace("stop_block")
    assert len(blocks) == 1 and "Make the button green" in blocks[0]["reason"]
    events = stand.events()
    assert "comment.delivered" in events and "comment.read" in events


def _live_project(stand: Stand) -> None:
    stand.mcp("project_upsert", slug="acme-shop", repo_path=stand.project_dir.as_posix())
    change = {
        "environment": "live",
        "kind": "live",
        "access": [{"kind": "ssh", "fields": {"alias": "prod-alias"}}],
    }
    stand.mcp("environment_propose", project="acme-shop", change=change, reason="The shop")
    with stand.board() as board:
        stand.post_form(board, "/p/acme-shop/proposals/1/accept")


def test_guard_denies_on_live_and_journals(stand_factory: Callable[..., Stand]) -> None:
    stand = stand_factory(guard_mode="block")
    _live_project(stand)
    # `tries`: the guard's one short try fails open by design; on a slow runner it may miss.
    command = "ssh prod-alias 'systemctl restart app'"
    scenario = {
        "turn": [
            {"tool": "Bash", "input": {"command": command}, "result": "restarted", "tries": 5},
            {"text": "Could not touch live."},
        ]
    }
    run = stand.run(scenario, mcp=False).finish()
    check_stream(run.messages)
    denied = run.of("system", "permission_denied")
    assert denied and denied[0]["tool_name"] == "Bash"
    assert denied[0]["decision_reason_type"] == "hook"
    assert "prod-alias" in denied[0]["decision_reason"]
    assert run.result["permission_denials"][0]["tool_input"]["command"] == command
    assert run.tool_result_text("Bash") == denied[0]["message"]
    assert f"live.action acme-shop live · deny · {command}" in stand.events()
    with stand.board() as board:
        assert "systemctl restart app" in board.get("/activity?kind=live.action").text


def _spooled(stand: Stand) -> list[dict[str, Any]]:
    return [json.loads(x) for x in stand.spool.read_text("utf-8").splitlines()]


def test_events_of_a_session_are_spooled_while_the_server_is_away(stand: Stand) -> None:
    session_id = "11111111-2222-4333-8444-555555555555"
    # A desktop session id: its task stays bound over a session end.
    desktop = "local_00000000-0000-0000-0000-000000000001"
    stand.mcp("project_upsert", slug="acme-shop", repo_path=stand.project_dir.as_posix())
    stand.mcp(
        "task_create",
        project="acme-shop",
        title="Checkout",
        stage="dev",
        session=desktop,
        hook_session=session_id,
    )
    stand.run({"turn": []}, "--resume", session_id, mcp=False).finish()
    assert "idle → ended" in stand.events()

    stand.stop_server()
    away = stand.run({"turn": [{"text": "Working alone."}]}, "--resume", session_id).finish()
    # The session never notices: the hooks fail open and only the MCP server shows as failed.
    assert away.result["subtype"] == "success" and away.init["session_id"] == session_id
    assert {s["status"] for s in away.init["mcp_servers"]} == {"failed"}
    spooled = _spooled(stand)
    assert [e["hook_event_name"] for e in spooled] == [
        "SessionStart",
        "UserPromptSubmit",
        "Stop",
        "SessionEnd",
    ]
    assert all(e["session_id"] == session_id for e in spooled)

    stand.start_server()
    # Any later hook that reaches the server sends the spool first, oldest first.
    stand.run({"session_id": "another-session", "turn": []}, mcp=False).finish()
    assert not stand.spool.exists()
    # Replayed in order: the spooled start, stop and end of the resumed session.
    replayed = stand.events().split("ended → working", 1)[1]
    assert "working → idle" in replayed and "idle → ended" in replayed


def test_stop_all_ends_the_turn_and_release_lets_work_go_on(stand: Stand) -> None:
    stand.mcp("project_upsert", slug="acme-shop", repo_path=stand.project_dir.as_posix())
    with stand.board() as board:
        stand.post_form(board, "/brake", scope="all", reason="Deploy went wrong", next="/sessions")
        assert "Brake on: all agents." in board.get("/sessions").text

    # `tries`: the forwarder's one short try on a tool call fails open by design.
    scenario = {
        "turn": [
            {"tool": "Bash", "input": {"command": "npm test"}, "result": "ran", "tries": 5},
            {"tool": "Bash", "input": {"command": "npm run deploy"}, "result": "deployed"},
            {"text": "This is never said."},
        ]
    }
    stopped = stand.run(scenario, mcp=False).finish()
    check_stream(stopped.messages)
    [event] = [e for e in stopped.trace("stopped") if e["event"] == "PreToolUse"]
    assert "Deploy went wrong" in event["reason"]
    denied = stopped.of("system", "permission_denied")
    assert denied and denied[0]["tool_name"] == "Bash"
    assert "relaybay brake on (all agents)" in denied[0]["decision_reason"]
    assert stopped.result["permission_denials"][0]["tool_input"]["command"] == "npm test"
    commands = [d["tool_input"]["command"] for d in stopped.result["permission_denials"]]
    assert commands == ["npm test"]  # the turn ended there: the next call was never tried

    # relaybay's own tools still pass, so that an agent under the brake can report.
    report = {
        "turn": [
            PROJECT,
            NEW_TASK,
            {"tool": "Bash", "input": {"command": "npm test"}, "result": "ran", "tries": 5},
        ]
    }
    reporting = stand.run(report).finish()
    assert [s["name"] for s in reporting.init["mcp_servers"]] == ["relaybay"]
    assert "acme-shop-1" in stand.mcp("task_list", project="acme-shop")
    assert reporting.result["permission_denials"][0]["tool_name"] == "Bash"

    with stand.board() as board:
        page = board.get("/sessions").text
        brake_id = page.split('action="/brake/')[1].split("/release")[0]
        stand.post_form(board, f"/brake/{brake_id}/release", next="/sessions")
        assert "Brake on" not in board.get("/sessions").text

    freed = stand.run(scenario, mcp=False).finish()
    check_stream(freed.messages)
    assert freed.result["subtype"] == "success" and not freed.result["permission_denials"]
    assert not freed.trace("stopped")
    assert freed.tool_result_text("Bash") in ("ran", "deployed")
    assert "brake.released" in stand.mcp("events_since", limit=500)


def test_an_agent_cannot_unbind_itself_from_under_a_project_brake(stand: Stand) -> None:
    stand.mcp("project_upsert", slug="acme-shop", repo_path=stand.project_dir.as_posix())
    stand.mcp("task_create", project="acme-shop", title="Checkout", session="dev-1")
    with stand.board() as board:
        stand.post_form(board, "/brake", scope="project", project="acme-shop", next="/sessions")
    for tool, extra in (("task_unbind_session", {}), ("task_bind_session", {"session": "other"})):
        # The SDK client's task groups wrap the failed assertion of the call.
        with pytest.raises(ExceptionGroup) as refused:
            stand.mcp(tool, task="acme-shop-1", **extra)
        assert refused.group_contains(AssertionError, match="under the brake")
    assert "dev-1" in stand.mcp("task_get", task="acme-shop-1")
