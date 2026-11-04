"""Use cases. The only writer of the database: state change + event row in one transaction,
then the committed events are published on the in-process bus."""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import aiosqlite

from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain import annotations as ann
from relaybay.domain import comments as comment_text
from relaybay.domain import digest as digests
from relaybay.domain import digest_limits as limits
from relaybay.domain import environments as envs
from relaybay.domain import guard
from relaybay.domain import map_graph as graphs
from relaybay.domain import memory as mem
from relaybay.domain import usage as usage_data
from relaybay.domain.annotations import Annotation
from relaybay.domain.comments import (
    COMMENT_MAX,
    CONTEXT_MAX,
    STOP_BLOCK_CAP,
    STOP_COMMENT_CAP,
)
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
    CONTINUED_END_REASONS,
    DELIVERY_EVENTS,
    HookEvent,
    folder_name,
    parse_hook,
    parse_message_recipient,
    session_start_context,
)
from relaybay.domain.models import (
    Binding,
    Comment,
    Decision,
    Event,
    ImportItem,
    Project,
    Report,
    Session,
    Task,
    TrackerEntry,
)
from relaybay.domain.plan import (
    DEFAULT_REMINDER_EVERY,
    NO_PLAN_HINT,
    PLAN_STEPS_MAX,
    REASON_MAX,
    STEP_STATUSES,
    TODO_ITEMS_MAX,
    Plan,
    PlanRevision,
    PlanStep,
    TodoChange,
    TodoItem,
    clean_conformance,
    clean_outcome,
    parse_todo,
    plan_body,
    reminder_line,
    reminder_text,
    title_diff,
)
from relaybay.domain.plan import STEP_DETAILS_MAX as PLAN_DETAILS_MAX
from relaybay.domain.plan import clean_step_title as clean_plan_title
from relaybay.domain.reports import REPORT_SIZES, parse_report
from relaybay.domain.review import (
    CAPTION_MAX,
    DEFAULT_FILE_MAX_MB,
    DEFAULT_TASK_MAX_MB,
    EXTENSIONS,
    MB,
    STEP_DETAILS_MAX,
    STEPS_MAX,
    Attachment,
    Review,
    ReviewStep,
    clean_markdown,
    clean_original_name,
    clean_step_title,
    read_source,
    require_image,
    require_png,
    resolve_source,
)
from relaybay.domain.rules import (
    DEFAULT_RULES,
    REPORT_SINCE_STAGE_CHANGE,
    RULES_MODES,
    Rules,
    merge_project_rules,
    refusal_text,
    rules_line,
    unmet,
    validate_rules,
    warning_text,
)
from relaybay.domain.sessions import DEFAULT_DESKTOP_LINK_TEMPLATE, parse_session_id
from relaybay.domain.stages import ACCEPTANCE_STAGES, OPEN_STAGES, STAGES, Stage, parse_stage
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
MESSAGE_WINDOW = 60.0  # seconds: at most one message event per sender and recipient
MESSAGE_RECIPIENTS_MAX = 20  # different recipients of one sender per window
HOOK_MEMO_MAX = 4096
MAP_RECENT_EVENTS = 2000
HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)  # use fullmatch
LIVE_ACTION = "live.action"
JOURNAL_COMMAND_MAX = 200
# Left free in the SessionStart answer next to the lines it holds anyway.
SESSION_CONTEXT_RESERVE = 500


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
    review: Review = field(default_factory=lambda: Review([], []))
    bind_choices: list[Session] = field(default_factory=list)  # for the board's session picker
    plan: Plan = field(default_factory=lambda: Plan(None, None, None))
    todo: list[TodoItem] = field(default_factory=list)  # the bound session's Claude Code todo
    plan_history: list[tuple[PlanRevision, list[str]]] = field(default_factory=list)


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
    review: list[Task] = field(default_factory=list)  # tasks in review/test with a checklist

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
            + len(self.review)
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


class NoteResult(NamedTuple):
    """What memory_add did: `candidates` (nothing written, similar notes with their title
    similarity), `add`, `update`, `supersede` or `noop`."""

    op: str
    note: mem.MemoryNote | None
    candidates: list[tuple[mem.MemoryNote, float]]


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
    if spec.kind == "size":
        size = (value or "").strip().upper()
        if size and size not in REPORT_SIZES:
            raise ValidationError(
                f"{name} must be one of {', '.join(REPORT_SIZES)}, got {value!r}."
            )
        return size or None
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


REVIEW_DECISIONS = ("accept", "reject", "reopen", "comment")
NO_PLAN_STAGE_HINT = "Hint: {key} has no plan and no todo yet: " + NO_PLAN_HINT + "."
PLAN_LINK_RE = re.compile(r"(?:report\s*[:#]?\s*|#)?(\d+)", re.IGNORECASE)
EMPTY_CHECKLIST_HINT = (
    "Hint: the review checklist of {key} is empty. Write what the owner should check by eye"
    " with review_steps_set (and attach screenshots with attach_file)."
)


def review_decision_text(
    step: ReviewStep, decision: str, note: str | None, steps: list[ReviewStep]
) -> str:
    """The owner comment that carries a decision to the agent, e.g. "Step 2 'Login with 2FA'
    accepted — continue with step 3"."""
    name = f"Step {step.position} '{step.title}'"
    if decision == "comment":
        return f"On {name[0].lower()}{name[1:]}: {note}"
    if decision == "reject":
        return f"{name} rejected: {note}" if note else f"{name} rejected."
    if decision == "reopen":
        return f"{name} reopened" + (f": {note}" if note else ".")
    rest = [s for s in steps if s.status != "accepted"]
    later = [s for s in rest if s.position > step.position]
    nxt = (later or rest)[0] if rest else None
    text = f"{name} accepted — " + (
        f"continue with step {nxt.position}" if nxt else f"all {len(steps)} steps accepted"
    )
    return text + (f". {note}" if note else "")


def _decode_base64(text: str, limit: int) -> bytes:
    compact = "".join(text.split())
    if len(compact) > (limit // 3 + 1) * 4:
        raise ValidationError(f"The file is larger than {limit // MB} MB.")
    try:
        data = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        raise ValidationError("content_base64 is not valid base64.") from None
    if len(data) > limit:
        raise ValidationError(f"The file is larger than {limit // MB} MB.")
    if not data:
        raise ValidationError("content_base64 is empty.")
    return data


def _sweep(root: Path, keep: set[tuple[str, str, str]]) -> int:
    """Delete files under `root` (attachments/<project>/<task>/<file>) not in `keep`, then
    empty folders. Symlinks are removed as links, never followed."""
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        here = Path(dirpath)
        parts = here.relative_to(root).parts
        for name in filenames:
            if len(parts) == 2 and (parts[0], parts[1], name) in keep:
                continue
            (here / name).unlink(missing_ok=True)
            removed += 1
        for name in dirnames:
            child = here / name
            if child.is_symlink():
                child.unlink(missing_ok=True)
                removed += 1
        if here != root:
            with contextlib.suppress(OSError):  # not empty: it still holds kept files
                here.rmdir()
    return removed


def _clean_doc_slug(value: Any) -> str:
    slug = str(value or "").strip().lower()
    if not mem.DOC_SLUG_RE.match(slug):
        raise ValidationError(
            f"Invalid document name {value!r}: use lowercase letters, digits and dashes,"
            " e.g. 'spec'."
        )
    return slug


def _clean_block_label(value: Any) -> str:
    label = str(value or "").strip().lower()
    if not mem.BLOCK_LABEL_RE.match(label):
        raise ValidationError(
            f"Invalid block label {value!r}: use lowercase letters, digits, '-' and '_',"
            " e.g. 'owner-preferences'."
        )
    return label


def _clean_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValidationError("base_version must be a whole number (0 creates).")
    try:
        number = int(value)
    except ValueError:
        raise ValidationError(f"base_version must be a whole number, got {value!r}.") from None
    if number < 0:
        raise ValidationError("base_version must not be negative.")
    return number


def _clean_tags(value: Any) -> str | None:
    """Comma or space separated tags, lowercased, each once: 'ui, Billing ui' -> 'ui, billing'."""
    text = _clean_text(value, "tags", mem.TAGS_MAX)
    if not text:
        return None
    tags = dict.fromkeys(t.lower() for t in re.split(r"[,\s]+", text) if t)
    return ", ".join(tags) or None


def _clean_date(value: Any, name: str) -> str | None:
    text = _clean_text(value, name, 40)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        raise ValidationError(f"{name} must be a date like 2026-12-31, got {text!r}.") from None


def _scan(rules: SecretRules, **texts: str | None) -> None:
    """Refuse text that holds a secret value; the error names the field and the detector."""
    for name, text in texts.items():
        if text:
            envs.check_secret(name, text, rules)


def _first_words(text: str, limit: int = 80) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


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
        attachments_dir: Path | None = None,
        attachment_roots: list[Path] | None = None,
        attachment_max_bytes: int = DEFAULT_FILE_MAX_MB * MB,
        attachment_task_max_bytes: int = DEFAULT_TASK_MAX_MB * MB,
        plan_reminder_every: int = DEFAULT_REMINDER_EVERY,
        owner_names: list[str] | None = None,
        require_tracker_log: bool = False,
        done_window_days: int = DEFAULT_DONE_WINDOW_DAYS,
        pulse_stale_days: int = limits.DEFAULT_PULSE_STALE_DAYS,
        task_stale_days: int = limits.DEFAULT_TASK_STALE_DAYS,
        digest_budget_chars: int = limits.DEFAULT_BUDGET,
        session_context: str = "auto",
        session_context_project_chars: int = limits.DEFAULT_SESSION_PROJECT_CHARS,
        usage_enabled: bool = True,
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
        self.attachments_dir = attachments_dir or db_path.parent / "attachments"
        self.attachment_roots = list(attachment_roots or [])
        self.attachment_max_bytes = attachment_max_bytes
        self.attachment_task_max_bytes = attachment_task_max_bytes
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
        # FTS5 full-text search; False when this SQLite has no FTS5 (search uses LIKE then).
        self.search_fts = False
        # Plan reminders per session: tool events since the session started, the last text sent.
        self.plan_reminder_every = plan_reminder_every
        self._tool_events: dict[int, int] = {}
        self._reminded: dict[int, str] = {}
        if session_context not in limits.SESSION_CONTEXT_MODES:
            modes = ", ".join(limits.SESSION_CONTEXT_MODES)
            raise ValueError(f"session_context must be one of: {modes}.")
        self.owner_names = owner_names if owner_names is not None else ["owner", "me", "you"]
        self.require_tracker_log = require_tracker_log
        self.done_window_days = done_window_days
        self.pulse_stale_days = pulse_stale_days
        self.task_stale_days = task_stale_days
        self.digest_budget_chars = digest_budget_chars
        self.session_context = session_context
        self.session_context_project_chars = session_context_project_chars
        self.usage_enabled = usage_enabled
        # When a message event was last written, per sender and recipient; what a hook's
        # transcript path was found to be, so the file system is asked once per path; subagent
        # files over the limit, per transcript file.
        self._message_written: dict[int, dict[str, float]] = {}
        self._transcript_checked: dict[str, bool] = {}
        self._usage_capped: dict[int, int] = {}
        # Projects whose missing or stale pulse a stage move already mentioned in this run.
        self._pulse_noted: set[int] = set()

    async def start(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_conn = await connect(self.db_path)
        await migrate(self._write_conn)
        self._read_conn = await connect(self.db_path)
        self.search_fts = await repo.ensure_search_index(self._write_conn)
        if self.search_fts and await repo.search_index_stale(self._write_conn):
            async with self._tx() as tx:
                count = await repo.rebuild_search_index(tx.conn)
            logger.info("Search index rebuilt: %d item(s).", count)
        # A broken project override is reported now; it never stops the server or the project.
        for slug, body in await repo.list_project_rules(self._read_conn):
            self._merged_rules(slug, body)
        # A broken global policy stops the start; broken project layers are only logged.
        self.policy_file.load()
        for row in await repo.list_policy_layers(self._read_conn):
            self._db_layer(row)
        await self._sweep_attachments()

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
            await repo.move_session_todo(tx.conn, other.id, session.id, cc_id)
            await repo.move_usage(tx.conn, other.id, session.id)
            changes.update(
                status=other.status,
                activity=other.activity,
                activity_at=other.activity_at,
                cwd=other.cwd,
            )
            if other.transcript_path:
                changes["transcript_path"] = other.transcript_path
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

    async def _host_desktop(
        self, tx: _Tx, hook: HookEvent, found: Session | None
    ) -> Session | None:
        """The desktop session a SessionStart reports it runs in (the desktop app's id in the
        hook's environment), when the new Claude Code id may be linked to it exactly. While
        the desktop session is exactly linked to another live Claude Code id, only the window
        itself moves it: an attended /clear, resume or compaction (a restart whose old process
        sent no SessionEnd). A `claude` started from one of its tools inherits the id but runs
        unattended, and a plain startup is never the same window. One that says it runs
        unattended never links, whatever the row's state. An id another session's agent linked
        itself is never taken."""
        if hook.host_session is None or hook.nested:
            return None
        desktop = await repo.get_session_by_external_id(tx.conn, hook.host_session)
        if desktop is None or (found is not None and found.id == desktop.id and found.cc_exact):
            return None
        if found is not None and found.id != desktop.id and found.cc_exact:
            return None
        live = (
            desktop.cc_exact
            and desktop.cc_session_id not in (None, hook.cc_session_id)
            and desktop.status not in (None, "ended")
        )
        if live and (hook.source == "startup" or not hook.attended):
            return None
        return desktop

    async def _hook_session(self, tx: _Tx, hook: HookEvent) -> Session:
        """The relaybay session of a hook event. First sight without an exact link: the one
        desktop session bound in the project of `cwd`; several possible ones (or a folder of
        several projects) stay unlinked; none, a plain session named after the folder."""
        found = await repo.get_session_by_cc(tx.conn, hook.cc_session_id)
        desktop = await self._host_desktop(tx, hook, found)
        if desktop is not None:
            return await self._link_hook_session(tx, desktop, hook.cc_session_id)
        if found is not None:
            return found
        projects = await repo.projects_with_repo_path(tx.conn)
        project, reason = self._project_for_cwd(projects, hook.cwd)
        if hook.nested:  # not guessed onto the window either: its own plain session
            return await self._new_hook_session(tx, hook, project.id if project else None, None)
        if project is not None:
            bound = await repo.bound_desktop_sessions(tx.conn, project.id)
            # An ended desktop session links its next Claude Code id itself (host id), so a
            # terminal `claude` in the same folder is not guessed onto it.
            bound = [s for s in bound if s.status != "ended"]
            free = [s for s in bound if s.cc_session_id is None]
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
        the window is written when it ends. A todo tool's change is stored either way."""
        hook = parse_hook(payload)
        if hook is None:
            return None
        if (
            hook.event == "SessionEnd"
            and await repo.get_session_by_cc(self._r, hook.cc_session_id) is None
        ):
            return None  # e.g. after /clear the new id took the session over first: nothing ends
        session = await self._held_back(hook) or await self._apply_hook(hook)
        change = parse_todo(payload)
        if change is not None:
            try:
                await self._record_todo(hook.cc_session_id, change)
            except Exception:
                logger.exception("Recording the session's todo failed; the hook goes on")
        recipient = parse_message_recipient(payload)
        if recipient is not None and session is not None:
            try:
                await self._record_message(session, recipient)
            except Exception:
                logger.exception("Recording a message edge failed; the hook goes on")
        return session

    async def _record_message(self, session: Session, recipient: str) -> None:
        """A `session.message` event: sender, recipient and time, never the text. At most one per
        sender and recipient per `MESSAGE_WINDOW` and `MESSAGE_RECIPIENTS_MAX` recipients per
        sender, so the hook path writes rarely. The memo holds a bounded number of short lists: each
        call looks only at its own sender's."""
        now = time.monotonic()
        sent = {
            name: at
            for name, at in self._message_written.get(session.id, {}).items()
            if now - at < MESSAGE_WINDOW
        }
        if recipient in sent or len(sent) >= MESSAGE_RECIPIENTS_MAX:
            return
        sent[recipient] = now
        if session.id not in self._message_written and (
            len(self._message_written) >= HOOK_MEMO_MAX
        ):
            del self._message_written[next(iter(self._message_written))]  # the oldest sender
        self._message_written[session.id] = sent
        async with self._tx() as tx:
            await tx.event(
                "session.message",
                session.name,
                project_id=session.project_id,
                session_id=session.id,
                session=session.name,
                to=recipient,
            )

    async def _allowed_transcript(self, value: str | None) -> str | None:
        """The hook's transcript path when it may be kept. The answer is remembered per path:
        most hook events repeat the path and none of them should touch the disk for it. The one
        check runs in a worker thread: a path can take long to answer, the hook path must not."""
        if value is None or not self.usage_enabled:
            return None
        allowed = self._transcript_checked.get(value)
        if allowed is None:
            if len(self._transcript_checked) >= HOOK_MEMO_MAX:
                self._transcript_checked.clear()
            allowed = await asyncio.to_thread(usage_data.transcript_file, value) is not None
            self._transcript_checked[value] = allowed
        return value if allowed else None

    async def _held_back(self, hook: HookEvent) -> Session | None:
        """The session when this activity is coalesced into the current window, else None."""
        if not hook.activity_only:
            return None
        current = await repo.get_session_by_cc(self._r, hook.cc_session_id)
        if current is None or current.status != "working":
            return None
        last = self._activity_written.get(current.id)
        elapsed = time.monotonic() - last if last is not None else None
        if elapsed is None or elapsed >= self.hook_activity_window:
            return None
        self._activity_pending[current.id] = hook
        if current.id not in self._activity_timers:
            self._activity_timers[current.id] = asyncio.get_running_loop().call_later(
                self.hook_activity_window - elapsed, self._flush_activity, current.id
            )
        return current

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
        transcript = await self._allowed_transcript(hook.transcript_path)
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
            if transcript is not None and transcript != session.transcript_path:
                changes["transcript_path"] = transcript
                await repo.track_usage_file(tx.conn, session.id, transcript, tx.now)
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
            if hook.event == "SessionEnd" and not self._continues(updated, hook):
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

    @staticmethod
    def _continues(session: Session, hook: HookEvent) -> bool:
        """A SessionEnd after which the session goes on under a new Claude Code id keeps its
        tasks: /clear and /resume in any session, and every end of a desktop session (its id
        outlives the Claude Code processes; the next SessionStart links the new one)."""
        return session.external_id is not None or hook.end_reason in CONTINUED_END_REASONS

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

    async def _take_undelivered(
        self, session: Session, budget: int = CONTEXT_MAX
    ) -> tuple[list[Comment], int]:
        """Mark the oldest undelivered owner comments that fit in one hook answer as delivered
        to `session`; returns them and how many were waiting."""
        if not await repo.undelivered_owner_comments(self._r, session.id):
            return [], 0  # the common case: no write transaction per tool call
        async with self._tx() as tx:
            waiting = await repo.undelivered_owner_comments(tx.conn, session.id)
            shown = comment_text.fit(waiting, budget)
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
            if hook.nested:
                return _context("SessionStart", context)
            unread = await repo.unread_owner_count(self._r, session.id) if session else 0
            if unread:
                context += " " + comment_text.unread_line(unread)
            if session is not None:
                # A new context: the agent has seen nothing yet.
                self._tool_events[session.id] = 0
                self._reminded.pop(session.id, None)
            # Always computed: it is marked as sent, and the fallback when no context is added.
            reminder = await self._plan_reminder(session) if session is not None else None
            room = CONTEXT_MAX - len(context) - len(reminder or "") - SESSION_CONTEXT_RESERVE
            added, has_plan = await self._session_context(hook, session, room)
            if added:
                context += "\n" + added
            if reminder and not has_plan:
                context += "\n" + reminder
            return _context("SessionStart", context)
        if hook.event == "PreToolUse":
            return await self._pre_tool_answer(payload, hook, session)
        if session is None:
            return None
        if hook.event == "UserPromptSubmit":
            self._stop_blocks.pop(session.id, None)  # a new turn
        if hook.event in DELIVERY_EVENTS:
            reminder = None
            due = self._reminder_due(session, payload) if hook.event == "PostToolUse" else None
            if due is not None:
                reminder = await self._plan_reminder(session, repeat=due == "periodic")
            budget = CONTEXT_MAX - (len(reminder) + 1 if reminder else 0)
            shown, total = await self._take_undelivered(session, budget)
            parts = [comment_text.delivery_text(shown, total)] if shown else []
            if reminder:
                parts.append(reminder)
            return _context(hook.event, "\n".join(parts)) if parts else None
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
            await repo.review_queue(self._r),
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

    # --- map ---------------------------------------------------------------

    async def map_graph(
        self,
        *,
        view: str = "structure",
        minutes: int = graphs.DEFAULT_MINUTES,
        project: str | None = None,
    ) -> dict[str, Any]:
        """The Map as JSON-ready nodes and edges. The Now view marks what happened in the last
        `minutes`. No text of a task, comment or message is in it beyond titles and names."""
        if view not in graphs.VIEWS:
            raise ValidationError(f"view must be one of: {', '.join(graphs.VIEWS)}.")
        if not 1 <= minutes <= graphs.MAX_MINUTES:
            raise ValidationError(f"minutes must be from 1 to {graphs.MAX_MINUTES}.")
        conn = self._r
        now = datetime.now(UTC)
        shown = await self.get_project(project) if project else None
        projects = [shown] if shown else await repo.list_projects(conn)
        project_id = shown.id if shown else None
        tasks = await repo.list_tasks(
            conn, project_id=project_id, stages=[s.value for s in OPEN_STAGES]
        )
        since = (now - timedelta(days=graphs.RECENT_DAYS)).isoformat(timespec="milliseconds")
        recent_since = (now - timedelta(minutes=minutes)).isoformat(timespec="milliseconds")
        bound = {t.session_id for t in tasks}
        sessions = [
            s
            for s in await repo.list_sessions(conn, project_id, graphs.SESSIONS_MAX)
            if s.id in bound or s.last_seen_at >= since
        ]
        environments = await repo.list_environments(conn, [p.id for p in projects])
        policies = await self._policies(conn, environments)
        messages = await repo.map_messages(conn, since, recent_since, graphs.MESSAGES_MAX)
        recipients: dict[str, Session] = {}
        for name in {m.recipient for m in messages}:
            found = await repo.find_sessions(conn, name)
            named = found[0] if found else await repo.get_session_by_cc(conn, name)
            if named is not None:
                recipients[name] = named
        data = graphs.MapData(
            projects=projects,
            tasks=tasks,
            sessions=sessions,
            environments=environments,
            levels={e.id: policies[e.id].level_for(e) for e in environments},
            comments=await repo.map_comments(conn),
            messages=messages,
            recipients=recipients,
            owner_names={name.strip().casefold() for name in self.owner_names},
        )
        if view == "now":
            data.now_comment_tasks = await repo.map_recent_comment_tasks(conn, recent_since)
            by_name = {(e.project_id, e.name): e.id for e in environments}
            for event in await repo.map_recent_events(conn, recent_since, MAP_RECENT_EVENTS):
                assert event.session_id is not None
                if event.kind == "live.action":
                    env_id = by_name.get((event.project_id, event.payload.get("environment")))
                    if env_id is not None:
                        data.now_access.add((event.session_id, env_id))
                elif event.task_id is not None:
                    data.now_bindings.add((event.session_id, event.task_id))
        return graphs.build_graph(data, view, minutes, shown.slug if shown else None)

    # --- usage -------------------------------------------------------------

    async def usage_pass(
        self,
        *,
        budget: int = usage_data.PASS_BYTES,
        chunk_bytes: int = usage_data.CHUNK_BYTES,
        max_lines: int = usage_data.LINES_PER_TRANSACTION,
    ) -> tuple[int, bool]:
        """One bounded round over the tracked transcripts: returns the messages newly counted and
        whether a file is still behind. Files are read in a worker thread with no transaction
        open; each read is stored in one transaction. A file that fails for any reason keeps its
        rows, the page says so, and the next file is read. Only a read that moved on is charged to
        `budget`: a partial line waiting for its end costs nothing and goes to the back."""
        spent = counted = 0
        await self._find_subagent_files()
        for file in await repo.list_usage_files(self._r):
            offset, skipping, error = file.read_offset, file.skipping, file.error
            try:
                while True:
                    if spent >= budget:
                        return counted, True
                    chunk = await asyncio.to_thread(
                        usage_data.read_file, file.path, offset, skipping, chunk_bytes, max_lines
                    )
                    moved = chunk.shrunk or chunk.offset != offset or chunk.skipping != skipping
                    if moved:
                        spent += chunk.read_bytes
                    if moved or chunk.lines or chunk.unknown or error is not None:
                        async with self._tx() as tx:
                            counted += await repo.apply_usage_chunk(tx.conn, file.id, chunk, tx.now)
                    elif chunk.read_bytes:
                        async with self._tx() as tx:
                            await repo.touch_usage_file(tx.conn, file.id, tx.now)
                    offset, skipping, error = chunk.offset, chunk.skipping, None
                    if not (moved and chunk.behind):
                        break
            except FileNotFoundError:
                continue  # not written yet, or removed: what was read stays counted
            except Exception as exc:
                await self._usage_file_failed(file, exc, error)
        return counted, False

    async def _usage_file_failed(
        self, file: usage_data.UsageFile, exc: Exception, previous: str | None
    ) -> None:
        """Only the kind of failure is kept: an exception's text could quote a line."""
        reason = (exc.strerror if isinstance(exc, OSError) else None) or type(exc).__name__
        logger.warning("Usage: transcript file #%d not read: %s", file.id, reason)
        if reason != previous:
            async with self._tx() as tx:
                await repo.set_usage_error(tx.conn, file.id, reason, tx.now)

    async def _find_subagent_files(self) -> None:
        """Track the subagent transcripts beside each reported transcript, for the same session,
        up to `SUBAGENT_FILES_MAX` per session. Here and not on the hook path: they appear while
        a session works, long after it reported its transcript."""
        files = await repo.list_usage_files(self._r)
        known = {f.path for f in files}
        per_session: dict[int | None, int] = {}
        for file in files:
            if file.parent_id is not None:
                per_session[file.session_id] = per_session.get(file.session_id, 0) + 1
        for file in files:
            if file.parent_id is not None:
                continue
            used = per_session.get(file.session_id, 0)
            room = max(usage_data.SUBAGENT_FILES_MAX - used, 0)
            try:
                found, over = await asyncio.to_thread(
                    usage_data.subagent_files, file.path, known, room
                )
                self._usage_capped[file.id] = over
                if found:
                    async with self._tx() as tx:
                        await repo.add_subagent_files(tx.conn, file, found, tx.now)
                    known.update(found)
                    per_session[file.session_id] = used + len(found)
            except Exception as exc:
                await self._usage_file_failed(file, exc, file.error)

    async def usage_loop(self, interval: float) -> None:
        """The background reader: never on the hook path, sooner again while a file is behind."""
        while True:
            behind = False
            try:
                _, behind = await self.usage_pass()
            except Exception:
                logger.exception("Reading transcripts failed; the next pass tries again")
            await asyncio.sleep(usage_data.CATCH_UP_SECONDS if behind else interval)

    async def usage_session_ids(self, ref: str) -> list[int]:
        """Sessions a name, external id, alias or Claude Code id names."""
        found = await repo.find_sessions(self._r, ref)
        if not found:
            by_cc = await repo.get_session_by_cc(self._r, ref)
            found = [by_cc] if by_cc else []
        if not found:
            raise NotFoundError(f"Session {ref!r} not found.")
        return [s.id for s in found]

    async def usage_report(
        self,
        *,
        project: str | None = None,
        session_ids: list[int] | None = None,
        model: str | None = None,
        days: int = 30,
        group: str = "day",
    ) -> usage_data.UsageReport:
        """Tokens per period, session and model. `days` 0 means all of it."""
        if group not in ("day", "week"):
            raise ValidationError("group must be day or week.")
        if not 0 <= days <= usage_data.DAYS_MAX:
            raise ValidationError(f"days must be from 0 (all) to {usage_data.DAYS_MAX}.")
        today = datetime.now(UTC).date()
        since = (today - timedelta(days=days - 1)).isoformat() if days else None
        project_id = (await self.get_project(project)).id if project else None
        rows = await repo.usage_day_rows(
            self._r, since=since, project_id=project_id, session_ids=session_ids, model=model
        )
        files, unread, unknown = await repo.usage_file_stats(self._r)
        return usage_data.build_report(
            rows,
            group=group,
            days=days,
            today=today,
            files=files,
            unread_files=unread,
            unknown_lines=unknown,
            capped_files=sum(self._usage_capped.values()),
        )

    async def usage_filters(self) -> tuple[list[tuple[int, str]], list[str]]:
        return await repo.usage_sessions(self._r), await repo.usage_models(self._r)

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
            review=await self._load_review(self._r, task.id),
            bind_choices=await self._bind_choices(task),
            plan=await self._load_plan(self._r, task),
            todo=await self._task_todo(self._r, task),
            plan_history=await self._plan_history(task),
        )

    async def _bind_choices(self, task: Task, limit: int = 40) -> list[Session]:
        """Sessions the board offers to bind: the project's first, then the others, most
        recently seen first; the bound one is left out."""
        own = await repo.list_sessions(self._r, task.project_id, limit)
        seen = {s.id for s in own}
        others = [s for s in await repo.list_sessions(self._r, None, limit) if s.id not in seen]
        return [s for s in [*own, *others] if s.id != task.session_id][:limit]

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
        actor: str = "mcp",
    ) -> tuple[Project, bool]:
        slug = _clean_slug(slug)
        name = _clean_text(name, "name", NAME_MAX)
        given = {
            "name": name,
            "repo_path": _clean_text(repo_path, "repo_path"),
            "description": _clean_text(description, "description"),
        }
        default_actor = actor
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
        plan_status = task.plan_status if task.plan_total else None
        missing = unmet(
            rules,
            stage,
            report_since_stage_change=reported,
            values=values,
            plan_status=plan_status,
        )
        if missing and self.rules_mode == "strict":
            raise ValidationError(refusal_text(stage, missing))
        return [warning_text(stage, req, plan_status) for req in missing]

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
            if diff.get("stage") in ACCEPTANCE_STAGES and not task.review_total:
                _note(EMPTY_CHECKLIST_HINT.format(key=task.key))
            no_plan = diff.get("stage") == Stage.DEV and not task.plan_total
            if no_plan and not await self._task_todo(tx.conn, task):
                _note(NO_PLAN_STAGE_HINT.format(key=task.key))
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
                    title=diff.get("title", task.title),
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
            if "stage" in diff:
                await self._pulse_note(tx.conn, task)
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

    async def bind_session_row(self, ref: str | int, session_id: int, *, actor: str) -> Task:
        """Bind the session row the board's picker names (no name or alias resolving); the
        same binding history and `task.session_bound` event as `bind_session`."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            session = await repo.get_session_by_id(tx.conn, session_id)
            if session is None:
                raise NotFoundError(f"Session #{session_id} not found.")
            await self._bind(tx, task, session, actor)
            return await self.get_task(task.id, tx.conn)

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
            if size and not task.size:
                clean["size"] = size  # the first report's size sizes a task that has none
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
            await self._index(tx, "report", report_id)
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
        source: str | None = None,
        supersedes: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Decision:
        """Record a decision of a project (scope `task` when a task is given). `supersedes`
        marks a current decision of the same project as replaced by this one; nothing is
        deleted, and a decision that is already superseded cannot be superseded again."""
        clean = _clean_text(text, "text")
        if not clean:
            raise ValidationError("Decision text must not be empty.")
        decided_by = _clean_text(decided_by, "decided_by", NAME_MAX)
        origin = mem.parse_source(source)
        default_actor = decided_by or "mcp"
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            task_row = await self.get_task(task, tx.conn) if task is not None else None
            if task_row is not None and task_row.project_id != proj.id:
                raise ValidationError(f"Task {task_row.key} does not belong to {proj.slug}.")
            secrets = await self._secret_rules(tx.conn, proj)
            _scan(
                secrets,
                text=clean,
                source=origin.text,
                source_ref=origin.ref,
                decided_by=decided_by,
            )
            await self._check_source(tx.conn, origin, proj)
            old = None
            if supersedes is not None:
                old = await repo.get_decision(tx.conn, int(supersedes))
                if old is None:
                    raise NotFoundError(f"Decision #{supersedes} not found.")
                if old.project_id != proj.id:
                    raise ValidationError(
                        f"Decision #{old.id} belongs to {old.project_slug}, not {proj.slug}."
                    )
                if old.superseded_by is not None:
                    raise ConflictError(
                        f"Decision #{old.id} is already superseded by #{old.superseded_by};"
                        f" supersede #{old.superseded_by} instead."
                    )
            sess = await self._session(tx, session, session_title, hook_session)
            session_id = sess.id if sess else None
            actor = sess.name if sess else default_actor
            decision_id = await repo.insert_decision(
                tx.conn,
                proj.id,
                task_row.id if task_row else None,
                clean,
                decided_by,
                tx.now,
                origin,
            )
            if old is not None:
                await repo.supersede_decision(tx.conn, old.id, decision_id, tx.now)
            await self._index(tx, "decision", decision_id)
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
                **({"supersedes": old.id} if old else {}),
            )
            decision = await repo.get_decision(tx.conn, decision_id)
        assert decision is not None
        return decision

    async def list_decisions(
        self,
        project: str,
        *,
        task: str | int | None = None,
        query: str | None = None,
        include_superseded: bool = False,
        limit: int = 20,
    ) -> tuple[Project, list[Decision]]:
        """Decisions of a project (or of one task), newest first; with `query`, the best
        search matches first."""
        limit = max(1, min(int(limit), 200))
        proj = await self.get_project(project)
        task_row = await self.get_task(task) if task is not None else None
        if task_row is not None and task_row.project_id != proj.id:
            raise ValidationError(f"Task {task_row.key} does not belong to {proj.slug}.")
        task_id = task_row.id if task_row else None
        if not (query or "").strip():
            items = await repo.list_decisions(
                self._r,
                project_id=proj.id,
                task_id=task_id,
                include_superseded=include_superseded,
                limit=limit,
            )
            return proj, items
        words = mem.query_words(query or "")
        found = await self._search_ids(self._r, words, ("decision",), proj.id, limit * 5)
        ids = [item_id for _, item_id in found]
        by_id = {
            d.id: d
            for d in await repo.list_decisions(
                self._r,
                ids=ids,
                task_id=task_id,
                include_superseded=include_superseded,
                limit=len(ids) or 1,
            )
        }
        return proj, [by_id[i] for i in ids if i in by_id][:limit]

    # --- memory and documents ----------------------------------------------------------

    async def _index(self, tx: _Tx, kind: str, item_id: int) -> None:
        if self.search_fts:
            await repo.index_item(tx.conn, kind, item_id)

    async def _secret_rules(self, conn: aiosqlite.Connection, project: Project | None) -> Any:
        if project is None:
            return self.global_policy().secret_rules
        return (await self._policy_of(conn, project, None)).secret_rules

    async def _check_source(
        self, conn: aiosqlite.Connection, source: mem.Source, project: Project | None
    ) -> None:
        """A report, comment or event named as the source exists (in the same project, when
        both have one)."""
        if source.kind in (None, "message"):
            return
        number = int(source.ref or 0)
        if source.kind == "event":
            found = await repo.events_after(conn, number - 1, None, 1)
            owner = found[0].project_id if found and found[0].id == number else -1
        else:
            rows = await repo.search_rows(conn, source.kind, [number])
            owner = rows[0]["project_id"] if rows else -1
        if owner == -1:
            raise NotFoundError(f"source: {source.kind} #{number} not found.")
        if project is not None and owner is not None and owner != project.id:
            raise ValidationError(
                f"source: {source.kind} #{number} belongs to another project than {project.slug}."
            )

    async def _search_ids(
        self,
        conn: aiosqlite.Connection,
        words: list[str],
        kinds: tuple[str, ...],
        project_id: int | None,
        limit: int,
        any_word: bool = False,
    ) -> list[tuple[str, int]]:
        if self.search_fts:
            match = mem.fts_query(words, any_word)
            return await repo.fts_search(conn, match, kinds, project_id, limit)
        return await repo.like_search(conn, words, kinds, project_id, limit, any_word)

    async def search(
        self,
        query: str,
        *,
        project: str | None = None,
        kinds: list[str] | None = None,
        scope: str | None = None,
        note_kinds: list[str] | None = None,
        include_superseded: bool = False,
        limit: int = 10,
    ) -> list[mem.SearchHit]:
        """Full-text search over documents (current versions), decisions, memory notes,
        reports and comments. With `project`, that project's items plus global and role notes.
        `scope` and `note_kinds` narrow the notes (and, without `kinds`, search notes only)."""
        words = mem.query_words(query)
        limit = max(1, min(int(limit), mem.SEARCH_LIMIT_MAX))
        if scope is not None and scope not in mem.NOTE_SCOPES:
            raise ValidationError(f"scope must be one of: {', '.join(mem.NOTE_SCOPES)}.")
        for kind in note_kinds or []:
            if kind not in mem.NOTE_KINDS:
                raise ValidationError(f"kinds of notes are: {', '.join(mem.NOTE_KINDS)}.")
        wanted = tuple(kinds or (["note"] if scope or note_kinds else mem.SEARCH_KINDS))
        for kind in wanted:
            if kind not in mem.SEARCH_KINDS:
                raise ValidationError(
                    f"Unknown kind {kind!r}; search covers: {', '.join(mem.SEARCH_KINDS)}."
                )
        project_id = (await self.get_project(project)).id if project else None
        found = await self._search_ids(self._r, words, wanted, project_id, limit * 5)
        rows: dict[tuple[str, int], Any] = {}
        for kind in wanted:
            ids = [item_id for k, item_id in found if k == kind]
            for row in await repo.search_rows(self._r, kind, ids):
                rows[(kind, row["id"])] = row
        hits = []
        for ref in found:
            row = rows.get(ref)
            if row is None or (row["superseded_at"] and not include_superseded):
                continue
            if row["kind"] == "note" and (
                (scope and row["note_scope"] != scope)
                or (note_kinds and row["note_kind"] not in note_kinds)
            ):
                continue
            line, snippet = mem.matching_line(row["body"] or "", words)
            hits.append(
                mem.SearchHit(
                    kind=row["kind"],
                    id=row["id"],
                    project_slug=row["project_slug"],
                    task_key=row["task_key"],
                    name=row["name"],
                    title=row["title"] or _first_words(row["body"] or ""),
                    line=line,
                    snippet=snippet,
                )
            )
            if len(hits) == limit:
                break
        return hits

    # Documents

    async def list_documents(self, project: str) -> tuple[Project, list[mem.Document]]:
        proj = await self.get_project(project)
        return proj, await repo.list_documents(self._r, proj.id)

    async def _document(
        self, conn: aiosqlite.Connection, project: str, doc: str
    ) -> tuple[Project, mem.Document]:
        proj = await self.get_project(project, conn)
        slug = _clean_doc_slug(doc)
        found = await repo.get_document(conn, proj.id, slug)
        if found is None:
            names = ", ".join(d.slug for d in await repo.list_documents(conn, proj.id)) or "none"
            raise NotFoundError(f"{proj.slug} has no document {slug!r}; documents: {names}.")
        return proj, found

    async def get_document(
        self,
        project: str,
        doc: str,
        *,
        version: int | None = None,
        section: str | None = None,
    ) -> tuple[mem.Document, str]:
        """A document with the text of `version` (the current one by default) and that text,
        or only one section of it (the heading up to the next heading of its level)."""
        _, found = await self._document(self._r, project, doc)
        wanted = found.version if version is None else int(version)
        row = await repo.get_document_version(self._r, found.id, wanted)
        if row is None:
            raise NotFoundError(
                f"{found.slug} has no version {wanted}; versions are 1 to {found.version}."
            )
        found.body, found.shown_version, found.author = row.body, row.version, row.author
        text = mem.section_text(row.body, section) if section else row.body
        return found, text

    async def document_history(
        self, project: str, doc: str
    ) -> tuple[mem.Document, list[mem.DocumentVersion]]:
        _, found = await self._document(self._r, project, doc)
        return found, await repo.list_document_versions(self._r, found.id)

    async def document_diff(
        self, project: str, doc: str, from_version: int, to_version: int
    ) -> tuple[mem.Document, list[str]]:
        """The unified diff between two versions of a document (any order)."""
        _, found = await self._document(self._r, project, doc)
        texts = []
        for number in (int(from_version), int(to_version)):
            row = await repo.get_document_version(self._r, found.id, number)
            if row is None:
                raise NotFoundError(
                    f"{found.slug} has no version {number}; versions are 1 to {found.version}."
                )
            texts.append(row.body)
        lines = mem.diff_lines(
            texts[0], texts[1], f"{found.slug} v{from_version}", f"{found.slug} v{to_version}"
        )
        return found, lines

    async def update_document(
        self,
        project: str,
        doc: str,
        body: str,
        reason: str,
        base_version: int,
        *,
        section: str | None = None,
        title: str | None = None,
        kind: str | None = None,
        author: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> mem.Document:
        """Write a new version of a document; `base_version` 0 creates it. With `section`, the
        text replaces only that section. A `base_version` other than the current version is a
        conflict whose message holds the current text to merge."""
        slug = _clean_doc_slug(doc)
        text = _clean_text(body, "body", mem.DOC_BODY_MAX)
        if not text:
            raise ValidationError("body must not be empty.")
        why = _clean_text(reason, "reason", mem.REASON_MAX)
        if not why:
            raise ValidationError("reason must say what changed and why.")
        new_title = _clean_text(title, "title", mem.TITLE_MAX)
        new_kind = _clean_text(kind, "kind", mem.DOC_KIND_MAX)
        base = _clean_version(base_version)
        default_actor = _clean_text(author, "author", NAME_MAX) or "mcp"
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            _scan(
                await self._secret_rules(tx.conn, proj),
                body=text,
                title=new_title,
                kind=new_kind,
                reason=why,
            )
            found = await repo.get_document(tx.conn, proj.id, slug)
            current = ""
            if found is None:
                if base != 0:
                    raise NotFoundError(
                        f"{proj.slug} has no document {slug!r}; create it with base_version 0."
                    )
                if section:
                    raise ValidationError("A new document is written whole, without section.")
                heading = next((s.title for s in mem.sections(text) if s.level == 1), None)
                doc_id = await repo.insert_document(
                    tx.conn, proj.id, slug, new_title or heading or slug, new_kind or "doc", tx.now
                )
                found = await repo.get_document(tx.conn, proj.id, slug)
                assert found is not None and found.id == doc_id
            else:
                latest = await repo.get_document_version(tx.conn, found.id, found.version)
                assert latest is not None
                current = latest.body
                if base != found.version:
                    shown = current
                    if section and any(
                        s.anchor == section.strip().lstrip("#").casefold()
                        for s in mem.sections(current)
                    ):
                        shown = mem.section_text(current, section)
                    raise ConflictError(
                        f"{found.slug} is at version {found.version}, not {base}: someone"
                        f" changed it since you read it. Merge your change into the current"
                        f" {'section' if shown is not current else 'text'} below and send it"
                        f" again with base_version {found.version}.\n\n{shown}"
                    )
            new_body = mem.replace_section(current, section, text) if section else text
            if len(new_body) > mem.DOC_BODY_MAX:
                raise ValidationError(f"The document would exceed {mem.DOC_BODY_MAX} characters.")
            final_title = new_title or found.title
            final_kind = new_kind or found.kind
            if found.version and (new_body, final_title, final_kind) == (
                current,
                found.title,
                found.kind,
            ):
                _note(f"{found.slug} is unchanged; it stays at version {found.version}.")
                return found
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else default_actor
            version = found.version + 1
            anchor = mem.find_section(current, section).anchor if section else None
            await repo.add_document_version(
                tx.conn,
                found.id,
                version=version,
                body=new_body,
                author=actor,
                reason=why,
                base_version=base,
                section=anchor,
                title=final_title,
                kind=final_kind,
                now=tx.now,
            )
            await self._index(tx, "doc", found.id)
            await tx.event(
                "document.updated",
                actor,
                project_id=proj.id,
                session_id=sess.id if sess else None,
                project=proj.slug,
                doc=found.slug,
                version=version,
                reason=why[:200],
                **({"section": anchor} if anchor else {}),
                **({"created": True} if version == 1 else {}),
            )
            saved = await repo.get_document(tx.conn, proj.id, slug)
        assert saved is not None
        saved.body, saved.shown_version, saved.author = new_body, version, actor
        return saved

    # Memory notes

    async def _note_or_404(self, conn: aiosqlite.Connection, note_id: Any) -> mem.MemoryNote:
        try:
            number = int(note_id)
        except (TypeError, ValueError):
            raise ValidationError(f"A note id is a number, got {note_id!r}.") from None
        found = await repo.get_note(conn, number)
        if found is None:
            raise NotFoundError(f"Memory note #{number} not found.")
        return found

    async def get_note(self, note_id: int) -> mem.MemoryNote:
        """A note; reading it counts (`last_read_at`, `read_count`)."""
        async with self._tx() as tx:
            found = await self._note_or_404(tx.conn, note_id)
            await repo.mark_note_read(tx.conn, found.id, tx.now)
            return await self._note_or_404(tx.conn, found.id)

    async def list_notes(
        self, project: str | None = None, *, include_superseded: bool = False
    ) -> list[mem.MemoryNote]:
        """A project's notes plus the global and role notes (only those without a project)."""
        project_id = (await self.get_project(project)).id if project else None
        return await repo.list_notes(self._r, project_id, include_superseded=include_superseded)

    async def _note_candidates(
        self,
        conn: aiosqlite.Connection,
        title: str,
        scope: str,
        project_id: int | None,
        role: str | None,
    ) -> list[tuple[mem.MemoryNote, float]]:
        """Current notes of the same scope whose title or body shares a word with `title`,
        most similar title first."""
        words = mem.candidate_words(title)
        if not words:
            return []
        # Filtered by scope after the search: a wide limit keeps a duplicate of a global or
        # role note from dropping out behind many project notes.
        found = await self._search_ids(conn, words, ("note",), project_id, 1000, any_word=True)
        notes = await repo.get_notes(conn, [item_id for _, item_id in found])
        order = {item_id: n for n, (_, item_id) in enumerate(found)}
        same = [
            n
            for n in notes
            if n.current and n.scope == scope and n.project_id == project_id and n.role == role
        ]
        scored = [(n, mem.title_similarity(title, n.title)) for n in same]
        scored.sort(key=lambda pair: (-pair[1], order[pair[0].id]))
        return scored[: mem.CANDIDATES_MAX]

    async def add_note(
        self,
        scope: str,
        kind: str,
        title: str,
        body: str,
        *,
        project: str | None = None,
        role: str | None = None,
        tags: str | None = None,
        links: str | None = None,
        source: str | None = None,
        review_at: str | None = None,
        op: str | None = None,
        target: int | None = None,
        base_version: int | None = None,
        force_new: bool = False,
        author: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> NoteResult:
        """Add a memory note in two steps. Without `op`, notes like it are looked up first:
        if there are any, nothing is written and they come back as candidates; the caller
        answers with `op` add / update / supersede / noop (`target` = the candidate's id for
        update and supersede). Adding next to a strong match needs `force_new`."""
        scope = str(scope or "").strip().lower()
        if scope not in mem.NOTE_SCOPES:
            raise ValidationError(f"scope must be one of: {', '.join(mem.NOTE_SCOPES)}.")
        kind = str(kind or "").strip().lower()
        if kind not in mem.NOTE_KINDS:
            raise ValidationError(f"kind must be one of: {', '.join(mem.NOTE_KINDS)}.")
        op = (op or "").strip().lower() or None
        if op is not None and op not in mem.NOTE_OPS:
            raise ValidationError(f"op must be one of: {', '.join(mem.NOTE_OPS)}.")
        if op in ("update", "supersede") and target is None:
            raise ValidationError(f"op={op} needs target: the id of the note to {op}.")
        if op == "update" and base_version is None:
            raise ValidationError(
                "op=update needs base_version: the candidate's version (shown as v<n>)."
            )
        clean_title = _clean_text(title, "title", mem.TITLE_MAX)
        clean_body = _clean_text(body, "body", mem.NOTE_BODY_MAX)
        if not clean_title or not clean_body:
            raise ValidationError("A note needs a title and a body (fact, why, how to apply).")
        clean_role = _clean_text(role, "role", mem.ROLE_MAX)
        clean_role = clean_role.lower() if clean_role else None
        if scope == "role" and not clean_role:
            raise ValidationError("A role note needs role, e.g. 'coordinator' or 'manager'.")
        if scope != "role" and clean_role:
            raise ValidationError("role is set only on scope 'role'.")
        if scope == "project" and not project:
            raise ValidationError("A project note needs project.")
        if scope != "project" and project:
            raise ValidationError(
                f"A {scope} note has no project; use scope 'project' for a project's note."
            )
        clean_tags = _clean_tags(tags)
        clean_links = _clean_text(links, "links", mem.LINKS_MAX)
        review = _clean_date(review_at, "review_at")
        origin = mem.parse_source(source)
        default_actor = _clean_text(author, "author", NAME_MAX) or "mcp"
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn) if project else None
            project_id = proj.id if proj else None
            _scan(
                await self._secret_rules(tx.conn, proj),
                title=clean_title,
                body=clean_body,
                tags=clean_tags,
                links=clean_links,
                role=clean_role,
                source=origin.text,
                source_ref=origin.ref,
            )
            await self._check_source(tx.conn, origin, proj)
            old = await self._note_or_404(tx.conn, target) if target is not None else None
            if op == "noop":
                return NoteResult("noop", old, [])
            if old is not None and not old.current:
                raise ConflictError(
                    f"Note #{old.id} is already superseded by #{old.superseded_by}; use"
                    f" #{old.superseded_by} as target."
                )
            if old is not None and (old.scope, old.project_id, old.role) != (
                scope,
                project_id,
                clean_role,
            ):
                raise ValidationError(
                    f"Note #{old.id} is a {old.scope} note"
                    f" ({old.project_slug or old.role or 'global'}); the target must have the"
                    " same scope."
                )
            if op in (None, "add"):
                candidates = await self._note_candidates(
                    tx.conn, clean_title, scope, project_id, clean_role
                )
                if op is None and candidates:
                    return NoteResult("candidates", None, candidates)
                strong = [(n, s) for n, s in candidates if s >= mem.STRONG_MATCH]
                if strong and not force_new:
                    names = ", ".join(f"#{n.id} {n.title!r}" for n, _ in strong)
                    raise ConflictError(
                        f"A note like this exists ({names}): update or supersede it, or pass"
                        " force_new=true to add a separate note anyway."
                    )
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else default_actor
            session_id = sess.id if sess else None
            if op == "update":
                assert old is not None
                base = _clean_version(base_version)
                if base != old.version:
                    raise ConflictError(
                        f"Note #{old.id} is at version {old.version}, not {base}: someone changed"
                        f" it since you read it. Merge into the current note below and send it"
                        f" again with base_version {old.version}.\n\n# {old.title}\n{old.body}"
                    )
                changes = {"title": clean_title, "body": clean_body}
                if tags is not None:
                    changes["tags"] = clean_tags
                if review_at is not None:
                    changes["review_at"] = review
                await repo.update_note(tx.conn, old.id, changes, tx.now)
                await self._index(tx, "note", old.id)
                saved = await self._note_or_404(tx.conn, old.id)
                await self._note_event(tx, "memory.updated", saved, actor, session_id)
                return NoteResult("update", saved, [])
            note_id = await repo.insert_note(
                tx.conn,
                scope=scope,
                project_id=project_id,
                role=clean_role,
                kind=kind,
                title=clean_title,
                body=clean_body,
                tags=clean_tags,
                source=origin,
                review_at=review,
                author=actor,
                now=tx.now,
                links=clean_links,
            )
            if op == "supersede":
                assert old is not None
                await repo.supersede_note(tx.conn, old.id, note_id, tx.now)
            await self._index(tx, "note", note_id)
            saved = await self._note_or_404(tx.conn, note_id)
            await self._note_event(
                tx, "memory.added", saved, actor, session_id, supersedes=old.id if old else None
            )
            return NoteResult("supersede" if op == "supersede" else "add", saved, [])

    async def _note_event(
        self,
        tx: _Tx,
        kind: str,
        note: mem.MemoryNote,
        actor: str,
        session_id: int | None,
        **extra: Any,
    ) -> None:
        await tx.event(
            kind,
            actor,
            project_id=note.project_id,
            session_id=session_id,
            project=note.project_slug,
            note_id=note.id,
            scope=note.scope,
            title=note.title[:200],
            version=note.version,
            **{k: v for k, v in extra.items() if v is not None},
        )

    async def update_note(
        self,
        note_id: int,
        base_version: int,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: str | None = None,
        links: str | None = None,
        review_at: str | None = None,
        author: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> mem.MemoryNote:
        """Change a note's title, body, tags or review date ('' clears tags and the review
        date). A `base_version` other than the note's version is a conflict that returns the
        current note to merge."""
        changes: dict[str, Any] = {}
        if title is not None:
            changes["title"] = _clean_text(title, "title", mem.TITLE_MAX)
            if not changes["title"]:
                raise ValidationError("title must not be empty.")
        if body is not None:
            changes["body"] = _clean_text(body, "body", mem.NOTE_BODY_MAX)
            if not changes["body"]:
                raise ValidationError("body must not be empty.")
        if tags is not None:
            changes["tags"] = _clean_tags(tags)
        if links is not None:
            changes["links"] = _clean_text(links, "links", mem.LINKS_MAX)
        if review_at is not None:
            changes["review_at"] = _clean_date(review_at, "review_at")
        if not changes:
            raise ValidationError("Nothing to change: pass title, body, tags or review_at.")
        base = _clean_version(base_version)
        default_actor = _clean_text(author, "author", NAME_MAX) or "mcp"
        async with self._tx() as tx:
            found = await self._note_or_404(tx.conn, note_id)
            if not found.current:
                raise ConflictError(
                    f"Note #{found.id} is superseded by #{found.superseded_by}; change that one."
                )
            if base != found.version:
                raise ConflictError(
                    f"Note #{found.id} is at version {found.version}, not {base}: someone changed"
                    f" it since you read it. Merge into the current note below and send it again"
                    f" with base_version {found.version}.\n\n# {found.title}\n{found.body}"
                )
            proj = (
                await self.get_project(found.project_slug, tx.conn) if found.project_slug else None
            )
            _scan(await self._secret_rules(tx.conn, proj), **changes)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else default_actor
            await repo.update_note(tx.conn, found.id, changes, tx.now)
            await self._index(tx, "note", found.id)
            saved = await self._note_or_404(tx.conn, found.id)
            await self._note_event(tx, "memory.updated", saved, actor, sess.id if sess else None)
            return saved

    # Markdown import and export

    async def import_items(self) -> list[ImportItem]:
        return await repo.list_import_items(self._r)

    async def record_import_item(
        self,
        kind: str,
        item: str,
        source_path: str,
        target_id: int,
        content_hash: str,
        target_hash: str,
    ) -> None:
        """Remember what an import wrote. Bookkeeping only: no event, nothing on the board."""
        async with self._tx() as tx:
            row = ImportItem(kind, item, source_path, target_id, content_hash, target_hash, tx.now)
            await repo.save_import_item(tx.conn, row)

    async def forget_import_item(self, kind: str, item: str) -> None:
        async with self._tx() as tx:
            await repo.delete_import_item(tx.conn, kind, item)

    async def secret_rules(self, project: str | None) -> SecretRules:
        """The secret rules of a project; of the global policy for a project not created yet."""
        found = await repo.get_project(self._r, project) if project else None
        rules: SecretRules = await self._secret_rules(self._r, found)
        return rules

    async def all_decisions(self, project_id: int) -> list[Decision]:
        """Every decision of a project, superseded ones too, oldest first."""
        items = await repo.list_decisions(
            self._r, project_id=project_id, include_superseded=True, limit=-1
        )
        return items[::-1]

    async def notes_by_id(self, ids: list[int]) -> list[mem.MemoryNote]:
        """Notes by id, superseded ones too; unlike `get_note`, reading them does not count."""
        return await repo.get_notes(self._r, ids)

    async def all_notes(self) -> list[mem.MemoryNote]:
        """Every current note: global, role and of any project, oldest first."""
        return await repo.list_all_notes(self._r)

    # Blocks

    async def list_blocks(self, project: str | None = None) -> list[mem.MemoryBlock]:
        """The blocks of a project, or the global ones."""
        project_id = (await self.get_project(project)).id if project else None
        return await repo.list_blocks(self._r, project_id)

    async def get_block(self, label: str, project: str | None = None) -> mem.MemoryBlock:
        clean = _clean_block_label(label)
        proj = await self.get_project(project) if project else None
        found = await repo.get_block(self._r, proj.id if proj else None, clean)
        if found is None:
            where = proj.slug if proj else "global scope"
            labels = ", ".join(
                b.label for b in await repo.list_blocks(self._r, proj.id if proj else None)
            )
            raise NotFoundError(
                f"No block {clean!r} in {where}; blocks: {labels or 'none'}."
                " block_set with base_version 0 creates one."
            )
        return found

    async def set_block(
        self,
        label: str,
        body: str,
        base_version: int,
        *,
        project: str | None = None,
        description: str | None = None,
        char_limit: int | None = None,
        owner_only: bool | None = None,
        by_owner: bool = False,
        author: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> mem.MemoryBlock:
        """Write a block (`base_version` 0 creates it). The body must fit the block's character
        limit. Blocks marked owner-only, and that mark itself, change only from the board."""
        clean = _clean_block_label(label)
        text = _clean_text(body, "body", mem.BLOCK_LIMIT_MAX) or ""
        about = _clean_text(description, "description", mem.TITLE_MAX)
        base = _clean_version(base_version)
        if owner_only is not None and not by_owner:
            raise ValidationError("Only the owner marks a block owner-only, on the board.")
        limit = None
        if char_limit is not None:
            if isinstance(char_limit, bool) or not 1 <= int(char_limit) <= mem.BLOCK_LIMIT_MAX:
                raise ValidationError(f"char_limit must be 1 to {mem.BLOCK_LIMIT_MAX}.")
            limit = int(char_limit)
        default_actor = "owner" if by_owner else _clean_text(author, "author", NAME_MAX) or "mcp"
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn) if project else None
            project_id = proj.id if proj else None
            found = await repo.get_block(tx.conn, project_id, clean)
            if found is not None and found.owner_only and not by_owner:
                raise ValidationError(
                    f"Block {clean!r} is owner-only: the owner edits it on the board."
                )
            if base != (found.version if found else 0):
                if found is None:
                    raise NotFoundError(f"No block {clean!r} yet; create it with base_version 0.")
                raise ConflictError(
                    f"Block {clean!r} is at version {found.version}, not {base}. Merge your"
                    f" change into the current text below and send it again with base_version"
                    f" {found.version}.\n\n{found.body}"
                )
            final_limit = limit or (found.char_limit if found else mem.BLOCK_LIMIT_DEFAULT)
            if len(text) > final_limit:
                raise ValidationError(
                    f"Block {clean!r} takes at most {final_limit} characters; this text has"
                    f" {len(text)}. Shorten it (or raise char_limit)."
                )
            _scan(await self._secret_rules(tx.conn, proj), body=text, description=about)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else default_actor
            final_about = (
                about if description is not None else (found.description if found else None)
            )
            final_owner = owner_only if owner_only is not None else bool(found and found.owner_only)
            if found is None:
                await repo.insert_block(
                    tx.conn,
                    project_id=project_id,
                    label=clean,
                    description=final_about,
                    body=text,
                    char_limit=final_limit,
                    owner_only=final_owner,
                    actor=actor,
                    now=tx.now,
                )
            else:
                await repo.update_block(
                    tx.conn,
                    found.id,
                    description=final_about,
                    body=text,
                    char_limit=final_limit,
                    owner_only=final_owner,
                    actor=actor,
                    now=tx.now,
                )
            saved = await repo.get_block(tx.conn, project_id, clean)
            assert saved is not None
            await tx.event(
                "block.updated",
                actor,
                project_id=project_id,
                session_id=sess.id if sess else None,
                project=proj.slug if proj else None,
                label=clean,
                scope=saved.scope,
                version=saved.version,
                **({"created": True} if found is None else {}),
            )
            return saved

    # --- pulse and digests -------------------------------------------------

    def pulse_stale(self, project: Project) -> bool:
        return digests.pulse_stale(project, self.pulse_stale_days, datetime.now(UTC))

    async def _pulse_note(self, conn: aiosqlite.Connection, task: Task) -> None:
        """One soft line to an MCP caller's stage move, once per project per server run. A board
        move has nobody to tell, so it does not use up the once."""
        if _NOTES.get() is None or task.project_id in self._pulse_noted:
            return
        project = await self.get_project(task.project_slug, conn)
        if project.pulse_at and not self.pulse_stale(project):
            return
        self._pulse_noted.add(task.project_id)
        state = f"older than {self.pulse_stale_days} days" if project.pulse_at else "missing"
        _note(
            f"Note: the pulse of {project.slug} is {state}; keep it current with"
            " project_pulse_set (at most 5 lines)."
        )

    async def set_pulse(
        self,
        project: str,
        text: str,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "mcp",
        at: str | None = None,
    ) -> Project:
        """Write the project's pulse. `at` records another time than now (the demo's stale one)."""
        clean = str(text).replace("\r\n", "\n").strip()
        if not clean:
            raise ValidationError("A pulse must not be empty.")
        digests.check_pulse(clean)
        default_actor = actor
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            _scan(await self._secret_rules(tx.conn, proj), text=clean)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else default_actor
            await repo.update_project(
                tx.conn,
                proj.id,
                {"pulse": clean, "pulse_at": at or tx.now, "pulse_by": actor},
                tx.now,
            )
            await tx.event(
                "project.pulse_set",
                actor,
                project_id=proj.id,
                session_id=sess.id if sess else None,
                project=proj.slug,
                text=_first_words(clean, 200),
            )
            return await self.get_project(proj.slug, tx.conn)

    @staticmethod
    def _reader_name(session: Session) -> str:
        return session.external_id or session.name

    def _budget(self, value: int | None) -> int:
        if value is None:
            return self.digest_budget_chars
        return max(limits.BUDGET_MIN, min(limits.BUDGET_MAX, int(value)))

    async def _digest_reader(
        self, session: str | None, title: str | None, hook_session: str | None
    ) -> Session | None:
        if not (session or "").strip():
            return None
        async with self._tx() as tx:
            return await self._session(tx, session, title, hook_session)

    async def _first_read_start(self) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=digests.FIRST_READ_HOURS)
        return await repo.last_event_before(self._r, cutoff.isoformat(timespec="seconds"))

    async def _changes(
        self,
        level: str,
        ref_id: int,
        reader: Session | None,
        since_event: int | None,
        *,
        project_id: int | None = None,
        task_id: int | None = None,
        new_context: bool = False,
    ) -> tuple[digests.Changes, bool]:
        """What changed since the reader's cursor (or `since_event`, or the last 24 hours), and
        whether reading it moves the cursor. A new context has read nothing yet, so it gets at
        least the last 24 hours. Nothing new after the cursor leaves it in place: the next read
        shows the same either way, and this read needs no write."""
        reached = await repo.last_event_id(self._r)
        day = f"in the last {digests.FIRST_READ_HOURS} hours"
        cursor = None
        if since_event is not None:
            after, label = int(since_event), f"since event #{int(since_event)}"
        else:
            if reader is not None:
                name = self._reader_name(reader)
                cursor = await repo.reader_cursor(self._r, name, level, ref_id)
            if cursor is None:
                after, label = await self._first_read_start(), day
            elif not new_context:
                after, label = cursor, "since your last read"
            else:
                after = min(cursor, await self._first_read_start())
                label = "since this session last read" if after == cursor else day
        rows = await repo.event_counts(
            self._r,
            after,
            reached,
            exclude_kinds=digests.NOISE_KINDS,
            project_id=project_id,
            task_id=task_id,
            own_session_id=reader.id if reader is not None and level == "task" else None,
            own_kinds=digests.OWN_HOOK_KINDS,
        )
        groups = digests.group_changes(rows)
        news = cursor is None or any(g.last_id > cursor for g in groups)
        moves = reader is not None and since_event is None and news
        return digests.Changes(after, reached, label, groups), moves

    def _footer(
        self,
        reader: Session | None,
        since_event: int | None,
        changes: digests.Changes,
        moves: bool,
    ) -> Callable[[bool], str]:
        name = self._reader_name(reader) if reader is not None else None
        return lambda complete: digests.footer_text(
            changes.reached, name, since_event, moves, complete
        )

    async def _move_cursor(
        self,
        level: str,
        ref_id: int,
        reader: Session | None,
        reached: int,
        moves: bool,
        rendered: digests.Rendered,
    ) -> None:
        """After the digest is built, and only when a budget cut left its changes whole: what the
        reader did not get stays ahead of the cursor."""
        if reader is not None and moves and rendered.changes_complete:
            name = self._reader_name(reader)
            async with self._tx() as tx:
                await repo.set_reader_cursor(tx.conn, name, level, ref_id, reached, tx.now)

    async def _environment_levels(self, project_id: int) -> list[tuple[Environment, str]]:
        environments = await repo.list_environments(self._r, [project_id])
        policies = await self._policies(self._r, environments)
        return [(e, policies[e.id].level_for(e)) for e in environments]

    async def portfolio_digest(
        self,
        *,
        budget: int | None = None,
        since_event: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> str:
        """All projects: stage counts and pulse, what waits for the owner, the week's decisions,
        tasks without progress, global blocks, changes since the reader's last read."""
        reader = await self._digest_reader(session, session_title, hook_session)
        changes, moves = await self._changes("portfolio", 0, reader, since_event)
        now = datetime.now(UTC)
        since = (now - timedelta(days=digests.RECENT_DECISION_DAYS)).isoformat(timespec="seconds")
        stale_before = (now - timedelta(days=self.task_stale_days)).isoformat(timespec="seconds")
        open_tasks = await repo.list_tasks(self._r, stages=[s.value for s in OPEN_STAGES])
        stale = [t for t in open_tasks if t.updated_at < stale_before]
        data = digests.PortfolioData(
            projects=[(s.project, s.counts) for s in await self.list_projects()],
            inbox=await self.inbox(
                self.owner_names,
                require_tracker_log=self.require_tracker_log,
                done_window_days=self.done_window_days,
            ),
            decisions=await repo.list_decisions(self._r, since=since, limit=digests.DECISIONS_MAX),
            decisions_total=await repo.count_current_decisions(self._r, since=since),
            stale_tasks=sorted(stale, key=lambda t: t.updated_at),
            blocks=await repo.list_blocks(self._r, None),
            changes=changes,
            pulse_stale_days=self.pulse_stale_days,
            task_stale_days=self.task_stale_days,
            now=now,
        )
        footer = self._footer(reader, since_event, changes, moves)
        rendered = digests.portfolio_text(data, footer, self._budget(budget))
        await self._move_cursor("portfolio", 0, reader, changes.reached, moves, rendered)
        return rendered.text

    async def _project_digest(
        self,
        project: Project,
        reader: Session | None,
        since_event: int | None,
        budget: int,
        new_context: bool = False,
    ) -> str:
        changes, moves = await self._changes(
            "project",
            project.id,
            reader,
            since_event,
            project_id=project.id,
            new_context=new_context,
        )
        notes = await repo.list_notes(self._r, project.id, limit=digests.NOTES_MAX)
        counts = await repo.stage_counts(self._r)
        data = digests.ProjectData(
            project=project,
            counts=counts.get(project.id, {}),
            tasks=await repo.list_tasks(
                self._r, project_id=project.id, stages=[s.value for s in OPEN_STAGES]
            ),
            owner_names={name.strip().casefold() for name in self.owner_names},
            decisions=await repo.list_decisions(
                self._r, project_id=project.id, limit=digests.DECISIONS_MAX
            ),
            decisions_total=await repo.count_current_decisions(self._r, project_id=project.id),
            environments=await self._environment_levels(project.id),
            blocks=await repo.list_blocks(self._r, project.id),
            notes=[n for n in notes if n.scope in ("project", "global")],
            changes=changes,
            pulse_stale_days=self.pulse_stale_days,
            now=datetime.now(UTC),
        )
        footer = self._footer(reader, since_event, changes, moves)
        rendered = digests.project_text(data, footer, budget)
        await self._move_cursor("project", project.id, reader, changes.reached, moves, rendered)
        return rendered.text

    async def project_digest(
        self,
        project: str,
        *,
        budget: int | None = None,
        since_event: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> str:
        """One project: pulse, tasks by stage, open questions, decisions, environments, blocks,
        notes with their flags, changes since the reader's last read."""
        found = await self.get_project(project)
        reader = await self._digest_reader(session, session_title, hook_session)
        return await self._project_digest(found, reader, since_event, self._budget(budget))

    async def _doc_lines(self, links: list[digests.DocLink]) -> list[str]:
        lines = []
        for link in links:
            project = await repo.get_project(self._r, link.project)
            doc = await repo.get_document(self._r, project.id, link.doc) if project else None
            version = await repo.get_document_version(self._r, doc.id, doc.version) if doc else None
            lines.append(link.line(doc, version.body if version else ""))
        return lines

    async def _relevant_notes(self, task: Task) -> list[mem.MemoryNote]:
        """Current project and global notes, best search match on the task's title first."""
        words = mem.candidate_words(task.title)
        if not words:
            return []
        found = await self._search_ids(
            self._r, words, ("note",), task.project_id, digests.NOTES_MAX, any_word=True
        )
        by_id = {n.id: n for n in await repo.get_notes(self._r, [i for _, i in found])}
        notes = [by_id[i] for _, i in found if i in by_id]
        wanted = [n for n in notes if n.current and n.scope in ("project", "global")]
        return wanted[: digests.RELEVANT_NOTES_MAX]

    async def _task_context(
        self,
        task: Task,
        reader: Session | None,
        since_event: int | None,
        budget: int,
        others: list[Task] | None = None,
        new_context: bool = False,
    ) -> tuple[str, bool]:
        """The text, and whether it holds everything the plan reminder would say."""
        changes, moves = await self._changes(
            "task", task.id, reader, since_event, task_id=task.id, new_context=new_context
        )
        project = await self.get_project(task.project_slug)
        links = digests.doc_links(task.description, task.progress)
        unread = await repo.list_comments(self._r, task.id, unread_only=True)
        project_id = task.project_id
        limit = digests.TASK_DECISIONS_MAX
        data = digests.TaskData(
            task=task,
            plan=await self._load_plan(self._r, task),
            todo=await self._task_todo(self._r, task),
            task_decisions=await repo.list_decisions(self._r, task_id=task.id, limit=limit),
            project_decisions=await repo.list_decisions(
                self._r, project_id=project_id, scope="project", limit=limit
            ),
            decisions_total=await repo.count_current_decisions(self._r, project_id=project_id),
            doc_lines=await self._doc_lines(links[: digests.DOC_LINKS_MAX]),
            doc_links_total=len(links),
            environments=await self._environment_levels(project_id),
            rules_line=rules_line(await self._rules(self._r, project)),
            comments=[c for c in unread if c.from_owner],
            reader_session_id=reader.id if reader is not None else None,
            notes=await self._relevant_notes(task),
            changes=changes,
            now=datetime.now(UTC),
            others=[(t, await self._load_plan(self._r, t)) for t in others or []],
        )
        footer = self._footer(reader, since_event, changes, moves)
        rendered = digests.task_text(data, footer, budget)
        await self._move_cursor("task", task.id, reader, changes.reached, moves, rendered)
        return rendered.text, digests.plan_shown(data, rendered)

    async def task_context(
        self,
        ref: str | int,
        *,
        budget: int | None = None,
        since_event: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> str:
        """One task: fields and progress, plan, decisions, linked document sections, environment
        map, rules, unread owner comments, relevant notes, changes since the reader's last read.
        Reading it delivers no comment."""
        task = await self.get_task(ref)
        reader = await self._digest_reader(session, session_title, hook_session)
        text, _ = await self._task_context(task, reader, since_event, self._budget(budget))
        return text

    async def _session_context(
        self, hook: HookEvent, exact: Session | None, room: int
    ) -> tuple[str | None, bool]:
        """What SessionStart adds, and whether that covers the plan reminder: the task context of
        an exactly linked session's newest open task, or a short digest of the project of `cwd`.
        Only an exact link is a reader: a guessed row may be someone else's window. A failure here
        costs the extra context only, never the hook answer."""
        if self.session_context == "off":
            return None, False
        new_context = (hook.source or "startup") in digests.NEW_CONTEXT_SOURCES
        try:
            if exact is not None:
                bound = await repo.open_tasks_of_session(self._r, exact.id)
                by_id = {t.id: t for t in bound if t.stage in OPEN_STAGES}
                ordered = await repo.bound_task_ids(self._r, exact.id)
                tasks = [by_id[i] for i in ordered if i in by_id]
                if tasks:
                    budget = min(self.digest_budget_chars, room)
                    return await self._task_context(
                        tasks[0], exact, None, budget, tasks[1:], new_context
                    )
            row = exact or await repo.get_session_by_cc(self._r, hook.cc_session_id)
            cwd = hook.cwd or (row.cwd if row is not None else None)
            projects = await repo.projects_with_repo_path(self._r)
            project, _ = self._project_for_cwd(projects, cwd)
            if project is None:
                return None, False
            budget = min(self.session_context_project_chars, room)
            text = await self._project_digest(project, exact, None, budget, new_context)
            return text, False
        except Exception:
            logger.exception("Adding context to SessionStart failed; the answer goes without it")
            return None, False

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
        step_id: int | None = None,
        attachment_id: int | None = None,
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
            step_id=step_id,
            attachment_id=attachment_id,
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
            **({"step_id": step_id} if step_id else {}),
        )
        await self._index(tx, "comment", comment_id)
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

    # --- review: checklist and screenshots ----------------------------------

    async def _load_review(self, conn: aiosqlite.Connection, task_id: int) -> Review:
        steps = await repo.list_review_steps(conn, task_id)
        by_id = {s.id: s for s in steps}
        loose = []
        for item in await repo.list_attachments(conn, task_id):
            step = by_id.get(item.step_id) if item.step_id is not None else None
            if step is None:
                loose.append(item)
            else:
                step.attachments.append(item)
        return Review(steps, loose)

    async def review(self, ref: str | int) -> tuple[Task, Review, list[Comment]]:
        """A task's checklist with statuses and screenshots, and its owner comments."""
        task = await self.get_task(ref)
        comments = [c for c in await repo.list_comments(self._r, task.id) if c.from_owner]
        return task, await self._load_review(self._r, task.id), comments

    async def _step_at(self, tx: _Tx, task: Task, step: Any) -> ReviewStep:
        """A step of `task` by its position (1, 2, …) as agents number them."""
        if isinstance(step, bool) or not isinstance(step, int | str):
            raise ValidationError("step is the step's number in the checklist: 1, 2, …")
        text = str(step).strip()
        if not text.isdigit():
            raise ValidationError(f"step is the step's number in the checklist, got {step!r}.")
        for item in await repo.list_review_steps(tx.conn, task.id):
            if item.position == int(text):
                return item
        raise NotFoundError(f"{task.key} has no review step {text}; see review_get.")

    async def _step_event(
        self, tx: _Tx, kind: str, task: Task, step: ReviewStep, actor: str, **extra: Any
    ) -> None:
        await tx.event(
            kind,
            actor,
            project_id=task.project_id,
            task_id=task.id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            step_id=step.id,
            position=step.position,
            title=step.title,
            **extra,
        )

    async def set_review_steps(
        self,
        ref: str | int,
        steps: Any,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Task, Review]:
        """Replace the checklist. Step N keeps its status and screenshots while its title and
        details stay the same; a changed step opens again; steps past the new end are removed
        (their screenshots stay on the task)."""
        if not isinstance(steps, list):
            raise ValidationError("steps must be a list of {title, details} objects.")
        if len(steps) > STEPS_MAX:
            raise ValidationError(f"A checklist has at most {STEPS_MAX} steps.")
        wanted: list[tuple[str, str | None]] = []
        for i, item in enumerate(steps, 1):
            if isinstance(item, str):
                item = {"title": item}
            if not isinstance(item, dict) or set(item) - {"title", "details"}:
                raise ValidationError(f"Step {i} must be an object with title and details.")
            details = clean_markdown(item.get("details"), f"Step {i} details", STEP_DETAILS_MAX)
            wanted.append((clean_step_title(item.get("title")), details))
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            current = await repo.list_review_steps(tx.conn, task.id)
            for old in current[len(wanted) :]:
                await repo.delete_review_step(tx.conn, old.id)
                await self._step_event(tx, "review.step_removed", task, old, actor)
            for position, (title, details) in enumerate(wanted, 1):
                old = current[position - 1] if position <= len(current) else None
                if old is None:
                    step_id = await repo.insert_review_step(
                        tx.conn, task.id, position, title, details, actor, tx.now
                    )
                    added = await repo.get_review_step(tx.conn, step_id)
                    assert added is not None
                    await self._step_event(tx, "review.step_added", task, added, actor)
                elif (old.title, old.details_md) != (title, details):
                    await self._rewrite_step(tx, task, old, title, details, actor)
            return await self.get_task(task.id, tx.conn), await self._load_review(tx.conn, task.id)

    async def _rewrite_step(
        self, tx: _Tx, task: Task, step: ReviewStep, title: str, details: str | None, actor: str
    ) -> None:
        """New text for a step; a decision about the old text no longer holds."""
        changes: dict[str, Any] = {"title": title, "details_md": details}
        if step.status != "open":
            changes |= {"status": "open", "decided_at": None, "decided_by": None}
        await repo.update_review_step(tx.conn, step.id, changes, tx.now)
        step.title = title
        await self._step_event(
            tx, "review.step_updated", task, step, actor, status="open", previous=step.status
        )

    async def update_review_step(
        self,
        ref: str | int,
        step: Any,
        *,
        title: str | None = None,
        details: str | None = None,
        status: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> ReviewStep:
        """An agent's change to one step: its text, or `status='open'` to ask for a new look
        after a fix. Only the owner accepts or rejects, on the board."""
        if status is not None and status != "open":
            raise ValidationError(
                "Agents may only set status 'open' (after a fix); the owner accepts or rejects"
                " a step on the board."
            )
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            current = await self._step_at(tx, task, step)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            new_title = current.title if title is None else clean_step_title(title)
            new_details = (
                current.details_md
                if details is None
                else clean_markdown(details, "details", STEP_DETAILS_MAX)
            )
            if (new_title, new_details) != (current.title, current.details_md):
                await self._rewrite_step(tx, task, current, new_title, new_details, actor)
            elif status == "open" and current.status != "open":
                await repo.update_review_step(
                    tx.conn,
                    current.id,
                    {"status": "open", "decided_at": None, "decided_by": None},
                    tx.now,
                )
                await self._step_event(
                    tx,
                    "review.step_updated",
                    task,
                    current,
                    actor,
                    status="open",
                    previous=current.status,
                )
            updated = await repo.get_review_step(tx.conn, current.id)
            assert updated is not None
            return updated

    async def decide_review_step(
        self,
        ref: str | int,
        step_id: int,
        decision: str,
        comment: str | None = None,
        *,
        actor: str = "owner",
    ) -> Comment | None:
        """The owner's decision on a step from the board: accept, reject, reopen, or only a
        comment. It reaches the bound session as an owner comment, through the hooks. A
        decision the step already has, without a comment (a double click, a stale panel),
        writes and delivers nothing: None."""
        if decision not in REVIEW_DECISIONS:
            raise ValidationError(f"decision must be one of: {', '.join(REVIEW_DECISIONS)}.")
        note = _clean_text(comment, "Comment", COMMENT_MAX)
        if decision == "comment" and not note:
            raise ValidationError("Comment must not be empty.")
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            step = await repo.get_review_step(tx.conn, step_id)
            if step is None or step.task_id != task.id:
                raise NotFoundError(f"Review step #{step_id} not found on {task.key}.")
            status = {"accept": "accepted", "reject": "rejected", "reopen": "open"}.get(decision)
            if status == step.status and not note:
                return None
            if status is not None and status != step.status:
                decided = {"decided_at": tx.now, "decided_by": actor}
                if status == "open":
                    decided = {"decided_at": None, "decided_by": None}
                await repo.update_review_step(
                    tx.conn, step.id, {"status": status} | decided, tx.now
                )
                kind = {"accepted": "review.step_accepted", "rejected": "review.step_rejected"}
                await self._step_event(
                    tx,
                    kind.get(status, "review.step_updated"),
                    task,
                    step,
                    actor,
                    status=status,
                    previous=step.status,
                )
            steps = await repo.list_review_steps(tx.conn, task.id)
            text = review_decision_text(step, decision, note, steps)
            await self._read(tx, task, await self._unread(tx, task, False), actor, None)
            return await self._insert_comment(
                tx,
                task,
                author_kind="owner",
                author=actor,
                session_id=None,
                body=text,
                in_reply_to=None,
                step_id=step.id,
            )

    # --- plan: steps, checkpoints, history, and the agent's own todo --------------------

    async def _load_plan(self, conn: aiosqlite.Connection, task: Task) -> Plan:
        steps = await repo.list_plan_steps(conn, task.id)
        if not steps:
            return Plan(None, None, None)
        return Plan(task.plan_status, task.plan_accepted_at, task.plan_accepted_by, steps)

    async def _task_todo(self, conn: aiosqlite.Connection, task: Task) -> list[TodoItem]:
        """The Claude Code todo of the session bound to `task` (its current Claude Code id)."""
        if task.session_id is None:
            return []
        session = await repo.get_session_by_id(conn, task.session_id)
        if session is None:
            return []
        return await repo.list_session_todo(conn, session.id, session.cc_session_id)

    async def _plan_history(self, task: Task) -> list[tuple[PlanRevision, list[str]]]:
        """Revisions newest first, each with the step titles it changed against the one before."""
        revisions = await repo.list_plan_revisions(self._r, task.id)
        out = []
        for i, rev in enumerate(revisions):
            before = revisions[i + 1].titles if i + 1 < len(revisions) else []
            out.append((rev, title_diff(before, rev.titles)))
        return out

    async def plan(self, ref: str | int) -> tuple[Task, Plan, list[TodoItem]]:
        """A task's plan and the bound session's todo (the agent's own list, not the plan)."""
        task = await self.get_task(ref)
        return task, await self._load_plan(self._r, task), await self._task_todo(self._r, task)

    async def plan_history(
        self, ref: str | int
    ) -> tuple[Task, list[tuple[PlanRevision, list[str]]]]:
        task = await self.get_task(ref)
        return task, await self._plan_history(task)

    async def _plan_event(
        self, tx: _Tx, kind: str, task: Task, actor: str, session_id: int | None, **extra: Any
    ) -> None:
        await tx.event(
            kind,
            actor,
            project_id=task.project_id,
            task_id=task.id,
            session_id=session_id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            **extra,
        )

    async def _plan_revision(self, tx: _Tx, task: Task, actor: str, reason: str) -> Plan:
        """Keep the plan as it is now (after this write) as a new revision."""
        fresh = await self.get_task(task.id, tx.conn)
        plan = await self._load_plan(tx.conn, fresh)
        body = plan_body(plan.status, plan.steps)
        await repo.insert_plan_revision(tx.conn, task.id, body, actor, reason, tx.now)
        return plan

    async def set_plan(
        self,
        ref: str | int,
        steps: Any,
        *,
        reason: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Task, Plan, bool]:
        """Replace the plan. Step N keeps its status and checkpoint while its title stays the
        same; a renamed step starts again as pending; steps past the new end are removed. A
        change of the titles or their order turns an accepted plan back into a draft (the third
        value says so). An empty list removes the plan; a write that changes nothing is not
        kept."""
        if not isinstance(steps, list):
            raise ValidationError("steps must be a list of {title, details} objects.")
        if len(steps) > PLAN_STEPS_MAX:
            raise ValidationError(f"A plan has at most {PLAN_STEPS_MAX} steps.")
        wanted: list[tuple[str, str | None]] = []
        for i, item in enumerate(steps, 1):
            if isinstance(item, str):
                item = {"title": item}
            if not isinstance(item, dict) or set(item) - {"title", "details"}:
                raise ValidationError(f"Step {i} must be an object with title and details.")
            details = clean_markdown(item.get("details"), f"Step {i} details", PLAN_DETAILS_MAX)
            wanted.append((clean_plan_title(item.get("title")), details))
        why = _clean_text(reason, "reason", REASON_MAX)
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            current = await repo.list_plan_steps(tx.conn, task.id)
            titles_changed = [s.title for s in current] != [t for t, _ in wanted]
            details_changed = [s.details_md for s in current] != [d for _, d in wanted]
            if not titles_changed and not details_changed:
                return task, await self._load_plan(tx.conn, task), False
            for old in current[len(wanted) :]:
                await repo.delete_plan_step(tx.conn, old.id)
            for position, (title, details) in enumerate(wanted, 1):
                old = current[position - 1] if position <= len(current) else None
                if old is None:
                    await repo.insert_plan_step(
                        tx.conn, task.id, position, title, details, actor, tx.now
                    )
                elif old.title != title:
                    restart = {"status": "pending", "done_at": None, "link": None}
                    restart |= {"outcome": None, "conformance": None}
                    changes = {"title": title, "details_md": details} | restart
                    await repo.update_plan_step(tx.conn, old.id, changes, tx.now)
                elif old.details_md != details:
                    await repo.update_plan_step(tx.conn, old.id, {"details_md": details}, tx.now)
            previous = task.plan_status if current else None
            status = previous
            if not wanted:
                status = None
            elif previous is None or (previous == "accepted" and titles_changed):
                status = "draft"
            reverted = previous == "accepted" and status == "draft"
            if status != task.plan_status or not wanted:
                await repo.set_plan_status(tx.conn, task.id, status, None, None)
            default = (
                "plan removed" if not wanted else "plan written" if not current else "plan changed"
            )
            plan = await self._plan_revision(tx, task, actor, why or default)
            await self._plan_event(
                tx,
                "plan.set",
                task,
                actor,
                sess.id if sess else None,
                steps=len(wanted),
                status=status,
                previous=previous,
                titles_changed=titles_changed,
                reason=why,
            )
            return await self.get_task(task.id, tx.conn), plan, reverted

    async def _plan_step_at(self, tx: _Tx, task: Task, step: Any) -> PlanStep:
        """A plan step of `task` by its position (1, 2, …)."""
        if isinstance(step, bool) or not isinstance(step, int | str):
            raise ValidationError("step is the step's number in the plan: 1, 2, …")
        text = str(step).strip()
        if not text.isdigit():
            raise ValidationError(f"step is the step's number in the plan, got {step!r}.")
        for item in await repo.list_plan_steps(tx.conn, task.id):
            if item.position == int(text):
                return item
        raise NotFoundError(f"{task.key} has no plan step {text}; see plan_get.")

    async def _plan_link(self, tx: _Tx, task: Task, value: Any) -> str | None:
        """A checkpoint link: a report of this task ('12', '#12', 'report:12') or an http(s)
        link such as a pull request."""
        if value is None or isinstance(value, bool):
            return None
        text = _clean_text(str(value), "link")
        if text is None:
            return None
        match = PLAN_LINK_RE.fullmatch(text)
        if match:
            report_id = int(match.group(1))
            if not await repo.report_of_task(tx.conn, report_id, task.id):
                raise ValidationError(f"Report #{report_id} is not a report of {task.key}.")
            return f"report:{report_id}"
        if HTTP_URL_RE.fullmatch(text):
            return text
        raise ValidationError(
            "link is a report of this task (12, '#12' or 'report:12') or an http(s) link such as"
            f" a pull request, got {text!r}."
        )

    async def update_plan_step(
        self,
        ref: str | int,
        step: Any,
        status: str,
        *,
        outcome: str | None = None,
        link: Any = None,
        conformance: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Task, PlanStep]:
        """Set a step's status. `done` is a checkpoint: it needs `conformance` ('as planned' or
        'deviation: why') and may carry a one-line outcome and a link (report or PR). Leaving
        done clears the checkpoint. Status changes never turn an accepted plan into a draft."""
        if status not in STEP_STATUSES:
            raise ValidationError(f"status must be one of: {', '.join(STEP_STATUSES)}.")
        if status != "done" and (link is not None or conformance is not None):
            raise ValidationError(
                "link and conformance are the checkpoint of a done step; pass them with"
                " status 'done'."
            )
        result = clean_outcome(outcome)
        verdict = clean_conformance(conformance) if status == "done" else None
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            current = await self._plan_step_at(tx, task, step)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            if status == "done":
                given = None if link is None else await self._plan_link(tx, task, link)
                changes = {
                    "status": "done",
                    "done_at": current.done_at if current.status == "done" else tx.now,
                    "outcome": result if outcome is not None else current.outcome,
                    "link": current.link if link is None else given,
                    "conformance": verdict,
                }
            else:
                changes = {"status": status, "done_at": None, "link": None}
                changes |= {"conformance": None, "outcome": result}
            changes = {k: v for k, v in changes.items() if v != getattr(current, k)}
            if not changes:
                return task, current
            await repo.update_plan_step(tx.conn, current.id, changes, tx.now)
            label = f"step {current.position} {status}"
            if verdict:
                label += f" ({verdict})"
            await self._plan_revision(tx, task, actor, label)
            await self._plan_event(
                tx,
                "plan.step_updated",
                task,
                actor,
                sess.id if sess else None,
                position=current.position,
                title=current.title,
                status=status,
                previous=current.status,
                conformance=verdict,
                outcome=changes.get("outcome", current.outcome),
            )
            steps = await repo.list_plan_steps(tx.conn, task.id)
            updated = next(s for s in steps if s.id == current.id)
            return await self.get_task(task.id, tx.conn), updated

    async def accept_plan(
        self,
        ref: str | int,
        *,
        actor: str = "owner",
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Task, Plan, bool]:
        """Accept the task's plan (the owner on the board, the coordinator over MCP). The third
        value is False when it was accepted already (nothing written)."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            sess = await self._session(tx, session, session_title, hook_session)
            if sess is not None and sess.id == task.session_id:
                raise ValidationError(
                    f"The session bound to {task.key} does not accept its own plan; the owner"
                    " (on the board) or the coordinator does."
                )
            who = sess.name if sess else actor
            plan = await self._load_plan(tx.conn, task)
            if not plan.steps:
                raise ValidationError(
                    f"{task.key} has no plan to accept; the agent writes it with plan_set."
                )
            if plan.status == "accepted":
                return task, plan, False
            await repo.set_plan_status(tx.conn, task.id, "accepted", tx.now, who)
            plan = await self._plan_revision(tx, task, who, "accepted")
            await self._plan_event(
                tx, "plan.accepted", task, who, sess.id if sess else None, steps=len(plan.steps)
            )
            return await self.get_task(task.id, tx.conn), plan, True

    async def _record_todo(self, cc_id: str, change: TodoChange) -> None:
        """Apply a todo tool call to the session's list. A new Claude Code id starts a fresh
        list; an update of an item it never saw is ignored. The session is looked up again in
        this transaction: an agent's write may have just moved the id to another session."""
        async with self._tx() as tx:
            session = await repo.get_session_by_cc(tx.conn, cc_id)
            if session is None:
                return
            await repo.clear_session_todo(tx.conn, session.id, keep_cc=cc_id)
            before = await repo.list_session_todo(tx.conn, session.id, cc_id)
            by_id = {i.item_id: i for i in before}
            if change.kind == "replace":
                await repo.clear_session_todo(tx.conn, session.id)
                for item in change.items:
                    await repo.upsert_session_todo(tx.conn, session.id, cc_id, item, tx.now)
            elif change.kind == "create" and change.item_id and change.subject:
                old = by_id.get(change.item_id)
                if old is not None or len(before) < TODO_ITEMS_MAX:
                    position = (
                        old.position if old else max((i.position for i in before), default=0) + 1
                    )
                    item = TodoItem(
                        change.item_id, change.subject, "pending", change.active_form, position
                    )
                    await repo.upsert_session_todo(tx.conn, session.id, cc_id, item, tx.now)
            elif change.kind == "update" and change.item_id in by_id:
                old = by_id[change.item_id]
                if change.status == "deleted":
                    await repo.delete_session_todo_item(tx.conn, session.id, old.item_id)
                else:
                    item = TodoItem(
                        old.item_id,
                        change.subject or old.subject,
                        change.status or old.status,
                        change.active_form or old.active_form,
                        old.position,
                    )
                    await repo.upsert_session_todo(tx.conn, session.id, cc_id, item, tx.now)
            after = await repo.list_session_todo(tx.conn, session.id, cc_id)
            shape = [(i.item_id, i.subject, i.status, i.active_form) for i in after]
            if shape == [(i.item_id, i.subject, i.status, i.active_form) for i in before]:
                return
            for task in await repo.open_tasks_of_session(tx.conn, session.id):
                await self._plan_event(
                    tx,
                    "session.todo_updated",
                    task,
                    session.name,
                    session.id,
                    items=len(after),
                    completed=sum(1 for i in after if i.status == "completed"),
                )

    def _reminder_due(self, session: Session, payload: Any) -> str | None:
        """PostToolUse: "todo" after a todo change, "periodic" every `plan_reminder_every`
        tool events, else None."""
        count = self._tool_events.get(session.id, 0) + 1
        self._tool_events[session.id] = count
        if parse_todo(payload) is not None:
            return "todo"
        every = self.plan_reminder_every
        return "periodic" if every > 0 and count % every == 0 else None

    async def _plan_reminder(self, session: Session, *, repeat: bool = False) -> str | None:
        """The current plan step of each open task bound to `session`, with the todo item in
        progress; None when there is nothing to say or (unless `repeat`, the periodic
        reminder) it was the last text sent."""
        tasks = [
            t
            for t in await repo.open_tasks_of_session(self._r, session.id)
            if t.stage in OPEN_STAGES
        ]
        if not tasks:
            return None
        todo = await repo.list_session_todo(self._r, session.id, session.cc_session_id)
        lines = []
        for task in tasks:
            plan = await self._load_plan(self._r, task)
            line = reminder_line(task.key, task.stage.value, plan, todo)
            if line:
                lines.append(line)
        text = reminder_text(lines)
        if text is None or (not repeat and text == self._reminded.get(session.id)):
            return None
        self._reminded[session.id] = text
        return text

    # Files: the bytes are checked and written outside the write transaction; the transaction
    # re-checks the limits and removes the new file again when it fails.

    def _task_folder(self, task: Task) -> Path:
        folder = (self.attachments_dir / task.project_slug / task.key).resolve()
        if not folder.is_relative_to(self.attachments_dir.resolve()):
            raise ValidationError("Invalid task folder.")  # slugs and keys never contain "/"
        return folder

    def _file_path(self, item: Attachment) -> Path:
        return self.attachments_dir / item.project_slug / item.task_key / item.stored_name

    def _annotated_path(self, item: Attachment) -> Path:
        assert item.annotated_name is not None
        return self.attachments_dir / item.project_slug / item.task_key / item.annotated_name

    def _drop_annotated(self, item: Attachment) -> None:
        """After the commit, so the write has succeeded: a file that cannot go now (another
        program holds it) stays for the startup sweep, which no row keeps it from."""
        path = self._annotated_path(item)
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove %s, the next start removes it: %s", path, exc)

    def _store_bytes(self, folder: Path, data: bytes, ext: str) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        name = secrets.token_hex(16) + ext
        tmp = folder / f".{name}.tmp"
        with tmp.open("xb") as fh:
            fh.write(data)
        tmp.replace(folder / name)
        return name

    def _source_roots(self, project: Project) -> list[Path]:
        roots = [Path(tempfile.gettempdir()), *self.attachment_roots]
        if project.repo_path:
            roots.insert(0, Path(project.repo_path).expanduser())
        return roots

    async def attach_file(
        self,
        ref: str | int,
        *,
        path: str | None = None,
        content_base64: str | None = None,
        filename: str | None = None,
        step: Any = None,
        caption: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Attachment, bool]:
        """Copy a screenshot to the task: from `path` (inside the allowed folders) or from
        `content_base64`. Returns the attachment and whether it is new (the same image on the
        same task is stored once)."""
        if (path is None) == (content_base64 is None):
            raise ValidationError("Pass either path or content_base64.")
        task = await self.get_task(ref)
        project = await self.get_project(task.project_slug)
        if path is not None:
            real, checked = resolve_source(
                path, self._source_roots(project), [self.attachments_dir], self.db_path.parent
            )
            data = await asyncio.to_thread(read_source, real, checked, self.attachment_max_bytes)
            original = filename or real.name
        else:
            data = _decode_base64(content_base64 or "", self.attachment_max_bytes)
            original = filename
        return await self._attach(
            task,
            data,
            step=step,
            caption=caption,
            original=original,
            session=session,
            session_title=session_title,
            hook_session=hook_session,
        )

    async def attach_pasted(
        self, ref: str | int, step_id: int, data: bytes, *, actor: str = "owner"
    ) -> tuple[Attachment, bool]:
        """A screenshot the owner pasted into a step on the board."""
        task = await self.get_task(ref)
        step = await repo.get_review_step(self._r, step_id)
        if step is None or step.task_id != task.id:
            raise NotFoundError(f"Review step #{step_id} not found on {task.key}.")
        if len(data) > self.attachment_max_bytes:
            raise ValidationError(f"The file is larger than {self.attachment_max_bytes // MB} MB.")
        return await self._attach(
            task, data, step=step.position, caption=None, original="pasted image", actor=actor
        )

    async def _attach(
        self,
        task: Task,
        data: bytes,
        *,
        step: Any,
        caption: str | None,
        original: str | None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "agent",
    ) -> tuple[Attachment, bool]:
        mime = require_image(data)
        caption_md = clean_markdown(caption, "caption", CAPTION_MAX)
        digest = hashlib.sha256(data).hexdigest()
        folder = self._task_folder(task)
        existing = await repo.find_attachment(self._r, task.id, digest)
        stored = None
        if existing is None:
            stored = await asyncio.to_thread(self._store_bytes, folder, data, EXTENSIONS[mime])
        try:
            async with self._tx() as tx:
                fresh = await self.get_task(task.id, tx.conn)
                sess = await self._session(tx, session, session_title, hook_session)
                who = sess.name if sess else actor
                target = await self._step_at(tx, fresh, step) if step is not None else None
                again = await repo.find_attachment(tx.conn, fresh.id, digest)
                if again is not None:
                    return again, False
                if stored is None:  # removed meanwhile: store it after all on the next try
                    raise ConflictError("The same image was removed meanwhile; try again.")
                used = await repo.task_attachment_bytes(tx.conn, fresh.id)
                if used + len(data) > self.attachment_task_max_bytes:
                    raise ValidationError(
                        f"{fresh.key} already holds {used // MB} MB of screenshots; the limit is"
                        f" {self.attachment_task_max_bytes // MB} MB per task."
                    )
                attachment_id = await repo.insert_attachment(
                    tx.conn,
                    task_id=fresh.id,
                    step_id=target.id if target else None,
                    stored_name=stored,
                    original_name=clean_original_name(original),
                    mime=mime,
                    size=len(data),
                    sha256=digest,
                    caption_md=caption_md,
                    created_by=who,
                    now=tx.now,
                )
                await tx.event(
                    "attachment.added",
                    who,
                    project_id=fresh.project_id,
                    task_id=fresh.id,
                    session_id=sess.id if sess else None,
                    project=fresh.project_slug,
                    key=fresh.key,
                    stage=fresh.stage.value,
                    attachment_id=attachment_id,
                    step_id=target.id if target else None,
                    position=target.position if target else None,
                )
                item = await repo.get_attachment(tx.conn, attachment_id)
                assert item is not None
                stored = None  # kept
                return item, True
        finally:
            if stored is not None:
                (folder / stored).unlink(missing_ok=True)

    async def set_attachment_caption(
        self,
        attachment_id: int,
        caption: str | None,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Attachment:
        caption_md = clean_markdown(caption, "caption", CAPTION_MAX)
        async with self._tx() as tx:
            item = await repo.get_attachment(tx.conn, attachment_id)
            if item is None:
                raise NotFoundError(f"Attachment #{attachment_id} not found.")
            sess = await self._session(tx, session, session_title, hook_session)
            if caption_md != item.caption_md:
                task = await self.get_task(item.task_id, tx.conn)
                await repo.update_attachment(tx.conn, item.id, {"caption_md": caption_md})
                await tx.event(
                    "attachment.updated",
                    sess.name if sess else "agent",
                    project_id=task.project_id,
                    task_id=task.id,
                    session_id=sess.id if sess else None,
                    project=task.project_slug,
                    key=task.key,
                    stage=task.stage.value,
                    attachment_id=item.id,
                )
            updated = await repo.get_attachment(tx.conn, item.id)
            assert updated is not None
            return updated

    async def remove_attachment(
        self, ref: str | int, attachment_id: int, *, actor: str = "owner"
    ) -> None:
        """Delete a screenshot and its file."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            item = await repo.get_attachment(tx.conn, attachment_id)
            if item is None or item.task_id != task.id:
                raise NotFoundError(f"Attachment #{attachment_id} not found on {task.key}.")
            await repo.delete_attachment(tx.conn, item.id)
            await tx.event(
                "attachment.removed",
                actor,
                project_id=task.project_id,
                task_id=task.id,
                project=task.project_slug,
                key=task.key,
                stage=task.stage.value,
                attachment_id=item.id,
            )
        # After the commit: a failed delete leaves an orphan for the startup sweep, never a
        # row without its file.
        self._file_path(item).unlink(missing_ok=True)
        if item.annotated_name:
            self._drop_annotated(item)

    async def attachment_file(self, attachment_id: int) -> tuple[Attachment, Path]:
        item = await repo.get_attachment(self._r, attachment_id)
        if item is None:
            raise NotFoundError(f"Attachment #{attachment_id} not found.")
        return item, self._file_path(item)

    # --- annotations: markup on a screenshot, pins and their threads ------------------------

    async def _attachment_in(self, conn: aiosqlite.Connection, attachment_id: int) -> Attachment:
        item = await repo.get_attachment(conn, attachment_id)
        if item is None:
            raise NotFoundError(f"Attachment #{attachment_id} not found.")
        return item

    async def _annotation_on(
        self, conn: aiosqlite.Connection, attachment_id: int, annotation_id: int
    ) -> Annotation:
        """An annotation only through its own screenshot: another one's id is not found."""
        found = await repo.get_annotation(conn, annotation_id)
        if found is None or found.attachment_id != attachment_id:
            raise NotFoundError(
                f"Annotation #{annotation_id} not found on attachment #{attachment_id}."
            )
        return found

    async def _annotation_event(
        self,
        tx: _Tx,
        kind: str,
        task: Task,
        annotation: Annotation,
        actor: str,
        session_id: int | None = None,
    ) -> None:
        await tx.event(
            kind,
            actor,
            project_id=task.project_id,
            task_id=task.id,
            session_id=session_id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            attachment_id=annotation.attachment_id,
            annotation_id=annotation.id,
            annotation_kind=annotation.kind,
            pin_number=annotation.pin_number,
        )

    async def _clean_pin_text(self, tx: _Tx, task: Task, value: Any) -> str | None:
        """The cleaning of a comment plus the secret scan: this text also reaches an agent."""
        if value is not None and not isinstance(value, str):
            raise ValidationError("body must be a string or null.")
        text = _clean_text(value, "body", ann.BODY_MAX)
        project = await self.get_project(task.project_slug, tx.conn)
        _scan(await self._secret_rules(tx.conn, project), body=text)
        return text

    async def annotations(self, attachment_id: int) -> tuple[Attachment, list[Annotation]]:
        """A screenshot and its live annotations with their threads."""
        item = await self._attachment_in(self._r, attachment_id)
        return item, await repo.list_annotations(self._r, item.id)

    async def add_annotation(
        self, attachment_id: int, values: Any, *, actor: str = "owner"
    ) -> Annotation:
        """New markup from the board: `kind`, `colour`, `geometry`, optional `stroke_width` and,
        on a pin, `body`."""
        if not isinstance(values, dict):
            raise ValidationError("An annotation is an object with kind, colour and geometry.")
        unknown = sorted(set(values) - {"kind", "colour", "stroke_width", "geometry", "body"})
        if unknown:
            raise ValidationError(f"Unknown annotation field(s): {', '.join(unknown)}.")
        kind = ann.clean_kind(values.get("kind"))
        colour = ann.clean_colour(values.get("colour"))
        stroke = ann.clean_stroke(values.get("stroke_width"))
        geometry = await asyncio.to_thread(ann.clean_geometry, kind, values.get("geometry"))
        if kind != "pin" and values.get("body"):
            raise ValidationError("Only a pin carries a comment.")
        async with self._tx() as tx:
            item = await self._attachment_in(tx.conn, attachment_id)
            task = await self.get_task(item.task_id, tx.conn)
            body = await self._clean_pin_text(tx, task, values.get("body"))
            if len(await repo.list_annotations(tx.conn, item.id)) >= ann.ANNOTATIONS_MAX:
                raise ValidationError(
                    f"A screenshot holds at most {ann.ANNOTATIONS_MAX} annotations."
                )
            number = await repo.next_pin_number(tx.conn, item.id) if kind == "pin" else None
            annotation_id = await repo.insert_annotation(
                tx.conn,
                attachment_id=item.id,
                kind=kind,
                colour=colour,
                stroke_width=stroke,
                geometry=geometry,
                pin_number=number,
                body_md=body,
                created_by=actor,
                now=tx.now,
            )
            added = await repo.get_annotation(tx.conn, annotation_id)
            assert added is not None
            await self._annotation_event(tx, "annotation.added", task, added, actor)
            return added

    async def update_annotation(
        self, attachment_id: int, annotation_id: int, body: Any, *, actor: str = "owner"
    ) -> Annotation:
        """The owner rewrites a pin's comment."""
        async with self._tx() as tx:
            item = await self._attachment_in(tx.conn, attachment_id)
            await self._annotation_on(tx.conn, item.id, annotation_id)
            pin = await self._live_pin(tx.conn, annotation_id)
            task = await self.get_task(item.task_id, tx.conn)
            text = await self._clean_pin_text(tx, task, body)
            if text != pin.body_md:
                await repo.update_annotation(tx.conn, pin.id, {"body_md": text}, tx.now)
                await self._annotation_event(tx, "annotation.updated", task, pin, actor)
            updated = await repo.get_annotation(tx.conn, pin.id)
            assert updated is not None
            return updated

    async def remove_annotation(
        self, attachment_id: int, annotation_id: int, *, actor: str = "owner"
    ) -> Annotation:
        """A soft delete: the row stays, so its pin number is not used again and Undo can bring
        it back. Removing a removed one changes nothing."""
        async with self._tx() as tx:
            item = await self._attachment_in(tx.conn, attachment_id)
            found = await self._annotation_on(tx.conn, item.id, annotation_id)
            if found.deleted_at is None:
                task = await self.get_task(item.task_id, tx.conn)
                await repo.update_annotation(tx.conn, found.id, {"deleted_at": tx.now}, tx.now)
                await self._annotation_event(tx, "annotation.removed", task, found, actor)
            updated = await repo.get_annotation(tx.conn, found.id)
            assert updated is not None
            return updated

    async def restore_annotation(
        self, attachment_id: int, annotation_id: int, *, actor: str = "owner"
    ) -> Annotation:
        """Undo of a delete: the annotation comes back with its pin number and thread."""
        async with self._tx() as tx:
            item = await self._attachment_in(tx.conn, attachment_id)
            found = await self._annotation_on(tx.conn, item.id, annotation_id)
            if found.deleted_at is not None:
                task = await self.get_task(item.task_id, tx.conn)
                await repo.update_annotation(tx.conn, found.id, {"deleted_at": None}, tx.now)
                await self._annotation_event(tx, "annotation.updated", task, found, actor)
            updated = await repo.get_annotation(tx.conn, found.id)
            assert updated is not None
            return updated

    async def _live_pin(self, conn: aiosqlite.Connection, annotation_id: int) -> Annotation:
        found = await repo.get_annotation(conn, annotation_id)
        if found is None or found.deleted_at or found.kind != "pin":
            raise NotFoundError(f"Pin annotation #{annotation_id} not found.")
        return found

    async def reply_as_owner(
        self, attachment_id: int, annotation_id: int, body: Any, *, actor: str = "owner"
    ) -> Annotation:
        """The owner answers in a pin's thread; it reaches the bound session as an owner comment."""
        async with self._tx() as tx:
            item = await self._attachment_in(tx.conn, attachment_id)
            await self._annotation_on(tx.conn, item.id, annotation_id)
            pin = await self._live_pin(tx.conn, annotation_id)
            task = await self.get_task(item.task_id, tx.conn)
            text = await self._clean_pin_text(tx, task, body)
            if not text:
                raise ValidationError("A reply must not be empty.")
            await repo.insert_annotation_reply(
                tx.conn,
                annotation_id=pin.id,
                author_kind="owner",
                author=actor,
                session_id=None,
                body_md=text,
                now=tx.now,
            )
            await self._annotation_event(tx, "annotation.replied", task, pin, actor)
            await self._read(tx, task, await self._unread(tx, task, False), actor, None)
            await self._insert_comment(
                tx,
                task,
                author_kind="owner",
                author=actor,
                session_id=None,
                body=f"On pin #{pin.pin_number} of screenshot #{item.id}"
                f" (annotation {pin.id}): {text}",
                in_reply_to=None,
                step_id=item.step_id,
                attachment_id=item.id,
            )
            updated = await repo.get_annotation(tx.conn, pin.id)
            assert updated is not None
            return updated

    async def reply_as_agent(
        self,
        annotation_id: int,
        body: Any,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Attachment, Annotation]:
        """An agent answers a pin. The owner sees it in the pin's thread and, as an agent reply,
        in the task's thread; it marks the owner's unread comments on this screenshot read."""
        async with self._tx() as tx:
            pin = await self._live_pin(tx.conn, annotation_id)
            item = await self._attachment_in(tx.conn, pin.attachment_id)
            task = await self.get_task(item.task_id, tx.conn)
            text = await self._clean_pin_text(tx, task, body)
            if not text:
                raise ValidationError("A reply must not be empty.")
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            session_id = sess.id if sess else None
            await repo.insert_annotation_reply(
                tx.conn,
                annotation_id=pin.id,
                author_kind="agent",
                author=actor,
                session_id=session_id,
                body_md=text,
                now=tx.now,
            )
            await self._annotation_event(tx, "annotation.replied", task, pin, actor, session_id)
            unread = [c for c in await self._unread(tx, task, True) if c.attachment_id == item.id]
            await self._read(tx, task, unread, actor, session_id)
            await self._insert_comment(
                tx,
                task,
                author_kind="agent",
                author=actor,
                session_id=session_id,
                body=f"Pin #{pin.pin_number} of screenshot #{item.id}: {text}",
                in_reply_to=None,
                step_id=item.step_id,
                attachment_id=item.id,
            )
            updated = await repo.get_annotation(tx.conn, pin.id)
            assert updated is not None
            return item, updated

    async def store_annotated(
        self, attachment_id: int, data: bytes, *, actor: str = "owner"
    ) -> Attachment:
        """The flattened PNG from the viewer, beside the original, which never changes. The name
        comes from here, never from the client; the previous file goes after the commit."""
        if len(data) > self.attachment_max_bytes:
            raise ValidationError(f"The file is larger than {self.attachment_max_bytes // MB} MB.")
        require_png(data)
        item = await self._attachment_in(self._r, attachment_id)
        task = await self.get_task(item.task_id)
        folder = self._task_folder(task)
        stored: str | None = await asyncio.to_thread(self._store_bytes, folder, data, ".png")
        try:
            async with self._tx() as tx:
                fresh = await self._attachment_in(tx.conn, attachment_id)
                used = await repo.task_attachment_bytes(tx.conn, fresh.task_id)
                used -= fresh.annotated_size or 0
                if used + len(data) > self.attachment_task_max_bytes:
                    raise ValidationError(
                        f"{task.key} already holds {used // MB} MB of screenshots; the limit is"
                        f" {self.attachment_task_max_bytes // MB} MB per task."
                    )
                await repo.update_attachment(
                    tx.conn,
                    fresh.id,
                    {
                        "annotated_name": stored,
                        "annotated_size": len(data),
                        "annotated_sha256": hashlib.sha256(data).hexdigest(),
                    },
                )
                await tx.event(
                    "attachment.updated",
                    actor,
                    project_id=task.project_id,
                    task_id=task.id,
                    project=task.project_slug,
                    key=task.key,
                    stage=task.stage.value,
                    attachment_id=fresh.id,
                    annotated=True,
                )
                updated = await repo.get_attachment(tx.conn, fresh.id)
                assert updated is not None
                stored = None  # kept
        finally:
            if stored is not None:
                (folder / stored).unlink(missing_ok=True)
        if fresh.annotated_name:
            self._drop_annotated(fresh)
        return updated

    async def annotated_file(self, attachment_id: int) -> tuple[Attachment, Path]:
        item = await self._attachment_in(self._r, attachment_id)
        if not item.annotated_name:
            raise NotFoundError(f"Attachment #{attachment_id} has no annotated image yet.")
        return item, self._annotated_path(item)

    async def send_annotations(self, attachment_id: int, *, actor: str = "owner") -> Comment | None:
        """ "Send to agent": one owner comment with the pins, their coordinates and the annotated
        image, tied to the step. The same text with the same image again (a double click, nothing
        changed) writes and delivers nothing: None. A new colour or a small move leaves the text
        alike, and the image's hash tells it apart."""
        async with self._tx() as tx:
            item = await self._attachment_in(tx.conn, attachment_id)
            task = await self.get_task(item.task_id, tx.conn)
            live = await repo.list_annotations(tx.conn, item.id)
            if not live:
                raise ValidationError("Nothing to send: this screenshot has no markup.")
            if not item.annotated_name:
                raise ValidationError("Upload the annotated image first.")
            siblings = await repo.list_attachments(tx.conn, task.id)
            same_place = [a.id for a in siblings if a.step_id == item.step_id]
            number = same_place.index(item.id) + 1
            where = (
                f"Screenshot {number} of step {item.step_position}"
                if item.step_position
                else f"Screenshot {number} of {task.key} (no step)"
            )
            text = ann.send_text(where, item.id, live)
            comments = await repo.list_comments(tx.conn, task.id)
            sent = [c for c in comments if c.from_owner and c.attachment_id == item.id]
            same_image = item.annotated_sha256 == item.annotated_sent_sha256
            if same_image and sent and sent[-1].body_md == text:
                return None
            await repo.update_attachment(
                tx.conn, item.id, {"annotated_sent_sha256": item.annotated_sha256}
            )
            await self._read(tx, task, await self._unread(tx, task, False), actor, None)
            return await self._insert_comment(
                tx,
                task,
                author_kind="owner",
                author=actor,
                session_id=None,
                body=text,
                in_reply_to=None,
                step_id=item.step_id,
                attachment_id=item.id,
            )

    async def attachment_image(
        self, attachment_id: int, *, annotated: bool
    ) -> tuple[Attachment, bytes, str]:
        """A screenshot's bytes and type for an MCP answer; too large is refused, by name."""
        item = await self._attachment_in(self._r, attachment_id)
        if annotated and not item.annotated_name:
            raise ValidationError(
                f"Attachment #{item.id} has no annotated image yet; the owner makes one with"
                " 'Send to agent' in the viewer."
            )
        path = self._annotated_path(item) if annotated else self._file_path(item)
        size = (item.annotated_size or 0) if annotated else item.size
        if size > ann.IMAGE_TOOL_MAX_BYTES:
            raise ValidationError(
                f"The image of attachment #{item.id} is {size / MB:.1f} MB, too large for a tool"
                f" answer (limit {ann.IMAGE_TOOL_MAX_BYTES / MB:.1f} MB). Ask the owner to look"
                " at it on the board, and use the pin coordinates from review_get."
            )
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except FileNotFoundError:
            raise NotFoundError(f"The file of attachment #{item.id} is missing.") from None
        # Sides are known for PNG, which every flattened image is; other originals are not read.
        width, height = ann.png_size(data) or (0, 0)
        if max(width, height) > ann.IMAGE_TOOL_MAX_SIDE:
            raise ValidationError(
                f"The image of attachment #{item.id} is {width}x{height} px, too large for a tool"
                f" answer (limit {ann.IMAGE_TOOL_MAX_SIDE} px on a side). Ask the owner to look at"
                " it on the board, and use the pin coordinates from review_get."
            )
        return item, data, "image/png" if annotated else item.mime

    async def review_markup(
        self, task: Task, review: Review
    ) -> tuple[dict[int, list[Annotation]], dict[int, bytes], dict[int, str]]:
        """For review_get: the annotations by screenshot, the flattened images that fit in one
        answer, and a note for each one that does not."""
        by_attachment: dict[int, list[Annotation]] = {}
        for found in await repo.list_task_annotations(self._r, task.id):
            by_attachment.setdefault(found.attachment_id, []).append(found)
        images: dict[int, bytes] = {}
        notes: dict[int, str] = {}
        left = ann.REVIEW_IMAGES_MAX_BYTES
        for item in review.images():
            if not item.annotated_name:
                continue
            size = item.annotated_size or 0
            if size > left:
                notes[item.id] = (
                    f"not in this answer, see attachment_get({item.id}, annotated=true)"
                )
                continue
            try:
                _, images[item.id], _ = await self.attachment_image(item.id, annotated=True)
            except (NotFoundError, ValidationError) as exc:
                notes[item.id] = str(exc)
                continue
            left -= size
        return by_attachment, images, notes

    async def _sweep_attachments(self) -> None:
        """Remove files no attachment row names (left by a crash, a removed task, a failed
        delete) and empty folders; never anything outside the attachments folder."""
        root = self.attachments_dir
        if not root.is_dir():
            return
        keep = await repo.stored_files(self._r)
        removed = await asyncio.to_thread(_sweep, root, keep)
        if removed:
            logger.info("Removed %d orphaned attachment file(s) from %s", removed, root)

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
