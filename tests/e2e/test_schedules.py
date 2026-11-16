"""Schedules end to end: the owner adds a read-only check on the Schedules page, Run now starts
the fake `claude` once with `-p`, without `--resume`, with `--no-session-persistence`,
`--max-turns 8` and the read-only profile, and its answer becomes a report on the card; a check
that reaches its step limit says so."""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from relaybay.domain import profiles
from tests.e2e.conftest import Runs, poll
from tests.e2e.test_coordinator import flag_values

READ_ONLY = ["Read", "Glob", "Grep", *profiles._READ_ONLY_GIT]


def add_check(runs: Runs, board: httpx.Client, text: str) -> int:
    later = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    page = runs.stand.post_form(
        board,
        "/schedules",
        kind="check",
        repeat="once",
        at=later,
        task="acme-shop-1",
        profile="read-only",
        prompt=text,
    )
    [row] = runs.rows("SELECT id FROM schedule ORDER BY id DESC LIMIT 1")
    assert f"Schedule {row['id']} added." in page.text
    return int(row["id"])


def ended_run(runs: Runs, schedule_id: int) -> sqlite3.Row:
    def found() -> sqlite3.Row | None:
        rows = runs.rows(
            "SELECT * FROM schedule_run WHERE schedule_id = ? AND ended_at IS NOT NULL",
            schedule_id,
        )
        return rows[0] if rows else None

    return poll(found, what=f"the check of schedule {schedule_id}")


def test_a_check_runs_once_read_only_and_reports_on_the_card(runs: Runs) -> None:
    stand = runs.stand
    runs.scenario({"turn": [{"text": "Looking."}, {"text": "All green: 14 of 14."}]})
    with stand.board() as board:
        schedule_id = add_check(runs, board, "Is the CI of PR 51 green?")
        stand.post_form(board, f"/schedules/{schedule_id}/run")
        done = ended_run(runs, schedule_id)
        assert (done["outcome"], done["reason"]) == ("ran", "All green: 14 of 14.")
        [run] = runs.rows("SELECT * FROM run WHERE kind = 'check'")
        assert (run["task_id"], run["status"], run["profile"]) == (None, "ended", "read-only")
        [report] = runs.rows(
            "SELECT r.* FROM report r JOIN task t ON t.id = r.task_id WHERE t.key = 'acme-shop-1'"
        )
        assert report["body_md"].splitlines()[0] == "inspected · S · All green: 14 of 14."
        assert done["report_id"] == report["id"]
        page = board.get("/schedules").text
        assert "All green: 14 of 14." in page and "report on acme-shop-1" in page
        # Run now keeps the planned time: the schedule is still due once, later.
        [schedule] = runs.rows("SELECT * FROM schedule WHERE id = ?", schedule_id)
        assert schedule["next_at"] is not None and schedule["times_run"] == 0
    [entry] = runs.trace("argv")
    argv = entry["argv"]
    assert "-p" in argv and "--resume" not in argv and "--no-session-persistence" in argv
    assert flag_values(argv, "--max-turns") == ["8"]
    assert flag_values(argv, "--permission-mode") == ["dontAsk"]
    assert flag_values(argv, "--allowedTools") == READ_ONLY
    taken = set(flag_values(argv, "--disallowedTools"))
    assert {*profiles.FIXED_DISALLOWED, *profiles.CHECK_DISALLOWED} <= taken
    assert entry["prompt"].startswith("relaybay check (schedule ")
    assert entry["prompt"].endswith("Is the CI of PR 51 green?")
    # No transcript: nothing to resume, nothing for the usage reader.
    [ended] = runs.trace("turn_end")
    projects = Path(stand.env["CLAUDE_CONFIG_DIR"]) / "projects"
    assert not list(projects.rglob(f"{ended['session_id']}.jsonl"))


def test_a_check_at_its_step_limit_is_reported_so(runs: Runs) -> None:
    stand = runs.stand
    runs.scenario({"turn": [{"text": f"Step {i}."} for i in range(12)]})
    with stand.board() as board:
        schedule_id = add_check(runs, board, "Read the whole log.")
        stand.post_form(board, f"/schedules/{schedule_id}/run")
        done = ended_run(runs, schedule_id)
        assert (done["outcome"], done["reason"]) == (
            "stopped",
            "check stopped at its step limit",
        )
        [report] = runs.rows("SELECT * FROM report")
        assert (
            report["body_md"].splitlines()[0] == "inspected · S · check stopped at its step limit"
        )
    [turn] = runs.rows("SELECT * FROM run_turn")
    assert (turn["result_subtype"], turn["num_turns"]) == ("error_max_turns", 8)
