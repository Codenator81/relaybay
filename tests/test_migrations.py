from importlib import resources
from pathlib import Path

from relaybay.db.connection import connect, migrate
from relaybay.service import Service

NOW = "2026-01-01T00:00:00.000+00:00"


MIGRATIONS = (
    "0001_init.sql",
    "0002_task_description_progress.sql",
    "0003_tracker_and_session_alias.sql",
)


async def make_old_db(path: Path, version: int = 1) -> None:
    """A database as an earlier release left it (migrations up to `version`), with one task."""
    folder = resources.files("relaybay.db").joinpath("migrations")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in MIGRATIONS[:version])
    conn = await connect(path)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 1, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO task (project_id, key, title, stage, next_step, created_at, updated_at)"
            " VALUES (1, 'acme-shop-1', 'Checkout page', 'dev', 'Review the cart', ?, ?)",
            (NOW, NOW),
        )
    finally:
        await conn.close()


async def test_upgrade_from_0001_keeps_data(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db)

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
            assert (await cur.fetchone())[0] == 4
        assert await migrate(conn) == 4  # idempotent
    finally:
        await conn.close()


async def test_upgrade_from_0002_adds_tracker_fields_and_aliases(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=2)

    svc = Service(db)
    await svc.start()
    try:
        task = await svc.get_task("acme-shop-1")
        assert (task.title, task.tracker_ref, task.tracker_logged) == ("Checkout page", None, False)
        updated = await svc.update_task(task.key, {"tracker_ref": "PROJ-7", "tracker_logged": True})
        assert (updated.tracker_ref, updated.tracker_logged, updated.version) == (
            "PROJ-7",
            True,
            2,
        )
        desktop = "local_00000000-0000-0000-0000-000000000001"
        await svc.bind_session(task.key, desktop)
        _, kept, _ = await svc.bind_session(task.key, "checkout-dev")
        assert kept is not None and kept.external_id == desktop
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 4
    finally:
        await conn.close()


async def test_upgrade_from_0003_backfills_next_step_freshness(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=3)

    svc = Service(db)
    await svc.start()
    try:
        task = await svc.get_task("acme-shop-1")
        assert (task.next_step, task.next_step_at, task.stage_changed_at) == (
            "Review the cart",
            NOW,
            NOW,
        )
        assert not task.next_step_stale  # an upgrade alone marks nothing stale
        moved = await svc.set_stage(task.key, "test")
        assert moved.stage_changed_at > NOW and moved.next_step_at == NOW
        assert moved.next_step_stale
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        assert await migrate(conn) == 4
    finally:
        await conn.close()
