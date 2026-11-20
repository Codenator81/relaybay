"""Migration 0058: the run tables go; questions and approvals keep every row and column, their
run ids stay as plain numbers, and both tables take writes again with foreign keys on."""

from pathlib import Path

from relaybay.db.connection import connect, migrate
from tests.test_migration_0049 import a_0_11_database, rows

DROPPED = {
    "run",
    "run_turn",
    "run_event",
    "run_worktree",
    "run_profile",
    "project_run_defaults",
    "coordinator_wake",
    "schedule_run",
}
NOW = "2026-11-20T10:00:00.000+00:00"


async def tables(conn) -> set[str]:  # type: ignore[no-untyped-def]
    return {r[0] for r in await rows(conn, "SELECT name FROM sqlite_master WHERE type = 'table'")}


async def test_0058_drops_the_run_tables_and_keeps_questions_and_approvals(
    tmp_path: Path,
) -> None:
    db = tmp_path / "relaybay.sqlite"
    await a_0_11_database(db)
    conn = await connect(db)
    try:
        assert await migrate(conn, upto=57) == 57
        assert await tables(conn) >= DROPPED
        questions = await rows(conn, "SELECT * FROM question ORDER BY id")
        approvals = await rows(conn, "SELECT * FROM approval ORDER BY id")
        indexes = await rows(
            conn,
            "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
            " AND tbl_name IN ('question', 'approval') AND sql IS NOT NULL ORDER BY name",
        )
        assert any(r[2] is not None for r in questions)  # the seed has questions of a run

        assert await migrate(conn) == 58
        left = await tables(conn)
        assert not DROPPED & left
        assert {"schedule", "question", "approval", "token_budget", "app_setting"} <= left
        assert await rows(conn, "SELECT * FROM question ORDER BY id") == questions
        assert await rows(conn, "SELECT * FROM approval ORDER BY id") == approvals
        assert (
            await rows(
                conn,
                "SELECT name, sql FROM sqlite_master WHERE type = 'index'"
                " AND tbl_name IN ('question', 'approval') AND sql IS NOT NULL ORDER BY name",
            )
            == indexes
        )
        assert await rows(conn, "PRAGMA foreign_key_check") == []
        for table in ("question", "approval"):
            keys = {r[2] for r in await rows(conn, f"PRAGMA foreign_key_list({table})")}
            assert not keys & DROPPED

        # Writes with foreign keys on: SQLite refuses them while a key names a missing table.
        await conn.execute(
            "INSERT INTO question (task_id, asked_by, kind, text, default_answer, blocking,"
            " asked_at, status) VALUES (2, 'checkout-dev', 'question', 'Ship?', 'yes', 1, ?,"
            " 'open')",
            (NOW,),
        )
        await conn.execute("UPDATE approval SET told_at = ? WHERE id = 3", (NOW,))
        await conn.execute("DELETE FROM task WHERE id = 1")  # cascades into both tables
        await conn.commit()
        assert await migrate(conn) == 58  # idempotent
    finally:
        await conn.close()


async def test_0058_keeps_a_dangling_key_the_database_already_had(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await a_0_11_database(db)
    conn = await connect(db)
    try:
        assert await migrate(conn, upto=57) == 57
        await conn.execute("PRAGMA foreign_keys=OFF")
        await conn.execute("UPDATE question SET session_id = 999 WHERE id = 2")
        await conn.commit()
        await conn.execute("PRAGMA foreign_keys=ON")
        # The rebuild renumbers question's keys; the old orphan is still not this migration's.
        assert await migrate(conn) == 58
        assert await rows(conn, "SELECT session_id FROM question WHERE id = 2") == [(999,)]
    finally:
        await conn.close()
