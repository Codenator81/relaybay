"""Worktree planning and the copy rules of a run, against real git repositories."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from relaybay.domain.errors import ValidationError
from relaybay.machine_setup import run_quiet
from relaybay.runner import worktrees
from relaybay.runner.worktrees import (
    WorktreeSpec,
    add_all,
    copy_into,
    copy_list,
    plan_worktrees,
    summary,
)
from tests.e2e.conftest import git, make_git_repo

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")


def test_plan_puts_new_worktrees_in_the_run_folder_and_keeps_in_place_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "worktrees"
    specs = plan_worktrees(
        "acme-shop-1",
        4,
        [(1, "api", "/code/api", True), (2, "web", "/code/web", False), (3, "API", "/x/api", True)],
        root,
    )
    assert [(s.repo_name, s.create) for s in specs] == [
        ("api", True),
        ("web", False),
        ("API", True),
    ]
    assert specs[0].path == str(root / "acme-shop-1-r4" / "api")
    assert specs[1].path == "/code/web"
    assert specs[2].path == str(root / "acme-shop-1-r4" / "API-2")  # case-folded clash
    names = worktrees.branch_names("acme-shop-1", 4)
    assert names[:3] == [
        "relaybay/acme-shop-1",
        "relaybay/acme-shop-1-r4",
        "relaybay/acme-shop-1-r4-2",
    ]


@needs_git
def test_copy_list_takes_untracked_claude_files_only(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "api", {".claude/commands/review.md": "tracked\n"})
    git(repo, "add", "-f", ".claude/commands/review.md")  # a global exclude may hide .claude
    git(repo, "commit", "-q", "--no-verify", "--no-gpg-sign", "-m", "commands")
    for name in ("CLAUDE.md", "CLAUDE.local.md", ".mcp.json", ".claude/settings.local.json"):
        (repo / name).write_text("local\n", encoding="utf-8")
    (repo / "notes.txt").write_text("not ours\n", encoding="utf-8")
    nested = make_git_repo(repo / ".claude" / "worktrees" / "old")
    assert nested.is_dir()
    assert copy_list(str(repo), GIT or "git") == [
        ".claude/settings.local.json",
        ".mcp.json",
        "CLAUDE.local.md",
        "CLAUDE.md",
    ]


@needs_git
def test_worktreeinclude_takes_matches_that_are_gitignored_too(tmp_path: Path) -> None:
    repo = make_git_repo(
        tmp_path / "api",
        {
            ".gitignore": "*.local.env\nconfig/\n",
            ".worktreeinclude": "*.local.env\nnotes.env\nconfig/\n",
            "app.local.env": "A=1\n",
            "sub/deep.local.env": "B=2\n",
            "notes.env": "matched, not ignored\n",
            "config/dev.json": "{}\n",
            "CLAUDE.md": "not listed\n",
        },
    )
    assert copy_list(str(repo), GIT or "git") == [
        "app.local.env",
        "config/dev.json",
        "sub/deep.local.env",
    ]


def test_include_pathspecs_narrow_anchored_patterns_only() -> None:
    assert worktrees.include_pathspecs("# c\n\n!x\n/app.env\nconfig/\n*.env\n") == [
        ":(glob)app.env",
        ":(glob)app.env/**",
        ":(glob)**/config",
        ":(glob)**/config/**",
        ":(glob)**/*.env",
        ":(glob)**/*.env/**",
    ]


def test_copy_into_skips_escapes_symlinks_and_what_exists(tmp_path: Path) -> None:
    source = tmp_path / "src"
    target = tmp_path / "dst"
    (source / ".claude").mkdir(parents=True)
    (source / "CLAUDE.md").write_text("rule\n", encoding="utf-8")
    (source / ".claude" / "a.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")
    (target / ".claude").mkdir(parents=True)
    (target / ".claude" / "a.json").write_text("theirs\n", encoding="utf-8")
    names = ["CLAUDE.md", ".claude/a.json", "../outside.txt", "/etc/passwd", "gone.md"]
    linked = False
    try:
        (source / "link.md").symlink_to(tmp_path / "outside.txt")
        names.append("link.md")
        linked = True
    except OSError:  # Windows without the symlink privilege
        pass
    copied, skipped = copy_into(str(source), str(target), names)
    assert copied == ["CLAUDE.md"]
    expected = {".claude/a.json", "../outside.txt", "/etc/passwd", "gone.md"}
    assert set(skipped) == expected | ({"link.md"} if linked else set())
    assert (target / ".claude" / "a.json").read_text("utf-8") == "theirs\n"
    assert not (target / "link.md").exists()


def test_copy_into_refuses_too_much(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worktrees, "COPY_MAX_FILES", 1)
    for name in ("a", "b"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    with pytest.raises(ValidationError, match=r"\.worktreeinclude"):
        copy_into(str(tmp_path), str(tmp_path / "dst"), ["a", "b"])


def test_summary_is_one_line() -> None:
    assert summary([]) == "nothing"
    assert summary([".claude/a.json", ".claude/b.json", "CLAUDE.md"]) == (
        ".claude/ (2 files), CLAUDE.md"
    )


@needs_git
def test_add_all_takes_the_run_branch_when_the_card_branch_exists(tmp_path: Path) -> None:
    repo = make_git_repo(tmp_path / "api", {"CLAUDE.md": "rule\n"})
    git(repo, "branch", "relaybay/acme-shop-1")
    [spec] = plan_worktrees("acme-shop-1", 2, [(1, "api", str(repo), True)], tmp_path / "wt")
    [made] = add_all([spec], GIT, "acme-shop-1", 2)
    assert made.branch == "relaybay/acme-shop-1-r2" and made.copied == ("CLAUDE.md",)
    assert (Path(made.path) / "CLAUDE.md").read_text("utf-8") == "rule\n"


@needs_git
def test_a_failed_start_writes_nothing_and_the_third_start_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_git_repo(tmp_path / "api", {".claude/a": "x", ".claude/b": "y"})
    monkeypatch.setattr(worktrees, "COPY_MAX_FILES", 1)
    for _ in range(2):
        [spec] = plan_worktrees("acme-shop-1", 7, [(1, "api", str(repo), True)], tmp_path / "wt")
        with pytest.raises(ValidationError, match="worktreeinclude"):
            add_all([spec], GIT, "acme-shop-1", 7)
    assert git(repo, "branch", "--list", "relaybay/*").strip() == ""  # checked before any add
    assert not (tmp_path / "wt").exists()
    monkeypatch.setattr(worktrees, "COPY_MAX_FILES", 2000)
    # Branches left by earlier failures (an older release, a removal that failed): the next one.
    git(repo, "branch", "relaybay/acme-shop-1")
    git(repo, "branch", "relaybay/acme-shop-1-r7")
    [spec] = plan_worktrees("acme-shop-1", 7, [(1, "api", str(repo), True)], tmp_path / "wt")
    [made] = add_all([spec], GIT, "acme-shop-1", 7)
    assert made.branch == "relaybay/acme-shop-1-r7-2"


@needs_git
def test_a_leftover_run_folder_moves_the_worktrees_to_a_numbered_one(tmp_path: Path) -> None:
    api = make_git_repo(tmp_path / "api")
    web = make_git_repo(tmp_path / "web")
    (tmp_path / "wt" / "acme-shop-1-r3" / "api").mkdir(parents=True)  # could not be removed
    specs = plan_worktrees(
        "acme-shop-1", 3, [(1, "api", str(api), True), (2, "web", str(web), True)], tmp_path / "wt"
    )
    made: list[WorktreeSpec] = []
    result = add_all(specs, GIT, "acme-shop-1", 3, made=made)
    assert [Path(m.path) for m in result] == [
        tmp_path / "wt" / "acme-shop-1-r3-2" / "api",
        tmp_path / "wt" / "acme-shop-1-r3-2" / "web",
    ]
    assert made == result  # the caller's list holds them as they are made


@needs_git
def test_a_repository_that_is_not_git_refuses_before_any_worktree(tmp_path: Path) -> None:
    api = make_git_repo(tmp_path / "api", {"CLAUDE.md": "rule\n"})
    plain = tmp_path / "web"
    plain.mkdir()
    specs = plan_worktrees(
        "acme-shop-1",
        1,
        [(1, "api", str(api), True), (2, "web", str(plain), True)],
        tmp_path / "wt",
    )
    with pytest.raises(ValidationError, match=r"web .* is not a git repository"):
        add_all(specs, GIT, "acme-shop-1", 1)
    assert not (tmp_path / "wt").exists()
    assert git(api, "branch", "--list", "relaybay/*").strip() == ""


@needs_git
def test_add_all_removes_what_it_made_when_a_later_add_fails(tmp_path: Path) -> None:
    api = make_git_repo(tmp_path / "api", {"CLAUDE.md": "rule\n"})
    web = make_git_repo(tmp_path / "web")
    adds: list[list[str]] = []

    def run(argv: list[str], **options: object) -> subprocess.CompletedProcess:
        if "worktree" in argv and "add" in argv:
            adds.append(argv)
            if len(adds) == 2:  # git refuses the second one (a lock, a full disk)
                return subprocess.CompletedProcess(argv, 128, "", "fatal: could not lock\n")
        return run_quiet(argv, **options)

    specs = plan_worktrees(
        "acme-shop-1", 1, [(1, "api", str(api), True), (2, "web", str(web), True)], tmp_path / "wt"
    )
    made: list[WorktreeSpec] = []
    with pytest.raises(ValidationError, match="git worktree add for web failed: fatal: could not"):
        add_all(specs, GIT, "acme-shop-1", 1, run=run, made=made)
    assert made == [] and not Path(specs[0].path).exists()
    assert not (tmp_path / "wt" / "acme-shop-1-r1").exists()
    assert git(api, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_add_all_checks_a_repository_in_place_exists(tmp_path: Path) -> None:
    spec = WorktreeSpec(1, "api", str(tmp_path / "missing"), str(tmp_path / "missing"), False)
    with pytest.raises(ValidationError, match="is not a folder"):
        add_all([spec], None, "acme-shop-1", 1)
    with pytest.raises(ValidationError, match="git is not on PATH"):
        add_all([WorktreeSpec(1, "api", str(tmp_path), str(tmp_path / "w"), True)], None, "k-1", 1)


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows only")
def test_a_leftover_junction_is_dropped_never_followed(tmp_path: Path) -> None:
    from relaybay.runner.worktrees import _drop_leftover_links

    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    left = tmp_path / "wt" / "run-1"
    (left / "node_modules").mkdir(parents=True)
    link = left / "node_modules" / "linked"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
    )
    _drop_leftover_links(left)
    assert not left.exists()
    assert (outside / "keep.txt").exists() and (outside / "inner").is_dir()
