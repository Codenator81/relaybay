"""Reads of git for the approval check: which branch a folder has checked out. Never a write."""

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from relaybay.domain.errors import ValidationError
from relaybay.machine_setup import run_quiet

GIT_TIMEOUT_SECONDS = 300
BRANCH_TIMEOUT_SECONDS = 5.0
# Variables that point git at another repository than the folder it is asked about.
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


class GitTooSlow(Exception):
    """git did not answer within the time it was given."""


def path_key(path: str) -> str:
    """A path as compared: case and separators folded the way this OS folds them."""
    return os.path.normcase(os.path.normpath(path))


def find_git(which: Callable[..., Any] = shutil.which) -> str | None:
    return which("git")


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


def branch_and_head(
    git: str, folder: str, timeout: float = BRANCH_TIMEOUT_SECONDS, run: Runner = run_quiet
) -> tuple[str | None, Path | None]:
    """The branch checked out in `folder` (a read) and the HEAD file of its working tree, which
    changes whenever the branch does. (None, None) when it is not a repository; the branch is
    None when the HEAD is detached. GitTooSlow when git does not answer within `timeout`."""
    args = ["rev-parse", "--absolute-git-dir", "--abbrev-ref", "HEAD"]
    try:
        done = _git(git, folder, args, run, timeout)
    except ValidationError as exc:
        if isinstance(exc.__cause__, subprocess.TimeoutExpired):
            raise GitTooSlow(str(exc)) from exc
        return None, None
    except OSError:
        return None, None
    lines = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or len(lines) != 2:
        return None, None
    git_dir, name = (line.strip() for line in lines)
    return (name if name and name != "HEAD" else None), Path(git_dir) / "HEAD"
