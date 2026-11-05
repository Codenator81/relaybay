"""`relaybay import`: bring a markdown setup into relaybay. A dry run prints the plan and its hash;
`--apply --expect <hash>` writes exactly that plan through `Service`, one item at a time, so one bad
item never stops the others. Layout and mapping: docs/how-to/markdown-import-export.md."""

import asyncio
import contextlib
import difflib
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from relaybay.config import Settings
from relaybay.domain.environments import DEFAULT_SECRET_RULES, SecretRules, find_secrets
from relaybay.domain.errors import DomainError, NotFoundError
from relaybay.domain.markdown_layout import (
    MEMORY_INDEX,
    REGISTRY,
    RETRO,
    Aliases,
    ParsedNote,
    RegistryRow,
    decision_bullets,
    doc_kind,
    escapes_root,
    first_heading,
    folder_from_document,
    load_aliases,
    normalize,
    parse_note,
    parse_registry,
    place_of,
    registry_folder,
    slugify,
    task_description,
    task_link,
    text_hash,
)
from relaybay.domain.memory import (
    DOC_BODY_MAX,
    DOC_SLUG_RE,
    NOTE_BODY_MAX,
    TITLE_MAX,
    MemoryNote,
    diff_lines,
)
from relaybay.domain.models import Decision, ImportItem, Project, Task
from relaybay.domain.task_fields import MARKDOWN_MAX, NAME_MAX, TEXT_MAX
from relaybay.service import SLUG_RE, Service

ACTOR = "import"
HIDDEN_NAME = "(hidden name)"
ROW_CELLS = ("project", "ref", "title", "stage_note", "waiting_on", "next_step", "document")
REASON = "import"
FILE_MAX_BYTES = 2_000_000
DIFF_ITEM_LINES = 40
DIFF_TOTAL_LINES = 400
# An edited bullet is one whose text still shares this much with a bullet that left the file.
SAME_DECISION_RATIO = 0.6
KINDS = ("project", "document", "task", "task_note", "decision", "note", "file")
ACTIONS = ("add", "update", "unchanged", "skipped", "conflict", "refused", "error")
FAILED = ("conflict", "refused", "error")
FAILED_NEW = ("refused", "error")  # a task in one of these does not exist for what depends on it
TASK_FIELDS = ("title", "stage", "stage_note", "waiting_on", "next_step", "doc_path")
CHANGED_ON_BOARD = (
    "changed on the board since the last import; 'relaybay export' brings the file up to date"
    " (with --force for a file it did not write), --overwrite takes the file's text"
)
NOT_IMPORTED = "already on the board and not from an import; --overwrite takes the file's text"


class ImportProblem(Exception):
    """The run cannot start: a bad root or aliases file."""


@dataclass(slots=True)
class Item:
    kind: str
    item: str
    source: str
    action: str
    detail: str = ""
    content_hash: str = ""
    target_hash: str = ""  # of the target as it is now
    record: bool = False  # nothing to write on the board, but the bookkeeping changes
    diff: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    fixed: str = ""  # what a new item keeps for good and its hashes do not cover

    @property
    def order(self) -> tuple[int, str, str]:
        return KINDS.index(self.kind), self.item, self.source


@dataclass(slots=True)
class Plan:
    items: list[Item]
    warnings: list[str]

    @property
    def hash(self) -> str:
        rows = [
            (i.kind, i.item, i.source, i.action, i.content_hash, i.target_hash, i.record, i.fixed)
            for i in self.items
        ]
        return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()[:16]

    @property
    def failed(self) -> bool:
        return any(i.action in FAILED for i in self.items)

    @property
    def writes(self) -> bool:
        return any(i.action in ("add", "update") or i.record for i in self.items)


# --- reading the folder ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceFile:
    rel: str
    text: str


def _read(path: Path, rel: str) -> SourceFile | Item:
    size = path.stat().st_size
    if size > FILE_MAX_BYTES:
        return Item(
            "file",
            rel,
            rel,
            "refused",
            f"the file has {size} bytes; the limit is {FILE_MAX_BYTES}",
        )
    try:
        return SourceFile(rel, path.read_bytes().decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        return Item("file", rel, rel, "error", f"not valid UTF-8 (byte {exc.start})")


def scan(
    root: Path, rules: SecretRules = DEFAULT_SECRET_RULES
) -> tuple[list[SourceFile], list[Item]]:
    """The markdown files of the layout, never following a link out of `root`: the top level plus
    `projects/` and `memory/`. Everything else in the folder is none of the import's business. A
    path holding a secret value goes no further and is only shown with that name hidden."""
    files: list[SourceFile] = []
    items: list[Item] = []

    def visit(folder: Path, prefix: str, top: bool) -> None:
        with os.scandir(folder) as found:
            entries = sorted(found, key=lambda e: e.name)
        for entry in entries:
            rel = prefix + entry.name
            if entry.name.startswith(".") or (
                top and entry.is_dir() and entry.name not in ("projects", "memory")
            ):
                continue
            found_secret = find_secrets(rel, rules)
            if found_secret:
                hidden = f"{prefix}{HIDDEN_NAME}"
                detail = f"a name here holds a secret value ({found_secret[0][1]}); rename it"
                items.append(Item("file", hidden, hidden, "refused", detail))
                continue
            if entry.is_symlink() or os.path.isjunction(entry.path):
                target = Path(entry.path).resolve()
                if not target.is_relative_to(root):
                    items.append(
                        Item("file", rel, rel, "refused", "a link leading out of the root")
                    )
                    continue
                if target.is_dir():  # read at its real place in the root
                    continue
            if entry.is_dir():
                visit(Path(entry.path), f"{rel}/", False)
            elif entry.name.endswith(".md") and entry.is_file():
                if place_of(rel) is None:
                    items.append(Item("file", rel, rel, "skipped", "not part of the layout"))
                    continue
                try:
                    read = _read(Path(entry.path), rel)
                except OSError as exc:
                    items.append(Item("file", rel, rel, "error", f"cannot read: {exc.strerror}"))
                    continue
                if isinstance(read, SourceFile):
                    files.append(read)
                else:
                    items.append(read)

    visit(root, "", True)
    return files, items


# --- planning --------------------------------------------------------------------------------


def _decide(
    *,
    exists: bool,
    tracked: ImportItem | None,
    content_hash: str,
    target_hash: str,
    same: bool,
    overwrite: bool,
) -> tuple[str, str, bool]:
    """(action, detail, record) for an item that has a target of its own. The board is never
    overwritten silently: a target not written by an import, or changed since, is a conflict
    unless the file already says the same."""
    if not exists:
        return "add", "", False
    if tracked is not None and target_hash == tracked.target_hash:
        if same or content_hash == tracked.content_hash:
            return "unchanged", "", content_hash != tracked.content_hash
        return "update", "", False
    if same:
        return "unchanged", "", True
    if overwrite:
        return "update", "--overwrite", False
    return "conflict", NOT_IMPORTED if tracked is None else CHANGED_ON_BOARD, False


def task_hash(task: Task) -> str:
    return text_hash(*(str(getattr(task, name) or "") for name in TASK_FIELDS))


def note_hash(note: MemoryNote) -> str:
    return text_hash(note.title, note.body, note.tags)


def _note_same(note: MemoryNote, parsed: ParsedNote) -> bool:
    return all(getattr(note, name) == value for name, value in parsed.values().items())


def _first_line(text: str, limit: int = 300) -> str:
    line = text.strip().split("\n")[0]
    return line if len(line) <= limit else line[: limit - 1] + "…"


@dataclass(slots=True)
class _Row:
    """A task as the files describe it: a registry row and / or a task folder."""

    project: str
    identity: str  # the ref, stable across imports and exports
    source: str
    line: int
    values: dict[str, str]
    folder: SourceFile | None = None
    warning: str | None = None  # shown once the row passed the secret scan
    explicit_ref: bool = False  # the registry names its ref: never matched by title


class _Planner:
    def __init__(
        self, service: Service, aliases: Aliases, retro_project: str | None, overwrite: bool
    ) -> None:
        self.service = service
        self.aliases = aliases
        self.retro_project = retro_project
        self.overwrite = overwrite
        self.items: list[Item] = []
        self.warnings: list[str] = []
        self.tracked: dict[tuple[str, str], ImportItem] = {}
        self.projects: dict[str, Project] = {}
        self.new_projects: dict[str, str | None] = {}  # slug -> name
        self.tasks: dict[str, list[Task]] = {}
        self.decisions: dict[str, list[Decision]] = {}
        self.rules: dict[str, SecretRules] = {}
        self.task_items: dict[str, Item] = {}
        self.title_claims: Counter[tuple[str, str | None]] = Counter()
        self.notes: list[MemoryNote] = []  # current ones
        self.tracked_notes: dict[int, MemoryNote] = {}  # by id, superseded ones too

    def add(self, *args: Any, **kwargs: Any) -> Item:
        item = Item(*args, **kwargs)
        self.items.append(item)
        return item

    async def _secret_rules(self, project: str | None) -> SecretRules:
        key = project or ""
        if key not in self.rules:
            self.rules[key] = await self.service.secret_rules(project)
        return self.rules[key]

    async def _secret(self, text: str, project: str | None, base_line: int = 0) -> str | None:
        """Where `text` holds a secret: lines and detectors, never the value."""
        found = find_secrets(text, await self._secret_rules(project))
        if not found:
            return None
        places = ", ".join(f"line {base_line + line} ({name})" for line, name in found)
        return f"secret value at {places}; store where it lives instead, never the value"

    def _valid_project(self, value: str, source: str) -> str | None:
        slug = value.strip().lower()
        if SLUG_RE.match(slug):
            return slug
        self.add("file", source, source, "refused", f"invalid project name {value!r}")
        return None

    async def _need_project(self, slug: str, name: str | None = None) -> None:
        if slug in self.projects:
            return
        try:
            self.projects[slug] = await self.service.get_project(slug)
        except NotFoundError:
            if name and (found := find_secrets(name, await self._secret_rules(slug))):
                self.warnings.append(f"projects/{slug}.md: heading not used ({found[0][1]})")
                name = None
            if name or slug not in self.new_projects:
                self.new_projects[slug] = name[:NAME_MAX] if name else None
            return
        self.tasks[slug] = await self.service.list_tasks(project=slug, include_done=True)
        self.decisions[slug] = await self.service.all_decisions(self.projects[slug].id)

    # Documents

    async def _document(
        self, project: str, doc: str, file: SourceFile, kind: str, base_line: int = 0
    ) -> None:
        name = f"{project}/{doc}"
        raw = file.text.replace("\r\n", "\n")
        body = normalize(raw)
        if not body:
            self.add("document", name, file.rel, "skipped", "the file is empty")
            return
        if len(body) > DOC_BODY_MAX:
            self.add(
                "document",
                name,
                file.rel,
                "refused",
                f"{len(body)} characters; a document holds at most {DOC_BODY_MAX}",
            )
            return
        secret = await self._secret(raw, project, base_line)
        if secret:
            self.add("document", name, file.rel, "refused", secret)
            return
        current, text = None, ""
        if project in self.projects:
            with contextlib.suppress(NotFoundError):  # not created yet: an add
                current, text = await self.service.get_document(project, doc)
        target_hash = text_hash(text) if current else ""
        tracked = self.tracked.get(("document", name))
        action, detail, record = _decide(
            exists=current is not None,
            tracked=tracked,
            content_hash=text_hash(body),
            target_hash=target_hash,
            same=current is not None and text == body,
            overwrite=self.overwrite,
        )
        if current is not None and current.source_url and action in ("update", "conflict"):
            detail = f"lives in the repository ({current.source_url}); change it there"
            self.add("document", name, file.rel, "skipped", detail)
            return
        diff = []
        if current and action in ("update", "conflict"):
            diff = diff_lines(text, body, f"{name} (board)", file.rel)
        self.add(
            "document",
            name,
            file.rel,
            action,
            detail,
            text_hash(body),
            target_hash,
            record,
            diff,
            {
                "project": project,
                "doc": doc,
                "body": body,
                "kind": kind,
                "base": current.version if current else 0,
                "target_id": current.id if current else 0,
            },
        )

    # Decisions

    async def _decisions(
        self, project: str, ref: str | None, file: SourceFile, body: str, blocked: str | None
    ) -> None:
        """Append-only: a new bullet adds a decision; one that replaces a bullet gone from the file
        supersedes it; a bullet gone without a successor changes nothing."""
        scope = f"{project}/{ref}" if ref else project
        bullets = decision_bullets(body, self.aliases)
        known = [t for (kind, item), t in self.tracked.items() if _in_scope(kind, item, scope)]
        if not bullets and not known:
            return
        decisions = {d.id: d for d in self.decisions.get(project, [])}
        names = {_decision_item(scope, text) for _, text in bullets}
        gone = [
            t
            for t in known
            if t.item not in names and t.target_id in decisions and decisions[t.target_id].current
        ]
        fresh: list[tuple[int, str, str]] = []
        for line, text in bullets:
            name = _decision_item(scope, text)
            tracked = self.tracked.get(("decision", name))
            if tracked is None or tracked.target_id not in decisions:
                fresh.append((line, text, name))
            elif decisions[tracked.target_id].current:
                self.add(
                    "decision",
                    name,
                    file.rel,
                    "unchanged",
                    content_hash=text_hash(text),
                    target_hash=text_hash(text),
                )
            else:
                self.add(
                    "decision",
                    name,
                    file.rel,
                    "skipped",
                    f"superseded on the board by #{decisions[tracked.target_id].superseded_by}",
                )
        for line, text, name in fresh:
            await self._new_decision(project, ref, file, name, line, text, gone, blocked)
        for tracked in gone:
            shown = _first_line(decisions[tracked.target_id].text, 80)
            self.add(
                "decision",
                tracked.item,
                file.rel,
                "skipped",
                f"the bullet left the file; decision #{tracked.target_id} stays: {shown}",
            )

    async def _new_decision(
        self,
        project: str,
        ref: str | None,
        file: SourceFile,
        name: str,
        line: int,
        text: str,
        gone: list[ImportItem],
        blocked: str | None,
    ) -> None:
        if blocked:
            self.add("decision", name, file.rel, "skipped", blocked)
            return
        if len(text) > TEXT_MAX:
            self.add(
                "decision",
                name,
                file.rel,
                "refused",
                f"line {line}: {len(text)} characters; a decision holds at most {TEXT_MAX}",
            )
            return
        secret = await self._secret(text, project, line - 1)
        if secret:
            self.add("decision", name, file.rel, "refused", secret)
            return
        hashed = text_hash(text)
        decisions = self.decisions.get(project, [])
        task_id = self.task_items[f"{project}/{ref}"].payload.get("target_id") if ref else None
        twin = None
        if not ref or task_id:
            twin = next(
                (d for d in decisions if d.current and d.task_id == task_id and d.text == text),
                None,
            )
        if twin is not None:
            self.add(
                "decision",
                name,
                file.rel,
                "unchanged",
                content_hash=hashed,
                target_hash=hashed,
                record=True,
                payload={"target_id": twin.id},
            )
            return
        by_id = {d.id: d for d in decisions}
        best: ImportItem | None = None
        score = SAME_DECISION_RATIO
        for tracked in gone:
            ratio = difflib.SequenceMatcher(None, by_id[tracked.target_id].text, text).ratio()
            if ratio >= score:
                best, score = tracked, ratio
        shown = _first_line(text, 80)
        payload: dict[str, Any] = {
            "project": project,
            "ref": f"{project}/{ref}" if ref else None,
            "text": text,
        }
        if best is None:
            self.add("decision", name, file.rel, "add", shown, hashed, payload=payload)
            return
        gone.remove(best)
        payload.update(supersedes=best.target_id, forget=best.item)
        detail = f"supersedes #{best.target_id}: {shown}"
        self.add("decision", name, file.rel, "update", detail, hashed, payload=payload)

    # Tasks

    def _existing_task(self, row: _Row) -> Task | None:
        """The task of a row: the one imported for it, else one no other item is imported for:
        the one whose key is its ref, or without an explicit ref the only one with its title (when
        only this row claims that title, so the order of the rows does not matter)."""
        tasks = self.tasks.get(row.project, [])
        tracked = self.tracked.get(("task", f"{row.project}/{row.identity}"))
        if tracked is not None:
            found = next((t for t in tasks if t.id == tracked.target_id), None)
            if found is not None:
                return found
        tied = {t.target_id for t in self.tracked.values() if t.kind == "task"}
        free = [t for t in tasks if t.id not in tied]
        found = next((t for t in free if t.key == row.identity), None)
        if found is not None:
            return found
        title = row.values.get("title")
        if row.explicit_ref or self.title_claims[(row.project, title)] > 1:
            return None
        named = [t for t in free if t.title == title]
        return named[0] if len(named) == 1 else None

    async def _row_secrets(self, row: RegistryRow) -> dict[str, str]:
        """Cell -> where it holds a secret. Runs first: a ref, a title or a document cell can become
        the item's name, and the name reaches the output and the database."""
        project = row.project if SLUG_RE.match(row.project) else None
        found = {}
        for cell in ROW_CELLS:
            secret = await self._secret(getattr(row, cell) or "", project, row.line - 1)
            if secret:
                found[cell] = secret
        return found

    def _refuse_row(
        self, row: RegistryRow, secrets: dict[str, str], folders: dict[str, SourceFile]
    ) -> None:
        """Shown by its line only. A task folder of the row waits with it, or it would come in as a
        task of its own."""
        source = f"{REGISTRY}:{row.line}"
        self.add("task", source, REGISTRY, "refused", "; ".join(secrets.values()))
        cells = (None if c in secrets else getattr(row, c) for c in ("ref", "document", "title"))
        key = registry_folder(row.project, *cells, folders)
        folder = folders.pop(f"{row.project}/{key}", None) if key else None
        if folder is not None:
            detail = f"its registry row {source} was refused"
            self.add("file", folder.rel, folder.rel, "skipped", detail)

    def _registry_row(self, row: RegistryRow, folders: dict[str, SourceFile]) -> _Row | None:
        source = f"{REGISTRY}:{row.line}"
        if not SLUG_RE.match(row.project):
            self.add("task", source, REGISTRY, "error", "invalid project name")
            return None
        if not row.title:
            self.add("task", source, REGISTRY, "error", "the row has no task title")
            return None
        document = row.document
        if document and escapes_root(document):
            self.warnings.append(f"{source}: the document path climbs out of the root; ignored")
            document = None
        pointed = folder_from_document(document)
        folder_key = registry_folder(row.project, row.ref, document, row.title, folders)
        identity = slugify(row.ref or "") or (slugify(pointed) if pointed else "")
        identity = identity or folder_key or slugify(row.title)
        if not identity:
            self.add("task", source, REGISTRY, "error", "no ref can be made from the title")
            return None
        values = {"title": row.title}
        for name in ("stage", "stage_note", "waiting_on", "next_step"):
            value = getattr(row, name)
            if value:
                values[name] = value
        if document:
            values["doc_path"] = document
        folder = folders.pop(f"{row.project}/{folder_key}", None) if folder_key else None
        if folder is not None:
            values["doc_path"] = task_link(row.project, folder_key or "")
        warning = None
        if row.unknown_stage:
            warning = (
                f"{source}: unknown stage {_first_line(row.stage_note or '', 40)!r}; plan, with"
                " the text as the note (--aliases maps other stage words)"
            )
        explicit = bool(slugify(row.ref or ""))
        return _Row(row.project, identity, REGISTRY, row.line, values, folder, warning, explicit)

    async def _task(self, row: _Row) -> None:
        name = f"{row.project}/{row.identity}"
        too_long = next((k for k, v in row.values.items() if len(v) > TEXT_MAX), None)
        if too_long or len(row.values["title"]) > TITLE_MAX:
            item = self.add(
                "task",
                name,
                row.source,
                "refused",
                f"{too_long or 'title'} is too long (line {row.line})",
            )
            self.task_items[name] = item
            return
        for value in row.values.values():
            secret = await self._secret(value, row.project, row.line - 1)
            if secret:
                self.task_items[name] = self.add("task", name, row.source, "refused", secret)
                return
        if row.warning:
            self.warnings.append(row.warning)
        task = self._existing_task(row)
        tracked = self.tracked.get(("task", name))
        changes = {
            k: v for k, v in row.values.items() if task is None or str(getattr(task, k) or "") != v
        }
        content_hash = text_hash(row.values)
        target_hash = task_hash(task) if task else ""
        action, detail, record = _decide(
            exists=task is not None,
            tracked=tracked,
            content_hash=content_hash,
            target_hash=target_hash,
            same=not changes,
            overwrite=self.overwrite,
        )
        if task and action in ("update", "conflict"):
            shown = ", ".join(
                f"{k}: {_first_line(str(getattr(task, k) or '-'), 40)} -> {_first_line(v, 40)}"
                for k, v in changes.items()
            )
            detail = f"{detail}; {shown}" if detail else shown
        self.task_items[name] = self.add(
            "task",
            name,
            row.source,
            action,
            detail,
            content_hash,
            target_hash,
            record,
            [],
            {
                "project": row.project,
                "values": row.values,
                "changes": changes,
                "key": task.key if task else None,
                "target_id": task.id if task else 0,
            },
        )

    async def _task_note(self, row: _Row, file: SourceFile, description: str | None) -> None:
        """The description a TASK.md gives. Export cannot write a description back into the file,
        so a board edit alone is no conflict: only a file that changed since the import can be."""
        name = f"{row.project}/{row.identity}"
        task_item = self.task_items[name]
        if description is None or task_item.action in FAILED_NEW:
            return
        if len(description) > MARKDOWN_MAX:
            self.add(
                "task_note",
                name,
                file.rel,
                "refused",
                f"{len(description)} characters; a description holds at most {MARKDOWN_MAX}",
            )
            return
        raw = file.text.replace("\r\n", "\n")
        at = raw.find(description.split("\n")[0])
        secret = await self._secret(description, row.project, raw.count("\n", 0, max(at, 0)))
        if secret:
            self.add("task_note", name, file.rel, "refused", secret)
            return
        task = self._existing_task(row)
        tracked = self.tracked.get(("task_note", name))
        content_hash = text_hash(description)
        target_hash = text_hash(task.description or "") if task else ""
        if tracked is not None and tracked.content_hash == content_hash:
            action, detail, record = "unchanged", "", False
        else:
            action, detail, record = _decide(
                exists=task is not None and bool(task.description),
                tracked=tracked,
                content_hash=content_hash,
                target_hash=target_hash,
                same=task is not None and task.description == description,
                overwrite=self.overwrite,
            )
        self.add(
            "task_note",
            name,
            file.rel,
            action,
            detail,
            content_hash,
            target_hash,
            record,
            [],
            {"description": description, "target_id": task.id if task else 0},
        )
        if task is None:
            task_item.payload["description"] = description

    async def _tasks(self, files: list[SourceFile]) -> None:
        folders: dict[str, SourceFile] = {}
        registry: SourceFile | None = None
        raw_names: dict[str, str] = {}
        for file in files:
            place = place_of(file.rel)
            if place is None:
                continue
            if place.kind == "registry":
                registry = file
            elif place.kind == "task":
                slug = self._valid_project(place.project or "", file.rel)
                ref = slugify(place.name or "")
                if slug is None:
                    continue
                if not DOC_SLUG_RE.match(f"task-{ref}"):
                    self.add("file", file.rel, file.rel, "refused", "invalid task folder name")
                elif f"{slug}/{ref}" in folders:
                    self.add(
                        "file",
                        file.rel,
                        file.rel,
                        "error",
                        f"same task as {folders[f'{slug}/{ref}'].rel}",
                    )
                else:
                    folders[f"{slug}/{ref}"] = file
                    raw_names[f"{slug}/{ref}"] = place.name or ref
        rows: list[_Row] = []
        if registry is not None:
            parsed, warnings = parse_registry(registry.text, self.aliases)
            self.warnings.extend(f"{REGISTRY}: {w}" for w in warnings)
            seen: set[str] = set()
            for row in parsed:
                secrets = await self._row_secrets(row)
                if secrets:
                    self._refuse_row(row, secrets, folders)
                    continue
                made = self._registry_row(row, folders)
                if made is None:
                    continue
                name = f"{made.project}/{made.identity}"
                if name in seen:
                    self.add(
                        "task",
                        f"{REGISTRY}:{row.line}",
                        REGISTRY,
                        "error",
                        f"a second row for {name}",
                    )
                    continue
                seen.add(name)
                rows.append(made)
        for key, file in folders.items():
            project, ref = key.split("/", 1)
            title = first_heading(file.text.replace("\r\n", "\n")) or raw_names[key]
            values = {"title": title, "stage": "plan", "doc_path": task_link(project, ref)}
            rows.append(_Row(project, ref, file.rel, 1, values, file))
        self.title_claims.update(
            (r.project, r.values.get("title")) for r in rows if not r.explicit_ref
        )
        for row in rows:
            await self._need_project(row.project)
            await self._task(row)
        for row in rows:
            if row.folder is None:
                continue
            place = place_of(row.folder.rel)
            assert place is not None
            doc_ref = slugify(place.name or "")
            body = normalize(row.folder.text)
            await self._document(row.project, f"task-{doc_ref}", row.folder, "task")
            await self._task_note(row, row.folder, task_description(body, self.aliases))
            task_item = self.task_items[f"{row.project}/{row.identity}"]
            blocked = "its task was not imported" if task_item.action in FAILED_NEW else None
            await self._decisions(row.project, row.identity, row.folder, body, blocked)

    # Notes

    async def _note(self, file: SourceFile, project: str | None, stem: str) -> None:
        name = f"{project or 'global'}/{stem}"
        parsed = parse_note(file.text, stem)
        if project:  # the folder says whose it is
            parsed = replace(parsed, role=None)
        if not parsed.body:
            self.add("note", name, file.rel, "skipped", "the note has no text")
            return
        if len(parsed.body) > NOTE_BODY_MAX or len(parsed.title) > TITLE_MAX:
            self.add(
                "note",
                name,
                file.rel,
                "refused",
                f"a note holds at most {NOTE_BODY_MAX} characters and a title {TITLE_MAX}",
            )
            return
        secret = await self._secret(file.text.replace("\r\n", "\n"), project)
        secret = secret or await self._parsed_note_secret(parsed, project)
        if secret:
            self.add("note", name, file.rel, "refused", secret)
            return
        tracked = self.tracked.get(("note", name))
        note = self._existing_note(tracked, parsed, project)
        content_hash = text_hash(parsed.values())
        target_hash = note_hash(note) if note else ""
        if note is not None and tracked is not None and not note.current:
            self.add(
                "note",
                name,
                file.rel,
                "conflict",
                f"note #{note.id} is superseded by #{note.superseded_by}; remove this file or"
                " rename it to import it as a new note",
                content_hash,
                target_hash,
            )
            return
        action, detail, record = _decide(
            exists=note is not None,
            tracked=tracked,
            content_hash=content_hash,
            target_hash=target_hash,
            same=note is not None and _note_same(note, parsed),
            overwrite=self.overwrite,
        )
        diff = []
        if note and action in ("update", "conflict"):
            diff = diff_lines(note.body, parsed.body, f"{name} (board)", file.rel)
        self.add(
            "note",
            name,
            file.rel,
            action,
            detail,
            content_hash,
            target_hash,
            record,
            diff,
            {
                "note": parsed,
                "project": project,
                "target_id": note.id if note else 0,
                "version": note.version if note else 0,
            },
            f"{parsed.kind}/{parsed.role or ''}" if action == "add" else "",
        )

    async def _parsed_note_secret(self, parsed: ParsedNote, project: str | None) -> str | None:
        """Frontmatter can spell a value so the raw file shows nothing (JSON escapes); what the
        service would store is what counts."""
        rules = await self._secret_rules(project)
        for name in ("title", "body", "tags", "role"):
            found = find_secrets(getattr(parsed, name) or "", rules)
            if found:
                return f"secret value in the note's {name} ({found[0][1]}); never the value"
        return None

    def _existing_note(
        self, tracked: ImportItem | None, parsed: ParsedNote, project: str | None
    ) -> MemoryNote | None:
        if tracked is not None:
            return self.tracked_notes.get(tracked.target_id)
        named = [
            n
            for n in self.notes
            if n.title == parsed.title and n.project_slug == project and n.role == parsed.role
        ]
        return next((n for n in named if _note_same(n, parsed)), named[0] if named else None)

    # The whole folder

    async def build(self, root: Path) -> Plan:
        files, problems = scan(root, await self._secret_rules(None))
        self.items.extend(problems)
        self.tracked = {(t.kind, t.item): t for t in await self.service.import_items()}
        self.notes = await self.service.all_notes()
        ids = [t.target_id for t in self.tracked.values() if t.kind == "note"]
        self.tracked_notes = {n.id: n for n in await self.service.notes_by_id(ids)}
        await self._tasks(files)
        await self._rest(files)
        for slug, name in sorted(self.new_projects.items()):
            self.add("project", slug, "", "add", payload={"name": name})
        return Plan(sorted(self.items, key=lambda i: i.order), self.warnings)

    async def _rest(self, files: list[SourceFile]) -> None:
        for file in files:
            place = place_of(file.rel)
            assert place is not None
            if place.kind in ("registry", "task", "decisions"):
                continue
            if place.kind == "retro":
                await self._retro(file)
            elif place.kind == "note":
                await self._note_file(file, place.project, place.name or "")
            elif place.kind == "brief":
                slug = self._valid_project(place.project or "", file.rel)
                if slug is not None:
                    await self._need_project(slug, first_heading(normalize(file.text)))
                    await self._document(slug, "brief", file, "brief")
                    await self._decisions(slug, None, file, normalize(file.text), None)
            elif place.kind == "doc":
                await self._doc_file(file, place.project or "", place.name or "")

    async def _doc_file(self, file: SourceFile, project: str, name: str) -> None:
        slug = self._valid_project(project, file.rel)
        if slug is None:
            return
        doc = name.lower()
        if not DOC_SLUG_RE.match(doc):
            self.add(
                "file",
                file.rel,
                file.rel,
                "refused",
                f"invalid document name {name!r}: use lowercase letters, digits and dashes",
            )
            return
        await self._need_project(slug)
        await self._document(slug, doc, file, doc_kind(doc))

    async def _note_file(self, file: SourceFile, project: str | None, stem: str) -> None:
        if stem == MEMORY_INDEX:
            self.add("file", file.rel, file.rel, "skipped", "the index of the memory folder")
            return
        slug = None
        if project is not None:
            slug = self._valid_project(project, file.rel)
            if slug is None:
                return
            await self._need_project(slug)
        await self._note(file, slug, stem)

    async def _retro(self, file: SourceFile) -> None:
        if self.retro_project is None:
            self.warnings.append(f"{RETRO}: skipped; --retro-project names the project it goes to")
            self.add("file", file.rel, file.rel, "skipped", "no --retro-project")
            return
        slug = self._valid_project(self.retro_project, file.rel)
        if slug is not None:
            await self._need_project(slug)
            await self._document(slug, "retro", file, "retro")


def _in_scope(kind: str, item: str, scope: str) -> bool:
    return kind == "decision" and item.rsplit("#", 1)[0] == scope


def _decision_item(scope: str, text: str) -> str:
    return f"{scope}#{text_hash(text)[:16]}"


async def make_plan(
    service: Service,
    root: Path,
    *,
    aliases: Aliases | None = None,
    retro_project: str | None = None,
    overwrite: bool = False,
) -> Plan:
    planner = _Planner(service, aliases or Aliases(), retro_project, overwrite)
    return await planner.build(root)


# --- applying --------------------------------------------------------------------------------


async def _record(service: Service, item: Item, target_id: int, target_hash: str) -> None:
    await service.record_import_item(
        item.kind, item.item, item.source, target_id, item.content_hash, target_hash
    )


async def _apply_document(service: Service, item: Item) -> None:
    p = item.payload
    if item.action in ("add", "update"):
        saved = await service.update_document(
            p["project"],
            p["doc"],
            p["body"],
            REASON,
            p["base"],
            kind=p["kind"] if item.action == "add" else None,
            author=ACTOR,
        )
        await _record(service, item, saved.id, text_hash(saved.body))
    else:
        await _record(service, item, p["target_id"], item.target_hash)


async def _apply_task(service: Service, item: Item, tasks: dict[str, str]) -> None:
    p = item.payload
    if item.action == "add":
        values = dict(p["values"])
        title = values.pop("title")
        task = await service.create_task(
            p["project"], title, description=p.get("description"), actor=ACTOR, **values
        )
    elif item.action == "update":
        task = await service.update_task(p["key"], p["changes"], actor=ACTOR)
    else:
        tasks[item.item] = p["key"]
        await _record(service, item, p["target_id"], item.target_hash)
        return
    tasks[item.item] = task.key
    await _record(service, item, task.id, task_hash(task))


async def _apply_task_note(service: Service, item: Item, tasks: dict[str, str]) -> None:
    p = item.payload
    key = tasks.get(item.item)
    if key is None:
        raise NotFoundError("its task was not imported")
    task = await service.get_task(key)
    if item.action in ("add", "update") and task.description != p["description"]:
        task = await service.update_task(key, {"description": p["description"]}, actor=ACTOR)
    await _record(service, item, task.id, text_hash(task.description or ""))


async def _apply_decision(service: Service, item: Item, tasks: dict[str, str]) -> None:
    p = item.payload
    if item.action not in ("add", "update"):
        await _record(service, item, p["target_id"], item.target_hash)
        return
    task = None
    if p["ref"]:
        task = tasks.get(p["ref"])
        if task is None:
            raise NotFoundError("its task was not imported")
    saved = await service.add_decision(
        p["project"],
        p["text"],
        task=task,
        decided_by=ACTOR,
        source=f"message:import {item.source}",
        supersedes=p.get("supersedes"),
    )
    await _record(service, item, saved.id, text_hash(saved.text))
    if p.get("forget"):
        await service.forget_import_item("decision", p["forget"])


async def _apply_note(service: Service, item: Item) -> None:
    p = item.payload
    parsed: ParsedNote = p["note"]
    if item.action == "add":
        scope = "project" if p["project"] else ("role" if parsed.role else "global")
        result = await service.add_note(
            scope,
            parsed.kind,
            parsed.title,
            parsed.body,
            project=p["project"],
            role=parsed.role,
            tags=parsed.tags,
            links=parsed.links,
            source=f"message:import {item.source}",
            op="add",
            force_new=True,
            author=ACTOR,
        )
        assert result.note is not None
        saved = result.note
    elif item.action == "update":
        saved = await service.update_note(
            p["target_id"],
            p["version"],
            title=parsed.title,
            body=parsed.body,
            tags=parsed.tags,
            links=parsed.links or "",
            author=ACTOR,
        )
    else:
        await _record(service, item, p["target_id"], item.target_hash)
        return
    await _record(service, item, saved.id, note_hash(saved))


async def apply_plan(service: Service, plan: Plan) -> None:
    """Write the plan in order. An item that fails turns into an `error`; the others go on."""
    tasks = {i.item: key for i in plan.items if i.kind == "task" and (key := i.payload.get("key"))}
    for item in plan.items:
        if item.action not in ("add", "update") and not item.record:
            continue
        try:
            if item.kind == "project":
                await service.upsert_project(item.item, name=item.payload["name"], actor=ACTOR)
            elif item.kind == "document":
                await _apply_document(service, item)
            elif item.kind == "task":
                await _apply_task(service, item, tasks)
            elif item.kind == "task_note":
                await _apply_task_note(service, item, tasks)
            elif item.kind == "decision":
                await _apply_decision(service, item, tasks)
            elif item.kind == "note":
                await _apply_note(service, item)
        except (DomainError, sqlite3.Error, OSError) as exc:
            item.action, item.record, item.detail = "error", False, _first_line(str(exc))


# --- output ----------------------------------------------------------------------------------


def format_plan(plan: Plan) -> list[str]:
    width = max(len(a) for a in ACTIONS)
    lines = ["  " + " " * 10 + " ".join(a.rjust(width) for a in ACTIONS)]
    for kind in KINDS:
        counts = [sum(1 for i in plan.items if i.kind == kind and i.action == a) for a in ACTIONS]
        if any(counts):
            lines.append(f"  {kind:<10}" + " ".join(str(c).rjust(width) for c in counts))
    budget = DIFF_TOTAL_LINES
    shown = [i for i in plan.items if i.action != "unchanged"]
    if shown:
        lines.append("")
    for item in shown:
        source = f" ({item.source})" if item.source and item.source != item.item else ""
        detail = f": {item.detail}" if item.detail else ""
        lines.append(f"  {item.action:<9} {item.kind} {item.item}{source}{detail}")
        cut = min(DIFF_ITEM_LINES, budget)
        lines.extend(f"    {line}" for line in item.diff[:cut])
        if len(item.diff) > cut:
            lines.append(f"    ... {len(item.diff) - cut} more diff lines")
        budget -= cut
    if plan.warnings:
        lines.append("")
        lines.extend(f"  Warning: {w}" for w in plan.warnings)
    return lines


# --- opening the data ----------------------------------------------------------------------


def _service(settings: Settings, db_path: Path, attachments: Path) -> Service:
    """A service with the server's rules and policy. Its attachments folder is a private empty
    one: a start sweeps files no row names, and a server on this data may be writing one now."""
    return Service(
        db_path,
        rules=settings.rules,
        rules_table=settings.rules_table,
        rules_mode=settings.rules_mode,
        permission_defaults=settings.policy_config.get("permission_defaults"),
        guard_mode=settings.policy_config.get("guard_mode"),
        policy_file=settings.policy_path,
        attachments_dir=attachments,
    )


def _copy_database(source: Path, target: Path) -> None:
    """A consistent copy, also while a server writes (SQLite's backup API reads through the WAL)."""
    uri = f"{source.resolve().as_uri()}?mode=ro"
    with (
        contextlib.closing(sqlite3.connect(uri, uri=True)) as src,
        contextlib.closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)


@contextlib.asynccontextmanager
async def open_copy(settings: Settings) -> AsyncIterator[Service]:
    """The board on a throwaway copy of its database: a dry run or an export touches nothing, not
    even a pending migration or a search index rebuild."""
    with tempfile.TemporaryDirectory(prefix="relaybay-") as folder:
        copy = Path(folder) / "relaybay.sqlite"
        if settings.db_path.is_file():
            await asyncio.to_thread(_copy_database, settings.db_path, copy)
        service = _service(settings, copy, Path(folder) / "attachments")
        await service.start()
        try:
            yield service
        finally:
            await service.close()


@contextlib.asynccontextmanager
async def open_board(settings: Settings) -> AsyncIterator[Service]:
    with tempfile.TemporaryDirectory(prefix="relaybay-") as folder:
        service = _service(settings, settings.db_path, Path(folder) / "attachments")
        await service.start()
        try:
            yield service
        finally:
            await service.close()


def read_aliases(path: Path | None) -> Aliases:
    if path is None:
        return Aliases()
    try:
        return load_aliases(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError) as exc:
        raise ImportProblem(f"cannot use the aliases file {path}: {exc}") from exc


def resolve_root(root: Path) -> Path:
    resolved = root.expanduser().resolve()
    if not resolved.is_dir():
        raise ImportProblem(f"{root} is not a folder")
    return resolved


async def run_import(
    settings: Settings,
    root: Path,
    *,
    aliases: Aliases,
    retro_project: str | None,
    overwrite: bool,
    apply: bool,
    expect: str | None,
) -> tuple[int, list[str]]:
    """(exit code, output lines): 1 when something was refused, in conflict or failed."""
    head = f"relaybay import: {root}"
    if not apply:
        async with open_copy(settings) as service:
            plan = await make_plan(
                service, root, aliases=aliases, retro_project=retro_project, overwrite=overwrite
            )
        lines = [f"{head} (dry run)", *format_plan(plan), ""]
        lines.append(
            f"Plan hash: {plan.hash} (write exactly this plan: add --apply --expect {plan.hash}"
            " to this command)"
        )
        lines.append("Nothing written.")
        return (1 if plan.failed else 0), lines
    stale = [
        f"{head}: the plan is not the one you approved (expected {expect}): the files or the board"
        " changed. Review the new plan and apply again. Nothing written."
    ]
    if expect is not None:  # on the copy: a stale hash must not even migrate the real database
        async with open_copy(settings) as service:
            plan = await make_plan(
                service, root, aliases=aliases, retro_project=retro_project, overwrite=overwrite
            )
        if expect != plan.hash:
            return 1, stale
    async with open_board(settings) as service:
        plan = await make_plan(
            service, root, aliases=aliases, retro_project=retro_project, overwrite=overwrite
        )
        if expect is not None and expect != plan.hash:
            return 1, stale
        await apply_plan(service, plan)
    return (1 if plan.failed else 0), [f"{head} (applied)", *format_plan(plan)]
