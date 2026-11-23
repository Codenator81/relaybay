"""A card's change document `spec-<task key>`: what the card changes in the main spec before it
is deployed, as a delta per main-spec section. `## <doc>#<anchor>` names the section; under it
`ADDED:` text goes below that section's text, `MODIFIED:` text replaces it (a `Was:` line quotes
the old value and is no part of the new text), `REMOVED:` deletes the section. A new section is
`ADDED:` under its parent, starting with its own heading. The format is checked softly: problems
are reported, nothing is refused."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from relaybay.domain.doc_index import IndexedSection, lookup
from relaybay.domain.memory import DOC_SLUG_RE

CHANGE_DOC_PREFIX = "spec-"
OPS = ("ADDED", "MODIFIED", "REMOVED")
OPS_TEXT = "ADDED:, MODIFIED: or REMOVED:"

_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_TARGET_RE = re.compile(r"^##[ \t]+(.*?)[ \t]*$")
# An op line is a word in capitals and a colon, alone or followed by the block's first line.
_OP_RE = re.compile(r"^([A-Z][A-Z_]+):[ \t]*(.*?)[ \t]*$")
_WAS_RE = re.compile(r"^Was:[ \t]*(.*?)[ \t]*$")
_ANCHOR_RE = re.compile(r"^[\w-]+$")


def change_doc_slug(key: str) -> str:
    return f"{CHANGE_DOC_PREFIX}{key.casefold()}"


@dataclass(frozen=True, slots=True)
class ChangeBlock:
    """One change of a main-spec section; `line` is the 1-based line of its op line."""

    doc: str
    anchor: str
    op: str
    text: str
    was: str | None
    line: int

    @property
    def target(self) -> str:
        return f"{self.doc}#{self.anchor}"


@dataclass(slots=True)
class ChangeDoc:
    blocks: list[ChangeBlock] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def targets(self) -> list[tuple[str, str]]:
        """(doc, anchor) of every section the document changes, each once, in order."""
        return list(dict.fromkeys((b.doc, b.anchor) for b in self.blocks))


@dataclass(slots=True)
class _Open:
    doc: str
    anchor: str
    op: str
    line: int
    lines: list[str] = field(default_factory=list)
    was: list[str] = field(default_factory=list)


def _block(item: _Open, problems: list[str]) -> ChangeBlock:
    lines = list(item.lines)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    text = "\n".join(lines)
    if not text and item.op != "REMOVED":
        problems.append(f"line {item.line}: {item.op} under {item.doc}#{item.anchor} has no text")
    return ChangeBlock(item.doc, item.anchor, item.op, text, "\n".join(item.was) or None, item.line)


def parse(body: str) -> ChangeDoc:
    """The blocks of a change document and the problems of its format. The text before the first
    `## ` heading (a title, a summary) is no change."""
    out = ChangeDoc()
    problems = out.problems
    target: tuple[str, str] | None = None
    seen_heading = False
    blocks_here = 0
    current: _Open | None = None
    fence: str | None = None
    stray = False  # text under a target before its first op line, reported once

    def close() -> None:
        nonlocal current
        if current is not None:
            out.blocks.append(_block(current, problems))
        current = None

    def end_target(line: int) -> None:
        if target is not None and not blocks_here:
            problems.append(f"line {line}: {target[0]}#{target[1]} has no {OPS_TEXT} block")

    lines = body.split("\n")
    target_line = 0
    for n, line in enumerate(lines, 1):
        mark = _FENCE_RE.match(line)
        if mark:
            token = mark.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
        heading = _TARGET_RE.match(line) if fence is None and not mark else None
        if heading:
            close()
            end_target(target_line)
            seen_heading, blocks_here, stray, target_line = True, 0, False, n
            doc, sep, anchor = heading.group(1).partition("#")
            doc, anchor = doc.strip().casefold(), anchor.strip().casefold()
            if sep and DOC_SLUG_RE.match(doc) and _ANCHOR_RE.match(anchor):
                target = (doc, anchor)
            else:
                target = None
                problems.append(
                    f"line {n}: heading {line.strip()!r} is not '## <doc>#<anchor>' of a main-spec"
                    " section (e.g. '## spec#gift-cards')"
                )
            continue
        op = _OP_RE.match(line) if fence is None and not mark else None
        if op and (op.group(1) in OPS or not op.group(2)):
            close()
            if op.group(1) not in OPS:
                problems.append(f"line {n}: unknown change {op.group(1)}:; use {OPS_TEXT}")
                continue
            if target is None:
                if not seen_heading:
                    problems.append(
                        f"line {n}: {op.group(1)}: before the first '## <doc>#<anchor>' heading"
                    )
                continue
            current = _Open(target[0], target[1], op.group(1), n)
            blocks_here += 1
            if op.group(2):
                current.lines.append(op.group(2))
            continue
        if current is not None:
            was = _WAS_RE.match(line) if current.op == "MODIFIED" and fence is None else None
            if was:
                current.was.append(was.group(1))
            else:
                current.lines.append(line)
        elif target is not None and line.strip() and not stray:
            stray = True
            problems.append(
                f"line {n}: text under {target[0]}#{target[1]} before its {OPS_TEXT} line"
            )
    close()
    end_target(target_line)
    if not seen_heading and body.strip():
        problems.append(
            "no '## <doc>#<anchor>' heading: name the main-spec section each change is for"
        )
    return out


def target_problems(
    doc: ChangeDoc, outlines: Mapping[str, list[IndexedSection] | None]
) -> list[str]:
    """A target whose document (None in `outlines`) or section does not exist."""
    out = []
    for slug, anchor in doc.targets():
        rows = outlines.get(slug)
        if rows is None:
            out.append(f"{slug}#{anchor}: the project has no document {slug}")
        elif lookup(rows, anchor) is None:
            out.append(f"{slug}#{anchor}: {slug} has no section {anchor}")
    return out


@dataclass(frozen=True, slots=True)
class ChangeMark:
    """An open card changing a section: its key, stage and change document."""

    key: str
    stage: str
    change_doc: str


def marks_by_anchor(
    rows: list[IndexedSection], changes: list[tuple[ChangeMark, list[str]]]
) -> dict[str, list[ChangeMark]]:
    """The marks per heading anchor of a document, from each card's anchors in it (its change
    document's targets and its linked sections); an anchor the outline lacks marks nothing."""
    out: dict[str, list[ChangeMark]] = {}
    for mark, anchors in changes:
        for anchor in anchors:
            row = lookup(rows, anchor)
            if row is None or not row.anchor:
                continue
            marks = out.setdefault(row.anchor, [])
            if mark not in marks:
                marks.append(mark)
    return out


def mark_line(mark: ChangeMark, link: str) -> str:
    return f"changing on {mark.stage}: {mark.key} · {link}"
