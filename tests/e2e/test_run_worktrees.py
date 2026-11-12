"""Runs over a project folder and several repositories: the worktrees a run makes, the files it
copies into them, relaybay's hooks from one source, and `run_start` over MCP."""

import json
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

from relaybay.machine_setup import hook_handler
from tests.e2e.conftest import Runs, git, make_git_repo

COORDINATOR = "lead-coordinator"


def flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, word in enumerate(argv) if word == flag]


def same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(Path(a).resolve()) == os.path.normcase(Path(b).resolve())


def worktree_paths(repo: Path) -> list[str]:
    listing = git(repo, "worktree", "list", "--porcelain")
    return [
        line.removeprefix("worktree ")
        for line in listing.splitlines()
        if line.startswith("worktree ")
    ]


def hook_counts(runs: Runs) -> Counter:
    return Counter(entry["event"] for entry in runs.trace("hook"))


def test_project_folder_over_two_repositories_makes_two_worktrees(runs: Runs) -> None:
    folder = runs.stand.root / "acme"
    api = make_git_repo(
        folder / "api",
        {"CLAUDE.md": "Rule from api.\n", ".claude/settings.local.json": "{}\n"},
    )
    web = make_git_repo(folder / "web", {"CLAUDE.md": "Rule from web.\n"})
    ids = runs.repos(api, web, folder=folder)
    with runs.stand.board() as board:
        form = board.get("/p/acme-shop/t/acme-shop-1/run").text
        assert "api: .claude/ (1 file), CLAUDE.md; web: CLAUDE.md" in form
        runs.start(board, cwd="folder", **{f"wt_{ids['api']}": "new", f"wt_{ids['web']}": "new"})
        run = runs.wait_run(lambda r: r["status"] == "idle")
        html = board.get("/p/acme-shop/t/acme-shop-1/run").text
    assert same(run["cwd"], folder) and run["cwd_repo_id"] is None
    rows = runs.worktrees(run["id"])
    assert [(r["repo_name"], r["created_by_run"], r["branch"]) for r in rows] == [
        ("api", 1, "relaybay/acme-shop-1"),
        ("web", 1, "relaybay/acme-shop-1"),
    ]
    assert json.loads(rows[0]["copied"]) == [".claude/settings.local.json", "CLAUDE.md"]
    assert json.loads(rows[1]["copied"]) == ["CLAUDE.md"]
    for source, row in ((api, rows[0]), (web, rows[1])):
        path = Path(row["path"])
        assert same(
            path.parent, runs.stand.settings.home / "worktrees" / f"acme-shop-1-r{run['id']}"
        )
        assert any(same(p, path) for p in worktree_paths(source))
        assert git(path, "ls-files", "CLAUDE.md").strip() == ""  # copied, never added
        assert git(source, "rev-list", "--all", "--count").strip() == "1"  # nothing committed
    [turn] = runs.trace("argv")
    assert [
        same(a, r["path"])
        for a, r in zip(flag_values(turn["argv"], "--add-dir"), rows, strict=True)
    ] == [
        True,
        True,
    ]
    assert turn["prompt"] is not None and "Work only in these folders." in turn["prompt"]
    [memory] = runs.trace("memory")
    assert {"Rule from api.", "Rule from web."} <= set(memory["lines"])
    assert all(
        not same(Path(f).parent, api) and not same(Path(f).parent, web) for f in memory["files"]
    )
    assert "new worktree on relaybay/acme-shop-1; copied: CLAUDE.md" in html


def test_worktreeinclude_chooses_what_is_copied(runs: Runs) -> None:
    api = make_git_repo(
        runs.stand.root / "api",
        {
            ".gitignore": "*.local.txt\n",
            ".worktreeinclude": "*.local.txt\nshared.txt\n",
            "notes.local.txt": "local\n",
            "shared.txt": "matched but not gitignored\n",
            "CLAUDE.md": "x\n",
        },
    )
    ids = runs.repos(api)
    with runs.stand.board() as board:
        runs.start(board, cwd=str(ids["api"]), **{f"wt_{ids['api']}": "new"})
        run = runs.wait_run(lambda r: r["status"] == "idle")
    [row] = runs.worktrees(run["id"])
    assert json.loads(row["copied"]) == ["notes.local.txt"]
    assert (Path(row["path"]) / "notes.local.txt").is_file()
    assert not (Path(row["path"]) / "CLAUDE.md").exists()
    assert (
        same(run["cwd"], row["path"])
        and flag_values(runs.trace("argv")[0]["argv"], "--add-dir") == []
    )


def test_a_failure_on_the_second_worktree_removes_the_first(runs: Runs) -> None:
    api = make_git_repo(runs.stand.root / "acme" / "api", {"CLAUDE.md": "x\n"})
    web = runs.stand.root / "acme" / "web"
    web.mkdir()  # not a git repository
    ids = runs.repos(api, web, folder=runs.stand.root / "acme")
    with runs.stand.board() as board:
        html = runs.start(
            board, cwd="folder", **{f"wt_{ids['api']}": "new", f"wt_{ids['web']}": "new"}
        )
    assert "web (" in html and "is not a git repository" in html
    assert runs.rows("SELECT * FROM run") == [] and runs.trace("argv") == []
    assert len(worktree_paths(api)) == 1  # only the repository itself
    assert not (runs.stand.settings.home / "worktrees" / "acme-shop-1-r1").exists()


def test_a_folder_in_use_by_an_open_run_is_refused(runs: Runs) -> None:
    folder = runs.stand.root / "acme"
    api = make_git_repo(folder / "api")
    web = make_git_repo(folder / "web")
    ids = runs.repos(api, web, folder=folder)
    runs.stand.mcp("task_create", project="acme-shop", title="Payment", stage="dev")
    with runs.stand.board() as board:
        runs.start(board, cwd="folder", **{f"wt_{ids['api']}": "new", f"wt_{ids['web']}": "self"})
        run = runs.wait_run(lambda r: r["status"] == "idle")
        refused = runs.start(
            board, key="acme-shop-2", cwd=str(ids["web"]), **{f"wt_{ids['web']}": "self"}
        )
        assert f"is in use by run {run['id']} on acme-shop-1" in refused
        assert len(runs.rows("SELECT * FROM run")) == 1
        runs.start(board, key="acme-shop-2", cwd=str(ids["web"]), **{f"wt_{ids['web']}": "new"})
        other = runs.wait_run(lambda r: r["status"] == "idle", key="acme-shop-2")
    [row] = runs.worktrees(other["id"])
    assert (row["created_by_run"], row["branch"]) == (1, "relaybay/acme-shop-2")


def test_user_claude_md_is_loaded(runs: Runs) -> None:
    (runs.stand.claude_home / ".claude" / "CLAUDE.md").write_text(
        "User rule: answer briefly.\n", encoding="utf-8"
    )
    with runs.stand.board() as board:
        runs.start(board)
        runs.wait_run(lambda r: r["status"] == "idle")
    [memory] = runs.trace("memory")
    assert "User rule: answer briefly." in memory["lines"]


def test_hooks_from_user_settings_fire_once(runs: Runs) -> None:
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
    assert (run["hooks_source"], run["mcp_source"]) == ("user", "user")
    [turn] = runs.trace("argv")
    assert "--settings" not in turn["argv"] and "--mcp-config" not in turn["argv"]
    counts = hook_counts(runs)
    assert counts["SessionStart"] == 1 and set(counts.values()) == {1}


def test_hooks_added_per_run_fire_once(runs: Runs) -> None:
    (runs.stand.claude_home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (runs.stand.claude_home / ".claude.json").write_text("{}", encoding="utf-8")
    with runs.stand.board() as board:
        form = board.get("/p/acme-shop/t/acme-shop-1/run").text
        assert "added for each run with --settings" in form
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
    assert (run["hooks_source"], run["mcp_source"]) == ("run", "run")
    folder = runs.stand.settings.home / "runs" / str(run["id"])
    [turn] = runs.trace("argv")
    assert [same(p, folder / "settings.json") for p in flag_values(turn["argv"], "--settings")] == [
        True
    ]
    assert [same(p, folder / "mcp.json") for p in flag_values(turn["argv"], "--mcp-config")] == [
        True
    ]
    assert "--strict-mcp-config" not in turn["argv"]
    hooks = json.loads((folder / "settings.json").read_text("utf-8"))["hooks"]
    wanted = hook_handler("command", runs.stand.url, runs.stand.settings.token_path)
    assert hooks["SessionStart"][0]["hooks"] == [wanted]
    entry = json.loads((folder / "mcp.json").read_text("utf-8"))["mcpServers"]["relaybay"]
    assert entry["headers"]["Authorization"] == f"Bearer {runs.stand.token}"
    if sys.platform != "win32":
        assert (folder / "mcp.json").stat().st_mode & 0o777 == 0o600
    counts = hook_counts(runs)
    assert counts["SessionStart"] == 1 and set(counts.values()) == {1}
    cc_id = run["claude_session_id"]
    [row] = runs.rows("SELECT id, kind FROM session WHERE cc_session_id = ?", cc_id)
    assert (row["id"], row["kind"]) == (run["session_id"], "run")


def test_both_sources_fire_once(runs: Runs, monkeypatch: pytest.MonkeyPatch) -> None:
    """The check found no hooks, but the CLI loads them from its own config folder too: the
    identical handler of --settings runs once."""
    user_settings = runs.stand.claude_home / ".claude" / "settings.json"
    alternative = runs.stand.root / "alt-config"
    alternative.mkdir()
    (alternative / "settings.json").write_text(user_settings.read_text("utf-8"), encoding="utf-8")
    user_settings.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alternative))
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
    assert run["hooks_source"] == "run"
    assert flag_values(runs.trace("argv")[0]["argv"], "--settings")
    counts = hook_counts(runs)
    assert counts["SessionStart"] == 1 and set(counts.values()) == {1}


def test_run_start_caller_rules(runs: Runs) -> None:
    stand = runs.stand
    api = make_git_repo(stand.root / "api", {"CLAUDE.md": "Rule from api.\n"})
    runs.repos(api)
    stand.settings.coordinators = [COORDINATOR]
    stand.stop_server()
    stand.start_server()
    refused = stand.mcp_error("run_start", task="acme-shop-1", session="dev-agent")
    assert "only for the coordinator" in refused and "backlog_add" in refused
    mode = stand.mcp_error(
        "run_start", task="acme-shop-1", session=COORDINATOR, permission_mode="bypassPermissions"
    )
    assert "takes no permission mode" in mode
    stand.mcp("task_create", project="acme-shop", title="Review me", stage="review")
    assert "plan or dev" in stand.mcp_error("run_start", task="acme-shop-2", session=COORDINATOR)
    stand.mcp("task_create", project="acme-shop", title="Braked", stage="dev")
    stand.mcp("brake_set", scope="task", task="acme-shop-3", reason="Hold", session=COORDINATOR)
    braked = stand.mcp_error("run_start", task="acme-shop-3", session=COORDINATOR)
    assert "under the brake" in braked
    assert runs.rows("SELECT * FROM run") == []
    started = stand.mcp("run_start", task="acme-shop-1", session=COORDINATOR, effort="high")
    assert "started on acme-shop-1" in started
    run = runs.wait_run(lambda r: r["status"] == "idle")
    assert (run["started_by"], run["started_by_kind"], run["permission_mode"], run["effort"]) == (
        COORDINATOR,
        "coordinator",
        "auto",
        "high",
    )
    [row] = runs.worktrees(run["id"])
    assert row["created_by_run"] == 1  # the coordinator always gets a new worktree
    [turn] = runs.turns(run["id"])
    assert turn["sent_by_kind"] == "coordinator"
    status = stand.mcp("run_status", task="acme-shop-1")
    assert f"Run {run['id']} on acme-shop-1: idle" in status
    assert "new worktree, branch relaybay/acme-shop-1; copied: CLAUDE.md" in status
    assert "no run" in stand.mcp("run_status", task="acme-shop-2")


def test_a_braked_coordinator_starts_no_run(runs: Runs) -> None:
    stand = runs.stand
    runs.repos(make_git_repo(stand.root / "api"))
    stand.settings.coordinators = [COORDINATOR]
    stand.stop_server()
    stand.start_server()
    stand.mcp(
        "brake_set", scope="session", target_session=COORDINATOR, reason="Stop", session=COORDINATOR
    )
    refused = stand.mcp_error("run_start", task="acme-shop-1", session=COORDINATOR)
    assert "You are under the brake" in refused
    assert runs.rows("SELECT * FROM run") == []
