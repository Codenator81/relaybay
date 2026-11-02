import asyncio
from pathlib import Path

import pytest

from relaybay import cli
from relaybay.demo import DEMO_DESKTOP_SESSION, DemoError, create_demo
from relaybay.service import Service


async def _board(db_path: Path) -> dict:
    service = Service(db_path)
    await service.start()
    try:
        tasks = await service.list_tasks(include_done=True)
        return {
            "projects": {p.project.slug for p in await service.list_projects()},
            "tasks": tasks,
            "details": [await service.task_detail(t.key) for t in tasks],
            "snapshot": await service.snapshot("acme-shop"),
        }
    finally:
        await service.close()


def test_demo_seeds_every_stage(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    result = create_demo(db)
    assert result.backup is None and result.projects == 2
    board = asyncio.run(_board(db))
    assert board["projects"] == {"acme-shop", "demo-api"}
    tasks = board["tasks"]
    assert len(tasks) == result.tasks
    assert {t.stage.value for t in tasks} == {"plan", "dev", "review", "test", "live", "done"}
    assert any(t.tracker_ref and t.tracker_logged for t in tasks)
    assert any(t.next_step_stale for t in tasks)
    assert any(t.description for t in tasks) and any(t.progress for t in tasks)
    assert sum(len(d.reports) for d in board["details"]) >= 4
    bound = [t for t in tasks if t.session_external_id == DEMO_DESKTOP_SESSION]
    assert len(bound) == 1
    assert bound[0].session_status == "working" and bound[0].session_activity == "Edit"
    assert board["snapshot"].decisions
    review = next(d for d in board["details"] if d.task.stage == "review")
    assert len(review.review.steps) == 3 and len(review.review.images()) == 2
    assert all(a.mime == "image/png" for a in review.review.images())


def test_demo_refuses_a_board_unless_forced(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    create_demo(db)
    with pytest.raises(DemoError, match="already holds a board"):
        create_demo(db)
    result = create_demo(db, force=True)
    assert result.backup is not None and result.backup.is_file()
    assert result.backup.name.startswith("relaybay.sqlite.before-demo-")
    assert len(asyncio.run(_board(db))["tasks"]) == result.tasks  # a fresh board, not doubled


def test_demo_uses_an_empty_database(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"

    async def migrate_only() -> None:
        service = Service(db)
        await service.start()
        await service.close()

    asyncio.run(migrate_only())
    assert create_demo(db).backup is None


def test_cli_demo_needs_a_home(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["demo"])
    assert "--home" in capsys.readouterr().err
    assert cli.main(["demo", "--home", str(tmp_path)]) == 0
    assert "2 projects" in capsys.readouterr().out
    assert cli.main(["demo", "--home", str(tmp_path)]) == 1
    assert "--force" in capsys.readouterr().err
