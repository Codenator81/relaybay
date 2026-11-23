"""Cards blocked by other cards (`blocked_by`): the link and whether it still blocks. Nothing is
written when a blocker moves: whether it blocks is read from its stage."""

import re
from dataclasses import dataclass
from urllib.parse import quote

from relaybay.domain.stages import Stage

# A blocker blocks while its work is still open; at live or done (or archived) it no longer does,
# and moved back to an earlier stage it blocks again.
BLOCKING_STAGES: frozenset[str] = frozenset(
    s.value for s in (Stage.PLAN, Stage.DEV, Stage.REVIEW, Stage.TEST)
)
NO_LONGER_BLOCKS = "no longer blocks"
# A card key: the project slug, a dash, the number (`acme-shop-12`).
TASK_KEY_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}-[0-9]+", re.IGNORECASE)


@dataclass(slots=True)
class BlockLink:
    """`task_id` is blocked by `blocker_id`; `key`, `title`, `stage` and `project_slug` are those of
    the other card: the blocker in a task's "Blocked by", the blocked card in its "Blocks"."""

    task_id: int
    blocker_id: int
    key: str
    title: str
    stage: str
    project_slug: str
    archived: bool
    created_by: str
    created_at: str

    @property
    def active(self) -> bool:
        return self.stage in BLOCKING_STAGES and not self.archived

    def board_link(self, board: str) -> str:
        """The other card's dialog on the board."""
        slug, key = quote(self.project_slug, safe=""), quote(self.key, safe="")
        return f"{board.rstrip('/')}/p/{slug}?task={key}"


def blocker_line(link: BlockLink, board: str) -> str:
    """`- acme-shop-12 · dev · Gift cards · http://127.0.0.1:8765/p/acme-shop?task=acme-shop-12`,
    with "(no longer blocks)" for a blocker at live or done."""
    gone = "" if link.active else f" ({NO_LONGER_BLOCKS})"
    return f"- {link.key} · {link.stage} · {link.title} · {link.board_link(board)}{gone}"


def blocks_text(links: list[BlockLink]) -> str | None:
    """`acme-shop-3, demo-api-7`: the open cards (not done) this one blocks; None when none."""
    keys = [b.key for b in links if b.stage != Stage.DONE.value and not b.archived]
    return ", ".join(keys) or None
