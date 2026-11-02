"""Use cases. The only writer of the database: state change + event row in one transaction,
then the committed events are published on the in-process bus."""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import aiosqlite

from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.domain.hooks import (
    CC_ID_MAX,
    HookEvent,
    folder_name,
    parse_hook,
)
from relaybay.domain.models import (
    Binding,
    Decision,
    Event,
    Project,
    Report,
    Session,
    Task,
    TrackerEntry,
)
from relaybay.domain.reports import parse_report
from relaybay.domain.rules import (
    DEFAULT_RULES,
    REPORT_SINCE_STAGE_CHANGE,
    RULES_MODES,
    Rules,
    merge_project_rules,
    refusal_text,
    unmet,
    validate_rules,
    warning_text,
)
from relaybay.domain.sessions import DEFAULT_DESKTOP_LINK_TEMPLATE, parse_session_id
from relaybay.domain.stages import OPEN_STAGES, STAGES, Stage, parse_stage
from relaybay.domain.task_fields import BY_NAME, FIELD_NAMES, NAME_MAX, TEXT_MAX, TITLE_MAX
from relaybay.domain.tasks import DEFAULT_DONE_WINDOW_DAYS, is_archived
from relaybay.events import EventBus

logger = logging.getLogger("relaybay")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOOK_ACTIVITY_WINDOW = 5.0  # seconds: at most one activity event per session
HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)  # use fullmatch


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(slots=True)
class _Tx:
    conn: aiosqlite.Connection
    now: str
    events: list[Event] = field(default_factory=list)

    async def event(
        self,
        kind: str,
        actor: str,
        *,
        project_id: int | None = None,
        task_id: int | None = None,
        session_id: int | None = None,
        **payload: Any,
    ) -> None:
        self.events.append(
            await repo.insert_event(
                self.conn,
                ts=self.now,
                kind=kind,
                actor=actor,
                project_id=project_id,
                task_id=task_id,
                session_id=session_id,
                payload=payload,
            )
        )


@dataclass(slots=True)
class ProjectSummary:
    project: Project
    counts: dict[str, int]

    @property
    def open_count(self) -> int:
        return sum(n for stage, n in self.counts.items() if stage != Stage.DONE)


@dataclass(slots=True)
class TaskDetail:
    task: Task
    reports: list[Report]
    decisions: list[Decision]
    bindings: list[Binding]
    tracker_history: list[TrackerEntry]


@dataclass(slots=True)
class Inbox:
    """What needs the owner: tasks waiting on them, tasks whose last stage move missed a rule,
    done tasks without the work-log mark (with `require_tracker_log`), unlinked hook sessions."""

    waiting: list[Task]
    rule_warnings: list[tuple[Task, list[str]]]
    not_logged: list[Task]
    unlinked: list[Session]

    @property
    def count(self) -> int:
        return (
            len(self.waiting) + len(self.rule_warnings) + len(self.not_logged) + len(self.unlinked)
        )


@dataclass(slots=True)
class SessionRow:
    session: Session
    tasks: list[Task]  # bound to the session now


class Saved(NamedTuple):
    task: Task
    warnings: list[str]  # rules the write did not meet (warn mode)


class BindResult(NamedTuple):
    task: Task
    kept: Session | None  # the desktop session a plain name did not replace
    alias_recorded: bool  # the name now resolves to `kept`


@dataclass(slots=True)
class Snapshot:
    project: Project
    tasks: list[Task]
    last_reports: dict[int, Report]
    decisions: list[Decision]
    rules: Rules


def _clean_text(value: Any, name: str, limit: int = TEXT_MAX) -> str | None:
    if value is None:
        return None
    # Browsers submit textareas with CRLF; stored text uses LF so unchanged values compare equal.
    text = str(value).replace("\r\n", "\n").strip()
    if len(text) > limit:
        raise ValidationError(f"{name} is longer than {limit} characters.")
    return text or None


def _clean_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    raise ValidationError(f"{name} must be true or false.")


def _clean_url(value: Any, name: str) -> str | None:
    url = _clean_text(value, name)
    if url is not None and not HTTP_URL_RE.fullmatch(url):
        raise ValidationError(f"{name} must be an http:// or https:// link, got {url!r}.")
    return url


def _clean_field(name: str, value: Any) -> Any:
    """Validate one task field by its entry in `TASK_FIELDS`; None (or '') clears a text."""
    spec = BY_NAME[name]
    if spec.kind == "bool":
        return _clean_bool(value, name)
    if spec.kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int | str | None):
            raise ValidationError(f"{name} must be an integer.")
        try:
            return int(value or 0)
        except ValueError:
            raise ValidationError(f"{name} must be an integer, got {value!r}.") from None
    if value is not None and not isinstance(value, str):
        raise ValidationError(f"{name} must be a string.")
    if spec.kind == "title":
        return _clean_title(value)
    if spec.kind == "stage":
        return parse_stage(value or "").value
    if spec.kind == "url":
        return _clean_url(value, name)
    return _clean_text(value, name, spec.max_length or TEXT_MAX)


def _clean_title(value: Any) -> str:
    title = _clean_text(value, "title", TITLE_MAX)
    if not title:
        raise ValidationError("title must not be empty.")
    return title


def _task_diff(task: Task, clean: dict[str, Any]) -> dict[str, Any]:
    """Fields of `clean` that change `task`. A next step passed with the same text still counts
    when it is stale or the same write changes the stage: it confirms the step as current."""
    diff = {k: v for k, v in clean.items() if v != getattr(task, k)}
    if "next_step" in clean and task.next_step and ("stage" in diff or task.next_step_stale):
        diff["next_step"] = clean["next_step"]
    return diff


def _unlog_reason(task: Task, diff: dict[str, Any]) -> str | None:
    """New work in `diff` that invalidates the task's work-log mark: a move back out of `live` or
    `done` to an earlier stage, or a changed progress summary. Metadata edits do not count."""
    reasons = []
    stage = diff.get("stage")
    if (
        stage is not None
        and task.stage in (Stage.LIVE, Stage.DONE)
        and STAGES.index(Stage(stage)) < STAGES.index(task.stage)
    ):
        reasons.append(f"stage moved back from {task.stage.value} to {stage}")
    if "progress" in diff:
        reasons.append("progress changed")
    return ", ".join(reasons) or None


def _project_override(slug: str, body: str) -> dict[str, Any]:
    """The project's partial rules document; ValueError says what is wrong with it."""
    source = f"project_rules for {slug!r}"
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source}: not valid JSON ({exc}).") from None
    return validate_rules(data, source)


def _clean_slug(value: str) -> str:
    slug = value.strip().lower()
    if not SLUG_RE.match(slug):
        raise ValidationError(
            f"Invalid project slug {value!r}: use lowercase letters, digits and dashes,"
            " e.g. 'acme-shop'."
        )
    return slug


class Service:
    def __init__(
        self,
        db_path: Path,
        bus: EventBus | None = None,
        *,
        desktop_link_template: str = DEFAULT_DESKTOP_LINK_TEMPLATE,
        rules: dict[str, Any] | None = None,
        rules_mode: str = "warn",
        hook_activity_window: float = HOOK_ACTIVITY_WINDOW,
    ) -> None:
        if "{id}" not in desktop_link_template:
            raise ValueError("desktop_link_template must contain '{id}'.")
        if rules_mode not in RULES_MODES:
            raise ValueError(f"rules_mode must be one of: {', '.join(RULES_MODES)}.")
        self.global_rules = Rules(rules if rules is not None else DEFAULT_RULES, mode=rules_mode)
        self.rules_mode = rules_mode
        self._logged_rule_errors: set[str] = set()
        self.db_path = db_path
        self.bus = bus or EventBus()
        self.desktop_link_template = desktop_link_template
        self._write_conn: aiosqlite.Connection | None = None
        self._read_conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()
        self.hook_activity_window = hook_activity_window
        # Per session: when activity was last written, the newest one held back, its timer.
        self._activity_written: dict[int, float] = {}
        self._activity_pending: dict[int, HookEvent] = {}
        self._activity_timers: dict[int, asyncio.TimerHandle] = {}
        self._flushes: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_conn = await connect(self.db_path)
        await migrate(self._write_conn)
        self._read_conn = await connect(self.db_path)
        # A broken project override is reported now; it never stops the server or the project.
        for slug, body in await repo.list_project_rules(self._read_conn):
            self._merged_rules(slug, body)

    async def close(self) -> None:
        for timer in self._activity_timers.values():
            timer.cancel()
        self._activity_timers.clear()
        self._activity_pending.clear()
        for task in list(self._flushes):
            task.cancel()
        for conn in (self._read_conn, self._write_conn):
            if conn is not None:
                await conn.close()
        self._read_conn = self._write_conn = None

    @property
    def _r(self) -> aiosqlite.Connection:
        if self._read_conn is None:
            raise RuntimeError("Service is not started")
        return self._read_conn

    @contextlib.asynccontextmanager
    async def _tx(self) -> AsyncIterator[_Tx]:
        if self._write_conn is None:
            raise RuntimeError("Service is not started")
        conn = self._write_conn
        async with self._write_lock:
            await conn.execute("BEGIN IMMEDIATE")
            tx = _Tx(conn=conn, now=now_iso())
            try:
                yield tx
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            # Shielded as one step: a cancelled request must not commit without publishing.
            await asyncio.shield(self._commit_and_publish(conn, tx.events))

    async def _commit_and_publish(self, conn: aiosqlite.Connection, events: list[Event]) -> None:
        await conn.execute("COMMIT")
        for event in events:
            self.bus.publish(event)

    def desktop_link(self, external_id: str) -> str:
        return self.desktop_link_template.replace("{id}", external_id)

    async def _session(
        self, tx: _Tx, value: str | None, title: str | None, hook_session: str | None = None
    ) -> Session | None:
        """Upsert the calling session: a desktop id (`local_<uuid>`) or a plain name. With
        `hook_session` (the Claude Code session id its hooks report), that id is linked to it."""
        cc_id = _clean_text(hook_session, "hook_session", CC_ID_MAX)
        value = _clean_text(value, "session", NAME_MAX)
        if value is None:
            return None
        found = await self._resolve_session(tx, value, title)
        if found is not None and cc_id is not None:
            found = await self._link_hook_session(tx, found, cc_id)
        return found

    async def _resolve_session(self, tx: _Tx, value: str, title: str | None) -> Session | None:
        title = _clean_text(title, "session_title", NAME_MAX)
        external_id = parse_session_id(value)
        if external_id is None:
            aliased = await repo.get_session_by_alias(tx.conn, value)
            if aliased is not None:
                await repo.touch_session(tx.conn, aliased.id, tx.now)
                return aliased
            existing = await repo.get_session_by_name(tx.conn, value)
            if existing is None:
                new_id = await repo.insert_session(tx.conn, value, tx.now)
                await tx.event("session.seen", value, session_id=new_id, session=value)
            else:
                await repo.touch_session(tx.conn, existing.id, tx.now)
            found = await repo.get_session_by_name(tx.conn, value)
        else:
            link = self.desktop_link(external_id)
            existing = await repo.get_session_by_external_id(tx.conn, external_id)
            if existing is None:
                name = title or external_id
                new_id = await repo.insert_session(
                    tx.conn,
                    name,
                    tx.now,
                    external_id=external_id,
                    kind="desktop",
                    desktop_link=link,
                )
                await tx.event(
                    "session.seen",
                    name,
                    session_id=new_id,
                    session=name,
                    session_external_id=external_id,
                )
            else:
                changes: dict[str, Any] = {}
                if title and title != existing.name:
                    changes["name"] = title
                if link != existing.desktop_link:
                    changes["desktop_link"] = link
                await repo.touch_session(tx.conn, existing.id, tx.now, changes)
            found = await repo.get_session_by_external_id(tx.conn, external_id)
        return found

    async def _bind(self, tx: _Tx, task: Task, session: Session, actor: str) -> bool:
        """Bind `session` to `task`, closing the previous binding. False if already bound."""
        previous = await repo.open_binding(tx.conn, task.id)
        if previous == session.id:
            return False
        await repo.rebind(tx.conn, task.id, session.id, actor, tx.now)
        if task.session_id != session.id:
            await repo.update_task(tx.conn, task.id, {"session_id": session.id}, tx.now)
        await tx.event(
            "task.session_bound",
            actor,
            project_id=task.project_id,
            task_id=task.id,
            session_id=session.id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            session=session.name,
            session_external_id=session.external_id,
            previous=task.session_name if previous is not None else None,
        )
        return True

    async def _sync_tracker(self, tx: _Tx, task: Task, diff: dict[str, Any]) -> None:
        """Mirror a write of the task's tracker fields into its `tracker_entry` history: a new
        key appends an entry, the work-log mark stamps the current one. A cleared key leaves the
        earlier entries as history."""
        if not any(k.startswith("tracker_") for k in diff):
            return
        ref = diff.get("tracker_ref", task.tracker_ref)
        if ref is None:
            return
        name = diff.get("tracker_name", task.tracker_name)
        url = diff.get("tracker_url", task.tracker_url)
        logged = diff.get("tracker_logged", task.tracker_logged)
        entry = (
            None
            if "tracker_ref" in diff
            else await repo.latest_tracker_entry(tx.conn, task.id, ref)
        )
        if entry is None:
            logged_at = tx.now if logged else None
            await repo.insert_tracker_entry(tx.conn, task.id, name, ref, url, logged_at, tx.now)
            return
        changes: dict[str, Any] = {
            k: v
            for k, v in (("tracker_name", name), ("tracker_url", url))
            if v != getattr(entry, k)
        }
        if "tracker_logged" in diff:
            # Logging again restarts the entry's mark: logged_at is the latest log.
            changes.update(
                {"logged_at": tx.now, "unlogged_at": None} if logged else {"unlogged_at": tx.now}
            )
        if changes:
            await repo.update_tracker_entry(tx.conn, entry.id, changes)

    async def _unlogged(
        self,
        tx: _Tx,
        task: Task,
        actor: str,
        session_id: int | None,
        stage: str,
        tracker_ref: str | None,
        reason: str,
    ) -> None:
        await tx.event(
            "task.tracker_unlogged",
            actor,
            project_id=task.project_id,
            task_id=task.id,
            session_id=session_id,
            project=task.project_slug,
            key=task.key,
            stage=stage,
            tracker_ref=tracker_ref,
            reason=reason,
        )

    # --- hook sessions -----------------------------------------------------

    async def _session_events(self, tx: _Tx, session: Session, actor: str, **payload: Any) -> None:
        """One `session.status_changed` per task the session is bound to (cards refresh by
        key), or one for the session alone."""
        tasks = await repo.open_tasks_of_session(tx.conn, session.id)
        common = {
            "session": session.name,
            "session_external_id": session.external_id,
            "status": session.status,
            "activity": session.activity,
            **payload,
        }
        if not tasks:
            await tx.event(
                "session.status_changed",
                actor,
                project_id=session.project_id,
                session_id=session.id,
                **common,
            )
        for task in tasks:
            await tx.event(
                "session.status_changed",
                actor,
                project_id=task.project_id,
                task_id=task.id,
                session_id=session.id,
                project=task.project_slug,
                key=task.key,
                stage=task.stage.value,
                **common,
            )

    async def _link_hook_session(self, tx: _Tx, session: Session, cc_id: str) -> Session:
        """Exact link from an agent's write: `cc_id` now belongs to `session`. A row the hooks
        created for it by guess hands over its status and is removed when nothing refers to it."""
        if session.cc_session_id == cc_id:
            return session
        changes: dict[str, Any] = {"cc_session_id": cc_id, "unlinked_reason": None}
        other = await repo.get_session_by_cc(tx.conn, cc_id)
        if other is not None and other.id != session.id:
            changes.update(
                status=other.status,
                activity=other.activity,
                activity_at=other.activity_at,
                cwd=other.cwd,
            )
            if other.kind == "hook" and not await repo.has_bindings(tx.conn, other.id):
                await repo.delete_session(tx.conn, other.id)
            else:
                await repo.touch_session(
                    tx.conn,
                    other.id,
                    tx.now,
                    {"cc_session_id": None, "unlinked_reason": None, "status": None},
                )
        if session.project_id is None and other is not None and other.project_id is not None:
            changes["project_id"] = other.project_id
        await repo.touch_session(tx.conn, session.id, tx.now, changes)
        linked = await repo.get_session_by_id(tx.conn, session.id)
        assert linked is not None
        self._activity_written.pop(session.id, None)
        await self._session_events(tx, linked, linked.name, hook_session=cc_id, linked=True)
        return linked

    def _project_for_cwd(
        self, projects: list[Project], cwd: str | None
    ) -> tuple[Project | None, str | None]:
        """The project whose repo_path is `cwd` or its closest parent; a tie is ambiguous and
        returns the reason instead."""
        if not cwd:
            return None, None
        target = os.path.normcase(os.path.normpath(cwd))
        best: list[Project] = []
        best_len = -1
        for project in projects:
            root = os.path.normcase(os.path.normpath(project.repo_path or ""))
            if not root or not (target == root or target.startswith(root.rstrip(os.sep) + os.sep)):
                continue
            if len(root) > best_len:
                best, best_len = [project], len(root)
            elif len(root) == best_len:
                best.append(project)
        if len(best) > 1:
            names = ", ".join(sorted(p.slug for p in best))
            return None, f"the folder belongs to several projects ({names})"
        return (best[0] if best else None), None

    async def _new_hook_session(
        self, tx: _Tx, hook: HookEvent, project_id: int | None, reason: str | None
    ) -> Session:
        """A plain session named after the folder; an unlinked one (with `reason`) or a second
        live session in the same folder also gets a short id, so names stay unique."""
        folder = folder_name(hook.cwd)
        name = folder
        if reason is None:
            existing = await repo.get_session_by_name(tx.conn, folder)
            if existing is not None and (
                existing.cc_session_id is None or existing.status == "ended"
            ):
                await repo.touch_session(
                    tx.conn,
                    existing.id,
                    tx.now,
                    {"cc_session_id": hook.cc_session_id, "cwd": hook.cwd},
                )
                found = await repo.get_session_by_id(tx.conn, existing.id)
                assert found is not None
                return found
        if reason is not None or await repo.get_session_by_name(tx.conn, folder) is not None:
            name = f"{folder} · {hook.cc_session_id[:8]}"
            if await repo.get_session_by_name(tx.conn, name) is not None:
                name = f"{folder} · {hook.cc_session_id}"
        new_id = await repo.insert_session(tx.conn, name, tx.now, kind="hook")
        await repo.touch_session(
            tx.conn,
            new_id,
            tx.now,
            {
                "cc_session_id": hook.cc_session_id,
                "cwd": hook.cwd,
                "project_id": project_id,
                "unlinked_reason": reason,
            },
        )
        await tx.event(
            "session.seen",
            name,
            project_id=project_id,
            session_id=new_id,
            session=name,
            hook_session=hook.cc_session_id,
            **({"unlinked_reason": reason} if reason else {}),
        )
        found = await repo.get_session_by_id(tx.conn, new_id)
        assert found is not None
        return found

    async def _hook_session(self, tx: _Tx, hook: HookEvent) -> Session:
        """The relaybay session of a hook event. First sight without an exact link: the one
        desktop session bound in the project of `cwd`; several possible ones (or a folder of
        several projects) stay unlinked; none, a plain session named after the folder."""
        found = await repo.get_session_by_cc(tx.conn, hook.cc_session_id)
        if found is not None:
            return found
        projects = await repo.projects_with_repo_path(tx.conn)
        project, reason = self._project_for_cwd(projects, hook.cwd)
        if project is not None:
            bound = await repo.bound_desktop_sessions(tx.conn, project.id)
            free = [s for s in bound if s.cc_session_id is None or s.status == "ended"]
            if len(free) == 1:
                session = free[0]
                changes = {"cc_session_id": hook.cc_session_id, "cwd": hook.cwd}
                if session.project_id is None:
                    changes["project_id"] = project.id
                await repo.touch_session(tx.conn, session.id, tx.now, changes)
                linked = await repo.get_session_by_id(tx.conn, session.id)
                assert linked is not None
                return linked
            if len(free) > 1:
                reason = f"{len(free)} desktop sessions in {project.slug} could match"
            elif bound:
                reason = f"the desktop sessions in {project.slug} are linked to other hooks"
        return await self._new_hook_session(tx, hook, project.id if project else None, reason)

    async def record_hook(self, payload: Any) -> Session | None:
        """Apply one Claude Code hook event to its session's status. Unknown events return None.
        Activity of a working session is written at most once per window; the latest one in
        the window is written when it ends."""
        hook = parse_hook(payload)
        if hook is None:
            return None
        if hook.activity_only:
            current = await repo.get_session_by_cc(self._r, hook.cc_session_id)
            if current is not None and current.status == "working":
                last = self._activity_written.get(current.id)
                elapsed = time.monotonic() - last if last is not None else None
                if elapsed is not None and elapsed < self.hook_activity_window:
                    self._activity_pending[current.id] = hook
                    if current.id not in self._activity_timers:
                        self._activity_timers[current.id] = asyncio.get_running_loop().call_later(
                            self.hook_activity_window - elapsed, self._flush_activity, current.id
                        )
                    return current
        return await self._apply_hook(hook)

    def _flush_activity(self, session_id: int) -> None:
        self._activity_timers.pop(session_id, None)
        hook = self._activity_pending.pop(session_id, None)
        if hook is None:
            return
        task = asyncio.get_running_loop().create_task(self._apply_hook(hook))
        self._flushes.add(task)
        task.add_done_callback(self._flush_done)

    def _flush_done(self, task: asyncio.Task[Any]) -> None:
        self._flushes.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error("Writing hook activity failed", exc_info=task.exception())

    async def _apply_hook(self, hook: HookEvent) -> Session:
        async with self._tx() as tx:
            session = await self._hook_session(tx, hook)
            timer = self._activity_timers.pop(session.id, None)
            if timer is not None:
                timer.cancel()
            self._activity_pending.pop(session.id, None)  # this write is newer
            changes: dict[str, Any] = {"status": hook.status, "activity": hook.activity}
            if hook.cwd and hook.cwd != session.cwd:
                changes["cwd"] = hook.cwd
            if changes["status"] != session.status or changes["activity"] != session.activity:
                changes["activity_at"] = tx.now
            await repo.touch_session(tx.conn, session.id, tx.now, changes)
            updated = await repo.get_session_by_id(tx.conn, session.id)
            assert updated is not None
            if "activity_at" in changes or hook.activity_only:
                # Also for an unchanged activity: the next ones in the window are held back.
                self._activity_written[session.id] = time.monotonic()
            if "activity_at" in changes:
                await self._session_events(
                    tx,
                    updated,
                    updated.name,
                    hook_event=hook.event,
                    **({"previous": session.status} if session.status != hook.status else {}),
                )
            if hook.event == "SessionEnd":
                self._activity_written.pop(session.id, None)
                for task in await repo.open_tasks_of_session(tx.conn, session.id):
                    await repo.unbind(tx.conn, task.id, tx.now)
                    await repo.update_task(tx.conn, task.id, {"session_id": None}, tx.now)
                    await tx.event(
                        "task.session_unbound",
                        updated.name,
                        project_id=task.project_id,
                        task_id=task.id,
                        session_id=updated.id,
                        project=task.project_slug,
                        key=task.key,
                        stage=task.stage.value,
                        session=updated.name,
                        session_external_id=updated.external_id,
                        reason="session_end",
                        end_reason=hook.end_reason,
                    )
            return updated

    async def inbox(
        self,
        owner_names: list[str],
        *,
        require_tracker_log: bool = False,
        done_window_days: int = DEFAULT_DONE_WINDOW_DAYS,
    ) -> Inbox:
        owners = {name.strip().casefold() for name in owner_names}
        tasks = await repo.list_tasks(self._r)
        moves = await repo.latest_stage_changes(self._r)
        now = datetime.now(UTC)
        waiting, warned, not_logged = [], [], []
        for task in tasks:
            if is_archived(task, now, done_window_days, require_tracker_log):
                continue
            if task.stage == Stage.DONE:
                if require_tracker_log and not task.tracker_logged:
                    not_logged.append(task)
            elif task.waiting_on and task.waiting_on.strip().casefold() in owners:
                waiting.append(task)
            move = moves.get(task.id)
            # Shown until the task moves again: the warning is about the move into this stage.
            if move and move.payload.get("warnings") and move.payload.get("to") == task.stage:
                warned.append((task, list(move.payload["warnings"])))
        return Inbox(waiting, warned, not_logged, await self.unlinked_hook_sessions())

    async def list_sessions(self, project: str | None = None, limit: int = 200) -> list[SessionRow]:
        """Sessions with the tasks bound to them now; with `project`, the sessions of that
        project (placed there by its hooks or bound to one of its tasks)."""
        project_id = (await self.get_project(project)).id if project else None
        sessions = await repo.list_sessions(self._r, project_id, limit)
        bound: dict[int, list[Task]] = {s.id: [] for s in sessions}
        for task in await repo.list_tasks(self._r, session_ids=list(bound)):
            if task.session_id is not None:
                bound[task.session_id].append(task)
        return [SessionRow(s, bound[s.id]) for s in sessions]

    async def activity(
        self,
        *,
        project: str | None = None,
        kind: str | None = None,
        before_id: int | None = None,
        after_id: int | None = None,
        exclude_kinds: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[Event]:
        """Events newest first (the Activity feed)."""
        project_id = (await self.get_project(project)).id if project else None
        return await repo.events_page(
            self._r,
            before_id=before_id,
            after_id=after_id,
            project_id=project_id,
            kind=kind,
            exclude_kinds=exclude_kinds,
            limit=max(1, min(limit, 500)),
        )

    async def unlinked_hook_sessions(self, limit: int = 50) -> list[Session]:
        """Hook sessions that could not be linked to one relaybay session (Settings page)."""
        return await repo.list_unlinked_hook_sessions(self._r, limit)

    # --- reads -------------------------------------------------------------

    async def list_projects(self) -> list[ProjectSummary]:
        projects = await repo.list_projects(self._r)
        counts = await repo.stage_counts(self._r)
        return [ProjectSummary(p, counts.get(p.id, {})) for p in projects]

    async def get_project(self, slug: str, conn: aiosqlite.Connection | None = None) -> Project:
        project = await repo.get_project(conn or self._r, slug.strip().lower())
        if project is None:
            raise NotFoundError(f"Project {slug!r} not found. Create it with project_upsert.")
        return project

    async def get_task(self, ref: str | int, conn: aiosqlite.Connection | None = None) -> Task:
        conn = conn or self._r
        text = str(ref).strip()
        task = (
            await repo.get_task_by_id(conn, int(text))
            if text.isdigit()
            else await repo.get_task_by_key(conn, text.lower())
        )
        if task is None:
            raise NotFoundError(f"Task {text!r} not found.")
        return task

    async def list_tasks(
        self,
        *,
        project: str | None = None,
        stage: str | None = None,
        waiting_on: str | None = None,
        session: str | None = None,
        include_done: bool = False,
    ) -> list[Task]:
        project_id = (await self.get_project(project)).id if project else None
        if stage:
            stages = [parse_stage(stage).value]
        else:
            stages = [s.value for s in (STAGES if include_done else OPEN_STAGES)]
        session_ids = None
        if session:
            needle = parse_session_id(session) or session.strip()
            session_ids = [x.id for x in await repo.find_sessions(self._r, needle)]
            if not session_ids:
                return []
        return await repo.list_tasks(
            self._r,
            project_id=project_id,
            stages=stages,
            waiting_on=_clean_text(waiting_on, "waiting_on"),
            session_ids=session_ids,
        )

    async def task_detail(self, ref: str | int, reports: int = 5) -> TaskDetail:
        task = await self.get_task(ref)
        return TaskDetail(
            task=task,
            reports=await repo.list_reports(self._r, task.id, reports),
            decisions=await repo.list_decisions(self._r, task_id=task.id, limit=10),
            bindings=await repo.list_bindings(self._r, task.id),
            tracker_history=await repo.list_tracker_entries(self._r, task.id),
        )

    def _merged_rules(self, slug: str, body: str | None) -> Rules:
        """The global rules with the project's override on top; a broken override is logged
        (once) and ignored, so the global rules apply and the reason is shown with them."""
        base = self.global_rules.body
        if body is None:
            return Rules(base, slug, self.rules_mode)
        try:
            override = _project_override(slug, body)
        except ValueError as exc:
            error = str(exc)
            if error not in self._logged_rule_errors:
                self._logged_rule_errors.add(error)
                logger.warning("%s The global rules apply to %s instead.", error, slug)
            return Rules(base, slug, self.rules_mode, override_error=error)
        return Rules(merge_project_rules(base, override), slug, self.rules_mode)

    async def _rules(self, conn: aiosqlite.Connection, project: Project) -> Rules:
        return self._merged_rules(project.slug, await repo.get_project_rules(conn, project.id))

    async def rules(self, project: str | None = None) -> Rules:
        """The global rules, or a project's rules merged on top of them."""
        if project is None:
            return self.global_rules
        return await self._rules(self._r, await self.get_project(project))

    async def last_reports(self, tasks: list[Task]) -> dict[int, Report]:
        return await repo.last_reports(self._r, [t.id for t in tasks])

    async def snapshot(self, slug: str, include_done: bool = False) -> Snapshot:
        project = await self.get_project(slug)
        tasks = await self.list_tasks(project=project.slug, include_done=include_done)
        return Snapshot(
            project=project,
            tasks=tasks,
            last_reports=await self.last_reports(tasks),
            decisions=await repo.list_decisions(self._r, project_id=project.id, limit=10),
            rules=await self._rules(self._r, project),
        )

    async def events_since(
        self, since_id: int = 0, project: str | None = None, limit: int = 50
    ) -> list[Event]:
        project_id = (await self.get_project(project)).id if project else None
        return await repo.events_after(self._r, since_id, project_id, max(1, min(limit, 500)))

    async def last_event_id(self) -> int:
        return await repo.last_event_id(self._r)

    async def replay(self, after_id: int, project_id: int | None, limit: int) -> list[Event]:
        return await repo.events_after(self._r, after_id, project_id, limit)

    async def waiting_on_values(self) -> list[str]:
        return await repo.distinct_waiting_on(self._r)

    # --- writes ------------------------------------------------------------

    async def upsert_project(
        self,
        slug: str,
        *,
        name: str | None = None,
        repo_path: str | None = None,
        description: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Project, bool]:
        slug = _clean_slug(slug)
        name = _clean_text(name, "name", NAME_MAX)
        given = {
            "name": name,
            "repo_path": _clean_text(repo_path, "repo_path"),
            "description": _clean_text(description, "description"),
        }
        default_actor = "mcp"
        async with self._tx() as tx:
            sess = await self._session(tx, session, session_title, hook_session)
            session_id = sess.id if sess else None
            actor = sess.name if sess else default_actor
            existing = await repo.get_project(tx.conn, slug)
            if existing is None:
                project_id = await repo.insert_project(
                    tx.conn, slug, name or slug, given["repo_path"], given["description"], tx.now
                )
                await tx.event(
                    "project.created",
                    actor,
                    project_id=project_id,
                    session_id=session_id,
                    project=slug,
                    name=name or slug,
                )
                created = True
            else:
                changes = {
                    k: v for k, v in given.items() if v is not None and v != getattr(existing, k)
                }
                if changes:
                    await repo.update_project(tx.conn, existing.id, changes, tx.now)
                    await tx.event(
                        "project.updated",
                        actor,
                        project_id=existing.id,
                        session_id=session_id,
                        project=slug,
                        changes=changes,
                    )
                created = False
            project = await self.get_project(slug, tx.conn)
        return project, created

    async def create_task(
        self,
        project: str,
        title: str,
        *,
        stage: str | None = None,
        waiting_on: str | None = None,
        next_step: str | None = None,
        branch: str | None = None,
        doc_path: str | None = None,
        stage_note: str | None = None,
        description: str | None = None,
        progress: str | None = None,
        priority: int = 0,
        tracker_ref: str | None = None,
        tracker_name: str | None = None,
        tracker_url: str | None = None,
        tracker_logged: bool = False,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "mcp",
    ) -> Task:
        fields: dict[str, Any] = {
            "title": _clean_title(title),
            "stage": parse_stage(stage) if stage else Stage.PLAN,
            "waiting_on": _clean_text(waiting_on, "waiting_on"),
            "next_step": _clean_text(next_step, "next_step"),
            "branch": _clean_text(branch, "branch"),
            "doc_path": _clean_text(doc_path, "doc_path"),
            "stage_note": _clean_text(stage_note, "stage_note"),
            "description": _clean_field("description", description),
            "progress": _clean_field("progress", progress),
            "priority": int(priority),
            "tracker_ref": _clean_field("tracker_ref", tracker_ref),
            "tracker_name": _clean_field("tracker_name", tracker_name),
            "tracker_url": _clean_field("tracker_url", tracker_url),
            "tracker_logged": _clean_field("tracker_logged", tracker_logged),
        }
        fields["stage"] = fields["stage"].value
        default_actor = actor
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            sess = await self._session(tx, session, session_title, hook_session)
            session_id = sess.id if sess else None
            actor = sess.name if sess else default_actor
            key = f"{proj.slug}-{await repo.next_task_number(tx.conn, proj.id)}"
            task_id = await repo.insert_task(tx.conn, proj.id, key, fields, session_id, tx.now)
            if fields["tracker_ref"]:
                await repo.insert_tracker_entry(
                    tx.conn,
                    task_id,
                    fields["tracker_name"],
                    fields["tracker_ref"],
                    fields["tracker_url"],
                    tx.now if fields["tracker_logged"] else None,
                    tx.now,
                )
            await tx.event(
                "task.created",
                actor,
                project_id=proj.id,
                task_id=task_id,
                session_id=session_id,
                project=proj.slug,
                key=key,
                title=fields["title"],
                stage=fields["stage"],
            )
            task = await self.get_task(task_id, tx.conn)
            if sess is not None:
                await self._bind(tx, task, sess, actor)
            return await self.get_task(task_id, tx.conn)

    async def update_task(
        self,
        ref: str | int,
        changes: dict[str, Any],
        *,
        expected_version: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "mcp",
    ) -> Task:
        """Patch fields; a value of None clears the field. Unchanged values are ignored."""
        saved = await self.update_task_checked(
            ref,
            changes,
            expected_version=expected_version,
            session=session,
            session_title=session_title,
            hook_session=hook_session,
            actor=actor,
        )
        return saved.task

    async def _stage_warnings(self, tx: _Tx, task: Task, diff: dict[str, Any]) -> list[str]:
        """Rule warnings for the stage move in `diff`; in strict mode an unmet rule refuses the
        write instead."""
        if "stage" not in diff:
            return []
        stage = diff["stage"]
        rules = await self._rules(tx.conn, await self.get_project(task.project_slug, tx.conn))
        requirements = rules.requirements(stage)
        if not requirements:
            return []
        reported = any(r["kind"] == REPORT_SINCE_STAGE_CHANGE for r in requirements) and (
            await repo.has_report_since(tx.conn, task.id, task.stage_changed_at)
        )
        values = {name: getattr(task, name) for name in FIELD_NAMES} | diff
        missing = unmet(rules, stage, report_since_stage_change=reported, values=values)
        if missing and self.rules_mode == "strict":
            raise ValidationError(refusal_text(stage, missing))
        return [warning_text(stage, req) for req in missing]

    async def update_task_checked(
        self,
        ref: str | int,
        changes: dict[str, Any],
        *,
        expected_version: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "mcp",
    ) -> Saved:
        """`update_task` that also returns the rule warnings of a stage move."""
        unknown = set(changes) - set(FIELD_NAMES)
        if unknown:
            raise ValidationError(
                f"Unknown task field(s): {', '.join(sorted(unknown))}."
                f" Allowed: {', '.join(FIELD_NAMES)}."
            )
        clean = {name: _clean_field(name, value) for name, value in changes.items()}
        default_actor = actor
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if expected_version is not None and expected_version != task.version:
                raise ConflictError(
                    f"Task {task.key} is at version {task.version}, not {expected_version}:"
                    " someone changed it meanwhile. Re-read it with task_get and retry."
                )
            diff = _task_diff(task, clean)
            if not diff:
                return Saved(task, [])
            warnings = await self._stage_warnings(tx, task, diff)
            # An explicit tracker_logged in the same write wins over the automatic unlog.
            unlog = (
                _unlog_reason(task, diff)
                if task.tracker_logged and "tracker_logged" not in clean
                else None
            )
            sess = await self._session(tx, session, session_title, hook_session)
            session_id = sess.id if sess else None
            actor = sess.name if sess else default_actor
            # The mark belongs to the issue: a new key starts not logged unless the write says so.
            new_issue = (
                "tracker_ref" in diff and "tracker_logged" not in clean and task.tracker_logged
            )
            writes = {**diff, "tracker_logged": False} if unlog or new_issue else diff
            await repo.update_task(tx.conn, task.id, writes, tx.now)
            await self._sync_tracker(tx, task, writes)
            ids = {"project_id": task.project_id, "task_id": task.id, "session_id": session_id}
            if "stage" in diff:
                await tx.event(
                    "task.stage_changed",
                    actor,
                    **ids,
                    project=task.project_slug,
                    key=task.key,
                    stage=diff["stage"],
                    **{"from": task.stage.value, "to": diff["stage"]},
                    note=diff.get("stage_note"),
                    **({"warnings": warnings} if warnings else {}),
                )
            # An automatic unlog has its own event; a new issue's reset is an ordinary change.
            others = {k: v for k, v in (diff if unlog else writes).items() if k != "stage"}
            if others:
                await tx.event(
                    "task.updated",
                    actor,
                    **ids,
                    project=task.project_slug,
                    key=task.key,
                    stage=diff.get("stage", task.stage.value),
                    changes=others,
                )
            if unlog:
                stage = diff.get("stage", task.stage.value)
                ref = writes.get("tracker_ref", task.tracker_ref)
                await self._unlogged(tx, task, actor, session_id, stage, ref, unlog)
            return Saved(await self.get_task(task.id, tx.conn), warnings)

    async def set_stage(
        self,
        ref: str | int,
        stage: str,
        *,
        note: str | None = None,
        next_step: str | None = None,
        waiting_on: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "mcp",
    ) -> Task:
        saved = await self.set_stage_checked(
            ref,
            stage,
            note=note,
            next_step=next_step,
            waiting_on=waiting_on,
            session=session,
            session_title=session_title,
            hook_session=hook_session,
            actor=actor,
        )
        return saved.task

    async def set_stage_checked(
        self,
        ref: str | int,
        stage: str,
        *,
        note: str | None = None,
        next_step: str | None = None,
        waiting_on: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "mcp",
    ) -> Saved:
        """Move the task; `next_step` / `waiting_on`, if given, are saved in the same
        transaction ('' clears them). Returns the task and the rule warnings of the move."""
        changes: dict[str, Any] = {"stage": stage}
        for name, value in (
            ("stage_note", note),
            ("next_step", next_step),
            ("waiting_on", waiting_on),
        ):
            if value is not None:
                changes[name] = value
        return await self.update_task_checked(
            ref,
            changes,
            session=session,
            session_title=session_title,
            hook_session=hook_session,
            actor=actor,
        )

    async def bind_session(
        self,
        ref: str | int,
        session: str,
        *,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> BindResult:
        """Bind a session to the task. A plain name never replaces a desktop session bound to
        the task (and its "Open in desktop" link): the binding is kept and the name becomes that
        session's alias, unless a plain session of that name already exists (aliases are global
        and would hijack it); then nothing is written."""
        name = _clean_text(session, "session", NAME_MAX)
        if name is None:
            raise ValidationError("session must not be empty.")
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if parse_session_id(name) is None:
                current_id = await repo.open_binding(tx.conn, task.id)
                current = await repo.get_session_by_id(tx.conn, current_id) if current_id else None
                if current is not None and current.external_id is not None:
                    taken = await repo.get_session_by_alias(tx.conn, name) is None and (
                        await repo.get_session_by_name(tx.conn, name) is not None
                    )
                    if taken:
                        return BindResult(task, current, alias_recorded=False)
                    await self._add_alias(tx, task, current, name)
                    cc_id = _clean_text(hook_session, "hook_session", CC_ID_MAX)
                    if cc_id is not None:
                        current = await self._link_hook_session(tx, current, cc_id)
                    return BindResult(task, current, alias_recorded=True)
            sess = await self._session(tx, name, session_title, hook_session)
            if sess is None:
                raise ValidationError("session must not be empty.")
            await self._bind(tx, task, sess, sess.name)
            return BindResult(await self.get_task(task.id, tx.conn), None, alias_recorded=False)

    async def _add_alias(self, tx: _Tx, task: Task, session: Session, name: str) -> None:
        previous = await repo.get_session_by_alias(tx.conn, name)
        await repo.touch_session(tx.conn, session.id, tx.now)
        if previous is not None and previous.id == session.id:
            return
        await repo.set_session_alias(tx.conn, name, session.id, tx.now)
        await tx.event(
            "session.alias_added",
            session.name,
            project_id=task.project_id,
            task_id=task.id,
            session_id=session.id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            alias=name,
            session=session.name,
            session_external_id=session.external_id,
            previous=previous.name if previous is not None else None,
        )

    async def unbind_session(
        self, ref: str | int, session: str | None = None, *, actor: str = "mcp"
    ) -> tuple[Task, Session | None]:
        """Close the task's current binding (history is kept) and clear its session.

        With `session`, the task must be bound to that session (desktop id or name), so a stale
        caller cannot unbind a session that took over meanwhile. Returns the unbound session, or
        None when nothing was bound (then nothing is written).
        """
        expected = _clean_text(session, "session", NAME_MAX)
        needle = (parse_session_id(expected) or expected) if expected else None
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            current_id = await repo.open_binding(tx.conn, task.id) or task.session_id
            if current_id is None:
                if needle is not None:
                    raise ConflictError(f"No session is bound to {task.key}; nothing changed.")
                return task, None
            current = await repo.get_session_by_id(tx.conn, current_id)
            if current is None:
                raise NotFoundError(f"Session #{current_id} bound to {task.key} not found.")
            aliased = await repo.get_session_by_alias(tx.conn, needle) if needle else None
            if (
                needle is not None
                and needle not in (current.external_id, current.name)
                and (aliased is None or aliased.id != current.id)
            ):
                raise ConflictError(
                    f"{task.key} is bound to {current.name}, not {expected}; nothing changed."
                    " Re-read the task with task_get."
                )
            await repo.unbind(tx.conn, task.id, tx.now)
            if task.session_id is not None:
                await repo.update_task(tx.conn, task.id, {"session_id": None}, tx.now)
            await tx.event(
                "task.session_unbound",
                actor,
                project_id=task.project_id,
                task_id=task.id,
                session_id=current.id,
                project=task.project_slug,
                key=task.key,
                stage=task.stage.value,
                session=current.name,
                session_external_id=current.external_id,
            )
            return await self.get_task(task.id, tx.conn), current

    async def add_report(
        self,
        ref: str | int,
        body: str,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        author: str | None = None,
        progress: str | None = None,
        next_step: str | None = None,
        waiting_on: str | None = None,
    ) -> tuple[Task, Report]:
        """Append a report; `progress`, `next_step` and `waiting_on`, if given, are saved in the
        same transaction ('' clears them)."""
        body = (body or "").strip()
        if not body:
            raise ValidationError("Report body must not be empty.")
        state, size, summary = parse_report(body)
        given = {"progress": progress, "next_step": next_step, "waiting_on": waiting_on}
        clean = {k: _clean_field(k, v) for k, v in given.items() if v is not None}
        default_actor = _clean_text(author, "author", NAME_MAX) or "mcp"
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            sess = await self._session(tx, session, session_title, hook_session)
            session_id = sess.id if sess else None
            actor = sess.name if sess else default_actor
            diff = _task_diff(task, clean)
            unlog = None
            if task.tracker_logged:
                unlog = ", ".join(x for x in ("report added", _unlog_reason(task, diff)) if x)
            writes = {**diff, "tracker_logged": False} if unlog else diff
            if writes:
                await repo.update_task(tx.conn, task.id, writes, tx.now)
                await self._sync_tracker(tx, task, writes)
            if diff:
                await tx.event(
                    "task.updated",
                    actor,
                    project_id=task.project_id,
                    task_id=task.id,
                    session_id=session_id,
                    project=task.project_slug,
                    key=task.key,
                    stage=task.stage.value,
                    changes=diff,
                )
            if unlog:
                await self._unlogged(
                    tx, task, actor, session_id, task.stage.value, task.tracker_ref, unlog
                )
            report_id = await repo.insert_report(
                tx.conn, task.id, session_id, actor, state, size, summary, body, tx.now
            )
            await tx.event(
                "report.added",
                actor,
                project_id=task.project_id,
                task_id=task.id,
                session_id=session_id,
                project=task.project_slug,
                key=task.key,
                stage=task.stage.value,
                report_id=report_id,
                summary=summary,
                state=state,
                size=size,
            )
            report = (await repo.list_reports(tx.conn, task.id, 1))[0]
            task = await self.get_task(task.id, tx.conn)
        return task, report

    async def add_decision(
        self,
        project: str,
        text: str,
        *,
        task: str | int | None = None,
        decided_by: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Decision:
        clean = _clean_text(text, "text")
        if not clean:
            raise ValidationError("Decision text must not be empty.")
        decided_by = _clean_text(decided_by, "decided_by", NAME_MAX)
        default_actor = decided_by or "mcp"
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            task_row = await self.get_task(task, tx.conn) if task is not None else None
            if task_row is not None and task_row.project_id != proj.id:
                raise ValidationError(f"Task {task_row.key} does not belong to {proj.slug}.")
            sess = await self._session(tx, session, session_title, hook_session)
            session_id = sess.id if sess else None
            actor = sess.name if sess else default_actor
            decision_id = await repo.insert_decision(
                tx.conn, proj.id, task_row.id if task_row else None, clean, decided_by, tx.now
            )
            await tx.event(
                "decision.added",
                actor,
                project_id=proj.id,
                task_id=task_row.id if task_row else None,
                session_id=session_id,
                project=proj.slug,
                key=task_row.key if task_row else None,
                stage=task_row.stage.value if task_row else None,
                decision_id=decision_id,
                text=clean[:200],
            )
            decisions = await repo.list_decisions(tx.conn, project_id=proj.id, limit=1)
        return decisions[0]
