"""Areas (design 0024): one per task or none, from a project list that grows from use; the board
chip, filter and settings; MCP; the backlog's "To plan"; export and import; migration 0048."""

from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from relaybay.db.connection import connect
from relaybay.domain.areas import AREA_COLORS, AREAS_MAX
from relaybay.domain.errors import ConflictError, ValidationError
from relaybay.domain.markdown_layout import render_registry
from relaybay.domain.models import Task
from relaybay.domain.stages import Stage
from relaybay.markdown_export import gather
from relaybay.markdown_import import apply_plan, make_plan
from relaybay.mcp_server.server import build_mcp
from relaybay.security import csrf_token
from relaybay.service import Service, collect_notes
from tests.conftest import BASE, run_in_app
from tests.test_migrations import LATEST, NOW

FORM = {"Origin": BASE}


async def test_names_match_without_case_and_outer_spaces(service: Service) -> None:
    await service.upsert_project("acme-shop")
    first = await service.create_task("acme-shop", "Checkout", area="Web shop")
    second = await service.create_task("acme-shop", "Cart", area="  web   SHOP ")
    assert first.area == second.area == "Web shop"
    assert first.area_id == second.area_id
    assert first.area_color == AREA_COLORS[0]
    _, areas = await service.project_areas("acme-shop")
    assert [(a.name, a.tasks) for a in areas] == [("Web shop", 2)]

    third = await service.update_task(second.key, {"area": "API"})
    assert (third.area, third.area_color) == ("API", AREA_COLORS[1])
    same = await service.update_task(third.key, {"area": "api"})
    assert same.version == third.version  # the same area: no change
    cleared = await service.update_task(third.key, {"area": "-"})
    assert cleared.area is None and cleared.area_id is None
    assert (await service.update_task(cleared.key, {"area": "API"})).area == "API"
    assert (await service.update_task(cleared.key, {"area": ""})).area is None
    changes = [
        e.payload["changes"]
        for e in await service.events_since(0, limit=500)
        if e.kind == "task.updated" and e.task_id == second.id
    ]
    assert changes == [{"area": "API"}, {"area": None}, {"area": "API"}, {"area": None}]
    added = [
        e.payload["changes"]
        for e in await service.events_since(0, limit=500)
        if e.kind == "project.updated" and "areas" in e.payload["changes"]
    ]
    assert added == [{"areas": {"added": ["Web shop"]}}, {"areas": {"added": ["API"]}}]


async def test_limits_and_validation(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout")
    with pytest.raises(ValidationError, match="longer than 32"):
        await service.update_task(task.key, {"area": "x" * 33})
    for n in range(AREAS_MAX):
        await service.add_area("acme-shop", f"area {n}")
    with pytest.raises(ValidationError, match="the limit") as caught:
        await service.update_task(task.key, {"area": "one more"})
    assert "area 0, area 1" in str(caught.value)
    with pytest.raises(ValidationError, match="the limit"):
        await service.create_task("acme-shop", "Cart", area="one more")
    assert len((await service.project_areas("acme-shop"))[1]) == AREAS_MAX
    # An existing one still works at the limit.
    assert (await service.update_task(task.key, {"area": "AREA 3"})).area == "area 3"
    with pytest.raises(ValidationError, match="already has"):
        await service.add_area("acme-shop", "Area 3")


async def test_an_agents_new_area_is_named_in_the_notes(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.add_area("acme-shop", "web shop")
    with collect_notes() as notes:
        await service.create_task("acme-shop", "Checkout", area="Web-shop")
    assert notes == [
        "Area 'Web-shop' was new: added to acme-shop. Its areas now: web shop, Web-shop."
    ]


async def test_rename_recolour_reorder_merge_and_delete(service: Service) -> None:
    await service.upsert_project("acme-shop")
    web = await service.create_task("acme-shop", "Checkout", area="web")
    api = await service.create_task("acme-shop", "Orders", area="api")
    _, (a_web, a_api) = await service.project_areas("acme-shop")

    renamed = await service.save_area("acme-shop", a_web.id, name="Web shop", color="rose")
    assert (renamed.name, renamed.color) == ("Web shop", "rose")
    assert (await service.get_task(web.key)).area_color == "rose"
    with pytest.raises(ValidationError, match="colour"):
        await service.save_area("acme-shop", a_web.id, name="Web shop", color="#fff")

    await service.move_area("acme-shop", a_api.id, -1)
    _, ordered = await service.project_areas("acme-shop")
    assert [a.name for a in ordered] == ["api", "Web shop"]

    # Renaming into another area's name merges the two.
    into = await service.save_area("acme-shop", a_api.id, name="WEB SHOP", color="blue")
    assert into.id == a_web.id
    assert (await service.get_task(api.key)).area == "Web shop"
    _, left = await service.project_areas("acme-shop")
    assert [(a.name, a.tasks) for a in left] == [("Web shop", 2)]

    before = await service.get_task(web.key)
    assert await service.delete_area("acme-shop", a_web.id) == 2
    after = await service.get_task(web.key)
    assert after.area is None and after.version == before.version + 1
    assert (await service.get_task(api.key)).area is None
    assert (await service.project_areas("acme-shop"))[1] == []
    events = await service.events_since(0, limit=500)
    cleared = [
        e
        for e in events
        if e.kind == "task.updated"
        and e.task_id == web.id
        and e.payload["changes"] == {"area": None}
    ]
    assert cleared, "the card's history keeps that its area was cleared"


async def test_a_rename_refuses_a_form_opened_before_it(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", area="web")
    _, (web,) = await service.project_areas("acme-shop")
    await service.save_area("acme-shop", web.id, name="web", color="rose")
    assert (await service.get_task(task.key)).version == task.version  # a colour only
    await service.save_area("acme-shop", web.id, name="Web shop", color="rose")
    renamed = await service.get_task(task.key)
    assert renamed.version == task.version + 1 and renamed.area == "Web shop"
    with pytest.raises(ConflictError):
        await service.update_task(task.key, {"area": "web"}, expected_version=task.version)
    assert [a.name for a in (await service.project_areas("acme-shop"))[1]] == ["Web shop"]


async def test_saved_filters_follow_rename_merge_and_delete(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.create_task("acme-shop", "Checkout", area="web")
    await service.create_task("acme-shop", "Orders", area="api")
    _, (web, api) = await service.project_areas("acme-shop")
    await service.save_filter("acme-shop", "Web", area="WEB", stage="dev")
    await service.save_filter("acme-shop", "Api", area="api")

    async def areas() -> dict[str, str | None]:
        return {f.name: f.area for f in await service.saved_filters("acme-shop")}

    await service.save_area("acme-shop", web.id, name="Web shop", color="blue")
    assert await areas() == {"Web": "Web shop", "Api": "api"}
    await service.save_area("acme-shop", api.id, name="web SHOP", color="blue")  # a merge
    assert await areas() == {"Web": "Web shop", "Api": "Web shop"}
    await service.delete_area("acme-shop", web.id)
    assert await areas() == {"Web": None, "Api": None}


async def test_list_filter_by_area(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    await service.create_task("acme-shop", "Checkout", area="web")
    await service.create_task("acme-shop", "Orders", area="api")
    await service.create_task("demo-api", "Tokens", area="API")
    titles = {t.title for t in await service.list_tasks(project="acme-shop", area="API")}
    assert titles == {"Orders"}
    assert {t.title for t in await service.list_tasks(area="api")} == {"Orders", "Tokens"}
    assert await service.list_tasks(project="acme-shop", area="nothing") == []


async def test_backlog_to_plan_carries_the_area(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.add_area("acme-shop", "Web shop")
    known = await service.add_backlog_item("acme-shop", "Coupon field", area="web shop")
    new = await service.add_backlog_item("acme-shop", "Push tokens", area="Mobile app")
    long = await service.add_backlog_item("acme-shop", "Long", area="a" * 40)
    assert await service.backlog_areas("acme-shop") == ["Web shop", "a" * 40, "mobile app"]
    planned = [
        await service.decide_backlog(item.id, "plan", actor="owner") for item in (known, new, long)
    ]
    tasks = [await service.get_task(p.task_key or "") for p in planned]
    assert [t.area for t in tasks] == ["Web shop", "mobile app", None]
    _, areas = await service.project_areas("acme-shop")
    assert [a.name for a in areas] == ["Web shop", "mobile app"]


async def test_migration_from_0047_keeps_every_task(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0048")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 47;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 2, ?, ?)",
            (NOW, NOW),
        )
        for n, stage in ((1, "dev"), (2, "done")):
            await conn.execute(
                "INSERT INTO task (project_id, key, title, stage, next_step, created_at,"
                " updated_at) VALUES (1, ?, ?, ?, 'Next', ?, ?)",
                (f"acme-shop-{n}", f"Task {n}", stage, NOW, NOW),
            )
        await conn.execute(
            "INSERT INTO saved_filter (project_id, name, stage, created_at)"
            " VALUES (1, 'In dev', 'dev', ?)",
            (NOW,),
        )
        await conn.commit()
        async with conn.execute("SELECT * FROM task ORDER BY id") as cur:
            before = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        task = await svc.update_task("acme-shop-1", {"area": "web"})
        assert task.area == "web"
        (saved,) = await svc.saved_filters("acme-shop")
        assert saved.query == {"stage": "dev"}
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == LATEST
        async with conn.execute("SELECT * FROM task ORDER BY id") as cur:
            after = [dict(r) for r in await cur.fetchall()]
        assert len(after) == len(before)
        added = (  # columns of later migrations
            "area_id",
            "archived_from",
            "deploy_notes",
            "deploy_done",
            "cancelled",
            "cancelled_at",
            "inspect_only",
        )
        untouched = {k: v for k, v in after[1].items() if k not in added}
        dropped = ("autonomy", "answer_received_at", "answer_message_id")  # by 0059
        kept = {k: v for k, v in before[1].items() if k not in dropped}
        assert untouched == kept and after[1]["area_id"] is None
        async with conn.execute("PRAGMA index_list(project_area)") as cur:
            assert "project_area_name" in {row[1] for row in await cur.fetchall()}
    finally:
        await conn.close()


# --- board ------------------------------------------------------------------------------------


def test_board_form_chip_filter_and_saved_filter(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    web = run_in_app(board, service.create_task, "acme-shop", "Checkout", area="web shop")
    api = run_in_app(board, service.create_task, "acme-shop", "Orders", stage="dev")
    csrf = {"csrf_token": csrf_token(token)}

    form = board.get(f"/p/acme-shop/t/{api.key}/form").text
    assert 'name="area"' in form and '<option value="web shop">' in form
    saved = board.post(
        f"/p/acme-shop/t/{api.key}",
        data={**csrf, "version": str(api.version), "area": "API"},
        headers=FORM,
    )
    assert saved.status_code == 200 and "Saved." in saved.text
    assert run_in_app(board, service.get_task, api.key).area == "API"

    card = board.get(f"/p/acme-shop/t/{web.key}/card").text
    assert '<span class="area-chip area-blue" title="Area: web shop">web shop</span>' in card
    assert "area-chip" in board.get(f"/p/acme-shop/t/{web.key}/view").text
    assert "area-chip area-green" in board.get(f"/p/acme-shop/t/{api.key}/dialog").text
    head = board.get(f"/p/acme-shop/t/{api.key}/area").text
    assert head.startswith('<span class="dialog-area"') and ">API</span>" in head
    assert "area-chip" in board.get(f"/t/{web.key}").text

    page = board.get("/p/acme-shop").text
    assert "Any area" in page and '<option value="API">API</option>' in page
    filtered = board.get("/p/acme-shop", params={"area": "api"}).text
    assert f'id="card-{api.key}"' in filtered and f'id="card-{web.key}"' not in filtered
    column = board.get("/p/acme-shop/col/plan", params={"area": "api"}).text
    assert f'id="card-{web.key}"' not in column

    filed = board.post(
        "/p/acme-shop/filters",
        data={**csrf, "name": "API work", "area": "API"},
        headers=FORM,
        follow_redirects=False,
    )
    assert filed.status_code == 303 and filed.headers["location"] == "/p/acme-shop?area=API"
    (kept,) = run_in_app(board, service.saved_filters, "acme-shop")
    assert kept.query == {"area": "API"} and kept.qs == "area=API"
    assert 'href="/p/acme-shop?area=API"' in board.get("/p/acme-shop").text


def test_settings_page_manages_areas(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    task = run_in_app(board, service.create_task, "acme-shop", "Checkout", area="web")
    csrf = {"csrf_token": csrf_token(token)}
    bearer = board.post(
        "/p/acme-shop/areas",
        data={**csrf, "name": "api"},
        headers={**FORM, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403
    added = board.post(
        "/p/acme-shop/areas", data={**csrf, "name": "api"}, headers=FORM, follow_redirects=False
    )
    assert added.status_code == 303 and added.headers["location"].endswith("#areas")
    _, (web, api) = run_in_app(board, service.project_areas, "acme-shop")
    page = board.get("/p/acme-shop/settings").text
    assert 'id="areas"' in page and "1 card" in page and 'value="rose"' in page

    recoloured = board.post(
        f"/p/acme-shop/areas/{web.id}", data={**csrf, "name": "Web", "color": "rose"}, headers=FORM
    )
    assert recoloured.status_code == 200
    assert run_in_app(board, service.get_task, task.key).area_color == "rose"
    bad = board.post(
        f"/p/acme-shop/areas/{web.id}", data={**csrf, "name": "", "color": "rose"}, headers=FORM
    )
    assert bad.status_code == 400 and "needs a name" in bad.text
    up = board.post(f"/p/acme-shop/areas/{api.id}/up", data=csrf, headers=FORM)
    assert up.status_code == 200
    _, ordered = run_in_app(board, service.project_areas, "acme-shop")
    assert [a.name for a in ordered] == ["api", "Web"]
    gone = board.post(f"/p/acme-shop/areas/{web.id}/delete", data=csrf, headers=FORM)
    assert gone.status_code == 200
    assert run_in_app(board, service.get_task, task.key).area is None
    again = board.post(f"/p/acme-shop/areas/{web.id}/delete", data=csrf, headers=FORM)
    assert again.status_code == 404


def test_inbox_and_switcher_show_the_area(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(
        board, service.create_task, "acme-shop", "Checkout", area="web shop", waiting_on="owner"
    )
    item = board.get("/switcher", params={"q": "web shop"}).json()["items"][0]
    assert (item["title"], item["area"], item["area_color"]) == ("Checkout", "web shop", "blue")
    assert "area-chip area-blue" in board.get("/inbox").text


def test_top_bar_has_a_home_icon_with_the_sidebar(board: TestClient) -> None:
    page = board.get("/").text
    assert '<a class="brand brand-home" href="/" aria-label="Home"' in page
    assert ">relaybay</a>" not in page
    login = TestClient(board.app, base_url=BASE).get("/login").text
    assert '<a class="brand" href="/">relaybay</a>' in login and "brand-home" not in login


# --- MCP --------------------------------------------------------------------------------------


async def test_mcp_create_update_patch_list_and_snapshot(service: Service) -> None:
    mcp = build_mcp(service)

    async def call(tool: str, **arguments: Any) -> str:
        result = await mcp.call_tool(tool, arguments)
        return "\n".join(c.text for c in result.content)

    await service.upsert_project("acme-shop")
    await service.add_area("acme-shop", "web shop")
    created = await call("task_create", project="acme-shop", title="Checkout", area="Web Shop")
    assert "was new" not in created
    added = await call("task_create", project="acme-shop", title="Orders", area="api")
    assert "Area 'api' was new: added to acme-shop. Its areas now: web shop, api." in added
    assert "- area: web shop" in await call("task_get", task="acme-shop-1")

    await call("task_update", task="acme-shop-1", area="api")
    assert (await service.get_task("acme-shop-1")).area == "api"
    await call("task_update", task="acme-shop-1", area="-")
    assert (await service.get_task("acme-shop-1")).area is None
    patched = await call("task_update", task="acme-shop-1", fields={"area": "mobile app"})
    assert "Area 'mobile app' was new" in patched
    await call("task_update", task="acme-shop-2", fields={"area": ""})
    assert (await service.get_task("acme-shop-2")).area is None

    listed = await call("task_list", project="acme-shop", area="MOBILE APP")
    assert "acme-shop-1" in listed and "acme-shop-2" not in listed
    assert "area: mobile app" in listed
    snapshot = await call("project_snapshot", project="acme-shop")
    assert "Areas (tasks carrying each" in snapshot
    assert "web shop (0), api (0), mobile app (1)" in snapshot
    context = await call("task_context", task="acme-shop-1")
    assert "Area: mobile app" in context
    digest = await call("project_digest", project="acme-shop")
    assert "area: mobile app" in digest
    schema = str(await mcp.read_resource("relaybay://schema/task"))
    assert "`area`" in schema and "Up to 32 characters" in schema
    with pytest.raises(ToolError, match="longer than 32"):
        await mcp.call_tool("task_update", {"task": "acme-shop-1", "area": "x" * 40})


# --- export and import ------------------------------------------------------------------------


async def test_export_and_import_carry_the_area_by_name(tmp_path: Path) -> None:
    source = Service(tmp_path / "a.sqlite")
    await source.start()
    try:
        await source.upsert_project("acme-shop")
        await source.create_task("acme-shop", "Checkout", area="Web shop")
        await source.create_task("acme-shop", "Docs")
        wanted, problems = await gather(source)
    finally:
        await source.close()
    assert not problems
    registry = wanted["TASKS.md"].text
    assert "| project | ref | task | area | stage |" in registry
    assert "| acme-shop | acme-shop-1 | Checkout | Web shop | plan |" in registry
    assert "| acme-shop | acme-shop-2 | Docs |  | plan |" in registry
    folder = tmp_path / "out"
    for rel, item in wanted.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(item.text, encoding="utf-8")

    target = Service(tmp_path / "b.sqlite")
    await target.start()
    try:
        await target.upsert_project("acme-shop")
        await target.add_area("acme-shop", "web shop")
        plan = await make_plan(target, folder)
        assert not plan.failed, [(i.item, i.detail) for i in plan.items if i.action == "error"]
        await apply_plan(target, plan)
        tasks = {t.title: t for t in await target.list_tasks(project="acme-shop")}
        assert tasks["Checkout"].area == "web shop" and tasks["Docs"].area is None
        assert not (await make_plan(target, folder)).writes  # case does not count as a change
        long_name = "x" * 33
        (folder / "TASKS.md").write_text(
            registry.replace("| Web shop |", f"| {long_name} |"), encoding="utf-8"
        )
        too_long = await make_plan(target, folder)
        assert not too_long.failed
        assert any("area is longer than 32 characters" in w for w in too_long.warnings)
        await apply_plan(target, too_long)
        kept = {t.title: t for t in await target.list_tasks(project="acme-shop")}
        assert kept["Checkout"].area == "web shop"  # the long text leaves the area as it was
    finally:
        await target.close()


async def test_import_counts_new_areas_against_the_limit(tmp_path: Path) -> None:
    folder = tmp_path / "in"
    folder.mkdir()
    rows = [f"| acme-shop | Task {n} | area {n:02d} | plan |" for n in range(AREAS_MAX + 2)]
    long = "| acme-shop | Long | " + "y" * 40 + " | plan |"
    text = "| project | task | area | stage |\n|---|---|---|---|\n" + "\n".join([*rows, long])
    (folder / "TASKS.md").write_text(text + "\n", encoding="utf-8")
    target = Service(tmp_path / "b.sqlite")
    await target.start()
    try:
        await target.upsert_project("acme-shop")
        plan = await make_plan(target, folder)
        assert not plan.failed
        assert sum("more than 24 areas" in w for w in plan.warnings) == 2
        assert sum("longer than 32" in w for w in plan.warnings) == 1
        assert sum(1 for i in plan.items if i.kind == "task" and i.action == "add") == 27
        await apply_plan(target, plan)
        tasks = {t.title: t for t in await target.list_tasks(project="acme-shop")}
        assert len(tasks) == 27
        assert tasks["Task 23"].area == "area 23"
        assert tasks["Task 24"].area is None and tasks["Task 25"].area is None
        assert tasks["Long"].area is None
        assert len((await target.project_areas("acme-shop"))[1]) == AREAS_MAX
    finally:
        await target.close()


def test_a_board_without_areas_exports_the_registry_it_did() -> None:
    task = Task(
        id=1,
        project_id=1,
        key="acme-shop-1",
        title="Checkout",
        stage=Stage.PLAN,
        stage_note=None,
        waiting_on=None,
        next_step=None,
        branch=None,
        doc_path=None,
        description=None,
        progress=None,
        session_id=None,
        priority=0,
        version=1,
        created_at=NOW,
        updated_at=NOW,
        closed_at=None,
    )
    text = render_registry([("acme-shop", task.key, task)])
    assert "| project | ref | task | stage | waiting on | next step | document | rank |" in text
