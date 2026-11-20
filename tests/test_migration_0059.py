"""Migration 0059: the rest of what 0.12 left unread goes (schedules, usage, budgets, the brake,
session messages, intake and their columns); task and backlog_item keep every other column,
row, CHECK and index, and the board still works on the result."""

from pathlib import Path

from relaybay.db.connection import connect, migrate
from relaybay.service import Service
from tests.test_migration_0049 import a_0_11_database, rows

DROPPED = {
    "schedule",
    "usage_file",
    "usage_day",
    "usage_message",
    "usage_task_day",
    "token_budget",
    "budget_mark",
    "brake",
    "session_message",
    "intake_source",
    "intake_message",
    "intake_thread",
}
COLUMNS = {
    "project": {"autonomy", "run_retention"},
    "session": {"transcript_path"},
    "task": {"autonomy", "answer_received_at", "answer_message_id"},
}
NOW = "2026-11-20T10:00:00.000+00:00"
SEED = f"""
INSERT INTO intake_source (id, slug, name, created_at) VALUES (1, 'chat', 'Chat', '{NOW}');
INSERT INTO intake_message (id, source_id, external_id, received_at, created_at)
    VALUES (1, 1, 'm1', '{NOW}', '{NOW}');
UPDATE task SET answer_message_id = 1, autonomy = 'on' WHERE id = 1;
UPDATE project SET task_seq = 2 WHERE id = 1;
UPDATE session SET transcript_path = '/t/a.jsonl' WHERE id = 1;
INSERT INTO usage_file (id, path, created_at) VALUES (1, '/t/a.jsonl', '{NOW}');
INSERT INTO backlog_item (id, project_id, title, origin, intake_message_id, created_at,
                          created_by, duplicate_of)
    VALUES (1, 1, 'From the chat', 'intake', 1, '{NOW}', 'owner', NULL),
           (2, 1, 'Same', 'owner', NULL, '{NOW}', 'owner', 1);
"""


async def columns(conn, table: str) -> list[str]:  # type: ignore[no-untyped-def]
    return [r[1] for r in await rows(conn, f"PRAGMA table_xinfo({table})")]


async def kept(conn, table: str) -> list[tuple]:  # type: ignore[no-untyped-def]
    names = [c for c in await columns(conn, table) if c not in COLUMNS.get(table, set())]
    return await rows(conn, f"SELECT {', '.join(names)} FROM {table} ORDER BY id")


async def indexes(conn) -> list[tuple]:  # type: ignore[no-untyped-def]
    return await rows(
        conn,
        "SELECT tbl_name, name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
        " AND tbl_name IN ('task', 'backlog_item') ORDER BY name",
    )


async def test_0059_drops_the_unread_tables_and_columns(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await a_0_11_database(db)
    conn = await connect(db)
    try:
        assert await migrate(conn, upto=58) == 58
        await conn.executescript(f"BEGIN;\n{SEED}\nCOMMIT;")
        before = {t: await kept(conn, t) for t in ("task", "backlog_item", "project", "session")}
        task_columns = await columns(conn, "task")
        before_indexes = await indexes(conn)

        assert await migrate(conn) == 59
        tables = {r[0] for r in await rows(conn, "SELECT name FROM sqlite_master")}
        assert not DROPPED & tables
        for table, gone in COLUMNS.items():
            assert not gone & set(await columns(conn, table))
        assert await columns(conn, "task") == [c for c in task_columns if c not in COLUMNS["task"]]
        assert {t: await kept(conn, t) for t in before} == before  # intake_message_id kept
        assert await indexes(conn) == before_indexes
        assert await rows(conn, "PRAGMA foreign_key_check") == []
        missing = [
            (t, fk[2])
            for t in tables
            for fk in await rows(conn, f"PRAGMA foreign_key_list({t})")
            if fk[2] not in tables
        ]
        assert missing == []
        await conn.execute("DELETE FROM backlog_item WHERE id = 1")  # the duplicate is let go
        assert await rows(conn, "SELECT duplicate_of FROM backlog_item WHERE id = 2") == [(None,)]
        await conn.commit()
        assert await migrate(conn) == 59
    finally:
        await conn.close()

    # The service on the result: a new card and a stage move.
    svc = Service(db)
    await svc.start()
    try:
        task = await svc.create_task("acme-shop", "After 0059", stage="dev")
        await svc.set_stage(task.key, "done")
        assert (await svc.get_task(task.key)).stage == "done"
    finally:
        await svc.close()
