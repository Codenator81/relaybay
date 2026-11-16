"""The coordinator's folder on disk: never inside a git repository (probe 15 of design 0023: the
repository's CLAUDE.md and its auto memory would load with it), made on Start, with the starter
CLAUDE.md written once when the folder has none."""

from importlib import resources
from pathlib import Path

import platformdirs

from relaybay.domain.coordinator import STARTER_NAME

FOLDER_NAME = "coordinator"


def git_repository_of(folder: Path) -> Path | None:
    """The folder of the git repository `folder` lies in (itself or a parent holds `.git`, a
    folder or a worktree's file), else None. The folder need not exist yet."""
    path = folder.expanduser().absolute()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def user_folder() -> Path:
    """relaybay's per-user data folder's coordinator folder: the fallback default."""
    return Path(platformdirs.user_data_dir("relaybay", appauthor=False)) / FOLDER_NAME


def default_folder(home: Path) -> tuple[Path, str | None]:
    """`<home>/coordinator`, unless that lies inside a git repository: then the per-user data
    folder, with the reason the default moved."""
    first = home / FOLDER_NAME
    repository = git_repository_of(first)
    if repository is None:
        return first, None
    return user_folder(), (
        f"{first} lies inside the git repository {repository}, so the default is the per-user"
        " data folder."
    )


def inside_repository_reason(folder: Path) -> str | None:
    """Why Start refuses the folder, or None."""
    repository = git_repository_of(folder)
    if repository is None:
        return None
    return (
        f"The coordinator's folder {folder} lies inside the git repository {repository}: it would"
        " load that repository's CLAUDE.md and auto memory. Pick a folder outside any repository"
        " in Settings -> Agents."
    )


def starter_text() -> str:
    return resources.files("relaybay.runner").joinpath("coordinator_claude.md").read_text("utf-8")


def prepare(folder: Path) -> bool:
    """Make the folder and write the starter CLAUDE.md when it has none; never overwrite it.
    True when the starter was written."""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / STARTER_NAME
    try:
        with target.open("x", encoding="utf-8", newline="\n") as out:
            out.write(starter_text())
    except FileExistsError:
        return False
    return True
