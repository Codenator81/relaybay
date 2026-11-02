from pathlib import Path

import pytest

from relaybay.config import DEFAULT_PORT, load_settings


def test_defaults(tmp_path: Path) -> None:
    settings = load_settings(home=tmp_path, environ={})
    assert (settings.host, settings.port) == ("127.0.0.1", DEFAULT_PORT)
    assert settings.db_path == tmp_path.resolve() / "relaybay.sqlite"
    assert settings.label("dev") == "dev"
    assert {"127.0.0.1", "localhost", "[::1]"} <= set(settings.host_allow_list())


def test_precedence_file_env_flags(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'port = 9000\nallowed_hosts = ["board.lan"]\n[stages.labels]\ndone = "closed"\n',
        encoding="utf-8",
    )
    from_file = load_settings(home=tmp_path, environ={})
    assert from_file.port == 9000 and from_file.label("done") == "closed"
    assert "board.lan" in from_file.host_allow_list()

    env = {"RELAYBAY_PORT": "9100", "RELAYBAY_HOST": "0.0.0.0"}
    from_env = load_settings(home=tmp_path, environ=env)
    assert (from_env.port, from_env.host) == (9100, "0.0.0.0")
    assert from_env.base_url == "http://127.0.0.1:9100"

    from_flags = load_settings(home=tmp_path, environ=env, port=9200)
    assert from_flags.port == 9200


def test_home_from_env(tmp_path: Path) -> None:
    settings = load_settings(environ={"RELAYBAY_HOME": str(tmp_path / "data")})
    assert settings.home == (tmp_path / "data").resolve()


def test_unknown_stage_label_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('[stages.labels]\nreview = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="review"):
        load_settings(home=tmp_path, environ={})


def test_desktop_link_template(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).desktop_link_template == (
        "claude://claude.ai/epitaxy/{id}"
    )
    config = tmp_path / "config.toml"
    config.write_text('desktop_link_template = "claude://open/{id}"\n', encoding="utf-8")
    assert load_settings(home=tmp_path, environ={}).desktop_link_template == "claude://open/{id}"
    config.write_text('desktop_link_template = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="desktop_link_template"):
        load_settings(home=tmp_path, environ={})


def test_report_labels_and_hints(tmp_path: Path) -> None:
    defaults = load_settings(home=tmp_path, environ={})
    assert defaults.report_label("verified") == "Verified"
    assert defaults.report_hint("L") == "Large"
    (tmp_path / "config.toml").write_text(
        '[reports.labels]\nverified = "проверено"\n[reports.hints]\nM = "средняя"\n',
        encoding="utf-8",
    )
    custom = load_settings(home=tmp_path, environ={})
    assert custom.report_label("verified") == "проверено"
    assert custom.report_hint("M") == "средняя"
    assert custom.report_label("blocked") == "Blocked"
    (tmp_path / "config.toml").write_text('[reports.labels]\nshipped = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="shipped"):
        load_settings(home=tmp_path, environ={})


def test_done_window_days(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).done_window_days == 7
    config = tmp_path / "config.toml"
    config.write_text("done_window_days = 14\n", encoding="utf-8")
    assert load_settings(home=tmp_path, environ={}).done_window_days == 14
    env = {"RELAYBAY_DONE_WINDOW_DAYS": "3"}
    assert load_settings(home=tmp_path, environ=env).done_window_days == 3
    for bad in ("0", "2.5", "true", '"week"'):
        config.write_text(f"done_window_days = {bad}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="done_window_days"):
            load_settings(home=tmp_path, environ={})


def test_require_tracker_log(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).require_tracker_log is False
    config = tmp_path / "config.toml"
    config.write_text("require_tracker_log = true\n", encoding="utf-8")
    assert load_settings(home=tmp_path, environ={}).require_tracker_log is True
    env = {"RELAYBAY_REQUIRE_TRACKER_LOG": "false"}
    assert load_settings(home=tmp_path, environ=env).require_tracker_log is False
    env = {"RELAYBAY_REQUIRE_TRACKER_LOG": "1"}
    config.unlink()
    assert load_settings(home=tmp_path, environ=env).require_tracker_log is True
    for bad in ("1", '"maybe"'):
        config.write_text(f"require_tracker_log = {bad}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="require_tracker_log"):
            load_settings(home=tmp_path, environ={})
