"""The backlog: findings of sessions and reviews, ideas from intake and the owner's notes. A
backlog item is not a task and not a stage; "To plan" on the board turns it into a task."""

import re
from dataclasses import dataclass

STATUSES = ("new", "later", "planned", "declined")
ORIGINS = ("session", "review", "intake", "owner")
# Statuses a coordinator may set over MCP: `planned` only comes from "To plan" on the board.
MCP_STATUSES = ("new", "later", "declined")
TITLE_MAX = 200
BODY_MAX = 20000
AREA_MAX = 60
REASON_MAX = 500
LIST_MAX = 500
FROM_BACKLOG = "From backlog #{id}"


@dataclass(slots=True)
class BacklogItem:
    id: int
    project_id: int
    project_slug: str
    title: str
    body_md: str | None
    area: str | None
    origin: str
    origin_task_id: int | None
    origin_task_key: str | None
    origin_task_stage: str | None
    origin_session_id: int | None
    origin_session_name: str | None
    intake_message_id: int | None
    status: str
    decline_reason: str | None
    task_id: int | None
    task_key: str | None
    decided_at: str | None
    decided_by: str | None
    created_at: str
    created_by: str

    @property
    def decision(self) -> str:
        """The `decision` column of backlog.md: empty for new."""
        if self.status == "declined":
            return f"declined: {self.decline_reason or ''}".rstrip()
        return "" if self.status == "new" else self.status


def parse_decision(text: str) -> tuple[str, str | None]:
    """(status, decline reason) from a backlog.md decision cell; ValueError names what is wrong."""
    value = text.strip()
    if not value or value.lower() == "new":
        return "new", None
    head, _, rest = value.partition(":")
    head = head.strip().lower()
    if head == "declined":
        reason = rest.strip()
        if not reason:
            raise ValueError("a declined row needs a reason: 'declined: <reason>'")
        return "declined", reason
    if head in ("later", "planned") and not rest.strip():
        return head, None
    raise ValueError(
        f"unknown decision {value!r}: use planned, later, 'declined: <reason>' or empty"
    )


def title_slug(title: str) -> str:
    """The title part of an imported row's identity: lowercase words joined by dashes."""
    return "-".join(re.findall(r"[a-z0-9]+", title.lower()))[:80] or "item"
