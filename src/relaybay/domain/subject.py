"""What a card is about (design 0027): word stems of its area, its linked sections and its
title, and the filters built on them for the task context: the open backlog items and the
project decisions that belong to the card. No I/O."""

import re
from dataclasses import dataclass

from relaybay.domain.backlog import BacklogItem
from relaybay.domain.doc_index import IndexedSection
from relaybay.domain.memory import stem
from relaybay.domain.models import Decision

SUBJECT_WORD_MIN = 4
OPEN_BACKLOG = ("new", "later", "planned")
# Two shared stems make a project decision related; one is too often a common word.
DECISION_STEMS_MIN = 2

_WORD_RE = re.compile(r"\w+")
# Words of four letters and more that say nothing about a subject. Russian ones are escaped:
# repository files carry no Cyrillic.
_STOP_WORDS = frozenset(
    {
        *("about", "after", "also", "been", "before", "does", "done", "each", "from", "have"),
        *("into", "just", "more", "must", "only", "other", "over", "should", "some", "than"),
        *("that", "then", "there", "these", "they", "this", "what", "when", "where", "which"),
        *("will", "with", "without", "your", "doing", "shouldn", "would", "could"),
        *("\u0435\u0441\u043b\u0438", "\u043a\u043e\u0433\u0434\u0430"),
        *("\u0447\u0442\u043e\u0431\u044b", "\u043f\u043e\u0441\u043b\u0435"),
        *("\u0447\u0435\u0440\u0435\u0437", "\u0442\u0430\u043a\u0436\u0435"),
        *("\u0442\u043e\u043b\u044c\u043a\u043e", "\u043c\u043e\u0436\u043d\u043e"),
        *("\u043d\u0443\u0436\u043d\u043e", "\u0431\u0443\u0434\u0435\u0442"),
        *("\u044d\u0442\u043e\u0433\u043e", "\u043a\u043e\u0442\u043e\u0440\u044b\u0439"),
        *("\u043a\u043e\u0442\u043e\u0440\u044b\u0435", "\u0431\u043e\u043b\u0435\u0435"),
        *("\u043f\u0435\u0440\u0435\u0434", "\u043c\u0435\u0436\u0434\u0443"),
        *("\u0432\u0441\u0435\u0445", "\u0432\u0435\u0441\u044c", "\u0441\u0432\u043e\u0439"),
        *("\u0442\u043e\u0433\u043e", "\u044d\u0442\u043e\u0442", "\u043d\u0430\u0434\u043e"),
        "\u0435\u0441\u0442\u044c",
    }
)
# Compared by stem, so other forms of a common word ("others", "having") are left out too.
_STOP_STEMS = frozenset(stem(w) for w in _STOP_WORDS)
# A heading's own number: "9.6a Payout provider" -> "9.6a".
_NUMBER_RE = re.compile(r"^\s*(\d+(?:\.\d+)*[a-z]?)(?=[\s.):]|$)", re.IGNORECASE)
# `path:line` in a location or a text; the path has an extension ("docs/spec.md:120") and may
# start with a drive letter ("C:\work\docs\spec.md:5"), never with a `repo:` prefix's last letter.
_PATH_LINE_RE = re.compile(r"((?:(?<![\w-])[A-Za-z]:)?[\w./\\-]*\w\.\w+):(\d+)")
_DRIVE_RE = re.compile(r"^[a-z]:/")
# A bare number names a section only after a prefix: "§3", "section 3".
_NUMBER_PREFIX = r"(?:§\s*|\bsection\s+|\bsec\.\s*)"


def stems(text: str | None) -> set[str]:
    """The stems of the words of `text` worth matching: four letters and more, no common word,
    no bare number."""
    found = set()
    for word in _WORD_RE.findall(text or ""):
        folded = word.casefold()
        if len(folded) < SUBJECT_WORD_MIN or folded.isdigit():
            continue
        stemmed = stem(folded)
        if stemmed not in _STOP_STEMS:
            found.add(stemmed)
    return found


@dataclass(frozen=True, slots=True)
class LinkedSection:
    """A section linked to the card, as the filters need it: the anchor it was linked with, its
    row in the current index (None when it is gone), the file of a file document and the titles
    of the document's top-level headings (its name, not a subject)."""

    doc: str
    anchor: str
    row: IndexedSection | None
    source_path: str | None = None
    roots: frozenset[str] = frozenset()

    @property
    def path_below_root(self) -> str:
        """The heading path without the top-level heading it starts with; the section's title
        when it is that heading."""
        if self.row is None:
            return ""
        path = self.row.heading_path
        top, sep, rest = path.partition(" > ")
        return rest if sep and self.row.level > 1 and top in self.roots else path

    @property
    def names(self) -> set[str]:
        """Every name of the section: the one it was linked with, its heading anchor and its
        explicit anchor."""
        found = {self.anchor}
        if self.row is not None:
            found |= {n for n in (self.row.anchor, self.row.alias) if n}
        return found

    @property
    def number(self) -> str | None:
        return section_number(self.row.title) if self.row else None


def section_number(title: str) -> str | None:
    match = _NUMBER_RE.match(title)
    return match[1].casefold() if match else None


def card_subject(area: str | None, title: str, linked: list[LinkedSection]) -> set[str]:
    """The card's subject: its area name, the titles of its linked sections and their heading
    paths below the document's title, and its title."""
    found = stems(area)
    for link in linked:
        if link.row is not None:
            found |= stems(link.row.title) | stems(link.path_below_root)
    return found | stems(title)


def _names(text: str, name: str, prefix: str = "") -> bool:
    """`name` stands alone in `text` (after `prefix`): not part of a longer word, anchor or
    number."""
    pattern = rf"{prefix}(?<![\w-]){re.escape(name)}(?![\w-]|\.\d)"
    return re.search(pattern, text, re.IGNORECASE) is not None


def _names_link(text: str, link: LinkedSection) -> bool:
    """A one-word anchor or a bare number is too common a word alone: the anchor counts only as
    `doc#anchor`, the number only after `§` or "section". The plain words are the stems' job."""
    doc = rf"(?<![\w-]){re.escape(link.doc)}#"
    by_anchor = any(
        _names(text, name) if "-" in name else _names(text, name, doc) for name in link.names
    )
    number = link.number
    if by_anchor or not number:
        return by_anchor
    return _names(text, number, "" if re.search(r"[.a-z]", number) else _NUMBER_PREFIX)


def names_section(text: str, linked: list[LinkedSection]) -> bool:
    """`text` names a linked section by its anchor or by its heading's number."""
    return any(_names_link(text, link) for link in linked)


def _norm_path(path: str) -> str:
    """Forward slashes and one case (Windows paths ignore it), without leading `./` or `../`."""
    flat = path.replace("\\", "/").strip().casefold()
    return re.sub(r"^(\.\.?/)+", "", flat)


def same_file(mentioned: str, source_path: str) -> bool:
    """A path in a text is the document's file: the same name, and when the text names folders
    too, the file's path ends with them; an absolute path is the same path."""
    told, real = _norm_path(mentioned), _norm_path(source_path)
    if not told:
        return False
    if _DRIVE_RE.match(told):
        return real == told
    told = told.lstrip("/")
    if "/" not in told:
        return real.rsplit("/", 1)[-1] == told
    return real == told or real.endswith("/" + told)


def points_inside(text: str, linked: list[LinkedSection]) -> bool:
    """A `path:line` of `text` falls in the lines of a linked section of the same file."""
    for path, line in _PATH_LINE_RE.findall(text):
        number = int(line)
        for link in linked:
            row = link.row
            if row is None or link.source_path is None:
                continue
            if row.first_line <= number <= row.last_line and same_file(path, link.source_path):
                return True
    return False


def backlog_for_card(
    items: list[BacklogItem], task_id: int, subject: set[str], linked: list[LinkedSection]
) -> list[BacklogItem]:
    """The open items that belong to the card: linked to it, of an area that meets its subject,
    or pointing inside a linked section. Linked ones first, then by priority, then newest."""
    picked: list[tuple[bool, BacklogItem]] = []
    for item in items:
        if item.status not in OPEN_BACKLOG:
            continue
        own = item.task_id == task_id
        text = "\n".join(x for x in (item.title, item.body_md, item.location) if x)
        if (
            own
            or (item.area and stems(item.area) & subject)
            or points_inside(text, linked)
            or names_section(text, linked)
        ):
            picked.append((own, item))
    picked.sort(key=lambda p: (not p[0], -p[1].priority, -p[1].id))
    return [item for _, item in picked]


def backlog_line(item: BacklogItem) -> str:
    """`- #12 [payouts] Retry fails twice · bug · priority 3`."""
    area = f" [{item.area}]" if item.area else ""
    kind = f" · {item.kind}" if item.kind else ""
    return f"- #{item.id}{area} {item.title}{kind} · priority {item.priority}"


def related_decision(
    decision: Decision, area: str | None, subject: set[str], linked: list[LinkedSection]
) -> bool:
    """A project decision belongs to the card: the card's area, a linked section named in its
    text, or at least DECISION_STEMS_MIN stems of the card's subject in its text."""
    if area and decision.area and decision.area.strip().casefold() == area.strip().casefold():
        return True
    if names_section(decision.text, linked):
        return True
    return len(stems(decision.text) & subject) >= DECISION_STEMS_MIN
