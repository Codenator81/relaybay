"""The coordinator run end to end: the owner starts it on the Coordinator page, the owner's box
starts a turn of the fake `claude` in its folder, it asks a question without a card over MCP, and
the owner's answer comes as its next turn."""

from tests.e2e.conftest import Runs, poll

SESSION = "coordinator run 1"  # the first run of the stand, as its head names it


def flag_values(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, word in enumerate(argv[:-1]) if word == flag]


def test_the_coordinator_asks_without_a_card_and_the_answer_is_its_next_turn(runs: Runs) -> None:
    stand = runs.stand
    # No relaybay server in the user's settings: the run passes its own --mcp-config, which the
    # fake connects to with the board's token.
    (stand.claude_home / ".claude.json").write_text("{}", encoding="utf-8")
    runs.scenario(
        {
            "turns": [
                [
                    {
                        "mcp": "question_ask",
                        "arguments": {
                            "text": "Which project first?",
                            "default": "acme-shop",
                            "options": ["acme-shop", "demo-api"],
                            "blocking": True,
                            "session": SESSION,
                            "hook_session": "${session_id}",
                        },
                        "capture": {"question": r"Question #(\d+) asked"},
                    },
                    {"text": "Asked the owner."},
                ],
                [{"text": "inspected · S · demo-api first, as the owner said."}],
            ]
        }
    )
    with stand.board() as board:
        page = stand.post_form(board, "/coordinator/start")
        assert "The coordinator is started" in page.text
        [run] = runs.rows("SELECT * FROM run")
        assert (run["kind"], run["status"], run["task_id"]) == ("coordinator", "idle", None)
        assert runs.turns(run["id"]) == []
        folder = stand.settings.home / "coordinator"
        assert run["cwd"] == str(folder) and (folder / "CLAUDE.md").is_file()
        stand.post_form(board, "/coordinator/say", text="Plan the week.")
        poll(
            lambda: runs.rows("SELECT status FROM run")[0]["status"] == "waiting",
            what="the coordinator waiting for the owner",
        )
        [q] = runs.rows("SELECT * FROM question")
        assert (q["task_id"], q["run_id"], q["status"]) == (None, run["id"], "open")
        assert "Which project first?" in board.get("/coordinator").text
        inbox = board.get("/inbox").text
        assert "Which project first?" in inbox and 'href="/coordinator"' in inbox
        stand.post_form(
            board, f"/questions/{q['id']}/answer", choice="demo-api", **{"from": "coordinator"}
        )
        poll(lambda: len([t for t in runs.turns(run["id"]) if t["ended_at"]]) == 2, what="turn 2")
        shown = board.get("/coordinator").text
        assert "demo-api first, as the owner said." in shown
    first, second = runs.trace("argv")[:2]
    assert first["prompt"].startswith("relaybay coordinator run 1: one coordinator")
    assert first["prompt"].endswith("The owner says on the board: Plan the week.")
    argv = first["argv"]
    assert flag_values(argv, "--permission-mode") == ["dontAsk"]
    edit = [r for r in flag_values(argv, "--allowedTools") if r.startswith("Edit(")]
    assert len(edit) == 1 and edit[0].startswith("Edit(//") and edit[0].endswith("/coordinator/**)")
    assert "--add-dir" not in argv
    assert "CronCreate" in flag_values(argv, "--disallowedTools")
    assert second["prompt"].startswith(f"Owner's answer to question {q['id']}")
    assert second["argv"][-2:] == ["--resume", runs.rows("SELECT claude_session_id FROM run")[0][0]]
    [memory] = runs.trace("memory")[:1]
    assert any(path.endswith("CLAUDE.md") and "coordinator" in path for path in memory["files"])
