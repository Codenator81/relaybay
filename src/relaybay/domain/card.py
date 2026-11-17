"""The session card (design 0025): its state line, its Open button and why an owner comment has
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


# --- the Open button --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenAction:
    """`kind`: "link" (the desktop link in `href`), "terminal" (resume in a new terminal window)
    or None (no button). `note` says in one line why the button is disabled or missing."""

    kind: str | None = None
    href: str | None = None
    disabled: bool = False
    note: str | None = None


def open_action(session: Session | None) -> OpenAction:
    """What Open does for the card's session (the bound one, or the ended one of the latest
    binding). A live terminal session is never opened: the board cannot find its window, and a
    second process on a live session is unsafe."""
    if session is None:
        return OpenAction()
    surface = session_surface(session)
    if surface == DESKTOP:
        if session.desktop_link:
            return OpenAction("link", href=session.desktop_link)
        return OpenAction(note="runs in the desktop app; its desktop id is not known yet")
    if surface == TERMINAL:
        if session.status != "ended":
            return OpenAction("terminal", disabled=True, note="running in a terminal")
        if resumable_id(session.cc_session_id) is None:
            return OpenAction(note="ended in a terminal; no Claude Code session id to resume")
        folder = session.start_cwd or session.cwd
        if not folder:
            return OpenAction(note="ended in a terminal; its folder is not known")
        return OpenAction("terminal", note=f"resumes it in a new terminal in {folder}")
    return OpenAction(note="not known whether it runs in the desktop app or a terminal")


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
