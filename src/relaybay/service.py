"""Use cases. The only writer of the database: state change + event row in one transaction,
then the committed events are published on the in-process bus."""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import aiosqlite

from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
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
from relaybay.domain.sessions import DEFAULT_DESKTOP_LINK_TEMPLATE, parse_session_id
from relaybay.domain.stages import OPEN_STAGES, STAGES, Stage, parse_stage
from relaybay.domain.task_fields import BY_NAME, FIELD_NAMES, NAME_MAX, TEXT_MAX, TITLE_MAX
from relaybay.events import EventBus

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
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
    ) -> None:
        if "{id}" not in desktop_link_template:
            raise ValueError("desktop_link_template must contain '{id}'.")
        self.db_path = db_path
        self.bus = bus or EventBus()
        self.desktop_link_template = desktop_link_template
        self._write_conn: aiosqlite.Connection | None = None
        self._read_conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_conn = await connect(self.db_path)
        await migrate(self._write_conn)
        self._read_conn = await connect(self.db_path)

    async def close(self) -> None:
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

    async def _session(self, tx: _Tx, value: str | None, title: str | None) -> Session | None:
        """Upsert the calling session: a desktop id (`local_<uuid>`) or a plain name."""
        value = _clean_text(value, "session", NAME_MAX)
        if value is None:
            return None
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
            sess = await self._session(tx, session, session_title)
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
            sess = await self._session(tx, session, session_title)
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
        actor: str = "mcp",
    ) -> Task:
        """Patch fields; a value of None clears the field. Unchanged values are ignored."""
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
                return task
            # An explicit tracker_logged in the same write wins over the automatic unlog.
            unlog = (
                _unlog_reason(task, diff)
                if task.tracker_logged and "tracker_logged" not in clean
                else None
            )
            sess = await self._session(tx, session, session_title)
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
            return await self.get_task(task.id, tx.conn)

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
        actor: str = "mcp",
    ) -> Task:
        """Move the task; `next_step` / `waiting_on`, if given, are saved in the same
        transaction ('' clears them)."""
        changes: dict[str, Any] = {"stage": stage}
        for name, value in (
            ("stage_note", note),
            ("next_step", next_step),
            ("waiting_on", waiting_on),
        ):
            if value is not None:
                changes[name] = value
        return await self.update_task(
            ref, changes, session=session, session_title=session_title, actor=actor
        )

    async def bind_session(
        self, ref: str | int, session: str, *, session_title: str | None = None
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
                    return BindResult(task, current, alias_recorded=True)
            sess = await self._session(tx, name, session_title)
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
            sess = await self._session(tx, session, session_title)
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
            sess = await self._session(tx, session, session_title)
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
