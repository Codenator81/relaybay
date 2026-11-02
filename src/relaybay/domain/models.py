from dataclasses import dataclass, field
from typing import Any

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
