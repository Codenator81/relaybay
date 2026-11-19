"""Where a session runs (design 0025): the hook records it from CLAUDE_CODE_ENTRYPOINT, the
session is asked only when the hook cannot tell, and `session_surface` in task_patch stores its
answer on the calling session."""

import json
from typing import Any

import pytest

from relaybay import hook_client
from relaybay.domain.errors import ValidationError
from relaybay.mcp_server import format as fmt
from relaybay.service import Service
from tests.test_hooks import CC_A, CC_B, CWD, DESKTOP_A, RECORDED, bound_task, hook

ASKED = "could not tell whether you run in the Claude desktop app or in a terminal"


def start(cc_id: str = CC_A, entrypoint: str | None = None) -> dict[str, Any]:
    extra = {"relaybay_entrypoint": entrypoint} if entrypoint is not None else {}
    return hook("SessionStart", cc_id, **extra)


async def context_of(service: Service, payload: dict[str, Any]) -> str:
    await service.record_hook(payload)
    answer = await service.hook_answer(payload)
    assert answer is not None
    return answer["hookSpecificOutput"]["additionalContext"]


def test_hook_client_sends_the_entrypoint_with_session_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = json.dumps(RECORDED["SessionStart"]).encode()
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    sent = json.loads(hook_client.with_host_session(body))
    assert sent["relaybay_entrypoint"] == "cli"
    assert "relaybay_host_session" not in sent  # no desktop id in a terminal
    stop = json.dumps(RECORDED["Stop"]).encode()
    assert hook_client.with_host_session(stop) is stop
    monkeypatch.setenv("CLAUDE_CODE_HOST_SESSION_ID", DESKTOP_A)
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    sent = json.loads(hook_client.with_host_session(body))
    assert (sent["relaybay_entrypoint"], sent["relaybay_host_session"]) == (
        "claude-desktop",
        DESKTOP_A,
    )


async def test_a_terminal_session_is_recorded_and_not_asked(service: Service) -> None:
    context = await context_of(service, start(entrypoint="cli"))
    assert ASKED not in context
    [row] = [r.session for r in await service.list_sessions()]
    assert (row.surface, row.entrypoint) == ("terminal", "cli")


async def test_a_desktop_entrypoint_is_recorded(service: Service) -> None:
    await service.record_hook(start(entrypoint="claude-desktop"))
    [row] = [r.session for r in await service.list_sessions()]
    assert (row.surface, row.entrypoint) == ("desktop", "claude-desktop")


async def test_an_older_hook_client_keeps_working_and_the_session_is_asked(
    service: Service,
) -> None:
    context = await context_of(service, start())
    assert ASKED in context and f'session="acme-shop", hook_session="{CC_A}"' in context
    [row] = [r.session for r in await service.list_sessions()]
    assert (row.surface, row.entrypoint) == (None, None)
    # An entrypoint that tells nothing is kept raw, and the session is still asked.
    context = await context_of(service, start(CC_B, entrypoint="sdk-py"))
    assert ASKED in context
    rows = {r.session.cc_session_id: r.session for r in await service.list_sessions()}
    assert (rows[CC_B].surface, rows[CC_B].entrypoint) == (None, "sdk-py")


async def test_a_session_with_a_desktop_id_is_never_asked(service: Service) -> None:
    await bound_task(service)  # binds DESKTOP_A in the folder of the recorded hooks
    context = await context_of(service, start() | {"relaybay_host_session": DESKTOP_A})
    assert ASKED not in context


async def test_the_session_answers_with_task_patch(service: Service) -> None:
    task = await bound_task(service, session="acme-shop")
    await service.record_hook(start())
    saved = await service.update_task_checked(
        task.key, {"session_surface": "Terminal"}, hook_session=CC_A, actor="mcp"
    )
    assert saved.task.version == task.version  # the task itself did not change
    [row] = [r.session for r in await service.list_sessions()]
    assert row.surface == "terminal"
    events = [e for e in await service.events_since(0) if e.kind == "session.surface_set"]
    assert [(e.payload["surface"], e.payload["key"]) for e in events] == [("terminal", task.key)]
    # Said again: nothing new is written. A later SessionStart that tells nothing keeps it.
    await service.update_task_checked(task.key, {"session_surface": "terminal"}, hook_session=CC_A)
    assert ASKED not in await context_of(service, start(entrypoint="sdk-py"))
    events = [e for e in await service.events_since(0) if e.kind == "session.surface_set"]
    assert len(events) == 1
    # '-' or null clears it, and the next start asks again.
    await service.update_task_checked(task.key, {"session_surface": None}, hook_session=CC_A)
    assert ASKED in await context_of(service, start())


async def test_session_surface_is_checked(service: Service) -> None:
    task = await bound_task(service, session="acme-shop")
    await service.record_hook(start())
    with pytest.raises(ValidationError, match="desktop, terminal"):
        await service.update_task_checked(task.key, {"session_surface": "phone"}, hook_session=CC_A)
    with pytest.raises(ValidationError, match="pass session and hook_session"):
        await service.update_task_checked(task.key, {"session_surface": "terminal"})
    with pytest.raises(ValidationError, match="session_surface"):
        await service.update_task_checked(task.key, {"owner": "x"})


def test_the_schema_lists_session_surface() -> None:
    schema = fmt.task_schema()
    assert "| `session_surface` | string |" in schema and "hook_session" in schema


async def test_a_session_cannot_set_another_sessions_surface(service: Service) -> None:
    task = await bound_task(service, session="acme-shop")
    await service.record_hook(start())
    # The terminal's agent linked its hook session itself.
    await service.update_task_checked(
        task.key, {"branch": "x"}, session="acme-shop", hook_session=CC_A
    )
    with pytest.raises(ValidationError, match="is linked to acme-shop"):
        await service.update_task_checked(
            task.key, {"session_surface": "desktop"}, hook_session=CC_A
        )
    # Another session naming itself and that id: the guard of the link keeps the id where it is,
    # and only the caller's own row gets the surface.
    await service.update_task_checked(
        task.key, {"session_surface": "desktop"}, session="coordinator", hook_session=CC_A
    )
    rows = {r.session.name: r.session for r in await service.list_sessions()}
    assert rows["acme-shop"].surface is None and rows["acme-shop"].cc_session_id == CC_A
    assert rows["coordinator"].surface == "desktop"
    # With its own name the session says it.
    await service.update_task_checked(
        task.key, {"session_surface": "terminal"}, session="acme-shop", hook_session=CC_A
    )
    rows = {r.session.name: r.session for r in await service.list_sessions()}
    assert rows["acme-shop"].surface == "terminal"


async def test_a_named_session_takes_the_surface_of_its_hook_row(service: Service) -> None:
    """A terminal session bound by name keeps what its hooks recorded when its agent links the
    hook session to the name: Open is offered after its end."""
    task = await bound_task(service, session="checkout-dev")
    await service.record_hook(start(entrypoint="cli"))  # a hook row named after the folder
    await service.update_task_checked(
        task.key, {"branch": "x"}, session="checkout-dev", hook_session=CC_A
    )
    rows = {r.session.name: r.session for r in await service.list_sessions()}
    named = rows["checkout-dev"]
    assert (named.surface, named.entrypoint, named.start_cwd) == ("terminal", "cli", CWD)
    await service.record_hook(hook("SessionEnd"))
    current = await service.get_task(task.key)
    detail = await service.task_detail(current.key)
    assert detail.state.label == "Ended" and detail.open is not None
    assert detail.open.terminal.enabled and not detail.open.desktop.enabled
