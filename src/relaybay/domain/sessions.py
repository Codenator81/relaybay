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
