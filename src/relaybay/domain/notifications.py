"""What the tray tells the owner: which events become a notice, and in what words."""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

from relaybay.domain.models import Event
from relaybay.domain.stages import ACCEPTANCE_STAGES

# Kind -> how a summary counts it (one, several); `server` comes from the program's watchdog.
LABELS = {
    "waiting": ("waiting", "waiting"),
    "review": ("review", "reviews"),
    "reply": ("reply", "replies"),
    "answer": ("answer", "answers"),
    "permission": ("permission request", "permission requests"),
    "brake": ("brake", "brakes"),
    "budget": ("budget notice", "budget notices"),
    "server": ("server notice", "server notices"),
}
KINDS = tuple(LABELS)
DEFAULT_KINDS = ("waiting", "review", "reply", "answer", "brake", "budget", "server")
# The events `classify` may answer for: the API reads only these.
EVENT_KINDS = (
    "task.updated",
    "task.stage_changed",
    "comment.added",
    "intake.answer_received",
    "session.status_changed",
    "brake.set",
    "budget.warned",
    "budget.exceeded",
)
BOARD_ACTORS = ("board", "owner")  # who writes for the signed-in owner on the board
SESSIONS_URL = "/sessions"
USAGE_URL = "/usage"


@dataclass(frozen=True, slots=True)
class Notice:
    kind: str
    title: str
    text: str
    url: str
    key: str  # what repeats collapse on: the task key (`permission`: the session name)


def _task_url(project: str, key: str, tab: str | None = None) -> str:
    url = f"/p/{quote(project, safe='')}?task={quote(key, safe='')}"
    return f"{url}&tab={tab}" if tab else url


def classify(
    event: Event, owner_names: set[str], titles: Mapping[int, str] | None = None
) -> Notice | None:
    """The notice for `event`, or None. `owner_names` are casefolded as the Inbox has them;
    `titles` gives the task's title by id where the event does not carry it. The owner's own
    writes are not news to him."""
    if event.actor in BOARD_ACTORS or event.actor.strip().casefold() in owner_names:
        return None
    p = event.payload
    if event.kind == "session.status_changed":
        session = p.get("session")
        if p.get("status") != "waiting" or "previous" not in p or not session:
            return None
        return Notice(
            "permission", f"{session} is waiting", p.get("activity") or "", SESSIONS_URL, session
        )
    if event.kind == "brake.set":
        # A budget's own brake is told by its `budget.exceeded`, one thing is one notice.
        if p.get("source") == "budget":
            return None
        target = p.get("target") or "everything"
        return Notice(
            "brake", f"Brake on {target}", p.get("reason") or "", SESSIONS_URL, f"brake:{target}"
        )
    if event.kind in ("budget.warned", "budget.exceeded"):
        target = p.get("target") or "everything"
        used, limit = p.get("used"), p.get("max_tokens")
        if event.kind == "budget.warned":
            title = f"Budget nearly spent: {target}"
        elif p.get("action") == "stop":
            title = f"Budget spent, agents stopped: {target}"
        else:
            title = f"Budget spent: {target}"
        return Notice(
            "budget", title, f"{used} of {limit} tokens", USAGE_URL, f"budget:{target}:{event.kind}"
        )
    key, project = p.get("key"), p.get("project")
    if not key or not project:
        return None
    title = (titles or {}).get(event.task_id or 0, "")
    if event.kind == "task.updated":
        waiting_on = (p.get("changes") or {}).get("waiting_on")
        if not waiting_on or str(waiting_on).strip().casefold() not in owner_names:
            return None
        return Notice("waiting", f"Waiting on you: {key}", title, _task_url(project, key), key)
    if event.kind == "task.stage_changed":
        if p.get("to") not in ACCEPTANCE_STAGES:
            return None
        return Notice(
            "review",
            f"Waiting for your acceptance: {key}",
            p.get("title") or title,
            _task_url(project, key, "review"),
            key,
        )
    if event.kind == "comment.added":
        if p.get("author_kind") != "agent":
            return None
        return Notice(
            "reply",
            f"Reply on {key} from {event.actor}",
            p.get("excerpt") or title,
            _task_url(project, key),
            key,
        )
    if event.kind == "intake.answer_received":
        return Notice(
            "answer",
            f"Answer received: {key}",
            f"From {p.get('source') or 'intake'}: {title}".removesuffix(": "),
            _task_url(project, key),
            key,
        )
    return None
