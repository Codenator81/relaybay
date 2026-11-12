"""Use cases. The only writer of the database: state change + event row in one transaction,
then the committed events are published on the in-process bus."""

import asyncio
import base64
import binascii
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
import unicodedata
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple, NoReturn
from urllib.parse import urlencode

import aiosqlite

from relaybay import updates
from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain import annotations as ann
from relaybay.domain import approvals as arules
from relaybay.domain import backlog, guard, intake
from relaybay.domain import comments as comment_text
from relaybay.domain import digest as digests
from relaybay.domain import digest_limits as limits
from relaybay.domain import environments as envs
from relaybay.domain import limits as agent_limits
from relaybay.domain import memory as mem
from relaybay.domain import messages as message_text
from relaybay.domain import questions as qrules
from relaybay.domain import run_events as run_lines
from relaybay.domain import runs as run_rules
from relaybay.domain import usage as usage_data
from relaybay.domain.annotations import Annotation
from relaybay.domain.approvals import Approval
from relaybay.domain.backlog import BacklogItem
from relaybay.domain.code import (
    REPO_KINDS,
    REPO_NAME_MAX,
    REPO_NAME_RE,
    REPO_PATH_MAX,
    REPOS_MAX,
    ProjectRepo,
    TaskCode,
    default_repo_name,
    holds,
    pr_number,
    pr_repo_name,
    task_code,
)
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
from relaybay.domain.errors import (
    ConfirmationNeeded,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from relaybay.domain.hooks import (
    CC_ID_MAX,
    CONTINUED_END_REASONS,
    DELIVERY_EVENTS,
    HookEvent,
    folder_name,
    parse_hook,
    parse_launched_session,
    parse_message_recipient,
    run_of_payload,
    session_start_context,
)
from relaybay.domain.intake import IntakeMessage
from relaybay.domain.messages import SessionMessage
from relaybay.domain.models import (
    Binding,
    Comment,
    Decision,
    Event,
    ImportItem,
    Project,
    Report,
    SavedFilter,
    Session,
    Task,
    TrackerEntry,
)
from relaybay.domain.notifications import BOARD_ACTORS, Notice
from relaybay.domain.notifications import EVENT_KINDS as NOTICE_EVENT_KINDS
from relaybay.domain.notifications import classify as classify_notice
from relaybay.domain.plan import (
    ACCEPTANCE_ROOM,
    ACCEPTANCE_TELL_HOURS,
    ACCEPTANCE_TOLD,
    CLOSED_PLAN,
    DEFAULT_DRAFT_HINT_HOURS,
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
    acceptance_line,
    clean_conformance,
    clean_outcome,
    coordinator_acceptance_line,
    parse_todo,
    plan_body,
    reminder_line,
    reminder_text,
    title_diff,
)
from relaybay.domain.plan import STEP_DETAILS_MAX as PLAN_DETAILS_MAX
from relaybay.domain.plan import clean_step_title as clean_plan_title
from relaybay.domain.questions import Question
from relaybay.domain.reports import REPORT_SIZES, ReportCheck, parse_checks, parse_report
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
    TRACKER_LOG_LABEL,
    RuleProposal,
    Rules,
    RuleVersion,
    check_patch_keys,
    merge_project_rules,
    merge_rules,
    refusal_text,
    report_format_problem,
    rules_diff,
    rules_hash,
    rules_line,
    rules_markdown,
    stored_global_rules,
    unmet,
    validate_rules,
    verified_marks_problem,
    warning_text,
)
from relaybay.domain.runs import Run, RunDefaults, RunTurn, RunView
from relaybay.domain.sessions import DEFAULT_DESKTOP_LINK_TEMPLATE, parse_session_id
from relaybay.domain.stages import ACCEPTANCE_STAGES, OPEN_STAGES, STAGES, Stage, parse_stage
from relaybay.domain.switcher import (
    EMPTY_QUERY_TASKS,
    QUERY_MAX,
    RESULTS_MAX,
    SESSIONS_SCANNED,
    TASKS_SCANNED,
    SwitcherItem,
    fuzzy_score,
)
from relaybay.domain.task_fields import (
    BY_NAME,
    FIELD_NAMES,
    LINK_NAMES,
    MARKDOWN_MAX,
    NAME_MAX,
    TEXT_MAX,
    TITLE_MAX,
)
from relaybay.domain.tasks import DEFAULT_DONE_WINDOW_DAYS, is_archived
from relaybay.events import EventBus
from relaybay.install import install_kind
from relaybay.machine_setup import claude_paths
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
from relaybay.runner import auth as run_auth
from relaybay.runner import desktop as run_desktop
from relaybay.runner import worktrees
from relaybay.runner.claude_cli import TurnSpec, build_command
from relaybay.runner.layers import LayerState, run_files, user_layer_state, write_run_files
from relaybay.runner.manager import RunManager, RunnerConfig
from relaybay.runner.stream import Result
from relaybay.runner.worktrees import WorktreeSpec

logger = logging.getLogger("relaybay")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
HOOK_ACTIVITY_WINDOW = 5.0  # seconds: at most one activity write per session
# Seconds: a working session's activity alone (no status change) is stored as an event at most
# this often; the session row still follows HOOK_ACTIVITY_WINDOW.
ACTIVITY_EVENT_WINDOW = 60.0
MESSAGE_WINDOW = 60.0  # seconds: at most one message event per sender and recipient
MESSAGE_RECIPIENTS_MAX = 20  # different recipients of one sender per window
HOOK_MEMO_MAX = 4096
HTTP_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)  # use fullmatch
LIVE_ACTION = "live.action"
NO_LIVE_ON = "session.no_live_on"
NO_LIVE_OFF = "session.no_live_off"
# What the Audit page adds to the journal when asked: changes to who may do what, and done
# cards archived or brought back by hand.
AUDIT_CHANGE_KINDS = (
    "policy.updated",
    "policy.proposed",
    "policy.proposal_accepted",
    "policy.proposal_rejected",
    "policy.proposal_reopened",
    "environment.created",
    "environment.updated",
    "environment.deleted",
    "environment.access_added",
    "environment.access_updated",
    "environment.access_removed",
    "environment.proposed",
    "environment.proposal_accepted",
    "environment.proposal_rejected",
    NO_LIVE_ON,
    NO_LIVE_OFF,
    "run.settings_changed",
    "question.asked",
    "question.answered",
    "question.defaulted",
    "question.delivered",
    "question.withdrawn",
    "task.archived",
    "task.unarchived",
    "approval.requested",
    "approval.decided",
    "approval.used",
    "approval.expired",
)
AUDIT_DAYS_MAX = 366
JOURNAL_COMMAND_MAX = 200
# Left free in the SessionStart answer next to the lines it holds anyway.
SESSION_CONTEXT_RESERVE = 500
LIMIT_LINE_EVENTS = ("UserPromptSubmit", "PostToolUse")


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


REPORT_MARKS_FROM_KEY = "report_marks_from"


def _first_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _plan_closed(task: Task) -> bool:
    return bool(task.plan_total) and task.plan_done == task.plan_total


def _note_closed_plan(task: Task) -> None:
    """Always a warning, never a refusal, in strict mode too."""
    _note(CLOSED_PLAN_WARNING.format(done=task.plan_done, total=task.plan_total))


_last_now: datetime | None = None


def now_iso() -> str:
    """Strictly increasing within the process: two writes in one clock tick (about 15 ms on
    Windows) still sort in the order they happened, which "a report after the plan closed"
    and other "after" checks rely on."""
    global _last_now
    moment = datetime.now(UTC)
    moment = moment.replace(microsecond=moment.microsecond // 1000 * 1000)
    if _last_now is not None and moment <= _last_now:
        moment = _last_now + timedelta(milliseconds=1)
    _last_now = moment
    return moment.isoformat(timespec="milliseconds")


# Calls that may be a git action that needs an approval; without environments the rest is free.
_APPROVAL_HINT_RE = re.compile(r"\b(git|gh)(\.exe)?\b", re.IGNORECASE)
# Run modes in which Claude Code itself refuses a call nobody allowed: there an approved call is
# answered `allow`; in the others relaybay gives no decision and Claude Code decides as usual.
_REFUSING_MODES = ("manual", "default", "dontAsk")


class _ApprovalTaken(Exception):
    """Another call used the approval in the same moment: the transaction rolls back."""


@dataclass(slots=True)
class _ApprovalScope:
    """What an approval check of one session's calls reads (`Service._approval_scope`)."""

    session: Session
    run: Run | None
    tasks: list[Task]  # the card the request opens on first
    policy: EffectivePolicy  # of that card's project
    environments: list[Environment]
    policies: dict[int, EffectivePolicy]  # by environment id
    folders: list[tuple[str, str]]  # (repository name, folder): worktrees and repositories


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
    # Board targets of the document links in the description, progress, reports and comments.
    doc_refs: dict[str, mem.DocRef] = field(default_factory=dict)
    document: "TaskDocument | None" = None  # when the document field is a relaybay://doc link
    intake: list[IntakeMessage] = field(default_factory=list)  # messages tied to the task
    from_backlog: BacklogItem | None = None  # the item "To plan" created this task from
    findings: list[BacklogItem] = field(default_factory=list)  # items this task is the origin of
    budget: agent_limits.BudgetState | None = None  # the task's own token budget
    messages: list[SessionMessage] = field(default_factory=list)  # newest first
    code: TaskCode = field(default_factory=lambda: TaskCode([], [], []))
    checks: dict[int, list[ReportCheck]] = field(default_factory=dict)  # `verified:` marks
    marks_from: int = 0  # reports with a smaller id were written before marks existed

    def marks_warning(self, report: Report) -> str | None:
        """The rules warning of an M or L report without `verified:` marks."""
        if report.id < self.marks_from:
            return None
        problem = verified_marks_problem(report.size, len(self.checks.get(report.id, [])))
        return f"Warning (rules): {problem}" if problem else None


@dataclass(slots=True)
class TaskDocument:
    """A task's document field as a link to a board document: the document (None when it does
    not exist) and the text shown, the linked section or the whole current text."""

    link: digests.DocLink
    ref: mem.DocRef
    doc: mem.Document | None
    text: str


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
    rule_proposals: list[RuleProposal] = field(default_factory=list)
    rule_notices: list[RuleVersion] = field(default_factory=list)  # direct rules_update, unread
    intake: list[IntakeMessage] = field(default_factory=list)  # new messages, not tied yet
    answers: list[Task] = field(default_factory=list)  # tasks with "answer received"
    backlog_new: int = 0  # one Inbox line, however many items
    tie_choices: list[Task] = field(default_factory=list)  # "Tie to a task": waiting ones first
    # A newer relaybay release: one line on the Inbox page, not counted (badge, digests).
    update: updates.UpdateNotice | None = None
    brakes: list[agent_limits.Brake] = field(default_factory=list)  # open: only the owner releases
    budgets: list[agent_limits.BudgetState] = field(default_factory=list)  # past a threshold now
    running_over: int | None = None  # sessions running while above the concurrency cap
    running_cap: int = 0
    message_loops: list[SessionMessage] = field(default_factory=list)  # replies refused
    questions: list[Question] = field(default_factory=list)  # open, of all cards
    approvals: list[Approval] = field(default_factory=list)  # open requests, of all cards

    @property
    def count(self) -> int:
        asked = {q.task_id for q in self.questions} | {a.task_id for a in self.approvals}
        return (
            len(self.questions)
            + len(self.approvals)
            + len(self.intake)
            + len(self.answers)
            + (1 if self.backlog_new else 0)
            # A card waiting on the owner for its open question or request counts once, as that.
            + len({t.id for t in self.waiting} - asked)
            + len(self.rule_warnings)
            + len(self.not_logged)
            + len(self.unlinked)
            + len(self.replies)
            + len(self.proposals)
            + len(self.policy_proposals)
            + len(self.rule_proposals)
            + len(self.rule_notices)
            + len(self.review)
            + len(self.brakes)
            + len(self.budgets)
            + (1 if self.running_over is not None else 0)
            + len(self.message_loops)
        )


@dataclass(slots=True)
class SessionRow:
    session: Session
    tasks: list[Task]  # bound to the session now
    unread_messages: int = 0  # session messages to it not acknowledged yet


FILTER_NAME_MAX = 60
FILTER_TEXT_MAX = 200


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


def has_hidden_chars(text: str) -> bool:
    """Control or format characters (NUL, bidi overrides, zero-width): a link holding one can
    read as something other than where it goes."""
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in text)


def _clean_url(value: Any, name: str) -> str | None:
    url = _clean_text(value, name)
    if url is None:
        return None
    if has_hidden_chars(url):
        raise ValidationError(f"{name} holds a control or invisible character.")
    if not HTTP_URL_RE.fullmatch(url):
        raise ValidationError(f"{name} must be an http:// or https:// link, got {url!r}.")
    return url


def _clean_repo(name: Any, path: Any, kind: Any, fallback: str) -> tuple[str, str, str | None]:
    """(name, path, kind) of one project repository. The path is kept as written: it is never
    opened. Without a name the path's last folder names it."""
    clean_path = _clean_text(path, "path", REPO_PATH_MAX)
    if clean_path is None:
        raise ValidationError("A repository needs a path.")
    if has_hidden_chars(clean_path):
        raise ValidationError("path holds a control or invisible character.")
    clean_name = _clean_text(name, "name", REPO_NAME_MAX) or default_repo_name(clean_path, fallback)
    if not REPO_NAME_RE.match(clean_name):
        raise ValidationError(
            f"Repository name {clean_name!r}: letters, digits, '.', '_' and '-' only, starting"
            " with a letter or digit."
        )
    clean_kind = (_clean_text(kind, "kind") or "").lower() or None
    if clean_kind is not None and clean_kind not in REPO_KINDS:
        raise ValidationError(
            f"Repository kind must be one of {', '.join(REPO_KINDS)}, got {kind!r}."
        )
    return clean_name, clean_path, clean_kind


def _link_entries(value: Any, name: str) -> list[str]:
    """The entries of a `repos` / `prs` write: a list of strings, or one string."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValidationError(f"{name} must be a list of strings.")
    entries = [v.strip() for v in value if v.strip()]
    if len(entries) > REPOS_MAX:
        raise ValidationError(f"{name}: at most {REPOS_MAX} entries in one write.")
    return entries


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


def _clean_message(body: Any) -> str:
    # Control characters and line separators would grow several times as a quoted literal.
    kept = []
    for char in str(body or ""):
        kind = unicodedata.category(char)
        if kind in ("Zl", "Zp"):
            kept.append("\n")
        elif char in "\n\r\t" or kind not in ("Cc", "Cf", "Cs"):
            kept.append(char)
    text = _clean_text("".join(kept), "Message", message_text.MESSAGE_MAX)
    if not text:
        raise ValidationError("Message must not be empty.")
    if message_text.too_long_quoted(text):
        raise ValidationError(
            "Message is too long for one hook answer once quoted: line breaks, quotes and"
            " backslashes count double. Shorten it."
        )
    return text


def _hook_proven(session: Session) -> bool:
    """Exactly linked and its hooks report: an id only written on a row proves nothing."""
    return session.cc_exact and session.status not in (None, "ended")


def _message_arrival(recipient: Session) -> str:
    """When a new message can reach `recipient`, said as it is."""
    name = intake.one_line(recipient.name)
    reason = message_text.no_delivery_reason(recipient.cc_exact, recipient.status)
    if reason is not None:
        return (
            f"Not delivered: {reason}. It stays unread until then; {name} can also read it"
            " with messages_list."
        )
    if recipient.status == "idle":
        return f"Not delivered yet: {name} is idle. It sees this at its next prompt or wake-up."
    return f"Not delivered yet: it reaches {name} at its next tool call or prompt."


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
CLOSED_PLAN_WARNING = "Warning (rules): " + CLOSED_PLAN + "."
REWORK_NOTE = "{key} moved from {stage} back to dev ({note})."
ALL_DONE_REVIEW_HINT = (
    "Hint: all steps done and the review checklist is set: move the task to review"
    " (task_set_stage)."
)
DRAFT_HINTS_MAX = 5
DRAFT_HINT = (
    "Hint: {key}'s plan is a draft for {hours} h: accept it (plan_accept) or ask for changes."
)
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


def _doc_ref(link: digests.DocLink, body: str | None) -> mem.DocRef:
    """The board target of a document link, given the document's current text (None when the
    document does not exist)."""
    if body is None:
        return mem.DocRef(None, "no such document")
    href = f"/p/{link.project}/docs/{link.doc}"
    if not link.anchor:
        return mem.DocRef(href)
    wanted = link.anchor.casefold()
    found = next((s for s in mem.sections(body) if s.anchor == wanted), None)
    if found is None:
        return mem.DocRef(href, "no such section")
    return mem.DocRef(f"{href}#{found.anchor}")


def _first_words(text: str, limit: int = 80) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _clean_source_slug(value: Any) -> str:
    slug = str(value or "").strip().lower()
    if not intake.SOURCE_SLUG_RE.match(slug):
        raise ValidationError(
            f"Invalid source {value!r}: use lowercase letters, digits and dashes (up to 40),"
            " e.g. 'support-chat'."
        )
    return slug


def _clean_id(value: Any, name: str) -> int:
    text = str(value if value is not None else "").strip().lstrip("#")
    if not text.isdigit() or len(text) > 18:
        raise ValidationError(f"{name} must be a number such as 12, got {value!r}.")
    return int(text)


def _clean_moment(value: Any, name: str) -> str | None:
    """An ISO 8601 time from a watcher, stored in UTC; None when not given."""
    text = _clean_text(value, name, 40)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError(f"{name} must be an ISO time like 2026-11-05T10:00:00Z.") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def _rate_limited(message: IntakeMessage) -> bool:
    return message.status == "refused" and (message.refused_reason or "").startswith("rate limit")


TIE_CHOICES_MAX = 40


def _tie_choices(tasks: list[Task]) -> list[Task]:
    """Open tasks for the Inbox's task picker: those waiting on someone first."""
    open_tasks = [t for t in tasks if t.stage != Stage.DONE]
    return sorted(open_tasks, key=lambda t: not t.waiting_on)[:TIE_CHOICES_MAX]


@dataclass(slots=True)
class PostTie:
    message: IntakeMessage
    task: Task
    woke: bool
    reason: str


def _clean_slug(value: str) -> str:
    slug = value.strip().lower()
    if not SLUG_RE.match(slug):
        raise ValidationError(
            f"Invalid project slug {value!r}: use lowercase letters, digits and dashes,"
            " e.g. 'acme-shop'."
        )
    return slug


CLAUDE_MISSING = (
    "claude was not found on PATH or in the usual install folders; install Claude Code or set"
    " claude_path in config.toml."
)
NO_RUN_REPO = "A run works in one of the project's repositories: add one on Stands & access first."
SEND_REPEAT_SECONDS = 5.0  # the same text from the same sender this soon is one turn
BRIEF_ROOM = 500  # left in BRIEF_MAX for the run header and the report line
RUN_REPORT_LINE = (
    "At the end of each turn add a report with report_add on this card; its first line is"
    " `state · size · gist`."
)
RUN_START_STAGES = ("plan", "dev")
RUN_START_NOT_COORDINATOR = (
    "run_start is only for the coordinator sessions named in the server's config; a side task"
    " goes to backlog_add. Nothing started."
)
ARCHIVE_NOT_COORDINATOR = (
    "{tool} is only for the coordinator sessions named in the server's config; a desktop id also"
    " needs that session's own hook_session. Nothing changed. The owner archives done cards on"
    " the board."
)
RUN_SETTINGS_NOT_COORDINATOR = (
    "run_settings is only for the coordinator sessions named in the server's config; the owner"
    " changes a run on the card. Nothing changed."
)
CLAUDE_SESSION_RE = re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


@dataclass(slots=True)
class RunForm:
    """What the Start form of a card shows."""

    defaults: RunDefaults
    repos: list[ProjectRepo]
    task_repo_ids: set[int]
    brief: str
    claude: str | None
    login: run_auth.LoginState | None
    login_line: str
    layers: LayerState | None
    refusals: list[str]
    fix_command: str
    repo_modes: dict[int, str] = field(default_factory=dict)  # "new" or "self", the default
    copy_line: str = ""


PREVIEW_TIMEOUT_SECONDS = 10.0
PREVIEW_CACHE_SECONDS = 15.0
# The Start form renders on every live event of the card: what a worktree of a repository gets
# copied is asked of git at most every PREVIEW_CACHE_SECONDS. Start itself always asks again.
_preview_cache: dict[str, tuple[float, list[str]]] = {}


def _preview_names(path: str, git: str) -> list[str]:
    now = time.monotonic()
    cached = _preview_cache.get(path)
    if cached is not None and now - cached[0] < PREVIEW_CACHE_SECONDS:
        return cached[1]
    names = worktrees.copy_list(path, git, timeout=PREVIEW_TIMEOUT_SECONDS)
    _preview_cache[path] = (now, names)
    return names


def _worktree_preview(repos: list[ProjectRepo], git: str | None) -> tuple[dict[int, str], str]:
    """Per repository the Start form's default (a new worktree for a git repository, else the
    repository itself) and one line saying what a new worktree of each gets copied."""
    if git is None:
        return {r.id: "self" for r in repos}, "git is not on PATH: runs use the repositories."
    modes: dict[int, str] = {}
    parts = []
    for r in repos:
        is_git = os.path.lexists(Path(r.path) / ".git")
        modes[r.id] = "new" if is_git else "self"
        if not is_git:
            continue
        try:
            names = _preview_names(r.path, git)
        except ValidationError as exc:
            parts.append(f"{r.name}: cannot tell ({exc})")
            continue
        listed = (Path(r.path) / worktrees.INCLUDE_FILE).is_file()
        by = f" (by its {worktrees.INCLUDE_FILE})" if listed else ""
        parts.append(f"{r.name}{by}: {worktrees.summary(names)}")
    if not parts:
        return modes, ""
    return modes, (
        "Copied into a new worktree, untracked and never added to git (CLAUDE.md,"
        " CLAUDE.local.md, .claude/, .mcp.json, or the repository's .worktreeinclude): "
        + "; ".join(parts)
        + "."
    )


async def _make_worktrees(
    specs: list[WorktreeSpec], git: str | None, task_key: str, run_id: int
) -> list[WorktreeSpec]:
    """`worktrees.add_all` in a thread. A Start cancelled while it works (the request went away,
    the server stops) does not stop the thread: what it made is removed once it is done."""
    made: list[WorktreeSpec] = []
    work = asyncio.ensure_future(
        asyncio.to_thread(worktrees.add_all, specs, git, task_key, run_id, made=made)
    )
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:

        def undo(_: object) -> None:
            threading.Thread(
                target=worktrees.remove_created, args=(list(made), git), daemon=True
            ).start()

        work.add_done_callback(undo)
        raise


def _add_dirs(cwd: str, paths: list[str]) -> tuple[str, ...]:
    """Every folder of the run other than the cwd, once each."""
    seen = {worktrees.path_key(cwd)}
    out = []
    for path in paths:
        key = worktrees.path_key(path)
        if key not in seen:
            seen.add(key)
            out.append(path)
    return tuple(out)


def _same_folder(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _paths_overlap(a: str, b: str) -> bool:
    """The same folder, or one inside the other."""
    x = os.path.normcase(os.path.normpath(a)).rstrip(os.sep)
    y = os.path.normcase(os.path.normpath(b)).rstrip(os.sep)
    return x == y or x.startswith(y + os.sep) or y.startswith(x + os.sep)


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
        plan_draft_hint_hours: int = DEFAULT_DRAFT_HINT_HOURS,
        owner_names: list[str] | None = None,
        require_tracker_log: bool = False,
        done_window_days: int = DEFAULT_DONE_WINDOW_DAYS,
        pulse_stale_days: int = limits.DEFAULT_PULSE_STALE_DAYS,
        task_stale_days: int = limits.DEFAULT_TASK_STALE_DAYS,
        digest_budget_chars: int = limits.DEFAULT_BUDGET,
        session_context: str = "auto",
        session_context_project_chars: int = limits.DEFAULT_SESSION_PROJECT_CHARS,
        usage_enabled: bool = True,
        coordinators: list[str] | None = None,
        max_concurrent_sessions: int = 0,
        rules_table: dict[str, Any] | None = None,
        runner: RunnerConfig | None = None,
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
        # What config.toml says; until start() reads the current version it is also in effect.
        self.config_rules = Rules(
            rules if rules is not None else DEFAULT_RULES,
            mode=rules_mode,
            require_tracker_log=require_tracker_log,
        )
        self.global_rules = self.config_rules
        # What a config change is measured by: the [rules] table itself, not its merge with the
        # defaults, so a release that changes a default never replaces accepted rules.
        table = rules_table if rules_table is not None else (rules if rules is not None else {})
        self._config_table_hash = rules_hash(table)
        self.rules_mode = rules_mode
        self.coordinators = [c.strip() for c in coordinators or [] if c.strip()]
        # Runs whose `claude --desktop` is being launched now (Back waits for it).
        self.desktop_launching: set[int] = set()
        # The first report id the missing-marks warning applies to: reports written before
        # migration 0036 never warn (set at start).
        self.report_marks_from = 0
        self.rules_changed: Callable[[], None] | None = None
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
        # Per session: when its last `session.status_changed` was stored.
        self._status_evented: dict[int, float] = {}
        # Per session: the trailing write of an activity the event throttle held back.
        self._status_timers: dict[int, asyncio.TimerHandle] = {}
        self.activity_event_window = ACTIVITY_EVENT_WINDOW
        self._flushes: set[asyncio.Task[Any]] = set()
        # Per session: Stop blocks in the current turn; the newest wake-up waiter's number.
        self._stop_blocks: dict[int, int] = {}
        self._waiters: dict[int, int] = {}
        # Set when the server is told to stop: a wake-up long poll must not hold the shutdown
        # until uvicorn's graceful timeout cancels it with a 500.
        self.stopping = False
        self._waiter_seq = 0
        # FTS5 full-text search; False when this SQLite has no FTS5 (search uses LIKE then).
        self.search_fts = False
        # Plan reminders per session: tool events since the session started, the last text sent.
        self.plan_reminder_every = plan_reminder_every
        self.plan_draft_hint_hours = plan_draft_hint_hours
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
        self.max_concurrent_sessions = max_concurrent_sessions
        # Budgets each session has heard about in this context: (budget, period key, level).
        self._budget_told: dict[int, set[tuple[int, str, str]]] = {}
        self._limits_wake = asyncio.Event()  # a new budget does not wait out the reader's sleep
        # When a message event was last written, per sender and recipient; what a hook's
        # transcript path was found to be, so the file system is asked once per path; subagent
        # files over the limit, per transcript file.
        self._message_written: dict[int, dict[str, float]] = {}
        self._transcript_checked: dict[str, bool] = {}
        self._usage_capped: dict[int, int] = {}
        self.update = updates.UpdateStatus()  # the last update check, in memory only
        # Projects whose missing or stale pulse a stage move already mentioned in this run.
        self._pulse_noted: set[int] = set()
        # The turns of runs on cards; None without a runner config (runs are then refused).
        self.runs: RunManager | None = RunManager(self, runner) if runner is not None else None

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
        await self._sync_rule_versions()
        # A broken project override is reported now; it never stops the server or the project.
        for slug, body in await repo.list_project_rules(self._read_conn):
            self._merged_rules(slug, body)
        # A broken global policy stops the start; broken project layers are only logged.
        self.policy_file.load()
        for row in await repo.list_policy_layers(self._read_conn):
            self._db_layer(row)
        await self._sweep_attachments()
        await self._recover_runs()
        await self._backfill_report_marks()

    async def _backfill_report_marks(self) -> None:
        """Once, on the first start with migration 0036: the `verified:` marks of the reports
        written before it, and the id from which a report without marks is warned about."""
        value = await repo.get_app_setting(self._r, REPORT_MARKS_FROM_KEY)
        if value is None:
            async with self._tx() as tx:
                reports = await repo.all_report_bodies(tx.conn)
                # A database that ran a build without the marker may hold marks already.
                await tx.conn.execute("DELETE FROM report_check")
                for report_id, body in reports:
                    checks = parse_checks(body)
                    if checks:
                        await repo.insert_report_checks(tx.conn, report_id, checks)
                value = str(reports[-1][0] + 1 if reports else 1)
                await repo.set_app_setting(
                    tx.conn, REPORT_MARKS_FROM_KEY, value, tx.now, "relaybay"
                )
        self.report_marks_from = int(value)

    async def close(self) -> None:
        if self.runs is not None:
            await self.runs.close()  # the turns end `stopped` while the database is still open
        for timer in [*self._activity_timers.values(), *self._status_timers.values()]:
            timer.cancel()
        self._activity_timers.clear()
        self._status_timers.clear()
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
        """One `session.status_changed` per project of the tasks the session is bound to, with
        their `keys` (cards refresh by key), or one for the session alone. Per project, not one
        in all: the project filters of the stream, events_since and Activity read the column."""
        self._status_evented[session.id] = time.monotonic()
        timer = self._status_timers.pop(session.id, None)
        if timer is not None:
            timer.cancel()  # this event carries the latest state
        common = {
            "session": session.name,
            "session_external_id": session.external_id,
            "status": session.status,
            "activity": session.activity,
            **payload,
        }
        by_project: dict[int, list[Task]] = {}
        for task in await repo.open_tasks_of_session(tx.conn, session.id):
            by_project.setdefault(task.project_id, []).append(task)
        if not by_project:
            await tx.event(
                "session.status_changed",
                actor,
                project_id=session.project_id,
                session_id=session.id,
                **common,
            )
        for project_id, tasks in by_project.items():
            await tx.event(
                "session.status_changed",
                actor,
                project_id=project_id,
                session_id=session.id,
                project=tasks[0].project_slug,
                keys=[t.key for t in tasks],
                **common,
            )

    def _activity_event_due(self, session_id: int) -> bool:
        """An activity change without a status change is stored once per ACTIVITY_EVENT_WINDOW."""
        last = self._status_evented.get(session_id)
        if last is not None and time.monotonic() - last < self.activity_event_window:
            return False
        if session_id not in self._status_evented and len(self._status_evented) >= HOOK_MEMO_MAX:
            self._status_evented.clear()
        return True

    def _status_event_later(self, session_id: int) -> None:
        """The throttle held an activity back: store the session's state when its window ends
        (unless another event comes first), so the board does not keep showing an older one."""
        if session_id in self._status_timers:
            return
        last = self._status_evented.get(session_id, 0.0)
        wait = max(0.0, self.activity_event_window - (time.monotonic() - last))
        self._status_timers[session_id] = asyncio.get_running_loop().call_later(
            wait, self._flush_status, session_id
        )

    def _flush_status(self, session_id: int) -> None:
        self._status_timers.pop(session_id, None)
        task = asyncio.get_running_loop().create_task(self._write_status(session_id))
        self._flushes.add(task)
        task.add_done_callback(self._flush_done)

    async def _write_status(self, session_id: int) -> None:
        async with self._tx() as tx:
            session = await repo.get_session_by_id(tx.conn, session_id)
            if session is not None and session.status != "ended":
                await self._session_events(tx, session, session.name)

    async def _link_hook_session(
        self, tx: _Tx, session: Session, cc_id: str, *, from_window: bool = False
    ) -> Session:
        """Exact link from an agent's write: `cc_id` now belongs to `session`. A row the hooks
        created for it by guess hands over its status and is removed when nothing refers to it.
        `from_window`: the desktop app itself reports the id (a hook, not an agent's write)."""
        if session.cc_session_id == cc_id and session.cc_exact:
            return session
        changes: dict[str, Any] = {"cc_session_id": cc_id, "cc_exact": 1, "unlinked_reason": None}
        other = await repo.get_session_by_cc(tx.conn, cc_id)
        # Moving the row to an id the hooks never saw leaves the real hooks a fresh row without the
        # switch: an agent could free itself with a made-up id. A live exact link stays too; an
        # ended one may go on to a real new id (a /clear in a terminal), and the switch goes along.
        held = other is None or (session.cc_exact and session.status != "ended")
        if session.no_live_at is not None and held and not from_window:
            _note(
                f"Warning: hook_session {cc_id} not linked: live is switched off for this session"
                " and its hook link stays as it is."
            )
            return session
        # The same for a session brake, on a guessed row too: the row keeps its hooks.
        if (
            held
            and not from_window
            and session.cc_session_id not in (None, cc_id)
            and await repo.session_brake_open(tx.conn, session.id)
        ):
            _note(
                f"Warning: hook_session {cc_id} not linked: this session is braked and its hook"
                " link stays as it is."
            )
            return session
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
            await repo.move_session_brakes(tx.conn, other.id, session.id)
            changes.update(
                status=other.status,
                activity=other.activity,
                activity_at=other.activity_at,
                cwd=other.cwd,
            )
            if other.transcript_path:
                changes["transcript_path"] = other.transcript_path
            if other.kind == "hook" and not await repo.has_bindings(tx.conn, other.id):
                # Only a guess is merged: naming the id of an ended session takes no mail.
                if not other.cc_exact:
                    await repo.move_session_messages(tx.conn, other.id, session.id)
                await repo.delete_session(tx.conn, other.id)
            else:
                await repo.move_open_bindings(tx.conn, other.id, session.id)
                await repo.touch_session(
                    tx.conn,
                    other.id,
                    tx.now,
                    {
                        "cc_session_id": None,
                        "cc_exact": 0,
                        "unlinked_reason": None,
                        "status": None,
                        "no_live_at": None,
                        "no_live_by": None,
                    },
                )
        if session.project_id is None and other is not None and other.project_id is not None:
            changes["project_id"] = other.project_id
        # `other` may just have been deleted: the owner's revocation must not go with it.
        carried = None
        if other is not None and other.id != session.id and session.no_live_at is None:
            carried = other if other.no_live_at is not None else None
        if carried is not None:
            changes.update(no_live_at=carried.no_live_at, no_live_by=carried.no_live_by)
        await repo.touch_session(tx.conn, session.id, tx.now, changes)
        linked = await repo.get_session_by_id(tx.conn, session.id)
        assert linked is not None
        if carried is not None:
            await self._no_live_event(tx, linked, NO_LIVE_ON, "relaybay", carried_from=carried.name)
        self._activity_written.pop(session.id, None)
        await self._session_events(tx, linked, linked.name, hook_session=cc_id, linked=True)
        return linked

    def _project_for_cwd(
        self, projects: list[Project], cwd: str | None
    ) -> tuple[Project | None, str | None]:
        """The project whose repo_path is `cwd` or its closest parent; a tie is ambiguous and
        returns the reason instead."""
        best = self._projects_at_cwd(projects, cwd)
        if len(best) > 1:
            names = ", ".join(sorted(p.slug for p in best))
            return None, f"the folder belongs to several projects ({names})"
        return (best[0] if best else None), None

    @staticmethod
    def _projects_at_cwd(projects: list[Project], cwd: str | None) -> list[Project]:
        """Every project whose repo_path is `cwd` or its closest parent."""
        if not cwd:
            return []
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
        return best

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
                # A switch on an ended row was aimed at that session, not at the folder.
                expired = existing.no_live_at is not None and existing.status == "ended"
                changes = {"cc_session_id": hook.cc_session_id, "cc_exact": 0, "cwd": hook.cwd}
                if expired:
                    changes.update(no_live_at=None, no_live_by=None)
                await repo.touch_session(tx.conn, existing.id, tx.now, changes)
                found = await repo.get_session_by_id(tx.conn, existing.id)
                assert found is not None
                if expired:
                    await self._no_live_event(
                        tx,
                        found,
                        NO_LIVE_OFF,
                        "relaybay",
                        reason="the session ended and a new one took this row",
                    )
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
        if hook.run_id is not None:
            linked = await self._run_hook_session(tx, hook)
            if linked is not None:
                return linked
            if found is not None:
                return found
            # Not that run's turn (a `claude` started inside it inherits the id): its own row,
            # never a guess onto a desktop session.
            projects = await repo.projects_with_repo_path(tx.conn)
            project, _ = self._project_for_cwd(projects, hook.cwd)
            return await self._new_hook_session(tx, hook, project.id if project else None, None)
        desktop = await self._host_desktop(tx, hook, found)
        if desktop is not None:
            return await self._link_hook_session(tx, desktop, hook.cc_session_id, from_window=True)
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
        if await self._stale_replay(hook):
            session = await self._link_only(hook)
        else:
            session = await self._held_back(hook) or await self._apply_hook(hook)
        change = parse_todo(payload)
        if change is not None:
            try:
                await self._record_todo(hook.cc_session_id, change, since=hook.spooled_at)
            except Exception:
                logger.exception("Recording the session's todo failed; the hook goes on")
        recipient = parse_message_recipient(payload)
        if recipient is not None and session is not None:
            try:
                await self._record_message(session, recipient)
            except Exception:
                logger.exception("Recording a message edge failed; the hook goes on")
        child = parse_launched_session(payload)
        if child is not None and session is not None and child != session.external_id:
            try:
                await self._record_launch(session, child)
            except Exception:
                logger.exception("Recording a session launch failed; the hook goes on")
        return session

    async def _record_launch(self, session: Session, child: str) -> None:
        """A `session.launched` event: the session that started another and the new one's desktop
        id, nothing else of the tool's input or answer. Recorded for a future agents view; for now
        only Activity shows it, and a replayed hook adds a second row."""
        async with self._tx() as tx:
            await tx.event(
                "session.launched",
                session.name,
                project_id=session.project_id,
                session_id=session.id,
                session=session.name,
                child=child,
            )

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

    async def _stale_replay(self, hook: HookEvent) -> bool:
        """A spooled event older than the session's last status or activity change: newer
        events already got through (an old SessionEnd must not end a session that started
        again)."""
        if hook.spooled_at is None:
            return False
        current = await repo.get_session_by_cc(self._r, hook.cc_session_id)
        return (
            current is not None
            and current.activity_at is not None
            and current.activity_at >= hook.spooled_at
        )

    async def _link_only(self, hook: HookEvent) -> Session:
        """A stale replay changes no status; a SessionStart among them still links the desktop
        session it names."""
        async with self._tx() as tx:
            return await self._hook_session(tx, hook)

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
        session = await self._apply_hook_status(hook)
        if hook.cwd and hook.event != "SessionEnd":
            try:
                await self._auto_link_repos(session, hook.cwd)
            except Exception:
                logger.exception("Linking the task's repository failed; the hook goes on")
        return session

    async def _apply_hook_status(self, hook: HookEvent) -> Session:
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
                # A replay keeps its own time, so the spooled events after it stay in order.
                changes["activity_at"] = hook.spooled_at or tx.now
            if transcript is not None and transcript != session.transcript_path:
                changes["transcript_path"] = transcript
                await repo.track_usage_file(tx.conn, session.id, transcript, tx.now)
            await repo.touch_session(tx.conn, session.id, tx.now, changes)
            updated = await repo.get_session_by_id(tx.conn, session.id)
            assert updated is not None
            if "activity_at" in changes or hook.activity_only:
                # Also for an unchanged activity: the next ones in the window are held back.
                self._activity_written[session.id] = time.monotonic()
            if "activity_at" in changes and (
                session.status != hook.status or self._activity_event_due(session.id)
            ):
                await self._session_events(
                    tx,
                    updated,
                    updated.name,
                    hook_event=hook.event,
                    **({"previous": session.status} if session.status != hook.status else {}),
                )
            elif "activity_at" in changes:
                self._status_event_later(session.id)
            if hook.event == "SessionEnd":
                self._activity_written.pop(session.id, None)
                self._status_evented.pop(session.id, None)
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
        tasks: /clear and /resume in any session, every end of a desktop session (its id
        outlives the Claude Code processes; the next SessionStart links the new one) and of a
        run's session."""
        return (
            session.external_id is not None
            or session.kind == "run"  # every turn of a run ends its process; the run goes on
            or hook.end_reason in CONTINUED_END_REASONS
        )

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
        ids = [c.id for c in comments]
        carried = await repo.undelivered_answers_of_comments(tx.conn, ids)
        await self._answers_delivered(tx, carried, session.name)
        decided = await repo.undelivered_decisions_of_comments(tx.conn, ids)
        await self._decisions_delivered(tx, decided)

    async def _take_undelivered(
        self, session: Session, budget: int = CONTEXT_MAX, *, alone: bool = True
    ) -> tuple[list[Comment], int]:
        """Mark the oldest undelivered owner comments that fit in one hook answer as delivered
        to `session`; returns them and how many were waiting. Not `alone`: other lines are in
        the answer, so a comment that does not fit waits instead of overflowing it."""
        if not await repo.undelivered_owner_comments(self._r, session.id):
            return [], 0  # the common case: no write transaction per tool call
        async with self._tx() as tx:
            waiting = await repo.undelivered_owner_comments(tx.conn, session.id)
            shown = comment_text.fit(waiting, budget, alone=alone)
            if shown:
                await self._deliver(tx, session, shown)
        return shown, len(waiting)

    async def _take_plan_acceptances(
        self, session: Session, budget: int = CONTEXT_MAX
    ) -> list[str]:
        """The owner's plan acceptances on the board for tasks bound to `session` and, for a
        session named in `coordinators`, for every task; each told once per session and only
        within ACCEPTANCE_TELL_HOURS. An acceptance over MCP (a coordinator's) is not news.
        The lines take at most ACCEPTANCE_ROOM of `budget`; the rest stays untold until the
        next hook."""
        since = (datetime.now(UTC) - timedelta(hours=ACCEPTANCE_TELL_HOURS)).isoformat(
            timespec="milliseconds"
        )
        args = (session.id, BOARD_ACTORS, ACCEPTANCE_TOLD, since)
        bound = await repo.untold_plan_acceptances(self._r, *args)
        others = await repo.untold_board_acceptances(self._r, *args) if self.coordinators else []
        # Asked only when there is something to tell: the check reads several rows.
        coordinator = bool(others) and await self._is_coordinator(
            self._r, session.external_id or session.name, session.cc_session_id
        )
        if not bound and not coordinator:
            return []  # the common case: no write transaction per tool call
        room = min(budget, ACCEPTANCE_ROOM)
        lines: list[str] = []
        async with self._tx() as tx:
            bound = await repo.untold_plan_acceptances(tx.conn, *args)
            told = [(t, acceptance_line(t.key, await self._load_plan(tx.conn, t))) for t in bound]
            if coordinator:
                # A task bound to it since before the acceptance gets the line above only.
                skip = {t.id for t in bound}
                for task in await repo.untold_board_acceptances(tx.conn, *args):
                    if task.id not in skip:
                        plan = await self._load_plan(tx.conn, task)
                        line = coordinator_acceptance_line(task.key, plan, task.session_name)
                        told.append((task, line))
            used = 0
            for task, line in told:
                used += len(line) + 1
                if used > room:
                    break
                await self._plan_event(
                    tx, ACCEPTANCE_TOLD, task, session.name, session.id, session=session.name
                )
                lines.append(line)
        return lines

    async def _unread_messages_line(self, session: Session) -> str:
        count = len(await repo.unread_session_messages(self._r, session.id))
        return " " + message_text.unread_line(count) if count else ""

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
                context += await self._unread_messages_line(session)
                # A new context: the agent has seen nothing yet.
                self._tool_events[session.id] = 0
                self._reminded.pop(session.id, None)
                self._budget_told.pop(session.id, None)
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
        lines = await self._limit_lines(hook) if hook.event in LIMIT_LINE_EVENTS else []
        if session is None:
            return _context(hook.event, "\n".join(lines)) if lines else None
        if hook.event == "UserPromptSubmit":
            self._stop_blocks.pop(session.id, None)  # a new turn
        if hook.event in DELIVERY_EVENTS:
            reminder = None
            due = self._reminder_due(session, payload) if hook.event == "PostToolUse" else None
            if due is not None:
                reminder = await self._plan_reminder(session, repeat=due == "periodic")
            budget = CONTEXT_MAX - (len(reminder) + 1 if reminder else 0)
            budget -= sum(len(line) + 1 for line in lines)
            accepted = await self._take_plan_acceptances(session, budget)
            budget -= sum(len(line) + 1 for line in accepted)
            shown, total = await self._take_undelivered(session, budget, alone=not accepted)
            parts = [*accepted, *await self._take_waiting_answers(session)]
            if shown:
                parts.append(comment_text.delivery_text(shown, total))
            if reminder:
                parts.append(reminder)
            parts += lines
            parts += await self._message_parts(session, *parts)
            return _context(hook.event, "\n".join(parts)) if parts else None
        if hook.event == "Stop":
            if await self._braked(session, hook.cwd):
                # The owner stopped it: no comment keeps the turn going.
                self._stop_blocks.pop(session.id, None)
                return None
            return await self._stop_answer(session, hook.stop_hook_active)
        return None

    async def _pre_tool_answer(
        self, payload: Any, hook: HookEvent, exact: Session | None
    ) -> dict[str, Any] | None:
        """PreToolUse: the guard's decision plus owner comments for an exactly linked session.
        The guard never stands in the way through a failure of its own: it then says nothing."""
        stopped = await self._brake_answer(payload, hook)
        if stopped is not None:
            return stopped
        try:
            touch, warnings = await self._guard(payload, hook)
        except Exception:
            logger.exception("relaybay guard failed; the tool call goes on")
            touch, warnings = None, []
        gate: tuple[str, str] | None = None
        if touch is None:
            # Only a call its level lets through can use an approval.
            try:
                gate = await self._approval_gate(payload, hook)
            except Exception:
                logger.exception("relaybay approval check failed")
                gate = await self._approval_check_failed(payload)
        contexts = list(warnings)
        if exact is not None:
            budget = CONTEXT_MAX - sum(len(line) + 1 for line in contexts)
            accepted = await self._take_plan_acceptances(exact, budget)
            contexts += accepted
            budget -= sum(len(line) + 1 for line in accepted)
            shown, total = await self._take_undelivered(exact, budget, alone=not accepted)
            contexts += await self._take_waiting_answers(exact)
            if shown:
                contexts.append(comment_text.delivery_text(shown, total))
            contexts += await self._message_parts(exact, *contexts)
        out: dict[str, Any] = {"hookEventName": "PreToolUse"}
        if touch is not None:
            # Never "allow": that would skip Claude Code's own permission prompt.
            out["permissionDecision"] = touch.decision
            out["permissionDecisionReason"] = touch.reason
        elif gate is not None:
            # "allow" only for a call the owner approved on the board, one approval per call.
            out["permissionDecision"], out["permissionDecisionReason"] = gate
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
        no_live = session is not None and session.no_live_at is not None
        # The switch says no live at all, also where the session's links do not reach.
        environments = await repo.list_environments(self._r, project_ids, or_live=no_live)
        policies = await self._policies(self._r, environments)
        touches = guard.check(call, environments, lambda env: policies[env.id], no_live)
        if not touches:
            return None, []
        live = [t for t in touches if t.environment.kind == "live"]
        if live:
            secrets = [v for e in environments for a in e.access for v in a.test_secrets.values()]
            try:
                await self._journal(session, call, live, secrets)
            except Exception:
                logger.exception("Writing the live journal failed; the decision still stands")
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
        due = (await self._stop_candidates(self._r, session))[0] or (
            await self._stop_message_due(self._r, session)
        )
        if not allowed or not due:
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
            reason = comment_text.stop_reason(shown, total) if shown else None
            messages = await self._stop_message_text(tx, session, reason)
        if not shown and not messages:
            self._stop_blocks.pop(session.id, None)
            return None
        self._stop_blocks[session.id] = blocks + 1
        text = "\n".join(part for part in (reason, messages) if part)
        return {"decision": "block", "reason": text}

    async def wait_for_comments(
        self,
        payload: Any,
        *,
        hold: float,
        poll: float = 1.0,
        gone: Callable[[], Awaitable[bool]] | None = None,
    ) -> str | None:
        """The wake-up hook's long poll: wait up to `hold` seconds until an owner comment, a
        session message or the owner's acceptance of a plan (as the tool hooks tell it) arrives
        for the idle, exactly linked session of `payload`, mark it delivered and return the
        text for the agent. None when the time is up, the session is not idle (its own hooks
        deliver then), a newer wait for the same session started, or `gone()` says the hook
        process went away."""
        cc_id = payload.get("session_id") if isinstance(payload, dict) else None
        if not isinstance(cc_id, str) or not cc_id.strip():
            return None
        session = await self._exact_session(cc_id.strip()[:CC_ID_MAX])
        # A run's turn ends its process: nothing would read a wake-up, and what it took would
        # be marked delivered and lost. The next turn's hooks tell it instead.
        if session is None or session.kind == "run":
            return None
        self._waiter_seq += 1
        me = self._waiters[session.id] = self._waiter_seq
        deadline = time.monotonic() + hold
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(poll)
                if self.stopping:
                    return None
                if self._waiters.get(session.id) != me or (gone is not None and await gone()):
                    return None
                current = await self._exact_session(cc_id.strip()[:CC_ID_MAX])
                if current is None or current.id != session.id:
                    return None
                if await self._braked(current, current.cwd):
                    return None
                # Not while a turn goes on: a Stop block of this turn means it continues.
                if current.status != "idle" or session.id in self._stop_blocks:
                    continue
                parts = await self._take_plan_acceptances(current)
                budget = CONTEXT_MAX - sum(len(line) + 1 for line in parts)
                shown, total = await self._take_undelivered(current, budget, alone=not parts)
                if shown:
                    parts.append(comment_text.delivery_text(shown, total))
                parts += await self._message_parts(current, *parts)
                if parts:
                    return "\n".join(parts)
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
            unlogged = task.stage == Stage.DONE and require_tracker_log and not task.tracker_logged
            if unlogged and task.archived_at is not None:
                # Archived by hand before its work log was marked: it still waits for the mark.
                not_logged.append(task)
                continue
            if is_archived(task, now, done_window_days, require_tracker_log):
                continue
            if task.stage == Stage.DONE:
                if require_tracker_log and not task.tracker_logged:
                    not_logged.append(task)
            elif task.waiting_on and task.waiting_on.strip().casefold() in owners:
                waiting.append(task)
            move = moves.get(task.id)
            # Shown until the task moves again: the warning is about the move into this stage.
            if move and move.payload.get("to") == task.stage:
                # A done task without the mark is already under "not logged", until it is marked.
                shown = [w for w in move.payload.get("warnings", []) if TRACKER_LOG_LABEL not in w]
                if shown:
                    warned.append((task, shown))
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
            await repo.list_rule_proposals(self._r),
            await repo.list_rule_notices(self._r),
            await repo.list_messages(self._r, status="new", limit=intake.LIST_MAX),
            [t for t in tasks if t.answer_received_at and t.stage != Stage.DONE],
            sum((await repo.backlog_new_counts(self._r)).values()),
            _tie_choices(tasks),
            self.update_notice(),
            await repo.open_brakes(self._r),
            await self._marked_budgets(
                self._r,
                now.date().isoformat(),
                await repo.budget_marks(self._r, now.date().isoformat(), agent_limits.TOTAL_KEY),
            ),
            await self._concurrency_over(now),
            self.max_concurrent_sessions,
            await repo.open_message_loops(self._r),
            await self.open_questions(),
            await self.open_approval_requests(),
        )

    def update_notice(self) -> updates.UpdateNotice | None:
        """The Inbox line about a newer release, with the upgrade command of this install."""
        latest = self.update.latest
        if latest is None or not self.update.available:
            return None
        command = updates.upgrade_hint(install_kind(), latest.version)
        return updates.UpdateNotice(latest.version, latest.url, command)

    async def check_update(self, opener: updates.Opener | None = None) -> updates.UpdateStatus:
        """One update check now; a failure is kept in the status and logged at debug level."""
        await asyncio.to_thread(updates.check, self.update, opener)
        if self.update.error:
            logger.debug("Update check failed: %s", self.update.error)
        return self.update

    async def update_loop(self, hours: float, delay: float = updates.START_DELAY) -> None:
        """Check for a newer release `delay` seconds after the start, then every `hours`."""
        await asyncio.sleep(delay)
        while True:
            try:
                await self.check_update()
            except Exception:
                logger.exception("The update check failed; the next one tries again")
            await asyncio.sleep(hours * 3600)

    async def list_sessions(self, project: str | None = None, limit: int = 200) -> list[SessionRow]:
        """Sessions with the tasks bound to them now; with `project`, the sessions of that
        project (placed there by its hooks or bound to one of its tasks)."""
        project_id = (await self.get_project(project)).id if project else None
        sessions = await repo.list_sessions(self._r, project_id, limit)
        bound: dict[int, list[Task]] = {s.id: [] for s in sessions}
        for task in await repo.list_tasks(self._r, session_ids=list(bound)):
            if task.session_id is not None:
                bound[task.session_id].append(task)
        unread = await repo.unread_session_message_counts(self._r, list(bound))
        return [SessionRow(s, bound[s.id], unread.get(s.id, 0)) for s in sessions]

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

    # --- audit -------------------------------------------------------------

    async def audit(
        self,
        *,
        project: str | None = None,
        environment: str | None = None,
        session_ids: list[int] | None = None,
        decision: str | None = None,
        days: int = 7,
        changes: bool = False,
        before_id: int | None = None,
        after_id: int | None = None,
        limit: int = 50,
    ) -> list[Event]:
        """Who did what on which live stand, newest first; `changes` adds the changes to the
        policy, the stands and the sessions' switches. `days` 0 means all of it."""
        if decision is not None and decision not in guard.DECISION_ORDER:
            raise ValidationError(f"decision must be one of: {', '.join(guard.DECISION_ORDER)}.")
        if not 0 <= days <= AUDIT_DAYS_MAX:
            raise ValidationError(f"days must be from 0 (all) to {AUDIT_DAYS_MAX}.")
        project_id = (await self.get_project(project)).id if project else None
        since = None
        if days:
            start = datetime.now(UTC) - timedelta(days=days)
            since = start.isoformat(timespec="milliseconds")
        return await repo.audit_page(
            self._r,
            before_id=before_id,
            after_id=after_id,
            project_id=project_id,
            environment=environment,
            session_ids=session_ids,
            decision=decision,
            since=since,
            kinds=(LIVE_ACTION, *AUDIT_CHANGE_KINDS) if changes else (LIVE_ACTION,),
            limit=max(1, min(limit, 500)),
        )

    async def environment_names(self) -> list[str]:
        """The stands of all projects by name, for the Audit filter."""
        return sorted({e.name for e in await repo.list_environments(self._r)})

    async def _no_live_event(
        self, tx: _Tx, session: Session, kind: str, actor: str, **payload: Any
    ) -> None:
        project = None
        if session.project_id is not None:
            project = await repo.get_project_by_id(tx.conn, session.project_id)
        await tx.event(
            kind,
            actor,
            project_id=session.project_id,
            session_id=session.id,
            session=session.name,
            **({"project": project.slug} if project else {}),
            **payload,
        )

    async def set_session_no_live(self, session_id: int, on: bool, *, actor: str) -> Session:
        """The owner's "no live now" switch. A repeat of the current state changes nothing."""
        async with self._tx() as tx:
            session = await repo.get_session_by_id(tx.conn, session_id)
            if session is None:
                raise NotFoundError(f"Session {session_id} not found.")
            if (session.no_live_at is not None) == on:
                return session
            at, by = (tx.now, actor) if on else (None, None)
            await repo.set_session_no_live(tx.conn, session.id, at, by)
            await self._no_live_event(tx, session, NO_LIVE_ON if on else NO_LIVE_OFF, actor)
            found = await repo.get_session_by_id(tx.conn, session.id)
            assert found is not None
            return found

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
        """The background reader and the budget check: never on the hook path, sooner again while
        a file is behind. With `usage_enabled` false nothing is read; the check still runs for the
        new day's release and the concurrency cap."""
        while True:
            behind = False
            wait = interval
            try:
                if self.usage_enabled:
                    _, behind = await self.usage_pass()
            except Exception:
                logger.exception("Reading transcripts failed; the next pass tries again")
            try:
                await self.limits_pass()
                if await self._limits_active():
                    wait = min(interval, agent_limits.CHECK_SECONDS)
            except Exception:
                logger.exception("Checking budgets failed; the next pass tries again")
            await self._limits_sleep(usage_data.CATCH_UP_SECONDS if behind else wait)

    async def _limits_sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._limits_wake.wait(), seconds)
        self._limits_wake.clear()

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

    # --- brake and token budgets ---------------------------------------------

    async def _brake_for(
        self, session: Session | None, cwd: str | None
    ) -> agent_limits.Brake | None:
        """The open brake that covers a session: every kind of link counts, a guessed one too.
        A read on every tool call; without an open brake nothing else is asked."""
        brakes = await repo.open_brakes(self._r)
        if not brakes:
            return None
        # All of them, not the guard's "the folder only without links": a brake on a project
        # also holds a session that works in its folder for a task elsewhere.
        project_ids = set(await self._guard_projects(session, cwd))
        # Two projects on one folder: a brake on either holds it, the guard's tie does not apply.
        projects = await repo.projects_with_repo_path(self._r)
        project_ids.update(p.id for p in self._projects_at_cwd(projects, cwd))
        task_ids = (
            {t.id for t in await repo.open_tasks_of_session(self._r, session.id)}
            if session is not None
            else set()
        )
        for brake in brakes:
            if (
                brake.scope == "all"
                or (brake.scope == "project" and brake.project_id in project_ids)
                or (brake.scope == "task" and brake.task_id in task_ids)
                or (
                    brake.scope == "session"
                    and session is not None
                    and brake.session_id == session.id
                )
            ):
                return brake
        return None

    async def _braked(self, session: Session, cwd: str | None) -> bool:
        """Fails open, like the guard: a brake that cannot be read blocks nothing."""
        try:
            return await self._brake_for(session, cwd) is not None
        except Exception:
            logger.exception("Reading the brake failed; the session goes on")
            return False

    async def _brake_answer(self, payload: Any, hook: HookEvent) -> dict[str, Any] | None:
        """PreToolUse under the brake: `continue: false` ends the turn, `deny` covers a client
        that reads only the decision. relaybay's own tools pass: the agent may still report."""
        tool = payload.get("tool_name") if isinstance(payload, dict) else None
        if isinstance(tool, str) and (
            tool.startswith(guard.OWN_TOOL_PREFIX) or tool in agent_limits.FREE_TOOLS
        ):
            return None
        try:
            session = await repo.get_session_by_cc(self._r, hook.cc_session_id)
            brake = await self._brake_for(session, hook.cwd)
        except Exception:
            logger.exception("Reading the brake failed; the tool call goes on")
            return None
        if brake is None:
            return None
        reason = agent_limits.deny_reason(brake)
        return {
            "continue": False,
            "stopReason": reason,
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
        }

    async def brake_stop(self, payload: Any) -> dict[str, Any] | None:
        """The stop of a `PreToolUse` under the brake, or None. Reads only: the receiver asks it
        before it writes the event, so a slow or failing write cannot lose the stop."""
        hook = parse_hook(payload)
        if hook is None or hook.event != "PreToolUse":
            return None
        return await self._brake_answer(payload, hook)

    async def _limit_lines(self, hook: HookEvent) -> list[str]:
        """Context lines of the brake (on the owner's prompt, which is never blocked) and of a
        budget past a threshold (once per session). Any link counts: no comment text is in them."""
        try:
            session = await repo.get_session_by_cc(self._r, hook.cc_session_id)
            lines = []
            if hook.event == "UserPromptSubmit":
                brake = await self._brake_for(session, hook.cwd)
                if brake is not None:
                    lines.append(agent_limits.prompt_line(brake))
            if session is not None:
                lines += await self._budget_lines(session, hook.cwd)
            return lines
        except Exception:
            logger.exception("Reading the limits failed; the session goes on")
            return []

    async def _budget_lines(self, session: Session, cwd: str | None) -> list[str]:
        today = datetime.now(UTC).date().isoformat()
        rows = await repo.budget_mark_rows(self._r, today, agent_limits.TOTAL_KEY)
        told = self._budget_told.setdefault(session.id, set())
        if not set(rows) - told:
            return []
        marks: dict[int, set[str]] = {}
        for budget_id, _, level in rows:
            marks.setdefault(budget_id, set()).add(level)
        project_ids = await self._guard_projects(session, cwd)
        task_ids = {t.id for t in await repo.open_tasks_of_session(self._r, session.id)}
        lines = []
        for state in await self._marked_budgets(self._r, today, marks):
            budget = state.budget
            covers = (
                budget.scope == "all"
                or (budget.scope == "project" and budget.project_id in project_ids)
                or (budget.scope == "task" and budget.task_id in task_ids)
            )
            own = {row for row in rows if row[0] == budget.id}
            if covers and own - told:
                lines.append(agent_limits.budget_line(state))
                told.update(own)
        return lines

    def _forget_told(self, budget_id: int) -> None:
        for told in self._budget_told.values():
            told -= {entry for entry in told if entry[0] == budget_id}

    async def _budget_used(
        self, conn: aiosqlite.Connection, budget: agent_limits.TokenBudget, today: str
    ) -> int:
        day = today if budget.period == "day" else None
        if budget.scope == "task":
            assert budget.task_id is not None
            return await repo.task_tokens(conn, budget.task_id, day)
        project_id = budget.project_id if budget.scope == "project" else None
        return await repo.usage_tokens(conn, project_id=project_id, day=day)

    async def _marked_budgets(
        self, conn: aiosqlite.Connection, today: str, marks: dict[int, set[str]]
    ) -> list[agent_limits.BudgetState]:
        """The budgets past a threshold in their current period, the highest one of each."""
        found = []
        for budget in await repo.list_budgets(conn):
            levels = marks.get(budget.id)
            if levels:
                level = (
                    agent_limits.EXCEEDED
                    if agent_limits.EXCEEDED in levels
                    else agent_limits.WARNED
                )
                used = await self._budget_used(conn, budget, today)
                found.append(agent_limits.BudgetState(budget, used, level))
        return found

    async def _insert_brake(
        self,
        tx: _Tx,
        *,
        scope: str,
        project: Project | None = None,
        session: Session | None = None,
        task: Task | None = None,
        reason: str,
        source: str,
        actor: str,
        budget_id: int | None = None,
        caller_id: int | None = None,
    ) -> agent_limits.Brake:
        """A brake already open on the same target is answered with that one, not a second row;
        a budget's own brake is apart, so releasing one never lifts the other."""
        project_id = project.id if project else (task.project_id if task else None)
        if session is not None and project_id is None:
            project_id = session.project_id
        session_id = session.id if session else None
        task_id = task.id if task else None
        for open_brake in await repo.open_brakes(tx.conn):
            if (
                (open_brake.scope, open_brake.session_id, open_brake.task_id)
                == (scope, session_id, task_id)
                and (scope != "project" or open_brake.project_id == project_id)
                and open_brake.budget_id == budget_id
            ):
                return open_brake
        brake_id = await repo.insert_brake(
            tx.conn,
            scope=scope,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            reason=reason,
            source=source,
            budget_id=budget_id,
            now=tx.now,
            actor=actor,
        )
        brake = await repo.get_brake(tx.conn, brake_id)
        assert brake is not None
        await tx.event(
            "brake.set",
            actor,
            project_id=project_id,
            task_id=task_id,
            session_id=caller_id,
            brake_id=brake_id,
            scope=scope,
            target=brake.target,
            reason=reason,
            source=source,
            **({"project": brake.project_slug} if brake.project_slug else {}),
            **({"key": task.key, "stage": task.stage.value} if task else {}),
        )
        return brake

    @staticmethod
    def _clean_brake_reason(value: Any, default: str | None = None) -> str:
        text = _clean_text(value, "reason", agent_limits.REASON_MAX) or default
        if not text:
            raise ValidationError("reason must say why the agents stop.")
        envs.check_secret("reason", text)
        return text

    async def set_board_brake(
        self, scope: str, *, project: str | None = None, reason: str | None = None
    ) -> agent_limits.Brake:
        """The owner's "Stop all agents": everything, or one project. Board only."""
        if scope not in ("all", "project"):
            raise ValidationError("The board stops all agents or the agents of one project.")
        text = self._clean_brake_reason(reason, agent_limits.DEFAULT_BOARD_REASON)
        async with self._tx() as tx:
            target = await self.get_project(project or "", tx.conn) if scope == "project" else None
            return await self._insert_brake(
                tx,
                scope=scope,
                project=target,
                reason=text,
                source=agent_limits.SOURCE_OWNER,
                actor="owner",
            )

    async def set_brake(
        self,
        scope: str,
        *,
        project: str | None = None,
        target_session: str | None = None,
        task: str | None = None,
        reason: str,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> agent_limits.Brake:
        """Stop agents, for a coordinator session. Only the owner releases, on the board."""
        if scope not in agent_limits.BRAKE_SCOPES:
            raise ValidationError(f"scope must be one of: {', '.join(agent_limits.BRAKE_SCOPES)}.")
        text = self._clean_brake_reason(reason)
        async with self._tx() as tx:
            if not await self._is_coordinator(tx.conn, session, hook_session):
                raise ValidationError(
                    "brake_set is only for the coordinator sessions named in the server's config;"
                    " a desktop id also needs that session's own hook_session. Nothing changed."
                    " Ask the owner: the board has Stop all agents."
                )
            caller = await self._session(tx, session, session_title, hook_session)
            assert caller is not None
            needed = {"project": project, "session": target_session, "task": task}.get(scope)
            if scope != "all" and not (needed or "").strip():
                raise ValidationError(f"scope {scope} needs its {scope} named.")
            target = (
                await self._brake_target(tx.conn, target_session or "")
                if scope == "session"
                else None
            )
            if target is not None and target.cc_session_id is None:
                _note(
                    f"Warning: {target.name} is not linked to hooks yet: this brake reaches it only"
                    " once it is linked."
                )
            braked_task = await self.get_task(task or "", tx.conn) if scope == "task" else None
            if braked_task is not None:
                await self._note_task_brake_reach(tx.conn, braked_task)
            return await self._insert_brake(
                tx,
                scope=scope,
                project=await self.get_project(project or "", tx.conn)
                if scope == "project"
                else None,
                session=target,
                task=braked_task,
                reason=text,
                source=agent_limits.SOURCE_COORDINATOR,
                actor=caller.name,
                caller_id=caller.id,
            )

    @staticmethod
    async def _note_task_brake_reach(conn: aiosqlite.Connection, task: Task) -> None:
        """A task brake holds the session bound to the task, seen by its hooks: say when none is."""
        bound = await repo.open_binding(conn, task.id)
        session = await repo.get_session_by_id(conn, bound) if bound is not None else None
        if session is None:
            _note(
                f"Warning: no session is bound to {task.key}: this brake reaches nobody until one"
                " is."
            )
        elif session.cc_session_id is None:
            _note(
                f"Warning: {session.name}, bound to {task.key}, is not linked to hooks yet: this"
                " brake reaches it only once it is linked."
            )

    @staticmethod
    async def _brake_target(conn: aiosqlite.Connection, ref: str) -> Session:
        """The one session a name, external id, alias or Claude Code id names. Never upserts: a
        misspelt name must not brake a new, empty session."""
        text = ref.strip()
        found = await repo.find_sessions(conn, text)
        if not found:
            by_cc = await repo.get_session_by_cc(conn, text)
            found = [by_cc] if by_cc else []
        if not found:
            raise NotFoundError(f"Session {ref!r} not found.")
        if len(found) > 1:
            ids = ", ".join(s.external_id or s.name for s in found)
            raise ValidationError(f"{ref!r} names several sessions: {ids}. Pass the desktop id.")
        return found[0]

    async def release_brake(self, brake_id: int, *, actor: str = "owner") -> agent_limits.Brake:
        """Board only. Releasing a released brake changes nothing."""
        async with self._tx() as tx:
            brake = await repo.get_brake(tx.conn, brake_id)
            if brake is None:
                raise NotFoundError(f"Brake #{brake_id} not found.")
            if brake.released_at is None:
                await self._release(tx, brake, actor)
            return brake

    async def _release(self, tx: _Tx, brake: agent_limits.Brake, actor: str) -> None:
        await repo.release_brake(tx.conn, brake.id, tx.now, actor)
        await tx.event(
            "brake.released",
            actor,
            project_id=brake.project_id,
            task_id=brake.task_id,
            brake_id=brake.id,
            scope=brake.scope,
            target=brake.target,
            **({"project": brake.project_slug} if brake.project_slug else {}),
            **({"key": brake.task_key} if brake.task_key else {}),
        )

    async def open_brakes(self, project: str | None = None) -> list[agent_limits.Brake]:
        """Open brakes; with `project`, those that reach it by name or by every agent."""
        brakes = await repo.open_brakes(self._r)
        if project is None:
            return brakes
        project_id = (await self.get_project(project)).id
        return [b for b in brakes if b.scope == "all" or b.project_id == project_id]

    async def set_budget(
        self,
        scope: str,
        *,
        project: str | None = None,
        task: str | None = None,
        period: str = "day",
        max_tokens: Any = None,
        action: str = "warn",
        actor: str = "owner",
    ) -> agent_limits.TokenBudget:
        """Set the one budget of a target. Board only. A change forgets the thresholds marked so
        far: the next check counts them again against the new numbers. An open brake stays."""
        if scope not in agent_limits.BUDGET_SCOPES:
            raise ValidationError(f"scope must be one of: {', '.join(agent_limits.BUDGET_SCOPES)}.")
        if period not in agent_limits.BUDGET_PERIODS:
            raise ValidationError(
                f"period must be one of: {', '.join(agent_limits.BUDGET_PERIODS)}."
            )
        if action not in agent_limits.BUDGET_ACTIONS:
            raise ValidationError(
                f"action must be one of: {', '.join(agent_limits.BUDGET_ACTIONS)}."
            )
        try:
            tokens = int(str(max_tokens).replace(",", "").replace(" ", ""))
        except ValueError:
            raise ValidationError("The limit must be a whole number of tokens.") from None
        if not 1 <= tokens <= usage_data.NUMBER_MAX:
            raise ValidationError(f"The limit must be from 1 to {usage_data.NUMBER_MAX} tokens.")
        if scope == "project" and not (project or "").strip():
            raise ValidationError("A project budget needs its project.")
        if scope == "task" and not (task or "").strip():
            raise ValidationError("A task budget needs its task.")
        async with self._tx() as tx:
            target = await self.get_project(project or "", tx.conn) if scope == "project" else None
            bound = await self.get_task(task or "", tx.conn) if scope == "task" else None
            project_id = target.id if target else None
            task_id = bound.id if bound else None
            old = await repo.find_budget(tx.conn, scope, project_id, task_id)
            if old is not None and (old.period, old.max_tokens, old.action) == (
                period,
                tokens,
                action,
            ):
                return old  # the same numbers again: the marks stay, nothing is announced twice
            budget_id = await repo.save_budget(
                tx.conn,
                old.id if old else None,
                scope=scope,
                project_id=project_id,
                task_id=task_id,
                period=period,
                max_tokens=tokens,
                action=action,
                now=tx.now,
                actor=actor,
            )
            await repo.clear_budget_marks(tx.conn, budget_id)
            self._forget_told(budget_id)
            budget = await repo.get_budget(tx.conn, budget_id)
            assert budget is not None
            await tx.event(
                "budget.set",
                actor,
                project_id=project_id or (bound.project_id if bound else None),
                task_id=task_id,
                budget_id=budget_id,
                target=budget.target,
                period=period,
                max_tokens=tokens,
                action=action,
                **({"project": budget.project_slug} if budget.project_slug else {}),
                **({"key": bound.key, "stage": bound.stage.value} if bound else {}),
            )
        # After the commit: woken earlier, the loop would read no budget and sleep again.
        self._limits_wake.set()
        return budget

    async def remove_budget(self, budget_id: int, *, actor: str = "owner") -> None:
        """Board only. A brake the budget set stays until the owner releases it."""
        async with self._tx() as tx:
            budget = await repo.get_budget(tx.conn, budget_id)
            if budget is None:
                raise NotFoundError(f"Budget #{budget_id} not found.")
            await repo.delete_budget(tx.conn, budget_id)
            self._forget_told(budget_id)
            await tx.event(
                "budget.removed",
                actor,
                project_id=budget.project_id,
                task_id=budget.task_id,
                budget_id=budget_id,
                target=budget.target,
                **({"project": budget.project_slug} if budget.project_slug else {}),
                **({"key": budget.task_key} if budget.task_key else {}),
            )

    async def budget_states(
        self, *, project: str | None = None, task: str | None = None
    ) -> list[agent_limits.BudgetState]:
        """Every budget with its current numbers; with `project` or `task`, those that count it:
        its own, its project's and the one over all projects."""
        today = datetime.now(UTC).date().isoformat()
        marks = await repo.budget_marks(self._r, today, agent_limits.TOTAL_KEY)
        budgets = await repo.list_budgets(self._r)
        if task:
            found = await self.get_task(task)
            budgets = [
                b
                for b in budgets
                if b.scope == "all"
                or b.task_id == found.id
                or (b.scope == "project" and b.project_id == found.project_id)
            ]
        elif project:
            project_id = (await self.get_project(project)).id
            budgets = [
                b
                for b in budgets
                if b.scope == "all" or (b.scope == "project" and b.project_id == project_id)
            ]
        states = []
        for budget in budgets:
            levels = marks.get(budget.id, set())
            level = (
                agent_limits.EXCEEDED
                if agent_limits.EXCEEDED in levels
                else next(iter(levels), None)
            )
            used = await self._budget_used(self._r, budget, today)
            states.append(agent_limits.BudgetState(budget, used, level))
        return states

    async def limits_pass(self) -> None:
        """After each usage pass, never on the hook path. A threshold counts once per period; its
        mark, its event and a `stop` budget's brake are written together."""
        now = datetime.now(UTC)
        today = now.date().isoformat()
        for brake in await repo.daily_brakes_before(self._r, today):
            async with self._tx() as tx:
                if (await repo.get_brake(tx.conn, brake.id) or brake).released_at is None:
                    await self._release(tx, brake, agent_limits.NEW_DAY)
        marks = await repo.budget_marks(self._r, today, agent_limits.TOTAL_KEY)
        for budget in await repo.list_budgets(self._r):
            used = await self._budget_used(self._r, budget, today)
            level = agent_limits.level_for(used, budget.max_tokens)
            if level is not None and level not in marks.get(budget.id, set()):
                await self._cross_threshold(budget.id, today)
        await self._check_concurrency(now)

    async def _cross_threshold(self, budget_id: int, today: str) -> None:
        """Numbers and marks are read again inside the transaction: the owner may have changed
        the budget since the pass looked at it."""
        async with self._tx() as tx:
            budget = await repo.get_budget(tx.conn, budget_id)
            if budget is None:
                return
            used = await self._budget_used(tx.conn, budget, today)
            level = agent_limits.level_for(used, budget.max_tokens)
            marks = await repo.budget_marks(tx.conn, today, agent_limits.TOTAL_KEY)
            if level is None or level in marks.get(budget_id, set()):
                return
            key = agent_limits.period_key(budget.period, today)
            # A jump past both thresholds is one event, not a warning followed by the real one.
            for mark in {level, agent_limits.WARNED}:
                await repo.add_budget_mark(tx.conn, budget_id, key, mark, tx.now)
            state = agent_limits.BudgetState(budget, used, level)
            task = await repo.get_task_by_id(tx.conn, budget.task_id) if budget.task_id else None
            await tx.event(
                f"budget.{level}",
                "relaybay",
                project_id=budget.project_id or (task.project_id if task else None),
                task_id=budget.task_id,
                budget_id=budget_id,
                target=budget.target,
                used=used,
                max_tokens=budget.max_tokens,
                period=budget.period,
                period_key=key,
                action=budget.action,
                **({"project": budget.project_slug} if budget.project_slug else {}),
                **({"key": task.key, "stage": task.stage.value} if task else {}),
            )
            if level == agent_limits.EXCEEDED and budget.action == "stop":
                project = None
                if budget.scope == "project":
                    project = await repo.get_project_by_id(tx.conn, budget.project_id or 0)
                await self._insert_brake(
                    tx,
                    scope=budget.scope,
                    project=project,
                    task=task,
                    reason=agent_limits.budget_reason(state),
                    source=agent_limits.SOURCE_BUDGET,
                    actor="budget",
                    budget_id=budget_id,
                )

    async def _concurrency_over(self, now: datetime) -> int | None:
        """How many sessions run when that is above the cap, else None. The cap only warns."""
        if not self.max_concurrent_sessions:
            return None
        since = now - timedelta(minutes=agent_limits.CONCURRENCY_MINUTES)
        running = await repo.running_sessions(self._r, since.isoformat(timespec="milliseconds"))
        return running if running > self.max_concurrent_sessions else None

    async def _check_concurrency(self, now: datetime) -> None:
        running = await self._concurrency_over(now)
        if running is None:
            return
        last = await repo.last_event_of_kind(self._r, "concurrency.over")
        window = now - timedelta(minutes=agent_limits.CONCURRENCY_MINUTES)
        if last is not None and last > window.isoformat(timespec="milliseconds"):
            return
        async with self._tx() as tx:
            await tx.event(
                "concurrency.over",
                "relaybay",
                running=running,
                max_sessions=self.max_concurrent_sessions,
            )

    async def _limits_active(self) -> bool:
        return bool(self.max_concurrent_sessions) or await repo.has_budgets(self._r)

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
        by_rank: bool = False,
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
            by_rank=by_rank,
        )

    async def task_detail(self, ref: str | int, reports: int = 5) -> TaskDetail:
        task = await self.get_task(ref)
        shown_reports = await repo.list_reports(self._r, task.id, reports)
        comments = await repo.list_comments(self._r, task.id)
        texts = [r.body_md for r in shown_reports] + [c.body_md for c in comments]
        cache: dict[tuple[str, str], tuple[mem.Document, str] | None] = {}
        return TaskDetail(
            task=task,
            reports=shown_reports,
            checks=await repo.report_checks(self._r, [r.id for r in shown_reports]),
            marks_from=self.report_marks_from,
            decisions=await repo.list_decisions(self._r, task_id=task.id, limit=10),
            bindings=await repo.list_bindings(self._r, task.id),
            tracker_history=await repo.list_tracker_entries(self._r, task.id),
            comments=comments,
            doc_refs=await self.resolve_doc_links(
                task.description, task.progress, *texts, cache=cache
            ),
            document=await self._task_document(task.doc_path, cache),
            live_actions=await repo.task_events(self._r, task.id, LIVE_ACTION, 20),
            review=await self._load_review(self._r, task.id),
            bind_choices=await self._bind_choices(task),
            plan=await self._load_plan(self._r, task),
            todo=await self._task_todo(self._r, task),
            plan_history=await self._plan_history(task),
            intake=await repo.list_messages(self._r, task_id=task.id, limit=50),
            from_backlog=next(iter(await repo.list_backlog(self._r, task_id=task.id)), None),
            findings=await repo.list_backlog(self._r, origin_task_id=task.id, limit=50),
            budget=next(
                (s for s in await self.budget_states(task=task.key) if s.budget.task_id == task.id),
                None,
            ),
            messages=await repo.list_session_messages(self._r, task_id=task.id, limit=50),
            code=await self.task_code(task),
        )

    async def _bind_choices(self, task: Task, limit: int = 40) -> list[Session]:
        """Sessions the board offers to bind: the project's first, then the others, most
        recently seen first; the bound one is left out."""
        own = await repo.list_sessions(self._r, task.project_id, limit)
        seen = {s.id for s in own}
        others = [s for s in await repo.list_sessions(self._r, None, limit) if s.id not in seen]
        return [s for s in [*own, *others] if s.id != task.session_id][:limit]

    def _merged_rules(self, slug: str, body: str | None, project_version: int = 0) -> Rules:
        """The global rules with the project's override on top; a broken override is logged
        (once) and ignored, so the global rules apply and the reason is shown with them."""
        base = self.global_rules.body
        versions = {
            "global_version": self.global_rules.global_version,
            "project_version": project_version,
            "require_tracker_log": self.global_rules.require_tracker_log,
        }
        if body is None:
            return Rules(base, slug, self.rules_mode, **versions)
        try:
            override = _project_override(slug, body)
        except ValueError as exc:
            error = str(exc)
            if error not in self._logged_rule_errors:
                self._logged_rule_errors.add(error)
                logger.warning("%s The global rules apply to %s instead.", error, slug)
            return Rules(base, slug, self.rules_mode, override_error=error, **versions)
        return Rules(merge_project_rules(base, override), slug, self.rules_mode, **versions)

    async def _rules(self, conn: aiosqlite.Connection, project: Project) -> Rules:
        return self._merged_rules(
            project.slug,
            await repo.get_project_rules(conn, project.id),
            await repo.rule_set_version(conn, project.id),
        )

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

    async def notifications(
        self, after: int | None, limit: int = 100
    ) -> tuple[int, list[tuple[int, Notice]]]:
        """Up to `limit` events after `after` read for the tray: the id to go on from and the
        notices among them. No `after`, or one past the newest event (another database behind
        the same address): nothing to tell, go on from the newest."""
        last = await repo.last_event_id(self._r)
        if after is None or after >= last:
            return last, []
        limit = max(1, min(limit, 500))
        events = await repo.events_of_kinds_after(self._r, after, NOTICE_EVENT_KINDS, limit)
        if len(events) == limit:
            last = events[-1].id  # more to read: go on from here, not from the newest
        elif events:
            last = max(last, events[-1].id)
        ids = {e.task_id for e in events if e.task_id}
        titles = await repo.task_titles(self._r, ids) if ids else {}
        owners = {name.strip().casefold() for name in self.owner_names}
        notices = ((e.id, classify_notice(e, owners, titles)) for e in events)
        return last, [(event_id, notice) for event_id, notice in notices if notice]

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
        repos: list[dict[str, Any]] | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        actor: str = "mcp",
    ) -> tuple[Project, bool]:
        """Create or update a project. `repos` replaces its repositories (matched by name, so the
        tasks' links to a kept one stay); `repo_path` alone sets the first one's path (or adds it).
        `project.repo_path` always ends up as the first repository's path."""
        slug = _clean_slug(slug)
        name = _clean_text(name, "name", NAME_MAX)
        given = {
            "name": name,
            "repo_path": _clean_text(repo_path, "repo_path"),
            "description": _clean_text(description, "description"),
        }
        wanted = self._wanted_repos(repos, slug, given["repo_path"]) if repos is not None else None
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
                repo_changes = await self._set_project_repos(
                    tx, project_id, slug, wanted, given["repo_path"]
                )
                await repo.sync_repo_path(tx.conn, project_id, tx.now)
                await tx.event(
                    "project.created",
                    actor,
                    project_id=project_id,
                    session_id=session_id,
                    project=slug,
                    name=name or slug,
                    **repo_changes,
                )
                created = True
            else:
                fields = {
                    k: v
                    for k, v in given.items()
                    if k != "repo_path" and v is not None and v != getattr(existing, k)
                }
                path = given["repo_path"]
                if fields:
                    await repo.update_project(tx.conn, existing.id, fields, tx.now)
                changes: dict[str, Any] = dict(fields)
                # Compared with the repositories, not with repo_path: with none left, the same
                # path adds one again.
                changes |= await self._set_project_repos(tx, existing.id, slug, wanted, path)
                await repo.sync_repo_path(tx.conn, existing.id, tx.now)
                synced = await repo.get_project_by_id(tx.conn, existing.id)
                if synced is not None and synced.repo_path != existing.repo_path:
                    changes["repo_path"] = synced.repo_path
                if changes:
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

    @staticmethod
    def _wanted_repos(
        repos: list[dict[str, Any]], slug: str, repo_path: str | None
    ) -> list[tuple[str, str, str | None]]:
        """The `repos` of a project_upsert, checked: names unique, the first one's path the same
        as a `repo_path` given with them."""
        if not isinstance(repos, list) or not all(isinstance(r, dict) for r in repos):
            raise ValidationError("repos must be a list of objects with name, path and kind.")
        if len(repos) > REPOS_MAX:
            raise ValidationError(f"repos: at most {REPOS_MAX} repositories in a project.")
        wanted = []
        for r in repos:
            unknown = set(r) - {"name", "path", "kind"}
            if unknown:
                raise ValidationError(
                    f"Unknown repository field(s): {', '.join(sorted(unknown))}."
                    " Allowed: name, path, kind."
                )
            wanted.append(_clean_repo(r.get("name"), r.get("path"), r.get("kind"), slug))
        names = [n.casefold() for n, _, _ in wanted]
        twice = sorted({n for n in names if names.count(n) > 1})
        if twice:
            raise ValidationError(f"Repository names must be unique: {', '.join(twice)}.")
        if repo_path is not None and wanted and wanted[0][1] != repo_path:
            raise ValidationError(
                "repo_path is the first repository's path: pass it in repos only, or the same."
            )
        return wanted

    async def _set_project_repos(
        self,
        tx: _Tx,
        project_id: int,
        slug: str,
        wanted: list[tuple[str, str, str | None]] | None,
        repo_path: str | None,
    ) -> dict[str, Any]:
        """Write the project's repositories: the whole list (`wanted`, matched by name), or only
        a new `repo_path` for the first one. Returns `{"repos": {added, changed, removed}}` for
        the event, or {} when nothing changed."""
        current = await repo.list_project_repos(tx.conn, project_id)
        added: list[str] = []
        changed: list[str] = []
        removed: list[str] = []
        if wanted is None:
            if repo_path is None:
                return {}
            if current:
                first = current[0]
                if first.path != repo_path:
                    await repo.update_project_repo(tx.conn, first.id, {"path": repo_path}, tx.now)
                    changed.append(first.name)
            else:
                name = default_repo_name(repo_path, slug)
                await repo.insert_project_repo(
                    tx.conn, project_id, name, repo_path, None, 0, tx.now
                )
                added.append(name)
        else:
            by_name = {r.name.casefold(): r for r in current}
            keep = {n.casefold() for n, _, _ in wanted}
            for r in current:
                if r.name.casefold() not in keep:
                    await repo.delete_project_repo(tx.conn, r.id)
                    removed.append(r.name)
            for position, (name, path, kind) in enumerate(wanted):
                old = by_name.get(name.casefold())
                if old is None:
                    await repo.insert_project_repo(
                        tx.conn, project_id, name, path, kind, position, tx.now
                    )
                    added.append(name)
                    continue
                diff: dict[str, Any] = {
                    k: v
                    for k, v in (("name", name), ("path", path), ("kind", kind))
                    if getattr(old, k) != v
                }
                if old.position != position:
                    diff["position"] = position
                if diff:
                    await repo.update_project_repo(tx.conn, old.id, diff, tx.now)
                    changed.append(name)
        report = {
            k: v for k, v in (("added", added), ("changed", changed), ("removed", removed)) if v
        }
        return {"repos": report} if report else {}

    async def project_repos(self, slug: str) -> tuple[Project, list[ProjectRepo]]:
        project = await self.get_project(slug)
        return project, await repo.list_project_repos(self._r, project.id)

    async def save_project_repo(
        self,
        slug: str,
        repo_id: int | None,
        *,
        name: str | None,
        path: str | None,
        kind: str | None,
        actor: str = "board",
    ) -> ProjectRepo:
        """Add a repository (`repo_id` None, last in the list) or change one; a rename keeps the
        row, so the tasks' links to it stay."""
        async with self._tx() as tx:
            project = await self.get_project(slug, tx.conn)
            current = await repo.list_project_repos(tx.conn, project.id)
            clean = _clean_repo(name, path, kind, project.slug)
            old = next((r for r in current if r.id == repo_id), None)
            if repo_id is not None and old is None:
                raise NotFoundError(f"Repository #{repo_id} not found in {project.slug}.")
            if any(r.name.casefold() == clean[0].casefold() for r in current if r is not old):
                raise ValidationError(f"{project.slug} already has a repository {clean[0]!r}.")
            wanted = [(r.name, r.path, r.kind) for r in current]
            if old is None:
                if len(current) >= REPOS_MAX:
                    raise ValidationError(f"At most {REPOS_MAX} repositories in a project.")
                wanted.append(clean)
            else:
                wanted[current.index(old)] = clean
                if old.name != clean[0]:
                    await repo.update_project_repo(tx.conn, old.id, {"name": clean[0]}, tx.now)
            changes = await self._set_project_repos(tx, project.id, project.slug, wanted, None)
            if old is not None and old.name != clean[0]:
                changes.setdefault("repos", {}).setdefault("changed", [clean[0]])
            await self._repos_written(tx, project, changes, actor)
            saved = next(
                r for r in await repo.list_project_repos(tx.conn, project.id) if r.name == clean[0]
            )
        return saved

    async def remove_project_repo(self, slug: str, repo_id: int, *, actor: str = "board") -> None:
        """Remove a repository: the tasks' links to it go, their pull requests stay, untied."""
        async with self._tx() as tx:
            project = await self.get_project(slug, tx.conn)
            current = await repo.list_project_repos(tx.conn, project.id)
            if not any(r.id == repo_id for r in current):
                raise NotFoundError(f"Repository #{repo_id} not found in {project.slug}.")
            wanted = [(r.name, r.path, r.kind) for r in current if r.id != repo_id]
            changes = await self._set_project_repos(tx, project.id, project.slug, wanted, None)
            await self._repos_written(tx, project, changes, actor)

    async def move_project_repo(
        self, slug: str, repo_id: int, step: int, *, actor: str = "board"
    ) -> None:
        """Move a repository one place up (`step` < 0) or down; the first one is the project's
        `repo_path`."""
        async with self._tx() as tx:
            project = await self.get_project(slug, tx.conn)
            current = await repo.list_project_repos(tx.conn, project.id)
            at = next((i for i, r in enumerate(current) if r.id == repo_id), None)
            if at is None:
                raise NotFoundError(f"Repository #{repo_id} not found in {project.slug}.")
            to = at + (1 if step > 0 else -1)
            if not 0 <= to < len(current):
                return
            current[at], current[to] = current[to], current[at]
            wanted = [(r.name, r.path, r.kind) for r in current]
            changes = await self._set_project_repos(tx, project.id, project.slug, wanted, None)
            await self._repos_written(tx, project, changes, actor)

    async def _repos_written(
        self, tx: _Tx, project: Project, changes: dict[str, Any], actor: str
    ) -> None:
        await repo.sync_repo_path(tx.conn, project.id, tx.now)
        synced = await repo.get_project_by_id(tx.conn, project.id)
        if synced is not None and synced.repo_path != project.repo_path:
            changes["repo_path"] = synced.repo_path
        if changes:
            await tx.event(
                "project.updated",
                actor,
                project_id=project.id,
                project=project.slug,
                changes=changes,
            )

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
        """`update_task` that also returns the rule warnings of a stage move. `repos` and `prs`
        in `changes` add or remove the task's code links in the same transaction."""
        links = {k: _link_entries(changes[k], k) for k in LINK_NAMES if k in changes}
        changes = {k: v for k, v in changes.items() if k not in LINK_NAMES}
        async with self._tx() as tx:
            return await self._update_task_tx(
                tx,
                ref,
                changes,
                links=links,
                expected_version=expected_version,
                session=session,
                session_title=session_title,
                hook_session=hook_session,
                actor=actor,
            )

    async def _update_task_tx(
        self,
        tx: _Tx,
        ref: str | int,
        changes: dict[str, Any],
        *,
        links: dict[str, Any] | None = None,
        expected_version: int | None,
        session: str | None,
        session_title: str | None,
        hook_session: str | None,
        actor: str,
    ) -> Saved:
        links = links or {}
        unknown = set(changes) - set(FIELD_NAMES)
        if unknown:
            raise ValidationError(
                f"Unknown task field(s): {', '.join(sorted(unknown))}."
                f" Allowed: {', '.join((*FIELD_NAMES, *LINK_NAMES))}."
            )
        clean = {name: _clean_field(name, value) for name, value in changes.items()}
        default_actor = actor
        task = await self.get_task(ref, tx.conn)
        if expected_version is not None and expected_version != task.version:
            raise ConflictError(
                f"Task {task.key} is at version {task.version}, not {expected_version}:"
                " someone changed it meanwhile. Re-read it with task_get and retry."
            )
        diff = _task_diff(task, clean)
        if not diff and not any(links.values()):
            return Saved(task, [])
        warnings = await self._stage_warnings(tx, task, diff)
        progress_in_dev = "progress" in diff and task.stage == Stage.DEV
        if progress_in_dev and await self._reported_after_plan_closed(tx, task):
            _note_closed_plan(task)
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
        new_issue = "tracker_ref" in diff and "tracker_logged" not in clean and task.tracker_logged
        writes = {**diff, "tracker_logged": False} if unlog or new_issue else diff
        if "stage" in diff:
            rank = await repo.top_rank(tx.conn, task.project_id, diff["stage"])
            await repo.set_rank(tx.conn, task.id, rank)
        if writes:  # links alone do not bump the task's version
            await repo.update_task(tx.conn, task.id, writes, tx.now)
            await self._sync_tracker(tx, task, writes)
        how = "owner" if default_actor in BOARD_ACTORS else "agent"
        linked = await self._write_code_links(tx, task, links, actor, how) if links else {}
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
            if diff["stage"] == Stage.DONE:
                await self._close_owner_items(tx, task, actor)
        # An automatic unlog has its own event; a new issue's reset is an ordinary change.
        others = {k: v for k, v in (diff if unlog else writes).items() if k != "stage"}
        others |= linked
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

    async def _write_code_links(
        self, tx: _Tx, task: Task, links: dict[str, list[str]], actor: str, how: str
    ) -> dict[str, Any]:
        """Add or remove (`-entry`) the task's repository and pull request links. Returns the
        event's `changes`: `{"repos": {"added": [...], "removed": [...]}, "prs": {...}}`."""
        repos = await repo.list_project_repos(tx.conn, task.project_id)
        by_name = {r.name.casefold(): r for r in repos}

        def find(name: str) -> ProjectRepo:
            found = by_name.get(name.casefold())
            if found is None:
                known = ", ".join(r.name for r in repos)
                raise ValidationError(
                    f"{task.project_slug} has no repository {name!r}."
                    + (
                        f" Its repositories: {known}."
                        if known
                        else " It has none yet: set them with project_upsert(repos=...)."
                    )
                )
            return found

        out: dict[str, dict[str, list[str]]] = {"repos": {}, "prs": {}}

        async def link_repo(r: ProjectRepo) -> None:
            if await repo.insert_task_repo(tx.conn, task.id, r.id, actor, how, tx.now):
                out["repos"].setdefault("added", []).append(r.name)

        for entry in links.get("repos", []):
            if entry.startswith("-"):
                r = find(entry[1:].strip())
                if await repo.delete_task_repo(tx.conn, task.id, r.id):
                    out["repos"].setdefault("removed", []).append(r.name)
            else:
                await link_repo(find(entry))
        for entry in links.get("prs", []):
            if entry.startswith("-"):
                url = _clean_url(entry[1:].strip(), "prs")
                if url and await repo.delete_task_pr(tx.conn, task.id, url):
                    out["prs"].setdefault("removed", []).append(url)
                continue
            words = entry.split()
            if len(words) > 2:
                raise ValidationError(
                    f"prs: {entry!r} is not a link, or a repository name and a link."
                )
            named = find(words[0]) if len(words) == 2 else None
            url = _clean_url(words[-1], "prs")
            assert url is not None
            target = named or by_name.get(pr_repo_name(url) or "")
            if await repo.insert_task_pr(
                tx.conn,
                task.id,
                target.id if target else None,
                url,
                pr_number(url),
                None,
                actor,
                tx.now,
            ):
                out["prs"].setdefault("added", []).append(url)
            if target is not None:
                await link_repo(target)  # a pull request in a repository: the task touches it
        return {k: v for k, v in out.items() if v}

    async def _auto_link_repos(self, session: Session, cwd: str) -> None:
        """A session bound to a task works in `cwd`: link the task to the project repository whose
        path holds it (the closest one). One read outside any transaction; a write transaction
        only for a new link, and its `task.updated` says `how: auto`, which the digests leave
        out. Its own transaction: a failure here never undoes the hook's status write."""
        best: dict[int, tuple[int, int, bool]] = {}  # task -> (path length, repo, linked)
        for task_id, repo_id, path, linked in await repo.session_repo_candidates(
            self._r, session.id
        ):
            size = holds(path, cwd)
            if size is None:
                continue
            if task_id not in best or size > best[task_id][0]:
                best[task_id] = (size, repo_id, linked)
        new = [(task_id, repo_id) for task_id, (_, repo_id, linked) in best.items() if not linked]
        if not new:
            return
        async with self._tx() as tx:
            for task_id, repo_id in new:
                await self._auto_link(tx, session, task_id, repo_id)

    async def _auto_link(self, tx: _Tx, session: Session, task_id: int, repo_id: int) -> None:
        if not await repo.insert_task_repo(tx.conn, task_id, repo_id, session.name, "auto", tx.now):
            return  # linked meanwhile
        task = await self.get_task(task_id, tx.conn)
        name = next(
            r.name
            for r in await repo.list_project_repos(tx.conn, task.project_id)
            if r.id == repo_id
        )
        await tx.event(
            "task.updated",
            session.name,
            project_id=task.project_id,
            task_id=task.id,
            session_id=session.id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            changes={"repos": {"added": [name]}},
            how="auto",
        )

    async def task_code(self, task: Task, conn: aiosqlite.Connection | None = None) -> TaskCode:
        conn = conn or self._r
        return task_code(
            task.id,
            await repo.list_project_repos(conn, task.project_id),
            await repo.task_repo_links(conn, task_id=task.id),
            await repo.task_prs(conn, task_id=task.id),
        )

    async def task_codes(self, project_id: int, tasks: list[Task]) -> dict[int, TaskCode]:
        """The Code rows of a board's tasks: three reads for the whole project."""
        repos = await repo.list_project_repos(self._r, project_id)
        links = await repo.task_repo_links(self._r, project_id=project_id)
        prs = await repo.task_prs(self._r, project_id=project_id)
        return {t.id: task_code(t.id, repos, links, prs) for t in tasks}

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

    async def move_task(
        self,
        ref: str | int,
        *,
        stage: str | None = None,
        before: str | None = None,
        after: str | None = None,
        actor: str = "board",
    ) -> Saved:
        """A card dropped on the board: a new `stage` goes the way of every stage change (rules,
        warnings, its event), then the card is placed right before the task `before`, else right
        after `after`, else at the top. The column is renumbered 1..n, and a changed order writes
        one `task.reordered`."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            saved = Saved(task, [])
            if stage and parse_stage(stage) != task.stage:
                saved = await self._update_task_tx(
                    tx,
                    task.id,
                    {"stage": stage},
                    expected_version=None,
                    session=None,
                    session_title=None,
                    hook_session=None,
                    actor=actor,
                )
                task = saved.task
            current = await repo.column_order(tx.conn, task.project_id, task.stage.value)
            wanted = [i for i in current if i != task.id]
            index = 0
            neighbour = before or after
            if neighbour:
                other = await self.get_task(neighbour, tx.conn)
                if other.id == task.id:
                    raise ValidationError(
                        f"{task.key} cannot be placed before or after itself; name another card."
                    )
                if other.id not in wanted:
                    raise ConflictError(
                        f"{other.key} is no longer in this column: the board changed meanwhile."
                        " Try again."
                    )
                index = wanted.index(other.id) + (0 if before else 1)
            wanted.insert(index, task.id)
            if wanted != current:
                await repo.renumber_column(tx.conn, wanted)
                await tx.event(
                    "task.reordered",
                    actor,
                    project_id=task.project_id,
                    task_id=task.id,
                    project=task.project_slug,
                    key=task.key,
                    stage=task.stage.value,
                    position=index + 1,
                )
            return Saved(await self.get_task(task.id, tx.conn), saved.warnings)

    async def set_task_rank(self, ref: str | int, rank: int, *, actor: str) -> Task:
        """A rank as an import file gives it; one `task.reordered` when it changes."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if rank != task.rank:
                await repo.set_rank(tx.conn, task.id, rank)
                await tx.event(
                    "task.reordered",
                    actor,
                    project_id=task.project_id,
                    task_id=task.id,
                    project=task.project_slug,
                    key=task.key,
                    stage=task.stage.value,
                    rank=rank,
                )
            return await self.get_task(task.id, tx.conn)

    async def bind_session(
        self,
        ref: str | int,
        session: str,
        *,
        session_title: str | None = None,
        caller: str | None = None,
        hook_session: str | None = None,
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
            await self._refuse_under_brake(tx.conn, task, caller, hook_session)
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

    async def _refuse_under_brake(
        self, conn: aiosqlite.Connection, task: Task, caller: str | None, hook_session: str | None
    ) -> None:
        """Over MCP a binding does not change while a brake holds the task or its project: moving
        the session off the task would lift the brake. Scope `all` is left out, a binding cannot
        lift it. The board and coordinators may."""
        for brake in await repo.open_brakes(conn):
            if (brake.scope == "task" and brake.task_id == task.id) or (
                brake.scope == "project" and brake.project_id == task.project_id
            ):
                if await self._is_coordinator(conn, caller, hook_session):
                    return
                raise ValidationError(
                    f"{task.key} is under the brake ({brake.target}): its binding stays until the"
                    " owner releases the brake on the board. Nothing changed."
                )

    async def unbind_session(
        self,
        ref: str | int,
        session: str | None = None,
        *,
        actor: str = "mcp",
        caller: str | None = None,
        hook_session: str | None = None,
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
            if actor != "board":
                await self._refuse_under_brake(tx.conn, task, caller, hook_session)
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
        given = {"progress": progress, "next_step": next_step, "waiting_on": waiting_on}
        clean = {k: _clean_field(k, v) for k, v in given.items() if v is not None}
        default_actor = _clean_text(author, "author", NAME_MAX) or "mcp"
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            rules = await self._rules(tx.conn, await self.get_project(task.project_slug, tx.conn))
            problem = report_format_problem(body, rules.body["report_format"])
            if problem and self.rules_mode == "strict":
                raise ValidationError(f"Rules: {problem} Nothing changed.")
            sess = await self._session(tx, session, session_title, hook_session)
            report_id = await self._write_report(
                tx,
                task,
                body,
                clean,
                session_id=sess.id if sess else None,
                actor=sess.name if sess else default_actor,
                format_problem=problem,
            )
            report = await repo.get_report(tx.conn, report_id)
            assert report is not None
            task = await self.get_task(task.id, tx.conn)
        return task, report

    async def _write_report(
        self,
        tx: _Tx,
        task: Task,
        body: str,
        clean: dict[str, Any],
        *,
        session_id: int | None,
        actor: str,
        format_problem: str | None,
    ) -> int:
        """The writes of one report, for report_add and a run's turn alike: the rules warnings
        (`format_problem`, the first line's, checked by the caller; no `verified:` mark on an M or
        L report), the task's fields, the report with its marks, its event and its index row."""
        state, size, summary = parse_report(body)
        checks = parse_checks(body)
        problems = (format_problem, verified_marks_problem(size, len(checks)))
        warnings = [f"Warning (rules): {p}" for p in problems if p]
        for warning in warnings:
            _note(warning)
        if await self._work_after_closed_plan(tx, task, state):
            _note_closed_plan(task)
        if size and not task.size:
            clean["size"] = size  # the first report's size sizes a task that has none
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
        if checks:
            await repo.insert_report_checks(tx.conn, report_id, checks)
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
            **({"checks": len(checks)} if checks else {}),
            **({"warnings": warnings} if warnings else {}),
        )
        await self._index(tx, "report", report_id)
        return report_id

    async def _reported_after_plan_closed(self, tx: _Tx, task: Task) -> bool:
        """Every plan step is done and a report came after the plan closed, so the work that
        follows is not the closing itself. The plan closes with its last done step or with the
        plan write that dropped the last open one, whichever is newer."""
        if not _plan_closed(task):
            return False
        steps = await repo.list_plan_steps(tx.conn, task.id)
        moments = [s.done_at for s in steps if s.done_at] + [task.plan_revised_at]
        closed_at = max((m for m in moments if m), default=None)
        return closed_at is not None and await repo.has_report_after(tx.conn, task.id, closed_at)

    async def _work_after_closed_plan(self, tx: _Tx, task: Task, state: str | None) -> bool:
        """A report says work goes on although every plan step is done: code changed in any open
        stage, or verified in dev; never the report that closes the plan."""
        changed = state == "changed" and task.stage in OPEN_STAGES
        if not (changed or (state == "verified" and task.stage == Stage.DEV)):
            return False
        return await self._reported_after_plan_closed(tx, task)

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

    async def _current_text(
        self, project: str, doc: str, cache: dict[tuple[str, str], tuple[mem.Document, str] | None]
    ) -> tuple[mem.Document, str] | None:
        """A document and its current text, read once per `cache` (one page)."""
        key = (project, doc)
        if key not in cache:
            proj = await repo.get_project(self._r, project)
            found = await repo.get_document(self._r, proj.id, doc) if proj else None
            row = (
                await repo.get_document_version(self._r, found.id, found.version) if found else None
            )
            cache[key] = (found, row.body) if found and row else None
        return cache[key]

    async def resolve_doc_links(
        self,
        *texts: str | None,
        cache: dict[tuple[str, str], tuple[mem.Document, str] | None] | None = None,
    ) -> dict[str, mem.DocRef]:
        """Where each `relaybay://doc/<project>/<doc>[#<anchor>]` link in `texts` points on the
        board, by link text. A missing section still points at the document, marked broken."""
        cache = {} if cache is None else cache
        refs: dict[str, mem.DocRef] = {}
        for link in digests.doc_links(*texts):
            loaded = await self._current_text(link.project, link.doc, cache)
            refs[link.uri] = _doc_ref(link, loaded[1] if loaded else None)
        return refs

    async def _task_document(
        self,
        doc_path: str | None,
        cache: dict[tuple[str, str], tuple[mem.Document, str] | None],
    ) -> TaskDocument | None:
        link = digests.doc_link(doc_path)
        if link is None:
            return None
        loaded = await self._current_text(link.project, link.doc, cache)
        ref = _doc_ref(link, loaded[1] if loaded else None)
        if loaded is None:
            return TaskDocument(link, ref, None, "")
        found, body = loaded
        text = body
        if link.anchor and not ref.broken:
            text = mem.section_text(body, link.anchor)
        return TaskDocument(link, ref, found, text)

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
        source_url: str | None = None,
        force: bool = False,
        author: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> mem.Document:
        """Write a new version of a document; `base_version` 0 creates it. With `section`, the
        text replaces only that section. A `base_version` other than the current version is a
        conflict whose message holds the current text to merge. `source_url` links the document
        to its repository ('-' unlinks it); a linked document changes only with `force`, and its
        body may be '-' (no text of its own)."""
        slug = _clean_doc_slug(doc)
        no_text = str(body or "").strip() == "-"
        text = "" if no_text else _clean_text(body, "body", mem.DOC_BODY_MAX) or ""
        if not text and not no_text:
            raise ValidationError("body must not be empty.")
        if no_text and section:
            raise ValidationError("A section needs text; '-' empties a linked document only.")
        unlink = str(source_url or "").strip() == "-"
        link = None if unlink else _clean_url(source_url, "source_url")
        if link is not None and len(link) > mem.SOURCE_URL_MAX:
            raise ValidationError(f"source_url is longer than {mem.SOURCE_URL_MAX} characters.")
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
                source_url=link,
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
                    tx.conn,
                    proj.id,
                    slug,
                    new_title or heading or slug,
                    new_kind or "doc",
                    tx.now,
                    link,
                )
                found = await repo.get_document(tx.conn, proj.id, slug)
                assert found is not None and found.id == doc_id
            else:
                if found.source_url and not force:
                    raise ValidationError(
                        f"{proj.slug}/{found.slug} lives in the repository: {found.source_url}."
                        " Change it there; pass force to write this copy anyway."
                    )
                if no_text and link and not force:
                    raise ValidationError(
                        f"body '-' drops the text of {proj.slug}/{found.slug}; pass force to link"
                        " it to its repository without the text kept here."
                    )
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
            final_link = None if unlink else link or found.source_url
            if not new_body and not final_link:
                raise ValidationError("Only a document linked with source_url may have no text.")
            if found.version and (new_body, final_title, final_kind, final_link) == (
                current,
                found.title,
                found.kind,
                found.source_url,
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
                source_url=final_link,
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

    def plan_draft_hours(self, task: Task, now: datetime | None = None) -> int | None:
        """Whole hours since the plan of a task in dev was last written while it is still a
        draft, once that reaches `plan_draft_hint_hours` (0: off); else None."""
        limit = self.plan_draft_hint_hours
        if limit <= 0 or task.stage != Stage.DEV or task.plan_status != "draft":
            return None
        if not task.plan_total or not task.plan_revised_at:
            return None
        age = (now or datetime.now(UTC)) - datetime.fromisoformat(task.plan_revised_at)
        hours = int(age.total_seconds() // 3600)
        return hours if hours >= limit else None

    async def plan_draft_hints(
        self, project: str | None = None, *, task: Task | None = None
    ) -> list[str]:
        """'Hint: <key>'s plan is a draft for N h …' for the coordinator: of one task, one
        project's tasks in dev, or every project's."""
        if self.plan_draft_hint_hours <= 0:
            return []
        now = datetime.now(UTC)
        if task is not None:
            hours = self.plan_draft_hours(task, now)
            return [] if hours is None else [DRAFT_HINT.format(key=task.key, hours=hours)]
        project_id = (await self.get_project(project)).id if project else None
        before = now - timedelta(hours=self.plan_draft_hint_hours)
        found, total = await repo.stale_draft_plans(
            self._r, before.isoformat(timespec="milliseconds"), project_id, DRAFT_HINTS_MAX
        )
        lines = []
        for key, revised in found:
            hours = int((now - datetime.fromisoformat(revised)).total_seconds() // 3600)
            lines.append(DRAFT_HINT.format(key=key, hours=hours))
        if total > len(found):
            lines.append(f"…and {total - len(found)} more drafts.")
        return lines

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
        # Asked before the reader links its hook session, or the call would prove itself.
        coordinator = await self._is_coordinator(self._r, session, hook_session)
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
            rules_line=rules_line(self.global_rules),
        )
        footer = self._footer(reader, since_event, changes, moves)
        rendered = digests.portfolio_text(data, footer, self._budget(budget))
        await self._move_cursor("portfolio", 0, reader, changes.reached, moves, rendered)
        hints = await self.plan_draft_hints() if coordinator else []
        return "\n".join([rendered.text, *hints])

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
            rules_line=rules_line(await self._rules(self._r, project)),
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
        coordinator = await self._is_coordinator(self._r, session, hook_session)
        reader = await self._digest_reader(session, session_title, hook_session)
        text = await self._project_digest(found, reader, since_event, self._budget(budget))
        hints = await self.plan_draft_hints(found.slug) if coordinator else []
        return "\n".join([text, *hints])

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
            comments=[c for c in unread if c.to_agent],
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
        # Asked before the reader links its hook session, or the call would prove itself.
        coordinator = await self._is_coordinator(self._r, session, hook_session)
        reader = await self._digest_reader(session, session_title, hook_session)
        text, _ = await self._task_context(task, reader, since_event, self._budget(budget))
        hints = await self.plan_draft_hints(task=task) if coordinator else []
        return "\n".join([text, *hints])

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

    async def _unread(self, tx: _Tx, task: Task, to_agent: bool) -> list[Comment]:
        unread = await repo.list_comments(tx.conn, task.id, unread_only=True)
        return [c for c in unread if c.to_agent == to_agent]

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
                if comment is None or not comment.to_agent:
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

    # --- session messages --------------------------------------------------

    async def _message_caller(
        self,
        tx: _Tx,
        session: str | None,
        title: str | None,
        hook_session: str | None,
        *,
        create: bool = True,
    ) -> tuple[Session, bool]:
        """Who writes, and whether its hook link proves it. Every session the name may mean counts,
        desktop titles too: a live, exactly linked one is only itself with that link's id, and an
        id a live session linked belongs to that session. All clients share one token, so this
        stops mistakes and casual impersonation, it is not access control; a message never grants
        anything, and a name no hook proved is framed as not verified. `create`: a new name may
        become a row (a send), or it is an error (an ack)."""
        cc_id = _clean_text(hook_session, "hook_session", CC_ID_MAX)
        value = intake.one_line(_clean_text(session, "session", NAME_MAX)) or None
        if value is None and cc_id is None:
            raise ValidationError(
                "Say who is calling: pass session, and hook_session if the session start gave you"
                " one."
            )
        holder = await repo.get_session_by_cc(tx.conn, cc_id) if cc_id else None
        if value is None and holder is None:
            raise ValidationError(f"Hook session {cc_id} is not known yet: pass session too.")
        if value is None:
            matches = [holder]
        else:
            matches = await repo.find_sessions(tx.conn, parse_session_id(value) or value)
        proven = [s for s in matches if _hook_proven(s)]
        own = next((s for s in proven if s.cc_session_id == cc_id), None)
        if proven and own is None:
            raise ValidationError(
                f"{proven[0].name} is a live session with its own hook link: pass its hook_session."
                " A session message cannot be written in another session's name. If that is you"
                " after a restart with no session end, any other write with session and your new"
                " hook_session (report_add, task_update) moves the link."
            )
        if holder is not None and _hook_proven(holder) and own is None:
            raise ValidationError(
                f"That hook_session belongs to another session ({holder.name}). Pass your own."
            )
        row = own or await self._message_row(tx.conn, value, matches)
        if row is None:
            if not create:
                raise NotFoundError(f"No session called {value!r}. Nothing was acknowledged.")
            caller = await self._session(tx, value, title, cc_id)
        elif value is None:
            assert cc_id is not None
            caller = await self._link_hook_session(tx, row, cc_id)
        else:
            caller = await self._session(tx, row.external_id or value, title, cc_id)
        assert caller is not None
        return caller, _hook_proven(caller) and caller.cc_session_id == cc_id

    async def _message_row(
        self, conn: aiosqlite.Connection, value: str | None, matches: list[Session]
    ) -> Session | None:
        """The one row a caller's name means, none of them live. Its plain name or alias goes
        first, as on every other write; desktop titles may repeat, and then only the id says."""
        known = await self._known_session(value, conn) if value else None
        if known is not None or len(matches) < 2:
            return known or (matches[0] if matches else None)
        ids = ", ".join(s.external_id or s.name for s in matches)
        raise ValidationError(f"{len(matches)} sessions are called {value!r}: {ids}.")

    async def _named_session(self, conn: aiosqlite.Connection, ref: str, field: str) -> Session:
        """The one session a name, desktop id, alias or hook session id names, without creating
        or touching a row. None or several: an error that says so."""
        text = _clean_text(ref, field, CC_ID_MAX)
        if text is None:
            raise ValidationError(f"{field} must not be empty.")
        external_id = parse_session_id(text)
        found = await repo.find_sessions(conn, external_id or text)
        if not found:
            by_cc = await repo.get_session_by_cc(conn, text)
            found = [by_cc] if by_cc else []
        if not found:
            raise NotFoundError(f"No session called {text!r}. Nothing was sent or listed.")
        if len(found) > 1:
            names = ", ".join(
                f"{s.external_id or 'a plain session, reach it through its task'}"
                f" ({s.status or 'no hook status'})"
                for s in found
            )
            raise ValidationError(f"{len(found)} sessions are called {text!r}: {names}.")
        return found[0]

    async def _message_recipient(
        self,
        tx: _Tx,
        to: str | None,
        task: Task | None,
        parent: SessionMessage | None,
        sender: Session,
    ) -> Session:
        """`to`; a reply goes to the other side of the message it answers; else the session
        bound to `task`."""
        if to:
            return await self._named_session(tx.conn, to, "to")
        other_id = None
        if parent is not None:
            mine = parent.recipient_session_id == sender.id
            other_id = parent.sender_session_id if mine else parent.recipient_session_id
        elif task is not None:
            other_id = await repo.open_binding(tx.conn, task.id)
            if other_id is None:
                raise ValidationError(f"No session is bound to {task.key}: name one with to.")
        else:
            raise ValidationError("Say who it is for: pass to, or task for its bound session.")
        found = await repo.get_session_by_id(tx.conn, other_id) if other_id else None
        if found is None:
            raise ValidationError("That session's row is gone: name the recipient with to.")
        return found

    async def _message_event(
        self, tx: _Tx, kind: str, actor: str, message: SessionMessage, session: Session
    ) -> None:
        """Marks and names only: the text is in the message row, not in the event feed."""
        about: dict[str, Any] = {"project_id": session.project_id}
        if message.task_id is not None:
            about = {
                "project_id": message.task_project_id,
                "task_id": message.task_id,
                "project": message.project_slug,
                "key": message.task_key,
                "stage": message.task_stage,
            }
        await tx.event(
            kind,
            actor,
            session_id=session.id,
            message_id=message.id,
            sender=message.sender_name,
            recipient=message.recipient_name,
            **about,
        )

    async def _read_session_messages(
        self, tx: _Tx, messages: list[SessionMessage], reader: Session, verified: bool
    ) -> list[int]:
        unread = [m for m in messages if m.read_at is None]
        if not unread:
            return []
        await repo.mark_session_messages_read(
            tx.conn, [m.id for m in unread], tx.now, verified=verified
        )
        for message in unread:
            await self._message_event(tx, "message.read", reader.name, message, reader)
        return [m.id for m in unread]

    async def send_session_message(
        self,
        body: Any,
        *,
        to: str | None = None,
        task: str | int | None = None,
        in_reply_to: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[SessionMessage, str]:
        """A message to another session; returns it and an honest line on when it can arrive.
        A reply over the depth limit is refused and shown in the Inbox, so that mark commits
        and the error follows it."""
        text = _clean_message(body)
        refused = None
        async with self._tx() as tx:
            sender, verified = await self._message_caller(tx, session, session_title, hook_session)
            parent = None
            if in_reply_to is not None:
                parent = await repo.get_session_message(tx.conn, in_reply_to)
                if parent is None:
                    raise NotFoundError(f"Session message #{in_reply_to} not found.")
                if sender.id not in (parent.sender_session_id, parent.recipient_session_id):
                    raise ValidationError(
                        f"Session message #{in_reply_to} is between {parent.sender_name} and"
                        f" {parent.recipient_name}: {sender.name} cannot answer it."
                    )
            row = await self.get_task(task, tx.conn) if task else None
            recipient = await self._message_recipient(tx, to, row, parent, sender)
            if recipient.id == sender.id:
                raise ValidationError("A session cannot send a message to itself.")
            if row is None and parent is not None and parent.task_id is not None:
                row = await self.get_task(parent.task_id, tx.conn)
            project = await self.get_project(row.project_slug, tx.conn) if row else None
            _scan(await self._secret_rules(tx.conn, project), body=text)
            start = datetime.now(UTC) - timedelta(seconds=message_text.RATE_SECONDS)
            sent = await repo.session_messages_sent_since(
                tx.conn, sender.id, start.isoformat(timespec="milliseconds")
            )
            if sent >= message_text.RATE_MAX:
                raise ValidationError(
                    f"Too many session messages: at most {message_text.RATE_MAX} per sender per"
                    " minute. Is this a loop? Wait, or ask the owner."
                )
            if parent is not None and parent.depth >= message_text.DEPTH_MAX:
                refused = (
                    f"Reply refused: the thread of #{parent.id} is {parent.depth} replies deep,"
                    f" the limit is {message_text.DEPTH_MAX}. This looks like a loop; the owner"
                    " sees it in the Inbox. Stop answering, or start over with the owner's word."
                )
                if await repo.stop_message_loop(tx.conn, parent.id, tx.now):
                    await self._message_event(
                        tx, "message.loop_stopped", sender.name, parent, sender
                    )
            else:
                message_id = await repo.insert_session_message(
                    tx.conn,
                    sender=sender,
                    verified=verified,
                    recipient=recipient,
                    task_id=row.id if row else None,
                    body_md=text,
                    in_reply_to=parent.id if parent else None,
                    depth=parent.depth + 1 if parent else 0,
                    now=tx.now,
                )
                saved = await repo.get_session_message(tx.conn, message_id)
                assert saved is not None
                await self._message_event(tx, "message.sent", sender.name, saved, sender)
                # Who wrote to whom, as for a desktop SendMessage (Activity shows it).
                await tx.event(
                    "session.message",
                    sender.name,
                    project_id=row.project_id if row else sender.project_id,
                    session_id=sender.id,
                    session=sender.name,
                    to=recipient.name,
                )
                # The same rule as message_ack: an exact link has to be proven.
                mine = parent is not None and parent.recipient_session_id == sender.id
                if mine and (verified or not sender.cc_exact):
                    await self._read_session_messages(tx, [parent], sender, verified)
        if refused is not None:
            raise ValidationError(refused)
        if not verified:
            _note(
                "Warning: your name is not verified, the recipient is told so: pass your own"
                " hook_session from the session start."
            )
        return saved, _message_arrival(recipient)

    async def ack_session_messages(
        self,
        *,
        message_id: int | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> list[int]:
        """Mark messages to the caller read: one by id, or all unread ones. Returns those that
        were unread. It creates no row. A session with an exact link has to prove it, or a
        message acknowledged in its name would never be handed over when it comes back; a
        row that never linked can only be believed, and the mark says so."""
        async with self._tx() as tx:
            caller, verified = await self._message_caller(
                tx, session, session_title, hook_session, create=False
            )
            if not verified and caller.cc_exact:
                raise ValidationError(
                    f"{caller.name} has an exact hook link that this call does not prove: pass"
                    " its live hook_session. A message cannot be acknowledged in another"
                    " session's name."
                )
            if message_id is None:
                targets = await repo.unread_session_messages(tx.conn, caller.id)
            else:
                message = await repo.get_session_message(tx.conn, message_id)
                if message is None:
                    raise NotFoundError(f"Session message #{message_id} not found.")
                if message.recipient_session_id != caller.id:
                    raise ValidationError(
                        f"Session message #{message_id} is for {message.recipient_name}, not for"
                        f" {caller.name}."
                    )
                targets = [message]
            return await self._read_session_messages(tx, targets, caller, verified)

    async def list_session_messages(
        self,
        session: str,
        *,
        box: str = "inbox",
        unread_only: bool = False,
        task: str | int | None = None,
        limit: int = 50,
    ) -> tuple[Session, list[SessionMessage]]:
        """A session's messages, newest first. Reading delivers and acknowledges nothing."""
        if box not in message_text.BOXES:
            raise ValidationError(f"box must be one of: {', '.join(message_text.BOXES)}.")
        found = await self._named_session(self._r, session, "session")
        row = await self.get_task(task) if task else None
        items = await repo.list_session_messages(
            self._r,
            recipient_id=found.id if box == "inbox" else None,
            sender_id=found.id if box == "sent" else None,
            task_id=row.id if row else None,
            unread_only=unread_only,
            limit=max(1, min(limit, message_text.LIST_MAX)),
        )
        return found, items

    async def session_messages_page(
        self, *, session_id: int | None = None, before_id: int | None = None, limit: int = 50
    ) -> list[SessionMessage]:
        """The board's list, newest first."""
        return await repo.list_session_messages(
            self._r, either_id=session_id, before_id=before_id, limit=limit
        )

    async def dismiss_message_loop(self, message_id: int, *, actor: str) -> None:
        """The owner has seen a stopped loop: its Inbox line goes."""
        async with self._tx() as tx:
            message = await repo.get_session_message(tx.conn, message_id)
            if message is None:
                raise NotFoundError(f"Session message #{message_id} not found.")
            await repo.dismiss_message_loop(tx.conn, message_id, tx.now)
            await tx.event("message.loop_dismissed", actor, message_id=message_id)

    async def _message_parts(self, session: Session, *taken: str) -> list[str]:
        """What is waiting for an exactly linked session, as parts of a hook answer: messages share
        what `taken` (comments, warnings, a reminder) left of the answer."""
        if not await repo.undelivered_session_messages(self._r, session.id):
            return []  # the common case: no write transaction per tool call
        budget = CONTEXT_MAX - sum(len(t) + 1 for t in taken)
        async with self._tx() as tx:
            waiting = await repo.undelivered_session_messages(tx.conn, session.id)
            shown = message_text.fit(waiting, budget, alone=not taken)
            await self._deliver_session_messages(tx, session, shown)
        return [message_text.delivery_text(shown, len(waiting))] if shown else []

    async def _deliver_session_messages(
        self, tx: _Tx, session: Session, messages: list[SessionMessage]
    ) -> None:
        if not messages:
            return
        await repo.mark_session_messages_delivered(tx.conn, [m.id for m in messages], tx.now)
        for message in messages:
            await self._message_event(tx, "message.delivered", session.name, message, session)

    async def _stop_message_text(self, tx: _Tx, session: Session, taken: str | None) -> str | None:
        """The messages a Stop block shows, each at most STOP_MESSAGE_CAP times in all."""
        unread = await repo.unread_session_messages(tx.conn, session.id)
        due = [m for m in unread if m.stop_blocks < message_text.STOP_MESSAGE_CAP]
        budget = CONTEXT_MAX - (len(taken) + 1 if taken else 0)
        shown = message_text.fit(due, budget, alone=not taken)
        if not shown:
            return None
        await self._deliver_session_messages(tx, session, [m for m in shown if not m.delivered_at])
        await repo.count_session_message_stop_blocks(tx.conn, [m.id for m in shown])
        return message_text.stop_reason(shown, len(unread))

    async def _stop_message_due(self, conn: aiosqlite.Connection, session: Session) -> bool:
        unread = await repo.unread_session_messages(conn, session.id)
        return any(m.stop_blocks < message_text.STOP_MESSAGE_CAP for m in unread)

    # --- intake: messages from chats and mailboxes -----------------------------------------

    async def _intake_match(
        self, conn: aiosqlite.Connection, source_id: int, thread: str | None, text: str, ref: Any
    ) -> tuple[Task | None, str | None]:
        """The task a new message belongs to and the route that found it: the watcher's `task`,
        a thread tied before, or the one task key of a known project the text names."""
        if ref:
            return await self.get_task(ref, conn), "watcher"
        if thread:
            task_id = await repo.thread_task(conn, source_id, thread)
            if task_id is not None:
                return await self.get_task(task_id, conn), "thread"
        slugs = [p.slug for p in await repo.list_projects(conn)]
        found = []
        for key in intake.named_keys(text, slugs):
            task = await repo.get_task_by_key(conn, key)
            if task is not None:
                found.append(task)
        if len(found) == 1:
            return found[0], "task key"
        return None, None

    async def _forward(self, tx: _Tx, message: IntakeMessage, task: Task) -> Comment:
        """The message as an `external` comment: the comment channel delivers it to the exactly
        linked bound session only, like an owner comment."""
        body = intake.frame(message.source_slug, message.author, message.text or "", task.next_step)
        return await self._insert_comment(
            tx,
            task,
            author_kind="external",
            author=message.source_slug,
            session_id=None,
            body=body,
            in_reply_to=None,
        )

    async def _tie(
        self, tx: _Tx, message: IntakeMessage, task: Task, actor: str, route: str
    ) -> tuple[bool, str]:
        """Tie `message` to `task`: the thread is remembered, a task waiting on someone gets the
        "answer received" mark, and the bound agent is woken when the one rule allows it. The
        stage never changes. Returns whether the agent was woken and why (or why not)."""
        await repo.update_message(tx.conn, message.id, {"status": "tied", "task_id": task.id})
        if message.thread:
            await repo.set_thread(tx.conn, message.source_id, message.thread, task.id, tx.now)
        ids = {"project_id": task.project_id, "task_id": task.id}
        about = {
            "project": task.project_slug,
            "key": task.key,
            "stage": task.stage.value,
            "message_id": message.id,
            "source": message.source_slug,
        }
        await tx.event("intake.tied", actor, **ids, **about, route=route)
        if task.waiting_on and task.stage != Stage.DONE:
            await repo.set_answer_mark(tx.conn, task.id, tx.now, message.id)
            await tx.event(
                "intake.answer_received", actor, **ids, **about, waiting_on=task.waiting_on
            )
        bound = await repo.get_session_by_id(tx.conn, task.session_id) if task.session_id else None
        if task.stage == Stage.DONE:
            reason = "Notification only: the task is done"
        elif not intake.wake_allowed(task, message.source_may_wake):
            reason = intake.autonomy_reason(task, message.source_may_wake)
        elif bound is None:
            reason = "Notification only: no session is bound to the task"
        elif not bound.cc_exact:
            # A comment written for it could never be delivered: say so instead of "woke".
            reason = "Notification only: the bound session is not linked exactly"
        else:
            comment = await self._forward(tx, message, task)
            reason = intake.autonomy_reason(task, True)
            await repo.update_message(tx.conn, message.id, {"outcome": "woke"})
            await tx.event(
                "intake.wake", actor, **ids, **about, comment_id=comment.id, reason=reason
            )
            return True, reason
        if message.outcome not in ("woke", "forwarded"):
            # A message that already reached an agent keeps saying so after a re-tie.
            await repo.update_message(tx.conn, message.id, {"outcome": "notified"})
        return False, reason

    async def intake_post(
        self,
        source: str,
        external_id: str,
        text: str,
        *,
        author: str | None = None,
        thread: str | None = None,
        received_at: str | None = None,
        task: str | None = None,
        kind: str | None = None,
        actor: str = "mcp",
    ) -> intake.PostResult:
        """Store one message from a watcher. The source is created on its first message,
        untrusted. Idempotent on (source, external_id): a repeat returns the stored message and
        writes nothing. A secret value or a source over its hourly limit is stored as `refused`
        without text. Then it is matched to a task (see `_intake_match`) and tied."""
        slug = _clean_source_slug(source)
        ext = _clean_text(external_id, "external_id", intake.EXTERNAL_ID_MAX)
        if not ext:
            raise ValidationError("external_id must not be empty.")
        body = str(text or "").replace("\r\n", "\n").strip()
        if not body:
            raise ValidationError("text must not be empty.")
        if len(body) > intake.TEXT_MAX:
            body = body[: intake.TEXT_MAX - 1].rstrip() + "…"
        # One line each: a name or thread printed in a frame can never start a line of its own.
        thread_id = intake.one_line(_clean_text(thread, "thread", intake.THREAD_MAX)) or None
        who = intake.one_line(_clean_text(author, "author", intake.AUTHOR_MAX)) or None
        source_kind = (kind or "other").strip().lower()
        if source_kind not in intake.SOURCE_KINDS:
            raise ValidationError(f"kind must be one of {', '.join(intake.SOURCE_KINDS)}.")
        received = _clean_moment(received_at, "received_at")
        # Idempotency first: a repeated post writes nothing, not even the source. Only a message
        # refused for the rate limit may come again, once the window has room.
        known = await repo.get_source(self._r, slug)
        if known is not None:
            repeat = await repo.find_message(self._r, known.id, ext)
            if repeat is not None and not _rate_limited(repeat):
                found = await self.get_task(repeat.task_id) if repeat.task_id else None
                return intake.PostResult(repeat, False, found, repeat.outcome == "woke")
        rules = await self._secret_rules(self._r, None)
        secrets_found = [
            name
            for value in (body, who, thread_id)
            if value
            for _, name in envs.find_secrets(value, rules)
        ]
        async with self._tx() as tx:
            src = await repo.get_source(tx.conn, slug)
            retry = None
            if src is None:
                await repo.insert_source(tx.conn, slug, source_kind, slug, tx.now)
                src = await repo.get_source(tx.conn, slug)
                assert src is not None
            elif (repeat := await repo.find_message(tx.conn, src.id, ext)) is not None:
                if not _rate_limited(repeat):
                    return intake.PostResult(repeat, False, None, repeat.outcome == "woke")
                retry = repeat
            since = (datetime.now(UTC) - timedelta(seconds=intake.RATE_WINDOW_SECONDS)).isoformat(
                timespec="milliseconds"
            )
            refused = None
            if await repo.count_messages_since(tx.conn, src.id, since) >= intake.RATE_LIMIT:
                if retry is not None:
                    return intake.PostResult(retry, False)  # still no room: nothing changes
                refused = intake.RATE_REASON.format(limit=intake.RATE_LIMIT)
            elif secrets_found:
                refused = f"looks like it contains a secret value ({secrets_found[0]})"
            # A secret may sit in any field: a refused message keeps no text, author or thread.
            secret = bool(refused and secrets_found)
            values = {
                "thread": None if secret else thread_id,
                "author": None if secret else who,
                "text": None if refused else body,
                "received_at": received or tx.now,
                "status": "refused" if refused else "new",
                "refused_reason": refused,
            }
            if retry is not None:
                message_id = retry.id
                await repo.update_message(tx.conn, message_id, {**values, "created_at": tx.now})
            else:
                message_id = await repo.insert_message(
                    tx.conn, source_id=src.id, external_id=ext, now=tx.now, **values
                )
            await repo.touch_source(tx.conn, src.id, tx.now)
            await tx.event(
                "intake.received",
                actor,
                message_id=message_id,
                source=slug,
                status="refused" if refused else "new",
                **({"reason": refused} if refused else {}),
            )
            message = await repo.get_message(tx.conn, message_id)
            assert message is not None
            if refused:
                return intake.PostResult(message, True)
            found, route = await self._intake_match(tx.conn, src.id, thread_id, body, task)
            if found is None or route is None:
                return intake.PostResult(message, True)
            woke, reason = await self._tie(tx, message, found, actor, route)
            fresh = await repo.get_message(tx.conn, message_id)
            assert fresh is not None
            return intake.PostResult(fresh, True, found, woke, reason)

    async def _message(self, conn: aiosqlite.Connection, ref: Any) -> IntakeMessage:
        number = _clean_id(ref, "message")
        found = await repo.get_message(conn, number)
        if found is None:
            raise NotFoundError(f"Intake message #{number} not found.")
        return found

    async def intake_message(self, ref: Any) -> IntakeMessage:
        return await self._message(self._r, ref)

    async def intake_tie(self, ref: Any, task: str | int, *, actor: str = "owner") -> PostTie:
        """Tie a message (new, or tied to another task) to a task; the owner's tie on the board
        teaches the thread. Refused and dismissed messages stay where they are."""
        async with self._tx() as tx:
            message = await self._message(tx.conn, ref)
            if message.status in ("refused", "dismissed"):
                raise ValidationError(
                    f"Message #{message.id} is {message.status}; nothing changed."
                )
            target = await self.get_task(task, tx.conn)
            woke, reason = await self._tie(tx, message, target, actor, "tie")
            fresh = await repo.get_message(tx.conn, message.id)
            assert fresh is not None
            return PostTie(fresh, target, woke, reason)

    async def intake_dismiss(self, ref: Any, *, actor: str = "owner") -> IntakeMessage:
        async with self._tx() as tx:
            message = await self._message(tx.conn, ref)
            if message.status != "new":
                raise ValidationError(
                    f"Message #{message.id} is {message.status}; only a new one is dismissed."
                )
            await repo.update_message(tx.conn, message.id, {"status": "dismissed"})
            await tx.event(
                "intake.dismissed", actor, message_id=message.id, source=message.source_slug
            )
            fresh = await repo.get_message(tx.conn, message.id)
            assert fresh is not None
            return fresh

    async def intake_forward(self, ref: Any, *, actor: str = "owner") -> Comment:
        """ "Forward to the agent": the owner sends a tied message to the bound session, framed
        the same way. The owner's own act, so no `intake.wake`."""
        async with self._tx() as tx:
            message = await self._message(tx.conn, ref)
            if message.task_id is None or not message.text:
                raise ValidationError(f"Message #{message.id} is not tied to a task.")
            if message.outcome in ("forwarded", "woke"):
                raise ValidationError(f"Message #{message.id} already reached the agent.")
            task = await self.get_task(message.task_id, tx.conn)
            comment = await self._forward(tx, message, task)
            await repo.update_message(tx.conn, message.id, {"outcome": "forwarded"})
            await tx.event(
                "intake.forwarded",
                actor,
                project_id=task.project_id,
                task_id=task.id,
                project=task.project_slug,
                key=task.key,
                stage=task.stage.value,
                message_id=message.id,
                source=message.source_slug,
                comment_id=comment.id,
            )
            return comment

    async def intake_list(
        self,
        *,
        source: str | None = None,
        status: str | None = None,
        task: str | int | None = None,
        limit: int = 20,
    ) -> list[IntakeMessage]:
        source_id = None
        if source:
            found = await repo.get_source(self._r, _clean_source_slug(source))
            if found is None:
                raise NotFoundError(f"Intake source {source!r} not found.")
            source_id = found.id
        if status and status not in intake.STATUSES:
            raise ValidationError(f"status must be one of {', '.join(intake.STATUSES)}.")
        task_id = (await self.get_task(task)).id if task else None
        return await repo.list_messages(
            self._r,
            source_id=source_id,
            status=status or None,
            task_id=task_id,
            limit=max(1, min(int(limit), intake.LIST_MAX)),
        )

    async def intake_sources(self) -> list[intake.IntakeSource]:
        return await repo.list_sources(self._r)

    async def set_source_trust(
        self, slug: str, may_wake: bool, *, actor: str = "owner"
    ) -> intake.IntakeSource:
        """Board only: whether messages from this source may wake agents."""
        async with self._tx() as tx:
            src = await repo.get_source(tx.conn, _clean_source_slug(slug))
            if src is None:
                raise NotFoundError(f"Intake source {slug!r} not found.")
            if src.may_wake != may_wake:
                await repo.set_source_may_wake(tx.conn, src.id, may_wake)
                kind = "intake.source_trusted" if may_wake else "intake.source_untrusted"
                await tx.event(kind, actor, source=src.slug)
            fresh = await repo.get_source(tx.conn, src.slug)
            assert fresh is not None
            return fresh

    async def set_task_autonomy(self, ref: str | int, value: str, *, actor: str = "owner") -> Task:
        """Board only: the task's autonomy switch (project, on, off)."""
        choice = str(value or "").strip().lower()
        if choice not in intake.AUTONOMY_VALUES:
            raise ValidationError(f"autonomy must be one of {', '.join(intake.AUTONOMY_VALUES)}.")
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if task.autonomy != choice:
                await repo.update_task(tx.conn, task.id, {"autonomy": choice}, tx.now)
                await tx.event(
                    "task.updated",
                    actor,
                    project_id=task.project_id,
                    task_id=task.id,
                    project=task.project_slug,
                    key=task.key,
                    stage=task.stage.value,
                    changes={"autonomy": choice},
                )
            return await self.get_task(task.id, tx.conn)

    async def set_project_autonomy(self, slug: str, on: bool, *, actor: str = "owner") -> Project:
        """Board only: whether the project's agents may continue after a trusted message."""
        async with self._tx() as tx:
            project = await self.get_project(slug, tx.conn)
            if project.autonomy != on:
                await repo.update_project(tx.conn, project.id, {"autonomy": int(on)}, tx.now)
                await tx.event(
                    "project.updated",
                    actor,
                    project_id=project.id,
                    project=project.slug,
                    changes={"autonomy": on},
                )
            return await self.get_project(project.slug, tx.conn)

    async def clear_answer(self, ref: str | int, *, actor: str = "owner") -> Task:
        """The owner clears the "answer received" mark on the card."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if task.answer_received_at:
                await repo.set_answer_mark(tx.conn, task.id, None, None)
                await tx.event(
                    "intake.answer_cleared",
                    actor,
                    project_id=task.project_id,
                    task_id=task.id,
                    project=task.project_slug,
                    key=task.key,
                    stage=task.stage.value,
                )
            return await self.get_task(task.id, tx.conn)

    async def set_archived(self, ref: str | int, archived: bool, *, actor: str = "owner") -> Task:
        """The board's "Archive" / "Back from the archive" on a done card."""
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            await self._set_archived_tx(tx, task, archived, actor, None)
            return await self.get_task(task.id, tx.conn)

    async def set_archived_mcp(
        self,
        ref: str | int,
        archived: bool,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Task:
        """`task_archive` / `task_unarchive`: for coordinator sessions only."""
        tool = "task_archive" if archived else "task_unarchive"
        async with self._tx() as tx:
            if not await self._is_coordinator(tx.conn, session, hook_session):
                raise ValidationError(ARCHIVE_NOT_COORDINATOR.format(tool=tool))
            caller = await self._session(tx, session, session_title, hook_session)
            assert caller is not None
            task = await self.get_task(ref, tx.conn)
            await self._set_archived_tx(tx, task, archived, caller.name, caller.id)
            return await self.get_task(task.id, tx.conn)

    async def _set_archived_tx(
        self, tx: _Tx, task: Task, archived: bool, actor: str, session_id: int | None
    ) -> None:
        """Only a done task is archived. A repeat changes nothing and records nothing. "Back from
        the archive" also takes a task the done window archived: its window starts again now."""
        if task.stage != Stage.DONE:
            raise ValidationError(
                f"Only a done task goes to the archive; {task.key} is in {task.stage.value}."
                " Nothing changed."
            )
        ids = {"project_id": task.project_id, "task_id": task.id, "session_id": session_id}
        payload = {"project": task.project_slug, "key": task.key, "stage": task.stage.value}
        if archived:
            if task.archived_at is not None:
                return
            await repo.set_archive_mark(tx.conn, task.id, tx.now, actor, task.unarchived_at)
            await tx.event("task.archived", actor, **ids, **payload)
            return
        if not is_archived(
            task, datetime.now(UTC), self.done_window_days, self.require_tracker_log
        ):
            return
        await repo.set_archive_mark(tx.conn, task.id, None, None, tx.now)
        await tx.event(
            "task.unarchived",
            actor,
            **ids,
            **payload,
            archived_by=task.archived_by,  # None: the done window had archived it
        )

    async def intake_propose(
        self,
        ref: Any,
        project: str,
        title: str,
        description: str | None = None,
        *,
        actor: str = "mcp",
    ) -> BacklogItem:
        """A backlog item from a message (origin `intake`); "To plan" on the board is the
        confirmation that creates the task. A watcher or agent never creates tasks directly."""
        async with self._tx() as tx:
            message = await self._message(tx.conn, ref)
            if message.status in ("refused", "dismissed", "proposed"):
                raise ValidationError(
                    f"Message #{message.id} is {message.status}; nothing changed."
                )
            proj = await self.get_project(project, tx.conn)
            item = await self._add_backlog(
                tx,
                proj,
                title,
                description if description is not None else message.text,
                None,
                origin="intake",
                created_by=actor,
                intake_message_id=message.id,
            )
            await repo.update_message(tx.conn, message.id, {"status": "proposed"})
            await tx.event(
                "intake.proposed",
                actor,
                project_id=proj.id,
                project=proj.slug,
                message_id=message.id,
                source=message.source_slug,
                backlog_id=item.id,
            )
            return item

    # --- backlog -----------------------------------------------------------------------------

    async def _add_backlog(
        self,
        tx: _Tx,
        project: Project,
        title: Any,
        body: Any,
        area: Any,
        *,
        origin: str,
        created_by: str,
        origin_task_id: int | None = None,
        origin_session_id: int | None = None,
        intake_message_id: int | None = None,
        priority: Any = 0,
        assignee: Any = None,
    ) -> BacklogItem:
        clean_title = _clean_text(title, "title", backlog.TITLE_MAX)
        if not clean_title:
            raise ValidationError("title must not be empty.")
        clean_body = _clean_text(body, "body", backlog.BODY_MAX)
        clean_area = _clean_text(area, "area", backlog.AREA_MAX)
        clean_assignee = _clean_text(assignee, "assignee", backlog.ASSIGNEE_MAX)
        rules = await self._secret_rules(tx.conn, project)
        _scan(rules, title=clean_title, body=clean_body, area=clean_area, assignee=clean_assignee)
        item_id = await repo.insert_backlog_item(
            tx.conn,
            {
                "project_id": project.id,
                "title": clean_title,
                "body_md": clean_body,
                "area": clean_area.lower() if clean_area else None,
                "priority": self._backlog_priority(priority),
                "assignee": clean_assignee,
                "origin": origin,
                "origin_task_id": origin_task_id,
                "origin_session_id": origin_session_id,
                "intake_message_id": intake_message_id,
                "created_at": tx.now,
                "created_by": created_by,
            },
        )
        await tx.event(
            "backlog.added",
            created_by,
            project_id=project.id,
            task_id=origin_task_id,
            session_id=origin_session_id,
            project=project.slug,
            backlog_id=item_id,
            title=clean_title,
            origin=origin,
        )
        item = await repo.get_backlog_item(tx.conn, item_id)
        assert item is not None
        return item

    async def add_backlog_item(
        self,
        project: str,
        title: str,
        *,
        body: str | None = None,
        area: str | None = None,
        task: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
        by_owner: bool = False,
        priority: Any = 0,
        assignee: str | None = None,
    ) -> BacklogItem:
        """A finding for later: from a session (origin `session`, with the task it came from
        when `task` is passed) or written by the owner on the board (origin `owner`)."""
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            origin_task = await self.get_task(task, tx.conn) if task else None
            sess = (
                None if by_owner else await self._session(tx, session, session_title, hook_session)
            )
            created_by = "owner" if by_owner else (sess.name if sess else "mcp")
            return await self._add_backlog(
                tx,
                proj,
                title,
                body,
                area,
                origin="owner" if by_owner else "session",
                created_by=created_by,
                origin_task_id=origin_task.id if origin_task else None,
                origin_session_id=sess.id if sess else None,
                priority=priority,
                assignee=assignee,
            )

    async def backlog_item(self, ref: Any) -> BacklogItem:
        return await self._backlog(self._r, ref)

    async def _backlog(self, conn: aiosqlite.Connection, ref: Any) -> BacklogItem:
        number = _clean_id(ref, "item")
        found = await repo.get_backlog_item(conn, number)
        if found is None:
            raise NotFoundError(f"Backlog item #{number} not found.")
        return found

    async def list_backlog(
        self,
        *,
        project: str | None = None,
        status: str | None = None,
        area: str | None = None,
        assignee: str | None = None,
        limit: int = 200,
    ) -> list[BacklogItem]:
        """`assignee` "-" means items that are for nobody."""
        project_id = (await self.get_project(project)).id if project else None
        if status and status not in backlog.STATUSES:
            raise ValidationError(f"status must be one of {', '.join(backlog.STATUSES)}.")
        return await repo.list_backlog(
            self._r,
            project_id=project_id,
            status=status or None,
            area=_clean_text(area, "area", backlog.AREA_MAX),
            assignee=_clean_text(assignee, "assignee", backlog.ASSIGNEE_MAX),
            limit=max(1, min(int(limit), backlog.LIST_MAX)),
        )

    async def all_backlog(self) -> list[BacklogItem]:
        """Every item, uncapped: export and import must never miss one."""
        return await repo.list_backlog(self._r, limit=-1)  # SQLite: a negative limit is none

    async def backlog_areas(self, project: str | None = None) -> list[str]:
        project_id = (await self.get_project(project)).id if project else None
        return await repo.backlog_areas(self._r, project_id)

    async def backlog_assignees(self, project: str | None = None) -> list[str]:
        project_id = (await self.get_project(project)).id if project else None
        return await repo.backlog_assignees(self._r, project_id)

    async def backlog_counts(self) -> dict[str, int]:
        """New backlog items per project slug (the sidebar)."""
        counts = await repo.backlog_new_counts(self._r)
        projects = await repo.list_projects(self._r)
        return {p.slug: counts[p.id] for p in projects if counts.get(p.id)}

    async def _set_backlog_status(
        self,
        tx: _Tx,
        item: BacklogItem,
        status: str,
        reason: str | None,
        actor: str,
        link: str | None = None,
    ) -> None:
        changes: dict[str, Any] = {
            "status": status,
            "decline_reason": reason if status in ("declined", "done") else None,
            "link": link if status == "done" else None,
            "decided_at": tx.now,
            "decided_by": actor,
        }
        await repo.update_backlog_item(tx.conn, item.id, changes)
        await tx.event(
            "backlog.updated",
            actor,
            project_id=item.project_id,
            project=item.project_slug,
            backlog_id=item.id,
            title=item.title,
            status=status,
            **({"reason": reason} if reason else {}),
            **({"link": changes["link"]} if changes["link"] else {}),
        )

    @staticmethod
    def _backlog_link(value: Any) -> str | None:
        """A done item's link: where the fix is, an http(s) link such as a pull request."""
        link = _clean_url(value, "link")
        if link is not None and (
            len(link) > backlog.LINK_MAX or not backlog.LINK_RE.fullmatch(link)
        ):
            raise ValidationError(
                f"link must be an http(s) link of at most {backlog.LINK_MAX} characters."
            )
        return link

    @staticmethod
    def _backlog_priority(value: Any) -> int:
        try:
            return backlog.parse_priority(value)
        except ValueError as exc:
            raise ValidationError(f"{exc}.") from exc

    async def update_backlog_item(
        self,
        ref: Any,
        *,
        status: str | None = None,
        reason: str | None = None,
        link: str | None = None,
        title: str | None = None,
        body: str | None = None,
        area: str | None = None,
        priority: Any = None,
        assignee: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> BacklogItem:
        """MCP: a status change (new, later, done with an optional link, declined with a reason)
        only for a configured coordinator; `planned` never (only "To plan" on the board creates
        the task). Anyone else may edit title, body, area, priority and assignee of an item that
        is still new and that the same session added."""
        async with self._tx() as tx:
            item = await self._backlog(tx.conn, ref)
            coordinator = await self._is_coordinator(tx.conn, session, hook_session)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "mcp"
            edits = {
                "title": title,
                "body": body,
                "area": area,
                "priority": priority,
                "assignee": assignee,
            }
            edits = {k: v for k, v in edits.items() if v is not None}
            if status is not None:
                wanted = status.strip().lower()
                if wanted == "planned":
                    raise ValidationError(
                        "planned is set only by 'To plan' on the board, which creates the task."
                        " Nothing changed."
                    )
                if wanted not in backlog.MCP_STATUSES:
                    raise ValidationError(
                        f"status must be one of {', '.join(backlog.MCP_STATUSES)}."
                    )
                if not coordinator:
                    raise ValidationError(
                        "Only a coordinator session named in the server's config changes a"
                        " backlog item's status; the owner decides on the board. Nothing changed."
                    )
                if item.status == "planned":
                    raise ValidationError(f"Backlog item #{item.id} is planned as {item.task_key}.")
            if link is not None and (status or "").strip().lower() != "done":
                raise ValidationError("link goes with status done. Nothing changed.")
            if edits and not coordinator:
                own = sess is not None and item.origin_session_id == sess.id
                if item.status != "new" or not own:
                    raise ValidationError(
                        "Only the session that added a backlog item edits it, and only while it is"
                        " new. Nothing changed."
                    )
            if not edits and status is None:
                raise ValidationError("Pass status, title, body, area, priority or assignee.")
            if edits:
                changes: dict[str, Any] = {}
                if "title" in edits:
                    clean = _clean_text(edits["title"], "title", backlog.TITLE_MAX)
                    if not clean:
                        raise ValidationError("title must not be empty.")
                    changes["title"] = clean
                if "body" in edits:
                    changes["body_md"] = _clean_text(edits["body"], "body", backlog.BODY_MAX)
                if "area" in edits:
                    clean_area = _clean_text(edits["area"], "area", backlog.AREA_MAX)
                    changes["area"] = clean_area.lower() if clean_area else None
                if "priority" in edits:
                    changes["priority"] = self._backlog_priority(edits["priority"])
                if "assignee" in edits:
                    changes["assignee"] = _clean_text(
                        edits["assignee"], "assignee", backlog.ASSIGNEE_MAX
                    )
                project = await self.get_project(item.project_slug, tx.conn)
                _scan(
                    await self._secret_rules(tx.conn, project),
                    title=changes.get("title"),
                    body=changes.get("body_md"),
                    area=changes.get("area"),
                    assignee=changes.get("assignee"),
                )
                await repo.update_backlog_item(tx.conn, item.id, changes)
                await tx.event(
                    "backlog.updated",
                    actor,
                    project_id=item.project_id,
                    project=item.project_slug,
                    backlog_id=item.id,
                    title=changes.get("title", item.title),
                    changes=sorted(changes),
                )
            if status is not None:
                wanted = status.strip().lower()
                why = _clean_text(reason, "reason", backlog.REASON_MAX)
                if wanted == "declined" and not why:
                    raise ValidationError("A declined item needs a reason.")
                if why:
                    envs.check_secret("reason", why)
                where = self._backlog_link(link) if wanted == "done" else None
                if where:
                    envs.check_secret("link", where)
                await self._set_backlog_status(tx, item, wanted, why, actor, where)
            return await self._backlog(tx.conn, item.id)

    async def set_backlog_queue(
        self, ref: Any, priority: Any, assignee: Any, *, actor: str = "owner"
    ) -> BacklogItem:
        """Board: an open item's priority and who it is for."""
        async with self._tx() as tx:
            item = await self._backlog(tx.conn, ref)
            if item.status == "planned":
                raise ValidationError(f"Backlog item #{item.id} is planned as {item.task_key}.")
            wanted = {
                "priority": self._backlog_priority(priority),
                "assignee": _clean_text(assignee, "assignee", backlog.ASSIGNEE_MAX),
            }
            changes = {k: v for k, v in wanted.items() if v != getattr(item, k)}
            if changes:
                project = await self.get_project(item.project_slug, tx.conn)
                _scan(await self._secret_rules(tx.conn, project), assignee=changes.get("assignee"))
                await repo.update_backlog_item(tx.conn, item.id, changes)
                await tx.event(
                    "backlog.updated",
                    actor,
                    project_id=item.project_id,
                    project=item.project_slug,
                    backlog_id=item.id,
                    title=item.title,
                    changes=sorted(changes),
                )
            return await self._backlog(tx.conn, item.id)

    async def import_backlog_item(
        self, project: str, values: dict[str, Any], day: str, *, actor: str = "import"
    ) -> BacklogItem:
        """A backlog.md row: its date and decision as written; `planned` records history only,
        no task is created."""
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            item = await self._add_backlog(
                tx,
                proj,
                values["title"],
                None,
                values.get("area"),
                origin=values["origin"],
                created_by=actor,
                priority=values["priority"],
                assignee=values["assignee"],
            )
            changes: dict[str, Any] = {"created_at": f"{day}T00:00:00.000+00:00"}
            if values["status"] != "new":
                changes |= {
                    "status": values["status"],
                    "decline_reason": values.get("reason"),
                    "link": values.get("link"),
                    "decided_at": tx.now,
                    "decided_by": actor,
                }
            await repo.update_backlog_item(tx.conn, item.id, changes)
            return await self._backlog(tx.conn, item.id)

    async def import_backlog_update(
        self, item_id: int, values: dict[str, Any], *, actor: str = "import"
    ) -> BacklogItem:
        async with self._tx() as tx:
            item = await self._backlog(tx.conn, item_id)
            project = await self.get_project(item.project_slug, tx.conn)
            _scan(
                await self._secret_rules(tx.conn, project),
                title=values["title"],
                assignee=values["assignee"],
            )
            changes = {
                "title": values["title"],
                "area": values.get("area"),
                "origin": values["origin"],
                "status": values["status"],
                "decline_reason": values.get("reason"),
                "link": values.get("link"),
                "priority": values["priority"],
                "assignee": values["assignee"],
            }
            if values["status"] == "done" == item.status:
                changes["decline_reason"] = item.decline_reason  # the note is not in backlog.md
            await repo.update_backlog_item(tx.conn, item.id, changes)
            await tx.event(
                "backlog.updated",
                actor,
                project_id=item.project_id,
                project=item.project_slug,
                backlog_id=item.id,
                title=values["title"],
                status=values["status"],
            )
            return await self._backlog(tx.conn, item.id)

    async def decide_backlog(
        self,
        ref: Any,
        decision: str,
        reason: str | None = None,
        *,
        link: str | None = None,
        actor: str = "owner",
    ) -> BacklogItem:
        """Board: "To plan" (creates the task in plan, with the item's title, body and priority,
        the line "From backlog #N" and its assignee as waiting_on), "Later", "Done" (fixed without
        a task, with an optional link), "Not doing" (needs a reason) or back to new."""
        choice = str(decision or "").strip().lower()
        if choice not in ("plan", "later", "done", "decline", "new"):
            raise ValidationError("decision must be plan, later, done, decline or new.")
        async with self._tx() as tx:
            item = await self._backlog(tx.conn, ref)
            if item.status == "planned":
                raise ValidationError(f"Backlog item #{item.id} is planned as {item.task_key}.")
            if choice == "plan":
                await self._plan_backlog(tx, item, actor)
            else:
                why = _clean_text(reason, "reason", backlog.REASON_MAX)
                if choice == "decline" and not why:
                    raise ValidationError("Not doing needs a reason.")
                where = self._backlog_link(link) if choice == "done" else None
                if why or where:
                    project = await self.get_project(item.project_slug, tx.conn)
                    _scan(await self._secret_rules(tx.conn, project), reason=why, link=where)
                status = {"later": "later", "done": "done", "decline": "declined", "new": "new"}
                await self._set_backlog_status(tx, item, status[choice], why, actor, where)
            return await self._backlog(tx.conn, item.id)

    async def _plan_backlog(self, tx: _Tx, item: BacklogItem, actor: str) -> Task:
        project = await self.get_project(item.project_slug, tx.conn)
        line = backlog.FROM_BACKLOG.format(id=item.id)
        body = item.body_md
        if item.origin == "intake":
            # External text never becomes the owner's markdown. Quoting can grow it, so the raw
            # text is cut until the quoted block fits whole.
            raw = body or ""
            body = intake.external_description(raw)
            while len(body) + len(line) + 2 > MARKDOWN_MAX:
                raw = raw[: len(raw) * 9 // 10]
                body = intake.external_description(raw)
        description = f"{body}\n\n{line}" if body else line
        if len(description) > MARKDOWN_MAX:
            description = description[: MARKDOWN_MAX - len(line) - 3] + "…\n\n" + line
        key = f"{project.slug}-{await repo.next_task_number(tx.conn, project.id)}"
        fields = {
            "title": item.title,
            "stage": Stage.PLAN.value,
            "description": description,
            "priority": item.priority,
            "waiting_on": item.assignee,
        }
        task_id = await repo.insert_task(tx.conn, project.id, key, fields, None, tx.now)
        await tx.event(
            "task.created",
            actor,
            project_id=project.id,
            task_id=task_id,
            project=project.slug,
            key=key,
            title=item.title,
            stage=Stage.PLAN.value,
        )
        await repo.update_backlog_item(
            tx.conn,
            item.id,
            {"status": "planned", "task_id": task_id, "decided_at": tx.now, "decided_by": actor},
        )
        if item.intake_message_id is not None:
            message = await repo.get_message(tx.conn, item.intake_message_id)
            if message is not None:
                await repo.update_message(
                    tx.conn, message.id, {"status": "tied", "task_id": task_id}
                )
                if message.thread:
                    await repo.set_thread(
                        tx.conn, message.source_id, message.thread, task_id, tx.now
                    )
        await tx.event(
            "backlog.planned",
            actor,
            project_id=project.id,
            task_id=task_id,
            project=project.slug,
            key=key,
            stage=Stage.PLAN.value,
            backlog_id=item.id,
            title=item.title,
        )
        return await self.get_task(task_id, tx.conn)

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
            out.append((rev, title_diff(before, rev.titles, rev.added)))
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
                    restart |= {"added_after_acceptance_at": None, "added_by": None}
                    restart |= {"added_reason": None}
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
            if current.status == "done" and status == "in_progress":
                await self._rework(tx, task, f"rework: {current.title}", actor, sess)
            steps = await repo.list_plan_steps(tx.conn, task.id)
            closes = status == "done" and current.status != "done"
            ready = task.stage == Stage.DEV and task.review_total > 0
            if closes and ready and all(s.status == "done" for s in steps):
                _note(ALL_DONE_REVIEW_HINT)
            updated = next(s for s in steps if s.id == current.id)
            return await self.get_task(task.id, tx.conn), updated

    async def _rework(
        self, tx: _Tx, task: Task, note: str, actor: str, sess: Session | None
    ) -> bool:
        """The stage follows the plan: new or reopened work on a task in review or test moves it
        back to dev in the caller's transaction. No rule check: the plan's own write decides."""
        if task.stage not in ACCEPTANCE_STAGES:
            return False
        note = _clean_text(note, "stage_note") or "rework"
        rank = await repo.top_rank(tx.conn, task.project_id, Stage.DEV.value)
        await repo.set_rank(tx.conn, task.id, rank)
        await repo.update_task(tx.conn, task.id, {"stage": Stage.DEV, "stage_note": note}, tx.now)
        await tx.event(
            "task.stage_changed",
            actor,
            project_id=task.project_id,
            task_id=task.id,
            session_id=sess.id if sess else None,
            project=task.project_slug,
            key=task.key,
            stage=Stage.DEV.value,
            title=task.title,
            **{"from": task.stage.value, "to": Stage.DEV.value},
            note=note,
        )
        _note(REWORK_NOTE.format(key=task.key, stage=task.stage.value, note=note))
        return True

    async def add_plan_step(
        self,
        ref: str | int,
        title: Any,
        *,
        details: str | None = None,
        reason: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Task, PlanStep]:
        """Append a pending step. Unlike `set_plan`, an accepted plan stays accepted: the step is
        marked as added after acceptance, by whom and why (`reason` is required). On a task in
        review or test the stage goes back to dev with the note 'rework: <reason>'."""
        clean_title = clean_plan_title(title)
        clean_details = clean_markdown(details, "details", PLAN_DETAILS_MAX)
        why = _clean_text(reason, "reason", REASON_MAX)
        if not why:
            raise ValidationError(
                "plan_step_add needs a reason: one line on why the step is added (e.g. the"
                " owner's decision after review)."
            )
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if task.stage == Stage.DONE:
                raise ValidationError(
                    f"{task.key}: the task is done: reopen it (task_set_stage) before adding steps."
                )
            current = await repo.list_plan_steps(tx.conn, task.id)
            if len(current) >= PLAN_STEPS_MAX:
                raise ValidationError(f"A plan has at most {PLAN_STEPS_MAX} steps.")
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            accepted = bool(current) and task.plan_status == "accepted"
            position = len(current) + 1
            await repo.insert_plan_step(
                tx.conn,
                task.id,
                position,
                clean_title,
                clean_details,
                actor,
                tx.now,
                added_reason=why if accepted else None,
            )
            if not current:
                await repo.set_plan_status(tx.conn, task.id, "draft", None, None)
            label = f"step {position} added" + (" after acceptance" if accepted else "")
            await self._plan_revision(tx, task, actor, f"{label}: {why}")
            moved = await self._rework(tx, task, f"rework: {why}", actor, sess)
            if task.stage == Stage.LIVE:
                _note(f"{task.key} is in live: the step does not move it.")
            await self._plan_event(
                tx,
                "plan.step_added",
                await self.get_task(task.id, tx.conn),
                actor,
                sess.id if sess else None,
                position=position,
                title=clean_title,
                reason=why,
                after_acceptance=accepted,
                moved_to_dev=moved,
            )
            steps = await repo.list_plan_steps(tx.conn, task.id)
            return await self.get_task(task.id, tx.conn), steps[-1]

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

    async def _record_todo(self, cc_id: str, change: TodoChange, since: str | None = None) -> None:
        """Apply a todo tool call to the session's list. A new Claude Code id starts a fresh
        list; an update of an item it never saw is ignored. The session is looked up again in
        this transaction: an agent's write may have just moved the id to another session. A
        replay from the forwarder's spool (`since`: when it happened) changes only items not
        written since."""
        async with self._tx() as tx:
            session = await repo.get_session_by_cc(tx.conn, cc_id)
            if session is None:
                return
            await repo.clear_session_todo(tx.conn, session.id, keep_cc=cc_id)
            before = await repo.list_session_todo(tx.conn, session.id, cc_id)
            by_id = {i.item_id: i for i in before}
            if since is not None:
                touched = before if change.kind == "replace" else [by_id.get(change.item_id or "")]
                if any(i is not None and i.updated_at >= since for i in touched):
                    return
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

    # --- rule versions -------------------------------------------------------------------------

    def _set_global_rules(self, body: dict[str, Any], number: int) -> None:
        self.global_rules = Rules(
            body,
            mode=self.rules_mode,
            global_version=number,
            require_tracker_log=self.config_rules.require_tracker_log,
        )
        if self.rules_changed is None:
            return
        # Runs after a commit, or during the start: it must fail neither.
        try:
            self.rules_changed()
        except Exception as exc:
            logger.warning("Could not refresh what follows the global rules: %r.", exc)

    @staticmethod
    def _scope_name(project: Project | None) -> str:
        """`global`, or a project's slug; a project called `global` is `project:global`."""
        if project is None:
            return "global"
        return f"project:{project.slug}" if project.slug == "global" else project.slug

    async def _valid_rule_body(
        self, conn: aiosqlite.Connection, project: Project | None
    ) -> tuple[dict[str, Any] | None, int, list[str]]:
        """The scope's newest version that validates (None: no such version) and its number, and
        what is wrong with the newer ones. A version edited by hand never stops the server."""
        rule_set = await repo.get_rule_set(conn, project.id if project else None)
        problems: list[str] = []
        if rule_set is None:
            return None, 0, problems
        for version in await repo.list_rule_versions(conn, rule_set.id):
            source = f"rule_version #{version.id} ({version.scope} v{version.number})"
            try:
                if version.body_error:
                    raise ValueError(f"{source}: {version.body_error}")
                if project is None:
                    return stored_global_rules(version.body, source), version.number, problems
                return validate_rules(version.body, source), version.number, problems
            except ValueError as exc:
                problems.append(str(exc))
        return None, 0, problems

    async def _rule_body(
        self, conn: aiosqlite.Connection, project: Project | None
    ) -> dict[str, Any]:
        """What a patch merges on and a diff compares with: the newest valid version; without
        one, the config for the global scope and an empty override for a project."""
        body, _, _ = await self._valid_rule_body(conn, project)
        if body is not None:
            return body
        return self.config_rules.body if project is None else {}

    async def rule_version_error(self, scope: str) -> str | None:
        """Why the scope's newest versions are not in force, for Settings → Rules."""
        _, project = await self._rules_scope(self._r, scope)
        body, number, problems = await self._valid_rule_body(self._r, project)
        if not problems:
            return None
        fallback = f"v{number} is in force" if body is not None else "the config is in force"
        if body is None and project is not None:
            fallback = "the project_rules row is in force"
        return f"{' '.join(problems)} Ignored: {fallback}."

    async def _new_rule_version(
        self,
        tx: _Tx,
        project: Project | None,
        body: dict[str, Any],
        *,
        author: str,
        reason: str,
        session_id: int | None = None,
        announce: bool = True,
        notice: bool = False,
    ) -> int:
        """Store `body` as the next version of the scope. A project's version also replaces its
        `project_rules` row, the one its rules are read from; the caller of a global version
        calls `_set_global_rules` after the commit."""
        scope = self._scope_name(project)
        found = await repo.get_rule_set(tx.conn, project.id if project else None)
        rule_set = found or await repo.insert_rule_set(
            tx.conn, scope, project.id if project else None, tx.now
        )
        previous = await self._rule_body(tx.conn, project) if found else {}
        if project is None:
            rendered = Rules(body, mode=self.rules_mode)
        else:
            rendered = Rules(merge_project_rules(self.global_rules.body, body), project.slug)
        number = await repo.insert_rule_version(
            tx.conn,
            rule_set,
            body,
            rules_markdown(rendered),
            author,
            session_id,
            reason,
            rules_diff(previous, body),
            tx.now,
            notice=notice,
        )
        if project is not None:
            text = json.dumps(body, ensure_ascii=False)
            await repo.set_project_rules(tx.conn, project.id, text, tx.now)
        if announce:
            await tx.event(
                "rules.updated",
                author,
                project_id=project.id if project else None,
                session_id=session_id,
                scope=scope,
                **({"project": project.slug} if project else {}),
                version=number,
                reason=reason,
            )
        return number

    async def _sync_rule_versions(self) -> None:
        """Make the database agree with `config.toml` and the `project_rules` rows.

        The first start makes each of them version 1. Later a `[rules]` table whose hash differs
        from the one last turned into a version becomes a new global version (author `config`),
        replacing what earlier versions changed; an unchanged table leaves accepted versions in
        force, also when a release changes the built-in defaults. A `project_rules` row edited by
        hand becomes a new project version the same way. Versions that do not validate are logged
        and skipped.
        """
        config_body = self.config_rules.body
        async with self._tx() as tx:
            rule_set = await repo.get_rule_set(tx.conn, None)
            if rule_set is None:
                number = await self._new_rule_version(
                    tx,
                    None,
                    config_body,
                    author="config",
                    reason="The rules at the first start with versions.",
                    announce=False,
                )
                body = config_body
            else:
                found, number, problems = await self._valid_rule_body(tx.conn, None)
                for problem in problems:
                    logger.error("%s Ignored: the newest valid rules are in force.", problem)
                body = found if found is not None else config_body
                if found is None:
                    number = rule_set.current_version
                changed = rule_set.config_hash != self._config_table_hash
                if changed and (found is None or body != config_body):
                    number = await self._new_rule_version(
                        tx,
                        None,
                        config_body,
                        author="config",
                        reason="config.toml [rules] changed.",
                    )
                    body = config_body
                    logger.warning(
                        "The [rules] in config.toml changed: they are now global rules version %d.",
                        number,
                    )
            stored = await repo.get_rule_set(tx.conn, None)
            assert stored is not None
            await repo.set_rules_config_hash(tx.conn, stored.id, self._config_table_hash)
            self._set_global_rules(body, number)
            await self._sync_project_rule_versions(tx)

    async def _sync_project_rule_versions(self, tx: _Tx) -> None:
        rows = dict(await repo.list_project_rules(tx.conn))
        for slug, text in rows.items():
            try:
                override = _project_override(slug, text)
            except ValueError:
                continue  # logged by _merged_rules; not a version, the history stays as it was
            project = await self.get_project(slug, tx.conn)
            first = await repo.get_rule_set(tx.conn, project.id) is None
            if not first and await self._rule_body(tx.conn, project) == override:
                continue
            await self._new_rule_version(
                tx,
                project,
                override,
                author="project_rules",
                reason=(
                    "The project's rules at the first start with versions."
                    if first
                    else "The project_rules row differs from the last version (edited by hand)."
                ),
                announce=not first,
            )
        for rule_set in await repo.list_project_rule_sets(tx.conn):
            project = await repo.get_project_by_id(tx.conn, rule_set.project_id)
            assert project is not None
            _, _, problems = await self._valid_rule_body(tx.conn, project)
            for problem in problems:
                logger.error("%s Ignored: the project_rules row is in force.", problem)
            if project.slug not in rows and await self._rule_body(tx.conn, project):
                await self._new_rule_version(
                    tx,
                    project,
                    {},
                    author="project_rules",
                    reason="The project_rules row was removed by hand.",
                )

    async def _is_coordinator(
        self, conn: aiosqlite.Connection, value: str | None, hook_session: str | None
    ) -> bool:
        """Is the caller an entry of `coordinators`? Asked before the call's own `hook_session`
        is linked, or it would prove itself. A desktop id needs that session's exact hook id,
        which no tool prints; a name or an alias only has to be passed, so it guards against
        mistakes, it is not access control: all clients share one token."""
        needle = (value or "").strip()
        if not needle:
            return False
        session = await self._known_session(needle, conn)
        cc_id = (hook_session or "").strip()
        for entry in self.coordinators:
            desktop = parse_session_id(entry)
            if desktop is not None:
                if (
                    session is not None
                    and session.external_id == desktop
                    and session.cc_exact
                    and cc_id
                    and session.cc_session_id == cc_id
                ):
                    return True
                continue
            if needle == entry or (session is not None and session.name == entry):
                return True
            aliased = await repo.get_session_by_alias(conn, entry)
            if aliased is not None and session is not None and aliased.id == session.id:
                return True
        return False

    async def _rules_scope(
        self, conn: aiosqlite.Connection, scope: str
    ) -> tuple[str, Project | None]:
        """`global` is always the global rules; `project:<slug>` names a project, the only way
        to reach one called `global`."""
        text = str(scope or "").strip()
        if text.lower() == "global":
            return "global", None
        slug = text.removeprefix("project:")
        if not slug or "/" in slug:
            raise ValidationError(f"scope must be 'global' or a project slug, got {scope!r}.")
        project = await self.get_project(slug, conn)
        return self._scope_name(project), project

    async def rules_of_scope(self, scope: str) -> Rules:
        _, project = await self._rules_scope(self._r, scope)
        return self.global_rules if project is None else await self._rules(self._r, project)

    async def rule_scopes(self) -> list[str]:
        projects = [self._scope_name(item.project) for item in await self.list_projects()]
        return ["global", *projects]

    async def _patched_rules(
        self, conn: aiosqlite.Connection, scope: str, patch: Any
    ) -> tuple[str, Project | None, dict[str, Any]]:
        """The scope's next document: `patch` merged on top of the current version and validated
        as a whole, with its free text scanned for secrets."""
        canonical, project = await self._rules_scope(conn, scope)
        if not isinstance(patch, dict) or not patch:
            raise ValidationError(
                "patch must be a non-empty object merged into the rules, e.g."
                " {'stage_requirements': {'test': []}}."
            )
        current = await self._rule_body(conn, project)
        source = f"rules patch for {canonical}"
        try:
            check_patch_keys(patch, source)
            clean = validate_rules(apply_patch(current, patch), source)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        body = clean if project is not None else merge_rules(DEFAULT_RULES, clean)
        if body == current:
            raise ValidationError(f"This patch changes nothing in the {canonical} rules.")
        # Only what this patch changes: text already in force is not the proposer's to answer for.
        texts = {
            "guidance": body.get("guidance"),
            "description_template": body.get("description_template"),
            "first_line": body.get("report_format", {}).get("first_line"),
        }
        before = {**current, **current.get("report_format", {})}
        _scan(
            await self._secret_rules(conn, project),
            **{name: text for name, text in texts.items() if text != before.get(name)},
        )
        return canonical, project, body

    @staticmethod
    def _rule_reason(reason: Any) -> str:
        text = _clean_text(reason, "reason", envs.REASON_MAX)
        if not text:
            raise ValidationError("reason must say why the change is needed.")
        envs.check_secret("reason", text)
        return text

    def _not_a_coordinator(self) -> ValidationError:
        if self.coordinators:
            return ValidationError(
                "rules_update is only for the coordinator sessions named in the server's config;"
                " a desktop id also needs that session's own hook_session. Nothing changed. Use"
                " rules_propose: the owner accepts it on the board."
            )
        return ValidationError(
            "No coordinator is configured, so every change of the rules is a proposal; nothing"
            " changed. Use rules_propose: the owner accepts it on the board."
        )

    async def update_rules(
        self,
        scope: str,
        patch: Any,
        reason: str,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[str, int]:
        """A new version of the scope's rules, for a coordinator session; returns the canonical
        scope and the version. The owner sees it in the Inbox until it is marked read."""
        async with self._tx() as tx:
            if not await self._is_coordinator(tx.conn, session, hook_session):
                raise self._not_a_coordinator()
            sess = await self._session(tx, session, session_title, hook_session)
            assert sess is not None
            text = self._rule_reason(reason)
            canonical, project, body = await self._patched_rules(tx.conn, scope, patch)
            number = await self._new_rule_version(
                tx,
                project,
                body,
                author=sess.name,
                reason=text,
                session_id=sess.id,
                notice=True,
            )
            # The caller knows: no "rules updated" line on its own change.
            await repo.set_rule_seen(tx.conn, sess.id, project.id if project else 0, number)
        if project is None:
            self._set_global_rules(body, number)
        return canonical, number

    async def close_rule_notice(self, version_id: int, *, actor: str = "board") -> None:
        """The owner has read about a direct `rules_update`. Board only."""
        async with self._tx() as tx:
            version = await repo.get_rule_version_by_id(tx.conn, version_id)
            if version is None:
                raise NotFoundError(f"Rule version #{version_id} not found.")
            if version.notice_open:
                await repo.close_rule_notice(tx.conn, version_id)
                await tx.event(
                    "rules.notice_read", actor, scope=version.scope, version=version.number
                )

    async def propose_rules(
        self,
        scope: str,
        patch: Any,
        reason: str,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> RuleProposal:
        """An agent's proposed change of the rules: recorded for the owner, nothing else changes.
        Validated now and again when it is accepted."""
        text = self._rule_reason(reason)
        async with self._tx() as tx:
            canonical, project, _ = await self._patched_rules(tx.conn, scope, patch)
            sess = await self._session(tx, session, session_title, hook_session)
            actor = sess.name if sess else "agent"
            proposal_id = await repo.insert_rule_proposal(
                tx.conn,
                canonical,
                project.id if project else None,
                patch,
                text,
                actor,
                sess.id if sess else None,
                tx.now,
            )
            await tx.event(
                "rules.proposed",
                actor,
                project_id=project.id if project else None,
                session_id=sess.id if sess else None,
                scope=canonical,
                **({"project": project.slug} if project else {}),
                proposal_id=proposal_id,
                change=", ".join(_patch_summary(patch)),
            )
            proposal = await repo.get_rule_proposal(tx.conn, proposal_id)
            assert proposal is not None
            return proposal

    async def decide_rule_proposal(
        self, proposal_id: int, accept: bool, *, actor: str = "board"
    ) -> RuleProposal:
        """Accept (a new version made from the patch on top of the current version, checked
        again here) or reject a proposal; a rejected one stays in the history. Board only."""
        body, number, project = None, 0, None
        async with self._tx() as tx:
            proposal = await repo.get_rule_proposal(tx.conn, proposal_id)
            if proposal is None:
                raise NotFoundError(f"Rules proposal #{proposal_id} not found.")
            if proposal.status != "open":
                raise ConflictError(f"Rules proposal #{proposal_id} is already {proposal.status}.")
            version = None
            if accept:
                _, project, body = await self._patched_rules(
                    tx.conn, proposal.scope, proposal.patch
                )
                number = version = await self._new_rule_version(
                    tx,
                    project,
                    body,
                    author=f"{proposal.proposed_by}, accepted by {actor}",
                    reason=proposal.reason,
                    session_id=proposal.session_id,
                )
            status = "accepted" if accept else "rejected"
            await repo.decide_rule_proposal(tx.conn, proposal.id, status, version, actor, tx.now)
            await tx.event(
                f"rules.proposal_{status}",
                actor,
                project_id=proposal.project_id,
                scope=proposal.scope,
                proposal_id=proposal.id,
                change=", ".join(_patch_summary(proposal.patch)),
            )
            decided = await repo.get_rule_proposal(tx.conn, proposal.id)
            assert decided is not None
        if body is not None and project is None:
            self._set_global_rules(body, number)
        return decided

    async def rollback_rules(
        self, scope: str, number: int, *, confirmed: bool = False, actor: str = "board"
    ) -> tuple[str, int]:
        """A new version with the body of version `number`; history is never rewritten. Board
        only, and it asks for a confirmation first."""
        async with self._tx() as tx:
            canonical, project = await self._rules_scope(tx.conn, scope)
            rule_set = await repo.get_rule_set(tx.conn, project.id if project else None)
            target = await repo.get_rule_version(tx.conn, rule_set.id, number) if rule_set else None
            if target is None:
                raise NotFoundError(f"The {canonical} rules have no version {number}.")
            source = f"rule_version #{target.id} ({canonical} v{number})"
            try:
                if target.body_error:
                    raise ValueError(f"{source}: {target.body_error}")
                body = (
                    validate_rules(target.body, source)
                    if project
                    else stored_global_rules(target.body, source)
                )
            except ValueError as exc:
                raise ValidationError(f"Cannot roll back to a broken version. {exc}") from None
            if body == await self._rule_body(tx.conn, project):
                raise ConflictError(f"The {canonical} rules already equal version {number}.")
            if not confirmed:
                raise ConfirmationNeeded(
                    f"Roll the {canonical} rules back to v{number}? A new version with the same"
                    " rules is created; the history stays."
                )
            made = await self._new_rule_version(
                tx, project, body, author=actor, reason=f"Rolled back to v{number}."
            )
        if project is None:
            self._set_global_rules(body, made)
        return canonical, made

    async def rule_proposal(self, proposal_id: int) -> RuleProposal:
        proposal = await repo.get_rule_proposal(self._r, proposal_id)
        if proposal is None:
            raise NotFoundError(f"Rules proposal #{proposal_id} not found.")
        return proposal

    async def rule_proposal_diffs(self) -> dict[int, str]:
        """Per open proposal, what accepting it would change now, or why it no longer applies."""
        diffs: dict[int, str] = {}
        for proposal in await repo.list_rule_proposals(self._r):
            try:
                _, project, body = await self._patched_rules(
                    self._r, proposal.scope, proposal.patch
                )
            except DomainError as exc:
                diffs[proposal.id] = f"No longer applies: {exc}"
                continue
            diffs[proposal.id] = rules_diff(await self._rule_body(self._r, project), body)
        return diffs

    async def rule_proposals(self) -> list[RuleProposal]:
        return await repo.list_rule_proposals(self._r)

    async def decided_rule_proposals(self, scope: str) -> list[RuleProposal]:
        return await repo.decided_rule_proposals(self._r, scope)

    async def rule_versions(self, scope: str) -> list[RuleVersion]:
        _, project = await self._rules_scope(self._r, scope)
        rule_set = await repo.get_rule_set(self._r, project.id if project else None)
        return await repo.list_rule_versions(self._r, rule_set.id) if rule_set else []

    async def _known_session(
        self, value: str, conn: aiosqlite.Connection | None = None
    ) -> Session | None:
        """The session a write named, without creating or touching it; None for an id that
        cannot be anyone's."""
        conn = conn or self._r
        try:
            external_id = parse_session_id(value)
        except ValidationError:
            return None
        if external_id is not None:
            return await repo.get_session_by_external_id(conn, external_id)
        aliased = await repo.get_session_by_alias(conn, value)
        return aliased or await repo.get_session_by_name(conn, value)

    async def note_rules_update(
        self, session: str | None, project: str | None = None, task: str | None = None
    ) -> None:
        """After a write: one note when the rules have a newer version than this session was
        last told about. The first time a session is met nothing is said, it reads them anyway.
        A global change is said once, whichever project the session writes to next."""
        value = (session or "").strip()
        found = await self._known_session(value) if value else None
        if found is None:
            return
        proj = await repo.get_project(self._r, project.strip().lower()) if project else None
        if proj is None and task:
            # `task` is what the write just used; only a task deleted meanwhile is missing.
            with contextlib.suppress(NotFoundError):
                proj = await self.get_project((await self.get_task(task)).project_slug)
        rules = await self._rules(self._r, proj) if proj else self.global_rules
        layers = [(0, rules.global_version)]
        if proj is not None:
            layers.append((proj.id, rules.project_version))
        stale, unseen = False, []
        for project_id, current in layers:
            seen = await repo.rule_seen(self._r, found.id, project_id)
            if seen is None or seen < current:
                unseen.append((project_id, current))
                stale = stale or seen is not None
        if unseen:
            async with self._tx() as tx:
                for project_id, current in unseen:
                    await repo.set_rule_seen(tx.conn, found.id, project_id, current)
        if stale:
            _note(f"rules updated to v{rules.version}: re-read {rules.uri}")

    # --- quick switcher and saved filters ------------------------------------------------------

    async def switcher(self, query: str) -> list[SwitcherItem]:
        """Tasks, projects and sessions matching `query`, best first; with nothing typed, the
        tasks changed last and the projects."""
        text = query.strip()[:QUERY_MAX]
        if not text:
            return await self._switcher_start()
        items: list[tuple[int, SwitcherItem]] = []
        for key, title, stage, slug in await repo.switcher_tasks(self._r, TASKS_SCANNED):
            score = fuzzy_score(text, key, title)
            if score is not None:
                url = f"/p/{slug}?{urlencode({'task': key})}"
                items.append((score, SwitcherItem("task", title, f"{stage} · {slug}", url, key)))
        for summary in await self.list_projects():
            p = summary.project
            score = fuzzy_score(text, p.slug, p.name)
            if score is not None:
                items.append((score, SwitcherItem("project", p.name, p.slug, f"/p/{p.slug}")))
        for session in await repo.list_sessions(self._r, None, SESSIONS_SCANNED):
            score = fuzzy_score(text, session.name, session.external_id)
            if score is not None:
                detail = session.status or session.kind or "session"
                url = f"/sessions#session-{session.id}"
                items.append((score, SwitcherItem("session", session.name, detail, url)))
        # sorted() is stable: equal scores keep the most recently changed first.
        ranked = sorted(items, key=lambda pair: -pair[0])
        return [item for _, item in ranked[:RESULTS_MAX]]

    async def _switcher_start(self) -> list[SwitcherItem]:
        """With nothing typed: the tasks changed last, then every project."""
        tasks = await repo.switcher_tasks(self._r, EMPTY_QUERY_TASKS)
        return [
            *(
                SwitcherItem(
                    "task", title, f"{stage} · {slug}", f"/p/{slug}?{urlencode({'task': key})}", key
                )
                for key, title, stage, slug in tasks
            ),
            *(
                SwitcherItem("project", s.project.name, s.project.slug, f"/p/{s.project.slug}")
                for s in await self.list_projects()
            ),
        ]

    async def saved_filters(self, project: str | None = None) -> list[SavedFilter]:
        project_id = (await self.get_project(project)).id if project else None
        return await repo.list_saved_filters(self._r, project_id)

    async def save_filter(
        self,
        project: str,
        name: str,
        *,
        stage: str | None = None,
        waiting_on: str | None = None,
        session: str | None = None,
        q: str | None = None,
        actor: str = "board",
    ) -> SavedFilter:
        """A named board query; the name is unique within the project."""
        clean_name = _clean_text(name, "name", FILTER_NAME_MAX)
        if not clean_name:
            raise ValidationError("A saved filter needs a name.")
        query = {
            "stage": parse_stage(stage).value if stage and stage.strip() else "",
            "waiting_on": _clean_text(waiting_on, "waiting on", FILTER_TEXT_MAX) or "",
            "session": _clean_text(session, "session", NAME_MAX) or "",
            "q": _clean_text(q, "search text", FILTER_TEXT_MAX) or "",
        }
        query = {key: value for key, value in query.items() if value}
        if not query:
            raise ValidationError("Set a stage, waiting on, session or search text first.")
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            if await repo.saved_filter_named(tx.conn, proj.id, clean_name) is not None:
                raise ConflictError(f"{proj.slug} already has a saved filter {clean_name!r}.")
            filter_id = await repo.insert_saved_filter(tx.conn, proj.id, clean_name, query, tx.now)
            await tx.event(
                "filter.saved", actor, project_id=proj.id, project=proj.slug, name=clean_name
            )
            saved = await repo.get_saved_filter(tx.conn, filter_id)
            assert saved is not None
            return saved

    async def delete_filter(self, project: str, filter_id: int, *, actor: str = "board") -> None:
        async with self._tx() as tx:
            proj = await self.get_project(project, tx.conn)
            found = await repo.get_saved_filter(tx.conn, filter_id)
            if found is None or found.project_id != proj.id:
                raise NotFoundError(f"{proj.slug} has no saved filter #{filter_id}.")
            await repo.delete_saved_filter(tx.conn, filter_id)
            await tx.event(
                "filter.deleted", actor, project_id=proj.id, project=proj.slug, name=found.name
            )

    # --- runs on a card ------------------------------------------------------

    def _claude_missing(self) -> str:
        configured = self.runs.config.claude_path if self.runs is not None else None
        if configured is not None:
            return f"claude_path is {configured}, which is not a file; fix it in config.toml."
        return CLAUDE_MISSING

    def _run_home_problem(self) -> str | None:
        """The settings check reads `claude_home`, but a run's claude runs with the server's own
        home folder: when they differ the check would judge files the turn never loads."""
        home = self.runs.config.claude_home if self.runs is not None else None
        if home is None or _same_folder(home, Path.home()):
            return None
        return (
            f"claude_home is {home}, but a run's claude uses the home folder {Path.home()}:"
            " runs need claude_home unset (or equal to that folder)."
        )

    def _run_manager(self) -> RunManager:
        if self.runs is None:
            raise ValidationError("Runs need a server started by relaybay serve or the program.")
        return self.runs

    async def run_defaults(
        self, slug: str, conn: aiosqlite.Connection | None = None
    ) -> RunDefaults:
        project = await self.get_project(slug, conn)
        found = await repo.get_run_defaults(conn or self._r, project.id)
        return found or RunDefaults(project.id)

    async def set_run_defaults(
        self, slug: str, values: dict[str, Any], *, actor: str = "owner"
    ) -> RunDefaults:
        """The project's run defaults. Board only (no MCP tool): an agent never widens its own
        permission mode, and `bypassPermissions` is never a default."""
        async with self._tx() as tx:
            project = await self.get_project(slug, tx.conn)
            current = await repo.get_run_defaults(tx.conn, project.id) or RunDefaults(project.id)
            new = RunDefaults(
                project.id,
                model=run_rules.clean_model(values.get("model", current.model)),
                effort=run_rules.clean_effort(values.get("effort", current.effort)),
                permission_mode=run_rules.clean_mode(
                    values.get("permission_mode", current.permission_mode), owner=False
                ),
                allowed_tools=run_rules.clean_allowed_tools(
                    values.get("allowed_tools", current.allowed_tools)
                ),
                chrome=bool(values.get("chrome", current.chrome)),
                folder=run_rules.clean_folder(values.get("folder", current.folder)),
            )
            await repo.upsert_run_defaults(tx.conn, new, tx.now, actor)
            await tx.event(
                "run.defaults_set",
                actor,
                project_id=project.id,
                project=project.slug,
                model=new.model,
                effort=new.effort,
                permission_mode=new.permission_mode,
                allowed_tools=new.allowed_tools,
                chrome=new.chrome,
                folder=new.folder,
            )
            saved = await repo.get_run_defaults(tx.conn, project.id)
            assert saved is not None
            return saved

    async def run_login_any(self) -> bool:
        return await repo.get_app_setting(self._r, run_rules.LOGIN_ANY_KEY) == "1"

    async def set_run_login_any(self, on: bool, *, actor: str = "owner") -> None:
        """The owner's switch in Settings → Agents: runs may start under an API key or another
        sign-in that is not a subscription."""
        async with self._tx() as tx:
            value = "1" if on else "0"
            await repo.set_app_setting(tx.conn, run_rules.LOGIN_ANY_KEY, value, tx.now, actor)
            await tx.event("settings.run_login_any", actor, on=on)

    async def run_layers(self) -> LayerState | None:
        if self.runs is None:
            return None
        paths = claude_paths(self.runs.config.claude_home)
        return await asyncio.to_thread(user_layer_state, paths, self.runs.config.base_url)

    async def run_brief(self, task: Task) -> str:
        """The first turn's text without the run header: the task's digest and the report line."""
        budget = min(self.digest_budget_chars, run_rules.BRIEF_MAX - BRIEF_ROOM)
        text, _ = await self._task_context(task, None, None, budget)
        return f"{text}\n\n{RUN_REPORT_LINE}"

    @staticmethod
    def _run_header(
        task: Task, run_id: int, session_name: str, cwd: str, specs: list[WorktreeSpec]
    ) -> str:
        lines = [
            f"relaybay run {run_id} on card {task.key} ({task.project_slug}).",
            f'In relaybay writes pass session "{session_name}" and the hook session your'
            " SessionStart gave you as hook_session.",
            f"Your working folder: {cwd}",
        ]
        for spec in specs:
            how = f"new worktree, branch {spec.branch}" if spec.create else "the repository itself"
            lines.append(f"- {spec.repo_name}: {spec.path} ({how})")
        lines += ["Work only in these folders.", ""]
        return "\n".join(lines)

    def _worktree_root(self) -> Path:
        return self._run_manager().config.home / "worktrees"

    def _run_folder(self, run_id: int) -> Path:
        """The run's own files (its --settings and --mcp-config) in the data folder."""
        return self._run_manager().config.home / "runs" / str(run_id)

    async def run_form(self, task: Task) -> RunForm:
        """What the Start form shows: defaults, the folder and repositories, what a new worktree
        gets copied, the brief and why Start would be refused (the sign-in, http hooks of an
        older setup)."""
        defaults = await self.run_defaults(task.project_slug)
        repos = await repo.list_project_repos(self._r, task.project_id)
        linked = {link.repo_id for link in await repo.task_repo_links(self._r, task_id=task.id)}
        refusals: list[str] = []
        login, layers, claude = None, None, None
        if self.runs is None:
            refusals.append("Runs need a server started by relaybay serve or the program.")
        else:
            claude = self.runs.claude()
            login = await self.runs.login()
            why = run_auth.refusal(login, await self.run_login_any())
            if claude is None:
                refusals.append(self._claude_missing())
            elif why:
                refusals.append(why)
            home = self._run_home_problem()
            if home:
                refusals.append(home)
            layers = await self.run_layers()
            if layers is not None and layers.refusal:
                refusals.append(layers.refusal)
        if not repos:
            refusals.append(NO_RUN_REPO)
        git = worktrees.find_git()
        modes, copy_line = await asyncio.to_thread(_worktree_preview, repos, git)
        return RunForm(
            defaults=defaults,
            repos=repos,
            task_repo_ids=linked or ({repos[0].id} if repos else set()),
            brief=await self.run_brief(task),
            claude=claude,
            login=login,
            login_line=run_auth.login_line(login) if login is not None else "",
            layers=layers,
            refusals=refusals,
            fix_command=run_auth.FIX_COMMAND,
            repo_modes=modes,
            copy_line=copy_line,
        )

    async def run_view(self, task: Task) -> RunView | None:
        """The card's open run, else its latest one."""
        run = await repo.latest_run_of_task(self._r, task.id)
        if run is None:
            return None
        turns = await repo.list_turns(self._r, run.id)
        folders = await repo.list_run_worktrees(self._r, run.id)
        view = RunView(run, turns, run_rules.totals(turns), folders)
        reported = [t for t in turns if t.report_id is not None]
        report = await repo.get_report(self._r, reported[-1].report_id) if reported else None
        if report is not None:
            view.last_report = next(
                (line.strip() for line in report.body_md.splitlines() if line.strip()), ""
            )
        started = [t for t in turns if t.started_at is not None]
        if started:
            view.last_counts = await repo.run_event_counts(self._r, started[-1].id)
        return view

    async def _run_turn(self, run_id: int, seq: int | None) -> tuple[Run, RunTurn]:
        """A turn of a run by its number; None: the latest one that started."""
        run = await self.get_run(run_id)
        turns = await repo.list_turns(self._r, run.id)
        if seq is None:
            started = [t for t in turns if t.started_at is not None]
            if not started:
                raise NotFoundError(f"Run {run.id} has no turn that started.")
            return run, started[-1]
        turn = next((t for t in turns if t.seq == seq), None)
        if turn is None:
            raise NotFoundError(f"Run {run.id} has no turn {seq}.")
        return run, turn

    async def run_turn_detail(
        self, run_id: int, seq: int
    ) -> tuple[Run, RunTurn, Report | None, list[ReportCheck], list[run_lines.EventItem]]:
        """The board's view of a turn: its report with its marks and its last BOARD_LINES lines,
        nested under the calls of their subagents, their texts within BOARD_TOTAL_MAX."""
        run, turn = await self._run_turn(run_id, seq)
        report = await repo.get_report(self._r, turn.report_id) if turn.report_id else None
        checks = []
        if report is not None:
            checks = (await repo.report_checks(self._r, [report.id])).get(report.id, [])
        lines = await repo.list_run_events(self._r, turn.id, limit=run_lines.BOARD_LINES, last=True)
        items = run_lines.items(lines, total=run_lines.BOARD_TOTAL_MAX)
        return run, turn, report, checks, run_lines.nest(items)

    async def run_events(
        self, run_id: int, seq: int | None, *, limit: int, from_seq: int | None = None
    ) -> tuple[Run, RunTurn, list[run_lines.EventItem]]:
        """run_events over MCP, open to every caller, changing nothing: a turn's lines as short
        items, from `from_seq` on, else the last `limit` lines."""
        run, turn = await self._run_turn(run_id, seq)
        lines = await repo.list_run_events(
            self._r, turn.id, from_seq=from_seq, limit=limit, last=from_seq is None
        )
        return run, turn, run_lines.items(lines, run_lines.COMPACT_TEXT_MAX)

    async def run_status(self, ref: str | int) -> tuple[Task, RunView | None]:
        """run_status over MCP: open to every caller, it changes nothing."""
        task = await self.get_task(ref)
        return task, await self.run_view(task)

    async def get_run(self, run_id: int, conn: aiosqlite.Connection | None = None) -> Run:
        run = await repo.get_run(conn or self._r, run_id)
        if run is None:
            raise NotFoundError(f"Run #{run_id} not found.")
        return run

    async def _run_preflight(self, runs: RunManager) -> tuple[str, LayerState | None]:
        """The sign-in and the user layer, right before a turn; returns the sign-in method and
        where relaybay's hooks and MCP server come from."""
        if runs.claude() is None:
            raise ValidationError(self._claude_missing())
        state = await runs.login(force=True)
        why = run_auth.refusal(state, await self.run_login_any())
        if why:
            raise ValidationError(why)
        home = self._run_home_problem()
        if home:
            raise ValidationError(home)
        layers = await self.run_layers()
        if layers is not None and layers.refusal:
            raise ValidationError(layers.refusal)
        return state.method or "unknown", layers

    async def _run_choices(
        self,
        task: Task,
        defaults: RunDefaults,
        *,
        repo_id: int | None,
        cwd: str | None,
        repos: list[tuple[int, bool]] | None,
        coordinator: bool,
    ) -> tuple[int | None, list[tuple[int | None, str, str, bool]]]:
        """The cwd (None: the project folder; else the repository id) and, per repository the
        run works in, (id, name, path, new worktree?). Nothing chosen (the 1a form, an MCP call
        without repos): the card's linked repositories, else the first one; the owner's default
        is the repository itself, the coordinator always gets new worktrees."""
        project_repos = await repo.list_project_repos(self._r, task.project_id)
        if not project_repos:
            raise ValidationError(NO_RUN_REPO)
        by_id = {r.id: r for r in project_repos}
        first = 0  # not an id: the first repository chosen
        cwd_repo: int | None
        if cwd == "folder" or (not cwd and repo_id is None and coordinator and defaults.folder):
            if not defaults.folder:
                raise ValidationError(
                    f"{task.project_slug} has no project folder: set it in the agent defaults on"
                    " Stands & access, or start in one of its repositories."
                )
            cwd_repo = None
        elif cwd:
            if not cwd.isdigit() or int(cwd) not in by_id:
                raise NotFoundError(f"{task.project_slug} has no repository #{cwd}.")
            cwd_repo = int(cwd)
        else:
            cwd_repo = repo_id if repo_id is not None else first
        if repos is None:
            if cwd_repo:
                picked = [cwd_repo]
            else:
                linked = await repo.task_repo_links(self._r, task_id=task.id)
                picked = [x.repo_id for x in linked if x.repo_id in by_id] or [project_repos[0].id]
            repos = [(rid, coordinator) for rid in picked]
        for rid, _ in repos:
            if rid not in by_id:
                raise NotFoundError(f"{task.project_slug} has no repository #{rid}.")
        if cwd_repo == first:
            cwd_repo = repos[0][0] if repos else project_repos[0].id
        if cwd_repo is not None and cwd_repo not in {rid for rid, _ in repos}:
            repos = [(cwd_repo, coordinator), *repos]
        if not repos:
            raise ValidationError("Choose at least one repository for the run to work in.")
        choices: list[tuple[int | None, str, str, bool]] = []
        for rid, create in dict(repos).items():
            found = by_id[rid]
            choices.append((found.id, found.name, found.path, create or coordinator))
        return cwd_repo, choices

    async def _worktree_conflict(
        self, conn: aiosqlite.Connection, specs: list[WorktreeSpec], cwd_folder: str | None
    ) -> None:
        """One open run per folder. A repository used in place conflicts with another one in
        place (the same folder or one inside the other) and with an open run's project folder
        it lies in; a worktree a run made only with the same path (it may lie inside a
        repository when the data folder does). A new run in a project folder conflicts with an
        open run using a repository in place inside it."""
        in_use: list[tuple[str, bool, int, str]] = [  # path, made by the run, run, card
            (w.path, w.created_by_run, w.run_id, key) for w, key in await repo.open_worktrees(conn)
        ]
        folders: list[tuple[str, int, str]] = []
        with_rows = {run_id for _, _, run_id, _ in in_use}
        for other in await repo.open_runs(conn):
            if other.cwd_repo_id is None:
                folders.append((other.cwd, other.id, other.task_key))
            elif other.id not in with_rows:
                # Started before its folders were recorded: its cwd is its one repository.
                in_use.append((other.cwd, False, other.id, other.task_key))

        def refuse(name: str, path: str, run_id: int, key: str) -> NoReturn:
            raise ConflictError(
                f"{name} ({path}) is in use by run {run_id} on {key}; end that run first."
            )

        for spec in specs:
            for path, made, run_id, key in in_use:
                if spec.create or made:
                    if worktrees.path_key(path) == worktrees.path_key(spec.path):
                        refuse(spec.repo_name, spec.path, run_id, key)
                elif _paths_overlap(path, spec.path):
                    refuse(spec.repo_name, spec.path, run_id, key)
            if not spec.create:
                for folder, run_id, key in folders:
                    if _paths_overlap(folder, spec.path):
                        refuse(spec.repo_name, spec.path, run_id, key)
        if cwd_folder is not None:
            for path, made, run_id, key in in_use:
                if not made and _paths_overlap(cwd_folder, path):
                    refuse("The project folder", cwd_folder, run_id, key)

    async def start_run(
        self,
        ref: str | int,
        *,
        actor: str = "owner",
        repo_id: int | None = None,
        cwd: str | None = None,
        repos: list[tuple[int, bool]] | None = None,
        model: str | None = None,
        effort: str | None = None,
        mode: str | None = None,
        brief: str | None = None,
        caller_kind: str = "owner",
    ) -> Run:
        """Start a run on the card: the owner's board button, or `run_start` of a coordinator
        (`start_run_mcp` checks the caller). Checks come first; then the new worktrees are made
        (no transaction is open); one transaction writes the run, its folders, its session, the
        binding and turn 1; the process starts after the commit. When the transaction fails,
        the worktrees just made are removed again.

        `cwd`: "folder" (the project folder) or a repository id; `repos`: (repository id, new
        worktree?) per repository the run works in."""
        runs = self._run_manager()
        coordinator = caller_kind == "coordinator"
        task = await self.get_task(ref)
        defaults = await self.run_defaults(task.project_slug)
        model = run_rules.clean_model(model or defaults.model)
        effort = run_rules.clean_effort(effort or defaults.effort)
        if coordinator:
            mode = run_rules.clean_mode(defaults.permission_mode, owner=False)
        else:
            mode = run_rules.clean_mode(mode or defaults.permission_mode, owner=True)
        if brief and brief.strip():
            text = run_rules.clean_prompt(brief, "brief")
        else:
            text = await self.run_brief(task)
        if coordinator:
            qrules.check_no_answer_line(text, "The brief")
            arules.check_no_decision_line(text, "The brief")
        cwd_repo, choices = await self._run_choices(
            task, defaults, repo_id=repo_id, cwd=cwd, repos=repos, coordinator=coordinator
        )
        if cwd_repo is None and not await asyncio.to_thread(Path(defaults.folder or "").is_dir):
            raise ValidationError(f"The project folder {defaults.folder} is not a folder here.")
        if await repo.open_run_of_task(self._r, task.id) is not None:
            raise ConflictError(f"{task.key} already has an open run; end it first.")
        run_id = await repo.next_run_id(self._r)
        specs = worktrees.plan_worktrees(task.key, run_id, choices, self._worktree_root())
        cwd_folder = defaults.folder if cwd_repo is None else None
        await self._worktree_conflict(self._r, specs, cwd_folder)
        method, layers = await self._run_preflight(runs)
        hooks_source = layers.hooks_source if layers is not None else "user"
        mcp_source = layers.mcp_source if layers is not None else "user"
        claude = runs.claude()
        assert claude is not None
        settings_file, mcp_file = run_files(
            self._run_folder(run_id), hooks=hooks_source == "run", mcp=mcp_source == "run"
        )
        # The batch-shim check of the whole command line, before anything is made or written.
        build_command(
            TurnSpec(
                claude,
                "",
                defaults.folder or specs[0].path,
                model,
                effort,
                mode,
                tuple(defaults.allowed_rules),
                add_dirs=tuple(s.path for s in specs),
                settings_file=str(settings_file) if settings_file else None,
                mcp_file=str(mcp_file) if mcp_file else None,
            )
        )
        git = worktrees.find_git()
        made = await _make_worktrees(specs, git, task.key, run_id)
        # The paths as made: a taken run folder moves to a numbered one.
        by_repo = {spec.repo_id: spec for spec in made}
        cwd_path = (defaults.folder or "") if cwd_repo is None else by_repo[cwd_repo].path
        try:
            turn_id = await self._insert_run(
                task,
                run_id,
                made,
                actor=actor,
                caller_kind=caller_kind,
                cwd_path=cwd_path,
                cwd_repo=cwd_repo,
                values={
                    "model": model,
                    "effort": effort,
                    "permission_mode": mode,
                    "allowed_tools": defaults.allowed_tools,
                    "chrome": int(defaults.chrome),
                    "auth_method": method,
                    "hooks_source": hooks_source,
                    "mcp_source": mcp_source,
                },
                text=text,
            )
        except BaseException:
            await asyncio.shield(asyncio.to_thread(worktrees.remove_created, made, git))
            raise
        await self._spawn_turn(run_id, turn_id)
        return await self.get_run(run_id)

    async def _insert_run(
        self,
        task: Task,
        run_id: int,
        made: list[WorktreeSpec],
        *,
        actor: str,
        caller_kind: str,
        cwd_path: str,
        cwd_repo: int | None,
        values: dict[str, Any],
        text: str,
    ) -> int:
        """The one transaction of a Start; returns turn 1's id."""
        async with self._tx() as tx:
            task = await self.get_task(task.id, tx.conn)
            if await repo.open_run_of_task(tx.conn, task.id) is not None:
                raise ConflictError(f"{task.key} already has an open run; end it first.")
            await self._worktree_conflict(tx.conn, made, cwd_path if cwd_repo is None else None)
            if await repo.next_run_id(tx.conn) != run_id:
                raise ConflictError("Another run started at the same moment; start this one again.")
            await repo.insert_run(
                tx.conn,
                {
                    "id": run_id,
                    "task_id": task.id,
                    "project_id": task.project_id,
                    "cwd": cwd_path,
                    "cwd_repo_id": cwd_repo,
                    **values,
                    "status": "running",
                    "started_by": actor,
                    "started_by_kind": caller_kind,
                },
                tx.now,
            )
            for spec in made:
                await repo.insert_run_worktree(
                    tx.conn,
                    run_id,
                    {
                        "project_repo_id": spec.repo_id,
                        "repo_name": spec.repo_name,
                        "source_path": spec.source,
                        "path": spec.path,
                        "path_key": worktrees.path_key(spec.path),
                        "branch": spec.branch,
                        "created_by_run": int(spec.create),
                        "copied": list(spec.copied),
                    },
                    tx.now,
                )
            name = f"{task.key} run {run_id}"
            session_id = await repo.insert_session(tx.conn, name, tx.now, kind="run")
            await repo.touch_session(
                tx.conn, session_id, tx.now, {"project_id": task.project_id, "cwd": cwd_path}
            )
            await repo.update_run(tx.conn, run_id, {"session_id": session_id}, tx.now)
            await tx.event(
                "session.seen",
                name,
                project_id=task.project_id,
                session_id=session_id,
                session=name,
            )
            session = await repo.get_session_by_id(tx.conn, session_id)
            assert session is not None
            await self._bind(tx, task, session, actor)
            prompt = self._run_header(task, run_id, name, cwd_path, made) + text
            turn_id = await repo.insert_turn(tx.conn, run_id, prompt, actor, caller_kind, tx.now)
            await repo.start_turn(tx.conn, turn_id, tx.now)
            await self._run_event(
                tx,
                "run.started",
                actor,
                task,
                run_id,
                model=values["model"],
                mode=values["permission_mode"],
                worktrees=sum(1 for s in made if s.create),
            )
            await self._run_event(tx, "run.turn_started", actor, task, run_id, seq=1)
        return turn_id

    async def start_run_mcp(
        self,
        ref: str,
        *,
        repos: list[str] | None = None,
        cwd: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        brief: str | None = None,
        permission_mode: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Run:
        """`run_start`: a coordinator starts a run on a card in plan or dev with no brake on it,
        in new worktrees, with the project's permission profile. Anyone else is refused."""
        if not await self._is_coordinator(self._r, session, hook_session):
            raise ValidationError(RUN_START_NOT_COORDINATOR)
        if (permission_mode or "").strip() not in ("", "-"):
            raise ValidationError(
                "run_start takes no permission mode: a run started over MCP uses the project's"
                " agent defaults, which only the owner changes on the board. Nothing started."
            )
        stopped = ("no run starts", "Nothing started.")
        await self._refuse_braked_caller(session, *stopped)
        task = await self.get_task(ref)
        if task.stage.value not in RUN_START_STAGES:
            raise ValidationError(
                f"run_start works on cards in plan or dev; {task.key} is in {task.stage.value}."
                " Nothing started."
            )
        await self._refuse_braked_task(task, *stopped)
        bound_id = await repo.open_binding(self._r, task.id)
        bound = await repo.get_session_by_id(self._r, bound_id) if bound_id else None
        if bound is not None and bound.kind != "run" and bound.status != "ended":
            raise ValidationError(
                f"{task.key} is bound to {bound.name}, which is not ended: a run would take the"
                " card from it. Unbind it first or ask the owner. Nothing started."
            )
        project_repos = await repo.list_project_repos(self._r, task.project_id)
        by_name = {r.name.lower(): r.id for r in project_repos}
        picked: list[tuple[int, bool]] | None = None
        if repos:
            unknown = [name for name in repos if name.strip().lower() not in by_name]
            if unknown:
                raise NotFoundError(
                    f"{task.project_slug} has no repository {', '.join(unknown)}; it has"
                    f" {', '.join(r.name for r in project_repos) or 'none'}."
                )
            picked = [(by_name[name.strip().lower()], True) for name in repos]
        where = (cwd or "").strip()
        if where.lower() in ("folder", "-"):
            where = where.lower()
        elif where:
            if where.lower() not in by_name:
                raise NotFoundError(
                    f"cwd is 'folder' or one of {task.project_slug}'s repositories:"
                    f" {', '.join(r.name for r in project_repos) or 'none'}."
                )
            where = where.lower()
        cwd_value = (
            None if where in ("", "-") else "folder" if where == "folder" else str(by_name[where])
        )
        async with self._tx() as tx:
            caller = await self._session(tx, session, session_title, hook_session)
            assert caller is not None
        return await self.start_run(
            task.key,
            actor=caller.name,
            cwd=cwd_value,
            repos=picked,
            model=model,
            effort=effort,
            brief=brief,
            caller_kind="coordinator",
        )

    async def _refuse_braked_caller(self, session: str | None, what: str, nothing: str) -> None:
        """A coordinator's run tool: refused while a brake covers the caller."""
        caller_row = await self._known_session((session or "").strip(), self._r)
        brake = await self._brake_for(caller_row, None)
        if brake is not None:
            raise ValidationError(
                f"You are under the brake ({brake.target}): {what} until the owner releases it"
                f" on the board. {nothing}"
            )

    async def _refuse_braked_task(self, task: Task, what: str, nothing: str) -> None:
        """A coordinator's run tool: refused while a brake of scope all, the card's project or
        the card is open."""
        for brake in await repo.open_brakes(self._r):
            if (
                brake.scope == "all"
                or (brake.scope == "project" and brake.project_id == task.project_id)
                or (brake.scope == "task" and brake.task_id == task.id)
            ):
                raise ValidationError(
                    f"{task.key} is under the brake ({brake.target}): {what} until the owner"
                    f" releases it on the board. {nothing}"
                )

    async def set_run_settings(
        self,
        run_id: int,
        *,
        mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        actor: str = "owner",
        caller_kind: str = "owner",
    ) -> Run:
        """The permission mode, model and effort of the run's next turns, passed to `claude -p
        --resume` from the next turn on; never while a turn runs. The owner may pick any mode; a
        coordinator only one that does not raise it (`domain.runs.MODE_RANK`) and never
        `bypassPermissions`. Empty or "-" keeps a value. A change is a `run.settings_changed`
        event with the old and the new values."""
        owner = caller_kind == "owner"
        runs = self._run_manager()

        def keep(value: str | None) -> bool:
            return (value or "").strip() in ("", "-")

        if not owner and (mode or "").strip() in run_rules.OWNER_ONLY_MODES:
            raise ValidationError(
                f"{mode} is the owner's alone, picked on the card. Nothing changed."
            )

        async with self._tx() as tx:
            run = await self.get_run(run_id, tx.conn)
            if not run.is_open:
                raise ValidationError(f"Run {run.id} has ended. Nothing changed.")
            if (
                run.status == "running"
                or runs.is_running(run.id)
                or await repo.active_turn(tx.conn, run.id) is not None
            ):
                raise ValidationError(
                    f"A turn of run {run.id} is running: the mode, model and effort change"
                    " between turns. Wait until it ends or stop it. Nothing changed."
                )
            new = {
                "permission_mode": (
                    run.permission_mode if keep(mode) else run_rules.clean_mode(mode, owner=owner)
                ),
                "model": run.model if keep(model) else run_rules.clean_model(model),
                "effort": run.effort if keep(effort) else run_rules.clean_effort(effort),
            }
            if not owner and run_rules.raises_mode(run.permission_mode, new["permission_mode"]):
                raise ValidationError(
                    f"{run.permission_mode} to {new['permission_mode']} raises the permission"
                    " mode, which only the owner does on the card. Nothing changed."
                )
            old = {
                "permission_mode": run.permission_mode,
                "model": run.model,
                "effort": run.effort,
            }
            changed = {k: {"from": old[k], "to": v} for k, v in new.items() if old[k] != v}
            if changed:
                task = await self.get_task(run.task_id, tx.conn)
                await repo.update_run(tx.conn, run.id, {k: new[k] for k in changed}, tx.now)
                await self._run_event(
                    tx, "run.settings_changed", actor, task, run.id, changed=changed
                )
        return await self.get_run(run_id)

    async def run_settings_mcp(
        self,
        ref: str,
        *,
        permission_mode: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Run:
        """`run_settings`: a coordinator changes the mode (never raising it), model or effort of
        the card's open run between turns, under the brake rules of `run_start`."""
        if not await self._is_coordinator(self._r, session, hook_session):
            raise ValidationError(RUN_SETTINGS_NOT_COORDINATOR)
        stopped = ("no run changes", "Nothing changed.")
        await self._refuse_braked_caller(session, *stopped)
        task = await self.get_task(ref)
        await self._refuse_braked_task(task, *stopped)
        run = await repo.open_run_of_task(self._r, task.id)
        if run is None:
            raise NotFoundError(f"{task.key} has no open run. Nothing changed.")
        async with self._tx() as tx:
            caller = await self._session(tx, session, session_title, hook_session)
            assert caller is not None
        return await self.set_run_settings(
            run.id,
            mode=permission_mode,
            model=model,
            effort=effort,
            actor=caller.name,
            caller_kind="coordinator",
        )

    async def open_in_desktop(self, run_id: int, *, actor: str = "owner") -> Run:
        """Hand the run's session to the owner in the Claude desktop app (the board only):
        `claude --desktop --resume <id>` in the run's cwd, in a console of its own, its stderr in
        a file of the run's folder so a refusal is shown. The run is with the owner from before
        the launch, so no turn starts meanwhile; a failed launch hands it back and starts a
        waiting text. A run handed back or ended while the launch ran keeps only the time."""
        runs = self._run_manager()
        run = await self.get_run(run_id)
        self._check_hand_over(run, runs.is_running(run.id))
        cc_id = run.claude_session_id
        if cc_id is None or not CLAUDE_SESSION_RE.fullmatch(cc_id):
            raise ValidationError(
                f"Run {run.id} has no Claude session yet: its first turn has not started."
            )
        launcher = runs.desktop_launcher
        if launcher is None:
            raise ValidationError(
                "relaybay opens the desktop app only on Windows, where it can give the command a"
                f" console of its own. {self._desktop_hint(run, cc_id)}"
            )
        claude = runs.claude()
        if claude is None:
            raise ValidationError(self._claude_missing())
        stderr_file = self._run_folder(run.id) / "desktop-stderr.txt"
        command = run_desktop.desktop_command_line(claude, cc_id, stderr_file)
        if run.id in self.desktop_launching:
            raise ValidationError(f"The desktop app is being opened for run {run.id} already.")
        async with self._tx() as tx:
            run = await self.get_run(run_id, tx.conn)
            self._check_hand_over(run, runs.is_running(run.id))
            if await repo.active_turn(tx.conn, run.id) is not None:
                raise ValidationError(f"A turn of run {run.id} is running; wait or stop it.")
            await repo.update_run(
                tx.conn, run.id, {"with_owner_at": tx.now, "with_owner_by": actor}, tx.now
            )
        self.desktop_launching.add(run.id)  # no await since the commit
        why: str | None = None
        try:
            await asyncio.to_thread(stderr_file.parent.mkdir, parents=True, exist_ok=True)
            env = run_desktop.desktop_env(os.environ)
            code = await asyncio.to_thread(
                launcher, command, run.cwd, env, run_desktop.DESKTOP_TIMEOUT_SECONDS
            )
            if code != 0:
                why = f"claude --desktop exited with code {code}"
        except subprocess.TimeoutExpired:
            why = f"claude --desktop did not finish in {run_desktop.DESKTOP_TIMEOUT_SECONDS:.0f} s"
        except OSError as exc:
            why = f"claude --desktop did not start: {exc}"
        finally:
            self.desktop_launching.discard(run.id)
            reason = await asyncio.to_thread(run_desktop.take_reason, stderr_file)
        if why is not None:
            if reason:
                why = f"{why}: {reason}"
            try:
                await self._hand_back(run.id, actor, event=False)
                stays = "the run stays with relaybay"
            except ValidationError as exc:  # the check before a waiting text refused
                stays = f"the run stays with the owner until Back to the program ({exc})"
            raise ValidationError(f"{why}; {stays}. {self._desktop_hint(run, cc_id)}")
        async with self._tx() as tx:
            run = await self.get_run(run_id, tx.conn)
            await repo.update_run(tx.conn, run.id, {"desktop_opened_at": tx.now}, tx.now)
            if run.is_open and run.with_owner:
                task = await self.get_task(run.task_id, tx.conn)
                await self._run_event(
                    tx, "run.opened_in_desktop", actor, task, run.id, claude_session_id=cc_id
                )
        return await self.get_run(run_id)

    @staticmethod
    def _desktop_hint(run: Run, cc_id: str) -> str:
        """Where else the session opens: the desktop app's link once it was opened there (a
        terminal would meet the same refusal), else the terminal command."""
        if run.desktop_link:
            return f"Open it in the desktop app: {run.desktop_link}"
        return f"Open the session in a terminal in {run.cwd}: {run_desktop.terminal_command(cc_id)}"

    @staticmethod
    def _check_hand_over(run: Run, running: bool) -> None:
        if not run.is_open:
            raise ValidationError(f"Run {run.id} has ended.")
        if run.with_owner:
            raise ValidationError(f"Run {run.id} is already with the owner in the desktop app.")
        if running or run.status == "running":
            raise ValidationError(f"A turn of run {run.id} is running; wait or stop it.")

    async def return_run(self, run_id: int, *, actor: str = "owner") -> Run:
        """Back to the program: the run is relaybay's again; the first waiting text starts. Not
        while the desktop app is still being opened (that launch would end on a run handed back)."""
        run = await self.get_run(run_id)
        if not run.with_owner:
            raise ValidationError(f"Run {run.id} is not with the owner.")
        if run.id in self.desktop_launching:
            raise ValidationError(
                f"The desktop app is still being opened for run {run.id}; try again in a moment."
            )
        await self._hand_back(run.id, actor, event=True)
        return await self.get_run(run_id)

    async def _hand_back(self, run_id: int, actor: str, *, event: bool) -> None:
        """Clear the owner's hold and start the first waiting text, if any. `event`: a
        `run.returned` in the journal (not after a launch that failed)."""
        runs = self._run_manager()
        queued = await repo.next_queued_turn(self._r, run_id)
        if queued is not None and run_id not in runs.checked_since_start:
            await self._run_preflight(runs)  # the first turn after a restart checks again
        follow: int | None = None
        async with self._tx() as tx:
            run = await self.get_run(run_id, tx.conn)
            if not run.with_owner:
                return
            task = await self.get_task(run.task_id, tx.conn)
            await repo.update_run(
                tx.conn, run.id, {"with_owner_at": None, "with_owner_by": None}, tx.now
            )
            run.with_owner_at = None
            if event:
                await self._run_event(tx, "run.returned", actor, task, run.id)
            if run.is_open and run.status != "running":
                follow = await self._start_next_queued(tx, run, task, actor)
        if follow is not None:
            await self._spawn_turn(run_id, follow)

    async def _start_next_queued(self, tx: _Tx, run: Run, task: Task, actor: str) -> int | None:
        """Mark the first waiting text started and the run running; its id, None if none waits.
        The caller spawns it after the commit."""
        queued = await repo.next_queued_turn(tx.conn, run.id)
        if queued is None or await repo.active_turn(tx.conn, run.id) is not None:
            return None
        await repo.start_turn(tx.conn, queued.id, tx.now)
        await self._set_run_status(tx, run, "running", None)
        await self._run_event(tx, "run.turn_started", actor, task, run.id, seq=queued.seq)
        return queued.id

    async def _run_event(
        self, tx: _Tx, kind: str, actor: str, task: Task, run_id: int, **payload: Any
    ) -> None:
        await tx.event(
            kind,
            actor,
            project_id=task.project_id,
            task_id=task.id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            run_id=run_id,
            **payload,
        )

    async def _set_run_status(
        self, tx: _Tx, run: Run, new: str, reason: str | None, **changes: Any
    ) -> None:
        """The one writer of a run's status: `domain.runs.check_transition` decides."""
        run_rules.check_transition(run.status, new)
        values: dict[str, Any] = {"status": new, "status_reason": reason, **changes}
        if new == "ended":
            values["ended_at"] = tx.now
        await repo.update_run(tx.conn, run.id, values, tx.now)
        run.status = new

    async def _spawn_turn(self, run_id: int, turn_id: int) -> None:
        """Start the process of a turn already marked started; a failure ends it visibly.
        Callers await it right after the commit that marked the turn started, so the reservation
        below is taken before anything else can run."""
        runs = self._run_manager()
        reserved = runs.reserve(run_id)
        try:
            run = await self.get_run(run_id)
            turn = await repo.get_turn(self._r, turn_id)
            folders = await repo.list_run_worktrees(self._r, run_id)
        except BaseException:
            runs.release(run_id)
            raise
        assert turn is not None
        if run.status != "running" or turn.ended_at is not None:
            # Stopped, ended or lost before the process existed: nothing to start.
            runs.release(run_id)
            return
        claude = runs.claude()
        error = self._claude_missing() if claude is None else None
        if not reserved:
            error = "the server is stopping" if runs.closing else "a turn of this run is running"
        pid = None
        if claude is not None and reserved:
            try:
                settings_file, mcp_file = await asyncio.to_thread(
                    write_run_files,
                    self._run_folder(run.id),
                    hooks=run.hooks_source == "run",
                    mcp=run.mcp_source == "run",
                    base_url=runs.config.base_url,
                    token_path=runs.config.token_path,
                )
                spec = TurnSpec(
                    claude,
                    turn.prompt,
                    run.cwd,
                    run.model,
                    run.effort,
                    run.permission_mode,
                    tuple(run.allowed_rules),
                    run.claude_session_id,
                    run.chrome,
                    add_dirs=_add_dirs(run.cwd, [w.path for w in folders if w.removed_at is None]),
                    settings_file=str(settings_file) if settings_file else None,
                    mcp_file=str(mcp_file) if mcp_file else None,
                )
                pid = await runs.start_turn(run.id, turn.id, spec, reserved=True)
            except (OSError, ValidationError, RuntimeError) as exc:
                error = f"claude did not start: {exc}"
        if pid is None and reserved:
            runs.release(run_id)
        async with self._tx() as tx:
            if pid is not None:
                runs.checked_since_start.add(run.id)
                await repo.set_turn_pid(tx.conn, turn.id, pid)
                await self._answers_delivered(
                    tx, await repo.undelivered_answers_of_turn(tx.conn, turn.id), run.session_name
                )
                await self._decisions_delivered(
                    tx, await repo.undelivered_decisions_of_turn(tx.conn, turn.id)
                )
                return
            run = await self.get_run(run_id, tx.conn)
            task = await self.get_task(run.task_id, tx.conn)
            await repo.end_turn(tx.conn, turn.id, {"outcome": "failed_to_start"}, tx.now)
            await repo.cancel_queued(tx.conn, run.id, tx.now)
            await self._set_run_status(tx, run, "stopped", error)
            await self._run_event(tx, "run.stopped", "relaybay", task, run.id, reason=error)

    async def turn_init(self, turn_id: int, claude_session_id: str) -> None:
        """The turn's `system/init`: the Claude Code session id every later turn resumes."""
        async with self._tx() as tx:
            turn = await repo.get_turn(tx.conn, turn_id)
            run = await repo.get_run(tx.conn, turn.run_id) if turn is not None else None
            if run is not None and run.claude_session_id != claude_session_id:
                await repo.update_run(
                    tx.conn, run.id, {"claude_session_id": claude_session_id}, tx.now
                )

    async def turn_events(self, turn_id: int, events: list[run_lines.StreamEvent]) -> None:
        """A batch of a turn's stream-json lines, masked and compressed by the runner. No board
        event: the card shows them when asked, and the turn's end refreshes it."""
        async with self._tx() as tx:
            await repo.insert_run_events(tx.conn, turn_id, events)

    async def _turn_report(
        self, tx: _Tx, task: Task, run: Run, turn: RunTurn, outcome: str, result: Result
    ) -> None:
        """The turn's result as a report on the card, in the turn's end transaction. A report
        that cannot be written must not keep the turn from ending: its writes are undone to a
        savepoint and the failure is logged. When the agent already reported this turn with
        report_add (the run's session, since the turn started) and the result adds nothing (no
        text, or the same first line, and no refusals), the turn links that report instead."""
        own = None
        if run.session_id is not None and turn.started_at is not None and not result.refusals:
            own = await repo.latest_report_of_session(
                tx.conn, task.id, run.session_id, turn.started_at
            )
        if own is not None:
            text = result.text.strip()
            if not text or _first_line(text) == _first_line(own.body_md):
                await repo.set_turn_report(tx.conn, turn.id, own.id)
                return
        body = run_rules.turn_report(
            run.id, turn.seq, outcome, result.text, [(r.tool, r.input) for r in result.refusals]
        )
        await tx.conn.execute("SAVEPOINT turn_report")
        events = len(tx.events)
        try:
            # No first-line warning: the turn's text is not a report_add the agent can fix.
            report_id = await self._write_report(
                tx,
                task,
                body,
                {},
                session_id=run.session_id,
                actor=run.session_name or f"run {run.id}",
                format_problem=None,
            )
            await repo.set_turn_report(tx.conn, turn.id, report_id)
        except Exception:
            await tx.conn.execute("ROLLBACK TO turn_report")
            del tx.events[events:]
            logger.exception("Writing the report of run %d turn %d failed", run.id, turn.seq)
        await tx.conn.execute("RELEASE turn_report")

    async def turn_done(
        self,
        turn_id: int,
        outcome: str,
        result: Result | None,
        exit_code: int | None,
        reason: str | None,
        stopped: bool = False,
    ) -> None:
        """A turn's process ended. The run goes idle (a result came), stopped (Stop, also after
        a result while the process was still ending) or lost; Stop and lost cancel the texts
        that waited, otherwise the next one starts."""
        follow: tuple[int, int] | None = None
        denied: list[tuple[arules.Need, str]] = []
        if result is not None and result.refusals:
            # Read before the transaction: the current branch comes from git.
            ended = await repo.get_turn(self._r, turn_id)
            assert ended is not None
            try:
                denied = await self._denied_needs(await self.get_run(ended.run_id), result)
            except Exception:
                logger.exception("Reading the denials of turn %d for approvals failed", turn_id)
        async with self._tx() as tx:
            turn = await repo.get_turn(tx.conn, turn_id)
            assert turn is not None
            run = await self.get_run(turn.run_id, tx.conn)
            task = await self.get_task(run.task_id, tx.conn)
            changes: dict[str, Any] = {"outcome": outcome, "exit_code": exit_code}
            if result is not None:
                changes.update(
                    result_subtype=result.subtype,
                    num_turns=result.num_turns,
                    duration_ms=result.duration_ms,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_read_tokens=result.cache_read_tokens,
                    cache_write_tokens=result.cache_write_tokens,
                    denials=result.denials,
                    denied_tools=result.denied_tools,
                )
            await repo.end_turn(tx.conn, turn.id, changes, tx.now)
            if result is not None:
                await self._turn_report(tx, task, run, turn, outcome, result)
            await self._requests_from_denials(tx, run, task, turn, denied)
            tokens = sum(
                changes.get(k, 0)
                for k in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                )
            )
            await self._run_event(
                tx,
                "run.turn_ended",
                "relaybay",
                task,
                run.id,
                seq=turn.seq,
                outcome=outcome,
                tokens=tokens,
            )
            if outcome in ("success", "error") and not stopped:
                subtype = result.subtype if result is not None else "unknown"
                why = None if outcome == "success" else f"the turn ended with an error ({subtype})"
                blocking = await repo.open_blocking_questions(tx.conn, run_id=run.id)
                requests = await repo.open_approvals(tx.conn, run_id=run.id)
                queued = None if run.with_owner else await repo.next_queued_turn(tx.conn, run.id)
                if (blocking or requests) and queued is None:
                    await self._run_waits(tx, run, task, blocking, why, requests)
                else:
                    await self._set_run_status(tx, run, "idle", why)
                if queued is not None:
                    queued_id = await self._start_next_queued(tx, run, task, "relaybay")
                    if queued_id is not None:
                        follow = (run.id, queued_id)
            else:
                new = "lost" if outcome == "lost" else "stopped"
                await self._set_run_status(tx, run, new, reason)
                await repo.cancel_queued(tx.conn, run.id, tx.now)
                await self._run_event(tx, f"run.{new}", "relaybay", task, run.id, reason=reason)
                await self._end_run_session(tx, run)
        if follow is not None:
            await self._spawn_turn(*follow)

    async def _end_run_session(self, tx: _Tx, run: Run) -> None:
        """A killed turn sends no SessionEnd (Windows kills hard): the row ends here."""
        session = await repo.get_session_by_id(tx.conn, run.session_id) if run.session_id else None
        if session is None or session.status == "ended":
            return
        await repo.touch_session(
            tx.conn,
            session.id,
            tx.now,
            {"status": "ended", "activity": "turn stopped", "activity_at": tx.now},
        )
        ended = await repo.get_session_by_id(tx.conn, session.id)
        assert ended is not None
        await self._session_events(tx, ended, ended.name, previous=session.status)

    async def send_to_run(self, run_id: int, text: str, *, actor: str = "owner") -> RunTurn:
        """Continue the run with the owner's text: a new turn with --resume, or, while a turn
        runs, a turn that waits for it (never two at once)."""
        runs = self._run_manager()
        prompt = run_rules.clean_prompt(text)
        run = await self.get_run(run_id)
        if not run.is_open:
            raise ValidationError(f"Run {run.id} has ended; start a new run on {run.task_key}.")
        if (
            not run.with_owner
            and not runs.is_running(run.id)
            and run.id not in runs.checked_since_start
        ):
            await self._run_preflight(runs)  # the first turn after a restart checks again
        async with self._tx() as tx:
            run = await self.get_run(run_id, tx.conn)
            if not run.is_open:
                raise ValidationError(f"Run {run.id} has ended; start a new run on {run.task_key}.")
            task = await self.get_task(run.task_id, tx.conn)
            repeat = await self._repeated_send(tx, run.id, prompt, actor)
            if repeat is not None:
                return repeat
            turn_id, launch = await self._queue_turn(tx, run, task, prompt, actor)
        if launch:
            await self._spawn_turn(run.id, turn_id)
        found = await repo.get_turn(self._r, turn_id)
        assert found is not None
        return found

    async def _queue_turn(
        self, tx: _Tx, run: Run, task: Task, text: str | None, actor: str
    ) -> tuple[int, bool]:
        """A new turn of the owner's on the run: started at once when no turn runs and the run is
        not with the owner, else it waits. The owner's answers that no turn's process got yet
        (their turn was cancelled) go first in its text. Returns the turn's id and whether the
        caller spawns it after the commit."""
        pending = await repo.pending_answers(tx.conn, run.id)
        decided = await repo.pending_decisions(tx.conn, run.id)
        parts = [qrules.answer_line(q) for q in pending]
        parts += [arules.decision_line(a) for a in decided]
        if text:
            parts.append(text)
        turn_id = await repo.insert_turn(
            tx.conn, run.id, "\n\n".join(parts), actor, "owner", tx.now
        )
        if pending:
            await repo.put_answers_on_turn(tx.conn, [q.id for q in pending], turn_id)
        if decided:
            await repo.put_decisions_on_turn(tx.conn, [a.id for a in decided], turn_id)
        turn = await repo.get_turn(tx.conn, turn_id)
        assert turn is not None
        # With the owner in the desktop app: the text waits for Back to the program.
        if (
            not run.with_owner
            and run.status != "running"
            and await repo.active_turn(tx.conn, run.id) is None
        ):
            await repo.start_turn(tx.conn, turn_id, tx.now)
            await self._set_run_status(tx, run, "running", None)
            await self._run_event(tx, "run.turn_started", actor, task, run.id, seq=turn.seq)
            return turn_id, True
        await self._run_event(tx, "run.turn_queued", actor, task, run.id, seq=turn.seq)
        return turn_id, False

    @staticmethod
    async def _repeated_send(tx: _Tx, run_id: int, prompt: str, actor: str) -> RunTurn | None:
        """The run's last turn when it is this text from this sender a moment ago (a double
        click), so the second send adds nothing."""
        turns = await repo.list_turns(tx.conn, run_id)
        last = turns[-1] if turns else None
        if last is None or last.ended_at is not None or last.sent_by != actor:
            return None
        # The owner's answers that waited for a turn go first in its text: compare the rest.
        carried = sorted(await repo.list_questions(tx.conn, turn_id=last.id), key=lambda q: q.id)
        decided = sorted(await repo.list_approvals(tx.conn, turn_id=last.id), key=lambda a: a.id)
        head = "".join(qrules.answer_line(q) + "\n\n" for q in carried)
        head += "".join(arules.decision_line(a) + "\n\n" for a in decided)
        if not last.prompt.startswith(head) or last.prompt[len(head) :] != prompt:
            return None
        queued = datetime.fromisoformat(last.queued_at)
        age = datetime.fromisoformat(tx.now) - queued
        return last if age.total_seconds() < SEND_REPEAT_SECONDS else None

    async def stop_run(self, run_id: int, *, actor: str = "owner") -> Run:
        """Kill the running turn's process tree; the session stays resumable."""
        runs = self._run_manager()
        run = await self.get_run(run_id)
        if await self._lose_orphan(runs, run):
            return await self.get_run(run_id)
        if not await runs.stop(run.id, f"stopped by {actor}"):
            raise ValidationError(f"No turn of run {run.id} is running.")
        return await self.get_run(run_id)

    async def _lose_orphan(self, runs: RunManager, run: Run) -> bool:
        """A run marked running with no process behind it (its end could not be stored, or
        the start failed oddly) is marked lost, so Stop and End work again. True if it was."""
        if run.status != "running" or runs.is_running(run.id):
            return False
        async with self._tx() as tx:
            run = await self.get_run(run.id, tx.conn)
            if run.status != "running" or runs.is_running(run.id):
                return False
            task = await self.get_task(run.task_id, tx.conn)
            reason = "no claude process was running for this turn"
            active = await repo.active_turn(tx.conn, run.id)
            if active is not None:
                await repo.end_turn(tx.conn, active.id, {"outcome": "lost"}, tx.now)
            await repo.cancel_queued(tx.conn, run.id, tx.now)
            await self._set_run_status(tx, run, "lost", reason)
            await self._run_event(tx, "run.lost", "relaybay", task, run.id, reason=reason)
            await self._end_run_session(tx, run)
        return True

    async def end_run(self, run_id: int, *, actor: str = "owner") -> Run:
        """No more turns: a running one is stopped first, waiting texts are cancelled."""
        runs = self._run_manager()
        run = await self.get_run(run_id)
        if not run.is_open:
            raise ValidationError(f"Run {run.id} has already ended.")
        if runs.is_running(run.id):
            await runs.stop(run.id, f"ended by {actor}")
        await self._lose_orphan(runs, run)
        async with self._tx() as tx:
            run = await self.get_run(run_id, tx.conn)
            task = await self.get_task(run.task_id, tx.conn)
            await repo.cancel_queued(tx.conn, run.id, tx.now)
            await self._set_run_status(
                tx,
                run,
                "ended",
                f"ended by {actor}",
                ended_by=actor,
                with_owner_at=None,
                with_owner_by=None,
            )
            await self._run_event(tx, "run.ended", actor, task, run.id)
            # No next turn will carry them: the owner's answers go to the card's session instead.
            for q in await repo.pending_answers(tx.conn, run.id):
                comment = await self._insert_comment(
                    tx,
                    task,
                    author_kind="owner",
                    author=actor,
                    session_id=None,
                    body=qrules.answer_line(q),
                    in_reply_to=None,
                )
                await repo.update_question(tx.conn, q.id, {"comment_id": comment.id})
            for a in await repo.pending_decisions(tx.conn, run.id):
                comment = await self._decision_comment(tx, task, a, actor)
                await repo.update_approval(tx.conn, a.id, {"comment_id": comment.id})
        return await self.get_run(run_id)

    async def _recover_runs(self) -> None:
        """At start: a run still `running` lost its process with the previous server."""
        async with self._tx() as tx:
            for run in await repo.open_runs(tx.conn):
                if run.status != "running":
                    continue
                task = await self.get_task(run.task_id, tx.conn)
                reason = "the server stopped while a turn ran"
                await self._set_run_status(tx, run, "lost", reason)
                await self._run_event(tx, "run.lost", "relaybay", task, run.id, reason=reason)
                await self._end_run_session(tx, run)
            await repo.lose_open_turns(tx.conn, tx.now)

    async def _run_hook_session(self, tx: _Tx, hook: HookEvent) -> Session | None:
        """The run's session row for a SessionStart that carries its id: only while one of its
        turns runs, and only for its own Claude Code session (none known yet, or this one). A
        `claude` started inside the turn inherits the variable and gets None."""
        assert hook.run_id is not None
        run = await repo.get_run(tx.conn, hook.run_id)
        if run is None or not run.is_open or run.session_id is None:
            return None
        if await repo.active_turn(tx.conn, run.id) is None:
            return None
        session = await repo.get_session_by_id(tx.conn, run.session_id)
        if session is None:
            return None
        cc_id = hook.cc_session_id
        known = {run.claude_session_id, session.cc_session_id} - {None}
        if known and cc_id not in known:
            return None
        return await self._link_hook_session(tx, session, cc_id, from_window=True)

    # --- questions to the owner -----------------------------------------------

    async def get_question(
        self, question_id: int, conn: aiosqlite.Connection | None = None
    ) -> Question:
        found = await repo.get_question(conn or self._r, question_id)
        if found is None:
            raise NotFoundError(f"Question {question_id} not found.")
        return found

    async def _question_event(
        self, tx: _Tx, kind: str, actor: str, task: Task, q: Question, **payload: Any
    ) -> None:
        await tx.event(
            kind,
            actor,
            project_id=task.project_id,
            task_id=task.id,
            session_id=q.session_id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            question_id=q.id,
            run_id=q.run_id,
            question_kind=q.kind,
            blocking=q.blocking,
            text=q.text,
            asked_by=q.asked_by,
            **payload,
        )

    async def _set_card(self, tx: _Tx, task: Task, actor: str, **changes: Any) -> None:
        await self._update_task_tx(
            tx,
            task.id,
            changes,
            expected_version=None,
            session=None,
            session_title=None,
            hook_session=None,
            actor=actor,
        )

    async def _card_waits_on_owner(self, tx: _Tx, task: Task, q: Question, actor: str) -> None:
        await self._set_card(
            tx,
            task,
            actor,
            waiting_on=self.owner_names[0],
            next_step=f'Answer question {q.id} on the board: "{qrules.quoted(q.text)}"',
        )

    async def _card_after_question(
        self, tx: _Tx, task: Task, q: Question, next_step: str, actor: str
    ) -> None:
        """Hand the card back to the agent once no blocking question of it waits on the owner,
        unless someone else already changed who it waits on."""
        if not q.blocking:
            return
        task = await self.get_task(task.id, tx.conn)
        owners = {name.strip().casefold() for name in self.owner_names}
        if (task.waiting_on or "").strip().casefold() not in owners:
            return
        others = await repo.open_blocking_questions(tx.conn, task_id=task.id)
        if others:
            # Still waiting on the owner: the next step names a question that is still open.
            await self._card_waits_on_owner(tx, task, others[0], actor)
            return
        requests = await repo.open_approvals(tx.conn, task_id=task.id)
        if requests:
            await self._card_waits_for_approval(tx, task, requests[0], actor)
            return
        await self._set_card(tx, task, actor, waiting_on=q.asked_by, next_step=next_step)

    async def _run_waits(
        self,
        tx: _Tx,
        run: Run,
        task: Task,
        blocking: list[Question],
        why: str | None,
        requests: list[Approval] | None = None,
    ) -> None:
        """A turn ended while blocking questions or approval requests of the run are open: the
        run waits for the owner, and so does the card."""
        requests = requests or []
        ids = ", ".join(str(q.id) for q in blocking)
        approval_ids = ", ".join(str(a.id) for a in requests)
        parts = []
        if blocking:
            noun = "question" if len(blocking) == 1 else "questions"
            parts.append(f"the owner's answer to {noun} {ids}")
        if requests:
            noun = "approval" if len(requests) == 1 else "approvals"
            parts.append(f"the owner's decision on {noun} {approval_ids}")
        reason = "waiting for " + " and ".join(parts)
        if why:
            reason = f"{why}; {reason}"
        await self._set_run_status(tx, run, "waiting", reason)
        await self._run_event(
            tx,
            "run.waiting",
            "relaybay",
            task,
            run.id,
            reason=reason,
            questions=ids,
            approvals=approval_ids,
        )
        if blocking:
            await self._card_waits_on_owner(tx, task, blocking[0], "relaybay")
        else:
            await self._card_waits_for_approval(tx, task, requests[0], "relaybay")

    async def _answers_delivered(
        self, tx: _Tx, carried: list[Question], session_name: str | None
    ) -> None:
        """The owner's answers reached the agent: a turn's process started with them, or the
        hooks handed over the owner comment that carries them."""
        for q in carried:
            await repo.update_question(tx.conn, q.id, {"delivered_at": tx.now})
            task = await self.get_task(q.task_id, tx.conn)
            await self._question_event(
                tx, "question.delivered", session_name or q.asked_by, task, q, session=session_name
            )

    async def _take_waiting_answers(self, session: Session) -> list[str]:
        """For a run's running turn: one line per owner's answer or decision queued as its next
        turn, told once. Only a read when nothing waits, as on every tool call."""
        if session.kind != "run":
            return []
        if not await repo.untold_answers(self._r, session.id) and not await repo.untold_decisions(
            self._r, session.id
        ):
            return []
        async with self._tx() as tx:
            waiting = await repo.untold_answers(tx.conn, session.id)
            for q in waiting:
                await repo.update_question(tx.conn, q.id, {"told_at": tx.now})
            decided = await repo.untold_decisions(tx.conn, session.id)
            for a in decided:
                await repo.update_approval(tx.conn, a.id, {"told_at": tx.now})
        return [qrules.waiting_line(q) for q in waiting] + [arules.waiting_line(a) for a in decided]

    async def ask_question(
        self,
        ref: str,
        text: str,
        default: str,
        *,
        options: list[str] | None = None,
        blocking: bool | None = None,
        default_after: str | None = None,
        kind: str | None = None,
        url: str | None = None,
        account: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Question, str]:
        """`question_ask`: a question for the owner on a card, with its default. A run's session
        asks for its run; any other session for itself. Returns the question and what the agent
        does next."""
        if (session or "").strip() in ("", "-"):
            raise ValidationError(
                "question_ask needs your session: the answer goes back to who asked. Nothing asked."
            )
        what = qrules.clean_kind(kind)
        body = qrules.clean_text(text)
        fallback = qrules.clean_default(default)
        link: str | None = None
        login: str | None = None
        if what == "look":
            if blocking is False:
                raise ValidationError("A look question is always blocking. Nothing asked.")
            if qrules.clean_options(options) not in ([], list(qrules.LOOK_OPTIONS)):
                raise ValidationError(
                    "A look question's options are fine and remarks; leave options out."
                )
            choices: list[str] = []
            stops = True
            link = qrules.clean_url(url)
            login = qrules.clean_account(account)
        else:
            if (url or "").strip() not in ("", "-") or (account or "").strip() not in ("", "-"):
                raise ValidationError("url and account are for look questions (kind look).")
            choices = qrules.clean_options(options)
            stops = bool(blocking)
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if task.stage == Stage.DONE:
                raise ValidationError(f"{task.key} is done: nothing to ask on it.")
            due = qrules.deadline(default_after, stops, datetime.fromisoformat(tx.now))
            project = await self.get_project(task.project_slug, tx.conn)
            rules = await self._secret_rules(tx.conn, project)
            _scan(rules, text=body, default=fallback, url=link, account=login)
            for option in choices:
                envs.check_secret("option", option, rules)
            if await repo.count_open_questions(tx.conn, task.id) >= qrules.OPEN_MAX:
                raise ValidationError(
                    f"{task.key} already has {qrules.OPEN_MAX} open questions: wait for the owner,"
                    " or withdraw one with question_withdraw. Nothing asked."
                )
            caller = await self._session(tx, session, session_title, hook_session)
            assert caller is not None
            run = await repo.run_of_session(tx.conn, caller.id) if caller.kind == "run" else None
            if run is not None and not run.is_open:
                run = None
            if run is not None and run.task_id != task.id:
                raise ValidationError(
                    f"Run {run.id} works on {run.task_key}: ask on that card. Nothing asked."
                )
            question_id = await repo.insert_question(
                tx.conn,
                {
                    "task_id": task.id,
                    "run_id": run.id if run else None,
                    "session_id": caller.id,
                    "asked_by": caller.name,
                    "kind": what,
                    "text": body,
                    "options": choices,
                    "default_answer": fallback,
                    "blocking": int(stops),
                    "look_url": link,
                    "look_account": login,
                    "default_after": due,
                },
                tx.now,
            )
            q = await self.get_question(question_id, tx.conn)
            await self._question_event(
                tx,
                "question.asked",
                caller.name,
                task,
                q,
                session=caller.name,
                options=q.choices,
                default=fallback,
                default_after=due,
                url=link,
                account=login,
            )
            if stops and run is None:
                # A desktop session ends its turn itself; a run waits at its turn's result.
                await self._card_waits_on_owner(tx, task, q, caller.name)
        return q, qrules.asked_hint(q, run is not None)

    async def list_questions(
        self, ref: str | None = None, status: str | None = None, limit: int = 100
    ) -> tuple[Task | None, list[Question]]:
        """`question_list`: the questions of all cards, or of one, with one status (`open` by
        default) or `all`; newest first."""
        await self.close_overdue_questions()
        wanted = (status or "").strip().lower() or "open"
        if wanted not in (*qrules.STATUSES, "all"):
            raise ValidationError(
                f"status must be one of {', '.join(qrules.STATUSES)} or all; got {wanted!r}."
            )
        task = await self.get_task(ref) if ref else None
        found = await repo.list_questions(
            self._r,
            task_id=task.id if task else None,
            statuses=None if wanted == "all" else (wanted,),
            limit=max(1, min(limit, 200)),
        )
        return task, found

    async def open_questions(self) -> list[Question]:
        """The Inbox's questions: the open ones of all cards, the oldest first."""
        await self.close_overdue_questions()
        found = await repo.list_questions(self._r, statuses=("open",))
        return sorted(found, key=lambda q: q.id)

    async def card_questions(self, task: Task) -> qrules.QuestionView:
        """The card's questions: the open ones, oldest first, and the closed ones, newest first."""
        await self.close_overdue_questions()
        every = await repo.list_questions(self._r, task_id=task.id, limit=50)
        return qrules.QuestionView(
            open=sorted((q for q in every if q.is_open), key=lambda q: q.id),
            closed=[q for q in every if not q.is_open],
        )

    async def withdraw_question(
        self,
        question_id: int,
        *,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> Question:
        """`question_withdraw`: only the asking session or a coordinator, only an open one."""
        async with self._tx() as tx:
            # Asked before the call's own hook_session is linked, or it would prove itself.
            coordinator = await self._is_coordinator(tx.conn, session, hook_session)
            q = await self.get_question(question_id, tx.conn)
            caller = await self._session(tx, session, session_title, hook_session)
            if caller is None or (caller.id != q.session_id and not coordinator):
                raise ValidationError(
                    f"Only the session that asked question {q.id} ({q.asked_by}) or a coordinator"
                    " withdraws it. Nothing changed."
                )
            if not q.is_open:
                raise ValidationError(f"Question {q.id} is {q.status}: only an open one goes.")
            task = await self.get_task(q.task_id, tx.conn)
            await repo.update_question(
                tx.conn,
                q.id,
                {"status": "withdrawn", "withdrawn_at": tx.now, "withdrawn_by": caller.name},
            )
            q = await self.get_question(q.id, tx.conn)
            await self._question_event(tx, "question.withdrawn", caller.name, task, q)
            run = await repo.get_run(tx.conn, q.run_id) if q.run_id else None
            if (
                run is not None
                and run.status == "waiting"
                and not await repo.open_blocking_questions(tx.conn, run_id=run.id)
                and not await repo.open_approvals(tx.conn, run_id=run.id)
            ):
                await self._set_run_status(tx, run, "idle", f"question {q.id} withdrawn")
            await self._card_after_question(
                tx, task, q, f"{q.asked_by} goes on: question {q.id} withdrawn", caller.name
            )
        return q

    async def answer_question(
        self,
        question_id: int,
        *,
        choice: str | None = None,
        text: str | None = None,
        actor: str = "owner",
    ) -> Question:
        """The owner's answer on the board. A run gets it as its next turn, at once when no turn
        runs, else when the running one ends (or after Back to the program); a desktop session
        and an ended run get it as an owner comment through the hooks."""
        q = await self.get_question(question_id)
        if not q.answerable:
            raise ValidationError(f"Question {q.id} is {q.status}: it takes no answer.")
        answer = qrules.compose_answer(q, choice, text)
        runs = self.runs
        run = await repo.get_run(self._r, q.run_id) if q.run_id else None
        if (
            run is not None
            and run.is_open
            and runs is not None
            and not run.with_owner
            and not runs.is_running(run.id)
            and run.id not in runs.checked_since_start
        ):
            await self._run_preflight(runs)  # the first turn after a restart checks again
        launch: tuple[int, int] | None = None
        async with self._tx() as tx:
            q = await self.get_question(question_id, tx.conn)
            if not q.answerable:
                raise ValidationError(f"Question {q.id} is {q.status}: it takes no answer.")
            task = await self.get_task(q.task_id, tx.conn)
            await repo.update_question(
                tx.conn,
                q.id,
                {
                    "status": "answered",
                    "answer": answer,
                    "answered_by": "owner",
                    "answered_at": tx.now,
                },
            )
            q = await self.get_question(q.id, tx.conn)
            await self._question_event(
                tx,
                "question.answered",
                actor,
                task,
                q,
                answer=answer,
                change_of_course=q.defaulted_at is not None,
            )
            run = await repo.get_run(tx.conn, q.run_id) if q.run_id else None
            if run is not None and run.is_open:
                turn_id, started = await self._queue_turn(tx, run, task, None, actor)
                if started:
                    launch = (run.id, turn_id)
            else:
                comment = await self._insert_comment(
                    tx,
                    task,
                    author_kind="owner",
                    author=actor,
                    session_id=None,
                    body=qrules.answer_line(q),
                    in_reply_to=None,
                )
                await repo.update_question(tx.conn, q.id, {"comment_id": comment.id})
            await self._card_after_question(
                tx,
                task,
                q,
                f"{q.asked_by} goes on with the owner's answer to question {q.id}",
                actor,
            )
        if launch is not None:
            await self._spawn_turn(*launch)
        return await self.get_question(question_id)

    async def close_overdue_questions(self) -> int:
        """Non-blocking questions past their deadline close with their default, visibly. Asked
        on reads and by the server's loop; the deadline is in the database, so a restart only
        delays it."""
        if not await repo.overdue_questions(self._r, now_iso()):
            return 0
        async with self._tx() as tx:
            due = await repo.overdue_questions(tx.conn, tx.now)
            for q in due:
                await repo.update_question(
                    tx.conn,
                    q.id,
                    {
                        "status": "defaulted",
                        "answer": q.default,
                        "answered_by": "default",
                        "answered_at": tx.now,
                        "defaulted_at": tx.now,
                    },
                )
                task = await self.get_task(q.task_id, tx.conn)
                await self._question_event(
                    tx, "question.defaulted", "relaybay", task, q, default=q.default
                )
        return len(due)

    async def question_loop(self, interval: float = qrules.CHECK_SECONDS) -> None:
        """The server's check for questions past their deadline."""
        while True:
            try:
                await self.close_overdue_questions()
            except Exception:
                logger.exception("Closing questions past their deadline failed; trying again")
            await asyncio.sleep(interval)

    # --- approvals ------------------------------------------------------------------------------

    async def get_approval(
        self, approval_id: int, conn: aiosqlite.Connection | None = None
    ) -> Approval:
        found = await repo.get_approval(conn or self._r, approval_id)
        if found is None:
            raise NotFoundError(f"Approval {approval_id} not found.")
        return found

    async def _approval_event(
        self, tx: _Tx, kind: str, actor: str, task: Task, a: Approval, **payload: Any
    ) -> None:
        await tx.event(
            kind,
            actor,
            project_id=task.project_id,
            task_id=task.id,
            session_id=a.session_id,
            project=task.project_slug,
            key=task.key,
            stage=task.stage.value,
            approval_id=a.id,
            run_id=a.run_id,
            action=a.action,
            target=a.target,
            what=a.what,
            requested_by=a.requested_by,
            **payload,
        )

    async def _card_waits_for_approval(self, tx: _Tx, task: Task, a: Approval, actor: str) -> None:
        await self._set_card(
            tx,
            task,
            actor,
            waiting_on=self.owner_names[0],
            next_step=f"Decide approval {a.id} on the board: {a.what}",
        )

    async def _card_after_approval(
        self, tx: _Tx, task: Task, a: Approval, next_step: str, actor: str
    ) -> None:
        """Hand the card back to the agent once nothing of it waits on the owner, unless someone
        else already changed who it waits on."""
        task = await self.get_task(task.id, tx.conn)
        owners = {name.strip().casefold() for name in self.owner_names}
        if (task.waiting_on or "").strip().casefold() not in owners:
            return
        questions = await repo.open_blocking_questions(tx.conn, task_id=task.id)
        if questions:
            await self._card_waits_on_owner(tx, task, questions[0], actor)
            return
        requests = await repo.open_approvals(tx.conn, task_id=task.id)
        if requests:
            await self._card_waits_for_approval(tx, task, requests[0], actor)
            return
        await self._set_card(tx, task, actor, waiting_on=a.requested_by, next_step=next_step)

    async def _close_owner_items(self, tx: _Tx, task: Task, actor: str) -> None:
        """A card that goes to done takes nothing more from the owner: its open questions are
        withdrawn, its open requests and approved, unused approvals expire (each with its
        event), and a run that waited only for them goes idle."""
        reason = "the card went to done"
        for q in await repo.list_questions(tx.conn, task_id=task.id, statuses=("open",)):
            await repo.update_question(
                tx.conn,
                q.id,
                {"status": "withdrawn", "withdrawn_at": tx.now, "withdrawn_by": "relaybay"},
            )
            q = await self.get_question(q.id, tx.conn)
            await self._question_event(tx, "question.withdrawn", actor, task, q, reason=reason)
        for a in await repo.expire_approvals(tx.conn, task.id, reason, tx.now):
            a = await self.get_approval(a.id, tx.conn)
            await self._approval_event(tx, "approval.expired", actor, task, a, reason=reason)
        run = await repo.open_run_of_task(tx.conn, task.id)
        if (
            run is not None
            and run.status == "waiting"
            and not await repo.open_blocking_questions(tx.conn, run_id=run.id)
            and not await repo.open_approvals(tx.conn, run_id=run.id)
        ):
            await self._set_run_status(tx, run, "idle", reason)

    async def _decision_comment(self, tx: _Tx, task: Task, a: Approval, actor: str) -> Comment:
        return await self._insert_comment(
            tx,
            task,
            author_kind="owner",
            author=actor,
            session_id=None,
            body=arules.decision_line(a),
            in_reply_to=None,
        )

    async def _decisions_delivered(self, tx: _Tx, decided: list[Approval]) -> None:
        """The owner's decisions reached the agent: a turn's process started with them, or the
        hooks handed over the owner comment that carries them."""
        for a in decided:
            await repo.update_approval(tx.conn, a.id, {"delivered_at": tx.now})

    async def _approval_scope(
        self, conn: aiosqlite.Connection, session: Session, run: Run | None = None
    ) -> _ApprovalScope | None:
        """What an approval check of the session's calls reads: the run's card, or the cards
        bound to a desktop session (the most recently bound first); the policy of that card's
        project, the environments and the repositories of the cards' projects. None for a
        session that works on no card: there is nothing to ask on."""
        if run is None and session.kind == "run":
            run = await repo.run_of_session(conn, session.id)
            if run is not None and not run.is_open:
                run = None
        if run is not None:
            tasks = [await self.get_task(run.task_id, conn)]
        else:
            bound = await repo.open_tasks_of_session(conn, session.id)
            order = {
                task_id: i for i, task_id in enumerate(await repo.bound_task_ids(conn, session.id))
            }
            tasks = sorted(bound, key=lambda t: order.get(t.id, len(order)))
        if not tasks:
            return None
        project = await self.get_project(tasks[0].project_slug, conn)
        policy = await self._policy_of(conn, project, None)
        project_ids = list(dict.fromkeys(t.project_id for t in tasks))
        environments = await repo.list_environments(conn, project_ids)
        policies = await self._policies(conn, environments)
        folders: list[tuple[str, str]] = []
        if run is not None:
            folders += [(w.repo_name, w.path) for w in await repo.list_run_worktrees(conn, run.id)]
        for project_id in project_ids:
            folders += [(r.name, r.path) for r in await repo.list_project_repos(conn, project_id)]
        return _ApprovalScope(session, run, tasks, policy, environments, policies, folders)

    @staticmethod
    def _repository_at(folder: str | None, folders: list[tuple[str, str]]) -> str | None:
        """The repository whose folder holds `folder`: the closest one."""
        if not folder:
            return None
        target = worktrees.path_key(folder)
        best: tuple[int, str] | None = None
        for name, path in folders:
            root = worktrees.path_key(path).rstrip(os.sep)
            inside = target == root or target.startswith(root + os.sep)
            if inside and (best is None or len(root) > best[0]):
                best = (len(root), name)
        return best[1] if best else None

    async def _call_needs(
        self, call: guard.ToolCall, scope: _ApprovalScope, cwd: str | None
    ) -> list[list[arules.Need]]:
        """The actions of a tool call that need the owner's approval, per command segment (an
        MCP call is one), their targets filled in: stand changes (deploy or SQL on test or live)
        and git actions (push, PR merge, commit), both. The current branch is read with git (no
        transaction is open), never after the call moved elsewhere (`cd`, `--git-dir`)."""
        strict = scope.run is not None
        segments = (
            [(call.text, [])]
            if call.is_mcp
            else arules.segment_needs(call, scope.policy.approvals, strict)
        )
        per: list[list[arules.Need]] = [[] for _ in segments]
        for need in arules.stand_needs(call, scope.environments, lambda e: scope.policies[e.id]):
            per[need.segment].append(need)
        git: str | None = None
        for index, (_, needs) in enumerate(segments):
            for need in needs:
                folder = str(Path(cwd or "", *need.folders)) if cwd or need.folders else None
                changes: dict[str, Any] = {}
                if need.repository is None and need.located:
                    changes["repository"] = self._repository_at(folder, scope.folders)
                if not need.resolved:
                    branch: str | None = None
                    if need.located and folder:
                        git = git or worktrees.find_git()
                        if git:
                            branch = await asyncio.to_thread(worktrees.current_branch, git, folder)
                    changes["pr" if need.action == "merge" else "branch"] = branch
                per[index].append(dataclasses.replace(need, **changes))
        return per

    async def _open_request(
        self,
        tx: _Tx,
        task: Task,
        scope_run: Run | None,
        session: Session,
        need: arules.Need,
        via: str,
        summary: str,
        rollback: str | None = None,
    ) -> Approval | None:
        """A new request for `need`, or the open one for the same action and target; None when
        the card already has OPEN_MAX open (the call is still denied)."""
        for a in await repo.open_approvals(tx.conn, task_id=task.id):
            if arules.same_target(a, need):
                return a
        if len(await repo.open_approvals(tx.conn, task_id=task.id)) >= arules.OPEN_MAX:
            return None
        approval_id = await repo.insert_approval(
            tx.conn,
            {
                "task_id": task.id,
                "run_id": scope_run.id if scope_run else None,
                "session_id": session.id,
                "requested_by": session.name,
                "requested_via": via,
                "action": need.action,
                "mode": need.mode,
                "repository": need.repository,
                "branch": need.branch,
                "pr": need.pr,
                "environment_id": need.environment_id,
                "environment": need.environment,
                "summary": summary,
                "rollback": rollback,
            },
            tx.now,
        )
        a = await self.get_approval(approval_id, tx.conn)
        await self._approval_event(
            tx,
            "approval.requested",
            session.name,
            task,
            a,
            session=session.name,
            via=via,
            summary=summary,
            rollback=rollback,
        )
        if scope_run is None:
            # A desktop session ends its turn itself; a run waits at its turn's result.
            await self._card_waits_for_approval(tx, task, a, session.name)
        return a

    async def _approval_gate(self, payload: Any, hook: HookEvent) -> tuple[str, str] | None:
        """PreToolUse: a call of a run (of a desktop session only when the policy says
        `approvals.desktop`) that takes an action the policy guards needs an approved, unused
        approval of its card for each such action. Not covered: denied, and a request opens if
        none is open. Covered, and the call is that action alone: each approval is marked used
        in the same transaction, and the answer is `allow` where Claude Code would otherwise
        refuse (a run in Manual mode not with the owner), else no decision. Covered but run
        with other commands: denied, the approvals kept. Only the approval rows count."""
        call = guard.tool_call(payload)
        if call is None:
            return None
        if not _APPROVAL_HINT_RE.search(call.text) and not await repo.has_environments(self._r):
            return None
        session = await repo.get_session_by_cc(self._r, hook.cc_session_id)
        if session is None:
            return None
        scope = await self._approval_scope(self._r, session)
        if scope is None or (scope.run is None and not scope.policy.approvals.desktop):
            return None
        per = await self._call_needs(call, scope, hook.cwd)
        needs = [n for segment in per for n in segment]
        if not needs:
            return None
        unknown = next((n for n in needs if not n.known), None)
        if unknown is not None:
            return "deny", arules.unknown_reason(unknown)
        alone = all(per)
        used = arules.used_by(call.tool, guard_command(call))
        in_run = scope.run is not None
        try:
            async with self._tx() as tx:
                usable = await repo.usable_approvals(tx.conn, [t.id for t in scope.tasks])
                picked: list[Approval] = []
                missing: list[arules.Need] = []
                for need in needs:
                    match = next(
                        (a for a in usable if a not in picked and arules.covers(a, need)), None
                    )
                    if match is None:
                        missing.append(need)
                    else:
                        picked.append(match)
                if missing:
                    reasons = []
                    for need in missing:
                        opened = await self._open_request(
                            tx,
                            scope.tasks[0],
                            scope.run,
                            session,
                            need,
                            "guard",
                            f"The agent ran: {used}",
                        )
                        reasons.append(
                            arules.deny_reason(need, opened.id if opened else None, in_run)
                        )
                    return "deny", " ".join(reasons)
                if not alone:
                    return "deny", arules.alone_reason(picked)
                for a in picked:
                    if not await repo.use_approval(tx.conn, a.id, used, session.name, tx.now):
                        raise _ApprovalTaken
                    task = await self.get_task(a.task_id, tx.conn)
                    await self._approval_event(
                        tx, "approval.used", session.name, task, a, used_by=used
                    )
        except _ApprovalTaken:
            return "deny", (
                "relaybay guard: another call used this approval at the same moment. Ask again"
                " with approval_request."
            )
        run = scope.run
        if run is not None and not run.with_owner and run.permission_mode in _REFUSING_MODES:
            named = "; ".join(f"approval {a.id} ({a.what})" for a in picked)
            return "allow", f"relaybay: the owner approved this on the board: {named}."
        return None

    async def _approval_check_failed(self, payload: Any) -> tuple[str, str] | None:
        """The approval check raised: a run's call that may be a guarded action is denied, so a
        failure never lets a push through; everything else goes on as with the guard."""
        call = guard.tool_call(payload)
        if call is None or run_of_payload(payload) is None:
            return None
        return ("deny", arules.UNAVAILABLE_REASON) if arules.guarded_hint(call) else None

    async def _denied_needs(self, run: Run, result: Result) -> list[tuple[arules.Need, str]]:
        """The actions of a turn's permission denials that need an approval, with the call that
        was denied. A call the guard's level blocks anyway, one the guard cannot read, or one no
        approval rule covers (a refusal of Claude Code's own mode) gives none."""
        session = await repo.get_session_by_id(self._r, run.session_id) if run.session_id else None
        if session is None:
            return []
        scope = await self._approval_scope(self._r, session, run=run)
        if scope is None:
            return []
        found = []
        for refusal in result.refusals:
            is_mcp = refusal.tool.startswith("mcp__")
            if refusal.tool != "Bash" and (
                not is_mcp or refusal.tool.startswith(guard.OWN_TOOL_PREFIX)
            ):
                continue
            call = guard.ToolCall(refusal.tool, refusal.text or refusal.input, is_mcp)
            no_live = session.no_live_at is not None
            touches = guard.check(
                call, scope.environments, lambda env: scope.policies[env.id], no_live
            )
            top = guard.strongest(touches)
            if top is not None and top.decision in ("deny", "ask"):
                continue
            for segment in await self._call_needs(call, scope, run.cwd):
                for need in segment:
                    if need.known:
                        found.append((need, arules.used_by(refusal.tool, refusal.input)))
        return found

    async def _requests_from_denials(
        self,
        tx: _Tx,
        run: Run,
        task: Task,
        turn: RunTurn,
        denied: list[tuple[arules.Need, str]],
    ) -> None:
        """A request per denied call that needs an approval and has none open: the agent did
        not ask for it itself (nor did the guard, which opens one when it denies)."""
        if not denied or run.session_id is None:
            return
        session = await repo.get_session_by_id(tx.conn, run.session_id)
        if session is None:
            return
        for need, call in denied:
            summary = f"From a permission denial in turn {turn.seq}: {call}"
            await self._open_request(tx, task, run, session, need, "denial", summary)

    async def request_approval(
        self,
        ref: str,
        action: str,
        target: str,
        summary: str,
        *,
        rollback: str | None = None,
        session: str | None = None,
        session_title: str | None = None,
        hook_session: str | None = None,
    ) -> tuple[Approval, str, bool]:
        """`approval_request`: the agent asks the owner to approve one action on a card. An open
        request for the same action and target is updated instead of a second one. Returns the
        request, what the agent does next and whether it was an update."""
        if (session or "").strip() in ("", "-"):
            raise ValidationError(
                "approval_request needs your session: the decision goes back to who asked."
                " Nothing requested."
            )
        what = arules.clean_action(action)
        aimed = arules.parse_target(what, target)
        repository, env_name = aimed.repository, aimed.environment
        body = arules.clean_summary(summary)
        undo = arules.clean_rollback(rollback, what)
        async with self._tx() as tx:
            task = await self.get_task(ref, tx.conn)
            if task.stage == Stage.DONE:
                raise ValidationError(f"{task.key} is done: nothing to approve on it.")
            project = await self.get_project(task.project_slug, tx.conn)
            rules = await self._secret_rules(tx.conn, project)
            _scan(rules, summary=body, rollback=undo, target=str(target))
            caller = await self._session(tx, session, session_title, hook_session)
            assert caller is not None
            run = await repo.run_of_session(tx.conn, caller.id) if caller.kind == "run" else None
            if run is not None and not run.is_open:
                run = None
            if run is not None and run.task_id != task.id:
                raise ValidationError(
                    f"Run {run.id} works on {run.task_key}: ask on that card. Nothing requested."
                )
            env_id: int | None = None
            env_text: str | None = None
            if env_name is not None:
                env = await repo.find_environment(tx.conn, project.id, env_name)
                if env is None:
                    names = [e.name for e in await repo.list_environments(tx.conn, [project.id])]
                    raise NotFoundError(
                        f"{project.slug} has no environment {env_name!r}"
                        f" (it has: {', '.join(names) or 'none'}). Nothing requested."
                    )
                env_id, env_text = env.id, f"{project.slug}/{env.name}"
            if repository is not None:
                known = [r.name for r in await repo.list_project_repos(tx.conn, project.id)]
                if run is not None:
                    known += [w.repo_name for w in await repo.list_run_worktrees(tx.conn, run.id)]
                match = next((n for n in known if n.casefold() == repository.casefold()), None)
                if known and match is None:
                    raise ValidationError(
                        f"{project.slug} has no repository {repository!r} (it has:"
                        f" {', '.join(dict.fromkeys(known))}). Nothing requested."
                    )
                repository = match or repository
            need = arules.Need(
                what,
                branch=aimed.branch,
                pr=aimed.pr,
                repository=repository,
                environment_id=env_id,
                environment=env_text,
                mode=aimed.mode,
            )
            existing = next(
                (
                    a
                    for a in await repo.open_approvals(tx.conn, task_id=task.id)
                    if arules.same_target(a, need)
                ),
                None,
            )
            if existing is not None:
                await repo.update_approval(
                    tx.conn,
                    existing.id,
                    {
                        "summary": body,
                        "rollback": undo,
                        "requested_by": caller.name,
                        "session_id": caller.id,
                        "run_id": run.id if run else existing.run_id,
                        "requested_via": "agent",
                    },
                )
                a = await self.get_approval(existing.id, tx.conn)
                await self._approval_event(
                    tx,
                    "approval.requested",
                    caller.name,
                    task,
                    a,
                    session=caller.name,
                    via="agent",
                    summary=body,
                    rollback=undo,
                    updated=True,
                )
                return a, arules.requested_hint(a, run is not None), True
            opened = await self._open_request(tx, task, run, caller, need, "agent", body, undo)
            if opened is None:
                raise ValidationError(
                    f"{task.key} already has {arules.OPEN_MAX} open approval requests: wait for"
                    " the owner. Nothing requested."
                )
        return opened, arules.requested_hint(opened, run is not None), False

    async def list_approval_requests(
        self, ref: str | None = None, status: str | None = None, limit: int = 100
    ) -> tuple[Task | None, list[Approval]]:
        """`approval_list`: the approvals of all cards, or of one, with one status (`open` by
        default) or `all`; newest first."""
        wanted = (status or "").strip().lower() or "open"
        if wanted not in (*arules.STATUSES, "all"):
            raise ValidationError(
                f"status must be one of {', '.join(arules.STATUSES)} or all; got {wanted!r}."
            )
        task = await self.get_task(ref) if ref else None
        found = await repo.list_approvals(
            self._r,
            task_id=task.id if task else None,
            statuses=None if wanted == "all" else (wanted,),
            limit=max(1, min(limit, 200)),
        )
        return task, found

    async def open_approval_requests(self) -> list[Approval]:
        """The Inbox's requests: the open ones of all cards, the oldest first."""
        found = await repo.list_approvals(self._r, statuses=("open",))
        return sorted(found, key=lambda a: a.id)

    async def card_approvals(self, task: Task) -> arules.ApprovalView:
        """The card's approvals: the open ones, oldest first, and the decided ones, newest first."""
        every = await repo.list_approvals(self._r, task_id=task.id, limit=50)
        return arules.ApprovalView(
            open=sorted((a for a in every if a.is_open), key=lambda a: a.id),
            closed=[a for a in every if not a.is_open],
        )

    async def decide_approval(
        self,
        approval_id: int,
        approve: bool,
        *,
        text: str | None = None,
        actor: str = "owner",
    ) -> Approval:
        """The owner's decision on the board. A run gets it as its next turn, at once when no
        turn runs, else when the running one ends (or after Back to the program); a desktop
        session and an ended run get it as an owner comment through the hooks."""
        line = arules.clean_decision_text(text)
        a = await self.get_approval(approval_id)
        if not a.is_open:
            raise ValidationError(f"Approval {a.id} is {a.status}: it is decided already.")
        if approve and a.needs_rollback:
            raise ValidationError(
                f"Approval {a.id} is a deploy without its rollback command: refuse it, or wait"
                " for the agent to ask again with the rollback."
            )
        runs = self.runs
        run = await repo.get_run(self._r, a.run_id) if a.run_id else None
        if (
            run is not None
            and run.is_open
            and runs is not None
            and not run.with_owner
            and not runs.is_running(run.id)
            and run.id not in runs.checked_since_start
        ):
            await self._run_preflight(runs)  # the first turn after a restart checks again
        launch: tuple[int, int] | None = None
        decision = "approved" if approve else "refused"
        async with self._tx() as tx:
            a = await self.get_approval(approval_id, tx.conn)
            if not a.is_open:
                raise ValidationError(f"Approval {a.id} is {a.status}: it is decided already.")
            task = await self.get_task(a.task_id, tx.conn)
            await repo.update_approval(
                tx.conn,
                a.id,
                {
                    "status": decision,
                    "decision": decision,
                    "decided_by": actor,
                    "decided_at": tx.now,
                    "decision_text": line,
                },
            )
            a = await self.get_approval(a.id, tx.conn)
            await self._approval_event(
                tx, "approval.decided", actor, task, a, decision=decision, text=line
            )
            run = await repo.get_run(tx.conn, a.run_id) if a.run_id else None
            if run is not None and run.is_open:
                turn_id, started = await self._queue_turn(tx, run, task, None, actor)
                if started:
                    launch = (run.id, turn_id)
            else:
                comment = await self._decision_comment(tx, task, a, actor)
                await repo.update_approval(tx.conn, a.id, {"comment_id": comment.id})
            await self._card_after_approval(
                tx,
                task,
                a,
                f"{a.requested_by} goes on: the owner {decision} {a.what} (approval {a.id})",
                actor,
            )
        if launch is not None:
            await self._spawn_turn(*launch)
        return await self.get_approval(approval_id)
