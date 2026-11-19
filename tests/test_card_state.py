"""The session card's derived parts (design 0025): the state line, the Open menu and why an
owner comment has not arrived."""

from dataclasses import replace

import pytest

from relaybay.domain.card import (
    OpenItem,
    OpenMenu,
    card_state,
    open_menu,
    session_surface,
    undelivered_reason,
)
from relaybay.domain.models import Session, Task
from relaybay.domain.sessions import DESKTOP, TERMINAL, resumable_id, surface_of_entrypoint
from relaybay.domain.stages import Stage

OWNERS = {"owner", "me"}
CC_ID = "123e4567-e89b-12d3-a456-426614174000"


def task(**changes: object) -> Task:
    base = Task(
        id=1,
        project_id=1,
        key="demo-api-1",
        title="Add a health check",
        stage=Stage.DEV,
        stage_note=None,
        waiting_on=None,
        next_step=None,
        branch=None,
        doc_path=None,
        description=None,
        progress=None,
        session_id=None,
        priority=0,
        version=1,
        created_at="2026-11-17T10:00:00.000+00:00",
        updated_at="2026-11-17T10:00:00.000+00:00",
        closed_at=None,
    )
    return replace(base, **changes)


def session(**changes: object) -> Session:
    base = Session(
        id=7,
        name="demo-api",
        external_id=None,
        kind="hook",
        desktop_link=None,
        status="idle",
        last_seen_at="2026-11-17T10:00:00.000+00:00",
        created_at="2026-11-17T10:00:00.000+00:00",
        cwd="/work/demo-api",
        cc_session_id=CC_ID,
        cc_exact=True,
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("status", "key", "label"),
    [("working", "working", "Working"), ("idle", "idle", "Idle"), ("ended", "ended", "Ended")],
)
def test_bound_session_status_names_the_state(status: str, key: str, label: str) -> None:
    state = card_state(task(session_id=7, session_status=status), OWNERS)
    assert (state.key, state.label, state.reasons) == (key, label, ())
    assert state.live is (status != "ended")


def test_no_session_and_an_ended_last_binding() -> None:
    assert card_state(task(), OWNERS).label == "No session"
    unbound = task(last_session_status="ended")
    assert card_state(unbound, OWNERS).label == "Ended"
    # The last session was unbound by hand while it still worked: no session on the card now.
    assert card_state(task(last_session_status="working"), OWNERS).label == "No session"


def test_a_bound_session_without_hook_status_is_idle() -> None:
    state = card_state(task(session_id=7, session_status=None), OWNERS)
    assert (state.label, state.live) == ("Idle", True)


def test_waits_for_you_names_every_reason_in_order() -> None:
    t = task(
        stage=Stage.REVIEW,
        session_id=7,
        session_status="waiting",
        session_activity="Claude needs your permission to use Bash",
        open_questions=2,
        open_approvals=1,
        review_total=3,
        review_accepted=1,
        waiting_on=" Owner ",
    )
    state = card_state(t, OWNERS)
    assert state.label == "Waits for you"
    assert state.live
    assert state.reasons == (
        "2 open questions",
        "1 approval request",
        "review 1/3",
        "waiting on Owner",
        "the session asks for its user: Claude needs your permission to use Bash",
    )


def test_waiting_on_someone_else_or_a_done_card_does_not_wait_for_you() -> None:
    assert card_state(task(waiting_on="reviewer"), OWNERS).label == "No session"
    done = task(stage=Stage.DONE, waiting_on="owner", review_total=2, last_session_status="ended")
    assert card_state(done, OWNERS).label == "Ended"
    # A checklist outside review and test is not a review waiting.
    assert card_state(task(review_total=2), OWNERS).label == "No session"


def test_a_question_left_by_an_ended_session_still_waits_for_you() -> None:
    state = card_state(task(open_questions=1, last_session_status="ended"), OWNERS)
    assert (state.label, state.live) == ("Waits for you", False)


@pytest.mark.parametrize(
    ("value", "surface"),
    [
        ("claude-desktop", DESKTOP),
        ("claude-desktop-3p", DESKTOP),
        ("cli", TERMINAL),
        (" cli ", TERMINAL),
        ("sdk-py", None),
        ("claude-vscode", None),
        ("", None),
        (None, None),
    ],
)
def test_surface_of_entrypoint(value: str | None, surface: str | None) -> None:
    assert surface_of_entrypoint(value) == surface


def test_a_desktop_id_makes_a_desktop_session_whatever_was_recorded() -> None:
    assert session_surface(session(external_id="local_x", surface=TERMINAL)) == DESKTOP
    assert session_surface(session(surface=TERMINAL)) == TERMINAL
    assert session_surface(session()) is None


def test_resumable_id_takes_only_a_lowercase_uuid() -> None:
    assert resumable_id(CC_ID) == CC_ID
    for bad in (None, "", CC_ID.upper(), CC_ID + " ", f"{CC_ID};calc", "--help", "../x"):
        assert resumable_id(bad) is None


def test_no_session_has_no_open_menu() -> None:
    assert open_menu(None) is None


def test_in_desktop_opens_a_desktop_session_live_or_ended() -> None:
    link = "claude://claude.ai/epitaxy/local_x"
    for status in ("working", "ended"):
        menu = open_menu(session(external_id="local_x", desktop_link=link, status=status))
        assert menu is not None and menu.desktop == OpenItem(True, href=link)
    no_id = open_menu(session(surface=DESKTOP))
    assert no_id is not None
    assert no_id.desktop == OpenItem(note="its desktop id is not known yet")
    terminal = open_menu(session(surface=TERMINAL, status="ended"))
    assert terminal is not None
    assert terminal.desktop == OpenItem(
        note="started in a terminal: the desktop app cannot open it"
    )


@pytest.mark.parametrize(
    ("surface", "note"),
    [
        (DESKTOP, "running in the desktop app"),
        (TERMINAL, "running in a terminal"),
        (None, "still running"),
    ],
)
def test_in_terminal_is_disabled_for_any_live_session(surface: str | None, note: str) -> None:
    menu = open_menu(session(surface=surface, status="working"))
    assert menu is not None and menu.terminal == OpenItem(note=note)


@pytest.mark.parametrize("surface", [DESKTOP, TERMINAL, None])
def test_in_terminal_resumes_any_ended_session(surface: str | None) -> None:
    menu = open_menu(session(surface=surface, status="ended", start_cwd="/work"))
    assert menu is not None
    assert menu.terminal == OpenItem(True, note="resumes it in a new terminal in /work")
    no_id = open_menu(session(surface=surface, status="ended", cc_session_id="x"))
    assert no_id is not None
    assert no_id.terminal == OpenItem(note="no Claude Code session id to resume")
    no_folder = open_menu(session(surface=surface, status="ended", cwd=None))
    assert no_folder is not None and no_folder.terminal == OpenItem(note="its folder is not known")


def test_a_desktop_session_resumed_in_a_terminal_is_not_opened_in_the_desktop() -> None:
    link = "claude://claude.ai/epitaxy/local_x"
    resumed = open_menu(
        session(external_id="local_x", desktop_link=link, surface=TERMINAL, status="working")
    )
    running = OpenItem(note="running in a terminal")
    assert resumed == OpenMenu(running, running)
    ended = open_menu(
        session(external_id="local_x", desktop_link=link, surface=TERMINAL, status="ended")
    )
    assert ended is not None and ended.desktop == OpenItem(True, href=link)


def test_an_unknown_surface_follows_the_desktop_link() -> None:
    unknown = open_menu(session(status="ended"))
    assert unknown is not None
    assert unknown.desktop == OpenItem(
        note="not known whether it runs in the desktop app or a terminal"
    )
    linked = open_menu(session(status="ended", desktop_link="claude://x"))
    assert linked is not None and linked.desktop == OpenItem(True, href="claude://x")
    assert linked.terminal.enabled


def test_undelivered_reason() -> None:
    bound = task(session_id=7)
    assert undelivered_reason(task(), None, False) == "no session bound"
    assert undelivered_reason(task(last_session_status="ended"), None, False) == (
        "the session ended"
    )
    assert undelivered_reason(bound, session(status="ended"), False) == "the session ended"
    assert "hook_session" in (undelivered_reason(bound, session(cc_exact=False), False) or "")
    assert undelivered_reason(bound, session(status="idle"), False) == (
        "the session is idle and the wake-up hook is not waiting"
    )
    assert undelivered_reason(bound, session(status="idle"), True) is None
    for waker in (False, True):  # the wake-up hook wakes only an idle session
        assert undelivered_reason(bound, session(status="waiting"), waker) == (
            "the session waits for its user: the comment arrives with its next tool call"
        )
    assert undelivered_reason(bound, session(status="working"), False) is None
