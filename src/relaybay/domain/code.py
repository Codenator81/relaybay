"""A project's repositories, the ones a task touches and its pull requests: the data, the checks
of their values and the "Code" row the board and `task_get` show."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from urllib.parse import quote, urlsplit

REPO_KINDS = ("app", "web", "api", "lib", "other")
LINK_HOWS = ("auto", "agent", "owner")
REPO_NAME_MAX = 60
REPO_PATH_MAX = 500
REPOS_MAX = 50
PR_TITLE_MAX = 200
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# The number at the end of a pull or merge request link (GitHub, GitLab, Bitbucket, Gitea).
_PR_NUMBER_RE = re.compile(r"/(?:pull|pulls|merge_requests|pull-requests)/(\d+)(?:[/?#]|$)")


@dataclass(slots=True)
class ProjectRepo:
    id: int
    project_id: int
    name: str
    path: str
    kind: str | None
    position: int
    created_at: str
    updated_at: str


@dataclass(slots=True)
class TaskRepo:
    task_id: int
    repo_id: int
    added_by: str
    how: str
    added_at: str


@dataclass(slots=True)
class TaskPr:
    id: int
    task_id: int
    repo_id: int | None
    url: str
    number: int | None
    title: str | None
    added_by: str
    added_at: str

    @property
    def label(self) -> str:
        """`#123` when the number is known, else the title, else the link."""
        if self.number is not None:
            return f"#{self.number}"
        return self.title or self.url


@dataclass(slots=True)
class CodeRepo:
    """One repository on a task's Code row, with the task's pull requests in it."""

    repo: ProjectRepo
    how: str
    prs: list[TaskPr] = field(default_factory=list)


@dataclass(slots=True)
class TaskCode:
    """The Code row of one task: the repositories it touches (in the project's order), its pull
    requests per repository and those tied to none, and all of the project's repositories for
    the "Repos: all" line of a task that names none."""

    repos: list[CodeRepo]
    loose_prs: list[TaskPr]
    project_repos: list[ProjectRepo]

    @property
    def pr_count(self) -> int:
        return len(self.loose_prs) + sum(len(r.prs) for r in self.repos)

    @property
    def shown(self) -> bool:
        return bool(self.repos or self.loose_prs or self.project_repos)

    @property
    def summary(self) -> str:
        """The compact card's line, e.g. `Code: 2 repos, 3 PRs`."""
        if self.repos:
            n = len(self.repos)
            parts = [f"{n} repo{'s' if n != 1 else ''}"]
        else:
            parts = ["all repos"]
        if self.pr_count:
            parts.append(f"{self.pr_count} PR{'s' if self.pr_count != 1 else ''}")
        return "Code: " + ", ".join(parts)


def task_code(
    task_id: int,
    project_repos: list[ProjectRepo],
    links: list[TaskRepo],
    prs: list[TaskPr],
) -> TaskCode:
    """Build a task's Code row from the project's repositories and the task's own rows."""
    linked = {link.repo_id: link for link in links if link.task_id == task_id}
    own_prs = [p for p in prs if p.task_id == task_id]
    repos = [
        CodeRepo(r, linked[r.id].how, [p for p in own_prs if p.repo_id == r.id])
        for r in project_repos
        if r.id in linked
    ]
    shown = {r.repo.id for r in repos}
    loose = [p for p in own_prs if p.repo_id not in shown]
    return TaskCode(repos, loose, project_repos)


def default_repo_name(path: str, fallback: str) -> str:
    """The last folder of `path` in either slash style, valid as a repository name."""
    name = PureWindowsPath(path.strip().rstrip("/\\")).name
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:REPO_NAME_MAX]
    return name if name and REPO_NAME_RE.match(name) else fallback


def pr_number(url: str) -> int | None:
    match = _PR_NUMBER_RE.search(urlsplit(url).path + "/")
    return int(match.group(1)) if match else None


def pr_repo_name(url: str) -> str | None:
    """The repository segment of a pull request link (`…/<owner>/<repo>/pull/1`), lowercased."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    for at, part in enumerate(parts):
        if part in ("pull", "pulls", "merge_requests", "pull-requests") and at >= 1:
            name = parts[at - 1]
            if name == "-" and at >= 2:  # GitLab: /<group>/<repo>/-/merge_requests/1
                name = parts[at - 2]
            return name.lower()
    return None


def vscode_url(path: str) -> str:
    """`vscode://file/<path>` for a folder: forward slashes, a Windows drive kept as `C:`, every
    other character that is not safe in a URL percent-encoded. A `~` is left to the editor."""
    text = path.strip().replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    return "vscode://file" + quote(text, safe="/:~")


def holds(path: str, cwd: str) -> int | None:
    """How closely the repository at `path` holds the folder `cwd`: the length of its normalised
    path when `cwd` is that folder or inside it (the case rules of this machine's file system),
    else None. Only `~` and `~/…` mean the user's home: `~name` is not expanded (POSIX expansion
    of an unknown user raises), so such a path matches only literally. Nothing on disk is read."""
    text = path.strip()
    if text == "~" or text.startswith(("~/", "~\\")):
        text = str(Path.home()) + text[1:]
    root = os.path.normcase(os.path.normpath(text))
    target = os.path.normcase(os.path.normpath(cwd))
    if target == root or target.startswith(root.rstrip(os.sep) + os.sep):
        return len(root)
    return None
