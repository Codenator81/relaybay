"""The board's run pages without any `claude`: the Agent block, project defaults, Settings → Agents.
`claude_path` names a file that does not exist, so nothing is ever started here."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import Settings
from relaybay.runner.layers import LayerState
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_runs_service import CC_A, StubRuns


@pytest.fixture
def board(settings: Settings, token: str, tmp_path: Path) -> Iterator[TestClient]:
    settings.claude_path = tmp_path / "no-such-claude"
    with TestClient(create_app(settings), base_url=BASE) as client:
        assert client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        service: Service = client.app.state.service
        repo = tmp_path / "acme-shop"
        repo.mkdir()
        run_in_app(client, service.upsert_project, "acme-shop", repo_path=str(repo))
        run_in_app(client, service.create_task, "acme-shop", "Checkout", stage="dev")
        yield client


def form(token: str, **fields: str) -> dict[str, str]:
    return {"csrf_token": csrf_token(token), **fields}


def test_agent_block_shows_the_start_form_and_why_it_would_refuse(
    board: TestClient, token: str
) -> None:
    view = board.get("/p/acme-shop?task=acme-shop-1")
    assert view.status_code == 200 and 'hx-get="/p/acme-shop/t/acme-shop-1/run"' in view.text
    panel = board.get("/p/acme-shop/t/acme-shop-1/run")
    assert panel.status_code == 200
    assert "which is not a file" in panel.text and 'name="brief"' in panel.text
    assert "Checkout" in panel.text  # the brief comes from the card
    refused = board.post(
        "/p/acme-shop/t/acme-shop-1/run/start",
        data=form(token),
        headers={"Origin": BASE, "HX-Request": "true"},
    )
    assert refused.status_code == 200 and "which is not a file" in refused.text
    bearer = board.post(
        "/p/acme-shop/t/acme-shop-1/run/start",
        data=form(token),
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403  # board only


def test_project_defaults_on_stands_and_access(board: TestClient, token: str) -> None:
    page = board.get("/p/acme-shop/environments")
    assert page.status_code == 200 and "Agent defaults" in page.text
    assert "bypassPermissions" not in page.text
    saved = board.post(
        "/p/acme-shop/run-defaults",
        data=form(
            token,
            model="sonnet",
            effort="high",
            permission_mode="plan",
            allowed_tools="mcp__relaybay, Bash(git diff *)",
            chrome=["0", "1"],  # type: ignore[arg-type]
        ),
        headers={"Origin": BASE},
    )
    assert saved.status_code == 200 and "Agent defaults saved." in saved.text
    service: Service = board.app.state.service
    defaults = run_in_app(board, service.run_defaults, "acme-shop")
    assert (defaults.model, defaults.permission_mode, defaults.chrome) == ("sonnet", "plan", True)
    bad = board.post(
        "/p/acme-shop/run-defaults",
        data=form(token, permission_mode="bypassPermissions", allowed_tools="mcp__relaybay"),
        headers={"Origin": BASE},
    )
    assert bad.status_code == 400 and "permission mode must be one of" in bad.text


def test_settings_agents_and_the_sign_in_switch(board: TestClient, token: str) -> None:
    page = board.get("/settings/agents")
    assert page.status_code == 200 and "not found on PATH" in page.text
    assert 'href="/settings/agents"' in board.get("/settings/notifications").text
    saved = board.post(
        "/settings/agents/login-methods",
        data=form(token, allow=["0", "1"]),  # type: ignore[arg-type]
        headers={"Origin": BASE},
    )
    assert saved.status_code == 200 and "Saved." in saved.text
    service: Service = board.app.state.service
    assert run_in_app(board, service.run_login_any) is True
    board.post(
        "/settings/agents/login-methods", data=form(token, allow="0"), headers={"Origin": BASE}
    )
    assert run_in_app(board, service.run_login_any) is False


def test_open_in_desktop_back_and_the_settings_between_turns(
    board: TestClient, token: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ok = LayerState("command", True, "command hooks from your user settings", "registered")
    monkeypatch.setattr("relaybay.service.user_layer_state", lambda *_: ok)
    service: Service = board.app.state.service
    stub = StubRuns(service, tmp_path)
    service.runs = stub  # type: ignore[assignment]
    htmx = {"Origin": BASE, "HX-Request": "true"}
    base = "/p/acme-shop/t/acme-shop-1/run"
    run = run_in_app(board, service.start_run, "acme-shop-1", brief="Build it")
    running = board.get(base).text
    assert "Open in the desktop app" not in running
    assert '<fieldset class="field-row" disabled>' in running
    refused = board.post(f"{base}/{run.id}/settings", data=form(token, model="haiku"), headers=htmx)
    assert "is running" in refused.text
    run_in_app(board, stub.finish, run.id)
    idle = board.get(base).text
    assert "Open in the desktop app" in idle and "disabled>" not in idle
    bearer = board.post(
        f"{base}/{run.id}/desktop",
        data=form(token),
        headers={"Origin": BASE, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403 and stub.desktop_calls == []  # board only
    opened = board.post(f"{base}/{run.id}/desktop", data=form(token), headers=htmx)
    assert opened.status_code == 200 and "Back to the program" in opened.text
    assert f'href="claude://claude.ai/epitaxy/local_{CC_A}"' in opened.text
    assert "waits until Back to the program" in opened.text
    saved = board.post(
        f"{base}/{run.id}/settings",
        data=form(token, model="sonnet", effort="high", permission_mode="plan"),
        headers=htmx,
    )
    assert "form-error" not in saved.text
    changed = run_in_app(board, service.get_run, run.id)
    assert (changed.model, changed.effort, changed.permission_mode) == ("sonnet", "high", "plan")
    assert "Close or leave the session in the desktop app" in opened.text
    service.desktop_launching.add(run.id)
    launching = board.get(base).text
    assert "Opening the desktop app" in launching and "/back" not in launching
    service.desktop_launching.discard(run.id)
    back = board.post(f"{base}/{run.id}/back", data=form(token), headers=htmx)
    assert "Back to the program" not in back.text and "Open in the desktop app" in back.text
    assert '<a class="btn primary" href="claude://' in back.text  # the link leads now
    assert "--add-dir" not in back.text  # one folder: no line about the others
    stub.desktop_launcher = None
    terminal = board.get(base).text
    assert "Open in the desktop app" not in terminal and "claude --resume" not in terminal
    assert "Go to the session in the desktop app" in terminal  # opened once: the link, not a CLI
