"""Claude Code hook events: which ones relaybay reads and what they mean for a session."""

from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

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


@dataclass(frozen=True, slots=True)
class HookEvent:
    cc_session_id: str
    event: str
    status: str
    cwd: str | None
    activity: str | None
    end_reason: str | None = None
    stop_hook_active: bool = False  # Stop: Claude Code is already continuing for a stop hook

    @property
    def activity_only(self) -> bool:
        """A working session doing the next thing: coalesced, not a status change."""
        return self.event in ("UserPromptSubmit", "PreToolUse", "PostToolUse")


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def parse_hook(payload: Any) -> HookEvent | None:
    """The part of a hook's input JSON relaybay uses, or None for an event it ignores. The
    prompt text and tool input are never kept."""
    if not isinstance(payload, dict):
        return None
    event = payload.get("hook_event_name")
    cc_id = _text(payload.get("session_id"), CC_ID_MAX)
    if event not in HOOK_STATUS or cc_id is None:
        return None
    if event == "SessionStart":
        activity = f"started ({_text(payload.get('source'), 40) or 'startup'})"
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
