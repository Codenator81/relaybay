"""The backlog: findings of sessions and reviews, ideas from intake and the owner's notes. A
backlog item is not a task and not a stage; "To plan" on the board turns it into a task."""

import hashlib
import re
from dataclasses import dataclass
from typing import Any

STATUSES = ("new", "later", "planned", "done", "declined")
OPEN_STATUSES = ("new", "later")
ORIGINS = ("session", "review", "intake", "owner")
# Statuses a coordinator may set over MCP: `planned` only comes from "To plan" on the board.
MCP_STATUSES = ("new", "later", "done", "declined")
TITLE_MAX = 200
BODY_MAX = 20000
AREA_MAX = 60
REASON_MAX = 500
LINK_MAX = 500
LINK_RE = re.compile(r"https?://[^\s<>\"'|]+", re.IGNORECASE)  # use fullmatch
LIST_MAX = 500
ASSIGNEE_MAX = 100
PRIORITY_MIN = -9
PRIORITY_MAX = 9
UNASSIGNED = "-"  # the assignee filter's value for items that are for nobody yet
# What backlog_patch takes: the parameters of backlog_update.
PATCH_FIELDS = ("status", "reason", "link", "title", "body", "area", "priority", "assignee")
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
    decline_reason: str | None  # why declined; for done, an optional note
    link: str | None  # done: where it was fixed (a pull request, a commit)
    task_id: int | None
    task_key: str | None
    decided_at: str | None
    decided_by: str | None
    created_at: str
    created_by: str
    priority: int = 0  # higher first within a status
    assignee: str | None = None  # who it is for; "To plan" makes it the task's waiting_on

    @property
    def decision(self) -> str:
        """The `decision` column of backlog.md: empty for new."""
        if self.status == "declined":
            return f"declined: {self.decline_reason or ''}".rstrip()
        if self.status == "done":
            return f"done: {self.link}" if self.link else "done"
        return "" if self.status == "new" else self.status


def parse_decision(text: str) -> tuple[str, str | None]:
    """(status, detail) from a backlog.md decision cell: the detail is the reason of a declined
    row and the link of a done one. ValueError names what is wrong."""
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
    if head == "done":
        link = rest.strip() or None
        if link is not None and (len(link) > LINK_MAX or not LINK_RE.fullmatch(link)):
            raise ValueError(f"a done row's link must be an http(s) link, got {link!r}")
        return "done", link
    if head in ("later", "planned") and not rest.strip():
        return head, None
    raise ValueError(
        f"unknown decision {value!r}: use planned, later, done, 'done: <link>',"
        " 'declined: <reason>' or empty"
    )


def parse_priority(value: Any) -> int:
    """A priority from a form, a JSON value or a backlog.md cell; empty means 0. ValueError names
    what is wrong."""
    text = "" if value is None else str(value).strip()
    if not text:
        return 0
    if isinstance(value, bool) or not re.fullmatch(r"[+-]?\d{1,4}", text):
        raise ValueError(f"priority must be a whole number, got {value!r}")
    number = int(text)
    if not PRIORITY_MIN <= number <= PRIORITY_MAX:
        raise ValueError(f"priority must be from {PRIORITY_MIN} to {PRIORITY_MAX}, got {number}")
    return number


def title_slug(title: str) -> str:
    """The title part of an imported row's identity: lowercase words joined by dashes, in any
    script. A title without a single word (punctuation, emoji) gets a short hash, so two such
    rows on one day stay two rows."""
    words = re.findall(r"[^\W_]+", title.lower())
    if words:
        return "-".join(words)[:80]
    digest = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:8]
    return f"item-{digest}"
