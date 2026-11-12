"""Runs on cards end to end: the server starts the fake `claude` itself, one turn at a time, and
its hooks report back through the real forwarder."""

import os
import sqlite3
import sys
import zlib
from pathlib import Path

from relaybay import doctor
from relaybay.security import csrf_token
from tests.e2e.conftest import Runs, alive, poll

FORBIDDEN = ("--bare", "--setting-sources", "--strict-mcp-config", "--dangerously-skip-permissions")
BATCH = sys.platform == "win32"  # the stand's shim is claude.cmd there: the prompt goes on stdin
IDENTITY = ("owner@example.invalid", "Acme Org", "org-123")


def expected_argv(prompt: str | None, resume: str | None = None) -> list[str]:
    head = ["-p"] if BATCH else ["-p", prompt or ""]
    tail = ["--resume", resume] if resume else []
    return [
        *head,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "opus",
        "--effort",
        "medium",
        "--permission-mode",
        "auto",
        "--allowedTools",
        "mcp__relaybay",
        *tail,
    ]


def done_turns(runs: Runs, run_id: int, count: int) -> list:
    def finished() -> list | None:
        turns = runs.turns(run_id)
        ended = [t for t in turns if t["ended_at"]]
        return turns if len(ended) >= count else None

    return poll(finished, what=f"{count} ended turn(s)")


def test_first_turn_session_id_then_resume(runs: Runs) -> None:
    runs.scenario({"turns": [[{"text": "First."}], [{"text": "Second."}]]})
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
        cc_id = run["claude_session_id"]
        assert cc_id and run["auth_method"] == "claude.ai"
        runs.act(board, run["id"], "send", text="Go on with the tests")
        turns = done_turns(runs, run["id"], 2)
        assert runs.wait_run(lambda r: r["status"] == "idle")["claude_session_id"] == cc_id
    assert [t["outcome"] for t in turns] == ["success", "success"]
    first, second = runs.trace("argv")
    assert first["prompt"].startswith(f"relaybay run {run['id']} on card acme-shop-1")
    assert second["prompt"] == "Go on with the tests"
    assert first["argv"] == expected_argv(first["prompt"])
    assert second["argv"] == expected_argv(second["prompt"], resume=cc_id)
    assert first["run_id"] == second["run_id"] == str(run["id"])
    for entry in (first, second):
        assert not [w for w in entry["argv"] if w in FORBIDDEN]


def test_hooks_link_both_turns_to_the_run_session(runs: Runs) -> None:
    runs.scenario({"turns": [[{"text": "First."}], [{"text": "Second."}]]})
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
        runs.act(board, run["id"], "send", text="Again")
        done_turns(runs, run["id"], 2)
    cc_id = run["claude_session_id"]
    rows = runs.rows("SELECT * FROM session WHERE cc_session_id = ?", cc_id)
    assert [(r["id"], r["kind"], r["cc_exact"]) for r in rows] == [(run["session_id"], "run", 1)]
    assert runs.rows("SELECT COUNT(*) FROM session WHERE kind = 'hook'")[0][0] == 0
    [task] = runs.rows("SELECT session_id FROM task WHERE key = 'acme-shop-1'")
    assert task["session_id"] == run["session_id"]  # each turn's SessionEnd keeps the card bound
    events = runs.rows(
        "SELECT COUNT(*) FROM event WHERE kind = 'task.session_unbound' AND session_id = ?",
        run["session_id"],
    )
    assert events[0][0] == 0


def test_stop_kills_the_process_tree_and_continue_resumes(runs: Runs) -> None:
    pidfile = runs.stand.root / "child.pid"
    never = runs.stand.root / "never"
    runs.scenario(
        {
            "turns": [
                [{"spawn_child": str(pidfile)}, {"wait_file": str(never), "timeout": 120}],
                [{"text": "Resumed."}],
            ]
        }
    )
    with runs.stand.board() as board:
        runs.start(board)
        poll(pidfile.exists, what="the grandchild")
        run = runs.wait_run(lambda r: r["claude_session_id"] is not None)
        child = int(pidfile.read_text("utf-8"))
        [turn] = runs.turns(run["id"])
        assert turn["pid"] and alive(turn["pid"]) and alive(child)
        html = runs.act(board, run["id"], "stop")
        assert "stopped by owner" in html
        poll(lambda: not alive(child) and not alive(turn["pid"]), what="the tree gone")
        stopped = runs.wait_run(lambda r: r["status"] == "stopped")
        assert stopped["status_reason"] == "stopped by owner"
        assert runs.turns(run["id"])[0]["outcome"] == "stopped"
        runs.act(board, run["id"], "send", text="Carry on")
        turns = done_turns(runs, run["id"], 2)
        assert turns[1]["outcome"] == "success"
    assert runs.trace("argv")[1]["argv"][-2:] == ["--resume", run["claude_session_id"]]


def test_second_open_run_is_refused_on_the_card_and_on_the_repository(runs: Runs) -> None:
    runs.stand.mcp("task_create", project="acme-shop", title="Payment", stage="dev")
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
        again = runs.start(board)
        assert "acme-shop-1 already has an open run" in again
        other = runs.start(board, key="acme-shop-2")
        assert f"is in use by run {run['id']} on acme-shop-1" in other
        assert len(runs.rows("SELECT * FROM run")) == 1
        runs.act(board, run["id"], "end")
        assert runs.runs()[-1]["status"] == "ended"
        runs.start(board, key="acme-shop-2")
        runs.wait_run(lambda r: r["status"] == "idle", key="acme-shop-2")


def test_text_during_a_turn_waits(runs: Runs) -> None:
    go = runs.stand.root / "go"
    runs.scenario({"turns": [[{"wait_file": str(go)}, {"text": "One."}], [{"text": "Two."}]]})
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["claude_session_id"] is not None)
        html = runs.act(board, run["id"], "send", text="And then this")
        assert "waits for the running turn" in html
        [first, second] = runs.turns(run["id"])
        assert first["started_at"] and second["started_at"] is None  # queued
        go.write_text("go", encoding="utf-8")
        turns = done_turns(runs, run["id"], 2)
    assert [t["outcome"] for t in turns] == ["success", "success"]
    [end_one, _] = runs.trace("turn_end")
    [_, start_two] = runs.trace("argv")
    assert end_one["ended"] <= start_two["started"]  # never two processes of one run at once


def test_usage_reaches_the_card(runs: Runs) -> None:
    runs.scenario({"turn": [{"text": "One."}, {"text": "Two."}]})
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
        [turn] = runs.turns(run["id"])
        assert (
            turn["input_tokens"],
            turn["output_tokens"],
            turn["cache_read_tokens"],
            turn["cache_write_tokens"],
            turn["num_turns"],
        ) == (20, 10, 200, 40, 2)
        html = board.get("/p/acme-shop/t/acme-shop-1/run").text
    assert "270" in html and "cache read 200" in html and "idle" in html


def test_signed_out_shows_login_and_starts_nothing(runs: Runs) -> None:
    runs.auth({"loggedIn": False, "authMethod": "none"}, code=1)
    with runs.stand.board() as board:
        html = runs.start(board)
    assert "claude: not signed in" in html and "claude, then /login inside it" in html
    assert runs.rows("SELECT * FROM run") == [] and runs.trace("argv") == []


def test_api_key_needs_the_switch(runs: Runs) -> None:
    runs.auth({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"})
    with runs.stand.board() as board:
        html = runs.start(board)
        assert "signed in with an API key" in html and "Settings → Agents" in html
        assert runs.rows("SELECT * FROM run") == []
        response = board.post(
            "/settings/agents/login-methods",
            data={"csrf_token": csrf_token(runs.stand.token), "allow": ["0", "1"]},
            headers={"Origin": runs.stand.url},
        )
        assert response.status_code == 200 and "Saved." in response.text
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
    assert run["auth_method"] == "api_key"


def test_third_party_starts_nothing(runs: Runs) -> None:
    runs.auth({"loggedIn": True, "authMethod": "third_party", "apiProvider": "bedrock"})
    with runs.stand.board() as board:
        html = runs.start(board)
    assert "third-party provider" in html and runs.rows("SELECT * FROM run") == []


def test_login_identity_is_stored_nowhere(runs: Runs) -> None:
    with runs.stand.board() as board:
        before = board.get("/p/acme-shop/t/acme-shop-1/run").text
        assert "signed in with a subscription (max)" in before
        runs.start(board)
        runs.wait_run(lambda r: r["status"] == "idle")
        answers = [
            board.get(path)
            for path in ("/p/acme-shop/t/acme-shop-1/run", "/settings/agents", "/p/acme-shop")
        ]
        assert [a.status_code for a in answers] == [200, 200, 200]
        assert "signed in with a subscription" in answers[1].text
        pages = [before, *(a.text for a in answers)]
    conn = sqlite3.connect(runs.stand.settings.db_path)
    try:
        dump = "\n".join(conn.iterdump())
    finally:
        conn.close()
    checks = doctor.doctor_checks(runs.stand.settings, environ=os.environ)
    [login] = [c for c in checks if c.name == "claude sign-in"]
    assert login.ok and "subscription" in login.detail
    doctor_text = "\n".join(f"{c.name} {c.detail}" for c in checks)
    for text in (*pages, dump, doctor_text):
        for secret in IDENTITY:
            assert secret not in text


def test_process_gone_without_result_is_lost(runs: Runs) -> None:
    runs.scenario({"turn": [{"text": "Working."}, {"exit": 3}]})
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "lost")
    assert run["status_reason"] == "claude exited with code 3 without a result"
    [turn] = runs.turns(run["id"])
    assert (turn["outcome"], turn["exit_code"]) == ("lost", 3)


def test_server_stop_stops_the_turn_and_its_process(runs: Runs) -> None:
    pidfile = runs.stand.root / "child.pid"
    never: Path = runs.stand.root / "never"
    runs.scenario(
        {"turn": [{"spawn_child": str(pidfile)}, {"wait_file": str(never), "timeout": 120}]}
    )
    with runs.stand.board() as board:
        runs.start(board)
        poll(pidfile.exists, what="the grandchild")
    child = int(pidfile.read_text("utf-8"))
    runs.stand.stop_server()
    run = runs.runs()[-1]
    assert (run["status"], run["status_reason"]) == ("stopped", "server stopped")
    poll(lambda: not alive(child), what="the grandchild gone")
    runs.stand.start_server()
    assert runs.runs()[-1]["status"] == "stopped"  # a clean stop leaves nothing to recover


def test_stop_while_the_process_ends_after_its_result_cancels_the_waiting_text(
    runs: Runs,
) -> None:
    release = runs.stand.root / "release"
    runs.scenario({"turn": [{"text": "Done."}], "end_wait_file": str(release)})
    with runs.stand.board() as board:
        runs.start(board)
        poll(lambda: runs.trace("result"), what="the result")
        run = runs.runs()[-1]
        assert run["status"] == "running"  # the result is out, the process still ends
        runs.act(board, run["id"], "send", text="Next")
        html = runs.act(board, run["id"], "stop")
        assert "stopped by owner" in html
    stopped = runs.runs()[-1]
    assert (stopped["status"], stopped["status_reason"]) == ("stopped", "stopped by owner")
    assert [t["outcome"] for t in runs.turns(run["id"])] == ["success", "cancelled"]
    assert len(runs.trace("argv")) == 1  # no second process


# Built at run time so no token shape sits in the source.
GITHUB_TOKEN = "ghp_" + "Fa1Ke2To3Ke4" * 3
ANTHROPIC_KEY = "sk-ant-" + "api03-" + "nOtArEaLkEy0" * 4


def test_a_turn_with_a_subagent_lands_in_full_masked_with_nested_lines(runs: Runs) -> None:
    final = "verified · M · the scan is done\n\n- the index holds (verified: grep in the fake)"
    runs.scenario(
        {
            "turn": [
                {
                    "tool": "Agent",
                    "input": {"description": "scan", "prompt": "look"},
                    "steps": [
                        {"tool": "Grep", "input": {"pattern": "x"}, "result": "3 hits"},
                        {"text": "The subagent found 3 hits."},
                    ],
                    "result": "subagent finished",
                },
                {"tool": "Read", "input": {"file_path": "a.env"}, "result": f"T={GITHUB_TOKEN}"},
                {"text": f"Key seen: {ANTHROPIC_KEY}"},
                {"tool": "Bash", "input": {"command": f"git push --token {GITHUB_TOKEN}"}},
                {"text": final},
            ]
        }
    )
    with runs.stand.board() as board:
        runs.start(board)
        run = runs.wait_run(lambda r: r["status"] == "idle")
        [turn] = runs.turns(run["id"])
        panel = board.get(f"/p/acme-shop/t/acme-shop-1/run/{run['id']}/turns/1").text
    assert turn["report_id"] and turn["denials"] == 1
    [report] = runs.rows("SELECT * FROM report WHERE id = ?", turn["report_id"])
    assert report["body_md"].startswith(final) and report["size"] == "M"
    assert "**Refusals** (1, permission denials of the turn):" in report["body_md"]
    assert "- Bash: `` git push --token *** ``" in report["body_md"]
    [check] = runs.rows("SELECT * FROM report_check WHERE report_id = ?", report["id"])
    assert check["location"] == "grep in the fake"
    events = runs.rows("SELECT * FROM run_event WHERE turn_id = ? ORDER BY seq", turn["id"])
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert {e["type"] for e in events} >= {"system", "assistant", "user", "result"}
    nested = [e for e in events if e["parent_tool_use_id"]]
    assert len(nested) >= 3  # the subagent's Grep call, its result and its text
    lines = [zlib.decompress(e["data"]).decode("utf-8") for e in events]
    assert all(GITHUB_TOKEN not in x and ANTHROPIC_KEY not in x for x in lines)
    assert any("ghp_***" in x for x in lines) and any("sk-***" in x for x in lines)
    assert "Grep" in panel and "3 hits" in panel and "Refused" in panel
    assert GITHUB_TOKEN not in panel
    # Nowhere in the database file, in any table, compressed or not.
    runs.stand.mcp("task_get", task="acme-shop-1")  # a write-free call; then read the files
    for path in (Path(runs.stand.settings.db_path), Path(f"{runs.stand.settings.db_path}-wal")):
        if path.exists():
            data = path.read_bytes()
            assert GITHUB_TOKEN.encode() not in data and ANTHROPIC_KEY.encode() not in data
    listed = runs.stand.mcp("run_events", run=run["id"], limit=200)
    assert f"Run {run['id']} turn 1 (success)" in listed and GITHUB_TOKEN not in listed
    assert "  #" in listed  # subagent lines indented under their call
    status = runs.stand.mcp("run_status", task="acme-shop-1")
    assert "Last turn's report: verified · M · the scan is done" in status
