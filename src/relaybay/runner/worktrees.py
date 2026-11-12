"""The folders of a run: a new `git worktree add` per repository by default, or the repository
itself. A worktree the run creates gets the source repository's untracked Claude Code files
(`CLAUDE.md`, `CLAUDE.local.md`, `.claude/`, `.mcp.json`, or the list of its `.worktreeinclude`),
copied as local files and never added to git.

The only git writes here are `git worktree add` and, when a Start fails, `git worktree remove` of
the worktrees that Start just created. Never a commit, a push or any other git write. No database:
the service writes the rows.
"""

import contextlib
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any

from relaybay.domain.errors import ValidationError
from relaybay.machine_setup import run_quiet

logger = logging.getLogger("relaybay")
COPY_DEFAULT = ("CLAUDE.md", "CLAUDE.local.md", ".claude", ".mcp.json")
INCLUDE_FILE = ".worktreeinclude"
COPY_MAX_FILES = 2000
COPY_MAX_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 300
NAME_TRIES = 50  # branch and folder names tried after the first one is taken
# Set around a git hook or by a caller's own git: they would point every command at another
# repository than the one named with -C (git's own `local_repo_env` list).
GIT_LOCATION_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True, slots=True)
class WorktreeSpec:
    repo_id: int | None
    repo_name: str
    source: str
    path: str
    create: bool  # False: the repository itself, used in place
    branch: str | None = None
    copied: tuple[str, ...] = field(default_factory=tuple)
    skipped: tuple[str, ...] = field(default_factory=tuple)


def path_key(path: str) -> str:
    """A path as compared: case and separators folded the way this OS folds them."""
    return os.path.normcase(os.path.normpath(path))


def run_folder(root: Path, task_key: str, run_id: int) -> Path:
    return root / f"{task_key}-r{run_id}"


def branch_names(task_key: str, run_id: int) -> list[str]:
    """The branches a new worktree may take, in order: the card's, the run's, then numbered
    ones. A failed Start keeps its branch and the next Start reuses the run id, so a taken name
    moves on to the next one instead of refusing."""
    first = [f"relaybay/{task_key}", f"relaybay/{task_key}-r{run_id}"]
    return first + [f"relaybay/{task_key}-r{run_id}-{n}" for n in range(2, NAME_TRIES + 2)]


def free_folder(folder: Path) -> Path:
    """The run's folder for new worktrees, or a numbered one when it is there already (a
    worktree that could not be removed after a failed Start, a folder held by another tool)."""
    for n in range(1, NAME_TRIES + 2):
        candidate = folder if n == 1 else folder.with_name(f"{folder.name}-{n}")
        if not os.path.lexists(candidate):
            return candidate
    raise ValidationError(f"{folder} and {NAME_TRIES} numbered folders next to it exist already.")


def plan_worktrees(
    task_key: str,
    run_id: int,
    choices: Sequence[tuple[int | None, str, str, bool]],
    root: Path,
) -> list[WorktreeSpec]:
    """`choices`: (repository id, name, path, new worktree?) per repository. A new worktree goes
    to `<root>/<task-key>-r<run>/<name>`; a repository in place keeps its own path."""
    folder = run_folder(root, task_key, run_id)
    used: set[str] = set()
    specs = []
    for repo_id, name, source, create in choices:
        if not create:
            specs.append(WorktreeSpec(repo_id, name, source, source, False))
            continue
        base = name if name and name not in (".", "..") else "repo"
        leaf, n = base, 1
        while leaf.lower() in used:
            n += 1
            leaf = f"{base}-{n}"
        used.add(leaf.lower())
        specs.append(WorktreeSpec(repo_id, name, source, str(folder / leaf), True))
    return specs


def _git_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCATION_VARIABLES}


def _git(
    git: str,
    source: str,
    args: Sequence[str],
    run: Runner,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    try:
        return run(
            [git, "-C", source, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError(
            f"git {args[0]} in {source} took longer than {timeout:.0f} s."
        ) from exc


def _first_line(done: subprocess.CompletedProcess) -> str:
    for text in (done.stderr, done.stdout):
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()[:300]
    return f"git exited with code {done.returncode}"


def check_source(spec: WorktreeSpec, git: str, run: Runner = run_quiet) -> None:
    """The source must be the top folder of a git repository: a worktree is of the whole one."""
    done = _git(git, spec.source, ["rev-parse", "--show-toplevel"], run)
    if done.returncode != 0:
        raise ValidationError(
            f"{spec.repo_name} ({spec.source}) is not a git repository, so no worktree can be"
            f" made of it: choose 'the repository itself' for it ({_first_line(done)})."
        )
    top = done.stdout.strip()
    if path_key(os.path.realpath(top)) != path_key(os.path.realpath(spec.source)):
        raise ValidationError(
            f"{spec.repo_name} ({spec.source}) is a folder inside the git repository {top}:"
            " set the repository's path to its top folder on Stands & access."
        )


def free_branch(spec: WorktreeSpec, names: Sequence[str], git: str, run: Runner) -> str:
    for name in names:
        args = ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"]
        done = _git(git, spec.source, args, run)
        if done.returncode == 1:
            return name
        if done.returncode != 0:
            raise ValidationError(f"{spec.repo_name}: git show-ref failed: {_first_line(done)}")
    raise ValidationError(
        f"{spec.repo_name}: the branches {names[0]} to {names[-1]} exist already; delete the"
        " ones of earlier runs of this card."
    )


def _safe_relative(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts and ":" not in name


def include_pathspecs(text: str) -> list[str]:
    """Pathspecs that cover every file a `.worktreeinclude` pattern can match, so git walks only
    there when a pattern is anchored (`config/`, `/app.env`); an unanchored one (`*.env`) needs
    the whole tree. The patterns themselves still decide, through --exclude-from."""
    specs: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "!")):
            continue
        body = line.rstrip("/")
        anchored = "/" in body
        body = body.lstrip("/")
        if not body:
            continue
        stem = body if anchored else f"**/{body}"
        specs += [f":(glob){stem}", f":(glob){stem}/**"]
    return specs


def _names(done: subprocess.CompletedProcess, source: str) -> list[str]:
    if done.returncode != 0:
        raise ValidationError(
            f"listing the untracked files of {source} failed: {_first_line(done)}"
        )
    return [n for n in done.stdout.split("\0") if n]


def _ignored(name: str, ignored: set[str]) -> bool:
    """`name` is gitignored: listed itself, or inside an ignored folder git listed as `dir/`."""
    if name in ignored:
        return True
    parts = PurePosixPath(name).parts
    return any("/".join(parts[:i]) + "/" in ignored for i in range(1, len(parts)))


def copy_list(
    source: str, git: str, run: Runner = run_quiet, timeout: float = GIT_TIMEOUT_SECONDS
) -> list[str]:
    """The untracked files to copy into a new worktree of `source`, as git names them (relative,
    with "/"). Default: the untracked Claude Code names. With a `.worktreeinclude`, as Claude
    Code does: the untracked files its patterns (gitignore syntax) match that are gitignored
    too. git does the matching; nothing tracked is listed."""
    include = Path(source) / INCLUDE_FILE
    if include.is_file():
        specs = include_pathspecs(include.read_text(encoding="utf-8", errors="replace"))
        if not specs:
            return []
        matched = _names(
            _git(
                git,
                source,
                [
                    "ls-files",
                    "-z",
                    "--others",
                    "--ignored",
                    f"--exclude-from={include}",
                    "--",
                    *specs,
                ],
                run,
                timeout,
            ),
            source,
        )
        # --directory: an ignored folder (node_modules/) is one entry, not walked.
        ignored = set(
            _names(
                _git(
                    git,
                    source,
                    [
                        "ls-files",
                        "-z",
                        "--others",
                        "--ignored",
                        "--exclude-standard",
                        "--directory",
                    ],
                    run,
                    timeout,
                ),
                source,
            )
        )
        names = [n for n in matched if _ignored(n, ignored)]
    else:
        names = _names(
            _git(git, source, ["ls-files", "-z", "--others", "--", *COPY_DEFAULT], run, timeout),
            source,
        )
    # An entry ending in "/" is a nested repository git does not look into: never copied.
    return sorted(n for n in names if not n.endswith("/"))


def _inside_symlink(root: Path, name: str) -> bool:
    current = root
    for part in PurePosixPath(name).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def copy_plan(source: str, names: Sequence[str]) -> tuple[list[str], list[str]]:
    """(to copy, skipped) of `names`, checked without writing anything. Skipped: a symlink (the
    file or a folder on its way), a name leaving the repository, a file that is gone. Too many
    files or bytes refuse, naming `.worktreeinclude`."""
    root = Path(source)
    real_root = os.path.realpath(root)
    plan: list[str] = []
    skipped: list[str] = []
    total = 0
    for name in names:
        if not _safe_relative(name) or _inside_symlink(root, name):
            skipped.append(name)
            continue
        src = root / name
        real = os.path.realpath(src)
        if os.path.commonpath([real_root, real]) != real_root or not src.is_file():
            skipped.append(name)
            continue
        total += src.stat().st_size
        plan.append(name)
        if len(plan) > COPY_MAX_FILES or total > COPY_MAX_BYTES:
            # Stop at the first file past a limit: a listed node_modules is not walked to its end.
            raise ValidationError(
                f"{source} has more than {COPY_MAX_FILES} untracked files or"
                f" {COPY_MAX_BYTES // 2**20} MB to copy into a new worktree: list what a worktree"
                f" needs in a {INCLUDE_FILE} file of the repository."
            )
    return plan, skipped


def copy_into(source: str, target: str, names: Sequence[str]) -> tuple[list[str], list[str]]:
    """Copy `names` from `source` into `target` after `copy_plan`; returns (copied, skipped).
    A file the worktree has already is skipped too, never overwritten."""
    plan, skipped = copy_plan(source, names)
    copied = []
    for name in plan:
        src = Path(source) / name
        dest = Path(target) / name
        if os.path.lexists(dest):
            skipped.append(name)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest, follow_symlinks=False)
        copied.append(name)
    return copied, sorted(skipped)


def summary(names: Sequence[str]) -> str:
    """One line: top-level names, a folder with its file count. Empty: "nothing"."""
    counts: dict[str, int] = {}
    for name in names:
        parts = PurePosixPath(name).parts
        top = parts[0] + ("/" if len(parts) > 1 else "")
        counts[top] = counts.get(top, 0) + 1
    if not counts:
        return "nothing"
    shown = []
    for top, count in counts.items():
        if top.endswith("/"):
            shown.append(f"{top} ({count} file{'s' if count != 1 else ''})")
        else:
            shown.append(top)
    return ", ".join(shown)


def add_all(
    specs: Sequence[WorktreeSpec],
    git: str | None,
    task_key: str,
    run_id: int,
    run: Runner = run_quiet,
    made: list[WorktreeSpec] | None = None,
) -> list[WorktreeSpec]:
    """Make the run's folders. First every check, with no write: each repository in place
    exists; each source is a repository's top folder; what to copy, within the limits; a free
    branch; a free run folder (numbered when the planned one is there). Then each `git worktree
    add` and its copy, in order. When one fails, the worktrees made so far are removed and the
    error names the repository with git's first line.

    `made`: the caller's list that holds each worktree the moment it exists, so a caller that
    gave up waiting (a cancelled request) can still remove them."""
    planned: list[tuple[WorktreeSpec, list[str], list[str]]] = []
    for spec in specs:
        if not spec.create:
            if not Path(spec.source).is_dir():
                raise ValidationError(
                    f"{spec.source} is not a folder on this machine ({spec.repo_name})."
                )
            planned.append((spec, [], []))
            continue
        if git is None:
            raise ValidationError(
                "git is not on PATH, so no worktree can be made: install git or choose"
                " 'the repository itself'."
            )
        check_source(spec, git, run)
        names = copy_list(spec.source, git, run)
        to_copy, skipped = copy_plan(spec.source, names)
        branch = free_branch(spec, branch_names(task_key, run_id), git, run)
        planned.append((replace(spec, branch=branch), to_copy, skipped))
    creates = [spec for spec, _, _ in planned if spec.create]
    if creates:
        folder = free_folder(Path(creates[0].path).parent)
        planned = [
            (replace(spec, path=str(folder / Path(spec.path).name)) if spec.create else spec, c, k)
            for spec, c, k in planned
        ]
    done = made if made is not None else []
    try:
        for spec, to_copy, skipped in planned:
            if not spec.create:
                done.append(spec)
                continue
            assert git is not None and spec.branch is not None
            Path(spec.path).parent.mkdir(parents=True, exist_ok=True)
            added = _git(git, spec.source, ["worktree", "add", "-b", spec.branch, spec.path], run)
            if added.returncode != 0:
                raise ValidationError(
                    f"git worktree add for {spec.repo_name} failed: {_first_line(added)}"
                )
            done.append(spec)
            copied, more = copy_into(spec.source, spec.path, to_copy)
            done[-1] = replace(spec, copied=tuple(copied), skipped=tuple(sorted(skipped + more)))
    except BaseException:
        remove_created(done, git, run)
        done.clear()
        raise
    return list(done)


def remove_created(specs: Sequence[WorktreeSpec], git: str | None, run: Runner = run_quiet) -> None:
    """Undo a Start that failed: remove the worktrees it created (with the files copied in, hence
    --force; nothing else is in them yet). The branch stays: deleting one is not ours to do."""
    for spec in reversed(specs):
        if not spec.create or git is None:
            continue
        gone = _git(git, spec.source, ["worktree", "remove", "--force", spec.path], run)
        if gone.returncode != 0:
            logger.warning(
                "Removing the worktree %s after a failed Start failed: %s",
                spec.path,
                _first_line(gone),
            )
        with contextlib.suppress(OSError):
            Path(spec.path).parent.rmdir()  # the run's folder, once empty


REASON_ENTRIES = 5  # changed files named in a kept worktree's reason


def _status_names(text: str) -> list[str]:
    """The paths of `git status --porcelain=v1 -z`: `XY path`, a rename followed by its source."""
    names, parts, i = [], text.split("\0"), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        names.append(entry[3:])
        if entry[0] in "RC":
            i += 1  # the rename's source
    return names


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.is_file() and b.is_file() and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def work_left(
    path: str,
    branch: str | None,
    source: str,
    copied: Sequence[str],
    git: str,
    run: Runner = run_quiet,
) -> str | None:
    """Why a worktree the run created must not be removed without the owner: changed or
    untracked files (the files relaybay copied in count only when they differ from the source
    now), commits on its HEAD that are on no remote and on no other local branch (the run's
    branch stays when the worktree goes), or a git that cannot tell. None: nothing would be lost."""
    done = _git(
        git,
        path,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"],
        run,
    )
    if done.returncode != 0:
        return f"git status failed: {_first_line(done)}"
    ours = set(copied)
    changed = []
    for name in _status_names(done.stdout or ""):
        if name in ours and _same_file(Path(path) / name, Path(source) / name):
            continue
        changed.append(name)
    head = _git(git, path, ["rev-parse", "--abbrev-ref", "HEAD"], run)
    current = (head.stdout or "").strip() if head.returncode == 0 else ""
    mine = [n for n in {branch, current} if n and n != "HEAD"]
    args = ["rev-list", "--count", "HEAD", "--not", "--remotes"]
    args += [f"--exclude={name}" for name in mine] + ["--branches"]
    ahead = _git(git, path, args, run)
    if ahead.returncode != 0:
        return f"git rev-list failed: {_first_line(ahead)}"
    commits = int((ahead.stdout or "0").strip() or "0")
    parts = []
    if changed:
        shown = ", ".join(changed[:REASON_ENTRIES])
        more = f" and {len(changed) - REASON_ENTRIES} more" if len(changed) > REASON_ENTRIES else ""
        parts.append(f"uncommitted changes ({shown}{more})")
    if commits:
        parts.append(f"{commits} commit{'s' if commits != 1 else ''} not on any remote")
    return "; ".join(parts) or None


def _drop_copied(path: str, copied: Sequence[str]) -> None:
    """Delete the files relaybay copied into the worktree (only those, never through a link),
    then the folders they leave empty, so that git sees a clean worktree."""
    root = Path(path)
    for name in copied:
        if not _safe_relative(name) or _inside_symlink(root, PurePosixPath(name).parent.as_posix()):
            continue
        target = root / name
        if target.is_symlink() or target.is_file():
            target.unlink()
    folders = {
        PurePosixPath(n).parents[i] for n in copied for i in range(len(PurePosixPath(n).parts) - 1)
    }
    for folder in sorted(folders, key=lambda f: len(f.parts), reverse=True):
        with contextlib.suppress(OSError):
            (root / folder).rmdir()


def remove_worktree(
    path: str,
    source: str,
    copied: Sequence[str],
    git: str,
    *,
    force: bool,
    run: Runner = run_quiet,
) -> str | None:
    """`git worktree remove` of a worktree the run created; None when it is gone, else git's
    first line. Without `force` git itself refuses a worktree with changes, after the copied
    files are deleted; `force` only on the owner's answer. The branch stays. The run's folder
    goes too once empty."""
    if not force:
        _drop_copied(path, copied)
    args = ["worktree", "remove", *(["--force"] if force else []), path]
    done = _git(git, source, args, run)
    if done.returncode != 0:
        return _first_line(done)
    with contextlib.suppress(OSError):
        Path(path).parent.rmdir()
    return None


def find_git(which: Callable[..., Any] = shutil.which) -> str | None:
    return which("git")


BRANCH_TIMEOUT_SECONDS = 5.0


def current_branch(git: str, folder: str, run: Runner = run_quiet) -> str | None:
    """The branch checked out in `folder` (a read); None when it is not a repository, the HEAD
    is detached or git does not answer in time."""
    try:
        done = _git(git, folder, ["rev-parse", "--abbrev-ref", "HEAD"], run, BRANCH_TIMEOUT_SECONDS)
    except (ValidationError, OSError):
        return None
    name = (done.stdout or "").strip()
    return name if done.returncode == 0 and name and name != "HEAD" else None
