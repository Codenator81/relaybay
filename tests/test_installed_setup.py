"""`relaybay setup` and `doctor` from the installed (frozen) program, and Reconnect in its
Settings window. Every test works on temporary folders; the frozen build is simulated with
`sys.frozen` and `sys.executable`."""

import json
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from relaybay import __version__, cli, doctor, hook_client, install, machine_setup
from relaybay.config import load_settings
from relaybay.desktop.app import DesktopController
from relaybay.desktop.settings import (
    DesktopSettings,
    external_token_path,
    save_desktop_settings,
    write_external_token,
)
from relaybay.desktop.settings_window import SettingsApi
from relaybay.domain.hooks import HOOK_EVENTS, HOOK_PATH, WAKE_PATH
from relaybay.machine_setup import (
    Check,
    Detection,
    claude_paths,
    desired_groups,
    handler_role,
    hook_command,
    hook_handler,
    make_plan,
    plan_apply,
    reconnect_target,
    wake_handler,
    write_change,
)
from relaybay.machine_setup import (
    detect as real_detect,
)

BASE = "http://127.0.0.1:8765"
NEW = Detection(claude_path="/usr/local/bin/claude", claude_version="2.1.320 (Claude Code)")
# Installed places, not under the temp folder (that one is refused as unstable).
WINDOWS_EXE = "C:/Users/Jane Doe/AppData/Local/Programs/relaybay/relaybay.exe"
POSIX_EXE = "/opt/my apps/relaybay/relaybay"


@pytest.fixture(autouse=True)
def fake_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs the real `claude` binary."""
    for module in (machine_setup, cli, doctor):
        monkeypatch.setattr(module, "detect", lambda **_: NEW)


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    home = tmp_path / "claude-home"
    (home / ".claude").mkdir(parents=True)
    return home


def frozen_at(monkeypatch: pytest.MonkeyPatch, executable: str) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", executable)


def hook_name() -> str:
    return "relaybay-hook.exe" if sys.platform == "win32" else "relaybay-hook"


def commands(path: Path) -> dict[str, list[str]]:
    hooks = json.loads(path.read_text(encoding="utf-8"))["hooks"]
    return {
        event: [h["command"] for g in groups for h in g["hooks"] if "command" in h]
        for event, groups in hooks.items()
    }


# --- the frozen hook command ---------------------------------------------------


def test_setup_wording_names_the_forwarder_of_this_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = machine_setup.mode_reason("command")
    assert "this Python" in source
    frozen_at(monkeypatch, POSIX_EXE)
    frozen = machine_setup.mode_reason("command")
    assert "relaybay-hook" in frozen and "Python" not in frozen
    # An explicit interpreter starts that interpreter, also in a frozen build.
    assert machine_setup.mode_reason("command", "/usr/bin/python3") == source
    assert machine_setup.mode_reason("http") == machine_setup.MODE_REASONS["http"]


def test_setup_plan_prints_the_frozen_wording(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, claude_home: Path
) -> None:
    paths = machine_setup.claude_paths(claude_home)
    arguments = {
        "base_url": BASE,
        "token": "t",
        "token_path": tmp_path / "token",
        "paths": paths,
        "detection": NEW,
        "environ": {},
    }
    assert "this Python" in make_plan(**arguments).mode_reason
    frozen_at(monkeypatch, POSIX_EXE)
    assert "relaybay-hook" in make_plan(**arguments).mode_reason


def test_frozen_hook_command_on_windows_quotes_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    frozen_at(monkeypatch, WINDOWS_EXE)
    token = Path("C:/Users/Jane Doe/AppData/Local/relaybay/token")
    command = hook_command(BASE, token, platform="win32")
    hook = "C:/Users/Jane Doe/AppData/Local/Programs/relaybay/relaybay-hook.exe"
    assert command.startswith(f'"{hook}" --url {BASE}{HOOK_PATH} --token-file ')
    assert "-m relaybay.hook_client" not in command
    words = shlex.split(command)
    assert words[0] == hook
    assert machine_setup._command_role(command) == "hook"


def test_frozen_hook_command_on_posix_quotes_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    frozen_at(monkeypatch, POSIX_EXE)
    command = hook_command(BASE, Path("/home/jane/data/token"), platform="linux", wait=True)
    assert command.startswith(f"'/opt/my apps/relaybay/relaybay-hook' --url {BASE}{WAKE_PATH}")
    assert command.endswith(" --wait")
    assert shlex.split(command)[0] == "/opt/my apps/relaybay/relaybay-hook"
    assert machine_setup._command_role(command) == "wake"


def test_an_explicit_python_keeps_the_module_form(monkeypatch: pytest.MonkeyPatch) -> None:
    frozen_at(monkeypatch, POSIX_EXE)
    command = hook_command(BASE, Path("/tmp/token"), python="/usr/bin/python3", platform="linux")
    assert command.startswith("/usr/bin/python3 -I -m relaybay.hook_client --url ")


@pytest.mark.parametrize(
    "command,role",
    [
        (f"relaybay-hook --url {BASE}{HOOK_PATH} --token-file /t", "hook"),
        (f'"C:/P F/relaybay-hook.exe" --url {BASE}{HOOK_PATH} --token-file /t', "hook"),
        (f"/opt/relaybay/relaybay-hook --url {BASE}{WAKE_PATH} --token-file /t --wait", "wake"),
        (f"relaybay-hook --url {BASE}{HOOK_PATH} --token-file /t --extra", None),
        (f"relaybay-hook --token-file /t --url {BASE}{HOOK_PATH}", None),
        (f"relaybay-hooks --url {BASE}{HOOK_PATH} --token-file /t", None),
        (f"relaybay-hook --url {BASE}{HOOK_PATH} --token-file /t && rm -rf /", None),
    ],
)
def test_marker_of_the_frozen_hook(command: str, role: str | None) -> None:
    assert handler_role({"type": "command", "command": command}) == role


# --- re-apply over the entries of a source checkout -----------------------------


def test_reapply_replaces_dev_venv_entries_in_place(
    tmp_path: Path, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = claude_paths(claude_home)
    token = tmp_path / "data" / "token"
    dev_python = "/home/jane/relaybay-src/.venv/bin/python"
    foreign = {"type": "command", "command": "python ~/.claude/hooks/git-policy.py"}
    data = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [foreign]}]}}
    paths.settings.write_text(json.dumps(data), encoding="utf-8")
    old = plan_apply(
        paths.settings,
        desired_groups(hook_handler("command", BASE, token, python=dev_python)),
        wake=wake_handler(BASE, token, python=dev_python),
    )
    write_change(old)

    frozen_at(monkeypatch, WINDOWS_EXE if sys.platform == "win32" else POSIX_EXE)
    plan = make_plan(
        base_url=BASE, token="t", token_path=token, paths=paths, detection=NEW, environ={}
    )
    assert plan.error is None and plan.hooks is not None and plan.hooks.changed
    write_change(plan.hooks, expect=plan.hooks.plan_hash)

    found = commands(paths.settings)
    for event in HOOK_EVENTS:
        ours = [c for c in found[event] if handler_role({"type": "command", "command": c})]
        roles = {handler_role({"type": "command", "command": c}) for c in ours}
        assert len([c for c in ours if "--wait" not in c]) == 1, (event, ours)
        assert all(shlex.split(c)[0].endswith(hook_name()) for c in ours), ours
        assert roles <= {"hook", "wake"}
    assert "python ~/.claude/hooks/git-policy.py" in found["PreToolUse"]
    assert found["PreToolUse"][0] == "python ~/.claude/hooks/git-policy.py"  # order kept
    # The opt-in wake-up hook stays, now with the frozen forwarder.
    wake = [c for c in found["Stop"] if c.endswith("--wait")]
    assert len(wake) == 1 and shlex.split(wake[0])[0].endswith(hook_name())
    assert all(dev_python not in c for cs in found.values() for c in cs)

    again = make_plan(
        base_url=BASE, token="t", token_path=token, paths=paths, detection=NEW, environ={}
    )
    assert again.hooks is not None and not again.hooks.changed


# --- unstable locations ---------------------------------------------------------


@pytest.mark.parametrize(
    "executable",
    [
        "/Volumes/relaybay/relaybay.app/Contents/MacOS/relaybay-desktop",
        "/private/var/folders/x/AppTranslocation/1234/d/relaybay.app/Contents/MacOS/relaybay",
    ],
)
def test_macos_bundle_from_the_dmg_or_translocated_is_refused(executable: str) -> None:
    problem = install.location_problem(executable, platform="darwin")
    assert problem is not None and "move relaybay.app to /Applications first" in problem


def test_macos_bundle_in_applications_is_stable() -> None:
    executable = "/Applications/relaybay.app/Contents/MacOS/relaybay-desktop"
    assert install.location_problem(executable, platform="darwin") is None


@pytest.mark.parametrize(
    "executable,stable",
    [
        ("C:/Users/Jane/AppData/Local/Temp/build/relaybay/relaybay.exe", False),
        ("C:\\Users\\Jane\\Downloads\\relaybay\\relaybay.exe", False),
        ("C:/Users/Jane/AppData/Local/Programs/relaybay/relaybay.exe", True),
    ],
)
def test_windows_program_from_temp_or_downloads_is_refused(executable: str, stable: bool) -> None:
    problem = install.location_problem(
        executable,
        platform="win32",
        temp="C:\\Users\\Jane\\AppData\\Local\\Temp",
        home=Path("C:/Users/Jane"),
    )
    assert (problem is None) is stable
    if problem:
        assert "windows-x64-setup.exe" in problem


def test_not_frozen_is_always_stable() -> None:
    assert install.location_problem() is None
    assert install.stable_location()


def test_setup_refuses_an_unstable_location(
    tmp_path: Path, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen_at(monkeypatch, str(tmp_path / "unpacked" / "relaybay"))  # under the temp folder
    paths = claude_paths(claude_home)
    plan = make_plan(
        base_url=BASE,
        token="t",
        token_path=tmp_path / "token",
        paths=paths,
        detection=NEW,
        environ={},
    )
    assert plan.hooks is None and plan.error and "temporary place" in plan.error
    assert not paths.settings.exists()
    # Removing relaybay's hooks is always allowed.
    removal = make_plan(
        base_url=BASE,
        token="t",
        token_path=tmp_path / "token",
        paths=paths,
        detection=NEW,
        remove=True,
        environ={},
    )
    assert removal.error is None


# --- relaybay-hook --version ----------------------------------------------------


def test_hook_client_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert hook_client.main(["--version"]) == 0
    assert capsys.readouterr().out == f"relaybay-hook {__version__}\n"


def test_hook_client_version_in_a_process_reads_no_stdin() -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-m", "relaybay.hook_client", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"relaybay-hook {__version__}"


def fake_run(stdout: str, code: int = 0):
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_interpreter_check_of_relaybay_hook_compares_the_version() -> None:
    command = f"'/opt/relaybay/relaybay-hook' --url {BASE}{HOOK_PATH} --token-file /t"
    run = fake_run(f"relaybay-hook {__version__}\n")
    check = machine_setup._interpreter_check(command, run)
    assert check.ok and run.calls == [["/opt/relaybay/relaybay-hook", "--version"]]
    stale = machine_setup._interpreter_check(command, fake_run("relaybay-hook 0.0.9\n"))
    assert not stale.ok and "0.0.9" in stale.detail and "another install" in stale.detail
    broken = machine_setup._interpreter_check(command, fake_run("", code=1))
    assert not broken.ok


# --- doctor ---------------------------------------------------------------------


def doctor_items(tmp_path: Path, claude_home: Path, run=None) -> dict[str, Check]:
    settings = load_settings(home=tmp_path / "data", claude_home=claude_home, environ={})
    run = run or fake_run(f"relaybay-hook {__version__}\n")
    checks = doctor.doctor_checks(settings, environ={}, run=run)
    return {c.name: c for c in checks}


def test_doctor_items_from_a_source_checkout(tmp_path: Path, claude_home: Path) -> None:
    items = doctor_items(tmp_path, claude_home)
    kind = install.install_kind()
    assert items["relaybay"].detail == f"{__version__} ({kind}: {sys.executable})"
    assert items["install"].ok and "stable location" in items["install"].detail
    assert items["server mode"].detail.startswith("embedded (http://127.0.0.1:")
    assert items["data folder source"].detail == "--home"
    assert items["hook executable"].ok
    assert "not a frozen build" in items["hook executable"].detail
    assert "start at login" in items["desktop"].detail
    assert "desktop.toml" in items["desktop"].detail


@pytest.mark.parametrize(
    "platform,kind",
    [("win32", "desktop-windows"), ("darwin", "desktop-macos"), ("linux", "desktop-deb")],
)
def test_doctor_items_of_the_installed_program(
    tmp_path: Path,
    claude_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    kind: str,
) -> None:
    if platform == "win32" and sys.platform != "win32":
        pytest.skip("the standard library cannot pretend to be Windows here (ssl, shutil)")
    folder = tmp_path / "Programs" / "relaybay"
    folder.mkdir(parents=True)
    exe = folder / ("relaybay.exe" if platform == "win32" else "relaybay")
    name = "relaybay-hook.exe" if platform == "win32" else "relaybay-hook"
    (folder / name).write_bytes(b"")
    frozen_at(monkeypatch, str(exe))
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(install, "location_problem", lambda: None)
    items = doctor_items(tmp_path, claude_home)
    assert items["relaybay"].detail == f"{__version__} ({kind}: {exe})"
    assert items["install"].ok and items["install"].detail.startswith(f"{kind} at {folder}")
    hook = items["hook executable"]
    assert hook.ok and str(folder / name) in hook.detail and "same version" in hook.detail

    stale = doctor_items(tmp_path, claude_home, run=fake_run("relaybay-hook 0.0.1\n"))
    assert not stale["hook executable"].ok


def test_doctor_flags_an_unstable_install_and_a_missing_hook(
    tmp_path: Path, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen_at(monkeypatch, str(tmp_path / "unpacked" / "relaybay"))
    items = doctor_items(tmp_path, claude_home)
    assert not items["install"].ok and "temporary place" in items["install"].detail
    assert not items["hook executable"].ok and "missing" in items["hook executable"].detail


def test_doctor_data_folder_source_and_docker(
    tmp_path: Path,
    claude_home: Path,
    desktop_config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    save_desktop_settings(desktop_config_dir, DesktopSettings(data_dir=tmp_path / "board"))
    monkeypatch.setenv("RELAYBAY_DISTRIBUTION", "docker")
    settings = load_settings(claude_home=claude_home)
    items = {c.name: c for c in doctor.doctor_checks(settings, environ={}, run=fake_run(""))}
    assert items["data folder source"].detail == "data_dir in desktop.toml"
    assert "(docker: " in items["relaybay"].detail
    env = load_settings(claude_home=claude_home, environ={"RELAYBAY_HOME": str(tmp_path / "e")})
    assert env.home_source == "environment"


# --- external mode ----------------------------------------------------------------


class NoNetwork:
    def open(self, request: object, timeout: float) -> None:
        raise URLError("no network in tests")


def external(desktop_config_dir: Path, url: str = "https://board.example.lan") -> Path:
    save_desktop_settings(
        desktop_config_dir, DesktopSettings(server_mode="external", server_url=url)
    )
    write_external_token(desktop_config_dir, "server-token")
    return external_token_path(desktop_config_dir)


def test_external_mode_hooks_point_at_the_url_and_the_token_file(
    tmp_path: Path,
    claude_home: Path,
    desktop_config_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    token_file = external(desktop_config_dir)
    common = ["setup", "--home", str(tmp_path / "data"), "--claude-home", str(claude_home)]
    assert cli.main([*common, "--apply"]) == 0
    out = capsys.readouterr().out
    assert (
        "claude mcp add --transport http --scope user relaybay https://board.example.lan/mcp"
        in (out)
    )
    found = commands(claude_paths(claude_home).settings)
    for event in HOOK_EVENTS:
        (command,) = found[event]
        words = shlex.split(command)
        assert words[words.index("--url") + 1] == f"https://board.example.lan{HOOK_PATH}"
        assert Path(words[words.index("--token-file") + 1]) == token_file.resolve()
    assert not (tmp_path / "data" / "token").exists()  # the local token is never made
    assert not (claude_home / ".claude.json").exists()  # MCP is only printed

    items = {
        c.name: c
        for c in doctor.doctor_checks(
            load_settings(home=tmp_path / "data", claude_home=claude_home),
            environ={},
            run=fake_run(""),
            opener=NoNetwork(),
        )
    }
    assert items["server mode"].detail == "external https://board.example.lan (desktop.toml)"


# --- the program: start-up check and Reconnect ------------------------------------


@pytest.fixture
def controller(tmp_path: Path, claude_home: Path, desktop_config_dir: Path) -> DesktopController:
    ctl = DesktopController(desktop_config_dir)
    ctl.settings = load_settings(
        home=tmp_path / "data", claude_home=claude_home, environ={}, serving=True
    )
    ctl.notices = []  # type: ignore[attr-defined]
    ctl.notify = ctl.notices.append  # type: ignore[attr-defined,method-assign]
    return ctl


def write_dev_hooks(controller: DesktopController, python: str) -> Path:
    assert controller.settings is not None
    paths = claude_paths(controller.settings.claude_home)
    handler = hook_handler("command", BASE, controller.settings.token_path, python=python)
    write_change(plan_apply(paths.settings, desired_groups(handler)))
    return paths.settings


def test_start_up_check_points_to_reconnect_and_writes_nothing(
    controller: DesktopController, monkeypatch: pytest.MonkeyPatch
) -> None:
    dev_python = Path(sys.executable).as_posix()
    path = write_dev_hooks(controller, dev_python)
    before = path.read_bytes()
    controller.check_hooks()
    assert controller.notices == []  # type: ignore[attr-defined]  # a source run: it works

    frozen_at(monkeypatch, WINDOWS_EXE if sys.platform == "win32" else POSIX_EXE)
    controller.check_hooks()
    assert controller.notices == [  # type: ignore[attr-defined]
        f"Claude Code hooks point to {dev_python}: open Settings -> Reconnect"
    ]
    assert path.read_bytes() == before


def test_reconnect_target_for_a_missing_interpreter(tmp_path: Path, claude_home: Path) -> None:
    paths = claude_paths(claude_home)
    gone = (tmp_path / "gone" / "python").as_posix()
    handler = hook_handler("command", BASE, tmp_path / "token", python=gone)
    write_change(plan_apply(paths.settings, desired_groups(handler)))
    failed = [Check(False, "command hook runs", "missing")]
    assert reconnect_target(failed, paths, BASE, None) == gone
    assert reconnect_target([], paths, BASE, None) is None


def test_reconnect_plan_diff_and_apply(
    controller: DesktopController, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_dev_hooks(controller, "/home/jane/relaybay-src/.venv/bin/python")
    frozen_at(monkeypatch, WINDOWS_EXE if sys.platform == "win32" else POSIX_EXE)
    api = SettingsApi(controller)

    plan = api.reconnect_plan()
    assert plan["ok"] and plan["hash"]
    assert "relaybay-src/.venv/bin/python" in plan["text"] and hook_name() in plan["text"]
    assert f"--expect {plan['hash']}" in plan["text"]  # the same text as `relaybay setup`
    unchanged = path.read_bytes()
    assert api.reconnect_apply("0000000000000000")["ok"] is False  # not the plan shown
    assert path.read_bytes() == unchanged

    result = api.reconnect_apply(plan["hash"])
    assert result["ok"] and result["message"].startswith(f"Wrote {path}")
    assert "(backup: settings.json.relaybay-backup-" in result["message"]
    found = commands(path)
    assert all(shlex.split(c)[0].endswith(hook_name()) for e in HOOK_EVENTS for c in found[e])

    again = api.reconnect_plan()
    assert again["ok"] and again["hash"] == "" and "already set up" in again["message"]


def test_reconnect_refused_from_an_unstable_location(
    controller: DesktopController, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen_at(monkeypatch, str(tmp_path / "unpacked" / "relaybay"))
    plan = SettingsApi(controller).reconnect_plan()
    assert plan["ok"] is False and plan["hash"] == "" and "temporary place" in plan["message"]


# --- review fixes -------------------------------------------------------------------


def recording_run(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen: list[dict] = []

    def run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        seen.append({"argv": argv, **kwargs})
        return SimpleNamespace(returncode=0, stdout="2.1.320 (Claude Code)\n", stderr="")

    monkeypatch.setattr(machine_setup.subprocess, "run", run)
    return seen


@pytest.mark.parametrize("folder", ["/scratch/tmp", "/Users/demo/Downloads"])
def test_a_macos_bundle_in_temp_or_downloads_is_unstable(folder: str) -> None:
    problem = install.location_problem(
        f"{folder}/relaybay.app/Contents/MacOS/relaybay",
        platform="darwin",
        temp="/scratch/tmp",
        home=Path("/Users/demo"),
    )
    assert problem is not None
    assert "temporary place" in problem
    assert "/Applications" in problem


@pytest.mark.skipif(sys.platform != "win32", reason="the flag exists on Windows only")
def test_children_get_no_console_window_on_windows(
    tmp_path: Path, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = recording_run(monkeypatch)
    monkeypatch.setattr(sys, "platform", "win32")
    real_detect(which=lambda _: "C:/bin/claude.exe", home=tmp_path)
    paths = claude_paths(claude_home)
    handler = hook_handler("command", BASE, tmp_path / "token", python="C:/py/python.exe")
    write_change(plan_apply(paths.settings, desired_groups(handler)))
    machine_setup.config_checks(base_url=BASE, token=None, paths=paths)
    frozen = f"/opt/relaybay/relaybay-hook --url {BASE}{HOOK_PATH} --token-file /t"
    machine_setup._interpreter_check(frozen, machine_setup.run_quiet)
    argv0 = [c["argv"][0] for c in seen]
    assert argv0 == ["C:/bin/claude.exe", "C:/py/python.exe", "/opt/relaybay/relaybay-hook"]
    assert all(c["creationflags"] == machine_setup.CREATE_NO_WINDOW for c in seen)


def test_children_keep_default_flags_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = recording_run(monkeypatch)
    monkeypatch.setattr(sys, "platform", "linux")
    machine_setup.run_quiet(["claude", "--version"], capture_output=True)
    assert "creationflags" not in seen[0]


@pytest.mark.skipif(sys.platform != "win32", reason="8.3 short names exist on Windows only")
def test_temp_folder_given_by_its_short_name_still_counts(tmp_path: Path) -> None:
    import ctypes

    folder = tmp_path / "a long folder name"
    folder.mkdir()
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(folder), buffer, 1024)
    if buffer.value.lower() == str(folder).lower():
        pytest.skip("8.3 names are off on this volume")
    exe = folder / "relaybay" / "relaybay.exe"
    problem = install.location_problem(
        str(exe), platform="win32", temp=buffer.value, home=Path("C:/nowhere")
    )
    assert problem is not None and "temporary place" in problem


def test_windows_path_with_dollar_or_backtick_is_refused() -> None:
    for bad in ("C:/Users/a$b/data/token", "C:/Users/a`b/data/token"):
        with pytest.raises(machine_setup.SetupError, match="backtick"):
            hook_command(BASE, Path(bad), python="C:/py/python.exe", platform="win32")
    # POSIX single quotes keep them literal.
    command = hook_command(
        BASE, Path("/home/a$b/token"), python="/usr/bin/python3", platform="linux"
    )
    assert "'" in command


@pytest.mark.skipif(sys.platform != "win32", reason="the plan uses this OS's quoting")
def test_windows_plan_with_a_dollar_path_is_an_error(tmp_path: Path, claude_home: Path) -> None:
    paths = claude_paths(claude_home)
    token = tmp_path / "data$x" / "token"
    plan = make_plan(
        base_url=BASE, token="t", token_path=token, paths=paths, detection=NEW, environ={}
    )
    assert plan.hooks is None and plan.error and "backtick" in plan.error
    assert not paths.settings.exists()


def test_doctor_survives_a_broken_launch_agent(
    tmp_path: Path, claude_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import plistlib

    from relaybay.desktop import autostart

    def broken(self: object) -> bool:
        raise plistlib.InvalidFileException()

    monkeypatch.setattr(autostart.Autostart, "is_enabled", broken)
    item = doctor_items(tmp_path, claude_home)["desktop"]
    assert item.ok and "start at login unknown" in item.detail
    monkeypatch.setattr(autostart.Autostart, "is_enabled", lambda self: {}["ProgramArguments"])
    assert "unknown" in doctor_items(tmp_path, claude_home)["desktop"].detail


def test_settings_apply_is_disabled_before_the_call() -> None:
    from relaybay.desktop.settings_window import page_html

    html = page_html()
    handler = html[html.index('$("apply").addEventListener') :]
    assert handler.index('$("apply").disabled = true') < handler.index("reconnect_apply(")
