"""`relaybay setup` and Settings → Integrations, always against temporary home folders."""

import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from relaybay import cli, machine_setup
from relaybay.config import Settings, load_settings
from relaybay.machine_setup import (
    ClaudePaths,
    Detection,
    FileChange,
    SetupError,
    claude_paths,
    desired_groups,
    detect,
    hook_command,
    hook_handler,
    installed_mode,
    is_ours,
    make_plan,
    merge_hooks,
    mode_notes,
    owned_hosts,
    plan_apply,
    plan_remove,
    remove_hooks,
    run_check,
    write_change,
)
from relaybay.security import csrf_token

BASE = "http://127.0.0.1:8765"
HTMX = {"HX-Request": "true", "Origin": BASE}
NEW = Detection(claude_path="/usr/local/bin/claude", claude_version="2.1.300 (Claude Code)")
OLD = Detection(claude_path="/usr/local/bin/claude", claude_version="2.0.9 (Claude Code)")

# A user settings file with foreign hooks in the same events relaybay uses, 4-space indent.
FOREIGN = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {"allow": ["Bash(npm run lint)"], "deny": ["Read(./.env)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "python ~/.claude/hooks/git-policy.py"}],
            },
            {
                "matcher": "Edit|Write",
                "hooks": [
                    {"type": "command", "command": "npx prettier --check", "timeout": 30},
                    {"type": "http", "url": "http://localhost:9000/audit"},
                ],
            },
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "notify-send done"}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "log.sh"}]}],
    },
    "model": "opus",
}


@pytest.fixture(autouse=True)
def fake_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs the real `claude` binary."""
    monkeypatch.setattr(machine_setup, "detect", lambda **_: NEW)
    monkeypatch.setattr(cli, "detect", lambda **_: NEW)


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude-home"
    (home / ".claude").mkdir(parents=True)
    return home


@pytest.fixture
def settings(tmp_path: Path, claude_home: Path) -> Settings:
    # Overrides the conftest fixture: the board's Integrations page targets the temp home.
    return load_settings(home=tmp_path / "data", claude_home=claude_home, environ={})


def write_foreign(path: Path) -> bytes:
    path.write_text(json.dumps(FOREIGN, indent=4) + "\n", encoding="utf-8")
    return path.read_bytes()


def ours(token_path: Path, mode: str = "command", base: str = BASE) -> dict[str, dict]:
    return desired_groups(hook_handler(mode, base, token_path, python="/opt/py/bin/python"))


def foreign_only(data: dict) -> dict:
    """`data` with relaybay's handlers taken out, groups left as they are."""
    return {
        **data,
        "hooks": {
            event: [
                {**g, "hooks": [h for h in g["hooks"] if not is_ours(h)]}
                for g in groups
                if any(not is_ours(h) for h in g["hooks"])
            ]
            for event, groups in data["hooks"].items()
            if any(not is_ours(h) for g in groups for h in g["hooks"])
        },
    }


# --- merge -------------------------------------------------------------------


def test_merge_keeps_foreign_entries_and_their_order(tmp_path: Path) -> None:
    groups = ours(tmp_path / "token")
    merged = merge_hooks(FOREIGN, groups)
    assert list(merged) == list(FOREIGN)  # top-level key order
    assert list(merged["hooks"])[:3] == ["PreToolUse", "Stop", "PostToolUse"]
    assert merged["hooks"]["PreToolUse"][:2] == FOREIGN["hooks"]["PreToolUse"]
    assert merged["hooks"]["PreToolUse"][2] == groups["PreToolUse"]
    assert merged["hooks"]["Stop"][0] == FOREIGN["hooks"]["Stop"][0]
    assert merged["hooks"]["PostToolUse"][:1] == FOREIGN["hooks"]["PostToolUse"]
    assert merged["hooks"]["PostToolUse"][1] == groups["PostToolUse"]
    assert foreign_only(merged) == FOREIGN | {"hooks": FOREIGN["hooks"]}
    for event in ("SessionStart", "UserPromptSubmit", "Notification", "SessionEnd"):
        assert merged["hooks"][event] == [groups[event]]
    assert installed_mode(merged) == "command"
    assert merge_hooks(merged, groups) == merged  # idempotent
    assert remove_hooks(merged) == FOREIGN


def test_changed_port_updates_relaybay_entries_in_place(tmp_path: Path) -> None:
    merged = merge_hooks(FOREIGN, ours(tmp_path / "token"))
    # The user moved relaybay's PreToolUse group to the front: it stays there.
    pre = merged["hooks"]["PreToolUse"]
    pre.insert(0, pre.pop())
    moved = merge_hooks(merged, ours(tmp_path / "token", base="http://127.0.0.1:9999"))
    handler = moved["hooks"]["PreToolUse"][0]["hooks"][0]
    assert "127.0.0.1:9999/hooks/claude-code" in handler["command"]
    assert moved["hooks"]["PreToolUse"][1:] == FOREIGN["hooks"]["PreToolUse"]
    assert sum(is_ours(h) for g in moved["hooks"]["PreToolUse"] for h in g["hooks"]) == 1


def test_http_handler_references_the_token_by_env_var(tmp_path: Path) -> None:
    handler = hook_handler("http", BASE, tmp_path / "token")
    assert handler["headers"] == {
        "Authorization": "Bearer ${RELAYBAY_TOKEN}",
        "X-Relaybay-Host-Session": "${CLAUDE_CODE_HOST_SESSION_ID}",
        "X-Relaybay-Host-Attended": "${CLAUDE_CODE_SESSION_ATTENDED}",
        "X-Relaybay-Run": "${RELAYBAY_RUN_ID}",
    }
    assert handler["allowedEnvVars"] == [
        "RELAYBAY_TOKEN",
        "CLAUDE_CODE_HOST_SESSION_ID",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "RELAYBAY_RUN_ID",
    ]
    assert handler["url"] == f"{BASE}/hooks/claude-code" and is_ours(handler)
    assert handler["timeout"] <= 5
    assert not is_ours({"type": "http", "url": "http://localhost:9000/audit"})
    assert not is_ours({"type": "command", "command": "relaybay-unrelated"})


def test_hook_command_runs_this_interpreter_and_quotes_spaces(tmp_path: Path) -> None:
    command = hook_command(BASE, Path("/data/relaybay/token"), python="/opt/py/bin/python")
    assert command.startswith("/opt/py/bin/python -I -m relaybay.hook_client --url ")
    assert f"{BASE}/hooks/claude-code" in command
    spaced = hook_command(
        BASE, tmp_path / "token", python="C:/Program Files/Python/python.exe", platform="win32"
    )
    assert spaced.startswith('"C:/Program Files/Python/python.exe" -I -m relaybay.hook_client')
    assert "\\" not in spaced
    assert is_ours({"type": "command", "command": spaced})


def test_posix_quoting_keeps_dollar_and_backtick_literal() -> None:
    python = "/home/dev/my $env/`py`/it's/python"
    command = hook_command(BASE, Path("/data/token"), python=python, platform="linux")
    assert shlex.split(command)[0] == python
    assert is_ours({"type": "command", "command": command})
    result = (
        subprocess.run(
            ["sh", "-c", f"printf %s {command.split(' -I ')[0]}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if shutil.which("sh")
        else None
    )
    if result is not None:
        assert result.stdout == python


def test_mode_notes_only_for_http() -> None:
    assert mode_notes("command", OLD, {}, "tok") == []
    notes = " ".join(mode_notes("http", OLD, {}, "tok"))
    assert "2.0.9" in notes and "RELAYBAY_TOKEN" in notes and "hook error" in notes
    notes = " ".join(mode_notes("http", NEW, {"RELAYBAY_TOKEN": "tok"}, "tok"))
    assert "RELAYBAY_TOKEN" not in notes and "hook error" in notes


@pytest.mark.parametrize(
    "handler",
    [
        {"type": "command", "command": "python ~/scripts/hooks/claude-code-notify.py"},
        {"type": "command", "command": "notify --url http://127.0.0.1:8765/hooks/claude-code"},
        {"type": "command", "command": "python -m relaybay.hook_client --url http://x/other"},
        {"type": "http", "url": "https://example.com/team/hooks/claude-code"},
        {"type": "http", "url": "http://127.0.0.1:8765/hooks/claude-code-audit"},
        {"type": "http", "url": "http://127.0.0.1:8765/x/hooks/claude-code"},
    ],
)
def test_lookalike_entries_are_not_ours(handler: dict) -> None:
    assert not is_ours(handler)


def test_lookalike_entries_survive_apply_and_remove(tmp_path: Path, claude_home: Path) -> None:
    probes = {
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": "python ~/hooks/claude-code-notify.py"},
                    {"type": "http", "url": "https://example.com/team/hooks/claude-code"},
                ],
            }
        ],
        "Stop": [
            {"hooks": [{"type": "http", "url": "https://example.com/team/hooks/claude-code"}]}
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "my-logger.sh && python -m relaybay.hook_client --url "
                        "http://127.0.0.1:8765/hooks/claude-code --token-file t",
                    },
                    {"type": "http", "url": "http://127.0.0.1:8765/hooks/claude-code?x=1"},
                ]
            }
        ],
    }
    data = {"hooks": probes}
    path = claude_home / ".claude" / "settings.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    original = path.read_bytes()
    write_change(plan_apply(path, ours(tmp_path / "token")))
    merged = json.loads(path.read_text(encoding="utf-8"))
    assert merged["hooks"]["PreToolUse"][0] == probes["PreToolUse"][0]
    assert merged["hooks"]["Stop"][0] == probes["Stop"][0]
    assert merged["hooks"]["SessionEnd"][0] == probes["SessionEnd"][0]
    assert len(merged["hooks"]["PreToolUse"]) == 2  # relaybay's group added, nothing replaced
    write_change(plan_remove(path))
    assert path.read_bytes() == original


def test_configured_host_counts_for_http_entries(tmp_path: Path) -> None:
    lan = "http://192.168.1.20:8765"
    handler = hook_handler("http", lan, tmp_path / "token")
    assert not is_ours(handler)
    assert is_ours(handler, owned_hosts(lan))


def test_detect_with_fakes(tmp_path: Path) -> None:
    def run(args, **_):
        assert args[1:] == ["--version"]
        return SimpleNamespace(stdout="2.1.300 (Claude Code)\n", stderr="")

    found = detect(which=lambda name: "/bin/claude", run=run, platform="linux", home=tmp_path)
    assert (found.claude_version, found.http_hooks) == ("2.1.300 (Claude Code)", True)
    assert OLD.http_hooks is False

    def broken(args, **_):
        raise subprocess.TimeoutExpired(args, 10)

    found = detect(which=lambda name: "/bin/claude", run=broken, platform="linux", home=tmp_path)
    assert found.claude_version is None and found.version_error and not found.http_hooks
    assert detect(which=lambda name: None, platform="linux", home=tmp_path).claude_path is None

    local = tmp_path / "local"
    (local / "Packages" / "Claude_0abc123").mkdir(parents=True)
    win = detect(which=lambda name: None, platform="win32", environ={"LOCALAPPDATA": str(local)})
    assert win.desktop_app and win.desktop_app.endswith("Claude_0abc123")
    (tmp_path / "Applications" / "Claude.app").mkdir(parents=True)
    mac = detect(which=lambda name: None, platform="darwin", home=tmp_path)
    assert mac.desktop_app is not None


def test_claude_paths_use_the_given_home(tmp_path: Path) -> None:
    paths = claude_paths(tmp_path)
    assert paths.settings == tmp_path / ".claude" / "settings.json"
    assert paths.config == tmp_path / ".claude.json"


# --- files -------------------------------------------------------------------


def test_apply_backs_up_and_remove_restores_the_bytes(tmp_path: Path, claude_home: Path) -> None:
    path = claude_home / ".claude" / "settings.json"
    original = write_foreign(path)
    change = plan_apply(path, ours(tmp_path / "token"))
    assert change.changed and change.diff().count("\n+") > 10
    assert all(
        not line.startswith("-") or line.startswith("---") for line in change.diff().split("\n")
    )
    backup = write_change(change)
    assert backup is not None and backup.read_bytes() == original
    assert re.fullmatch(r"settings\.json\.relaybay-backup-\d{8}-\d{6}", backup.name)
    text = path.read_text(encoding="utf-8")
    assert '\n    "permissions"' in text  # the file's own indent is kept

    again = plan_apply(path, ours(tmp_path / "token"))
    assert not again.changed and write_change(again) is None  # idempotent: no write, no backup

    removal = plan_remove(path)
    second_backup = write_change(removal)
    assert second_backup is not None and second_backup != backup
    assert path.read_bytes() == original
    assert not plan_remove(path).changed


def test_missing_file_is_created_without_backup(claude_home: Path, tmp_path: Path) -> None:
    path = claude_home / ".claude" / "settings.json"
    change = plan_apply(path, ours(tmp_path / "token"))
    assert change.before is None and write_change(change) is None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert installed_mode(data) == "command"
    write_change(plan_remove(path))
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_broken_or_odd_files_are_never_touched(claude_home: Path, tmp_path: Path) -> None:
    path = claude_home / ".claude" / "settings.json"
    for text, message in (
        ('{"hooks": {', "not valid JSON"),
        ("[1, 2]", "JSON object"),
        ('{"hooks": []}', "'hooks' is not an object"),
        ('{"hooks": {"Stop": {}}}', "'hooks.Stop' is not a list"),
        ('{"hooks": {"Stop": ["x"]}}', "matcher group"),
    ):
        path.write_text(text, encoding="utf-8")
        with pytest.raises(SetupError, match=re.escape(message)):
            plan_apply(path, ours(tmp_path / "token"))
        assert path.read_text(encoding="utf-8") == text


def test_write_refuses_a_file_changed_after_the_plan(claude_home: Path, tmp_path: Path) -> None:
    path = claude_home / ".claude" / "settings.json"
    write_foreign(path)
    change = plan_apply(path, ours(tmp_path / "token"))
    path.write_text('{"model": "sonnet"}\n', encoding="utf-8")
    with pytest.raises(SetupError, match="changed since the plan"):
        write_change(change)
    assert path.read_text(encoding="utf-8") == '{"model": "sonnet"}\n'


def test_mcp_registration_is_read_never_written(claude_home: Path, tmp_path: Path) -> None:
    paths = ClaudePaths(claude_home / ".claude" / "settings.json", claude_home / ".claude.json")
    plan = make_plan(
        base_url=BASE,
        token="tok",
        token_path=tmp_path / "token",
        paths=paths,
        detection=NEW,
        environ={},
    )
    assert not plan.mcp_ok and "claude mcp add --transport http --scope user" in plan.mcp_command
    assert "<token>" in plan.mcp_command
    paths.config.write_text(
        json.dumps({"mcpServers": {"relaybay": {"type": "http", "url": f"{BASE}/mcp"}}}),
        encoding="utf-8",
    )
    before = paths.config.read_bytes()
    assert make_plan(
        base_url=BASE,
        token="tok",
        token_path=tmp_path / "token",
        paths=paths,
        detection=NEW,
        environ={},
    ).mcp_ok
    assert paths.config.read_bytes() == before


def test_token_is_never_written(claude_home: Path, tmp_path: Path) -> None:
    token = "secret-token-value"
    path = claude_home / ".claude" / "settings.json"
    for mode in ("http", "command"):
        change = plan_apply(path, ours(tmp_path / "token", mode=mode))
        assert token not in (change.after or "")


# --- CLI -----------------------------------------------------------------------


def run_cli(*args: str) -> int:
    return cli.main(["setup", *args])


def test_cli_dry_run_apply_remove(
    tmp_path: Path,
    claude_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RELAYBAY_TOKEN", raising=False)
    data = tmp_path / "data"
    path = claude_home / ".claude" / "settings.json"
    original = write_foreign(path)
    common = ["--home", str(data), "--claude-home", str(claude_home)]

    assert run_cli(*common) == 0
    out = capsys.readouterr().out
    assert "dry run" in out and "+" in out and "Nothing written" in out
    assert "Hook type: command (a small forwarder" in out
    assert "Plan hash: " in out
    assert "claude mcp add --transport http --scope user relaybay" in out
    assert path.read_bytes() == original

    assert run_cli(*common, "--apply") == 0
    out = capsys.readouterr().out
    assert "Wrote" in out and "backup" in out
    assert installed_mode(json.loads(path.read_text(encoding="utf-8"))) == "command"
    token = (data / "token").read_text(encoding="utf-8").strip()
    assert token not in path.read_text(encoding="utf-8")

    assert run_cli(*common, "--apply") == 0
    assert "already set up, no change" in capsys.readouterr().out

    assert run_cli(*common, "--remove") == 0
    assert path.read_bytes() == original
    backups = sorted(path.parent.glob("settings.json.relaybay-backup-*"))
    assert len(backups) == 2


def test_cli_refuses_a_broken_file(
    tmp_path: Path, claude_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = claude_home / ".claude" / "settings.json"
    path.write_text("{oops", encoding="utf-8")
    common = ["--home", str(tmp_path / "data"), "--claude-home", str(claude_home)]
    assert run_cli(*common, "--apply") == 1
    assert "not valid JSON" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == "{oops"


def test_check_against_a_running_server(live_server, claude_home: Path) -> None:
    server = live_server()
    paths = claude_paths(claude_home)
    checks = run_check(base_url=server.url, token=server.token, paths=paths, environ={})
    by_name = {c.name: c for c in checks}
    assert by_name["relaybay server"].ok
    assert by_name["MCP with the token"].ok
    assert by_name["hook endpoint"].ok
    assert not by_name["hooks in user settings"].ok
    assert not by_name["MCP registered for Claude Code"].ok

    real = hook_handler("command", server.url, server.settings.token_path, python=sys.executable)
    write_change(plan_apply(paths.settings, desired_groups(real)))
    paths.config.write_text(
        json.dumps({"mcpServers": {"relaybay": {"type": "http", "url": f"{server.url}/mcp"}}}),
        encoding="utf-8",
    )
    checks = run_check(base_url=server.url, token=server.token, paths=paths, environ={})
    assert all(c.ok for c in checks), checks
    assert {c.name for c in checks} >= {"command hook runs"}

    wrong = run_check(base_url=server.url, token="not-the-token", paths=paths, environ={})
    assert not {c.name: c for c in wrong}["MCP with the token"].ok

    # An interpreter that can no longer import relaybay (venv moved, package removed).
    def broken(args, **_):
        assert args[1:] == ["-I", "-c", "import relaybay.hook_client"]
        return SimpleNamespace(returncode=1, stderr="ModuleNotFoundError: relaybay\n")

    checks = run_check(base_url=server.url, token=server.token, paths=paths, environ={}, run=broken)
    runs = {c.name: c for c in checks}["command hook runs"]
    assert not runs.ok and "ModuleNotFoundError" in runs.detail


def test_cli_check_json(live_server, claude_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = live_server()
    home = str(server.settings.home)
    port = str(server.settings.port)
    code = cli.main(
        [
            "setup",
            "--check",
            "--json",
            "--home",
            home,
            "--port",
            port,
            "--claude-home",
            str(claude_home),
        ]
    )
    results = json.loads(capsys.readouterr().out)
    assert code == 1  # no hooks, no MCP entry in the temp home
    assert {r["name"]: r["ok"] for r in results}["relaybay server"] is True
    assert set(results[0]) == {"ok", "name", "detail"}


def test_check_reports_a_stopped_server(claude_home: Path) -> None:
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    checks = run_check(
        base_url=f"http://127.0.0.1:{port}", token="t", paths=claude_paths(claude_home)
    )
    assert [(c.name, c.ok) for c in checks] == [("relaybay server", False)]


# --- board ---------------------------------------------------------------------


def test_integrations_page_writes_only_on_button(board: TestClient, claude_home: Path) -> None:
    path = claude_home / ".claude" / "settings.json"
    original = write_foreign(path)
    page = board.get("/settings/integrations")
    assert page.status_code == 200
    assert "Integrations" in page.text and "/usr/local/bin/claude" in page.text
    assert "hooks/claude-code" in page.text  # the diff
    assert path.read_bytes() == original
    assert 'href="/settings/integrations"' in board.get("/").text  # sidebar link

    token = board.app.state.settings.token_path.read_text(encoding="utf-8").strip()
    form = {"csrf_token": csrf_token(token), "mode": "command"}
    assert (
        board.post(
            "/settings/integrations/apply", data={"mode": "command"}, headers=HTMX
        ).status_code
        == 403
    )
    assert path.read_bytes() == original

    apply_hash = expected_hashes(page.text)[0]
    # The file changed after the page was shown: the approved plan no longer holds.
    path.write_bytes(original.replace(b'"opus"', b'"sonnet"'))
    stale = board.post(
        "/settings/integrations/apply", data=form | {"expect": apply_hash}, headers=HTMX
    )
    assert "not the one you approved" in stale.text
    assert path.read_bytes() == original.replace(b'"opus"', b'"sonnet"')
    path.write_bytes(original)
    missing = board.post("/settings/integrations/apply", data=form, headers=HTMX)
    assert "not the one you approved" in missing.text and path.read_bytes() == original

    applied = board.post(
        "/settings/integrations/apply", data=form | {"expect": apply_hash}, headers=HTMX
    )
    assert applied.status_code == 200 and "Wrote" in applied.text
    assert installed_mode(json.loads(path.read_text(encoding="utf-8"))) == "command"
    assert "already set up, no change" in applied.text

    remove_hash = expected_hashes(applied.text)[1]
    removed = board.post(
        "/settings/integrations/remove", data=form | {"expect": remove_hash}, headers=HTMX
    )
    assert "Removed relaybay" in removed.text
    assert path.read_bytes() == original


def expected_hashes(html: str) -> list[str]:
    return re.findall(r'name="expect" value="([0-9a-f]*)"', html)


def test_integrations_page_shows_a_non_utf8_file_as_an_error(
    board: TestClient, claude_home: Path
) -> None:
    path = claude_home / ".claude" / "settings.json"
    path.write_bytes(b'{"model": "caf\xe9"}')
    page = board.get("/settings/integrations")
    assert page.status_code == 200 and "is not UTF-8 text" in page.text
    assert path.read_bytes() == b'{"model": "caf\xe9"}'


def test_integrations_check_and_unlinked_sessions(
    board: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import run_in_app

    service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", repo_path="/home/dev/acme-shop")
    for n in (1, 2):
        run_in_app(
            board,
            service.create_task,
            "acme-shop",
            f"Task {n}",
            session=f"local_00000000-0000-0000-0000-00000000000{n}",
        )
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": "abc12345-x",
        "cwd": "/home/dev/acme-shop",
    }
    run_in_app(board, service.record_hook, payload)

    # Never reach a real port from this test: the check itself is tested above.
    seen: dict = {}

    def fake_check(**kwargs):
        seen.update(kwargs)
        return [machine_setup.Check(True, "relaybay server", "running, same token")]

    monkeypatch.setattr(machine_setup, "run_check", fake_check)
    token = board.app.state.settings.token_path.read_text(encoding="utf-8").strip()
    response = board.post(
        "/settings/integrations/check",
        data={"csrf_token": csrf_token(token)},
        headers=HTMX,
    )
    assert "running, same token" in response.text
    assert seen["paths"].settings.is_relative_to(board.app.state.settings.claude_home)
    assert "acme-shop · abc12345" in response.text
    assert "2 desktop sessions in acme-shop could match" in response.text


def test_file_change_diff_names_the_file(tmp_path: Path) -> None:
    change = FileChange(tmp_path / "s.json", None, "{}\n")
    assert "/dev/null" in change.diff() and change.changed


def test_cli_expect_writes_only_the_approved_plan(
    tmp_path: Path, claude_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = claude_home / ".claude" / "settings.json"
    original = write_foreign(path)
    common = ["--home", str(tmp_path / "data"), "--claude-home", str(claude_home)]
    assert run_cli(*common) == 0
    shown = re.search(r"Plan hash: ([0-9a-f]+)", capsys.readouterr().out)
    assert shown is not None

    path.write_bytes(original.replace(b'"opus"', b'"sonnet"'))
    assert run_cli(*common, "--apply", "--expect", shown[1]) == 1
    assert "not the one you approved" in capsys.readouterr().err
    assert installed_mode(json.loads(path.read_text(encoding="utf-8"))) is None

    path.write_bytes(original)
    assert run_cli(*common, "--apply", "--expect", shown[1]) == 0
    assert installed_mode(json.loads(path.read_text(encoding="utf-8"))) == "command"

    assert run_cli(*common, "--remove", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "remove (dry run)" in out and "--remove --expect" in out
    assert installed_mode(json.loads(path.read_text(encoding="utf-8"))) == "command"


def test_cli_reports_a_non_utf8_file(
    tmp_path: Path, claude_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = claude_home / ".claude" / "settings.json"
    path.write_bytes(b'{"model": "caf\xe9"}')
    common = ["--home", str(tmp_path / "data"), "--claude-home", str(claude_home)]
    assert run_cli(*common, "--apply") == 1
    assert "is not UTF-8 text" in capsys.readouterr().out
    assert path.read_bytes() == b'{"model": "caf\xe9"}'


def test_symlinked_settings_are_written_through(tmp_path: Path, claude_home: Path) -> None:
    real = tmp_path / "dotfiles" / "claude-settings.json"
    real.parent.mkdir()
    original = write_foreign(real)
    link = claude_home / ".claude" / "settings.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("creating symlinks is not allowed here")
    backup = write_change(plan_apply(link, ours(tmp_path / "token")))
    assert link.is_symlink() and installed_mode(json.loads(real.read_text("utf-8"))) == "command"
    assert backup is not None and backup.read_bytes() == original
    assert not list(real.parent.glob(".*.relaybay.tmp"))


def test_failed_replace_leaves_no_temp_file(
    tmp_path: Path, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = claude_home / ".claude" / "settings.json"
    original = write_foreign(path)
    change = plan_apply(path, ours(tmp_path / "token"))

    def refuse(self, target):
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(PermissionError):
        write_change(change)
    monkeypatch.undo()
    assert path.read_bytes() == original
    assert not list(path.parent.glob(".*.relaybay.tmp"))


# --- wake-up hook (opt-in) -------------------------------------------------------


def test_wake_hook_is_opt_in_marked_and_removable(
    tmp_path: Path,
    claude_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "data"
    path = claude_home / ".claude" / "settings.json"
    write_foreign(path)
    common = ["--home", str(data), "--claude-home", str(claude_home)]

    assert run_cli(*common, "--apply") == 0
    applied = json.loads(path.read_text(encoding="utf-8"))
    assert not machine_setup.our_handlers(applied, "Stop", role="wake")  # default: off

    assert run_cli(*common) == 0 and "--wake" not in capsys.readouterr().out
    # This Claude Code (2.1.300) is older than the release verified to run asyncRewake.
    assert machine_setup.MIN_ASYNC_REWAKE_VERSION == (2, 1, 309)
    assert run_cli(*common, "--apply", "--wake") == 1
    assert "Add --force-wake" in capsys.readouterr().out
    assert json.loads(path.read_text(encoding="utf-8")) == applied
    with monkeypatch.context() as m:
        verified = Detection(claude_path="/usr/bin/claude", claude_version="2.1.309 (Claude Code)")
        m.setattr(cli, "detect", lambda **_: verified)
        assert run_cli(*common, "--wake") == 0
        assert "--apply --mode command --wake --expect" in capsys.readouterr().out
    assert run_cli(*common, "--wake", "--force-wake") == 0
    out = capsys.readouterr().out
    assert "verified with Claude Code 2.1.309" in out and "hold the end of every turn" in out
    assert "--apply --mode command --wake --force-wake" in out
    assert run_cli(*common, "--apply", "--wake", "--force-wake") == 0
    data_wake = json.loads(path.read_text(encoding="utf-8"))
    [wake] = machine_setup.our_handlers(data_wake, "Stop", role="wake")
    assert wake["asyncRewake"] is True and wake["timeout"] == machine_setup.WAKE_TIMEOUT
    words = shlex.split(wake["command"])
    assert words[-1] == "--wait" and words[words.index("--url") + 1].endswith(
        "/hooks/claude-code/wait"
    )
    # The normal hooks are untouched by it, the foreign Stop hook stays first.
    assert installed_mode(data_wake) == "command"
    assert data_wake["hooks"]["Stop"][0] == FOREIGN["hooks"]["Stop"][0]
    assert len(machine_setup.our_handlers(data_wake, "Stop")) == 1

    # Idempotent; a plain apply keeps it; --no-wake removes only it; --remove removes all.
    assert run_cli(*common, "--apply", "--wake", "--force-wake") == 0
    assert "already set up" in capsys.readouterr().out
    assert run_cli(*common, "--apply") == 0
    assert json.loads(path.read_text(encoding="utf-8")) == data_wake
    assert run_cli(*common, "--apply", "--no-wake") == 0
    assert json.loads(path.read_text(encoding="utf-8")) == applied
    assert run_cli(*common, "--apply", "--wake", "--force-wake") == 0
    checks = machine_setup.config_checks(
        base_url=BASE,
        token=None,
        paths=claude_paths(claude_home),
        run=lambda *a, **k: SimpleNamespace(returncode=0, stderr=""),
    )
    [wake_check] = [c for c in checks if c.name == "wake-up hook"]
    assert wake_check.detail.startswith("asyncRewake hook at Stop (opt-in, verified from")
    assert run_cli(*common, "--remove") == 0
    assert json.loads(path.read_text(encoding="utf-8")) == FOREIGN

    capsys.readouterr()
    assert run_cli(*common, "--apply", "--wake", "--force-wake", "--mode", "http") == 1
    assert "--wake needs --mode command" in capsys.readouterr().out


def test_wake_marker_is_exact(tmp_path: Path) -> None:
    wake = machine_setup.wake_handler(BASE, tmp_path / "token", python="/opt/py/bin/python")
    assert machine_setup.handler_role(wake) == "wake" and is_ours(wake)
    command = wake["command"]
    for foreign in (
        command.replace(" --wait", ""),  # the wait URL without --wait
        command + " --verbose",
        command.replace("/hooks/claude-code/wait", "/hooks/claude-code/waiting"),
        command.replace(" --wait", " --wait && rm -rf x"),
    ):
        assert machine_setup.handler_role({"type": "command", "command": foreign}) is None
    plain = hook_handler("command", BASE, tmp_path / "token", python="/opt/py/bin/python")
    assert machine_setup.handler_role(plain) == "hook"
    assert (
        machine_setup.handler_role({"type": "http", "url": BASE + "/hooks/claude-code/wait"})
        is None
    )
