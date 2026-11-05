"""desktop.toml: read and write, the data-folder precedence, external mode."""

from pathlib import Path

import pytest

from relaybay import cli
from relaybay.config import load_settings
from relaybay.desktop.settings import (
    DesktopSettings,
    external_token_path,
    load_desktop_settings,
    save_desktop_settings,
    settings_path,
    write_external_token,
)


def test_missing_file_means_defaults(tmp_path: Path) -> None:
    assert load_desktop_settings(tmp_path) == DesktopSettings()


def test_round_trip(tmp_path: Path) -> None:
    data = tmp_path / 'data "quoted" \\ folder'
    saved = DesktopSettings(
        data_dir=data, autostart=True, server_mode="external", server_url="https://board.lan/"
    )
    save_desktop_settings(tmp_path, saved)
    loaded = load_desktop_settings(tmp_path)
    assert loaded.data_dir == data
    assert loaded.autostart is True
    assert loaded.server_mode == "external"
    assert loaded.server_url == "https://board.lan"  # trailing slash dropped
    assert "token" not in settings_path(tmp_path).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('server_mode = "cloud"', "server_mode must be one of"),
        ('server_mode = "external"', "needs server_url"),
        ('server_url = "ftp://x"', "http:// or https://"),
        ("autostart = 1", "autostart must be true or false"),
        ('colour = "red"', "unknown key(s): colour"),
    ],
)
def test_bad_values_name_the_key(tmp_path: Path, text: str, message: str) -> None:
    settings_path(tmp_path).write_text(text + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_desktop_settings(tmp_path)


def test_data_dir_precedence(tmp_path: Path, desktop_config_dir: Path) -> None:
    from_file, from_env, from_flag = tmp_path / "file", tmp_path / "env", tmp_path / "flag"
    save_desktop_settings(desktop_config_dir, DesktopSettings(data_dir=from_file))
    env = {"RELAYBAY_HOME": str(from_env)}
    assert load_settings(home=from_flag, environ=env).home == from_flag.resolve()
    assert load_settings(environ=env).home == from_env.resolve()
    assert load_settings(environ={}).home == from_file.resolve()
    save_desktop_settings(desktop_config_dir, DesktopSettings())
    assert load_settings(environ={}).home != from_file.resolve()  # the per-user default


def test_config_dir_from_environment(tmp_path: Path) -> None:
    save_desktop_settings(tmp_path, DesktopSettings(data_dir=tmp_path / "x"))
    env = {"RELAYBAY_CONFIG_DIR": str(tmp_path)}
    assert load_settings(environ=env).home == (tmp_path / "x").resolve()


def test_external_mode_base_url_and_token(tmp_path: Path, desktop_config_dir: Path) -> None:
    save_desktop_settings(
        desktop_config_dir,
        DesktopSettings(server_mode="external", server_url="http://192.168.1.5:8765"),
    )
    settings = load_settings(home=tmp_path, environ={})
    assert settings.base_url == "http://192.168.1.5:8765"
    assert settings.token_path == external_token_path(desktop_config_dir)
    served = load_settings(home=tmp_path, environ={}, serving=True)
    assert served.base_url == "http://127.0.0.1:8765"
    assert served.token_path == tmp_path / "token"


def test_external_token_written_private(tmp_path: Path) -> None:
    path = write_external_token(tmp_path, "  abc  \n")
    assert path.read_text(encoding="utf-8") == "abc\n"
    with pytest.raises(ValueError):
        write_external_token(tmp_path, " ")


def test_token_command_in_external_mode(
    tmp_path: Path, desktop_config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    save_desktop_settings(
        desktop_config_dir, DesktopSettings(server_mode="external", server_url="http://b.lan:1")
    )
    assert cli.main(["token", "--home", str(tmp_path)]) == 2  # no token saved yet
    assert not external_token_path(desktop_config_dir).exists()  # never invented
    write_external_token(desktop_config_dir, "remote-token")
    assert cli.main(["token", "--home", str(tmp_path), "--rotate"]) == 2
    assert cli.main(["token", "--home", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "http://b.lan:1/login?token=remote-token" in out


def test_desktop_config_command(
    tmp_path: Path, desktop_config_dir: Path, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    from relaybay.desktop import autostart

    calls: list[str] = []

    class FakeAutostart:
        def enable(self, command: list[str]) -> None:
            calls.append("enable")

        def disable(self) -> None:
            calls.append("disable")

        def command(self) -> str | None:
            return "x" if calls and calls[-1] == "enable" else None

        def location(self) -> str:
            return "fake"

    monkeypatch.setattr(autostart, "Autostart", FakeAutostart)
    monkeypatch.delenv("RELAYBAY_HOME", raising=False)
    assert cli.main(["desktop", "config", "--home", str(tmp_path / "d")]) == 0
    assert load_desktop_settings(desktop_config_dir).data_dir == (tmp_path / "d").resolve()
    assert "(desktop.toml)" in capsys.readouterr().out

    assert cli.main(["desktop", "config", "--mode", "external"]) == 2  # no URL
    assert "needs --url" in capsys.readouterr().err
    token = tmp_path / "t.txt"
    token.write_text("tok\n", encoding="utf-8")
    args = ["desktop", "config", "--mode", "external", "--url", "http://h:1"]
    assert cli.main(args) == 2  # no token
    assert cli.main([*args, "--token-file", str(token)]) == 0
    assert external_token_path(desktop_config_dir).read_text(encoding="utf-8") == "tok\n"
    assert load_desktop_settings(desktop_config_dir).server_mode == "external"

    assert cli.main(["desktop", "config", "--autostart", "on"]) == 0
    assert "Autostart:     on (fake)" in capsys.readouterr().out
    assert load_desktop_settings(desktop_config_dir).autostart is True
    assert cli.main(["desktop", "config", "--autostart", "off", "--home", "-"]) == 0
    assert calls == ["enable", "disable"]
    current = load_desktop_settings(desktop_config_dir)
    assert current.autostart is False and current.data_dir is None


def test_desktop_without_extra(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    from relaybay.desktop import app

    monkeypatch.setattr(app, "extra_installed", lambda: False)
    assert cli.main(["desktop"]) == 2
    assert "install relaybay[desktop]" in capsys.readouterr().err


def test_settings_form_validates_and_saves(tmp_path: Path) -> None:
    from relaybay.desktop.settings_window import apply_form, page_html

    with pytest.raises(ValueError, match="needs its URL"):
        apply_form(tmp_path, {"server_mode": "external"})
    with pytest.raises(ValueError, match="needs its token"):
        apply_form(tmp_path, {"server_mode": "external", "server_url": "http://h:1"})
    before, after = apply_form(
        tmp_path,
        {"server_mode": "external", "server_url": "http://h:1/", "token": " t ", "autostart": True},
    )
    assert before == DesktopSettings()
    assert after.server_url == "http://h:1" and after.autostart
    assert external_token_path(tmp_path).read_text(encoding="utf-8") == "t\n"
    assert load_desktop_settings(tmp_path) == after
    html = page_html()
    assert "/*APP_CSS*/" not in html and "--accent" in html


def test_relative_data_dir_is_refused(tmp_path: Path) -> None:
    from relaybay.desktop.settings_window import apply_form

    settings_path(tmp_path).write_text('data_dir = "board-data"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="absolute"):
        load_desktop_settings(tmp_path)
    settings_path(tmp_path).unlink()
    with pytest.raises(ValueError, match="absolute"):
        apply_form(tmp_path, {"data_dir": "board-data"})
    home_relative = load_desktop_settings(tmp_path)  # nothing written
    assert home_relative == DesktopSettings()


def test_cleartext_warning() -> None:
    from relaybay.desktop.settings import cleartext_warning

    def external(url: str) -> DesktopSettings:
        return DesktopSettings(server_mode="external", server_url=url)

    assert "clear text" in (cleartext_warning(external("http://board.lan:8765")) or "")
    assert cleartext_warning(external("https://board.lan")) is None
    assert cleartext_warning(external("http://127.0.0.1:9000")) is None
    assert cleartext_warning(external("http://localhost:9000")) is None
    assert cleartext_warning(DesktopSettings(server_url="http://board.lan")) is None


def test_desktop_config_warns_about_clear_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token = tmp_path / "t.txt"
    token.write_text("tok\n", encoding="utf-8")
    args = ["desktop", "config", "--mode", "external", "--url", "http://board.lan:8765"]
    assert cli.main([*args, "--token-file", str(token)]) == 0
    assert "travels in clear text" in capsys.readouterr().err


def test_settings_form_warns_about_clear_text(tmp_path: Path) -> None:
    from relaybay.desktop.settings_window import SettingsApi

    restarts: list[int] = []

    class Controller:
        config_dir = tmp_path
        desktop = DesktopSettings()

        def set_autostart(self, on: bool) -> None:
            pass

        def restart_async(self) -> None:
            restarts.append(1)

    answer = SettingsApi(Controller()).save(
        {"server_mode": "external", "server_url": "http://board.lan:1", "token": "t"}
    )
    assert answer["ok"] and "clear text" in answer["message"] and restarts == [1]


def test_serve_survives_a_broken_or_newer_desktop_toml(
    tmp_path: Path, desktop_config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data = tmp_path / "data"
    settings_path(desktop_config_dir).write_text(
        f'data_dir = "{data.as_posix()}"\nagent_runtime = "local"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown key"):
        load_settings(environ={})  # the CLI names the problem
    assert load_settings(environ={}, serving=True).home == data.resolve()  # newer key skipped
    settings_path(desktop_config_dir).write_text("server_mode = [broken\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="relaybay.config"):
        served = load_settings(home=tmp_path, environ={}, serving=True)
    assert served.base_url == "http://127.0.0.1:8765" and "ignoring" in caplog.text


def test_desktop_config_overwrites_a_broken_file(
    tmp_path: Path, desktop_config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path(desktop_config_dir).write_text("server_mode = [broken\n", encoding="utf-8")
    assert cli.main(["desktop", "config"]) == 2
    assert "Fix the file" in capsys.readouterr().err
    assert cli.main(["desktop", "config", "--home", str(tmp_path / "d")]) == 0
    assert load_desktop_settings(desktop_config_dir).data_dir == (tmp_path / "d").resolve()


def test_reload_child_ignores_external_mode(tmp_path: Path, desktop_config_dir: Path, monkeypatch):
    from relaybay.app import create_app_from_env

    save_desktop_settings(
        desktop_config_dir, DesktopSettings(server_mode="external", server_url="http://b.lan:1")
    )
    write_external_token(desktop_config_dir, "REMOTE-TOKEN")
    monkeypatch.setenv("RELAYBAY_HOME", str(tmp_path))
    app = create_app_from_env()
    assert app.state.settings.token_path == tmp_path / "token"
    assert app.state.settings.base_url == "http://127.0.0.1:8765"


def test_setup_and_doctor_in_external_mode_without_token(
    tmp_path: Path, desktop_config_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from relaybay.doctor import _token_check

    save_desktop_settings(
        desktop_config_dir, DesktopSettings(server_mode="external", server_url="http://b.lan:1")
    )
    assert cli.main(["setup", "--home", str(tmp_path), "--claude-home", str(tmp_path)]) == 2
    assert "desktop config --token-file" in capsys.readouterr().err
    assert not external_token_path(desktop_config_dir).exists()
    check, token = _token_check(load_settings(home=tmp_path, environ={}))
    assert not check.ok and token is None
    assert "relaybay desktop config --token-file" in check.detail


def test_healthz_skips_the_proxy_only_for_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    from urllib.request import ProxyHandler

    from relaybay.desktop import server_thread

    seen: list[tuple] = []

    class Opener:
        def open(self, url: str, timeout: float):
            raise OSError("not reached")

    def fake_build_opener(*handlers):
        seen.append(handlers)
        return Opener()

    monkeypatch.setattr(server_thread, "build_opener", fake_build_opener)
    server_thread.healthz("http://127.0.0.1:8765")
    server_thread.healthz("https://board.example.lan")
    assert len(seen[0]) == 1 and isinstance(seen[0][0], ProxyHandler)
    assert seen[1] == ()
