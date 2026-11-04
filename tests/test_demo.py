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
            "spec": await service.document_history("acme-shop", "spec"),
            "decisions": (
                await service.list_decisions("acme-shop", include_superseded=True, limit=50)
            )[1],
            "notes": await service.list_notes("acme-shop"),
            "blocks": await service.list_blocks("acme-shop"),
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
    _, versions = board["spec"]
    assert [v.version for v in versions] == [2, 1] and versions[0].section == "gift-cards"
    assert any(d.superseded_by for d in board["decisions"])
    assert {n.scope for n in board["notes"]} == {"global", "project"}
    assert [b.label for b in board["blocks"]] == ["conventions"]
    planned = next(d for d in board["details"] if d.plan.steps)
    assert planned.task.size == "M" and planned.plan.status == "accepted"
    assert [s.status for s in planned.plan.steps] == ["done", "done", "in_progress", "pending"]
    assert planned.plan.steps[0].link and planned.plan.deviations == 1
    small = next(d for d in board["details"] if d.todo)
    assert small.task.size == "S" and not small.plan.steps
    assert [i.status for i in small.todo] == ["completed", "in_progress", "pending"]


def test_demo_has_a_current_pulse_and_a_stale_one(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    create_demo(db)

    async def read() -> tuple[bool, bool, str]:
        service = Service(db)
        await service.start()
        try:
            acme = await service.get_project("acme-shop")
            api = await service.get_project("demo-api")
            assert acme.pulse and api.pulse
            digest = await service.portfolio_digest()
            return service.pulse_stale(acme), service.pulse_stale(api), digest
        finally:
            await service.close()

    acme_stale, api_stale, digest = asyncio.run(read())
    assert not acme_stale and api_stale
    assert "21d ago, stale" in digest and "Checkout is in dev" in digest


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
