"""The markdown layout of `relaybay import` and `export`: where things live and how their text reads
and renders. No I/O. Layout and mapping: docs/how-to/markdown-import-export.md."""

import hashlib
import json
import re
from collections.abc import Container
from dataclasses import dataclass, field
from typing import Any

from relaybay.domain.memory import DOC_KINDS, NOTE_KINDS, sections
from relaybay.domain.models import Decision, Task
from relaybay.domain.stages import Stage

REGISTRY = "TASKS.md"
RETRO = "retro.md"
MANIFEST = ".relaybay-export.json"
TASK_FILE = "TASK.md"
MEMORY_INDEX = "MEMORY"  # an index of the memory folder, not a note
TASK_LINK = "relaybay://doc/{project}/task-{ref}"
TITLE_SLUG_MAX = 60
# File names Windows refuses whatever the extension, and the memory index.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "memory", *(f"{d}{n}" for d in ("com", "lpt") for n in range(10))}
)

COLUMNS = ("project", "ref", "task", "stage", "waiting_on", "next_step", "document")
COLUMN_ALIASES = {
    "project": "project",
    "ref": "ref",
    "key": "ref",
    "id": "ref",
    "task": "task",
    "title": "task",
    "name": "task",
    "stage": "stage",
    "status": "stage",
    "state": "stage",
    "waiting on": "waiting_on",
    "waiting_on": "waiting_on",
    "waiting": "waiting_on",
    "next step": "next_step",
    "next_step": "next_step",
    "next": "next_step",
    "document": "document",
    "doc": "document",
    "doc_path": "document",
    "notes": "document",
}
STAGE_ALIASES = {
    **{s.value: s.value for s in Stage},
    "planned": "plan",
    "planning": "plan",
    "backlog": "plan",
    "todo": "plan",
    "to do": "plan",
    "idea": "plan",
    "new": "plan",
    "development": "dev",
    "in development": "dev",
    "wip": "dev",
    "in progress": "dev",
    "doing": "dev",
    "in review": "review",
    "code review": "review",
    "testing": "test",
    "in test": "test",
    "qa": "test",
    "staging": "test",
    "released": "live",
    "shipped": "live",
    "deployed": "live",
    "production": "live",
    "prod": "live",
    "closed": "done",
    "finished": "done",
    "complete": "done",
    "completed": "done",
    "archived": "done",
}
BACKLOG = "backlog.md"
BACKLOG_COLUMNS = ("date", "project", "area", "what", "origin", "decision")
BACKLOG_ALIASES = {
    **{c: c for c in BACKLOG_COLUMNS},
    "created": "date",
    "when": "date",
    "component": "area",
    "title": "what",
    "item": "what",
    "finding": "what",
    "source": "origin",
    "from": "origin",
    "status": "decision",
}
HEADING_NAMES = ("decisions", "task")
_STAGE_WORDS_MAX = 3
_DASHES = "-" + chr(0x2013) + chr(0x2014)
_SEPARATOR_RE = re.compile(rf"^\s*[{_DASHES}:{chr(0xB7)},]\s*")
_TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_BULLET_RE = re.compile(r"^[-*+][ \t]+(.*)$")
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$")
_ANY_HEADING_RE = re.compile(r"^ {0,3}#{1,6}([ \t]|$)")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n]+)\]\]")
_TASKS_PATH_RE = re.compile(r"(?:^|/)tasks/([^/]+)(?:/|$)")
_TASK_LINK_RE = re.compile(r"^relaybay://doc/[^/]+/task-(.+)$")
_BLOCK_MARKS = (">", "|", ">-", "|-", ">+", "|+")


def text_hash(*parts: Any) -> str:
    """The fingerprint of a value: stable across runs and machines."""
    data = json.dumps(parts, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def normalize(text: str) -> str:
    """Text as the service stores it, so an unchanged file compares equal to its target."""
    return text.replace("\r\n", "\n").strip()


def slugify(text: str, limit: int = TITLE_SLUG_MAX) -> str:
    """`Gift cards at checkout` -> `gift-cards-at-checkout`; letters of any language stay."""
    slug = re.sub(r"[^\w-]+|_+", "-", text.casefold()).strip("-")
    return re.sub(r"-{2,}", "-", slug)[:limit].strip("-")


def note_file_stem(title: str, note_id: int) -> str:
    """A file name for a note, safe on every platform; `note-<id>` when the title gives none."""
    slug = slugify(title)
    return slug if slug and slug not in _RESERVED_NAMES else f"note-{note_id}"


def doc_kind(name: str) -> str:
    return name if name in DOC_KINDS else "doc"


# --- where things live ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Place:
    kind: str  # registry, retro, brief, doc, task, note, decisions
    project: str | None = None
    name: str | None = None  # the document, the task folder or the note's file stem


def place_of(rel: str) -> Place | None:
    """What a path relative to the root is in the layout; None when it is not part of it. Names
    are not validated here."""
    parts = rel.split("/")
    if not parts[-1].endswith(".md") or any(
        p in ("", ".", "..") or "\\" in p or ":" in p for p in parts
    ):
        return None
    stem = parts[-1][: -len(".md")]
    if len(parts) == 1:
        return {
            REGISTRY: Place("registry"),
            RETRO: Place("retro"),
            BACKLOG: Place("backlog"),
        }.get(rel)
    if parts[0] == "memory":
        return Place("note", None, stem) if len(parts) == 2 else None
    if parts[0] != "projects":
        return None
    if len(parts) == 2:
        return Place("brief", stem)
    project = parts[1]
    if len(parts) == 3:
        return Place("decisions", project) if parts[2] == "decisions.md" else None
    if len(parts) == 4 and parts[2] == "docs":
        return Place("doc", project, stem)
    if len(parts) == 4 and parts[2] == "memory":
        return Place("note", project, stem)
    if len(parts) == 5 and parts[2] == "tasks" and parts[4] == TASK_FILE:
        return Place("task", project, parts[3])
    return None


def task_link(project: str, ref: str) -> str:
    return TASK_LINK.format(project=project, ref=ref)


def folder_from_document(cell: str | None) -> str | None:
    """The task folder a registry `document` cell points at: a path through `tasks/<ref>/` or a
    `relaybay://doc/<project>/task-<ref>` link."""
    if not cell:
        return None
    link = _TASK_LINK_RE.match(cell.strip())
    if link:
        return link.group(1)
    found = _TASKS_PATH_RE.search(cell.strip().replace("\\", "/"))
    return found.group(1) if found else None


def escapes_root(cell: str) -> bool:
    """A relative path that climbs out of wherever it starts (`../../x`)."""
    return ".." in cell.replace("\\", "/").split("/")


# --- aliases -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Aliases:
    columns: dict[str, str] = field(default_factory=lambda: dict(COLUMN_ALIASES))
    stages: dict[str, str] = field(default_factory=lambda: dict(STAGE_ALIASES))
    headings: dict[str, str] = field(default_factory=lambda: {n: n for n in HEADING_NAMES})
    backlog: dict[str, str] = field(default_factory=lambda: dict(BACKLOG_ALIASES))


def load_aliases(data: Any) -> Aliases:
    """The built-in aliases plus an `--aliases` file: `{"columns": {"their name": "column"},
    "stages": {"their word": "stage"}, "headings": {"their heading": "decisions" or "task"},
    "backlog": {"their name": "backlog column"}}`."""
    if not isinstance(data, dict):
        raise ValueError("the aliases file must hold a JSON object")
    unknown = sorted(set(data) - {"columns", "stages", "headings", "backlog"})
    if unknown:
        raise ValueError(
            f"unknown key(s) {', '.join(unknown)}; use columns, stages, headings, backlog"
        )
    allowed = {
        "columns": COLUMNS,
        "stages": tuple(s.value for s in Stage),
        "headings": HEADING_NAMES,
        "backlog": BACKLOG_COLUMNS,
    }
    base = Aliases()
    for section, known in allowed.items():
        extra = data.get(section, {})
        if not isinstance(extra, dict):
            raise ValueError(f"{section} must be an object of alias -> name")
        for alias, target in extra.items():
            if target not in known:
                raise ValueError(
                    f"{section}: {alias!r} maps to {target!r}; use one of: {', '.join(known)}"
                )
            getattr(base, section)[str(alias).strip().casefold()] = target
    return base


# --- the task registry ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegistryRow:
    line: int  # 1-based line in the file
    project: str
    title: str
    stage: str | None
    stage_note: str | None
    waiting_on: str | None
    next_step: str | None
    document: str | None
    ref: str | None
    unknown_stage: bool = False


def _split_row(line: str) -> list[str]:
    """The cells of a table line: `\\|` is a pipe, `\\\\` a backslash, `<br>` a new line and
    `\\<br>` the text `<br>`."""
    cells: list[str] = []
    current: list[str] = []
    text = line.strip()
    text = text[1:] if text.startswith("|") else text
    i = 0
    closed = False
    while i < len(text):
        char = text[i]
        closed = False
        if char == "\\" and i + 1 < len(text) and text[i + 1] in "|\\":
            current.append(text[i + 1])
            i += 2
            continue
        if text.startswith("\\<br>", i):
            current.append("<br>")
            i += 5
            continue
        if text.startswith("<br>", i):
            current.append("\n")
            i += 4
            continue
        if char == "|":
            cells.append("".join(current))
            current = []
            closed = True
        else:
            current.append(char)
        i += 1
    if not closed:
        cells.append("".join(current))
    return [c.strip() for c in cells]


def _cell(value: str | None) -> str:
    text = (value or "").replace("\\", "\\\\").replace("|", "\\|").replace("<br>", "\\<br>")
    return text.replace("\n", "<br>")


def _is_table_line(line: str) -> bool:
    return "|" in line and bool(line.strip())


def split_stage(cell: str, aliases: Aliases) -> tuple[str | None, str | None, bool]:
    """(stage, note, known). The leading stage word goes through the alias map and the rest is the
    note; an unknown word means stage plan with the whole text as the note."""
    text = cell.strip()
    if not text:
        return None, None, True
    words = list(re.finditer(r"\S+", text))
    for count in range(min(_STAGE_WORDS_MAX, len(words)), 0, -1):
        said = " ".join(m.group(0) for m in words[:count]).casefold().rstrip(":,." + _DASHES)
        stage = aliases.stages.get(said)
        if stage:
            rest = _SEPARATOR_RE.sub("", text[words[count - 1].end() :], count=1).strip()
            return stage, rest or None, True
    return Stage.PLAN.value, text, False


def parse_registry(text: str, aliases: Aliases) -> tuple[list[RegistryRow], list[str]]:
    """Every table with a project and a task column; tables without them give a warning."""
    rows: list[RegistryRow] = []
    warnings: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) - 1:
        if not (_is_table_line(lines[i]) and _TABLE_RULE_RE.match(lines[i + 1])):
            i += 1
            continue
        header = [aliases.columns.get(c.casefold()) for c in _split_row(lines[i])]
        start = i + 1
        i += 2
        table: list[tuple[int, list[str]]] = []
        while i < len(lines) and _is_table_line(lines[i]):
            table.append((i + 1, _split_row(lines[i])))
            i += 1
        if "project" not in header or "task" not in header:
            warnings.append(
                f"line {start}: a table without project and task columns was skipped"
                " (--aliases maps other column names)"
            )
            continue
        for number, cells in table:
            if not any(cells):
                continue
            values = {
                name: cells[n] if n < len(cells) else ""
                for n, name in enumerate(header)
                if name is not None
            }
            stage, note, known = split_stage(values.get("stage", ""), aliases)
            rows.append(
                RegistryRow(
                    line=number,
                    project=values["project"].strip("*` ").lower(),
                    title=values["task"],
                    stage=stage,
                    stage_note=note,
                    waiting_on=values.get("waiting_on") or None,
                    next_step=values.get("next_step") or None,
                    document=values.get("document") or None,
                    ref=values.get("ref") or None,
                    unknown_stage=not known,
                )
            )
    return rows, warnings


def registry_folder(
    project: str, ref: str | None, document: str | None, title: str | None, folders: Container[str]
) -> str | None:
    """The task folder (`<project>/<key>` in `folders`) a row means: its ref, its document cell or
    its title as a slug. Returns the key."""
    if document and escapes_root(document):
        document = None
    said = (ref, folder_from_document(document), document, title)
    return next((k for k in map(slugify, filter(None, said)) if f"{project}/{k}" in folders), None)


def row_doc_path(row: RegistryRow, folders: Container[str]) -> str | None:
    """The task's document field as an import of this row sets it."""
    key = registry_folder(row.project, row.ref, row.document, row.title, folders)
    if key:
        return task_link(row.project, key)
    return None if row.document and escapes_root(row.document) else row.document


def render_stage(task: Task) -> str:
    stage = task.stage.value
    return f"{stage} - {task.stage_note}" if task.stage_note else stage


def render_registry(rows: list[tuple[str, str, Task]]) -> str:
    """(project slug, ref, task) rows as the task registry."""
    lines = [
        "# Tasks",
        "",
        "| project | ref | task | stage | waiting on | next step | document |",
        "|---|---|---|---|---|---|---|",
    ]
    for project, ref, task in rows:
        cells = (
            project,
            ref,
            task.title,
            render_stage(task),
            task.waiting_on,
            task.next_step,
            task.doc_path,
        )
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


# --- the backlog -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BacklogRow:
    line: int
    date: str
    project: str
    area: str | None
    title: str
    origin: str | None
    decision: str


def parse_backlog(text: str, aliases: Aliases) -> tuple[list[BacklogRow], list[str]]:
    """Every table with a project and a what column; tables without them give a warning."""
    rows: list[BacklogRow] = []
    warnings: list[str] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) - 1:
        if not (_is_table_line(lines[i]) and _TABLE_RULE_RE.match(lines[i + 1])):
            i += 1
            continue
        header = [aliases.backlog.get(c.casefold()) for c in _split_row(lines[i])]
        start = i + 1
        i += 2
        table: list[tuple[int, list[str]]] = []
        while i < len(lines) and _is_table_line(lines[i]):
            table.append((i + 1, _split_row(lines[i])))
            i += 1
        if "project" not in header or "what" not in header:
            warnings.append(
                f"line {start}: a table without project and what columns was skipped"
                " (--aliases maps other column names under backlog)"
            )
            continue
        for number, cells in table:
            if not any(cells):
                continue
            values = {
                name: cells[n] if n < len(cells) else ""
                for n, name in enumerate(header)
                if name is not None
            }
            rows.append(
                BacklogRow(
                    line=number,
                    date=values.get("date", "").strip(),
                    project=values["project"].strip("*` ").lower(),
                    area=values.get("area", "").strip() or None,
                    title=values["what"].strip(),
                    origin=values.get("origin", "").strip().lower() or None,
                    decision=values.get("decision", "").strip(),
                )
            )
    return rows, warnings


def render_backlog(items: list[Any]) -> str:
    """Backlog items (domain.backlog.BacklogItem), oldest first, as backlog.md."""
    lines = [
        "# Backlog",
        "",
        "| date | project | area | what | origin | decision |",
        "|---|---|---|---|---|---|",
    ]
    for b in sorted(items, key=lambda b: (b.created_at, b.id)):
        cells = (b.created_at[:10], b.project_slug, b.area, b.title, b.origin, b.decision)
        lines.append("| " + " | ".join(_cell(c) for c in cells) + " |")
    return "\n".join(lines) + "\n"


def backlog_meaning(text: str, aliases: Aliases) -> set[tuple[Any, ...]]:
    rows, _ = parse_backlog(text, aliases)
    return {(r.date, r.project, r.area, r.title, r.origin, r.decision) for r in rows}


def registry_meaning(
    text: str, aliases: Aliases, folders: Container[str] | None = None
) -> set[tuple[Any, ...]]:
    """What a registry says, whatever its column names and order. With `folders` the document
    field is what an import of the row would set, else the cell as written."""
    rows, _ = parse_registry(text, aliases)
    return {
        (
            r.project,
            r.title,
            r.stage or Stage.PLAN.value,
            r.stage_note,
            r.waiting_on,
            r.next_step,
            r.document if folders is None else row_doc_path(r, folders),
        )
        for r in rows
    }


# --- text of documents ---------------------------------------------------------------------


def named_sections(body: str, name: str, aliases: Aliases) -> list[tuple[int, list[str]]]:
    """(0-based line of the first line under the heading, lines under it) for every heading that
    means `name`."""
    lines = body.split("\n")
    return [
        (s.start + 1, lines[s.start + 1 : s.end])
        for s in sections(body)
        if aliases.headings.get(s.title.casefold()) == name
    ]


def decision_bullets(body: str, aliases: Aliases) -> list[tuple[int, str]]:
    """(1-based line, text) of the bullets under `## Decisions`, each once; an indented line belongs
    to the bullet above it."""
    bullets: list[tuple[int, str]] = []
    for offset, lines in named_sections(body, "decisions", aliases):
        current: list[str] | None = None
        start = 0
        for n, line in enumerate(lines):
            mark = _BULLET_RE.match(line)
            if mark:
                if current is not None:
                    bullets.append((start, " ".join(current).strip()))
                current, start = [mark.group(1)], offset + n + 1
            elif current is not None and line.strip() and line[0] in " \t":
                current.append(line.strip())
            elif current is not None:
                bullets.append((start, " ".join(current).strip()))
                current = None
        if current is not None:
            bullets.append((start, " ".join(current).strip()))
    seen: set[str] = set()
    out = []
    for line, text in bullets:
        if text and text not in seen:
            seen.add(text)
            out.append((line, text))
    return out


def task_description(body: str, aliases: Aliases) -> str | None:
    """The `## Task` section, else the first paragraph that is not a heading."""
    found = named_sections(body, "task", aliases)
    if found:
        text = "\n".join(found[0][1]).strip()
        if text:
            return text
    paragraph: list[str] = []
    fence = False
    for line in body.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            if paragraph:
                break
            continue
        if fence:
            continue
        if _ANY_HEADING_RE.match(line) or not line.strip():
            if paragraph:
                break
            continue
        paragraph.append(line)
    return "\n".join(paragraph).strip() or None


def first_heading(body: str) -> str | None:
    """The text of the first `# Heading`, outside fenced code."""
    return next((s.title for s in sections(body) if s.level == 1 and s.title), None)


# --- notes ---------------------------------------------------------------------------------


def _scalar(value: str) -> str:
    text = value.strip()
    if text.startswith('"'):
        try:
            loaded = json.loads(text)
        except ValueError:
            return text.strip('"')
        return loaded if isinstance(loaded, str) else text
    if len(text) > 1 and text[0] == text[-1] == "'":
        return text[1:-1].replace("''", "'")
    return text


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(fields, the rest). The small part of YAML notes use: `key: value`, one level of nesting
    (`metadata.type`) and block scalars; anything else is ignored rather than guessed. A block
    without its closing `---` is not frontmatter."""
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0].rstrip() != "---":
        return {}, text.replace("\r\n", "\n")
    end = next((n for n, line in enumerate(lines[1:], 1) if line.rstrip() == "---"), None)
    if end is None:
        return {}, text.replace("\r\n", "\n")
    fields: dict[str, str] = {}
    parent: str | None = None
    block: str | None = None
    for line in lines[1:end]:
        if not line.strip():
            continue
        indented = line[0] in " \t"
        if indented and block:
            fields[block] = f"{fields[block]} {line.strip()}".strip()
            continue
        block = None
        key, sep, value = line.strip().partition(":")
        if not sep or not re.fullmatch(r"[\w.-]+", key):
            continue
        if not indented:
            parent = None
        name = f"{parent}.{key}" if indented and parent else key
        if value.strip() in _BLOCK_MARKS:
            fields[name], block = "", name
        elif value.strip():
            fields[name] = _scalar(value)
        elif not indented:
            parent = key
    return fields, "\n".join(lines[end + 1 :])


@dataclass(frozen=True, slots=True)
class ParsedNote:
    title: str
    kind: str
    body: str
    links: str | None
    tags: str | None
    role: str | None

    def values(self) -> dict[str, str]:
        """The fields a file can change on an existing note. Kind and role stay as created; links
        follow from the body."""
        found = {"title": self.title, "body": self.body}
        if self.tags:
            found["tags"] = self.tags
        return found


def _normal_tags(text: str) -> str | None:
    """Tags as the service stores them: 'UI, billing ui' -> 'ui, billing'."""
    tags = dict.fromkeys(t.lower() for t in re.split(r"[,\s]+", text) if t)
    return ", ".join(tags) or None


def parse_note(text: str, stem: str) -> ParsedNote:
    """Title from `name`, else a first `# Heading` (which then leaves the body), else the file
    name; kind from `metadata.type`, anything unknown is `reference`; the `description` becomes the
    first line of the body unless it already is."""
    fields, rest = split_frontmatter(text)
    body = normalize(rest)
    title = fields.get("name", "").strip()
    if not title:
        lines = body.split("\n")
        heading = _H1_RE.match(lines[0]) if lines else None
        if heading:
            title = heading.group(1)
            body = normalize("\n".join(lines[1:]))
    kind = (fields.get("metadata.type") or fields.get("type") or "").strip().lower()
    description = fields.get("description", "").strip()
    if description:
        first = next((line.strip() for line in body.split("\n") if line.strip()), "")
        if first != description:
            body = normalize(f"{description}\n\n{body}")
    links = list(dict.fromkeys(m.strip() for m in _WIKILINK_RE.findall(body)))
    return ParsedNote(
        title=title or stem,
        kind=kind if kind in NOTE_KINDS else "reference",
        body=body,
        links=", ".join(links) or None,
        tags=_normal_tags(fields.get("metadata.tags", "")),
        role=fields.get("metadata.role", "").strip().lower() or None,
    )


def render_note(
    title: str, kind: str, body: str, *, tags: str | None = None, role: str | None = None
) -> str:
    lines = ["---", f"name: {json.dumps(title, ensure_ascii=False)}", "metadata:"]
    lines.append(f"  type: {kind}")
    if role:
        lines.append(f"  role: {role}")
    if tags:
        lines.append(f"  tags: {json.dumps(tags, ensure_ascii=False)}")
    return "\n".join([*lines, "---", "", body]) + "\n"


# --- decisions -----------------------------------------------------------------------------


def _decision_lines(decision: Decision) -> list[str]:
    day = (decision.recorded_at or decision.created_at)[:10]
    task = f" ({decision.task_key})" if decision.task_key else ""
    text = decision.text.replace("\n", "\n  ")
    lines = [f"- #{decision.id} {day}{task}: {text}"]
    origin = " ".join(
        x for x in (decision.source_kind, decision.source_ref, decision.source_text) if x
    )
    facts = [
        f"Decided by {decision.decided_by}." if decision.decided_by else "",
        f"Source: {origin}." if origin else "",
    ]
    if decision.superseded_at:
        replaced = (
            f"Replaced by #{decision.superseded_by}" if decision.superseded_by else "Replaced"
        )
        facts.append(f"{replaced} on {decision.superseded_at[:10]}.")
    said = " ".join(f for f in facts if f)
    if said:
        lines.append(f"  {said}")
    return lines


def render_decisions(project: str, decisions: list[Decision]) -> str:
    """Current and superseded decisions in id order. Written for reading and grep: `import` does
    not read this file."""
    lines = [f"# Decisions of {project}", ""]
    for heading, group in (
        ("Current", [d for d in decisions if d.current]),
        ("Superseded", [d for d in decisions if not d.current]),
    ):
        if group:
            lines += [f"## {heading}", ""]
            for decision in group:
                lines += _decision_lines(decision)
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"
