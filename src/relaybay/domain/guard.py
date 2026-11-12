"""The PreToolUse guard: which environment a tool call touches and what the agent's level there
allows. A safety net against mistakes, not a sandbox: it reads only the tool call's text and
recognises an environment only by the identifiers in its access map."""

import functools
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from relaybay.domain.environments import Environment, level_rank

if TYPE_CHECKING:
    from relaybay.policy import EffectivePolicy

_I = re.IGNORECASE


class Matcher(Protocol):
    """A compiled pattern: `re.Pattern` for the built-in rules, a time-limited one for a
    policy's own patterns (`relaybay.policy.SafePattern`)."""

    def search(self, string: str, pos: int = 0) -> Any: ...


@dataclass(frozen=True, slots=True)
class Rule:
    """A heuristic for "this command changes something". `id` is stable (a policy disables a
    built-in rule by id); `name` is what the guard's reason says ("this looks like a <name>")."""

    id: str
    name: str
    capability: str | None
    command: Matcher | None
    change: Matcher


# The built-in heuristics, in one table. A rule applies when `change` follows `command` in the
# same command segment (split at `|`, `;`, `&` and newlines outside quotes). They see text only:
# a script, an alias or an unknown tool is not recognised. `command` None: the change pattern
# alone (MCP tool input).
MUTATING_RULES: tuple[Rule, ...] = (
    Rule(
        "kubectl-change",
        "kubectl change",
        "kubectl",
        re.compile(r"\bkubectl\b", _I),
        re.compile(
            r"\b(apply|create|delete|edit|patch|replace|scale|set|label|annotate|drain|cordon"
            r"|uncordon|taint|exec|cp|rollout\s+(restart|undo|pause|resume))\b",
            _I,
        ),
    ),
    Rule(
        "helm-change",
        "helm change",
        "deploy",
        re.compile(r"\bhelm\b", _I),
        re.compile(r"\b(upgrade|install|uninstall|delete|rollback)\b", _I),
    ),
    Rule(
        "ssh-remote-change",
        "remote change over ssh",
        "ssh",
        re.compile(r"\bssh\b", _I),
        re.compile(
            r"\b(rm|mv|cp|dd|tee|truncate|kill|pkill|killall|reboot|shutdown|chmod|chown|apt"
            r"|apt-get|yum|dnf|apk|pip|npm|sudo|crontab|systemctl\s+(start|stop|restart|reload"
            r"|enable|disable|kill|mask)|service\s+\S+\s+(start|stop|restart|reload)"
            r"|docker\s+(rm|rmi|stop|restart|run|kill|exec|compose|pull)|sed\s+-i)\b",
            _I,
        ),
    ),
    Rule(
        "scp-to-host",
        "file copy to a host",
        "ssh",
        re.compile(r"\b(scp|rsync)\b", _I),
        # The last argument is remote (`host:path`): copying *to* the host. From it is a read.
        re.compile(r"\s(?![a-z]+://)[^\s:'\"]+:[^\s]*\s*$", _I),
    ),
    Rule(
        "sftp-session",
        "file transfer session",
        "ssh",
        re.compile(r"\bsftp\b", _I),
        re.compile(r"\S", _I),
    ),
    Rule(
        "sql-change",
        "SQL change",
        "db",
        re.compile(
            r"\b(psql|mysql|mariadb|sqlite3|sqlcmd|mongosh|mongo|redis-cli|clickhouse-client"
            r"|cqlsh|pg_restore|mysqlimport)\b",
            _I,
        ),
        re.compile(
            r"\b(insert\s+into|update\s+[\w.\"`]+\s+set|delete\s+from|drop\s+(table|database"
            r"|schema|index|view|user|role)|alter\s+(table|database|user|role)|truncate"
            r"|create\s+(table|database|index|user|role)|grant|revoke|flushall|flushdb)\b",
            _I,
        ),
    ),
    Rule("git-push", "git push", "deploy", re.compile(r"\bgit\b", _I), re.compile(r"\bpush\b", _I)),
    Rule(
        "terraform-change",
        "terraform change",
        "deploy",
        re.compile(r"\bterraform\b", _I),
        re.compile(r"\b(apply|destroy|import)\b", _I),
    ),
)
# MCP tools whose name says they change something (their input is matched as text as well);
# the policy switches this check off with the rule id `mcp-tool-name`.
MCP_TOOL_RULE_ID = "mcp-tool-name"
RULE_IDS = (*(r.id for r in MUTATING_RULES), MCP_TOOL_RULE_ID)
MUTATING_TOOL_RE = re.compile(
    r"(^|_)(create|update|delete|remove|apply|deploy|write|exec|execute|run|restart|scale|patch"
    r"|set|put|post|insert|drop|upload|push|merge|rollback)(_|$)",
    _I,
)
# relaybay's own MCP tools mention environments by design (environment_propose).
OWN_TOOL_PREFIX = "mcp__relaybay__"
TEXT_MAX = 8 * 1024  # characters of a tool call the guard reads
SEGMENTS_MAX = 200
IDENTIFIERS_MAX = 50  # per environment
_SEGMENT_RE = re.compile(r"""(?:'[^'\n]*'|"[^"\n]*"|[^|;&\n'"]|['"])+""")
_BOUNDARY = r"A-Za-z0-9_\-"


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool: str
    text: str
    is_mcp: bool


@dataclass(frozen=True, slots=True)
class Touch:
    """One environment a tool call mentions."""

    environment: Environment
    identifier: str
    level: str
    mutating: str | None  # the name of the rule that matched, if any
    decision: str  # "allow", "log", "warn", "ask" or "deny"
    reason: str


def _strings(value: Any, out: list[str], budget: list[int]) -> None:
    if budget[0] <= 0:
        return
    if isinstance(value, str):
        out.append(value[: budget[0]])
        budget[0] -= len(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out, budget)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out, budget)


def tool_call(payload: Any) -> ToolCall | None:
    """The text the guard checks: a Bash command, or every string in an MCP tool's input."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return None
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool, str) or not isinstance(tool_input, dict):
        return None
    if tool == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return ToolCall(tool, command[:TEXT_MAX], False)
        return None
    if tool.startswith("mcp__") and not tool.startswith(OWN_TOOL_PREFIX):
        parts: list[str] = []
        _strings(tool_input, parts, [TEXT_MAX])
        return ToolCall(tool, "\n".join(parts)[:TEXT_MAX], True)
    return None


@functools.lru_cache(maxsize=1024)
def _identifier_re(identifier: str) -> re.Pattern[str]:
    """A host name, a dashed alias, a repository: anywhere as a whole token."""
    end = rf"(?![{_BOUNDARY}]|\.[A-Za-z0-9])"
    return re.compile(rf"(?<![{_BOUNDARY}.]){re.escape(identifier)}{end}", _I)


# Shell-like words of a segment: quotes keep spaces, backslash escapes (quotes removed after).
_WORD_RE = re.compile(r"""(?:"[^"]*"|'[^']*'|\\.|[^\s"'\\])+|["'\\]""")
_ASSIGNMENT_WORD_RE = re.compile(r"^[A-Za-z_]\w*=")
_WRAPPERS = frozenset({"sudo", "doas", "env", "time", "nohup", "exec", "command", "nice"})
# Options of ssh / sftp and scp that take a value (`-p 22`, `-i key`; `-p22` has it attached).
_SSH_VALUE_OPTIONS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
_REMOTE_RE = re.compile(r"^(?:[^@/\s]+@)?([^:/\s@\[\]]+):(?!//)")  # [user@]host:path
_URL_HOST_RE = re.compile(r"://(?:[^\s/@]*@)?([^\s/:@?#\[\]]+)")
_HOST_FLAG_RE = re.compile(r"^(?:--(?:kube-)?context|--host|-h)(?:=(.+))?$")
_ATTACHED_HOST_RE = re.compile(r"^-h(.+)$")


def _words(segment: str) -> list[str]:
    return [w.replace('"', "").replace("'", "") for w in _WORD_RE.findall(segment)]


def _ssh_destination(args: list[str]) -> str | None:
    """The first operand of ssh / sftp after its options."""
    skip = False
    for i, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg == "--":
            return args[i + 1] if i + 1 < len(args) else None
        if arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            for k, letter in enumerate(letters):
                if letter in _SSH_VALUE_OPTIONS:
                    skip = k == len(letters) - 1  # a value follows only when not attached
                    break
            continue
        return arg
    return None


def _host_of(word: str) -> str:
    host = word.rsplit("@", 1)[-1]
    return host.split(":", 1)[0]


@functools.lru_cache(maxsize=64)
def hosts(text: str) -> frozenset[str]:
    """The words of a tool call that name a host: the destination of ssh / sftp, the host of
    `[user@]host:path` for scp / rsync / sftp, after `--context`, `--kube-context`, `-h`,
    `--host`, and the host of a URL. Lower case."""
    found: set[str] = set()
    for segment in _segments(text):
        words = _words(segment)
        start = 0
        while start < len(words) and (
            _ASSIGNMENT_WORD_RE.match(words[start]) or words[start] in _WRAPPERS
        ):
            start += 1
        command = words[start].rsplit("/", 1)[-1].lower() if start < len(words) else ""
        args = words[start + 1 :]
        if command in ("ssh", "sftp"):
            destination = _ssh_destination(args)
            if destination:
                found.add(_host_of(destination))
        if command in ("scp", "rsync", "sftp"):
            for arg in args:
                remote = _REMOTE_RE.match(arg)
                if remote:
                    found.add(remote.group(1))
        if command not in ("ssh", "sftp", "scp"):
            for i, arg in enumerate(args):
                flag = _HOST_FLAG_RE.match(arg)
                if flag:
                    value = flag.group(1) or (args[i + 1] if i + 1 < len(args) else "")
                    found.add(value)
                elif attached := _ATTACHED_HOST_RE.match(arg):
                    found.add(attached.group(1))
        for word in words:
            found.update(m.group(1) for m in _URL_HOST_RE.finditer(word))
    return frozenset(h.lower() for h in found if h)


def mentions(text: str, identifier: str) -> bool:
    """`identifier` as a whole token (`prod` is not in `prod-eu`). A single word (`app`,
    `postgres`) is too common in text: it counts only as a host (see `hosts`)."""
    if len(identifier) < 3:
        return False
    if re.search(r"[.\-/]", identifier):
        # Most identifiers are absent: past the regex cache each would be compiled on every call.
        if identifier.casefold() not in text.casefold():
            return False
        return _identifier_re(identifier).search(text) is not None
    return identifier.lower() in hosts(text)


def _segments(text: str) -> list[str]:
    return _SEGMENT_RE.findall(text)[:SEGMENTS_MAX]


def commands(text: str) -> list[tuple[str, list[str]]]:
    """Each command segment of a call with its words from the command on: leading variable
    assignments and wrappers (`sudo`, `env`, `time`…) skipped, quotes removed."""
    found = []
    for segment in _segments(text):
        words = _words(segment)
        start = 0
        while start < len(words) and (
            _ASSIGNMENT_WORD_RE.match(words[start]) or words[start] in _WRAPPERS
        ):
            start += 1
        found.append((segment, words[start:]))
    return found


def mutating_rules(
    call: ToolCall, rules: tuple[Rule, ...] = MUTATING_RULES, tool_names: bool = True
) -> list[tuple[str, str | None]]:
    """(rule name, capability) of every mutating rule the call matches; `tool_names`: also the
    MCP tool name check."""
    found: list[tuple[str, str | None]] = []
    segments = _segments(call.text)
    for rule in rules:
        # An MCP database tool takes the SQL itself: no client command in front of it.
        optional = rule.command is None or (call.is_mcp and rule.capability == "db")
        for segment in segments:
            start = rule.command.search(segment) if rule.command is not None else None
            if start is None and not optional:
                continue
            if rule.change.search(segment, start.end() if start else 0):
                found.append((rule.name, rule.capability))
                break
    if call.is_mcp and tool_names:
        tool = call.tool.rsplit("__", 1)[-1]
        if MUTATING_TOOL_RE.search(tool):
            found.append((f"MCP tool {tool}", None))
    return found


def mutating_rule(call: ToolCall) -> tuple[str, str | None] | None:
    rules = mutating_rules(call)
    return rules[0] if rules else None


def _decision(level: str, mutating: str | None, mode: str) -> str:
    if level == "full" or (level == "read" and not mutating):
        return "allow"
    if mode == "log":
        return "log"
    if level == "ask":
        return "ask"
    return "deny" if mode == "block" else "warn"


def _reason(env: Environment, level: str, mutating: str | None, identifier: str) -> str:
    where = f"{env.project_slug} {env.name} ({env.kind})"
    if level == "none":
        what = f"{where} is off limits for agents (level none)"
    elif level == "read":
        what = f"{where} is read-only for agents; this looks like a {mutating}"
    else:
        what = f"{where} needs the owner's approval for agent actions (level ask)"
    return (
        f"relaybay guard: {what} (matched '{identifier}'). Ask the owner, or propose a change"
        " with environment_propose."
    )


# `log` records the call in the journal and tells nobody.
DECISION_ORDER = ("allow", "log", "warn", "ask", "deny")
# Not in RULE_IDS: the policy cannot disable the owner's switch on a session.
NO_LIVE_RULE = "no-live-session"


def _no_live_touch(env: Environment, identifier: str) -> Touch:
    where = f"{env.project_slug} {env.name} ({env.kind})"
    reason = (
        f"relaybay guard: the owner switched live off for this session: {where}, matched"
        f" '{identifier}'. Reads count too. Only the owner can switch it back, on the board."
    )
    return Touch(env, identifier, "none", NO_LIVE_RULE, "deny", reason)


def check(
    call: ToolCall,
    environments: list[Environment],
    policy_of: Callable[[Environment], "EffectivePolicy"],
    no_live: bool = False,
) -> list[Touch]:
    """Every environment the call mentions, with the strongest decision its level gives under
    the environment's effective policy (levels, guard mode, rules). A mutating rule counts for
    an identifier only when their capabilities agree (an identifier without one, the base URL
    host, takes any rule). `no_live`: the session's switch is on, so any touch of a live
    environment is denied, whatever its level and guard mode."""
    touches = []
    matched: dict[tuple[tuple[Rule, ...], bool], list[tuple[str, str | None]]] = {}
    seen: dict[str, bool] = {}  # identifier -> mentioned; environments share names
    for env in environments:
        policy = policy_of(env)
        mode = policy.guard_mode
        revoked = no_live and env.kind == "live"
        if mode == "off" and not revoked:
            continue
        rule_set = (policy.rules, policy.mcp_tool_names)
        if rule_set not in matched:
            matched[rule_set] = mutating_rules(call, *rule_set)
        rules = matched[rule_set]
        best: Touch | None = None
        for identifier, capability in env.identifiers()[:IDENTIFIERS_MAX]:
            key = identifier.casefold()
            if key not in seen:
                seen[key] = mentions(call.text, identifier)
            if not seen[key]:
                continue
            if revoked:
                best = _no_live_touch(env, identifier)
                break
            applicable = [
                (name, cap)
                for name, cap in rules
                if capability is None or cap is None or cap == capability
            ]
            mutating = applicable[0][0] if applicable else None
            caps = {c for c in (capability, *(cap for _, cap in applicable)) if c}
            level = min(
                (policy.level_for(env, cap) for cap in caps),
                key=level_rank,
                default=policy.level_for(env),
            )
            decision = _decision(level, mutating, mode)
            reason = _reason(env, level, mutating, identifier) if decision != "allow" else ""
            touch = Touch(env, identifier, level, mutating, decision, reason)
            if best is None or DECISION_ORDER.index(decision) > DECISION_ORDER.index(best.decision):
                best = touch
        if best is not None:
            touches.append(best)
    return touches


def strongest(touches: list[Touch]) -> Touch | None:
    """The touch whose decision wins (deny > ask > warn > allow)."""
    if not touches:
        return None
    return max(touches, key=lambda t: DECISION_ORDER.index(t.decision))
