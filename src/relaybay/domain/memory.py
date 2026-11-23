"""Memory and documents: project documents with versions and section anchors, decisions with
supersede chains, memory notes and blocks, and the word handling behind their search."""

import difflib
import re
from dataclasses import dataclass

import snowballstemmer

from relaybay.domain.errors import ConflictError, ValidationError

NOTE_SCOPES = ("global", "role", "project")
NOTE_KINDS = ("user", "feedback", "project", "reference")
NOTE_OPS = ("add", "update", "supersede", "noop")
BLOCK_SCOPES = ("global", "project")
SOURCE_KINDS = ("report", "event", "comment", "message")
SEARCH_KINDS = ("note", "decision", "doc", "report", "comment", "backlog")

DOC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
DOC_BODY_MAX = 200_000
DOC_KIND_MAX = 40
# Well-known kinds, in the order the Docs & rules page groups them; any other kind is free text
# and its group follows these, by name.
DOC_KINDS = ("spec", "status", "task", "brief", "runbook", "adr", "retro")
SOURCE_URL_MAX = 500
TITLE_MAX = 200
REASON_MAX = 500
NOTE_BODY_MAX = 8_000
TAGS_MAX = 300
LINKS_MAX = 2_000
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
    source_url: str | None = None  # set: the document lives in a repository, see there
    body: str = ""  # the text of the version that was loaded (current unless asked otherwise)
    shown_version: int = 0
    author: str | None = None  # of the shown version
    # A file document: the text lives in this file (absolute path) and is read on demand; the
    # file_* fields are what the last read saw.
    source_path: str | None = None
    file_hash: str | None = None
    file_size: int | None = None
    file_mtime: float | None = None
    file_seen_at: str | None = None
    file_missing_at: str | None = None
    masked_lines: int = 0
    # The last change of the file relaybay saw: by 'relaybay' (a doc_update) or 'outside', when,
    # and the anchors of its sections (comma-separated); writes through relaybay are opt-in.
    file_changed_by: str | None = None
    file_changed_at: str | None = None
    file_changed_sections: str | None = None
    file_writable: bool = False
    # Set by a read of a file document: why its text is not there (missing, unreadable), and
    # where it is in words (`<repo>:<path>` inside a repository of the project, else the path).
    file_problem: str | None = None
    place: str | None = None

    @property
    def is_file(self) -> bool:
        return self.source_path is not None


@dataclass(frozen=True, slots=True)
class DocRef:
    """Where a `relaybay://doc/...` link points on the board: the document page (with the
    section's anchor), or nowhere when the document does not exist; `broken` says what is wrong."""

    href: str | None
    broken: str | None = None


def group_documents(items: list[Document]) -> list[tuple[str, list[Document]]]:
    """Documents by kind: the well-known kinds in their order, then the others by name."""
    groups: dict[str, list[Document]] = {}
    for d in items:
        groups.setdefault(d.kind, []).append(d)
    known = [k for k in DOC_KINDS if k in groups]
    return [(k, groups[k]) for k in [*known, *sorted(k for k in groups if k not in DOC_KINDS)]]


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
    anchor: str | None = None  # a document's section that holds the line


def anchor_at(body: str, line: int) -> str | None:
    """The anchor of the innermost section whose own text holds 1-based `line`."""
    return next((s.anchor for s in sections(body) if s.start < line <= s.own_end), None)


# --- sections ------------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# `<a id="name"></a>` alone on a line above a heading (blank lines between allowed): a name for
# that section that survives a reworded heading.
_ALIAS_RE = re.compile(r"""^ {0,3}<a\s+id=["']([\w-]+)["']\s*>\s*</a>\s*$""")


@dataclass(frozen=True, slots=True)
class Section:
    anchor: str
    level: int
    title: str
    start: int  # line index of the heading
    end: int  # line index after the section (next heading of the same or a higher level)
    alias: str | None = None  # the explicit `<a id>` above the heading
    own_end: int = 0  # line index after the section's own text (the next heading of any level)

    def names(self) -> tuple[str, ...]:
        return (self.anchor, self.alias) if self.alias else (self.anchor,)


def parse_heading(line: str) -> tuple[int, str] | None:
    """(level, title) of an ATX heading line, None for any other line."""
    m = _HEADING_RE.match(line)
    return (len(m.group(1)), (m.group(2) or "").strip()) if m else None


def heading_anchor(text: str) -> str:
    """`## Foo bar` → `foo-bar`: lowercase, punctuation dropped, spaces and dashes collapsed."""
    words = re.sub(r"[^\w\s-]", "", text.casefold().replace("_", " "))
    return re.sub(r"[\s-]+", "-", words).strip("-") or "section"


def github_anchor(text: str) -> str:
    """The anchor GitHub (and editors that follow it) gives a heading: like `heading_anchor`,
    but every space is a dash and dashes are not collapsed (`## A / B` → `a--b`)."""
    return re.sub(r"[^\w\- ]", "", text.strip().casefold()).replace(" ", "-")


def loose_anchor(anchor: str) -> str:
    """An anchor written the GitHub way, in relaybay's form: runs of dashes are one dash. A
    relaybay anchor never holds two dashes in a row, so this names at most one heading."""
    return re.sub(r"-{2,}", "-", anchor).strip("-")


def unique_anchors(titles: list[str]) -> list[str]:
    """Anchors for headings in order; a repeated one gets `-2`, `-3`…"""
    seen: set[str] = set()
    tried: dict[str, int] = {}  # per base: the last number tried, so a repeat does not recount
    out = []
    for title in titles:
        base = anchor = heading_anchor(title)
        n = tried.get(base, 1)
        while anchor in seen:
            n += 1
            anchor = f"{base}-{n}"
        tried[base] = n
        seen.add(anchor)
        out.append(anchor)
    return out


def _scan_headings(lines: list[str]) -> tuple[list[tuple[int, int, str, str | None]], set[int]]:
    """(line, level, title, alias) of each ATX heading outside fenced code, and the indexes of
    the `<a id>` lines that named one."""
    found: list[tuple[int, int, str, str | None]] = []
    used: set[int] = set()
    fence: str | None = None
    alias: tuple[int, str] | None = None
    for i, line in enumerate(lines):
        mark = _FENCE_RE.match(line)
        if mark:
            alias = None
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
            title = (heading.group(2) or "").strip()
            found.append((i, len(heading.group(1)), title, alias[1] if alias else None))
            if alias:
                used.add(alias[0])
            alias = None
            continue
        named = _ALIAS_RE.match(line)
        if named:
            alias = (i, named.group(1).casefold())
        elif line.strip():
            alias = None
    return found, used


_CODE_SPAN_RE = re.compile(r"(`+).*?\1")
_LOCAL_LINK_RE = re.compile(r"\]\(#([^)\s]+)\)")


def prose(text: str) -> str:
    """`text` without its fenced code and code spans: a link written there is an example, not a
    link."""
    kept: list[str] = []
    fence: str | None = None
    for line in text.split("\n"):
        mark = _FENCE_RE.match(line)
        if mark:
            token = mark.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None if not line.strip()[len(token) :] else fence
            continue
        if fence is None:
            kept.append(_CODE_SPAN_RE.sub("", line))
    return "\n".join(kept)


def local_links(text: str) -> list[str]:
    """The anchors of the in-document markdown links `[text](#anchor)` of `text`, each once."""
    return list(dict.fromkeys(_LOCAL_LINK_RE.findall(text)))


def alias_lines(body: str) -> set[int]:
    """Line indexes of the `<a id>` lines that name a section (the page does not show them)."""
    return _scan_headings(body.split("\n"))[1]


def sections(body: str) -> list[Section]:
    """The ATX headings (`#` … `######`) outside fenced code, with their line ranges and the
    explicit anchor above each one."""
    lines = body.split("\n")
    found, _ = _scan_headings(lines)
    anchors = unique_anchors([title for _, _, title, _ in found])
    taken = set(anchors)
    out = []
    for n, ((start, level, title, name), anchor) in enumerate(zip(found, anchors, strict=True)):
        end = next((s for s, lvl, _, _ in found[n + 1 :] if lvl <= level), len(lines))
        own_end = found[n + 1][0] if n + 1 < len(found) else len(lines)
        # An alias equal to another section's anchor would make one name point at two places.
        alias = name if name and name != anchor and name not in taken else None
        if alias:
            taken.add(alias)
        out.append(Section(anchor, level, title, start, end, alias, own_end))
    return out


def section_named(found: list[Section], anchor: str) -> Section | None:
    """The section whose anchor or explicit anchor is `anchor` (a leading `#` and case do not
    matter)."""
    wanted = anchor.strip().lstrip("#").casefold()
    exact = next((s for s in found if wanted in s.names()), None)
    if exact is not None or "--" not in wanted:
        return exact
    github = next((s for s in found if github_anchor(s.title) == wanted), None)
    loose = loose_anchor(wanted)
    return github or next((s for s in found if s.anchor == loose), None)


def find_section(body: str, anchor: str) -> Section:
    found = sections(body)
    section = section_named(found, anchor)
    if section is not None:
        return section
    known = ", ".join(s.anchor for s in found) or "none (the document has no headings)"
    raise ValidationError(f"No section {anchor!r}; sections: {known}.")


def section_text(body: str, anchor: str) -> str:
    """The heading and everything until the next heading of the same or a higher level."""
    section = find_section(body, anchor)
    return "\n".join(body.split("\n")[section.start : section.end]).rstrip("\n")


def _trailing_blank(lines: list[str]) -> int:
    n = 0
    while n < len(lines) and not lines[len(lines) - 1 - n].strip():
        n += 1
    return n


def replace_section(body: str, anchor: str, text: str) -> str:
    """`text` replaces the section, heading included; text that does not start with a heading
    replaces only what is under the heading (which stays). Only the section's own lines change:
    the blank lines that end it stay as they were (and, for text without a heading, those right
    under the heading), so a section replaced by its own text leaves the body byte for byte."""
    section = find_section(body, anchor)
    lines = body.split("\n")
    old = lines[section.start : section.end]
    tail_size = _trailing_blank(old[1:])
    tail = old[len(old) - tail_size :] if tail_size else []
    new = text.split("\n")
    while new and not new[0].strip():
        new.pop(0)
    new = new[: len(new) - _trailing_blank(new)]
    if not new or not _HEADING_RE.match(new[0]):
        under = old[1 : len(old) - tail_size]
        gap = 0
        while gap < len(under) and not under[gap].strip():
            gap += 1
        new = [old[0], *(under[:gap] if new else []), *new]
    return "\n".join([*lines[: section.start], *new, *tail, *lines[section.end :]])


def parse_lines(value: str, total: int) -> tuple[int, int]:
    """`a-b` (or `a`) as 1-based inclusive line numbers of a text of `total` lines; a range past
    the end stops at the last line."""
    text = str(value or "").strip()
    m = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", text)
    if not m:
        raise ValidationError(f"lines must look like '120-180', got {value!r}.")
    first = int(m.group(1))
    last = int(m.group(2)) if m.group(2) else first
    if first < 1 or last < first:
        raise ValidationError(f"lines {text}: lines start at 1 and the range goes forward.")
    if first > total:
        raise ValidationError(f"lines {text}: the document has {total} lines.")
    return first, min(last, total)


def replace_lines(body: str, first: int, last: int, expected: str, text: str | None) -> str:
    """Lines `first`-`last` (1-based) replaced by `text` (None removes them) when they read
    `expected` now, both compared without their trailing blank lines; a ConflictError with their
    current text otherwise. A replacement keeps the range's trailing blank lines, as
    `replace_section` keeps a section's."""
    lines = body.split("\n")
    old = lines[first - 1 : last]
    blank = _trailing_blank(old)
    wanted = expected.replace("\r\n", "\n").split("\n")
    if old[: len(old) - blank] != wanted[: len(wanted) - _trailing_blank(wanted)]:
        current = "\n".join(old)
        raise ConflictError(
            f"Lines {first}-{last} no longer read as `expected`: the document changed since you"
            " read them. Their current text is below; send the change again with it as"
            f" expected.\n\n{current}"
        )
    new: list[str] = []
    if text is not None:
        given = text.replace("\r\n", "\n").split("\n")
        new = [*given[: len(given) - _trailing_blank(given)], *old[len(old) - blank :]]
    return "\n".join([*lines[: first - 1], *new, *lines[last:]])


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


COVER_WORD_MIN = 4  # letters: shorter words say too little about what a title is about


def word_cover(title: str, text: str) -> float:
    """The share of `title`'s words (stems, stop words and words under four letters dropped)
    found in `text`; 0.0 for a title with fewer than two such words, where one shared word
    would be a full cover."""
    words = {
        stem(w)
        for w in _WORD_RE.findall(title)
        if len(w) >= COVER_WORD_MIN and w.casefold() not in _STOP_WORDS
    }
    if len(words) < 2:
        return 0.0
    return len(words & set(stem_text(text).split())) / len(words)


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
