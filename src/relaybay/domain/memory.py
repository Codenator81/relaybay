"""Memory and documents: project documents with versions and section anchors, decisions with
supersede chains, memory notes and blocks, and the word handling behind their search."""

import difflib
import re
from dataclasses import dataclass

import snowballstemmer

from relaybay.domain.errors import ValidationError

NOTE_SCOPES = ("global", "role", "project")
NOTE_KINDS = ("user", "feedback", "project", "reference")
NOTE_OPS = ("add", "update", "supersede", "noop")
BLOCK_SCOPES = ("global", "project")
SOURCE_KINDS = ("report", "event", "comment", "message")
SEARCH_KINDS = ("note", "decision", "doc", "report", "comment")

DOC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DOC_BODY_MAX = 200_000
DOC_KIND_MAX = 40
TITLE_MAX = 200
REASON_MAX = 500
NOTE_BODY_MAX = 8_000
TAGS_MAX = 300
ROLE_MAX = 40
SOURCE_MAX = 300
BLOCK_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
BLOCK_LIMIT_DEFAULT = 2_000
BLOCK_LIMIT_MAX = 20_000
SEARCH_LIMIT_MAX = 50
QUERY_WORDS_MAX = 12
# memory_add: a candidate is a strong match when the stemmed words of the two titles overlap by
# at least this share (Jaccard: shared words / all words); adding next to one needs force_new.
STRONG_MATCH = 0.6
CANDIDATES_MAX = 5


@dataclass(slots=True)
class Document:
    id: int
    project_id: int
    project_slug: str
    slug: str
    title: str
    kind: str
    version: int
    created_at: str
    updated_at: str
    body: str = ""  # the text of the version that was loaded (current unless asked otherwise)
    shown_version: int = 0
    author: str | None = None  # of the shown version


@dataclass(slots=True)
class DocumentVersion:
    version: int
    author: str
    reason: str
    base_version: int
    section: str | None  # the anchor when the write replaced one section
    created_at: str
    size: int  # characters
    body: str = ""  # loaded only when asked for


@dataclass(slots=True)
class MemoryNote:
    id: int
    scope: str
    project_id: int | None
    project_slug: str | None
    role: str | None
    kind: str
    title: str
    body: str
    tags: str | None
    source_kind: str | None
    source_ref: str | None
    source_text: str | None
    links: str | None
    review_at: str | None
    valid_from: str
    valid_to: str | None
    recorded_at: str
    updated_at: str
    superseded_at: str | None
    superseded_by: int | None
    last_read_at: str | None
    read_count: int
    version: int
    author: str

    @property
    def current(self) -> bool:
        return self.superseded_at is None

    @property
    def hook(self) -> str:
        """The first line of the body, short: what an index line shows next to the title."""
        first = next((line.strip() for line in self.body.splitlines() if line.strip()), "")
        return first if len(first) <= 120 else first[:119] + "…"


@dataclass(slots=True)
class MemoryBlock:
    id: int
    scope: str
    project_id: int | None
    project_slug: str | None
    label: str
    description: str | None
    body: str
    char_limit: int
    owner_only: bool
    version: int
    updated_by: str
    updated_at: str
    created_at: str


@dataclass(slots=True)
class Source:
    """Where a decision or note came from: a report, event or comment id, or an external
    message id, plus display text; or display text alone."""

    kind: str | None = None
    ref: str | None = None
    text: str | None = None

    def label(self) -> str:
        head = f"{self.kind} {self.ref}" if self.kind else ""
        return " · ".join(x for x in (head, self.text or "") if x)


@dataclass(slots=True)
class SearchHit:
    kind: str  # one of SEARCH_KINDS
    id: int
    project_slug: str | None
    task_key: str | None
    name: str | None  # a document's slug
    title: str
    line: int  # 1-based line of the body the snippet comes from
    snippet: str


# --- sections ------------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True, slots=True)
class Section:
    anchor: str
    level: int
    title: str
    start: int  # line index of the heading
    end: int  # line index after the section (next heading of the same or a higher level)


def heading_anchor(text: str) -> str:
    """`## Foo bar` → `foo-bar`: lowercase, punctuation dropped, spaces and dashes collapsed."""
    words = re.sub(r"[^\w\s-]", "", text.casefold().replace("_", " "))
    return re.sub(r"[\s-]+", "-", words).strip("-") or "section"


def unique_anchors(titles: list[str]) -> list[str]:
    """Anchors for headings in order; a repeated one gets `-2`, `-3`…"""
    seen: set[str] = set()
    out = []
    for title in titles:
        base = anchor = heading_anchor(title)
        n = 1
        while anchor in seen:
            n += 1
            anchor = f"{base}-{n}"
        seen.add(anchor)
        out.append(anchor)
    return out


def sections(body: str) -> list[Section]:
    """The ATX headings (`#` … `######`) outside fenced code, with their line ranges."""
    lines = body.split("\n")
    found: list[tuple[int, int, str]] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        mark = _FENCE_RE.match(line)
        if mark:
            token = mark.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None if not line.strip()[len(token) :] else fence
            continue
        if fence is not None:
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            found.append((i, len(heading.group(1)), (heading.group(2) or "").strip()))
    anchors = unique_anchors([title for _, _, title in found])
    out = []
    for n, ((start, level, title), anchor) in enumerate(zip(found, anchors, strict=True)):
        end = next((s for s, lvl, _ in found[n + 1 :] if lvl <= level), len(lines))
        out.append(Section(anchor, level, title, start, end))
    return out


def find_section(body: str, anchor: str) -> Section:
    wanted = anchor.strip().lstrip("#").casefold()
    found = sections(body)
    for section in found:
        if section.anchor == wanted:
            return section
    known = ", ".join(s.anchor for s in found) or "none (the document has no headings)"
    raise ValidationError(f"No section {anchor!r}; sections: {known}.")


def section_text(body: str, anchor: str) -> str:
    """The heading and everything until the next heading of the same or a higher level."""
    section = find_section(body, anchor)
    return "\n".join(body.split("\n")[section.start : section.end]).rstrip("\n")


def replace_section(body: str, anchor: str, text: str) -> str:
    """`text` replaces the section, heading included; text that does not start with a heading
    replaces only what is under the heading (which stays)."""
    section = find_section(body, anchor)
    lines = body.split("\n")
    new = text.strip("\n").split("\n")
    if not _HEADING_RE.match(new[0]):
        new = [lines[section.start], *new]
    if section.end < len(lines) and new[-1].strip():
        new.append("")
    return "\n".join([*lines[: section.start], *new, *lines[section.end :]])


def diff_lines(old: str, new: str, old_label: str, new_label: str) -> list[str]:
    """A unified diff of two texts, one entry per line, without line endings."""
    return list(
        difflib.unified_diff(
            old.split("\n"), new.split("\n"), fromfile=old_label, tofile=new_label, lineterm=""
        )
    )


# --- words and search --------------------------------------------------------------------

_WORD_RE = re.compile(r"\w+")
_CYRILLIC_RE = re.compile(f"[{chr(0x400)}-{chr(0x4FF)}]")
_STEMMERS = {
    "english": snowballstemmer.stemmer("english"),
    "russian": snowballstemmer.stemmer("russian"),
}
_STOP_WORDS = frozenset(
    {
        *("a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it"),
        *("of", "on", "or", "that", "the", "this", "to", "was", "we", "with"),
    }
)


def stem(word: str) -> str:
    """The Snowball stem of a word: Russian for Cyrillic words, English otherwise."""
    word = word.casefold()
    language = "russian" if _CYRILLIC_RE.search(word) else "english"
    return str(_STEMMERS[language].stemWord(word))


def stem_text(text: str) -> str:
    """The stems of every word, space separated: the shadow column of the search index."""
    return " ".join(stem(w) for w in _WORD_RE.findall(text))


def query_words(query: str) -> list[str]:
    words = list(dict.fromkeys(w.casefold() for w in _WORD_RE.findall(query or "")))
    if not words:
        raise ValidationError("The search query has no words.")
    return words[:QUERY_WORDS_MAX]


def fts_query(words: list[str], any_word: bool = False) -> str:
    """An FTS5 MATCH expression built from plain words only, so user text never reaches the
    query parser as syntax: each word matches as a prefix or by its stem."""
    groups = [f'("{w}"* OR stems:"{stem(w)}"*)' for w in words]
    return (" OR " if any_word else " AND ").join(groups)


def title_words(title: str) -> set[str]:
    return {stem(w) for w in _WORD_RE.findall(title) if w.casefold() not in _STOP_WORDS}


def title_similarity(a: str, b: str) -> float:
    left, right = title_words(a), title_words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def candidate_words(title: str) -> list[str]:
    """The words of a new note's title that are worth searching for (stop words and one- and
    two-letter words dropped)."""
    words = [w.casefold() for w in _WORD_RE.findall(title)]
    kept = [w for w in words if len(w) > 2 and w not in _STOP_WORDS]
    return list(dict.fromkeys(kept or words))[:QUERY_WORDS_MAX]


def matching_line(body: str, words: list[str]) -> tuple[int, str]:
    """The line of `body` that holds most of `words` (a word or its stem; the first such line),
    as (1-based number, shortened text); the first non-empty line when none does."""
    needles = [(w.casefold(), stem(w)) for w in words]
    lines = body.split("\n")
    best, best_count = 0, 0
    for number, line in enumerate(lines, 1):
        folded = line.casefold()
        count = sum(1 for w, s in needles if w in folded or (s and s in folded))
        if count > best_count:
            best, best_count = number, count
    if best:
        return best, _short(lines[best - 1])
    for number, line in enumerate(lines, 1):
        if line.strip():
            return number, _short(line)
    return 1, ""


def _short(line: str, limit: int = 160) -> str:
    text = " ".join(line.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_source(value: str | None) -> Source:
    """`report:12`, `comment:7`, `event:301` or `message:<id>`, optionally followed by display
    text (`comment:7 owner in the review thread`); anything else is display text alone."""
    text = (value or "").strip()
    if not text:
        return Source()
    if len(text) > SOURCE_MAX:
        raise ValidationError(f"source is longer than {SOURCE_MAX} characters.")
    head, _, rest = text.partition(" ")
    kind, sep, ref = head.partition(":")
    if not sep or kind.lower() not in SOURCE_KINDS:
        return Source(text=text)
    kind = kind.lower()
    if not ref:
        raise ValidationError(f"source {kind}: needs an id, e.g. '{kind}:12'.")
    if kind != "message" and not ref.isdigit():
        raise ValidationError(f"source {kind}:{ref} needs a numeric id.")
    return Source(kind=kind, ref=ref, text=rest.strip() or None)
