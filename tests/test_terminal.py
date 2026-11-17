"""Open an ended terminal session again from its card (design 0025): the argument lists per
platform, the refusals, the validation, the endpoint. No test opens a window."""

import shlex
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.app import create_app
from relaybay.config import Settings
from relaybay.domain.errors import ConflictError, NotFoundError
from relaybay.security import csrf_token
from relaybay.service import Service
from relaybay.terminal import Launch, TerminalLauncher, resume_launch
from tests.conftest import BASE, run_in_app
from tests.test_hooks import hook

CC_ID = "123e4567-e89b-12d3-a456-426614174000"
CLAUDE = "/opt/bin/claude"


def which_of(*found: str):
    paths = {name: f"/opt/bin/{name}" for name in found}
    return paths.get


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    path = tmp_path / "demo api"
    path.mkdir()
    return path


def test_windows_uses_windows_terminal(folder: Path) -> None:
    launch = resume_launch(CC_ID, str(folder), "win32", which_of("claude", "wt"))
    assert launch == Launch(
        ["/opt/bin/wt", "-d", str(folder), CLAUDE, "--resume", CC_ID], folder, False
    )


def test_windows_without_windows_terminal_opens_a_console_of_its_own(folder: Path) -> None:
    launch = resume_launch(CC_ID, str(folder), "win32", which_of("claude"))
    assert launch == Launch([CLAUDE, "--resume", CC_ID], folder, new_console=True)


def test_windows_terminal_is_skipped_for_a_folder_with_a_semicolon(tmp_path: Path) -> None:
    folder = tmp_path / "a;b"
    folder.mkdir()
    launch = resume_launch(CC_ID, str(folder), "win32", which_of("claude", "wt"))
    assert launch.argv == [CLAUDE, "--resume", CC_ID] and launch.new_console


def test_macos_quotes_the_folder_for_the_shell_and_for_applescript(tmp_path: Path) -> None:
    folder = tmp_path / "it's $x"  # Windows forbids a double quote in a name
    folder.mkdir()
    launch = resume_launch(CC_ID, str(folder), "darwin", which_of("claude", "osascript"))
    assert launch.argv[0] == "/opt/bin/osascript" and launch.argv[1] == "-e"
    script = launch.argv[2]
    assert script.startswith('tell application "Terminal" to do script "')
    # Unescaped for AppleScript, the line is the shell's: the folder one quoted word.
    line = script.removeprefix('tell application "Terminal" to do script "')[:-1]
    line = line.replace('\\"', '"').replace("\\\\", "\\")
    words = shlex.split(line)
    assert words == ["cd", str(folder), "&&", CLAUDE, "--resume", CC_ID]
    assert launch.argv[3:] == ["-e", 'tell application "Terminal" to activate']


def test_macos_without_osascript_is_refused(folder: Path) -> None:
    with pytest.raises(ConflictError, match="No terminal found"):
        resume_launch(CC_ID, str(folder), "darwin", which_of("claude"))


@pytest.mark.parametrize(
    ("found", "argv"),
    [
        (("x-terminal-emulator", "xterm"), ["/opt/bin/x-terminal-emulator", "-e"]),
        (("gnome-terminal", "xterm"), ["/opt/bin/gnome-terminal", "--working-directory={}", "--"]),
        (("konsole",), ["/opt/bin/konsole", "--workdir", "{}", "-e"]),
        (("xterm",), ["/opt/bin/xterm", "-e"]),
    ],
)
def test_linux_takes_the_first_terminal_found(
    folder: Path, found: tuple[str, ...], argv: list[str]
) -> None:
    launch = resume_launch(CC_ID, str(folder), "linux", which_of("claude", *found))
    expected = [part.replace("{}", str(folder)) for part in argv]
    assert launch.argv == [*expected, CLAUDE, "--resume", CC_ID]
    assert launch.cwd == folder and not launch.new_console


def test_linux_without_a_terminal_is_refused(folder: Path) -> None:
    with pytest.raises(ConflictError, match="No terminal found: none of x-terminal-emulator"):
        resume_launch(CC_ID, str(folder), "linux", which_of("claude"))


@pytest.mark.parametrize(
    "cc_id",
    [None, "", CC_ID.upper(), f"{CC_ID} --dangerously-skip-permissions", "--help", "../../x"],
)
def test_only_a_claude_session_id_is_resumed(folder: Path, cc_id: str | None) -> None:
    with pytest.raises(ConflictError, match="no Claude Code session id"):
        resume_launch(cc_id, str(folder), "linux", which_of("claude", "xterm"))


def test_the_folder_must_exist_and_claude_must_be_found(tmp_path: Path, folder: Path) -> None:
    which = which_of("claude", "xterm")
    with pytest.raises(ConflictError, match="does not exist"):
        resume_launch(CC_ID, str(tmp_path / "gone"), "linux", which)
    with pytest.raises(ConflictError, match="does not exist"):
        resume_launch(CC_ID, "relative/folder", "linux", which)
    with pytest.raises(ConflictError, match="not known"):
        resume_launch(CC_ID, None, "linux", which)
    with pytest.raises(ConflictError, match="not known"):
        resume_launch(CC_ID, f"{folder}\n", "linux", which)
    with pytest.raises(ConflictError, match="`claude` is not on"):
        resume_launch(CC_ID, str(folder), "linux", which_of("xterm"))


class Recorder:
    def __init__(self) -> None:
        self.started: list[Launch] = []

    def __call__(self, launch: Launch) -> None:
        self.started.append(launch)


def launcher(started: Recorder) -> TerminalLauncher:
    return TerminalLauncher(platform="linux", which=which_of("claude", "xterm"), start=started)


async def terminal_session(service: Service, folder: Path, *, ended: bool = True) -> int:
    start = hook("SessionStart", CC_ID, cwd=str(folder), relaybay_entrypoint="cli")
    await service.record_hook(start)
    if ended:
        await service.record_hook(hook("SessionEnd", CC_ID, cwd=str(folder)))
    [row] = [r.session for r in await service.list_sessions()]
    return row.id


async def test_service_opens_an_ended_terminal_session_and_audits_it(
    service: Service, folder: Path
) -> None:
    started = Recorder()
    service.terminal = launcher(started)
    session_id = await terminal_session(service, folder)
    await service.open_terminal(session_id, actor="owner")
    assert [s.argv for s in started.started] == [
        ["/opt/bin/xterm", "-e", CLAUDE, "--resume", CC_ID]
    ]
    [event] = [e for e in await service.audit(changes=True) if e.kind == "session.terminal_opened"]
    assert (event.actor, event.payload["cwd"], event.payload["program"]) == (
        "owner",
        str(folder),
        "xterm",
    )


async def test_service_refuses_a_live_or_unknown_session(service: Service, folder: Path) -> None:
    started = Recorder()
    service.terminal = launcher(started)
    live = await terminal_session(service, folder, ended=False)
    with pytest.raises(ConflictError, match="running in a terminal"):
        await service.open_terminal(live)
    with pytest.raises(NotFoundError):
        await service.open_terminal(live + 1000)
    # A session that never said where it runs is not opened in a terminal either.
    await service.record_hook(hook("SessionStart", "2f9d7a10-5b3c-4e8a-a1d2-77c3e4f5a6b7"))
    await service.record_hook(hook("SessionEnd", "2f9d7a10-5b3c-4e8a-a1d2-77c3e4f5a6b7"))
    rows = {r.session.cc_session_id: r.session for r in await service.list_sessions()}
    unknown = rows["2f9d7a10-5b3c-4e8a-a1d2-77c3e4f5a6b7"]
    with pytest.raises(ConflictError, match="does not run in a terminal"):
        await service.open_terminal(unknown.id)
    assert started.started == []
    assert not [e for e in await service.audit(changes=True) if e.kind == "session.terminal_opened"]


@pytest.fixture
def local(settings: Settings, token: str) -> Iterator[TestClient]:
    """A browser on the machine itself, signed in."""
    app = create_app(settings)
    origin = {"Origin": BASE}
    with TestClient(app, base_url=BASE, client=("127.0.0.1", 50000), headers=origin) as test_client:
        assert test_client.get(f"/login?token={token}", follow_redirects=False).status_code == 303
        yield test_client


def test_endpoint_opens_only_from_this_machine_with_the_board_cookie(
    local: TestClient, board: TestClient, token: str, folder: Path
) -> None:
    started = Recorder()
    for client in (local, board):
        client.app.state.service.terminal = launcher(started)
    session_id = run_in_app(local, terminal_session, local.app.state.service, folder)
    url = f"/sessions/{session_id}/open-terminal"
    form = {"csrf_token": csrf_token(token)}

    assert local.post(url, data={}).status_code == 403  # no CSRF token
    bearer = local.post(url, data=form, headers={"Authorization": f"Bearer {token}"})
    assert bearer.status_code == 403  # a script with the token is not the board
    assert local.get(url).status_code == 405

    opened = local.post(url, data=form)
    assert opened.status_code == 200 and "A terminal resumes" in opened.text
    assert len(started.started) == 1

    # The phone's board (any address but loopback) is refused with the reason shown on the card.
    board_session = run_in_app(board, terminal_session, board.app.state.service, folder)
    remote = board.post(
        f"/sessions/{board_session}/open-terminal", data=form, headers={"Origin": BASE}
    )
    assert remote.status_code == 409 and "only on the machine relaybay runs on" in remote.text
    assert len(started.started) == 1
    assert local.post("/sessions/9999/open-terminal", data=form).status_code == 404


def test_endpoint_refusal_is_a_409_with_the_reason(
    local: TestClient, token: str, folder: Path
) -> None:
    service: Service = local.app.state.service
    service.terminal = TerminalLauncher(
        platform="linux", which=which_of("claude"), start=Recorder()
    )
    session_id = run_in_app(local, terminal_session, service, folder)
    refused = local.post(
        f"/sessions/{session_id}/open-terminal", data={"csrf_token": csrf_token(token)}
    )
    assert refused.status_code == 409 and "No terminal found" in refused.text
