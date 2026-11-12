import json
import socket
import sys
from pathlib import Path

import pytest

from relaybay import cli, doctor
from relaybay.config import load_settings
from relaybay.machine_setup import (
    Detection,
    claude_paths,
    desired_groups,
    hook_handler,
    plan_apply,
    write_change,
)
from relaybay.runner.auth import LoginState, check_login

NEW = Detection(claude_path="/usr/local/bin/claude", claude_version="2.1.300 (Claude Code)")
TOO_OLD = Detection(claude_path="/usr/local/bin/claude", claude_version="1.0.90 (Claude Code)")


SIGNED_IN = LoginState(True, "claude.ai", "max", 0, readable=True)


@pytest.fixture(autouse=True)
def fake_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs the real `claude` binary."""
    monkeypatch.setattr(doctor, "detect", lambda **_: NEW)
    monkeypatch.setattr(doctor, "check_login", lambda *_: SIGNED_IN)


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude-home"
    (home / ".claude").mkdir(parents=True)
    return home


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def by_name(checks) -> dict:
    return {c.name: c for c in checks}


def test_all_green_against_a_running_server(live_server, claude_home: Path) -> None:
    server = live_server()
    paths = claude_paths(claude_home)
    handler = hook_handler("command", server.url, server.settings.token_path, python=sys.executable)
    write_change(plan_apply(paths.settings, desired_groups(handler)))
    paths.config.write_text(
        json.dumps({"mcpServers": {"relaybay": {"type": "http", "url": f"{server.url}/mcp"}}}),
        encoding="utf-8",
    )
    settings = load_settings(
        home=server.settings.home, port=server.settings.port, claude_home=claude_home, environ={}
    )
    before = paths.settings.read_bytes()
    checks = doctor.doctor_checks(settings, environ={})
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]
    names = [c.name for c in checks]
    for expected in (
        "relaybay",
        "Python",
        "OS",
        "data folder",
        "token file",
        "Claude Code",
        "relaybay server",
        "MCP with the token",
        "hook endpoint",
        "rules hash",
        "hooks in user settings",
        "command hook runs",
        "MCP registered for Claude Code",
    ):
        assert expected in names
    assert "the server uses the same" in by_name(checks)["rules hash"].detail
    assert paths.settings.read_bytes() == before  # read-only


def test_rules_changed_since_the_server_started(live_server, claude_home: Path) -> None:
    server = live_server()
    settings = load_settings(
        home=server.settings.home, port=server.settings.port, claude_home=claude_home, environ={}
    )
    settings.rules_mode = "strict"  # as if config.toml changed after the start
    item = by_name(doctor.doctor_checks(settings, environ={}))["rules hash"]
    assert not item.ok and "restart" in item.detail


def test_stopped_server_still_reports_the_claude_code_files(
    tmp_path: Path, claude_home: Path
) -> None:
    home = tmp_path / "data"
    home.mkdir()
    (home / "token").write_text("t" * 43, encoding="utf-8")
    settings = load_settings(home=home, port=free_port(), claude_home=claude_home, environ={})
    checks = by_name(doctor.doctor_checks(settings, environ={}))
    assert not checks["relaybay server"].ok and "not reachable" in checks["relaybay server"].detail
    assert "MCP with the token" not in checks
    assert checks["rules hash"].ok
    assert not checks["hooks in user settings"].ok
    assert not checks["MCP registered for Claude Code"].ok


def test_never_creates_the_token(tmp_path: Path, claude_home: Path) -> None:
    home = tmp_path / "fresh"
    settings = load_settings(home=home, port=free_port(), claude_home=claude_home, environ={})
    checks = by_name(doctor.doctor_checks(settings, environ={}))
    assert not checks["data folder"].ok
    assert not checks["token file"].ok
    assert not checks["relaybay server"].ok
    assert not home.exists()


def test_claude_code_version_is_checked() -> None:
    assert doctor._claude_check(NEW).ok
    old = doctor._claude_check(TOO_OLD)
    assert not old.ok and "minimum supported 2.0.0" in old.detail
    assert not doctor._claude_check(Detection()).ok
    unknown = doctor._claude_check(Detection(claude_path="/bin/claude", version_error="boom"))
    assert not unknown.ok and "boom" in unknown.detail


def test_cli_json_shape_and_exit_code(
    tmp_path: Path, claude_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(
        [
            "doctor",
            "--json",
            "--home",
            str(tmp_path / "data"),
            "--port",
            str(free_port()),
            "--claude-home",
            str(claude_home),
        ]
    )
    results = json.loads(capsys.readouterr().out)
    assert code == 1
    assert all(set(r) == {"ok", "name", "detail"} for r in results)
    assert results[0]["name"] == "relaybay" and results[0]["ok"] is True


def test_cli_reports_broken_settings_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "data"
    home.mkdir()
    (home / "config.toml").write_text("port = 'x'\n", encoding="utf-8")
    assert cli.main(["doctor", "--json", "--home", str(home)]) == 1
    results = json.loads(capsys.readouterr().out)
    assert results == [
        {"ok": False, "name": "settings", "detail": results[0]["detail"]},
    ]
    assert "port must be an integer" in results[0]["detail"]


def test_claude_sign_in_line_per_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def answering(code: int, **fields: object):
        body = json.dumps({**fields, "email": "owner@example.invalid", "orgName": "Acme Org"})

        def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            assert argv[1:] == ["auth", "status"]
            return subprocess.CompletedProcess(argv, code, body, "")

        return run

    monkeypatch.setattr(doctor, "check_login", check_login)  # the real one, a fake process
    cases = [
        (answering(0, loggedIn=True, authMethod="claude.ai", subscriptionType="max"), True),
        (answering(1, loggedIn=False, authMethod="none"), False),
        (answering(0, loggedIn=True, authMethod="api_key"), True),
        (answering(0, loggedIn=True, authMethod="third_party"), True),
    ]
    details = []
    for run, ok in cases:
        item = doctor._login_check(NEW, run)
        assert item.name == "claude sign-in" and item.ok is ok
        details.append(item.detail)
    assert details[0] == "claude: signed in with a subscription (max)"
    assert "not signed in" in details[1] and "/login" in details[1]
    assert details[2].endswith("runs need Settings → Agents to allow it")
    assert "third-party provider" in details[3]
    assert not any("example.invalid" in d or "Acme Org" in d for d in details)
    assert not doctor._login_check(Detection(), answering(0)).ok
