"""Questions on a run end to end: the fake `claude` asks over MCP the way the real CLI calls a
tool, the server sets the run waiting at the turn's result, the owner answers on the board and
the next turn's prompt holds the answer verbatim."""

from typing import Any

from tests.e2e.conftest import Runs, poll

SESSION = "acme-shop-1 run 1"  # the first run's session, as the run header names it


def ask_step(**arguments: Any) -> dict[str, Any]:
    return {
        "mcp": "question_ask",
        "arguments": {
            "task": "acme-shop-1",
            "session": SESSION,
            "hook_session": "${session_id}",
            **arguments,
        },
        "capture": {"question": r"Question #(\d+) asked"},
    }


def mcp_per_run(runs: Runs) -> None:
    """No relaybay server in the user's settings: the run passes its own --mcp-config, which
    the fake connects to with the board's token."""
    (runs.stand.claude_home / ".claude.json").write_text("{}", encoding="utf-8")


def answer(runs: Runs, board: Any, question: int, **fields: str) -> str:
    return runs.post(board, f"/questions/{question}/answer", **fields)


def test_a_blocking_question_waits_and_the_answer_starts_the_next_turn(runs: Runs) -> None:
    mcp_per_run(runs)
    runs.scenario(
        {
            "turns": [
                [
                    ask_step(
                        text="Which payment provider?",
                        default="Stripe",
                        options=["Stripe", "Adyen"],
                        blocking=True,
                    ),
                    {"text": "Asked the owner; ending the turn."},
                ],
                [{"text": "Going on with Adyen."}],
            ]
        }
    )
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "waiting")
        [q] = runs.rows("SELECT * FROM question")
        assert (q["run_id"], q["blocking"], q["status"]) == (run["id"], 1, "open")
        assert run["status_reason"] == f"waiting for the owner's answer to question {q['id']}"
        [task] = runs.rows("SELECT waiting_on FROM task WHERE key = 'acme-shop-1'")
        assert task["waiting_on"] == "owner"
        inbox = board.get("/inbox").text
        assert "Which payment provider?" in inbox and "blocks the run" in inbox
        html = answer(runs, board, q["id"], choice="Adyen", text="the contract is with them")
        assert "answered" in html
        poll(lambda: len([t for t in runs.turns(run["id"]) if t["ended_at"]]) == 2, what="turn 2")
    second = runs.trace("argv")[1]
    assert second["prompt"] == (
        f'Owner\'s answer to question {q["id"]} ("Which payment provider?"):'
        " Adyen: the contract is with them"
    )
    assert second["argv"][-2:] == ["--resume", run["claude_session_id"]]
    [done] = runs.rows("SELECT * FROM question")
    assert done["status"] == "answered" and done["delivered_at"] is not None
    kinds = [
        r["kind"]
        for r in runs.rows("SELECT kind FROM event WHERE kind LIKE 'question.%' ORDER BY id")
    ]
    assert kinds == ["question.asked", "question.answered", "question.delivered"]


def test_a_look_answer_during_the_turn_waits_and_the_hooks_say_so(runs: Runs) -> None:
    mcp_per_run(runs)
    go = runs.stand.root / "go"
    runs.scenario(
        {
            "turns": [
                [
                    ask_step(
                        kind="look",
                        text="Check the cart total under the Pay button",
                        default="fine",
                        url="http://127.0.0.1:5173/cart",
                        account="demo-buyer",
                    ),
                    {"wait_file": str(go)},
                    {"tool": "Read", "input": {"file_path": "README.md"}, "result": "ok"},
                    {"text": "Done for now."},
                ],
                [{"text": "Fixing the total."}],
            ]
        }
    )
    with runs.stand.board() as board:
        runs.start(board)
        poll(lambda: runs.rows("SELECT * FROM question"), what="the question")
        [q] = runs.rows("SELECT * FROM question")
        assert (q["kind"], q["blocking"], q["look_url"], q["look_account"]) == (
            "look",
            1,
            "http://127.0.0.1:5173/cart",
            "demo-buyer",
        )
        card = board.get("/p/acme-shop/t/acme-shop-1/questions").text
        assert 'href="http://127.0.0.1:5173/cart" target="_blank"' in card and "demo-buyer" in card
        answer(runs, board, q["id"], choice="remarks", text="The total is cut off on a phone")
        run = runs.runs()[-1]
        [first, second] = runs.turns(run["id"])
        assert first["started_at"] and second["started_at"] is None  # queued behind the turn
        go.write_text("go", encoding="utf-8")
        poll(lambda: len([t for t in runs.turns(run["id"]) if t["ended_at"]]) == 2, what="turn 2")
    # Told once, by whichever tool hook comes first after the answer (PreToolUse or PostToolUse).
    told = [c["text"] for c in runs.trace("context") if c["event"] in ("PreToolUse", "PostToolUse")]
    waiting = f"Owner's answer to question {q['id']}"
    assert sum(waiting in text and "is waiting" in text for text in told) == 1
    assert runs.trace("argv")[1]["prompt"].endswith("): The total is cut off on a phone")
