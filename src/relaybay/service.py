"""Use cases. The only writer of the database: state change + event row in one transaction,
then the committed events are published on the in-process bus."""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import aiosqlite

from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain import comments as comment_text
from relaybay.domain import environments as envs
from relaybay.domain import guard
from relaybay.domain.comments import COMMENT_MAX, STOP_BLOCK_CAP, STOP_COMMENT_CAP
from relaybay.domain.environments import (
    DEFAULT_LEVELS,
    GUARD_MODES,
    KINDS,
    LEVELS,
    Environment,
    PolicyProposal,
    Proposal,
    SecretRules,
)
from relaybay.domain.errors import ConfirmationNeeded, ConflictError, NotFoundError, ValidationError
from relaybay.domain.hooks import (
    CC_ID_MAX,
    DELIVERY_EVENTS,
    HookEvent,
    folder_name,
    parse_hook,
    session_start_context,
)
from relaybay.domain.models import (
    Binding,
    Comment,
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
from relaybay.policy import (
    DEFAULT_PRESET,
    EffectivePolicy,
    Layer,
    PolicyError,
    PolicyFile,
    apply_patch,
    config_layer,
    guard_weakened,
    parse_layer,
    preset,
    validate_layer,
    widened_on_live,
)

logger = logging.getLogger("relaybay")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOOK_ACTIVITY_WINDOW = 5.0  # seconds: at most one activity event per session
HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)  # use fullmatch
LIVE_ACTION = "live.action"
JOURNAL_COMMAND_MAX = 200


# Lines a write wants its caller to see without failing (e.g. a hook_session that was not
# linked); collected per MCP tool call by `collect_notes`.
_NOTES: ContextVar[list[str] | None] = ContextVar("relaybay_notes", default=None)


@contextlib.contextmanager
def collect_notes() -> Iterator[list[str]]:
    notes: list[str] = []
    token = _NOTES.set(notes)
    try:
        yield notes
    finally:
        _NOTES.reset(token)


def _note(text: str) -> None:
    notes = _NOTES.get()
    if notes is not None and text not in notes:
        notes.append(text)


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
    comments: list[Comment] = field(default_factory=list)
    live_actions: list[Event] = field(default_factory=list)


@dataclass(slots=True)
class Inbox:
    """What needs the owner: tasks waiting on them, tasks whose last stage move missed a rule,
    done tasks without the work-log mark (with `require_tracker_log`), unlinked hook sessions,
    tasks with agent replies the owner has not marked read."""

    waiting: list[Task]
    rule_warnings: list[tuple[Task, list[str]]]
    not_logged: list[Task]
    unlinked: list[Session]
    replies: list[tuple[Task, list[Comment]]] = field(default_factory=list)
    proposals: list[Proposal] = field(default_factory=list)
    policy_proposals: list[PolicyProposal] = field(default_factory=list)

    @property
    def count(self) -> int:
        return (
            len(self.waiting)
            + len(self.rule_warnings)
            + len(self.not_logged)
            + len(self.unlinked)
            + len(self.replies)
            + len(self.proposals)
            + len(self.policy_proposals)
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
    environments: list[Environment] = field(default_factory=list)
    policies: dict[int, EffectivePolicy] = field(default_factory=dict)  # by environment id


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


def _context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def _clean_comment(body: Any) -> str:
    text = _clean_text(body, "Comment", COMMENT_MAX)
    if not text:
        raise ValidationError("Comment must not be empty.")
    return text


def _clean_environment(values: dict[str, Any], secrets: SecretRules) -> dict[str, Any]:
    """The given environment fields, validated; secret values are refused."""
    unknown = sorted(set(values) - set(ENVIRONMENT_FIELDS))
    if unknown:
        raise ValidationError(f"Unknown environment field(s): {', '.join(unknown)}.")
    clean: dict[str, Any] = {}
    if "name" in values:
        clean["name"] = envs.clean_name(values["name"])
    if "kind" in values:
        clean["kind"] = envs.clean_kind(values["kind"])
    if "base_url" in values:
        clean["base_url"] = envs.clean_base_url(values["base_url"], secrets)
    if "sign_in_allowed" in values:
        clean["sign_in_allowed"] = envs.clean_bool(values["sign_in_allowed"], "sign_in_allowed")
    if "login_state_path" in values:
        clean["login_state_path"] = envs.clean_path(
            values["login_state_path"], "login_state_path", secrets
        )
    if "notes" in values:
        clean["notes"] = envs.clean_notes(values["notes"], secrets)
    if "level" in values:
        clean["level"] = envs.clean_level(values["level"])
    if "overrides" in values:
        clean["overrides"] = envs.clean_overrides(values["overrides"])
    return clean


ENVIRONMENT_FIELDS = (
    "name",
    "kind",
    "base_url",
    "sign_in_allowed",
    "login_state_path",
    "notes",
    "level",
    "overrides",
)


def guard_command(call: guard.ToolCall, test_secrets: list[str] | None = None) -> str:
    """The journal's short, redacted summary of a tool call; known test secrets are masked
    too."""
    text = call.text
    for value in sorted(test_secrets or [], key=len, reverse=True):
        text = text.replace(value, "***")
    text = " ".join(envs.redact(text).split())
    if len(text) > JOURNAL_COMMAND_MAX:
        text = text[: JOURNAL_COMMAND_MAX - 1].rstrip() + "…"
    return text


POLICY_SCOPE_HINT = "scope is 'global', a project slug, or '<project>/<environment>'"


def _patch_summary(patch: dict[str, Any], prefix: str = "") -> list[str]:
    paths = []
    for key, value in patch.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            paths.extend(_patch_summary(value, f"{path}."))
        else:
            paths.append(path)
    return paths


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
        permission_defaults: dict[str, str] | None = None,
        guard_mode: str | None = None,
        policy_file: Path | None = None,
    ) -> None:
        """`permission_defaults` and `guard_mode` are the legacy `config.toml` keys: a policy
        layer between the preset and the global `policy_file` (only what was set)."""
        if "{id}" not in desktop_link_template:
            raise ValueError("desktop_link_template must contain '{id}'.")
        if guard_mode is not None and guard_mode not in GUARD_MODES:
            raise ValueError(f"guard_mode must be one of: {', '.join(GUARD_MODES)}.")
        defaults = {**DEFAULT_LEVELS, **(permission_defaults or {})}
        if set(defaults) != set(KINDS) or not set(defaults.values()) <= set(LEVELS):
            raise ValueError(
                f"permission_defaults maps {', '.join(KINDS)} to one of: {', '.join(LEVELS)}."
            )
        self._config_policy = config_layer(permission_defaults, guard_mode)
        self.policy_file = PolicyFile(policy_file)
        self._logged_policy_errors: set[str] = set()
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
        # Per session: Stop blocks in the current turn; the newest wake-up waiter's number.
        self._stop_blocks: dict[int, int] = {}
        self._waiters: dict[int, int] = {}
        self._waiter_seq = 0

    async def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_conn = await connect(self.db_path)
        await migrate(self._write_conn)
        self._read_conn = await connect(self.db_path)
        # A broken project override is reported now; it never stops the server or the project.
        for slug, body in await repo.list_project_rules(self._read_conn):
            self._merged_rules(slug, body)
        # A broken global policy stops the start; broken project layers are only logged.
        self.policy_file.load()
        for row in await repo.list_policy_layers(self._read_conn):
            self._db_layer(row)

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
        if session.cc_session_id == cc_id and session.cc_exact:
            return session
        changes: dict[str, Any] = {"cc_session_id": cc_id, "cc_exact": 1, "unlinked_reason": None}
        other = await repo.get_session_by_cc(tx.conn, cc_id)
        if (
            other is not None
            and other.id != session.id
            and other.cc_exact
            and other.status != "ended"
        ):
            # Another live session's agent linked this id itself: a write naming the wrong id
            # (or a coordinator passing its own) must not move that session's comments here.
            _note(
                f"Warning: hook_session {cc_id} belongs to another session ({other.name}); not"
                " relinked. Pass your own hook session id."
            )
            return session
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
                    {"cc_session_id": None, "cc_exact": 0, "unlinked_reason": None, "status": None},
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
                    {"cc_session_id": hook.cc_session_id, "cc_exact": 0, "cwd": hook.cwd},
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
                changes = {"cc_session_id": hook.cc_session_id, "cc_exact": 0, "cwd": hook.cwd}
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

    # --- comment delivery through hooks ------------------------------------

    async def _exact_session(self, cc_session_id: str) -> Session | None:
        """The session a hook event belongs to, only when the agent linked it itself."""
        session = await repo.get_session_by_cc(self._r, cc_session_id)
        if session is None or not session.cc_exact or session.status == "ended":
            return None
        return session

    async def _deliver(self, tx: _Tx, session: Session, comments: list[Comment]) -> None:
        await repo.mark_comments_delivered(tx.conn, [c.id for c in comments], session.id, tx.now)
        tasks = {t.id: t for t in await repo.open_tasks_of_session(tx.conn, session.id)}
        for comment in comments:
            task = tasks[comment.task_id]
            await tx.event(
                "comment.delivered",
                session.name,
                project_id=task.project_id,
                task_id=task.id,
                session_id=session.id,
                project=task.project_slug,
                key=task.key,
                stage=task.stage.value,
                comment_id=comment.id,
                session=session.name,
            )

    async def _take_undelivered(self, session: Session) -> tuple[list[Comment], int]:
        """Mark the oldest undelivered owner comments that fit in one hook answer as delivered
        to `session`; returns them and how many were waiting."""
        if not await repo.undelivered_owner_comments(self._r, session.id):
            return [], 0  # the common case: no write transaction per tool call
        async with self._tx() as tx:
            waiting = await repo.undelivered_owner_comments(tx.conn, session.id)
            shown = comment_text.fit(waiting)
            if shown:
                await self._deliver(tx, session, shown)
        return shown, len(waiting)

    async def hook_answer(self, payload: Any) -> dict[str, Any] | None:
        """The JSON a hook event is answered with, or None for an empty answer. Comment text goes
        only to the hook session its agent linked itself and only for the tasks bound to it."""
        hook = parse_hook(payload)
        if hook is None:
            return None
        session = await self._exact_session(hook.cc_session_id)
        if hook.event == "SessionStart":
            context = session_start_context(hook.cc_session_id)
            unread = await repo.unread_owner_count(self._r, session.id) if session else 0
            if unread:
                context += " " + comment_text.unread_line(unread)
            return _context("SessionStart", context)
        if hook.event == "PreToolUse":
            return await self._pre_tool_answer(payload, hook, session)
        if session is None:
            return None
        if hook.event == "UserPromptSubmit":
            self._stop_blocks.pop(session.id, None)  # a new turn
        if hook.event in DELIVERY_EVENTS:
            shown, total = await self._take_undelivered(session)
            if shown:
                return _context(hook.event, comment_text.delivery_text(shown, total))
            return None
        if hook.event == "Stop":
            return await self._stop_answer(session, hook.stop_hook_active)
        return None

    async def _pre_tool_answer(
        self, payload: Any, hook: HookEvent, exact: Session | None
    ) -> dict[str, Any] | None:
        """PreToolUse: the guard's decision plus owner comments for an exactly linked session.
        The guard never stands in the way through a failure of its own: it then says nothing."""
        try:
            touch, warnings = await self._guard(payload, hook)
        except Exception:
            logger.exception("relaybay guard failed; the tool call goes on")
            touch, warnings = None, []
        contexts = list(warnings)
        if exact is not None:
            shown, total = await self._take_undelivered(exact)
            if shown:
                contexts.append(comment_text.delivery_text(shown, total))
        out: dict[str, Any] = {"hookEventName": "PreToolUse"}
        if touch is not None:
            # Never "allow": that would skip Claude Code's own permission prompt.
            out["permissionDecision"] = touch.decision
            out["permissionDecisionReason"] = touch.reason
        if contexts:
            out["additionalContext"] = "\n".join(contexts)
        return {"hookSpecificOutput": out} if len(out) > 1 else None

    async def _guard_projects(self, session: Session | None, cwd: str | None) -> list[int]:
        """Projects whose environments apply to a session: its own project and those of the
        tasks bound to it; without either, the project of its working directory."""
        ids: list[int] = []
        if session is not None:
            if session.project_id is not None:
                ids.append(session.project_id)
            ids += [t.project_id for t in await repo.open_tasks_of_session(self._r, session.id)]
        if not ids:
            project, _ = self._project_for_cwd(await repo.projects_with_repo_path(self._r), cwd)
            if project is not None:
                ids.append(project.id)
        return list(dict.fromkeys(ids))

    async def _guard(self, payload: Any, hook: HookEvent) -> tuple[guard.Touch | None, list[str]]:
        """The strongest deny / ask decision for a PreToolUse call and the warnings of warn
        mode. Calls that touch a live environment are written to the journal (`live.action`),
        in `log` mode too. Levels, mode and rules come from each environment's policy."""
        call = guard.tool_call(payload)
        if call is None or not await repo.has_environments(self._r):
            return None, []
        session = await repo.get_session_by_cc(self._r, hook.cc_session_id)
        project_ids = await self._guard_projects(session, hook.cwd)
        environments = await repo.list_environments(self._r, project_ids)
        policies = await self._policies(self._r, environments)
        touches = guard.check(call, environments, lambda env: policies[env.id])
        if not touches:
            return None, []
        live = [t for t in touches if t.environment.kind == "live"]
        if live:
            secrets = [v for e in environments for a in e.access for v in a.test_secrets.values()]
            await self._journal(session, call, live, secrets)
        top = guard.strongest(touches)
        if top is not None and top.decision in ("deny", "ask"):
            return top, []
        warnings = [f"{t.reason} (warn mode: not blocked)" for t in touches if t.decision == "warn"]
        return None, warnings

    async def _journal(
        self,
        session: Session | None,
        call: guard.ToolCall,
        touches: list[guard.Touch],
        test_secrets: list[str],
    ) -> None:
        actor = session.name if session else "hook"
        command = guard_command(call, test_secrets)
        async with self._tx() as tx:
            bound = await repo.open_tasks_of_session(tx.conn, session.id) if session else []
            for touch in touches:
                env = touch.environment
                task = next((t for t in bound if t.project_id == env.project_id), None)
                await tx.event(
                    LIVE_ACTION,
                    actor,
                    project_id=env.project_id,
                    task_id=task.id if task else None,
                    session_id=session.id if session else None,
                    project=env.project_slug,
                    **({"key": task.key, "stage": task.stage.value} if task else {}),
                    environment=env.name,
                    tool=call.tool,
                    command=command,
                    decision=touch.decision,
                    level=touch.level,
                    rule=touch.mutating,
                )

    async def _stop_candidates(
        self, conn: aiosqlite.Connection, session: Session
    ) -> tuple[list[Comment], int]:
        """Owner comments a Stop may block for, oldest first: delivered to this session and not
        acknowledged, or not delivered to it yet; each at most STOP_COMMENT_CAP times in all.
        Also returns how many unread ones there are, capped or not."""
        unread = await repo.delivered_unread_comments(conn, session.id)
        new = await repo.undelivered_owner_comments(conn, session.id)
        every = sorted(unread + new, key=lambda c: c.id)
        return [c for c in every if c.stop_blocks < STOP_COMMENT_CAP], len(every)

    async def _stop_answer(self, session: Session, stop_hook_active: bool) -> dict[str, Any] | None:
        """Block the end of the turn while owner comments are unacknowledged: at most
        STOP_BLOCK_CAP times per turn and STOP_COMMENT_CAP times per comment, and never on top
        of another hook's continuation (`stop_hook_active` before relaybay blocked in this
        turn). After that the SessionStart line and comments_list remind of them."""
        blocks = self._stop_blocks.get(session.id, 0)
        allowed = blocks < STOP_BLOCK_CAP and (blocks > 0 or not stop_hook_active)
        if not allowed or not (await self._stop_candidates(self._r, session))[0]:
            self._stop_blocks.pop(session.id, None)  # the turn ends
            return None
        async with self._tx() as tx:
            candidates, total = await self._stop_candidates(tx.conn, session)
            shown = comment_text.fit(candidates)
            fresh = [c for c in shown if c.delivered_to_session_id != session.id]
            if fresh:
                await self._deliver(tx, session, fresh)
            if shown:
                await repo.count_stop_blocks(tx.conn, [c.id for c in shown])
        if not shown:
            self._stop_blocks.pop(session.id, None)
            return None
        self._stop_blocks[session.id] = blocks + 1
        return {"decision": "block", "reason": comment_text.stop_reason(shown, total)}

    async def wait_for_comments(
        self,
        payload: Any,
        *,
        hold: float,
        poll: float = 1.0,
        gone: Callable[[], Awaitable[bool]] | None = None,
    ) -> str | None:
        """The wake-up hook's long poll: wait up to `hold` seconds until an owner comment arrives
        for the idle, exactly linked session of `payload`, mark it delivered and return the
        text for the agent. None when the time is up, the session is not idle (its own hooks
        deliver then), a newer wait for the same session started, or `gone()` says the hook
        process went away."""
        cc_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(cc_id, str) or not cc_id.strip():
            return None
        session = await self._exact_session(cc_id.strip()[:CC_ID_MAX])
        if session is None:
            return None
        self._waiter_seq += 1
        me = self._waiters[session.id] = self._waiter_seq
        deadline = time.monotonic() + hold
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(poll)
                if self._waiters.get(session.id) != me or (gone is not None and await gone()):
                    return None
                current = await self._exact_session(cc_id.strip()[:CC_ID_MAX])
                if current is None or current.id != session.id:
                    return None
                # Not while a turn goes on: a Stop block of this turn means it continues.
                if current.status != "idle" or session.id in self._stop_blocks:
                    continue
                shown, total = await self._take_undelivered(current)
                if shown:
                    return comment_text.delivery_text(shown, total)
            return None
        finally:
            if self._waiters.get(session.id) == me:
                del self._waiters[session.id]

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
        by_id = {t.id: t for t in tasks}
        replies: dict[int, list[Comment]] = {}
        for reply in await repo.unread_agent_replies(self._r):
            replies.setdefault(reply.task_id, []).append(reply)
        return Inbox(
            waiting,
            warned,
            not_logged,
            await self.unlinked_hook_sessions(),
            [(by_id[task_id], items) for task_id, items in replies.items() if task_id in by_id],
            await repo.list_proposals(self._r),
            await repo.list_policy_proposals(self._r),
        )

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
            comments=await repo.list_comments(self._r, task.id),
            live_actions=await repo.task_events(self._r, task.id, LIVE_ACTION, 20),
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
        environments = await repo.list_environments(self._r, [project.id])
        return Snapshot(
            project=project,
            tasks=tasks,
            last_reports=await self.last_reports(tasks),
            decisions=await repo.list_decisions(self._r, project_id=project.id, limit=10),
            rules=await self._rules(self._r, project),
            environments=environments,
            policies=await self._policies(self._r, environments),
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
    ) -> BindResult:
        """Bind a session to the task. A plain name never replaces a desktop session bound to
        the task (and its "Open in desktop" link): the binding is kept and the name becomes that
        session's alias, unless a plain session of that name already exists (aliases are global
        and would hijack it); then nothing is written. No hook session is linked here: the
        caller may be binding someone else (a coordinator), so only the agent's own writes link
        its `hook_session`."""
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

    # --- comments ----------------------------------------------------------

    async def _read(
        self,
        tx: _Tx,
        task: Task,
        comments: list[Comment],
        actor: str,
        session_id: int | None,
    ) -> list[int]:
        ids = [c.id for c in comments if c.read_at is None]
        if not ids:
            return []
        await repo.mark_comments_read(tx.conn, ids, tx.now)
        for comment_id in ids:
            await tx.event(
                "comment.read",
                actor,
                project_id=task.project_id,
                task_id=task.id,
                session_id=session_id,
                project=task.project_slug,
                key=task.key,
                stage=task.stage.value,
                comment_id=comment_id,
            )
        return ids

    async def _insert_comment(
        self,
        tx: _Tx,
        task: Task,
        *,
        author_kind: str,
        author: str,
        session_id: int | None,
        body: str,
        in_reply_to: int | None,
    ) -> Comment:
        comment_id = await repo.insert_comment(
            tx.conn,
            task_id=task.id,
            author_kind=author_kind,
            author=author,
            session_id=session_id,
            body_md=body,
            in_reply_to=in_reply_to,
            now=tx.now,
        )
        await tx.event(
            "comment.added",
            author,
            project_id=task.project_id,
            task_id=task.id,
            session_id=session_id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            comment_id=comment_id,
            author_kind=author_kind,
            in_reply_to=in_reply_to,
            excerpt=" ".join(body.split())[:200],
        )
        comment = await repo.get_comment(tx.conn, comment_id)
        assert comment is not None
        return comment

    async def _unread(self, tx: _Tx, task: Task, from_owner: bool) -> list[Comment]:
        unread = await repo.list_comments(tx.conn, task.id, unread_only=True)
        return [c for c in unread if c.from_owner == from_owner]

    async def add_owner_comment(
        self, ref: str | int, body: str, *, actor: str = "owner"
    ) -> Comment:
        """A comment from the board for the agent of the bound session. Writing one also marks
        the agent's replies on the task read: the owner has seen the thread."""
        text = _clean_comment(body)
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            await self._read(tx, task, await self._unread(tx, task, False), actor, None)
            return await self._insert_comment(
                tx,
                task,
                author_kind="owner",
                author=actor,
                session_id=None,
                body=text,
                in_reply_to=None,
            )

    async def add_agent_comment(
        self,
        ref: str | int,
        body: str,
        *,
        in_reply_to: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Comment, list[int]]:
        """An agent's reply. It marks the owner comment it answers read (`in_reply_to`), or
        every unread owner comment of the task. Returns the reply and the ids marked read."""
        text = _clean_comment(body)
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            session_id = sess.id if sess else None
            unread = await self._unread(tx, task, True)
            if in_reply_to is not None:
                target = await repo.get_comment(tx.conn, in_reply_to)
                if target is None or target.task_id != task.id:
                    raise NotFoundError(f"Comment #{in_reply_to} not found on {task.key}.")
                unread = [c for c in unread if c.id == in_reply_to]
            read = await self._read(tx, task, unread, actor, session_id)
            comment = await self._insert_comment(
                tx,
                task,
                author_kind="agent",
                author=actor,
                session_id=session_id,
                body=text,
                in_reply_to=in_reply_to,
            )
        return comment, read

    async def ack_comments(
        self,
        *,
        comment_id: int | None = None,
        task: str | int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Task, list[int]]:
        """Mark owner comments read: one by id, or every unread one of a task. Returns the task
        and the ids that were unread."""
        async with self._tx() as tx:
            if comment_id is not None:
                comment = await repo.get_comment(tx.conn, comment_id)
                if comment is None or not comment.from_owner:
                    raise NotFoundError(f"Owner comment #{comment_id} not found.")
                row = await self.get_task(comment.task_id, tx.conn)
                if task is not None and (await self.get_task(task, tx.conn)).id != row.id:
                    raise ValidationError(f"Comment #{comment_id} is not on {task}.")
                targets = [comment]
            elif task is not None:
                row = await self.get_task(task, tx.conn)
                targets = await self._unread(tx, row, True)
            else:
                raise ValidationError("Pass comment_id or task.")
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            read = await self._read(tx, row, targets, actor, sess.id if sess else None)
        return row, read

    async def mark_replies_read(self, ref: str | int, *, actor: str = "owner") -> list[int]:
        """The owner has read the agent replies on the task."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            return await self._read(tx, task, await self._unread(tx, task, False), actor, None)

    async def list_comments(
        self, ref: str | int, *, unread_only: bool = False
    ) -> tuple[Task, list[Comment]]:
        task = await self.get_task(ref)
        return task, await repo.list_comments(self._r, task.id, unread_only=unread_only)

    # --- environments ------------------------------------------------------

    async def environments(self, project: str) -> tuple[Project, list[Environment]]:
        proj = await self.get_project(project)
        return proj, await repo.list_environments(self._r, [proj.id])

    async def proposals(self, project: str | None = None) -> list[Proposal]:
        """Pending environment proposals, oldest first."""
        project_id = (await self.get_project(project)).id if project else None
        return await repo.list_proposals(self._r, project_id)

    async def live_journal(self, project: str | None = None, limit: int = 20) -> list[Event]:
        """The newest agent actions on live environments (`live.action` events)."""
        return await self.activity(project=project, kind=LIVE_ACTION, limit=limit)

    @staticmethod
    def _level(
        policy: EffectivePolicy,
        level: str | None,
        overrides: dict[str, str],
        kind: str,
        cap: str | None,
    ) -> str:
        if cap is not None and cap in overrides:
            return overrides[cap]
        return level or policy.level(kind, cap)

    async def _write_environment(
        self,
        tx: _Tx,
        project: Project,
        current: Environment | None,
        values: dict[str, Any],
        actor: str,
        confirm_live: bool,
    ) -> Environment:
        """Create or change an environment from `values`. Raising live above read (explicitly,
        not through the policy's levels) needs `confirm_live`."""
        pol = await self._policy_of(tx.conn, project, current)
        clean = _clean_environment(values, pol.secret_rules)
        name = clean.get("name", current.name if current else None)
        kind = clean.get("kind", current.kind if current else None)
        if name is None or kind is None:
            raise ValidationError("A new environment needs a name and a kind (local, test, live).")
        if kind == "live" and current is not None and any(a.test_secrets for a in current.access):
            raise ValidationError(
                f"{current.name} holds test secrets, which are never allowed on live; remove"
                " them from its access entries first."
            )
        other = await repo.find_environment(tx.conn, project.id, name)
        if other is not None and (current is None or other.id != current.id):
            raise ValidationError(f"{project.slug} already has an environment named {name!r}.")
        overrides = dict(current.overrides) if current else {}
        for cap, value in clean.get("overrides", {}).items():
            if value is None:
                overrides.pop(cap, None)
            else:
                overrides[cap] = value
        level = clean.get("level", current.level if current else None)
        sign_in = clean.get(
            "sign_in_allowed", current.sign_in_allowed if current else kind != "live"
        )
        raised = []
        was_live = current is not None and current.kind == "live"
        # What live gets from the project's policy alone: the baseline for an environment that
        # becomes live, so an environment layer staged while it was test cannot skip the
        # confirmation (its levels and guard settings were never confirmed for live).
        base = await self._policy_of(tx.conn, project, None)
        for cap in (None, *envs.CAPABILITIES):
            after = self._level(pol, level, overrides, kind, cap)
            if was_live:
                # Staying live, or leaving it: the effective levels, the policy's included (a
                # live environment at read turned into test at full is a raise).
                assert current is not None
                before = self._level(pol, current.level, current.overrides, current.kind, cap)
            elif kind == "live":
                before = self._level(base, None, {}, "live", cap)
            else:
                break
            if envs.raises_live("live", before, after):
                raised.append(f"{cap or 'level'} {after}")
        if kind == "live" and not was_live:
            raised += guard_weakened(base, pol)
        if raised and not confirm_live:
            raise ConfirmationNeeded(
                f"Raising {project.slug} {name} (live) above read ({', '.join(raised)}) lets"
                " agents change live without asking. Confirm to save."
            )
        row = {
            "name": name,
            "kind": kind,
            "base_url": clean.get("base_url", current.base_url if current else None),
            "sign_in_allowed": bool(sign_in) and kind != "live",
            "login_state_path": clean.get(
                "login_state_path", current.login_state_path if current else None
            ),
            "notes": clean.get("notes", current.notes if current else None),
            "level": level,
            "overrides": overrides,
        }
        payload = {"project": project.slug, "environment": name, "env_kind": kind}
        if current is None:
            env_id = await repo.insert_environment(tx.conn, project.id, row, actor, tx.now)
            await tx.event("environment.created", actor, project_id=project.id, **payload)
        else:
            env_id = current.id
            changes = {k: v for k, v in row.items() if v != getattr(current, k)}
            if changes:
                await repo.update_environment(tx.conn, env_id, changes, actor, tx.now)
                await tx.event(
                    "environment.updated",
                    actor,
                    project_id=project.id,
                    **payload,
                    changes=sorted(changes),
                )
        saved = await repo.get_environment(tx.conn, env_id)
        assert saved is not None
        return saved

    async def _project_environment(
        self, conn: aiosqlite.Connection, project: str, env_id: int
    ) -> tuple[Project, Environment]:
        proj = await self.get_project(project, conn)
        env = await repo.get_environment(conn, env_id)
        if env is None or env.project_id != proj.id:
            raise NotFoundError(f"Environment #{env_id} not found in {proj.slug}.")
        return proj, env

    async def save_environment(
        self,
        project: str,
        values: dict[str, Any],
        *,
        env_id: int | None = None,
        confirm_live: bool = False,
        actor: str = "board",
    ) -> Environment:
        """Create (`env_id` None) or change an environment. Board only: agents propose."""
        async with self._tx() as tx:
            if env_id is None:
                proj, current = await self.get_project(project, tx.conn), None
            else:
                proj, current = await self._project_environment(tx.conn, project, env_id)
            return await self._write_environment(tx, proj, current, values, actor, confirm_live)

    async def delete_environment(self, project: str, env_id: int, *, actor: str = "board") -> None:
        async with self._tx() as tx:
            proj, env = await self._project_environment(tx.conn, project, env_id)
            await repo.delete_environment(tx.conn, env.id)
            await tx.event(
                "environment.deleted",
                actor,
                project_id=proj.id,
                project=proj.slug,
                environment=env.name,
                env_kind=env.kind,
            )

    async def _access_event(
        self, tx: _Tx, kind: str, actor: str, proj: Project, env: Environment, **payload: Any
    ) -> None:
        await repo.update_environment(tx.conn, env.id, {}, actor, tx.now)
        await tx.event(
            kind,
            actor,
            project_id=proj.id,
            project=proj.slug,
            environment=env.name,
            env_kind=env.kind,
            **payload,
        )

    async def save_access(
        self,
        project: str,
        env_id: int,
        kind: str,
        fields: dict[str, Any],
        *,
        test_secrets: dict[str, Any] | None = None,
        access_id: int | None = None,
        actor: str = "board",
    ) -> Environment:
        """Add an access entry (`access_id` None) or replace one. Test secrets (in `fields` as
        `{"value": ..., "test": true}`, or in `test_secrets`; `***` keeps a stored one) only on
        local and test environments whose policy allows them. With neither, a replaced entry
        keeps its stored test secrets."""
        plain, marked = envs.split_test_secrets(fields)
        keep = test_secrets is None and not marked
        values = {**marked, **(test_secrets or {})}
        async with self._tx() as tx:
            proj, env = await self._project_environment(tx.conn, project, env_id)
            pol = await self._policy_of(tx.conn, proj, env)
            entry = None
            if access_id is not None:
                entry = await repo.get_access(tx.conn, access_id)
                if entry is None or entry.environment_id != env.id:
                    raise NotFoundError(f"Access entry #{access_id} not found in {env.name}.")
            if keep and entry is not None:
                tests = entry.test_secrets
            else:
                tests = envs.clean_test_secrets(
                    values,
                    env.kind,
                    pol.test_secrets_allowed(env.kind),
                    entry.test_secrets if entry else None,
                )
            kind, clean = envs.clean_access(kind, plain, pol.secret_rules, allow_empty=bool(tests))
            if entry is None:
                access_id = await repo.insert_access(
                    tx.conn, env.id, kind, clean, actor, tx.now, tests
                )
                event = "environment.access_added"
            else:
                if (entry.kind, entry.fields, entry.test_secrets) == (kind, clean, tests):
                    return env
                await repo.update_access(tx.conn, entry.id, kind, clean, actor, tx.now, tests)
                event = "environment.access_updated"
            await self._access_event(
                tx, event, actor, proj, env, access_kind=kind, access_id=access_id
            )
            saved = await repo.get_environment(tx.conn, env.id)
            assert saved is not None
            return saved

    async def delete_access(
        self, project: str, env_id: int, access_id: int, *, actor: str = "board"
    ) -> None:
        async with self._tx() as tx:
            proj, env = await self._project_environment(tx.conn, project, env_id)
            entry = await repo.get_access(tx.conn, access_id)
            if entry is None or entry.environment_id != env.id:
                raise NotFoundError(f"Access entry #{access_id} not found in {env.name}.")
            await repo.delete_access(tx.conn, access_id)
            await self._access_event(
                tx,
                "environment.access_removed",
                actor,
                proj,
                env,
                access_kind=entry.kind,
                access_id=access_id,
            )

    async def propose_environment_change(
        self,
        project: str,
        change: Any,
        reason: str,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Proposal:
        """An agent's proposal: recorded for the owner, nothing else changes."""
        secrets = (await self.effective_policy(project)).secret_rules
        clean = envs.clean_change(change, secrets)
        text = _clean_text(reason, "reason", envs.REASON_MAX)
        if not text:
            raise ValidationError("reason must say why the change is needed.")
        envs.check_secret("reason", text, secrets)
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            current = await repo.find_environment(tx.conn, proj.id, clean["environment"])
            if current is None and "kind" not in clean:
                raise ValidationError(
                    f"{proj.slug} has no environment {clean['environment']!r}; a new one needs"
                    " kind (local, test or live)."
                )
            if current is not None:
                clean["environment"] = current.name
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            proposal_id = await repo.insert_proposal(
                tx.conn, proj.id, clean, text, actor, sess.id if sess else None, tx.now
            )
            await tx.event(
                "environment.proposed",
                actor,
                project_id=proj.id,
                session_id=sess.id if sess else None,
                project=proj.slug,
                environment=clean["environment"],
                proposal_id=proposal_id,
                change=envs.change_summary(clean),
            )
            proposal = await repo.get_proposal(tx.conn, proposal_id)
            assert proposal is not None
            return proposal

    async def decide_proposal(
        self,
        proposal_id: int,
        accept: bool,
        *,
        project: str | None = None,
        confirm_live: bool = False,
        actor: str = "board",
    ) -> Proposal:
        """Accept (apply the change) or reject an agent's proposal. Board only."""
        async with self._tx() as tx:
            proposal = await repo.get_proposal(tx.conn, proposal_id)
            if proposal is None or (
                project is not None and proposal.project_slug != project.strip().lower()
            ):
                raise NotFoundError(f"Proposal #{proposal_id} not found.")
            if proposal.status != "pending":
                raise ConflictError(f"Proposal #{proposal_id} is already {proposal.status}.")
            proj = await self.get_project(proposal.project_slug, tx.conn)
            if accept:
                change = dict(proposal.change)
                access = change.pop("access", [])
                name = change.pop("environment")
                current = await repo.find_environment(tx.conn, proj.id, name)
                values = {"name": current.name if current else name, **change}
                env = await self._write_environment(tx, proj, current, values, actor, confirm_live)
                for entry in access:
                    access_id = await repo.insert_access(
                        tx.conn, env.id, entry["kind"], entry["fields"], actor, tx.now
                    )
                    await self._access_event(
                        tx,
                        "environment.access_added",
                        actor,
                        proj,
                        env,
                        access_kind=entry["kind"],
                        access_id=access_id,
                    )
            status = "accepted" if accept else "rejected"
            await repo.decide_proposal(tx.conn, proposal.id, status, actor, tx.now)
            await tx.event(
                f"environment.proposal_{status}",
                actor,
                project_id=proj.id,
                project=proj.slug,
                environment=proposal.environment,
                proposal_id=proposal.id,
                change=envs.change_summary(proposal.change),
            )
            decided = await repo.get_proposal(tx.conn, proposal.id)
            assert decided is not None
            return decided

    # --- policy ------------------------------------------------------------

    def _log_policy_error(self, error: str) -> None:
        if error not in self._logged_policy_errors:
            self._logged_policy_errors.add(error)
            logger.warning("%s The layer is ignored.", error)

    def _db_layer(self, row: repo.PolicyRow) -> Layer:
        """A project or environment layer from the database; a broken one is ignored."""
        if row.environment_id is None:
            name, scope = f"project:{row.project_slug}", "project"
        else:
            name, scope = f"environment:{row.project_slug}/{row.environment_name}", "environment"
        try:
            body = parse_layer(row.body, name, scope)
        except PolicyError as exc:
            self._log_policy_error(str(exc))
            return Layer(name, scope, {}, error=str(exc))
        return Layer(name, scope, body)

    def _global_layers(self, global_body: dict[str, Any] | None = None) -> list[Layer]:
        """Preset → config.toml → global file (`global_body` instead of the file: a preview)."""
        self.policy_file.refresh()
        body = self.policy_file.body if global_body is None else global_body
        name = body.get("preset", DEFAULT_PRESET)
        layers = [Layer(f"preset:{name}", "preset", preset(name))]
        if self._config_policy:
            layers.append(Layer("config", "config", self._config_policy))
        layers.append(Layer("global", "global", body))
        return layers

    def global_policy(self) -> EffectivePolicy:
        return EffectivePolicy(self._global_layers())

    @property
    def permission_defaults(self) -> dict[str, str]:
        """The global policy's level per environment kind."""
        pol = self.global_policy()
        return {kind: pol.level(kind) for kind in KINDS}

    @property
    def guard_mode(self) -> str:
        return self.global_policy().guard_mode

    async def _layers_of(
        self, conn: aiosqlite.Connection, project_ids: list[int]
    ) -> tuple[dict[int, Layer], dict[int, Layer]]:
        """Project layers by project id, environment layers by environment id."""
        projects: dict[int, Layer] = {}
        environments: dict[int, Layer] = {}
        for row in await repo.list_policy_layers(conn, project_ids):
            layer = self._db_layer(row)
            if row.environment_id is None:
                projects[row.project_id] = layer
            else:
                environments[row.environment_id] = layer
        return projects, environments

    async def _policies(
        self, conn: aiosqlite.Connection, environments: list[Environment]
    ) -> dict[int, EffectivePolicy]:
        """The effective policy of each environment, by environment id."""
        if not environments:
            return {}
        base = self._global_layers()
        projects, layers = await self._layers_of(
            conn, list(dict.fromkeys(e.project_id for e in environments))
        )
        out = {}
        for env in environments:
            extra = [projects.get(env.project_id), layers.get(env.id)]
            out[env.id] = EffectivePolicy(base + [x for x in extra if x is not None])
        return out

    async def environment_policies(
        self, environments: list[Environment]
    ) -> dict[int, EffectivePolicy]:
        return await self._policies(self._r, environments)

    async def _policy_of(
        self, conn: aiosqlite.Connection, project: Project, env: Environment | None
    ) -> EffectivePolicy:
        projects, layers = await self._layers_of(conn, [project.id])
        extra = [projects.get(project.id), layers.get(env.id) if env else None]
        return EffectivePolicy(self._global_layers() + [x for x in extra if x is not None])

    async def _resolve_scope(
        self, conn: aiosqlite.Connection, scope: str
    ) -> tuple[str, Project | None, Environment | None]:
        """`global`, `<project>` or `<project>/<environment>` → (canonical scope, project,
        environment)."""
        text = str(scope or "").strip()
        if not text:
            raise ValidationError(f"Missing scope: {POLICY_SCOPE_HINT}.")
        if text.lower() == "global":
            return "global", None, None
        slug, _, env_name = text.partition("/")
        project = await self.get_project(slug, conn)
        if not env_name:
            return project.slug, project, None
        env = await repo.find_environment(conn, project.id, env_name.strip())
        if env is None:
            raise NotFoundError(f"{project.slug} has no environment {env_name.strip()!r}.")
        return f"{project.slug}/{env.name}", project, env

    async def effective_policy(
        self, project: str | None = None, environment: str | None = None
    ) -> EffectivePolicy:
        """The effective policy of the global scope, a project or one of its environments."""
        if project is None:
            if environment:
                raise ValidationError("environment needs a project.")
            return self.global_policy()
        scope = f"{project}/{environment}" if environment else project
        _, proj, env = await self._resolve_scope(self._r, scope)
        assert proj is not None
        return await self._policy_of(self._r, proj, env)

    async def policy_layer_text(self, scope: str) -> str:
        """The raw JSON of one layer as stored (empty: no layer)."""
        _, proj, env = await self._resolve_scope(self._r, scope)
        if proj is None:
            self.policy_file.refresh()
            return self.policy_file.text
        for row in await repo.list_policy_layers(self._r, [proj.id]):
            if row.environment_id == (env.id if env else None):
                return row.body
        return ""

    async def policy_layers(self) -> list[repo.PolicyRow]:
        return await repo.list_policy_layers(self._r)

    async def _policy_change(
        self,
        conn: aiosqlite.Connection,
        scope: str,
        body: dict[str, Any],
    ) -> tuple[str, Project | None, Environment | None, EffectivePolicy, list[str]]:
        """Validate a new layer for `scope`: the policy after it and what it widens on live."""
        canonical, proj, env = await self._resolve_scope(conn, scope)
        kind = "global" if proj is None else "environment" if env else "project"
        validate_layer(body, f"{kind} policy {canonical}", kind)
        if proj is None:
            before = self.global_policy()
            after = EffectivePolicy(self._global_layers(body))
        else:
            projects, layers = await self._layers_of(conn, [proj.id])
            project_layer = projects.get(proj.id)
            env_layer = layers.get(env.id) if env else None
            base = self._global_layers()
            old = [x for x in (project_layer, env_layer) if x is not None]
            before = EffectivePolicy(base + old)
            new = Layer(f"{kind}:{canonical}", kind, body)
            below = [project_layer, new] if env else [new]
            after = EffectivePolicy(base + [x for x in below if x is not None])
        widened = [] if env is not None and env.kind != "live" else widened_on_live(before, after)
        return canonical, proj, env, after, widened

    async def preview_policy(self, scope: str, text: str) -> tuple[EffectivePolicy, list[str]]:
        """The effective policy a layer's new text would give, and what it widens on live;
        PolicyError when the text does not validate."""
        canonical, proj, env = await self._resolve_scope(self._r, scope)
        kind = "global" if proj is None else "environment" if env else "project"
        body = parse_layer(text, f"{kind} policy {canonical}", kind)
        _, _, _, after, widened = await self._policy_change(self._r, scope, body)
        return after, widened

    async def _store_policy(
        self,
        tx: _Tx,
        scope: str,
        body: dict[str, Any],
        actor: str,
        confirm_live: bool,
    ) -> tuple[str, dict[str, Any] | None]:
        """Store a validated layer in the transaction; for the global scope, the body the
        caller writes to the file after the commit (no file I/O while the transaction is open).
        """
        canonical, proj, env, _, widened = await self._policy_change(tx.conn, scope, body)
        if widened and not confirm_live:
            raise ConfirmationNeeded(
                f"This policy change widens agents' access on live ({', '.join(widened)}):"
                " agents may change live without asking. Confirm to save."
            )
        if proj is None:
            self._global_writable()
        else:
            text = json.dumps(body, indent=2, ensure_ascii=False) if body else None
            await repo.set_policy_layer(
                tx.conn, proj.id, env.id if env else None, text, actor, tx.now
            )
        await tx.event(
            "policy.updated",
            actor,
            project_id=proj.id if proj else None,
            scope=canonical,
            **({"project": proj.slug} if proj else {}),
            widened=widened,
        )
        return canonical, body if proj is None else None

    def _global_writable(self) -> None:
        """A change merged into the global file never overwrites the owner's broken edit."""
        self.policy_file.refresh()
        if self.policy_file.error:
            raise PolicyError(
                f"The global policy file is broken, so it was not changed: {self.policy_file.error}"
                " Fix the file (or replace it in the editor) first."
            )

    def _write_global(self, body: dict[str, Any] | None) -> None:
        if body is not None:
            self.policy_file.write(body)

    async def save_policy_layer(
        self, scope: str, text: str, *, confirm_live: bool = False, actor: str = "board"
    ) -> str:
        """Replace one layer with `text` (JSON; empty removes a project or environment layer).
        Board only: agents propose. A change that widens access on live needs `confirm_live`."""
        canonical, proj, env = await self._resolve_scope(self._r, scope)
        kind = "global" if proj is None else "environment" if env else "project"
        body = parse_layer(text, f"{kind} policy {canonical}", kind)
        if proj is None:
            # The editor replaces the whole file, a broken one included.
            _, _, _, _, widened = await self._policy_change(self._r, canonical, body)
            if widened and not confirm_live:
                raise ConfirmationNeeded(
                    f"This policy change widens agents' access on live ({', '.join(widened)}):"
                    " agents may change live without asking. Confirm to save."
                )
            self.policy_file.write(body)
            async with self._tx() as tx:
                await tx.event("policy.updated", actor, scope="global", widened=widened)
            return canonical
        async with self._tx() as tx:
            canonical, _ = await self._store_policy(tx, canonical, body, actor, confirm_live)
        return canonical

    async def set_preset(
        self, name: str, *, confirm_live: bool = False, actor: str = "board"
    ) -> None:
        """Name the preset in the global file, keeping the rest of it."""
        preset(name)  # unknown names fail here
        self._global_writable()
        body = {**self.policy_file.body, "preset": name}
        async with self._tx() as tx:
            _, pending = await self._store_policy(tx, "global", body, actor, confirm_live)
        self._write_global(pending)

    async def policy_proposals(self) -> list[PolicyProposal]:
        return await repo.list_policy_proposals(self._r)

    async def propose_policy_change(
        self,
        scope: str,
        patch: Any,
        reason: str,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> PolicyProposal:
        """An agent's proposed policy change: recorded for the owner, nothing else changes. The
        patch is merged into the layer's JSON and the result validated now."""
        if not isinstance(patch, dict) or not patch:
            raise ValidationError(
                "patch must be a non-empty object merged into the layer, e.g."
                " {'guard': {'mode': 'block'}}."
            )
        text = _clean_text(reason, "reason", envs.REASON_MAX)
        if not text:
            raise ValidationError("reason must say why the change is needed.")
        envs.check_secret("reason", text)
        async with self._tx() as tx:
            canonical, proj, _ = await self._resolve_scope(tx.conn, scope)
            current = await self._layer_body(tx.conn, canonical)
            await self._policy_change(tx.conn, canonical, apply_patch(current, patch))
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            proposal_id = await repo.insert_policy_proposal(
                tx.conn,
                canonical,
                proj.id if proj else None,
                patch,
                text,
                actor,
                sess.id if sess else None,
                tx.now,
            )
            await tx.event(
                "policy.proposed",
                actor,
                project_id=proj.id if proj else None,
                session_id=sess.id if sess else None,
                scope=canonical,
                **({"project": proj.slug} if proj else {}),
                proposal_id=proposal_id,
                change=", ".join(_patch_summary(patch)),
            )
            proposal = await repo.get_policy_proposal(tx.conn, proposal_id)
            assert proposal is not None
            return proposal

    async def _layer_body(self, conn: aiosqlite.Connection, scope: str) -> dict[str, Any]:
        """A layer's current JSON (a broken stored layer counts as empty)."""
        _, proj, env = await self._resolve_scope(conn, scope)
        if proj is None:
            self.policy_file.refresh()
            return dict(self.policy_file.body)
        projects, layers = await self._layers_of(conn, [proj.id])
        layer = layers.get(env.id) if env else projects.get(proj.id)
        return dict(layer.body) if layer is not None else {}

    async def decide_policy_proposal(
        self,
        proposal_id: int,
        accept: bool,
        *,
        confirm_live: bool = False,
        actor: str = "board",
    ) -> PolicyProposal:
        """Accept (merge the patch into the layer) or reject a policy proposal. Board only."""
        pending = None
        async with self._tx() as tx:
            proposal = await repo.get_policy_proposal(tx.conn, proposal_id)
            if proposal is None:
                raise NotFoundError(f"Policy proposal #{proposal_id} not found.")
            if proposal.status != "pending":
                raise ConflictError(f"Policy proposal #{proposal_id} is already {proposal.status}.")
            if accept:
                current = await self._layer_body(tx.conn, proposal.scope)
                body = apply_patch(current, proposal.patch)
                _, pending = await self._store_policy(tx, proposal.scope, body, actor, confirm_live)
            status = "accepted" if accept else "rejected"
            await repo.decide_policy_proposal(tx.conn, proposal.id, status, actor, tx.now)
            await tx.event(
                f"policy.proposal_{status}",
                actor,
                project_id=proposal.project_id,
                scope=proposal.scope,
                proposal_id=proposal.id,
                change=", ".join(_patch_summary(proposal.patch)),
            )
            decided = await repo.get_policy_proposal(tx.conn, proposal.id)
            assert decided is not None
        try:
            self._write_global(pending)
        except PolicyError:
            # The file is the policy: without it the proposal is not accepted after all.
            async with self._tx() as tx:
                await repo.reopen_policy_proposal(tx.conn, proposal_id)
                await tx.event(
                    "policy.proposal_reopened",
                    actor,
                    scope=decided.scope,
                    proposal_id=proposal_id,
                    reason="the global policy file could not be written",
                )
            raise
        return decided
