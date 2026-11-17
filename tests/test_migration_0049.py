"""Migration 0049 (design 0025): a 0.11 database with open runs, questions, approvals and
schedules keeps every row; what a removed part left open is closed with its reason, and the
worktrees runs left on disk become one Inbox line."""

from importlib import resources
from pathlib import Path

import aiosqlite
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.db.connection import connect
from relaybay.security import csrf_token
from relaybay.service import LEFTOVER_WORKTREES_KEY, Service
from tests.conftest import BASE, run_in_app

NOW = "2026-11-16T10:00:00.000+00:00"
REASON = "runs inside the program were removed in 0.12"
TABLES = (
    "project",
    "task",
    "session",
    "run",
    "run_turn",
    "run_worktree",
    "question",
    "approval",
    "schedule",
    "schedule_run",
)

SEED = f"""
INSERT INTO project (id, slug, name, created_at, updated_at)
    VALUES (1, 'acme-shop', 'Acme Shop', '{NOW}', '{NOW}');
INSERT INTO task (id, project_id, key, title, stage, created_at, updated_at, answer_received_at)
    VALUES (1, 1, 'acme-shop-1', 'Checkout', 'dev', '{NOW}', '{NOW}', '{NOW}'),
           (2, 1, 'acme-shop-2', 'Search', 'dev', '{NOW}', '{NOW}', NULL);
INSERT INTO session (id, name, kind, last_seen_at, created_at)
    VALUES (1, 'run-1', 'run', '{NOW}', '{NOW}'),
           (2, 'checkout-dev', 'hook', '{NOW}', '{NOW}');
UPDATE session SET status = 'waiting', activity = 'Bash: git push' WHERE id = 1;
UPDATE task SET waiting_on = 'owner',
    next_step = 'Answer question 1 on the board: "Which provider?"' WHERE id = 1;
UPDATE task SET waiting_on = 'owner',
    next_step = 'Answer question 2 on the board: "Rename?"' WHERE id = 2;
INSERT INTO run (id, kind, task_id, project_id, session_id, cwd, model, effort, permission_mode,
                 allowed_tools, status, hooks_source, mcp_source, started_by, started_by_kind,
                 created_at, updated_at)
    VALUES (1, 'card', 1, 1, 1, '/code/acme-shop', 'opus', 'medium', 'default', 'Bash',
            'running', 'user', 'user', 'owner', 'owner', '{NOW}', '{NOW}');
INSERT INTO run (id, kind, task_id, project_id, cwd, model, effort, permission_mode,
                 allowed_tools, status, hooks_source, mcp_source, started_by, started_by_kind,
                 created_at, updated_at, ended_at, ended_by, status_reason)
    VALUES (2, 'card', 2, 1, '/code/acme-shop', 'opus', 'medium', 'default', 'Bash',
            'ended', 'user', 'user', 'owner', 'owner', '{NOW}', '{NOW}', '{NOW}', 'owner',
            'ended by the owner');
INSERT INTO run_turn (id, run_id, seq, prompt, sent_by, sent_by_kind, queued_at, started_at)
    VALUES (1, 1, 1, 'Build it', 'owner', 'owner', '{NOW}', '{NOW}'),
           (2, 1, 2, 'And the tests', 'owner', 'owner', '{NOW}', NULL);
INSERT INTO question (id, task_id, run_id, session_id, asked_by, kind, text, default_answer,
                      blocking, asked_at, status)
    VALUES (1, 1, 1, 1, 'run-1', 'question', 'Which provider?', 'Stripe', 1, '{NOW}', 'open'),
           (2, 2, NULL, 2, 'checkout-dev', 'question', 'Rename?', 'keep', 1, '{NOW}', 'open'),
           (3, 2, NULL, NULL, 'relaybay', 'question', 'Remove the worktree?', 'keep', 1,
            '{NOW}', 'open');
INSERT INTO run_worktree (id, run_id, repo_name, source_path, path, path_key, created_by_run,
                          created_at, removed_at, question_id)
    VALUES (1, 1, 'shop', '/code/shop', '/code/.relaybay-runs/acme-shop-1-run1/shop', 'k1', 1,
            '{NOW}', NULL, NULL),
           (2, 2, 'shop', '/code/shop', '/code/.relaybay-runs/acme-shop-2-run2/shop', 'k2', 1,
            '{NOW}', NULL, 3),
           (3, 2, 'api', '/code/api', '/code/.relaybay-runs/acme-shop-2-run2/api', 'k3', 1,
            '{NOW}', '{NOW}', NULL),
           (4, 1, 'docs', '/code/docs', '/code/docs', 'k4', 0, '{NOW}', NULL, NULL);
INSERT INTO approval (id, task_id, run_id, session_id, requested_by, requested_via, action,
                      branch, summary, requested_at, status, decision, decided_by, decided_at)
    VALUES (1, 1, 1, 1, 'run-1', 'denial', 'push', 'feat/a', 'x', '{NOW}', 'open', NULL, NULL,
            NULL),
           (2, 1, 1, 1, 'run-1', 'agent', 'push', 'feat/b', 'x', '{NOW}', 'approved', 'approved',
            'owner', '{NOW}'),
           (3, 2, NULL, 2, 'checkout-dev', 'agent', 'push', 'feat/c', 'x', '{NOW}', 'open', NULL,
            NULL, NULL);
INSERT INTO schedule (id, kind, repeat, at, timezone, prompt, created_by, created_by_kind,
                      next_at, created_at, updated_at, updated_by)
    VALUES (1, 'reminder', 'once', '{NOW}', 'UTC', 'Look at the stand', 'owner', 'owner',
            '{NOW}', '{NOW}', '{NOW}', 'owner');
INSERT INTO schedule (id, kind, repeat, time_of_day, timezone, prompt, created_by,
                      created_by_kind, next_at, created_at, updated_at, updated_by)
    VALUES (2, 'coordinator', 'daily', '09:00', 'UTC', 'Morning round', 'owner', 'owner',
            '{NOW}', '{NOW}', '{NOW}', 'owner');
INSERT INTO schedule_run (id, schedule_id, due_at, started_at)
    VALUES (1, 2, '{NOW}', '{NOW}');
"""


async def a_0_11_database(db: Path) -> None:
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0049")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 48;\nCOMMIT;")
        await conn.executescript(f"BEGIN;\n{SEED}\nCOMMIT;")
    finally:
        await conn.close()


async def counts(conn: aiosqlite.Connection) -> dict[str, int]:
    found = {}
    for table in TABLES:
        async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            found[table] = (await cur.fetchone())[0]
    return found


async def rows(conn: aiosqlite.Connection, sql: str) -> list[tuple]:
    async with conn.execute(sql) as cur:
        return [tuple(r) for r in await cur.fetchall()]


async def test_0049_closes_what_runs_left_open_and_keeps_every_row(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await a_0_11_database(db)
    conn = await connect(db)
    before = await counts(conn)
    await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        inbox = await svc.inbox(["owner"])
        assert inbox.leftover_worktrees == [
            "/code/.relaybay-runs/acme-shop-1-run1/shop",
            "/code/.relaybay-runs/acme-shop-2-run2/shop",
        ]
        assert [q.id for q in inbox.questions] == [2]  # the desktop session's stays open
        assert [a.id for a in inbox.approvals] == [3]
        closed = await svc.get_task("acme-shop-1")  # its question went with the run
        assert closed.waiting_on is None
        assert closed.next_step.startswith("Runs inside the program were removed in 0.12")
        kept = await svc.get_task("acme-shop-2")  # the desktop session's question stays
        assert kept.waiting_on == "owner"
        assert kept.next_step.startswith("Answer question 2")
        sessions = {r.session.name: r.session.status for r in await svc.list_sessions()}
        assert sessions["run-1"] == "ended"
        await svc.dismiss_leftover_worktrees()
        assert (await svc.inbox(["owner"])).leftover_worktrees == []
        assert [e.kind for e in await svc.activity(kind="inbox.worktrees_seen")] == [
            "inbox.worktrees_seen"
        ]
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        assert await counts(conn) == before
        assert await rows(conn, "SELECT id, status, status_reason, ended_by FROM run") == [
            (1, "ended", REASON, "relaybay"),
            (2, "ended", "ended by the owner", "owner"),
        ]
        assert await rows(conn, "SELECT id, outcome FROM run_turn ORDER BY id") == [
            (1, "lost"),
            (2, "cancelled"),
        ]
        assert await rows(conn, "SELECT id, status, withdrawn_by FROM question ORDER BY id") == [
            (1, "withdrawn", f"relaybay: {REASON}"),
            (2, "open", None),
            (3, "withdrawn", f"relaybay: {REASON}"),
        ]
        assert await rows(conn, "SELECT id, status, expired_reason FROM approval") == [
            (1, "expired", REASON),
            (2, "expired", REASON),
            (3, "open", None),
        ]
        assert await rows(conn, "SELECT id, enabled, next_at IS NULL FROM schedule") == [
            (1, 0, 1),
            (2, 0, 1),
        ]
        assert await rows(conn, "SELECT outcome, reason FROM schedule_run") == [
            ("stopped", "schedules were removed in 0.12")
        ]
        assert await rows(conn, "SELECT answer_received_at FROM task ORDER BY id") == [
            (None,),
            (None,),
        ]
        # The worktree rows stay as they were: relaybay touches nothing on disk.
        assert await rows(conn, "SELECT COUNT(*) FROM run_worktree WHERE removed_at IS NULL") == [
            (3,)
        ]
    finally:
        await conn.close()


async def test_0049_without_leftover_worktrees_adds_no_inbox_line(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite")
    await svc.start()
    try:
        inbox = await svc.inbox(["owner"])
        assert inbox.leftover_worktrees == [] and inbox.count == 0
        await svc.dismiss_leftover_worktrees()  # nothing to dismiss: no event
        assert await svc.activity(kind="inbox.worktrees_seen") == []
    finally:
        await svc.close()


def test_the_inbox_lists_the_worktrees_and_seen_takes_the_line_away(
    board: TestClient, token: str
) -> None:
    service: Service = board.app.state.service

    async def left() -> None:
        async with service._tx() as tx:
            await repo.set_app_setting(
                tx.conn, LEFTOVER_WORKTREES_KEY, '["/code/wt/a"]', tx.now, "relaybay"
            )

    run_in_app(board, left)
    page = board.get("/inbox").text
    assert "Worktrees left by runs" in page and "<code>/code/wt/a</code>" in page
    assert "git worktree remove" in page
    seen = board.post(
        "/inbox/worktrees/seen",
        data={"csrf_token": csrf_token(token)},
        headers={"Origin": BASE},
    )
    assert seen.status_code == 200
    assert "Worktrees left by runs" not in board.get("/inbox").text
    assert board.post("/inbox/worktrees/seen", data={}).status_code == 403
