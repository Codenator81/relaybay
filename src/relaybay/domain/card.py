"""The session card (design 0025): its state line, its Open menu and why an owner comment has
not arrived, all derived from what the board already holds."""

from dataclasses import dataclass

from relaybay.domain.models import Session, Task
from relaybay.domain.sessions import DESKTOP, TERMINAL, resumable_id
from relaybay.domain.stages import ACCEPTANCE_STAGES, Stage


def session_surface(session: Session) -> str | None:
    """A session with a desktop id runs in the desktop app, whatever else was recorded."""
    return DESKTOP if session.external_id else session.surface


# --- the card's state line (design 0025) ----------------------------------------------------

WORKING = "working"
IDLE = "idle"
WAITS = "waits"
ENDED = "ended"
NO_SESSION = "none"
STATE_LABELS = {
    WORKING: "Working",
    IDLE: "Idle",
    WAITS: "Waits for you",
    ENDED: "Ended",
    NO_SESSION: "No session",
}


@dataclass(frozen=True, slots=True)
class CardState:
    key: str  # one of STATE_LABELS
    reasons: tuple[str, ...] = ()  # why it waits for you, in the order of the derivation
    live: bool = False  # a bound session that has not ended (it may still wait for the owner)

    @property
    def label(self) -> str:
        return STATE_LABELS[self.key]


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def card_state(task: Task, owner_names: set[str]) -> CardState:
    """The card's state, derived and never set by hand. Waits for you: an open question or
    approval request, a review in an acceptance stage, `waiting_on` naming the owner
    (`owner_names` casefolded) or a bound session that asks for its user; else the bound
    session's hook status, Ended when the session of the latest binding has ended, else No
    session. A bound session without a hook status counts as idle."""
    is_open = task.stage is not Stage.DONE
    bound = task.session_id is not None
    status = task.session_status if bound else None
    reasons: list[str] = []
    if task.open_questions:
        reasons.append(_plural(task.open_questions, "open question"))
    if task.open_approvals:
        reasons.append(_plural(task.open_approvals, "approval request"))
    if is_open and task.stage in ACCEPTANCE_STAGES and task.review_total:
        reasons.append(f"review {task.review_accepted}/{task.review_total}")
    waiting_on = (task.waiting_on or "").strip()
    if is_open and waiting_on.casefold() in owner_names:
        reasons.append(f"waiting on {waiting_on}")
    if status == "waiting":
        reasons.append(f"the session asks for its user: {task.session_activity or 'needs you'}")
    live = bound and status != "ended"
    if reasons:
        return CardState(WAITS, tuple(reasons), live)
    if status == "working":
        return CardState(WORKING, live=True)
    if live:
        return CardState(IDLE, live=True)
    if (status if bound else task.last_session_status) == "ended":
        return CardState(ENDED)
    return CardState(NO_SESSION)


# --- the Open menu ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenItem:
    """One item of the Open menu: `href` is the desktop link of "In desktop"; `note` says why a
    disabled item is disabled, or what an enabled "In terminal" does."""

    enabled: bool = False
    href: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class OpenMenu:
    desktop: OpenItem  # the session's desktop link
    terminal: OpenItem  # `claude --resume` in a new terminal window in the session's folder


RUNNING_IN = {DESKTOP: "running in the desktop app", TERMINAL: "running in a terminal"}


def _desktop_item(session: Session, surface: str | None) -> OpenItem:
    if session.desktop_link and surface != TERMINAL:
        return OpenItem(True, href=session.desktop_link)
    if surface == DESKTOP:
        return OpenItem(note="its desktop id is not known yet")
    if surface == TERMINAL:
        return OpenItem(note="started in a terminal: the desktop app cannot open it")
    return OpenItem(note="not known whether it runs in the desktop app or a terminal")


def _terminal_item(session: Session, surface: str | None) -> OpenItem:
    if session.status != "ended":
        # A second process on a live session is unsafe, wherever it runs.
        return OpenItem(note=RUNNING_IN.get(surface, "still running"))
    if resumable_id(session.cc_session_id) is None:
        return OpenItem(note="no Claude Code session id to resume")
    folder = session.start_cwd or session.cwd
    if not folder:
        return OpenItem(note="its folder is not known")
    return OpenItem(True, note=f"resumes it in a new terminal in {folder}")


def open_menu(session: Session | None) -> OpenMenu | None:
    """The card's Open menu for its session (the bound one, or the ended one of the latest
    binding); None without a session. "In desktop" opens a desktop session, live or ended;
    "In terminal" resumes any ended session that has a Claude Code id and a folder."""
    if session is None:
        return None
    surface = session_surface(session)
    if surface == DESKTOP and session.surface == TERMINAL and session.status != "ended":
        # A desktop session resumed in a terminal (In terminal): the desktop app must not open
        # it next to that process.
        running = OpenItem(note=RUNNING_IN[TERMINAL])
        return OpenMenu(running, running)
    return OpenMenu(_desktop_item(session, surface), _terminal_item(session, surface))


# --- why an owner comment has not arrived -----------------------------------------------------


def undelivered_reason(task: Task, session: Session | None, wake_hook: bool) -> str | None:
    """Why the task's undelivered owner comments wait, or None when they go out with the bound
    session's next prompt or tool call. `session` is the bound one; `wake_hook` says its
    wake-up hook is waiting for comments now."""
    if session is None or task.session_id is None:
        return "the session ended" if task.last_session_status == "ended" else "no session bound"
    if session.status == "ended":
        return "the session ended"
    if not session.cc_exact:
        return "the session has not linked its hooks yet (hook_session on its writes)"
    if session.status == "waiting":
        return "the session waits for its user: the comment arrives with its next tool call"
    if session.status == "idle" and not wake_hook:
        return "the session is idle and the wake-up hook is not waiting"
    return None
