from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from relaybay.domain.stages import Stage


@dataclass(slots=True)
class Project:
    id: int
    slug: str
    name: str
    repo_path: str | None
    description: str | None
    created_at: str
    updated_at: str
    archived_at: str | None = None
    pulse: str | None = None  # where the project stands, at most 5 lines
    pulse_at: str | None = None
    pulse_by: str | None = None
    autonomy: bool = False  # agents may continue by themselves after a trusted message


@dataclass(slots=True)
class Session:
    id: int
    name: str
    external_id: str | None
    kind: str | None
    desktop_link: str | None
    status: str | None
    last_seen_at: str
    created_at: str
    project_id: int | None = None
    cwd: str | None = None
    activity: str | None = None
    activity_at: str | None = None
    cc_session_id: str | None = None
    unlinked_reason: str | None = None
    cc_exact: bool = False  # cc_session_id was linked by the agent itself (`hook_session`)
    transcript_path: str | None = None  # the transcript file its hooks reported last
    no_live_at: str | None = None  # the owner's "no live now" switch is on since then
    no_live_by: str | None = None


@dataclass(slots=True)
class Task:
    id: int
    project_id: int
    key: str
    title: str
    stage: Stage
    stage_note: str | None
    waiting_on: str | None
    next_step: str | None
    branch: str | None
    doc_path: str | None
    description: str | None
    progress: str | None
    session_id: int | None
    priority: int
    version: int
    created_at: str
    updated_at: str
    closed_at: str | None
    tracker_ref: str | None = None
    tracker_name: str | None = None
    tracker_url: str | None = None
    tracker_logged: bool = False
    next_step_at: str | None = None
    stage_changed_at: str | None = None
    project_slug: str = ""
    session_name: str | None = None
    session_external_id: str | None = None
    session_status: str | None = None
    session_link: str | None = None
    session_activity: str | None = None
    session_activity_at: str | None = None
    review_total: int = 0  # review checklist: steps in all, accepted, rejected
    review_accepted: int = 0
    review_rejected: int = 0
    size: str | None = None  # S / M / L
    plan_status: str | None = None  # None (no plan), draft or accepted
    plan_accepted_at: str | None = None
    plan_accepted_by: str | None = None
    plan_total: int = 0  # plan steps in all, done, done with a deviation
    plan_done: int = 0
    plan_deviations: int = 0
    plan_now: str | None = None  # title of the step in progress
    plan_now_position: int | None = None
    plan_revised_at: str | None = None  # when the newest plan revision was written
    autonomy: str = "project"  # project (follows the project's switch), on, off
    rank: int = 0  # the owner's order within the stage column, lower first
    project_autonomy: bool = False
    answer_received_at: str | None = None  # an external message answered `waiting_on`
    answer_message_id: int | None = None
    archived_at: str | None = None  # sent to the archive by hand (done only)
    archived_by: str | None = None
    unarchived_at: str | None = None  # the last "Back from the archive"

    @property
    def autonomy_on(self) -> bool:
        """Effective autonomy: the task's own switch, or the project's when it follows it."""
        if self.autonomy == "project":
            return self.project_autonomy
        return self.autonomy == "on"

    @property
    def next_step_stale(self) -> bool:
        """The next step was written before the last stage change (ISO UTC strings compare in
        time order)."""
        return bool(
            self.next_step
            and self.next_step_at
            and self.stage_changed_at
            and self.next_step_at < self.stage_changed_at
        )


@dataclass(slots=True)
class Binding:
    """One period during which a session was bound to a task (`task_session` row)."""

    id: int
    task_id: int
    session_id: int
    session_name: str
    session_external_id: str | None
    session_link: str | None
    bound_at: str
    unbound_at: str | None
    bound_by: str


@dataclass(slots=True)
class TrackerEntry:
    """One issue the task has had in an external tracker (`tracker_entry` row). `logged_at` is
    when the work log was last marked entered; `unlogged_at` is set when the mark is cleared."""

    id: int
    task_id: int
    tracker_name: str | None
    tracker_ref: str
    tracker_url: str | None
    logged_at: str | None
    unlogged_at: str | None
    created_at: str


@dataclass(slots=True)
class Report:
    id: int
    task_id: int
    session_id: int | None
    author: str
    state: str | None
    size: str | None
    summary: str
    body_md: str
    created_at: str


@dataclass(slots=True)
class Decision:
    id: int
    task_id: int | None
    project_id: int
    text: str
    decided_by: str | None
    created_at: str
    task_key: str | None = None
    scope: str = "project"  # 'task' when task_id is set
    source_kind: str | None = None  # report / event / comment / message
    source_ref: str | None = None
    source_text: str | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    recorded_at: str | None = None
    superseded_at: str | None = None
    superseded_by: int | None = None
    project_slug: str | None = None

    @property
    def current(self) -> bool:
        return self.superseded_at is None


@dataclass(slots=True)
class Comment:
    """A comment on a task: the owner's (delivered to the bound session through its hooks) or an
    agent's reply. `read_at` is when the agent acknowledged an owner comment, or when the owner
    marked an agent reply read."""

    id: int
    task_id: int
    author_kind: str  # 'owner', 'agent' or 'external' (an intake message forwarded to the agent)
    author: str
    session_id: int | None
    body_md: str
    in_reply_to: int | None
    created_at: str
    delivered_at: str | None
    delivered_to_session_id: int | None
    read_at: str | None
    stop_blocks: int = 0  # Stop blocks this comment caused since it was delivered
    task_key: str = ""
    delivered_to_name: str | None = None
    step_id: int | None = None  # a comment about one review step (a decision is one)
    attachment_id: int | None = None

    @property
    def from_owner(self) -> bool:
        return self.author_kind == "owner"

    @property
    def to_agent(self) -> bool:
        """Delivered to the bound agent through its hooks: an owner or an external comment."""
        return self.author_kind != "agent"


@dataclass(slots=True)
class ImportItem:
    """One thing `relaybay import` wrote (`import_item` row)."""

    kind: str  # document, task, task_note, decision, note
    item: str  # stable name of the thing, e.g. 'acme-shop/spec'
    source_path: str  # relative to the import root
    target_id: int
    content_hash: str  # the file's value at the import
    target_hash: str  # the target's value right after the import
    imported_at: str


@dataclass(slots=True)
class Event:
    id: int
    ts: str
    kind: str
    project_id: int | None
    task_id: int | None
    session_id: int | None
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SavedFilter:
    """A named board query of one project."""

    id: int
    project_id: int
    project_slug: str
    name: str
    stage: str | None
    waiting_on: str | None
    session: str | None
    q: str | None
    created_at: str

    @property
    def query(self) -> dict[str, str]:
        """The board's query parameters; the filter's URL is built from them."""
        given = {
            "stage": self.stage,
            "waiting_on": self.waiting_on,
            "session": self.session,
            "q": self.q,
        }
        return {name: value for name, value in given.items() if value}

    @property
    def qs(self) -> str:
        return urlencode(self.query)
