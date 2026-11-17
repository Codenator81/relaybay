import re

from relaybay.domain.errors import ValidationError

DESKTOP_ID_PREFIX = "local_"
DEFAULT_DESKTOP_LINK_TEMPLATE = "claude://claude.ai/epitaxy/{id}"

_CANONICAL = re.compile(r"^local_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_session_id(value: str) -> str | None:
    """Return the normalised desktop session id (`local_<uuid>`, lowercase), or None for a plain
    name. Only the canonical hyphenated form is accepted; braces or `urn:uuid:` are rejected."""
    candidate = value.strip().lower()
    if not candidate.startswith(DESKTOP_ID_PREFIX):
        return None
    if not _CANONICAL.match(candidate):
        raise ValidationError(
            f"{value!r} looks like a desktop session id but is not 'local_<uuid>'"
            " (e.g. local_123e4567-e89b-12d3-a456-426614174000)."
        )
    return candidate


# --- where a session runs (design 0025) -----------------------------------------------------

DESKTOP = "desktop"
TERMINAL = "terminal"
SURFACES = (DESKTOP, TERMINAL)
ENTRYPOINT_MAX = 60
# Claude Code's own session id, the only thing `claude --resume` is ever given.
_CLAUDE_SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def surface_of_entrypoint(value: str | None) -> str | None:
    """Where a session runs by Claude Code's `CLAUDE_CODE_ENTRYPOINT`: the desktop app sets
    `claude-desktop…`, the terminal `cli`; anything else (an SDK, an IDE, nothing) is unknown."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("claude-desktop"):
        return DESKTOP
    return TERMINAL if value == "cli" else None


def resumable_id(value: str | None) -> str | None:
    """The Claude Code session id when it is one `claude --resume` may be given, else None."""
    if value is None or not _CLAUDE_SESSION_ID.match(value):
        return None
    return value
