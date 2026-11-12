"""Claude Code hook events: which ones relaybay reads and what they mean for a session."""

import re
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

from relaybay.domain.errors import ValidationError
from relaybay.domain.sessions import parse_session_id

HOOK_PATH = "/hooks/claude-code"
# Opt-in wake-up hook (`relaybay setup --wake`): an `asyncRewake` command hook started at Stop
# that long-polls this path and exits 2 when an owner comment arrives for the idle session.
WAKE_PATH = HOOK_PATH + "/wait"
WAKE_HOLD_SECONDS = 1500.0

# Event name -> session status. Anything else is ignored.
HOOK_STATUS = {
    "SessionStart": "working",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Notification": "waiting",
    "Stop": "idle",
    "SessionEnd": "ended",
}
HOOK_EVENTS = tuple(HOOK_STATUS)
# Events whose answer may carry `hookSpecificOutput.additionalContext` with owner comments.
DELIVERY_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse")
CC_ID_MAX = 200
ACTIVITY_MAX = 200
# The Claude desktop app runs Claude Code with its own session id (`local_<uuid>`) in this
# variable; hooks inherit it. The forwarder adds it to SessionStart as HOST_SESSION_FIELD (http
# hooks send it as HOST_SESSION_HEADER), so a new Claude Code id after /clear or a restart is
# linked to the desktop session it belongs to.
HOST_SESSION_ENV = "CLAUDE_CODE_HOST_SESSION_ID"
HOST_SESSION_FIELD = "relaybay_host_session"
HOST_SESSION_HEADER = "X-Relaybay-Host-Session"
# "1" in the desktop window's own Claude Code; a `claude -p` started from one of its tools
# inherits HOST_SESSION_ENV but runs with "0" here, so it never takes the window's link.
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"
ATTENDED_FIELD = "relaybay_host_attended"
ATTENDED_HEADER = "X-Relaybay-Host-Attended"
# A run relaybay started (`relaybay.runner`) has its id in this variable; the forwarder adds it to
# SessionStart as RUN_FIELD (http hooks send it as RUN_HEADER), so the turn's hooks land on the
# run's own session row.
RUN_ID_ENV = "RELAYBAY_RUN_ID"
RUN_FIELD = "relaybay_run"
RUN_HEADER = "X-Relaybay-Run"
RUN_ID_MAX_DIGITS = 12
# SessionEnd reasons after which the same window goes on with a new Claude Code id.
CONTINUED_END_REASONS = ("clear", "resume")
# The forwarder's spool: an event the server did not take is sent again later with the time it
# happened in this field (UTC, ISO 8601). Such an event never changes a session that has seen
# newer activity, and it gets no answer (comments are not delivered to a replay).
SPOOLED_FIELD = "relaybay_spooled_at"
TRANSCRIPT_PATH_MAX = 4096
RECIPIENT_MAX = 120
# The tools whose `to` names another session. Any other tool's `to` may be a mail address or a
# file: it says nothing about who talks to whom.
MESSAGE_TOOLS = ("SendMessage",)
# An address or an address list is not a session. `name@team` still counts.
_NOT_A_RECIPIENT = re.compile(r"@[^@\s]+\.[^@\s]+|[<,;]")
# The desktop tool that starts a session; its `PostToolUse` answer names the new one.
LAUNCH_TOOLS = ("mcp__ccd_session__start_session",)
_LAUNCHED_ID = re.compile(r"local_[0-9a-f-]{36}", re.IGNORECASE)
# How much of a launch answer is searched: strings only, this many characters and this deep.
LAUNCH_TEXT_MAX = 64 * 1024
_LAUNCH_DEPTH_MAX = 6


@dataclass(frozen=True, slots=True)
class HookEvent:
    cc_session_id: str
    event: str
    status: str
    cwd: str | None
    activity: str | None
    end_reason: str | None = None
    stop_hook_active: bool = False  # Stop: Claude Code is already continuing for a stop hook
    source: str | None = None  # SessionStart: startup, resume, clear or compact
    host_session: str | None = None  # SessionStart: the desktop session id (`local_<uuid>`)
    # SessionStart: True in the desktop window itself, False when it says it runs unattended ("0"),
    # None when the field is empty or missing (older desktop apps may not send it).
    attended: bool | None = None
    transcript_path: str | None = None  # the session's transcript file (`*.jsonl`)
    spooled_at: str | None = None  # a replay from the forwarder's spool: when it happened (UTC)
    run_id: int | None = None  # SessionStart: the relaybay run the process was started for

    @property
    def nested(self) -> bool:
        """A `claude` started from a desktop session's tools: it inherits the session's id."""
        return self.host_session is not None and self.attended is False

    @property
    def activity_only(self) -> bool:
        """A working session doing the next thing: coalesced, not a status change."""
        return self.event in ("UserPromptSubmit", "PreToolUse", "PostToolUse")


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def _host_session(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_session_id(value)
    except ValidationError:
        return None


def _attended(value: Any) -> bool | None:
    if value == "1":
        return True
    return False if value == "0" else None


def _run_id(value: Any) -> int | None:
    if not isinstance(value, str) or not value.isdigit() or len(value) > RUN_ID_MAX_DIGITS:
        return None
    return int(value)


def _transcript_path(value: Any) -> str | None:
    """The path as sent, not collapsed like other text: only one too long or not `*.jsonl` is
    refused here. What it points at is checked before it is stored and before it is read."""
    if not isinstance(value, str) or len(value) > TRANSCRIPT_PATH_MAX:
        return None
    if not value.lower().endswith(".jsonl") or not value.isprintable():
        return None
    return value


def _spooled_at(value: Any) -> str | None:
    """The spooled event's time in the server's own format, or None when it is not one."""
    # Imported here: the forwarder imports this module and never parses a time.
    from datetime import UTC, datetime

    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def parse_message_recipient(payload: Any) -> str | None:
    """The recipient of a message tool's call (`MESSAGE_TOOLS`): `tool_input.to` of a `PreToolUse`,
    capped and cleaned. Nothing else of the tool's input is read, so the message text never gets
    further than this request."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
        return None
    if payload.get("tool_name") not in MESSAGE_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    recipient = tool_input.get("to") if isinstance(tool_input, dict) else None
    if not isinstance(recipient, str):
        return None
    clean = " ".join("".join(c for c in recipient if c.isprintable()).split())[:RECIPIENT_MAX]
    return clean if clean and not _NOT_A_RECIPIENT.search(clean) else None


def _strings(value: Any, out: list[str], budget: list[int], depth: int = 0) -> None:
    if budget[0] <= 0 or depth > _LAUNCH_DEPTH_MAX:
        return
    if isinstance(value, str):
        out.append(value[: budget[0]])
        budget[0] -= len(out[-1])
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out, budget, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out, budget, depth + 1)


def parse_launched_session(payload: Any) -> str | None:
    """The desktop session id (`local_<uuid>`) a launch tool's call (`LAUNCH_TOOLS`) started: the
    first one in `tool_response` of a `PostToolUse`, which may be a string, an object or a list of
    content blocks. Nothing else of the input or the answer is read or kept."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostToolUse":
        return None
    if payload.get("tool_name") not in LAUNCH_TOOLS:
        return None
    texts: list[str] = []
    _strings(payload.get("tool_response"), texts, [LAUNCH_TEXT_MAX])
    for text in texts:
        found = _LAUNCHED_ID.search(text)
        if found is not None:
            return _host_session(found.group(0))
    return None


def parse_hook(payload: Any) -> HookEvent | None:
    """The part of a hook's input JSON relaybay uses, or None for an event it ignores. The
    prompt text and tool input are never kept."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    cc_id = _text(payload.get("session_id"), CC_ID_MAX)
    if event not in HOOK_STATUS or cc_id is None:
        return None
    source = _text(payload.get("source"), 40) if event == "SessionStart" else None
    if event == "SessionStart":
        activity = f"started ({source or 'startup'})"
    elif event == "UserPromptSubmit":
        activity = "prompt"
    elif event in ("PreToolUse", "PostToolUse"):
        activity = _text(payload.get("tool_name"), ACTIVITY_MAX) or "tool"
    elif event == "Notification":
        activity = _text(payload.get("message"), ACTIVITY_MAX) or "needs you"
    elif event == "Stop":
        activity = "turn finished"
    else:
        activity = "session ended"
    return HookEvent(
        cc_session_id=cc_id,
        event=event,
        status=HOOK_STATUS[event],
        cwd=_text(payload.get("cwd"), 4096),
        activity=activity,
        end_reason=_text(payload.get("reason"), 40) if event == "SessionEnd" else None,
        stop_hook_active=event == "Stop" and payload.get("stop_hook_active") is True,
        source=source,
        host_session=(
            _host_session(payload.get(HOST_SESSION_FIELD)) if event == "SessionStart" else None
        ),
        attended=_attended(payload.get(ATTENDED_FIELD)) if event == "SessionStart" else None,
        transcript_path=_transcript_path(payload.get("transcript_path")),
        spooled_at=_spooled_at(payload.get(SPOOLED_FIELD)),
        run_id=_run_id(payload.get(RUN_FIELD)) if event == "SessionStart" else None,
    )


def folder_name(cwd: str | None) -> str:
    """Last path component of a working directory, for either path style."""
    if not cwd:
        return "claude-code"
    name = PureWindowsPath(cwd.rstrip("/\\")).name  # also splits on "/"
    return name or cwd


def session_start_context(cc_session_id: str) -> str:
    return (
        f"Your relaybay hook session: {cc_session_id}. Pass it as `hook_session` in relaybay"
        " writes."
    )
