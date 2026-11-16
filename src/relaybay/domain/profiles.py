"""Permission profiles: what a run's turns start with (mode, allowed and taken-away tools) and
what a run under the profile may do on the board. Stored rows, edited by the owner on the board,
read at the start of every turn.

Pure rules only: names, validation, the shipped rows and the fixed lines no profile changes.
"""

import json
import re
import shlex
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path, PureWindowsPath
from typing import Any

from relaybay.domain.errors import ValidationError
from relaybay.domain.runs import (
    ALLOWED_RULE_MAX,
    ALLOWED_RULES_MAX,
    OWNER_ONLY_MODES,
    PERMISSION_MODES,
    RULE_RE,
    split_rules,
)

KINDS = ("card", "coordinator", "check")
DEFAULT_PROFILE = "agent"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,69}$")
DESCRIPTION_MAX = 500
# Claude Code's own scheduling tools (probe 17 of design 0023): a timer made inside a `-p` turn
# dies with the process or schedules outside relaybay. Every turn takes them away, whatever its
# profile's list says.
FIXED_DISALLOWED = ("CronCreate", "CronDelete", "ScheduleWakeup", "RemoteTrigger")
# The board rights, in the editor's order; `run_start_per_day` is a number, the rest yes / no.
# Stored and shown since 0.11's first step; the coordinator's run enforces them.
RIGHTS = (
    ("run_start", "run_start on cards in plan or dev, within slots and budgets"),
    ("run_settings", "run_settings between turns (lower only)"),
    ("tasks_write", "create and update tasks, triage the backlog and intake"),
    ("plan_accept", "plan_accept"),
    ("rules_update", "rules_update"),
    ("schedule_add", "schedule_add (one-off checks and reminders)"),
    ("messages", "messages to agents"),
)
RIGHT_NAMES = tuple(name for name, _ in RIGHTS)
PER_DAY = "run_start_per_day"
PER_DAY_MAX = 1000
# Shown read-only in the editor: what no profile changes.
FIXED_LINES = (
    "An approval is a board button of the owner.",
    "Nothing answers the owner's questions for the owner.",
    "Policy, environments, levels and the brake release have no MCP write.",
    "Live always needs an approval.",
    "relaybay never passes --bare or a skip of permissions; bypassPermissions is never a"
    " profile's mode, only the owner's choice on a card's Start form.",
    "A coordinator never starts a coordinator.",
    "Every turn takes away " + ", ".join(FIXED_DISALLOWED) + ": schedules are relaybay's own.",
)
_READ_ONLY_GIT = (
    "Bash(git log *)",
    "Bash(git status *)",
    "Bash(git diff *)",
    "Bash(git show *)",
    "Bash(gh pr view *)",
    "Bash(gh pr checks *)",
)


def _no_rights(**on: int) -> dict[str, int]:
    rights = dict.fromkeys(RIGHT_NAMES, 0)
    rights[PER_DAY] = 0
    rights.update(on)
    return rights


# The shipped rows, as migration 0043 seeds them and "Reset to shipped" restores them (a test
# checks the two agree). The coordinator's Edit and Write inside its own folder are added at
# runtime by the coordinator run, not stored here.
BUILTIN: dict[str, dict[str, Any]] = {
    "agent": {
        "description": "A run on a card: auto mode, relaybay's MCP tools allowed.",
        "permission_mode": "auto",
        "allowed_tools": "mcp__relaybay",
        "disallowed_tools": ", ".join(FIXED_DISALLOWED),
        "rights": _no_rights(messages=1),
        "kinds": ["card"],
    },
    "coordinator": {
        "description": "The coordinator run: reads, the board and read-only git and gh.",
        "permission_mode": "dontAsk",
        "allowed_tools": ", ".join(
            (
                "Read",
                "Glob",
                "Grep",
                "Task",
                "WebFetch",
                "WebSearch",
                "mcp__relaybay",
                *_READ_ONLY_GIT,
            )
        ),
        "disallowed_tools": ", ".join(FIXED_DISALLOWED),
        "rights": _no_rights(
            run_start=1,
            run_settings=1,
            tasks_write=1,
            plan_accept=1,
            rules_update=1,
            schedule_add=1,
            messages=1,
            **{PER_DAY: 10},
        ),
        "kinds": ["coordinator"],
    },
    "read-only": {
        "description": "A one-shot check: reads and read-only git and gh, nothing on the board.",
        "permission_mode": "dontAsk",
        "allowed_tools": ", ".join(("Read", "Glob", "Grep", *_READ_ONLY_GIT)),
        "disallowed_tools": ", ".join(FIXED_DISALLOWED),
        "rights": _no_rights(),
        "kinds": ["check"],
    },
}


@dataclass(slots=True)
class RunProfile:
    id: int
    name: str
    description: str
    permission_mode: str
    allowed_tools: str
    disallowed_tools: str
    rights: dict[str, int]
    kinds: list[str]
    builtin: bool
    version: int
    updated_at: str
    updated_by: str
    projects: list[str] = field(default_factory=list)  # slugs whose run defaults name it

    @property
    def allowed_rules(self) -> list[str]:
        return split_rules(self.allowed_tools)

    @property
    def disallowed_rules(self) -> list[str]:
        return split_rules(self.disallowed_tools)

    @property
    def per_day(self) -> int:
        return int(self.rights.get(PER_DAY, 0))

    def snapshot(self) -> dict[str, Any]:
        """What an Audit line keeps as its before and after."""
        return {
            "description": self.description,
            "permission_mode": self.permission_mode,
            "allowed_tools": self.allowed_tools,
            "disallowed_tools": self.disallowed_tools,
            "rights": dict(self.rights),
            "kinds": list(self.kinds),
        }


def clean_name(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not NAME_RE.match(text):
        raise ValidationError(
            f"Invalid profile name {value!r}: lowercase letters, digits and dashes, at most 70,"
            " e.g. 'agent-strict'."
        )
    return text


def clean_description(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > DESCRIPTION_MAX:
        raise ValidationError(f"The description is longer than {DESCRIPTION_MAX} characters.")
    return text


def clean_mode(value: Any) -> str:
    text = str(value or "").strip()
    if text in OWNER_ONLY_MODES:
        raise ValidationError(
            f"{text} is never a profile's mode: it stays the owner's choice for one run on the"
            " card's Start form."
        )
    if text not in PERMISSION_MODES:
        raise ValidationError(
            f"permission mode must be one of: {', '.join(PERMISSION_MODES)}; got {text!r}."
        )
    return text


def clean_rules(value: Any, name: str, *, required: bool) -> str:
    """Rules one per line (commas outside parentheses also separate), joined by ", "."""
    rules: list[str] = []
    for line in str(value or "").replace("\r\n", "\n").split("\n"):
        for rule in split_rules(line):
            if rule not in rules:
                rules.append(rule)
    if required and not rules:
        raise ValidationError(
            f"{name} must name at least one rule, e.g. mcp__relaybay or Bash(git diff *)."
        )
    if len(rules) > ALLOWED_RULES_MAX:
        raise ValidationError(f"{name} take at most {ALLOWED_RULES_MAX} rules.")
    for rule in rules:
        if len(rule) > ALLOWED_RULE_MAX or not RULE_RE.fullmatch(rule):
            raise ValidationError(
                f"{rule[:60]!r} is not a tool rule: use a tool name, mcp__server, or"
                " Tool(pattern) such as Bash(git diff *)."
            )
    return ", ".join(rules)


def clean_rights(values: dict[str, Any]) -> dict[str, int]:
    """Yes / no per right (any true value is yes) and the daily cap of run_start."""
    rights = {name: int(bool(values.get(name))) for name in RIGHT_NAMES}
    text = str(values.get(PER_DAY) if values.get(PER_DAY) is not None else 0).strip() or "0"
    if not re.fullmatch(r"[0-9]+", text) or int(text) > PER_DAY_MAX:
        raise ValidationError(
            f"run_start a day must be a whole number from 0 to {PER_DAY_MAX}; got {text!r}."
        )
    rights[PER_DAY] = int(text)
    return rights


def clean_kinds(value: Any) -> list[str]:
    given = [value] if isinstance(value, str) else list(value or [])
    kinds = [k for k in KINDS if k in {str(v).strip() for v in given}]
    unknown = sorted({str(v).strip() for v in given} - set(KINDS) - {""})
    if unknown:
        raise ValidationError(f"Run kinds are {', '.join(KINDS)}; got {', '.join(unknown)}.")
    if not kinds:
        raise ValidationError("A profile is for at least one run kind: card, coordinator, check.")
    return kinds


def turn_disallowed(stored: str | None) -> tuple[str, ...]:
    """The --disallowedTools rules of a turn: the profile's list (None: a run from before
    profiles) and then the fixed ones it lacks."""
    rules = split_rules(stored or "")
    return tuple(rules + [t for t in FIXED_DISALLOWED if t not in rules])


def rights_json(rights: dict[str, int]) -> str:
    return json.dumps(rights, sort_keys=True)


def kinds_json(kinds: list[str]) -> str:
    return json.dumps(kinds)


# A check takes these away besides the fixed ones: `dontAsk` pre-approves the user's and the
# project's own `permissions.allow` rules, and a deny beats an allow.
CHECK_DISALLOWED = ("Edit", "Write", "MultiEdit", "NotebookEdit", "PowerShell", "mcp__relaybay")
# Tools an allow rule of the file kind covers: an `Edit` rule covers every file write.
_FILE_TOOLS = {
    "Edit": ("Edit", "Write", "MultiEdit", "NotebookEdit"),
    "Read": ("Read",),
    "Write": ("Write",),
    "NotebookEdit": ("NotebookEdit",),
    "Glob": ("Glob",),
    "Grep": ("Grep",),
}
# A Bash command with one of these runs more than the one command a rule names.
_SHELL_CHAINING = re.compile(r"[;&|`<>$\r\n]")
# Options of the read-only git and gh commands that write, run something or wait: git
# accepts any unique prefix of a long option (`--outp=x`), so a prefix of one is refused too.
REFUSED_OPTIONS = (
    "--output",
    "--ext-diff",
    "--textconv",
    "--config-env",
    "--exec",
    "--exec-path",
    "--upload-pack",
    "--receive-pack",
    "--open-files-in-pager",
    "--paginate",
    "--web",
    "--watch",
)
# `-c name=value` (also `-cname=value`) sets any git config, a pager or a hook among them.
REFUSED_SHORT = ("-c",)
# Short forms that mean one of REFUSED_OPTIONS only for one program (`gh -w` is `--web`;
# `git log -w` ignores whitespace and stays allowed).
REFUSED_SHORT_OF = {"gh": ("-w",)}


def _call_path(tool_input: dict[str, Any], cwd: str | None, windows: bool) -> str | None:
    raw = tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    given = Path(raw)
    # Resolved (`..` and links followed): a path written to look inside a folder is not.
    resolved = str((given if given.is_absolute() or cwd is None else Path(cwd) / given).resolve())
    if windows:
        win = PureWindowsPath(resolved)
        drive = win.drive.rstrip(":").lower()
        parts = [p for p in win.parts[1:] if p not in ("\\", "/")]
        return ("/" + "/".join([drive, *parts])).lower()
    return resolved


def _refused_option(command: str) -> bool:
    """A word of the command is one of REFUSED_OPTIONS (or a prefix of one, with or without
    `=value`) or a short option of REFUSED_SHORT; a command that does not split is refused."""
    try:
        words = shlex.split(command)
    except ValueError:
        return True
    for word in words[1:]:
        if word.startswith("--") and len(word) > 2:
            name = word.split("=", 1)[0]
            if any(option.startswith(name) for option in REFUSED_OPTIONS):
                return True
        elif word.startswith("-") and not word.startswith("--"):
            shorts = REFUSED_SHORT + REFUSED_SHORT_OF.get(words[0], ())
            if any(word.startswith(short) for short in shorts):
                return True
    return False


def call_allowed(
    rules: list[str],
    tool: str,
    tool_input: dict[str, Any],
    *,
    cwd: str | None = None,
    windows: bool = False,
) -> bool:
    """Whether a tool call matches one of a profile's allow rules: a tool name (`*` globs), an
    MCP server (`mcp__server`) or tool, or `Tool(pattern)` with the pattern globbed against the
    Bash command (one command only: no chaining, pipes or redirection) or the file's resolved
    path (`//` anchors at the root; an `Edit` rule covers Write and NotebookEdit)."""
    for rule in rules:
        found = re.fullmatch(r"([^()]+)\((.*)\)", rule)
        if found is None:
            if rule.startswith("mcp__") and "__" not in rule[5:]:
                if tool == rule or tool.startswith(rule + "__"):
                    return True
            elif fnmatchcase(tool, rule):
                return True
            continue
        name, pattern = found[1], found[2]
        if name == "Bash":
            command = tool_input.get("command")
            if tool != "Bash" or not isinstance(command, str):
                continue
            command = command.strip()
            if _SHELL_CHAINING.search(command) or _refused_option(command):
                continue
            bare = pattern[:-2] if pattern.endswith(" *") else None
            if fnmatchcase(command, pattern) or command == bare:
                return True
            continue
        if tool not in _FILE_TOOLS.get(name, (name,)):
            continue
        path = _call_path(tool_input, cwd, windows)
        if path is None or not pattern.startswith("//"):
            continue
        wanted = pattern[1:].lower() if windows else pattern[1:]
        if fnmatchcase(path, wanted) or fnmatchcase(path + "/", wanted):
            return True
    return False
