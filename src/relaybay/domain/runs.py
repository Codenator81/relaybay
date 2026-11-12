"""Runs: the user's own `claude` started by relaybay on a card, one turn at a time.

Pure rules only: which statuses follow which, and what a run may be started with. The processes
live in `relaybay.runner`, the orchestration in the service.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relaybay.domain.errors import ValidationError

RUN_STATUSES = ("queued", "running", "waiting", "idle", "stopped", "ended", "lost")
OPEN_STATUSES = tuple(s for s in RUN_STATUSES if s != "ended")
# None: a run that does not exist yet. `queued` and `waiting` are not produced yet; they lead on
# like `idle`. A running run is stopped before it ends.
TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({"running"}),
    "running": frozenset({"idle", "stopped", "lost"}),
    "idle": frozenset({"running", "ended"}),
    "stopped": frozenset({"running", "ended"}),
    "lost": frozenset({"running", "ended"}),
    "queued": frozenset({"running", "ended"}),
    "waiting": frozenset({"running", "ended"}),
    "ended": frozenset(),
}
TURN_OUTCOMES = ("success", "error", "stopped", "lost", "cancelled", "failed_to_start")
SENT_BY_KINDS = ("owner", "coordinator", "relaybay")

# Claude Code's --permission-mode values a project may default to. `bypassPermissions` only on the
# owner's Start form, for one run, never as a project default.
PERMISSION_MODES = ("manual", "default", "acceptEdits", "plan", "auto", "dontAsk")
OWNER_ONLY_MODES = ("bypassPermissions",)
MODELS = ("opus", "sonnet", "haiku")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MODEL = "opus"
DEFAULT_EFFORT = "medium"
DEFAULT_MODE = "auto"
DEFAULT_ALLOWED_TOOLS = "mcp__relaybay"
BRIEF_MAX = 20000
FOLDER_MAX = 500
ALLOWED_RULES_MAX = 30
ALLOWED_RULE_MAX = 200
# A tool name, an MCP server or tool (`mcp__name`, `mcp__name__tool`, `*`), with an optional
# argument pattern in parentheses: `Bash(git diff *)`.
_RULE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.*-]*(\([^()\r\n]*\))?")
LOGIN_ANY_KEY = "run_login_any"


def check_transition(current: str | None, new: str) -> None:
    """Raise when a run may not go from `current` (None: not created yet) to `new`."""
    if new not in RUN_STATUSES:
        raise ValidationError(f"Unknown run status {new!r}.")
    if new not in TRANSITIONS.get(current, frozenset()):
        raise ValidationError(f"A run cannot go from {current or 'new'} to {new}.")


def split_rules(text: str) -> list[str]:
    """The rules of an `--allowedTools` text: separated by commas outside parentheses."""
    rules, depth, current = [], 0, []
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            rules.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    rules.append("".join(current).strip())
    return [r for r in rules if r]


def clean_allowed_tools(value: Any) -> str:
    """The rules joined by ", "; each one is passed with its own --allowedTools flag."""
    rules = split_rules(str(value or ""))
    if not rules:
        raise ValidationError(
            "allowed tools must name at least one rule, e.g. mcp__relaybay or Bash(git diff *)."
        )
    if len(rules) > ALLOWED_RULES_MAX:
        raise ValidationError(f"allowed tools take at most {ALLOWED_RULES_MAX} rules.")
    for rule in rules:
        if len(rule) > ALLOWED_RULE_MAX or not _RULE_RE.fullmatch(rule):
            raise ValidationError(
                f"{rule[:60]!r} is not a tool rule: use a tool name, mcp__server, or"
                " Tool(pattern) such as Bash(git diff *)."
            )
    return ", ".join(rules)


def _choice(value: Any, name: str, choices: tuple[str, ...], default: str) -> str:
    text = str(value or "").strip() or default
    if text not in choices:
        raise ValidationError(f"{name} must be one of: {', '.join(choices)}; got {text!r}.")
    return text


def clean_model(value: Any) -> str:
    return _choice(value, "model", MODELS, DEFAULT_MODEL)


def clean_effort(value: Any) -> str:
    return _choice(value, "effort", EFFORTS, DEFAULT_EFFORT)


def clean_mode(value: Any, *, owner: bool) -> str:
    """A project default is never `bypassPermissions`; the owner's Start form may pick it."""
    choices = PERMISSION_MODES + (OWNER_ONLY_MODES if owner else ())
    return _choice(value, "permission mode", choices, DEFAULT_MODE)


def clean_folder(value: Any) -> str | None:
    """The project folder a run may use as its cwd: an absolute path on this machine, kept as
    written; empty (or "-") clears it. Whether it exists is checked at Start."""
    text = str(value or "").strip()
    if text in ("", "-"):
        return None
    if len(text) > FOLDER_MAX:
        raise ValidationError(f"The project folder is longer than {FOLDER_MAX} characters.")
    if any(c in text for c in "\x00\r\n"):
        raise ValidationError("The project folder holds a control character.")
    if not Path(text).is_absolute():
        raise ValidationError(f"The project folder must be an absolute path; got {text!r}.")
    return text


def clean_prompt(value: Any, name: str = "text") -> str:
    text = str(value or "").replace("\r\n", "\n").strip()
    if not text:
        raise ValidationError(f"The {name} is empty.")
    if len(text) > BRIEF_MAX:
        raise ValidationError(f"The {name} is longer than {BRIEF_MAX} characters.")
    if "\x00" in text:
        raise ValidationError(f"The {name} holds a NUL character.")
    return text


@dataclass(slots=True)
class RunDefaults:
    project_id: int
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    permission_mode: str = DEFAULT_MODE
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS
    chrome: bool = False
    folder: str | None = None
    updated_at: str | None = None
    updated_by: str | None = None

    @property
    def allowed_rules(self) -> list[str]:
        return split_rules(self.allowed_tools)


@dataclass(slots=True)
class Run:
    id: int
    task_id: int
    project_id: int
    session_id: int | None
    cwd: str
    cwd_repo_id: int | None
    model: str
    effort: str
    permission_mode: str
    allowed_tools: str
    chrome: bool
    claude_session_id: str | None
    status: str
    status_reason: str | None
    auth_method: str | None
    hooks_source: str
    mcp_source: str
    started_by: str
    started_by_kind: str
    created_at: str
    updated_at: str
    ended_at: str | None
    ended_by: str | None
    task_key: str = ""
    project_slug: str = ""
    session_name: str | None = None

    @property
    def is_open(self) -> bool:
        return self.ended_at is None

    @property
    def allowed_rules(self) -> list[str]:
        return split_rules(self.allowed_tools)


@dataclass(slots=True)
class RunTurn:
    id: int
    run_id: int
    seq: int
    prompt: str
    sent_by: str
    sent_by_kind: str
    queued_at: str
    started_at: str | None
    ended_at: str | None
    pid: int | None
    outcome: str | None
    result_subtype: str | None
    exit_code: int | None
    num_turns: int | None
    duration_ms: int | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    denials: int
    denied_tools: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        if self.outcome:
            return self.outcome
        return "running" if self.started_at else "queued"

    @property
    def tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(slots=True)
class RunWorktree:
    """A folder of a run: a worktree it created (`created_by_run`) or a repository in place."""

    id: int
    run_id: int
    project_repo_id: int | None
    repo_name: str
    source_path: str
    path: str
    branch: str | None
    created_by_run: bool
    copied: list[str] = field(default_factory=list)
    created_at: str = ""
    removed_at: str | None = None


@dataclass(slots=True)
class RunTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    denials: int = 0

    @property
    def tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


@dataclass(slots=True)
class RunView:
    run: Run
    turns: list[RunTurn]
    totals: RunTotals
    worktrees: list[RunWorktree] = field(default_factory=list)


def totals(turns: list[RunTurn]) -> RunTotals:
    out = RunTotals()
    for t in turns:
        out.input_tokens += t.input_tokens
        out.output_tokens += t.output_tokens
        out.cache_read_tokens += t.cache_read_tokens
        out.cache_write_tokens += t.cache_write_tokens
        out.denials += t.denials
    return out
