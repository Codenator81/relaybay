from importlib import resources
from pathlib import Path

from relaybay.db.connection import connect, migrate
from relaybay.service import Service

NOW = "2026-01-01T00:00:00.000+00:00"


MIGRATIONS = (
    "0001_init.sql",
    "0002_task_description_progress.sql",
    "0003_tracker_and_session_alias.sql",
    "0004_next_step_freshness.sql",
    "0005_tracker_history.sql",
    "0006_project_rules.sql",
    "0007_hook_sessions.sql",
    "0008_comments.sql",
    "0009_environments.sql",
    "0010_policy.sql",
    "0011_review.sql",
    "0012_memory.sql",
    "0013_plan.sql",
    "0014_digests.sql",
    "0015_import.sql",
)
LATEST = 39


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
            assert (await cur.fetchone())[0] == LATEST
        assert await migrate(conn) == LATEST  # idempotent
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
            assert (await cur.fetchone())[0] == LATEST
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
        assert await migrate(conn) == LATEST
    finally:
        await conn.close()


async def test_upgrade_from_0004_backfills_tracker_history(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=4)
    later = "2026-01-05T00:00:00.000+00:00"
    conn = await connect(db)
    try:
        await conn.execute(
            "UPDATE task SET tracker_name = 'Linear', tracker_ref = 'ENG-1',"
            " tracker_url = 'https://tracker.example/ENG-1', tracker_logged = 1, updated_at = ?"
            " WHERE key = 'acme-shop-1'",
            (later,),
        )
        await conn.execute(
            "INSERT INTO task (project_id, key, title, stage, tracker_ref, created_at, updated_at)"
            " VALUES (1, 'acme-shop-2', 'Payment', 'dev', 'ENG-2', ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO task (project_id, key, title, stage, created_at, updated_at)"
            " VALUES (1, 'acme-shop-3', 'No tracker', 'plan', ?, ?)",
            (NOW, NOW),
        )
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        logged = (await svc.task_detail("acme-shop-1")).tracker_history
        assert [(e.tracker_name, e.tracker_ref, e.tracker_url) for e in logged] == [
            ("Linear", "ENG-1", "https://tracker.example/ENG-1")
        ]
        assert (logged[0].logged_at, logged[0].unlogged_at, logged[0].created_at) == (
            later,
            None,
            later,
        )
        open_entry = (await svc.task_detail("acme-shop-2")).tracker_history
        assert [(e.tracker_ref, e.logged_at) for e in open_entry] == [("ENG-2", None)]
        assert (await svc.task_detail("acme-shop-3")).tracker_history == []
        task = await svc.get_task("acme-shop-1")
        assert (task.tracker_ref, task.tracker_logged, task.version) == ("ENG-1", True, 1)
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        assert await migrate(conn) == LATEST  # idempotent: no second backfill
        async with conn.execute("SELECT COUNT(*) FROM tracker_entry") as cur:
            assert (await cur.fetchone())[0] == 2
    finally:
        await conn.close()


async def test_upgrade_from_0005_adds_project_rules(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=5)

    svc = Service(db)
    await svc.start()
    try:
        assert (await svc.rules("acme-shop")).hash == svc.global_rules.hash
        task = await svc.get_task("acme-shop-1")
        moved = await svc.set_stage_checked(task.key, "test")
        assert moved.task.stage == "test" and len(moved.warnings) == 1
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        assert await migrate(conn) == LATEST
        await conn.execute(
            "INSERT INTO project_rules (project_id, body, updated_at) VALUES (1, ?, ?)",
            ('{"stage_requirements": {"test": []}}', NOW),
        )
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        assert (await svc.rules("acme-shop")).requirements("test") == []
    finally:
        await svc.close()


async def test_upgrade_from_0007_adds_comments(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=7)
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO session (name, cc_session_id, last_seen_at, created_at)"
            " VALUES ('acme-shop', 'cc-1', ?, ?)",
            (NOW, NOW),
        )
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        task = await svc.get_task("acme-shop-1")
        assert (await svc.task_detail(task.key)).comments == []
        comment = await svc.add_owner_comment(task.key, "Please rename the button")
        assert (comment.author_kind, comment.delivered_at, comment.read_at) == ("owner", None, None)
        # A hook link made before 0008 was not made by the agent: no comment goes to it.
        rows = await svc.list_sessions()
        assert [(r.session.cc_session_id, r.session.cc_exact) for r in rows] == [("cc-1", False)]
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        assert await migrate(conn) == LATEST
    finally:
        await conn.close()


async def test_upgrade_from_0023_adds_backlog_priority_and_assignee(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0024")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 23;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 0, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO backlog_item (project_id, title, area, origin, status, link, created_at,"
            " created_by) VALUES (1, 'Badge', 'cart', 'owner', 'done', 'https://x.test/1', ?,"
            " 'owner')",
            (NOW,),
        )
        await conn.commit()
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        [kept] = await svc.list_backlog()
        assert (kept.title, kept.area, kept.status, kept.link) == (
            "Badge",
            "cart",
            "done",
            "https://x.test/1",
        )
        assert (kept.priority, kept.assignee) == (0, None)
        saved = await svc.set_backlog_queue(kept.id, 2, "qa")
        assert (saved.priority, saved.assignee) == (2, "qa")
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA index_list(backlog_item)") as cur:
            assert "backlog_item_queue" in {row[1] for row in await cur.fetchall()}
        assert await migrate(conn) == LATEST
    finally:
        await conn.close()


async def test_upgrade_from_0024_adds_the_brake_tables_and_keeps_usage(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0025")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 24;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO usage_file (path, created_at) VALUES ('/d/t.jsonl', ?)", (NOW,)
        )
        await conn.execute(
            "INSERT INTO usage_day (file_id, day, model, input, messages)"
            " VALUES (1, '2026-01-01', 'demo-model', 7, 1)"
        )
        await conn.commit()
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        assert (await svc.usage_report(days=0)).total.tokens == 7
        assert await svc.open_brakes() == []
        budget = await svc.set_budget("all", period="total", max_tokens=5)
        assert budget.action == "warn"
        await svc.limits_pass()
        [state] = await svc.budget_states()
        assert (state.used, state.level) == (7, "exceeded")
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("SELECT COUNT(*) FROM usage_task_day") as cur:
            assert (await cur.fetchone())[0] == 0  # no backfill
        assert await migrate(conn) == LATEST
    finally:
        await conn.close()


async def test_upgrade_from_0027_ranks_tasks_in_the_order_the_board_showed(
    tmp_path: Path,
) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0028")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 27;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 4, ?, ?)",
            (NOW, NOW),
        )
        # The old board order: the most recently updated first; another stage is its own column.
        for n, (stage, updated) in enumerate(
            [("dev", "2026-01-02"), ("dev", "2026-01-04"), ("dev", "2026-01-03"), ("plan", NOW)], 1
        ):
            await conn.execute(
                "INSERT INTO task (project_id, key, title, stage, created_at, updated_at)"
                " VALUES (1, ?, ?, ?, ?, ?)",
                (f"acme-shop-{n}", f"Task {n}", stage, NOW, updated),
            )
        await conn.commit()
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        tasks = await svc.list_tasks(project="acme-shop", by_rank=True)
        assert [(t.key, t.rank) for t in tasks if t.stage == "dev"] == [
            ("acme-shop-2", 1),
            ("acme-shop-3", 2),
            ("acme-shop-1", 3),
        ]
        assert [t.rank for t in tasks if t.stage == "plan"] == [1]
        created = await svc.create_task("acme-shop", "Payment", stage="dev")
        assert created.rank == 0  # on top of the column
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA index_list(task)") as cur:
            assert "task_board_order" in {row[1] for row in await cur.fetchall()}
        assert await migrate(conn) == LATEST
    finally:
        await conn.close()


async def test_upgrade_from_0029_adds_the_run_tables(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0030")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 29;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 1, ?, ?)",
            (NOW, NOW),
        )
        await conn.commit()
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        defaults = await svc.run_defaults("acme-shop")
        assert (defaults.model, defaults.effort, defaults.permission_mode) == (
            "opus",
            "medium",
            "auto",
        )
        assert defaults.allowed_tools == "mcp__relaybay" and not defaults.chrome
        assert await svc.run_login_any() is False  # no row: off
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        for table, wanted in (
            ("run", {"run_open_task", "run_claude_session", "run_project"}),
            ("run_turn", {"run_turn_active", "run_turn_queued"}),
            ("run_worktree", {"run_worktree_run", "run_worktree_path"}),
        ):
            async with conn.execute(f"PRAGMA index_list({table})") as cur:
                assert wanted <= {row[1] for row in await cur.fetchall()}
        async with conn.execute("SELECT COUNT(*) FROM project_run_defaults") as cur:
            assert (await cur.fetchone())[0] == 0  # no backfill
        assert await migrate(conn) == LATEST
    finally:
        await conn.close()
