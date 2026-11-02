from importlib import resources
from pathlib import Path

from relaybay.db.connection import connect, migrate
from relaybay.service import Service

NOW = "2026-01-01T00:00:00.000+00:00"


async def make_0001_db(path: Path) -> None:
    """A database as stage 1 left it: only migration 0001 applied, with one task in it."""
    sql = resources.files("relaybay.db").joinpath("migrations/0001_init.sql")
    conn = await connect(path)
    try:
        await conn.executescript(
            f"BEGIN;\n{sql.read_text(encoding='utf-8')}\nPRAGMA user_version = 1;\nCOMMIT;"
        )
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 1, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO task (project_id, key, title, stage, created_at, updated_at)"
            " VALUES (1, 'acme-shop-1', 'Checkout page', 'dev', ?, ?)",
            (NOW, NOW),
        )
    finally:
        await conn.close()


async def test_upgrade_from_0001_keeps_data(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_0001_db(db)

    svc = Service(db)
    await svc.start()
    try:
        task = await svc.get_task("acme-shop-1")
        assert (task.title, task.description, task.progress) == ("Checkout page", None, None)
        updated = await svc.update_task(task.key, {"description": "Why", "progress": "Cart"})
        assert (updated.description, updated.progress, updated.version) == ("Why", "Cart", 2)
        second = await svc.create_task("acme-shop", "Payment", description="Retry failed cards")
        assert second.key == "acme-shop-2" and second.description == "Retry failed cards"
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 2
        assert await migrate(conn) == 2  # idempotent
    finally:
        await conn.close()
