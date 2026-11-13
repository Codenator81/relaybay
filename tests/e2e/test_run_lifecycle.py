"""The brake on a run and a card's close, end to end: the server starts the fake `claude`, its
PreToolUse hook goes through the real forwarder, and git makes and removes the worktrees."""

import json
from pathlib import Path

from relaybay.security import csrf_token
from tests.e2e.conftest import Runs, alive, git, make_git_repo, poll


def test_a_project_brake_kills_the_running_turn_after_its_refused_call(runs: Runs) -> None:
    root = runs.stand.root
    pidfile, go, never = root / "child.pid", root / "go", root / "never"
    runs.scenario(
        {
            "turn": [
                {"spawn_child": str(pidfile)},
                {"wait_file": str(go), "timeout": 120},
                {"tool": "Bash", "input": {"command": "make build"}, "result": "built"},
            ],
            # After the refusal the process stays until it is killed: the kill is the test.
            "end_wait_file": str(never),
        }
    )
    with runs.stand.board() as board:
        runs.start(board)
        poll(pidfile.exists, what="the grandchild")
        run = runs.wait_run(lambda r: r["status"] == "running")
        child = int(pidfile.read_text("utf-8"))
        runs.stand.post_form(
            board, "/brake", scope="project", project="acme-shop", reason="Stop now", next="/"
        )
        go.write_text("go", encoding="utf-8")
        stopped = runs.wait_run(lambda r: r["status"] == "stopped")
        assert stopped["status_reason"] == "braked (project acme-shop): Stop now"
        poll(lambda: not alive(child), what="the grandchild gone")
        assert "PreToolUse" in [e["event"] for e in runs.trace("hook")]
        assert runs.turns(run["id"])[0]["outcome"] in ("stopped", "success")
        html = runs.act(board, run["id"], "send", text="Carry on")
        assert "braked (project acme-shop)" in html and "Start anyway" in html
        held = runs.wait_run(lambda r: r["status"] == "queued")
        assert held["status_reason"].startswith("braked (project acme-shop)")
        refused = runs.act(board, run["id"], "anyway")
        assert "does not go past a brake" in refused
        assert len(runs.trace("argv")) == 1  # no second turn started


def test_a_card_to_done_removes_the_worktrees_its_run_made(runs: Runs) -> None:
    stand = runs.stand
    folder = stand.root / "acme"
    api, web = make_git_repo(folder / "api"), make_git_repo(folder / "web")
    ids = runs.repos(api, web, folder=folder)
    runs.scenario({"turn": [{"text": "Done."}]})
    with stand.board() as board:
        runs.start(board, cwd="folder", **{f"wt_{ids['api']}": "new", f"wt_{ids['web']}": "new"})
        run = runs.wait_run(lambda r: r["status"] == "idle")
        made = runs.worktrees(run["id"])
        assert len(made) == 2 and all(w["created_by_run"] for w in made)
        Path(made[1]["path"], "draft.txt").write_text("left over\n", encoding="utf-8")
        stand.mcp("task_set_stage", task="acme-shop-1", stage="done", session="tester")
        ended = runs.wait_run(lambda r: r["status"] == "ended")
        assert ended["status_reason"] == "the card went to done"

        def settled() -> list | None:
            rows = runs.worktrees(run["id"])
            return rows if rows[0]["removed_at"] and rows[1]["kept_reason"] else None

        first, second = poll(settled, what="the close of the card")
        assert not Path(first["path"]).exists()
        assert Path(second["path"], "draft.txt").is_file()
        assert second["kept_reason"] == "uncommitted changes (draft.txt)"
        [q] = runs.rows("SELECT * FROM question WHERE id = ?", second["question_id"])
        assert (q["status"], json.loads(q["options"]), q["default_answer"]) == (
            "open",
            ["remove", "keep"],
            "keep",
        )
        listed = git(api, "worktree", "list", "--porcelain").replace("\\", "/")
        assert first["path"].replace("\\", "/") not in listed
        response = board.post(
            f"/questions/{q['id']}/answer",
            data={"csrf_token": csrf_token(stand.token), "choice": "remove"},
            headers={"Origin": stand.url},
        )
        assert response.status_code in (200, 303)
        poll(lambda: runs.worktrees(run["id"])[1]["removed_at"], what="the removal on the answer")
        assert not Path(second["path"]).exists()
        assert api.is_dir() and web.is_dir() and (web / "README.md").is_file()
