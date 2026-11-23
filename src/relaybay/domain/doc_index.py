"""The section index of a document (design 0026): one row per section with its lines and size,
the outline built from it, capped reads that point at the next part instead of cutting silently,
where a file document's text comes from, and how a change is spliced back into its bytes. No
I/O except `read_file` and `write_file`."""

import bisect
import contextlib
import fnmatch
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from relaybay.domain.environments import DEFAULT_SECRET_RULES, SecretRules, redact, secret_spans
from relaybay.domain.errors import ValidationError
from relaybay.domain.memory import (
    DOC_BODY_MAX,
    Document,
    Section,
    github_anchor,
    loose_anchor,
    parse_heading,
    sections,
    unique_anchors,
)

# A read of a document over DOC_BODY_MAX characters (only a file document can be one) is capped
# at this many characters unless the caller passes its own max_chars.
READ_CAP = 40_000
MAX_CHARS_MIN = 2_000
# What a capped answer needs besides the text and the outline: its header, the file line, the
# note on the cut and the call that reads on.
CUT_RESERVE = 1_500
MAX_CHARS_MAX = 1_000_000
FILE_MAX_BYTES = 8 * 1024 * 1024
BYTES_PER_TOKEN = 3.5
MASKED_LINE = "[masked: a secret detector matched this line]"
SEARCH_LIMIT_MAX = 50
SNIPPET_MAX = 240
# A `document.updated` event of a file names at most this many anchors per kind of change.
EVENT_ANCHORS_MAX = 20


@dataclass(frozen=True, slots=True)
class IndexedSection:
    """A row of `document_section`. Lines are 1-based and inclusive."""

    position: int
    anchor: str  # "" for the text before the first heading
    alias: str | None
    level: int  # 0: the text before the first heading
    title: str
    heading_path: str  # "Spec > Payments > Gift cards"
    first_line: int
    last_line: int  # with the subsections
    own_last: int  # the section's own text ends here
    chars: int  # with the subsections
    bytes: int
    hash: str  # of the own text
    text: str = ""  # the own text as indexed (secrets masked); not a column

    @property
    def tokens(self) -> int:
        return tokens(self.bytes)

    @property
    def name(self) -> str:
        return self.anchor or "(start)"


def tokens(size_bytes: int) -> int:
    return round(size_bytes / BYTES_PER_TOKEN)


def size_label(chars: int, size_bytes: int) -> str:
    """`52k chars (~15k tokens)`."""
    return f"{_k(chars)} chars (~{_k(tokens(size_bytes))} tokens)"


def short_size(chars: int, size_bytes: int) -> str:
    """`52k/15k`: characters and estimated tokens on an outline line."""
    return f"{_k(chars)}/{_k(tokens(size_bytes))}"


def chars_label(n: int) -> str:
    """`52k chars`: a section's size on the board."""
    return f"{_k(n)} chars"


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def whole_size(rows: list[IndexedSection]) -> tuple[int, int]:
    """Characters and bytes of the whole text, from its index: the top sections cover it."""
    chars = size = end = 0
    for r in rows:
        if r.first_line > end:
            chars, size, end = chars + r.chars + 1, size + r.bytes + 1, r.last_line
    return max(chars - 1, 0), max(size - 1, 0)


def _join(lines: list[str]) -> str:
    return "\n".join(lines)


def masked_lines(text: str, rules: SecretRules = DEFAULT_SECRET_RULES) -> tuple[set[int], set[int]]:
    """Indexes of the lines that hold a secret, as two sets: the lines a detector matches on
    their own, and the other lines only a whole-text match covers (a private key to its END
    line, a flag and its value on two lines, a command named elsewhere). Linear: one pass per
    line and one over the whole text."""
    lines = text.split("\n")
    own = {i for i, line in enumerate(lines) if next(secret_spans(line, rules), None)}
    starts = [0]
    for line in lines[:-1]:
        starts.append(starts[-1] + len(line) + 1)
    covered: set[int] = set()
    for span in secret_spans(text, rules):
        first = bisect.bisect_right(starts, span.start) - 1
        last = bisect.bisect_right(starts, max(span.end - 1, span.start)) - 1
        covered.update(range(first, last + 1))
    return own, covered - own


def _mask(line: str, own: bool, rules: SecretRules) -> str:
    """A masked line; a heading stays a heading of its level, so the structure is kept. Only a
    line whose secret is its own may keep the rest of its text (`redact`), and only when nothing
    in what is left is found again; a line covered by a match across lines never keeps any."""
    heading = parse_heading(line)
    whole = MASKED_LINE if heading is None else f"{'#' * heading[0]} [masked]"
    if not own:
        return whole
    hidden = redact(line)
    if hidden == line or next(secret_spans(hidden, rules), None) is not None:
        return whole
    if heading is not None and (parse_heading(hidden) or (0, ""))[0] != heading[0]:
        return whole
    return hidden


def build(text: str, rules: SecretRules = DEFAULT_SECRET_RULES) -> tuple[list[IndexedSection], int]:
    """The index rows of `text` (LF line endings) and how many lines were masked in it. Titles,
    heading paths, anchors and aliases come from the masked lines: a secret in a heading never
    reaches the index, the outline, a search hit, a pointer or an event."""
    lines = text.split("\n")
    own, spanned = masked_lines(text, rules)
    hidden = own | spanned
    shown = [
        (_mask(lines[i], i in own, rules) if i in hidden else lines[i]) for i in range(len(lines))
    ]
    found = sections(text)
    titles = []
    for s in found:
        parsed = parse_heading(shown[s.start])
        titles.append(parsed[1] if parsed else "[masked]")
    # The anchors sections() made stand unless a heading was masked: counted once, not twice.
    anchors = (
        unique_anchors(titles)
        if any(s.start in hidden for s in found)
        else [s.anchor for s in found]
    )
    taken = set(anchors)
    rows: list[IndexedSection] = []
    first = found[0].start if found else len(lines)
    if any(line.strip() for line in lines[:first]):
        rows.append(_row(0, None, lines, shown, 0, first, first, "", "", None))
    trail: list[tuple[int, str]] = []
    for s, title, anchor in zip(found, titles, anchors, strict=True):
        while trail and trail[-1][0] >= s.level:
            trail.pop()
        trail.append((s.level, title or anchor))
        path = " > ".join(t for _, t in trail)
        alias = None if _alias_masked(lines, s.start, hidden) else s.alias
        if alias in taken:
            alias = None
        rows.append(
            _row(len(rows), s, lines, shown, s.start, s.end, s.own_end, path, anchor, alias, title)
        )
    return rows, len(hidden)


def _alias_masked(lines: list[str], heading: int, hidden: set[int]) -> bool:
    """The `<a id>` line above a heading (blank lines between) was masked."""
    i = heading - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    return i in hidden


def _row(
    position: int,
    s: Section | None,
    lines: list[str],
    shown: list[str],
    start: int,
    end: int,
    own_end: int,
    path: str,
    anchor: str,
    alias: str | None,
    title: str = "",
) -> IndexedSection:
    whole = _join(lines[start:end])
    own = _join(lines[start:own_end])
    return IndexedSection(
        position=position,
        anchor=anchor,
        alias=alias,
        level=s.level if s else 0,
        title=title,
        heading_path=path,
        first_line=start + 1,
        last_line=max(start + 1, end),
        own_last=max(start + 1, own_end),
        chars=len(whole),
        bytes=len(whole.encode("utf-8")),
        hash=hashlib.sha256(own.encode("utf-8")).hexdigest(),
        text=_join(shown[start:own_end]),
    )


def changed_anchors(old: list[IndexedSection], new: list[IndexedSection]) -> dict[str, list[str]]:
    """Which sections were added, removed or changed between two indexes, by anchor."""
    before = {r.name: r.hash for r in old}
    after = {r.name: r.hash for r in new}
    return {
        "added": [a for a in after if a not in before],
        "removed": [a for a in before if a not in after],
        "changed": [a for a in after if a in before and before[a] != after[a]],
    }


def lookup(rows: list[IndexedSection], anchor: str) -> IndexedSection | None:
    """The section a heading anchor or an explicit anchor names (a GitHub-style anchor, with
    runs of dashes, names the same heading); None when there is none."""
    wanted = anchor.strip().lstrip("#").casefold()
    exact = next((r for r in rows if r.anchor and wanted in (r.anchor, r.alias)), None)
    if exact is not None or "--" not in wanted:
        return exact
    # The heading GitHub names so first (the board page gives it that id), then the one whose
    # anchor is the same with the runs of dashes made one.
    github = next((r for r in rows if r.anchor and github_anchor(r.title) == wanted), None)
    loose = loose_anchor(wanted)
    return github or next((r for r in rows if r.anchor == loose), None)


def find(rows: list[IndexedSection], anchor: str) -> IndexedSection:
    found = lookup(rows, anchor)
    if found is not None:
        return found
    known = ", ".join(r.anchor for r in rows if r.anchor) or "none (the document has no headings)"
    raise ValidationError(f"No section {anchor!r}; sections: {known}.")


def holding(rows: list[IndexedSection], line: int) -> IndexedSection | None:
    """The innermost section whose own text holds `line`."""
    return next((r for r in rows if r.first_line <= line <= r.own_last), None)


def outline(
    rows: list[IndexedSection], depth: int | None = None, under: IndexedSection | None = None
) -> list[IndexedSection]:
    """The headings, at most `depth` levels deep (counted from `under`'s level when given), and
    with `under` only those inside that section."""
    picked = [r for r in rows if r.level > 0]
    base = 0
    if under is not None:
        picked = [
            r
            for r in picked
            if under.first_line < r.first_line <= under.last_line and r.level > under.level
        ]
        base = under.level
    if depth is not None:
        picked = [r for r in picked if r.level - base <= depth]
    return picked


def clean_max_chars(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValidationError("max_chars must be a whole number.")
    try:
        number = int(value)
    except ValueError:
        raise ValidationError(f"max_chars must be a whole number, got {value!r}.") from None
    if not MAX_CHARS_MIN <= number <= MAX_CHARS_MAX:
        raise ValidationError(f"max_chars must be {MAX_CHARS_MIN} to {MAX_CHARS_MAX}.")
    return number


def cap_for(total_chars: int, max_chars: int | None) -> int | None:
    """The cap of a read: the caller's, else READ_CAP for a document over DOC_BODY_MAX."""
    if max_chars is not None:
        return max_chars
    return READ_CAP if total_chars > DOC_BODY_MAX else None


def outline_line(r: IndexedSection) -> str:
    """`  - h2 #gift-cards (alias #gc) Gift cards · 2140-2610 · 52k/15k`."""
    alias = f" (alias #{r.alias})" if r.alias else ""
    return (
        f"{'  ' * max(r.level - 1, 0)}- h{r.level} #{r.anchor}{alias} {r.title or '(no title)'}"
        f" · {r.first_line}-{r.last_line} · {short_size(r.chars, r.bytes)}"
    )


@dataclass(frozen=True, slots=True)
class Cut:
    """A read that did not fit: lines first-shown_last were returned (`long_line`: that one line
    was longer than the room and is cut); `inside` are the top two levels of headings in the
    range that fit, `more` how many headings were left out; `next_lines` is the range to read
    next within the cap."""

    first: int
    shown_last: int
    last: int
    total_chars: int
    inside: list[IndexedSection]
    more: int
    next_lines: str
    long_line: int | None = None


def capped(
    lines: list[str], first: int, last: int, cap: int | None, rows: list[IndexedSection]
) -> tuple[str, Cut | None]:
    """Lines `first`-`last` (1-based, inclusive) of `lines`, or as many of the first ones as fit
    in `cap` next to a bounded outline of the range, with the call that reads on. The answer
    built from it (CUT_RESERVE for its header and notes) is never over the cap."""
    wanted = lines[first - 1 : last]
    text = _join(wanted)
    if cap is None or len(text) <= cap:
        return text, None
    every = [r for r in rows if r.level > 0 and first < r.first_line <= last]
    top = min((r.level for r in every), default=0)
    budget = (cap - CUT_RESERVE) // 2
    inside: list[IndexedSection] = []
    used = 0
    for r in (r for r in every if r.level - top < 2):
        size = len(outline_line(r)) + 1
        if used + size > budget:
            break
        inside.append(r)
        used += size
    room = cap - CUT_RESERVE - used
    taken, shown = 0, 0
    for line in wanted:
        if taken + len(line) + 1 > room:
            break
        taken += len(line) + 1
        shown += 1
    long_line = None
    if shown:
        body = _join(wanted[:shown])
    else:
        body, shown, long_line = wanted[0][:room], 1, first
    shown_last = first + shown - 1
    start = shown_last + 1
    end, size = start, 0
    while end <= last and (size + len(lines[end - 1]) + 1 <= cap - CUT_RESERVE or end == start):
        size += len(lines[end - 1]) + 1
        end += 1
    next_lines = f"{start}-{end - 1}" if start <= last else ""
    cut = Cut(
        first, shown_last, last, len(text), inside, len(every) - len(inside), next_lines, long_line
    )
    return body, cut


# --- file documents ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FileStat:
    size: int
    mtime: float


# Places a file document may never be read from, whatever the path says before links are
# resolved: credentials folders under the home folder, the kernel's pseudo file systems, and file
# names that hold keys or secrets. The data folder (except its worktrees) and the token and
# desktop.toml come from PathRules.
DENIED_HOME_DIRS = (".ssh", ".aws", ".gnupg", ".config/gh", ".docker")
DENIED_ROOTS = ("/proc", "/sys", "/dev")
DENIED_NAMES = (
    ".env*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.kdbx",
    "credentials*",
)


@dataclass(frozen=True, slots=True)
class PathRules:
    """What a file document's resolved path is checked against."""

    data_dir: Path
    protected: tuple[Path, ...]  # single files: the token, desktop.toml
    home: Path
    network_ok: Callable[[str], bool]  # a network share or device path is allowed (configured)


def _real(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path


def denied(real: Path, rules: PathRules) -> str | None:
    """Why the resolved path `real` may not be a file document, or None."""
    text = str(real)
    if not rules.network_ok(text):
        return "it is a network share or a device path (one in `attachment_roots` is allowed)"
    data = _real(rules.data_dir)
    if real.is_relative_to(data) and not real.is_relative_to(data / "worktrees"):
        return "it is inside relaybay's data folder"
    if any(real == _real(p) for p in rules.protected):
        return "it is a file relaybay keeps a secret in"
    home = _real(rules.home)
    for folder in DENIED_HOME_DIRS:
        if real.is_relative_to(home / folder):
            return f"it is inside ~/{folder}, a folder of credentials"
    posix = real.as_posix()
    for root in DENIED_ROOTS:
        if posix == root or posix.startswith(root + "/"):
            return f"it is inside {root}"
    name = real.name.casefold()
    for pattern in DENIED_NAMES:
        if fnmatch.fnmatchcase(name, pattern):
            return f"its name matches {pattern}, a file that holds keys or secrets"
    return None


def check_path(raw: str, rules: PathRules, name: str = "source_path") -> Path:
    """The real path of a file document (or of a file imported once, `name` its parameter):
    absolute, links resolved, a regular file, and then not in a denied place by its resolved
    path (a link never leads around the list)."""
    text = (raw or "").strip()
    if not text:
        raise ValidationError(f"{name} must not be empty.")
    if not rules.network_ok(text):  # before the disk is touched: reading a share can block
        raise ValidationError(
            f"{text!r} is a network share or a device path; relaybay reads a file there only"
            " when that share is in `attachment_roots`."
        )
    given = Path(text).expanduser()
    if not given.is_absolute():
        raise ValidationError(f"{name} must be an absolute path, got {text!r}.")
    try:
        real = given.resolve(strict=True)
        info = real.stat()
    except FileNotFoundError:
        raise ValidationError(f"File {text!r} not found.") from None
    except (OSError, RuntimeError) as exc:  # RuntimeError: a symlink loop
        raise ValidationError(f"Cannot read {text!r}: {exc}.") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"{text!r} is not a regular file.")
    reason = denied(real, rules)
    if reason:
        raise ValidationError(f"relaybay does not read {text!r}: {reason}.")
    return real


def recheck(path: str, rules: PathRules) -> str | None:
    """A stored file document's path, resolved again before a read: why it is refused now (it
    was swapped for a link into a denied place), or None."""
    real = _real(Path(path))
    reason = denied(real, rules)
    return f"{path} is refused now: {reason}" if reason else None


def file_stat(path: str) -> FileStat | None:
    """Size and modification time, None when the file is not there (or not a file)."""
    try:
        info = Path(path).stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return FileStat(info.st_size, info.st_mtime)


def read_file(path: str) -> tuple[bytes, str]:
    """The bytes and the text (UTF-8, a byte order mark dropped, CRLF as LF) of a file
    document; over FILE_MAX_BYTES or not UTF-8 is refused."""
    with Path(path).open("rb") as fh:
        data = fh.read(FILE_MAX_BYTES + 1)
    if len(data) > FILE_MAX_BYTES:
        raise ValidationError(
            f"{path} has more than {FILE_MAX_BYTES // (1024 * 1024)} MB; a file document holds"
            " at most that."
        )
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path} is not valid UTF-8 (byte {exc.start}).") from None
    return data, text.replace("\r\n", "\n")


def place(path: str, repos: list[tuple[str, str]]) -> str:
    """`<repo>:<path>` when `path` is inside one of the (name, folder) repositories, else the
    path itself."""
    real = Path(path)
    for name, folder in repos:
        try:
            root = Path(folder).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if real.is_relative_to(root):
            return f"{name}:{real.relative_to(root).as_posix()}"
    return path


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def outside_sections(
    earlier: list[str], changes: dict[str, list[str]], rows: list[IndexedSection]
) -> list[str]:
    """The sections an outside change names, earlier outside changes first: the added and
    changed ones the file still has, and the removed ones as `-anchor` (dropped again when the
    section is back)."""
    present = {r.name for r in rows}
    out: list[str] = []
    for name in [*earlier, *changes["added"], *changes["changed"]]:
        if name and not name.startswith("-") and name in present:
            out.append(name)
    for name in [*(e[1:] for e in earlier if e.startswith("-")), *changes["removed"]]:
        if name and name not in present:
            out.append(f"-{name}")
    return list(dict.fromkeys(out))


def outside_note(doc: Document) -> str | None:
    """`Note: spec was changed outside relaybay at <time>: sections a, b (version N).` while
    the last change of a file document's file seen by relaybay was not its own."""
    if doc.file_changed_by != "outside":
        return None
    names = [a for a in (doc.file_changed_sections or "").split(",") if a]
    named = [a for a in names if not a.startswith("-")]
    removed = [a[1:] for a in names if a.startswith("-")]
    sections = f": sections {', '.join(named)}" if named else ""
    if removed:
        sections += f"{';' if named else ':'} removed {', '.join(removed)}"
    return (
        f"Note: {doc.slug} was changed outside relaybay at {(doc.file_changed_at or '')[:16]}Z"
        f"{sections} (version {doc.version})."
    )


def outside_mark(doc: Document) -> str:
    """The one-line form of `outside_note` for a list of hits: `Note: spec was changed outside
    relaybay at <time> (3 sections, version N); doc_get(project, "spec") names them.`"""
    names = [a for a in (doc.file_changed_sections or "").split(",") if a]
    count = f"{len(names)} section{'s' if len(names) != 1 else ''}, " if names else ""
    return (
        f"Note: {doc.slug} was changed outside relaybay at {(doc.file_changed_at or '')[:16]}Z"
        f" ({count}version {doc.version}); doc_get(project, {doc.slug!r}) names them."
    )


_BOM = b"\xef\xbb\xbf"


def _empty_tail(lines: list[str]) -> int:
    n = 0
    while n < len(lines) and not lines[len(lines) - 1 - n].rstrip("\r"):
        n += 1
    return n


def splice(data: bytes, old: str, new: str) -> bytes:
    """The bytes of a file whose text (as `read_file` gives it: no byte order mark, LF) goes
    from `old` to `new`: only the lines that differ are written again, every other line keeps
    its bytes and its own line ending. A new line gets the ending of the line where the change
    starts (the line before it at the end of the file, else the file's: CRLF when it has any).
    The byte order mark and the newlines at the end of the file stay as they were. A carriage
    return left at a line's end (a CR CR LF ending) does not count as a difference: a sender
    that turns CRLF into LF loses it, and the line keeps its bytes."""
    bom = _BOM if data.startswith(_BOM) else b""
    pieces = data[len(bom) :].split(b"\n")
    raw = [(p[:-1], b"\r\n") if p.endswith(b"\r") else (p, b"\n") for p in pieces[:-1]]
    raw.append((pieces[-1], b""))
    old_lines, new_lines = old.split("\n"), new.split("\n")
    assert len(old_lines) == len(raw)
    # The empty lines at the end (a lone CR counts as empty) are the file's own.
    tail = _empty_tail(old_lines)
    new_lines = (
        new_lines[: len(new_lines) - _empty_tail(new_lines)] + old_lines[len(old_lines) - tail :]
    )
    n, m = len(old_lines), len(new_lines)
    first = 0
    old_cmp = [line.rstrip("\r") for line in old_lines]
    new_cmp = [line.rstrip("\r") for line in new_lines]
    while first < min(n, m) and old_cmp[first] == new_cmp[first]:
        first += 1
    same_end = 0
    while same_end < min(n, m) - first and old_cmp[n - 1 - same_end] == new_cmp[m - 1 - same_end]:
        same_end += 1
    if first < n and raw[first][1]:
        eol = raw[first][1]
    elif first > 0 and raw[first - 1][1]:
        eol = raw[first - 1][1]
    else:
        eol = b"\r\n" if any(e == b"\r\n" for _, e in raw) else b"\n"
    out = [
        *raw[:first],
        *[(line.encode("utf-8"), eol) for line in new_lines[first : m - same_end]],
        *raw[n - same_end :],
    ]
    # Every line but the last ends with a line ending; the last ends the file.
    fixed = [(line, e or eol) for line, e in out[:-1]]
    return bom + b"".join(line + e for line, e in [*fixed, (out[-1][0], b"")])


def read_only(path: Path) -> bool:
    """The file may not be written: no write permission, or the Windows read-only attribute
    (os.replace would overwrite a read-only file on POSIX and fail on Windows)."""
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    return not os.access(path, os.W_OK) or bool(
        attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0)
    )


def _remove(tmp: str) -> None:
    """Remove a temporary file, read-only or not; a failure here never hides the error that
    led to it."""
    with contextlib.suppress(OSError):
        Path(tmp).chmod(stat.S_IREAD | stat.S_IWRITE)
        Path(tmp).unlink()


def write_file(path: Path, data: bytes, expected_hash: str) -> FileStat | None:
    """Write `data` to `path` atomically: a temporary file in the same folder replaces it. The
    file is hashed again right before the replace; when it is no longer `expected_hash` nothing
    is written and the answer is None, else the size and time of the bytes written."""
    if read_only(path):
        raise ValidationError(f"{path} is read-only; nothing was written.")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        shutil.copymode(path, tmp)
        written = Path(tmp).stat()
        with path.open("rb") as fh:
            now = content_hash(fh.read(FILE_MAX_BYTES + 1))
        if now != expected_hash:
            _remove(tmp)
            return None
        Path(tmp).replace(path)
    except BaseException:
        _remove(tmp)
        raise
    return FileStat(written.st_size, written.st_mtime)
