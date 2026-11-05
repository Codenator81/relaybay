"""`relaybay export`: write documents, decisions, notes and the task registry back to the markdown
layout for git history, backup and grep. Deterministic, so diffs stay small; reads a copy of the
database, so it changes nothing on the board. A manifest protects files changed by hand."""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from relaybay.config import Settings
from relaybay.domain.environments import find_secrets
from relaybay.domain.markdown_layout import (
    MANIFEST,
    REGISTRY,
    TASK_FILE,
    Aliases,
    normalize,
    note_file_stem,
    parse_note,
    place_of,
    registry_meaning,
    render_decisions,
    render_note,
    render_registry,
    slugify,
    task_link,
)
from relaybay.domain.memory import MemoryNote, diff_lines
from relaybay.domain.models import ImportItem, Task
from relaybay.markdown_import import DIFF_ITEM_LINES, DIFF_TOTAL_LINES, open_copy
from relaybay.service import Service

MANIFEST_VERSION = 1
ACTIONS = ("add", "update", "remove", "unchanged", "conflict", "refused", "error")
FAILED = ("conflict", "refused", "error")
HAND_CHANGED = "changed by hand since the last export; --force overwrites it"
NOT_OURS = "this file was not written by export; --force overwrites it"
TASK_DOC_PREFIX = "task-"


class ExportProblem(Exception):
    """The run cannot go on: no board to export, or the manifest is unreadable."""


@dataclass(slots=True)
class Wanted:
    """A file export wants in place: its text, and for notes the note it says."""

    text: str
    note: MemoryNote | None = None


@dataclass(slots=True)
class FileAction:
    rel: str
    action: str
    detail: str = ""
    text: str | None = None
    diff: list[str] = field(default_factory=list)
    keep_hash: str | None = None  # the file stays as it is: this hash goes into the manifest
    written: bool = True  # in the manifest: export wrote this file (else it only kept it)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- what the layout should hold ---------------------------------------------------------------


def _doc_path(project: str, doc: str, tracked: ImportItem | None, linked: set[str]) -> str:
    """The file a document came from, else its place in the layout. A task document no task links
    to goes under docs/: under tasks/ the next import would create a task for it."""
    if tracked is not None and place_of(tracked.source_path) is not None:
        return tracked.source_path
    if doc == "brief":
        return f"projects/{project}.md"
    if doc.startswith(TASK_DOC_PREFIX) and doc in linked:
        return f"projects/{project}/tasks/{doc[len(TASK_DOC_PREFIX) :]}/{TASK_FILE}"
    return f"projects/{project}/docs/{doc}.md"


def _note_path(note: MemoryNote, tracked: ImportItem | None) -> str:
    if tracked is not None and place_of(tracked.source_path) is not None:
        return tracked.source_path
    folder = f"projects/{note.project_slug}/memory" if note.project_slug else "memory"
    return f"{folder}/{note_file_stem(note.title, note.id)}.md"


def _claim(
    wanted: dict[str, Wanted],
    places: list[str],
    want: Wanted,
    label: str,
    problems: list[FileAction],
) -> None:
    """The first free path of `places`; names differing only in case are one file on some systems.
    Nothing is overwritten silently: with no place left, the item is an error."""
    taken = {path.casefold() for path in wanted}
    free = next((path for path in places if path.casefold() not in taken), None)
    if free is None:
        detail = f"{label} has no free path here, so it was not exported"
        problems.append(FileAction(places[0], "error", detail))
    else:
        wanted[free] = want


async def gather(service: Service) -> tuple[dict[str, Wanted], list[FileAction]]:
    """Every file by path, and what found no path. Same data, same bytes: sorted, and no time but
    the stored ones."""
    problems: list[FileAction] = []
    tracked = {(t.kind, t.target_id): t for t in await service.import_items()}
    wanted: dict[str, Wanted] = {}
    rows: list[tuple[str, str, Task]] = []
    projects = sorted((p.project for p in await service.list_projects()), key=lambda p: p.slug)
    for project in projects:
        tasks = await service.list_tasks(project=project.slug, include_done=True)
        tasks.sort(key=lambda t: t.id)
        linked = set()
        for task in tasks:
            mark = tracked.get(("task", task.id))
            ref = mark.item.split("/", 1)[1] if mark else task.key
            rows.append((project.slug, ref, task))
            prefix = task_link(project.slug, "")
            if task.doc_path and task.doc_path.startswith(prefix):
                linked.add(f"{TASK_DOC_PREFIX}{task.doc_path[len(prefix) :]}")
        _, documents = await service.list_documents(project.slug)
        for document in sorted(documents, key=lambda d: d.slug):
            _, body = await service.get_document(project.slug, document.slug)
            if document.source_url and not body.strip():
                continue  # lives in a repository with no text here: nothing to write
            mark = tracked.get(("document", document.id))
            path = _doc_path(project.slug, document.slug, mark, linked)
            places = [path, f"projects/{project.slug}/docs/{document.slug}.md"]
            label = f"document {project.slug}/{document.slug}"
            _claim(wanted, places, Wanted(body + "\n"), label, problems)
        decisions = await service.all_decisions(project.id)
        if decisions:
            path = f"projects/{project.slug}/decisions.md"
            wanted[path] = Wanted(render_decisions(project.slug, decisions))
    if rows:
        wanted[REGISTRY] = Wanted(render_registry(rows))
    for note in await service.all_notes():
        path = _note_path(note, tracked.get(("note", note.id)))
        text = render_note(note.title, note.kind, note.body, tags=note.tags, role=note.role)
        places = [path, f"{path[: -len('.md')]}-{note.id}.md"]
        _claim(wanted, places, Wanted(text, note), f"note #{note.id}", problems)
    return wanted, problems


# --- comparing with the folder -----------------------------------------------------------------


def read_manifest(root: Path) -> dict[str, dict[str, Any]]:
    path = root / MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data["files"]
        if not isinstance(files, dict):
            raise TypeError("files is not an object")
        return {
            str(k): {"sha256": v["sha256"], "written": bool(v["written"])} for k, v in files.items()
        }
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise ExportProblem(
            f"{path} cannot be read ({exc}); fix it or remove it, then every file there counts as"
            " not written by export"
        ) from exc


def _write_manifest(root: Path, files: dict[str, dict[str, Any]]) -> None:
    data = {"version": MANIFEST_VERSION, "files": dict(sorted(files.items()))}
    _write_text(root / MANIFEST, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.relaybay.tmp")
    try:
        tmp.write_bytes(text.encode("utf-8"))
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _leaves_root(root: Path, rel: str) -> bool:
    """A link anywhere on the way would put the write outside the root."""
    path = root.joinpath(*rel.split("/"))
    return path.is_symlink() or path.is_junction() or not path.parent.resolve().is_relative_to(root)


def _task_folders(root: Path, wanted: dict[str, Wanted]) -> set[str]:
    """`<project>/<key>` of every task folder after this export, as the import looks for them."""
    paths = [p.relative_to(root).as_posix() for p in root.glob(f"projects/*/tasks/*/{TASK_FILE}")]
    places = [place_of(path) for path in [*paths, *wanted]]
    return {
        f"{place.project.lower()}/{slugify(place.name)}"
        for place in places
        if place is not None and place.kind == "task" and place.project and place.name
    }


def _says_the_same(
    rel: str, existing: str, wanted: Wanted, aliases: Aliases, folders: set[str]
) -> bool:
    """A file that says the same in its own words is kept, so exporting into the folder a setup was
    imported from leaves hand-written files alone until the board really differs."""
    if normalize(existing) == normalize(wanted.text):
        return True
    if rel == REGISTRY:
        said = registry_meaning(existing, aliases, folders)
        return said == registry_meaning(wanted.text, aliases)
    if wanted.note is not None:
        stem = rel.rsplit("/", 1)[-1][: -len(".md")]
        said = parse_note(existing, stem)
        note = wanted.note
        return (said.title, said.body, said.kind, said.role, said.tags) == (
            note.title,
            note.body,
            note.kind,
            note.role,
            note.tags,
        )
    return False


def plan_files(
    root: Path,
    wanted: dict[str, Wanted],
    manifest: dict[str, dict[str, Any]],
    *,
    aliases: Aliases,
    force: bool,
) -> list[FileAction]:
    actions: list[FileAction] = []
    folders = _task_folders(root, wanted)
    for rel, want in sorted(wanted.items()):
        actions.append(_plan_file(root, rel, want, manifest.get(rel), aliases, force, folders))
    for rel, entry in sorted(manifest.items()):
        if rel in wanted or not entry["written"]:
            continue
        actions.append(_plan_removal(root, rel, entry, force))
    return actions


def _diff(rel: str, existing: str, text: str) -> list[str]:
    """What a file on disk would lose. It can hold anything, so a secret there is not shown."""
    found = find_secrets(existing)
    if found:
        places = ", ".join(f"line {line} ({name})" for line, name in found)
        return [f"diff hidden: {places}"]
    return diff_lines(existing.replace("\r\n", "\n"), text, rel, f"{rel} (board)")


def _plan_file(
    root: Path,
    rel: str,
    want: Wanted,
    entry: dict[str, Any] | None,
    aliases: Aliases,
    force: bool,
    folders: set[str],
) -> FileAction:
    if _leaves_root(root, rel):
        return FileAction(rel, "refused", "a link on the way leads out of the root")
    path = root.joinpath(*rel.split("/"))
    if not path.is_file():
        return FileAction(rel, "add", text=want.text)
    data = path.read_bytes()
    current_hash = file_hash(data)
    written = bool(entry and entry["written"])
    if data == want.text.encode("utf-8"):
        return FileAction(rel, "unchanged", keep_hash=current_hash, written=written)
    try:
        existing = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        existing = None
    if existing is not None and _says_the_same(rel, existing, want, aliases, folders):
        # A hand edit that says the same must not become the new base: the next change from the
        # board would overwrite it. Line endings and outer whitespace lose nothing.
        same_text = normalize(existing) == normalize(want.text)
        base = current_hash if entry is None or same_text else entry["sha256"]
        return FileAction(rel, "unchanged", keep_hash=base, written=written)
    diff = _diff(rel, existing, want.text) if existing is not None else []
    if force:
        return FileAction(rel, "update", "--force", want.text, diff)
    if not written:
        return FileAction(rel, "conflict", NOT_OURS, diff=diff)
    assert entry is not None
    if entry["sha256"] == current_hash:
        return FileAction(rel, "update", text=want.text, diff=diff)
    return FileAction(rel, "conflict", HAND_CHANGED, diff=diff)


def _plan_removal(root: Path, rel: str, entry: dict[str, Any], force: bool) -> FileAction:
    """Only what the layout can hold: the manifest is a file anyone can edit."""
    if place_of(rel) is None:
        return FileAction(rel, "refused", "not a path of the layout; the manifest was edited?")
    if _leaves_root(root, rel):
        return FileAction(rel, "refused", "a link on the way leads out of the root")
    path = root.joinpath(*rel.split("/"))
    if not path.is_file():
        return FileAction(rel, "remove", "already gone")
    if file_hash(path.read_bytes()) == entry["sha256"] or force:
        return FileAction(rel, "remove", "no longer on the board")
    return FileAction(
        rel,
        "conflict",
        "no longer on the board, but changed by hand since the last export; --force removes it",
    )


def apply_files(root: Path, actions: list[FileAction], manifest: dict[str, dict[str, Any]]) -> None:
    """Write and remove; a file that fails turns into an `error`, the others go on. The manifest
    then says what is really there."""
    root.mkdir(parents=True, exist_ok=True)
    files = dict(manifest)
    for action in actions:
        path = root.joinpath(*action.rel.split("/"))
        try:
            if action.action in ("add", "update"):
                assert action.text is not None
                _write_text(path, action.text)
                files[action.rel] = {
                    "sha256": file_hash(action.text.encode("utf-8")),
                    "written": True,
                }
            elif action.action == "remove":
                path.unlink(missing_ok=True)
                files.pop(action.rel, None)
            elif action.action == "unchanged" and action.keep_hash:
                files[action.rel] = {"sha256": action.keep_hash, "written": action.written}
        except OSError as exc:
            action.action, action.detail = "error", f"cannot write: {exc.strerror}"
    _write_manifest(root, files)


def format_actions(actions: list[FileAction]) -> list[str]:
    counts = "  " + ", ".join(
        f"{sum(1 for a in actions if a.action == name)} {name}" for name in ACTIONS
    )
    lines = [counts]
    budget = DIFF_TOTAL_LINES
    for action in actions:
        if action.action == "unchanged":
            continue
        detail = f": {action.detail}" if action.detail else ""
        lines.append(f"  {action.action:<9} {action.rel}{detail}")
        cut = min(DIFF_ITEM_LINES, budget)
        lines.extend(f"    {line}" for line in action.diff[:cut])
        if len(action.diff) > cut:
            lines.append(f"    ... {len(action.diff) - cut} more diff lines")
        budget -= cut
    return lines


async def run_export(
    settings: Settings, root: Path, *, aliases: Aliases, apply: bool, force: bool
) -> tuple[int, list[str]]:
    """(exit code, output lines): 1 when a file was refused, in conflict or failed."""
    if not settings.db_path.is_file():
        raise ExportProblem(
            f"there is no board at {settings.db_path}; nothing to export (wrong --home?)"
        )
    async with open_copy(settings) as service:
        wanted, problems = await gather(service)
        empty_board = not await service.list_projects()
    manifest = read_manifest(root)
    if apply and empty_board and manifest:
        raise ExportProblem(
            f"the board at {settings.db_path} has no projects, but {root} holds an earlier export;"
            " nothing was written or removed (wrong --home?)"
        )
    actions = [*plan_files(root, wanted, manifest, aliases=aliases, force=force), *problems]
    head = f"relaybay export: {root}"
    if apply:
        apply_files(root, actions, manifest)
    code = 1 if any(a.action in FAILED for a in actions) else 0
    if apply:
        return code, [f"{head} (written)", *format_actions(actions)]
    return code, [f"{head} (dry run)", *format_actions(actions), "", "Nothing written."]
