"""The order in which the cards on test go to the main branch: a card after the test cards that
block it, otherwise the owner's order of the test column. Pure: the service reads the data."""

from dataclasses import dataclass, field

from relaybay.domain.blockers import BlockLink
from relaybay.domain.models import Task
from relaybay.domain.plan import one_line
from relaybay.domain.stages import Stage

# Fewer cards on test have no order to tell.
MERGE_ORDER_MIN = 2
MERGE_ORDER_TITLE = "## Merge order (test)"
_TITLE_MAX = 80
_PRS_MAX = 3


@dataclass(slots=True)
class MergeEntry:
    task: Task
    repos: list[str] = field(default_factory=list)  # names of the linked project repositories
    prs: list[str] = field(default_factory=list)  # pull request labels
    waits: list[BlockLink] = field(default_factory=list)  # blockers still open outside test

    def line(self, n: int) -> str:
        """`1. acme-shop-4 Checkout · repos web, api · PR #12, #13 · waits for demo-api-2 · dev`."""
        bits = [f"{n}. {self.task.key} {one_line(self.task.title, _TITLE_MAX) or ''}".rstrip()]
        if self.repos:
            bits.append(f"repos {', '.join(self.repos)}")
        if self.prs:
            more = f" +{len(self.prs) - _PRS_MAX}" if len(self.prs) > _PRS_MAX else ""
            bits.append(f"PR {', '.join(self.prs[:_PRS_MAX])}{more}")
        bits += [f"waits for {b.key} · {b.stage}" for b in self.waits]
        return " · ".join(bits)


@dataclass(slots=True)
class MergeOrder:
    entries: list[MergeEntry]
    warnings: list[str]

    @property
    def shown(self) -> bool:
        return len(self.entries) >= MERGE_ORDER_MIN

    def lines(self) -> list[str]:
        """The numbered cards, then the warnings: the digest's and the snapshot's section."""
        return [e.line(n) for n, e in enumerate(self.entries, 1)] + self.warnings


def merge_order(
    cards: list[Task],
    repos_by_card: dict[int, list[str]],
    blockers: dict[int, list[BlockLink]],
    prs_by_card: dict[int, list[str]] | None = None,
) -> MergeOrder:
    """`cards` are the project's cards (any stage); only those on test and not archived take part.
    `blockers` maps a card id to the cards still blocking it (`BlockLink.active`)."""
    test = sorted(
        (t for t in cards if t.stage == Stage.TEST and t.archived_at is None),
        key=lambda t: (t.rank, t.id),
    )
    ids = {t.id for t in test}
    before = {
        t.id: {b.blocker_id for b in blockers.get(t.id, []) if b.blocker_id in ids} for t in test
    }
    placed: list[Task] = []
    done: set[int] = set()
    left = list(test)
    while left:
        # The first card in the owner's order whose test blockers are all placed; a cycle (writes
        # refuse one) falls back to the owner's order.
        ready = next((t for t in left if before[t.id] <= done), left[0])
        placed.append(ready)
        done.add(ready.id)
        left.remove(ready)
    prs_by_card = prs_by_card or {}
    entries = [
        MergeEntry(
            t,
            repos_by_card.get(t.id, []),
            prs_by_card.get(t.id, []),
            [b for b in blockers.get(t.id, []) if b.active and b.blocker_id not in ids],
        )
        for t in placed
    ]
    warnings: list[str] = []
    by_repo: dict[str, list[str]] = {}
    for e in entries:
        for name in e.repos:
            by_repo.setdefault(name, []).append(e.task.key)
    for name, keys in by_repo.items():
        if len(keys) > 1:
            warnings.append(f"repos overlap: {name} — {', '.join(keys)}")
    for e in entries:
        warnings += [f"{e.task.key} waits for {b.key} ({b.stage})" for b in e.waits]
    return MergeOrder(entries, warnings)
