from pathlib import Path

import pytest

from relaybay import cli
from relaybay.cli import main
from relaybay.security import token_id


@pytest.fixture(autouse=True)
def no_running_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "running_token_id", lambda settings: None)


def test_token_prints_token_and_mcp_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["token", "--home", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    token = (tmp_path / "token").read_text(encoding="utf-8").strip()
    assert out.splitlines()[0] == token
    assert (
        "claude mcp add --transport http --scope user relaybay http://127.0.0.1:8765/mcp"
        f' --header "Authorization: Bearer {token}"'
    ) in out

    assert main(["token", "--home", str(tmp_path), "--rotate"]) == 0
    rotated = (tmp_path / "token").read_text(encoding="utf-8").strip()
    assert rotated != token and rotated in capsys.readouterr().out


def test_token_shows_data_folder_and_warns_on_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    main(["token", "--home", str(tmp_path)])
    capsys.readouterr()
    same = token_id((tmp_path / "token").read_text(encoding="utf-8").strip())
    monkeypatch.setattr(cli, "running_token_id", lambda settings: same)
    assert main(["token", "--home", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert f"Data folder: {tmp_path.resolve()}" in captured.out
    assert "WARNING" not in captured.err

    monkeypatch.setattr(cli, "running_token_id", lambda settings: "0123456789ab")
    assert main(["token", "--home", str(tmp_path)]) == 0
    assert "uses a different token" in capsys.readouterr().err
