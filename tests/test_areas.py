"""Areas (designs 0024, 0029): a list per card, from a project list that grows from use; the
board chips, filter and settings; MCP; the backlog's "To plan"; export and import; migrations
0048 and 0065."""

from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient

from relaybay.db.connection import connect
from relaybay.domain.areas import AREA_COLORS, AREAS_MAX, split_area_text
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
    api, none = {"areas": ["API"], "area": "API"}, {"areas": [], "area": None}
    assert changes == [api, none, api, none]
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
        and e.payload["changes"] == {"areas": [], "area": None}
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
    assert 'name="area"' in form and '<datalist id="area-values' not in form
    assert 'class="area-chip area-blue area-pick" data-area="web shop" aria-pressed="false"' in form
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
    assert "Any area" in page and '<input type="checkbox" name="area" value="API">' in page
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
    assert kept.query == {"area": ["API"]} and kept.qs == "area=API"
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
    assert "`area` | string or list of strings |" in schema
    assert "at most 8 a card and 32 characters a name" in schema
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


# --- several areas on one card (design 0029) -------------------------------------------------


async def test_several_areas_by_list_comma_text_and_clears(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Tokens", area="backend, api")
    assert task.area_names == ["backend", "api"] and task.area == "backend"
    assert task.area_id == task.areas[0].id
    listed = await service.update_task(task.key, {"area": ["API", "web", "Backend", "web"]})
    assert listed.area_names == ["api", "web", "backend"]  # stored spelling, the same once
    assert listed.area_id == listed.areas[0].id
    same = await service.update_task(task.key, {"area": "api, WEB, backend"})
    assert same.version == listed.version
    moved = await service.update_task(task.key, {"areas": ["web", "api"]})
    assert moved.area_names == ["web", "api"] and moved.version == listed.version + 1
    for clear in ("-", "", [], None):
        await service.update_task(task.key, {"area": ["web"]})
        cleared = await service.update_task(task.key, {"area": clear})
        assert cleared.areas == [] and cleared.area_id is None
    with pytest.raises(ValidationError, match="not both"):
        await service.update_task(task.key, {"area": "web", "areas": ["web"]})
    with pytest.raises(ValidationError, match="at most 8 areas"):
        await service.update_task(task.key, {"area": [f"a{n}" for n in range(9)]})
    with pytest.raises(ValidationError, match="cannot hold a comma"):
        await service.update_task(task.key, {"area": ["web, shop"]})
    with pytest.raises(ValidationError, match="cannot hold a comma"):
        await service.add_area("acme-shop", "web, shop")
    with pytest.raises(ValidationError, match="string or a list"):
        await service.update_task(task.key, {"area": [1]})
    _, areas = await service.project_areas("acme-shop")
    assert [a.name for a in areas] == ["backend", "api", "web"]


async def test_new_names_of_one_write_count_together_against_the_limit(service: Service) -> None:
    await service.upsert_project("acme-shop")
    for n in range(AREAS_MAX - 1):
        await service.add_area("acme-shop", f"area {n}")
    task = await service.create_task("acme-shop", "Checkout")
    with pytest.raises(ValidationError, match="the limit"):
        await service.update_task(task.key, {"area": ["one", "two"]})
    assert len((await service.project_areas("acme-shop"))[1]) == AREAS_MAX - 1
    with collect_notes() as notes:
        await service.update_task(task.key, {"area": ["area 0", "one"]})
    assert len(notes) == 1 and "Area 'one' was new" in notes[0]


async def _rename_in_db(db: Path, old: str, new: str) -> None:
    """An area named before the comma rule, as an older version could store it."""
    conn = await connect(db)
    try:
        await conn.execute("UPDATE project_area SET name = ? WHERE name = ?", (new, old))
        await conn.commit()
    finally:
        await conn.close()


async def test_an_existing_comma_name_stays_one_area(service: Service, tmp_path: Path) -> None:
    await service.upsert_project("acme-shop")
    await service.add_area("acme-shop", "web")
    _, (web,) = await service.project_areas("acme-shop")
    await _rename_in_db(service.db_path, "web", "web, shop")
    task = await service.create_task("acme-shop", "Checkout", area="Web, Shop")
    assert task.area_names == ["web, shop"]
    both = await service.update_task(task.key, {"area": ["web, shop", "api"]})
    assert both.area_names == ["web, shop", "api"]
    split = await service.update_task(task.key, {"area": "api, docs"})
    assert split.area_names == ["api", "docs"]
    beside = await service.update_task(task.key, {"area": "api, WEB, shop, docs"})
    assert beside.area_names == ["api", "web, shop", "docs"]  # adjacent parts form the old name
    assert split_area_text("web, shop", set()) == ["web", "shop"]
    assert split_area_text("a, b, c, web", {"a, b, c", "a, b"}) == ["a, b, c", "web"]
    renamed = await service.save_area("acme-shop", web.id, name="WEB, SHOP", color="rose")
    assert renamed.name == "WEB, SHOP"  # the same key: a case change is no new comma name


async def test_list_tasks_matches_any_of_the_areas(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.create_task("acme-shop", "Checkout", area=["web", "api"])
    await service.create_task("acme-shop", "Orders", area="api")
    await service.create_task("acme-shop", "Docs", area="docs")
    await service.create_task("acme-shop", "None")

    async def titles(area: str | list[str]) -> set[str]:
        return {t.title for t in await service.list_tasks(project="acme-shop", area=area)}

    assert await titles("API") == {"Checkout", "Orders"}
    assert await titles("web") == {"Checkout"}
    assert await titles(["web", "docs"]) == {"Checkout", "Docs"}
    assert await titles("docs, WEB") == {"Checkout", "Docs"}
    assert await titles(["docs", "nothing"]) == {"Docs"}
    assert await titles(["nothing", "else"]) == set()
    _, areas = await service.project_areas("acme-shop")
    assert [(a.name, a.tasks) for a in areas] == [("web", 1), ("api", 2), ("docs", 1)]


async def test_rename_merge_and_delete_on_cards_with_several_areas(service: Service) -> None:
    await service.upsert_project("acme-shop")
    both = await service.create_task("acme-shop", "Both", area=["docs", "web", "api"])
    api_first = await service.create_task("acme-shop", "Api first", area=["api", "web"])
    only_web = await service.create_task("acme-shop", "Only web", area="web")
    _, (docs, web, api) = await service.project_areas("acme-shop")
    await service.save_filter("acme-shop", "Web or api", area=["web", "api"])
    await service.save_filter("acme-shop", "Web", area="web", stage="plan")
    await service.save_filter("acme-shop", "Docs", area=["docs"])

    async def names(task: Task) -> list[str]:
        return (await service.get_task(task.key)).area_names

    async def filters() -> dict[str, tuple[str, ...]]:
        return {f.name: f.areas for f in await service.saved_filters("acme-shop")}

    start = (await service.events_since(0, limit=1000))[-1].id
    await service.save_area("acme-shop", web.id, name="Web shop", color="blue")
    assert await names(both) == ["docs", "Web shop", "api"]
    renamed = [
        e.payload["changes"]
        for e in await service.events_since(start, limit=500)
        if e.kind == "task.updated" and e.task_id == both.id
    ]
    assert renamed == [{"areas": ["docs", "Web shop", "api"], "area": "docs"}]
    assert (await filters())["Web or api"] == ("Web shop", "api")

    # api into "Web shop": a card with both keeps the target once, at the first of the two.
    before = await service.get_task(api_first.key)
    await service.save_area("acme-shop", api.id, name="web SHOP", color="blue")
    assert await names(both) == ["docs", "Web shop"]
    assert await names(api_first) == ["Web shop"]
    assert (await service.get_task(api_first.key)).version == before.version + 1
    assert await filters() == {
        "Web or api": ("Web shop",),
        "Web": ("Web shop",),
        "Docs": ("docs",),
    }

    await service.delete_area("acme-shop", docs.id)
    after = await service.get_task(both.key)
    assert after.area_names == ["Web shop"] and after.area_id == web.id
    assert await names(only_web) == ["Web shop"]
    saved = {f.name: f for f in await service.saved_filters("acme-shop")}
    assert saved["Docs"].areas == () and saved["Docs"].query == {}
    deleted = [
        e.payload["changes"]
        for e in await service.events_since(0, limit=1000)
        if e.kind == "task.updated" and e.task_id == both.id
    ]
    assert deleted[-1] == {"areas": ["Web shop"], "area": "Web shop"}
    _, left = await service.project_areas("acme-shop")
    assert [(a.name, a.tasks) for a in left] == [("Web shop", 3)]


async def test_migration_from_0064_copies_areas_and_filters(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0065")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 64;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 2, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO project_area (project_id, name, color, position, created_at)"
            " VALUES (1, 'web', 'rose', 0, ?), (1, 'api', 'teal', 1, ?)",
            (NOW, NOW),
        )
        for n, area_id in ((1, 2), (2, None)):
            await conn.execute(
                "INSERT INTO task (project_id, key, title, stage, area_id, created_at,"
                " updated_at) VALUES (1, ?, ?, 'dev', ?, ?, ?)",
                (f"acme-shop-{n}", f"Task {n}", area_id, NOW, NOW),
            )
        await conn.execute(
            "INSERT INTO saved_filter (project_id, name, stage, area, created_at)"
            " VALUES (1, 'Api', NULL, 'api', ?), (1, 'Dev', 'dev', NULL, ?)",
            (NOW, NOW),
        )
        await conn.commit()
        async with conn.execute("SELECT * FROM project_area ORDER BY id") as cur:
            areas_before = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        first = await svc.get_task("acme-shop-1")
        assert first.area_names == ["api"] and first.area_color == "teal"
        assert (await svc.get_task("acme-shop-2")).areas == []
        saved = {f.name: f for f in await svc.saved_filters("acme-shop")}
        assert saved["Api"].areas == ("api",) and saved["Api"].qs == "area=api"
        assert saved["Dev"].areas == () and saved["Dev"].query == {"stage": "dev"}
        await svc.update_task("acme-shop-2", {"area": ["web", "api"]})
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("SELECT * FROM project_area ORDER BY id") as cur:
            assert [dict(r) for r in await cur.fetchall()] == areas_before
        async with conn.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []
        async with conn.execute("SELECT key, area_id FROM task ORDER BY id") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [("acme-shop-1", 2), ("acme-shop-2", 1)]
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
            " AND tbl_name IN ('task', 'task_area')"
        ) as cur:
            indexes = {r[0] for r in await cur.fetchall()}
        assert {"task_first_area", "task_area_area"} <= indexes and "task_area" not in indexes
        async with conn.execute("SELECT area, areas FROM saved_filter ORDER BY id") as cur:
            rows = [tuple(r) for r in await cur.fetchall()]
        assert rows == [("api", '["api"]'), (None, None)]
    finally:
        await conn.close()


async def test_mcp_takes_a_list_and_shows_several_areas(service: Service) -> None:
    mcp = build_mcp(service)

    async def call(tool: str, **arguments: Any) -> str:
        result = await mcp.call_tool(tool, arguments)
        return "\n".join(c.text for c in result.content)

    await service.upsert_project("acme-shop")
    created = await call(
        "task_create", project="acme-shop", title="Tokens", area=["backend", "mobile app"]
    )
    assert "Area 'backend' was new" in created and "Area 'mobile app' was new" in created
    assert "- areas: backend, mobile app" in await call("task_get", task="acme-shop-1")
    assert "Areas: backend, mobile app" in await call("task_context", task="acme-shop-1")
    assert "areas: backend, mobile app" in await call("project_digest", project="acme-shop")
    listed = await call("task_list", project="acme-shop", area=["MOBILE APP", "web"])
    assert "acme-shop-1" in listed and "areas: backend, mobile app" in listed
    assert "acme-shop-1" in await call("task_list", project="acme-shop", area="web, backend")
    await call("task_update", task="acme-shop-1", area="api, backend")
    assert (await service.get_task("acme-shop-1")).area_names == ["api", "backend"]
    await call("task_update", task="acme-shop-1", area=[])
    assert (await service.get_task("acme-shop-1")).areas == []
    await call("task_update", task="acme-shop-1", fields={"areas": ["web", "api"]})
    assert (await service.get_task("acme-shop-1")).area_names == ["web", "api"]
    await call("task_update", task="acme-shop-1", area="web")
    assert "- area: web\n" in await call("task_get", task="acme-shop-1")
    with pytest.raises(ToolError, match="twice"):
        await mcp.call_tool(
            "task_update", {"task": "acme-shop-1", "area": "api", "fields": {"area": ["web"]}}
        )
    snapshot = await call("project_snapshot", project="acme-shop")
    assert "backend (0), mobile app (0), api (0), web (1)" in snapshot


def test_board_filters_by_several_areas_and_saves_them(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    web = run_in_app(board, service.create_task, "acme-shop", "Checkout", area=["web", "api"])
    docs = run_in_app(board, service.create_task, "acme-shop", "Guide", area="docs")
    other = run_in_app(board, service.create_task, "acme-shop", "Other")
    csrf = {"csrf_token": csrf_token(token)}

    card = board.get(f"/p/acme-shop/t/{web.key}/card").text
    assert '>web</span><span class="area-chip area-green" title="Area: api">api</span>' in card
    assert "<dt>Areas</dt>" in board.get(f"/p/acme-shop/t/{web.key}/view").text
    form = board.get(f"/p/acme-shop/t/{web.key}/form").text
    assert 'value="web, api"' in form
    assert 'data-area="api" aria-pressed="true"' in form
    assert 'data-area="docs" aria-pressed="false"' in form
    saved = board.post(
        f"/p/acme-shop/t/{docs.key}",
        data={**csrf, "version": str(docs.version), "area": "docs, API"},
        headers=FORM,
    )
    assert saved.status_code == 200 and "Saved." in saved.text
    assert run_in_app(board, service.get_task, docs.key).area_names == ["docs", "api"]

    page = board.get("/p/acme-shop", params=[("area", "web"), ("area", "docs")]).text
    assert f'id="card-{web.key}"' in page and f'id="card-{docs.key}"' in page
    assert f'id="card-{other.key}"' not in page
    assert '<input type="checkbox" name="area" value="web" checked>' in page
    assert '<input type="checkbox" name="area" value="api">' in page
    assert ">web, docs</summary>" in page
    assert 'col/plan?area=web&amp;area=docs"' in page
    hidden = (
        '<input type="hidden" name="area" value="web">'
        '<input type="hidden" name="area" value="docs">'
    )
    assert hidden in page
    unknown = board.get("/p/acme-shop", params={"area": "nope"}).text
    assert 'value="nope" checked> nope (no such area)' in unknown
    column = board.get("/p/acme-shop/col/plan", params=[("area", "docs")]).text
    assert f'id="card-{docs.key}"' in column and f'id="card-{web.key}"' not in column

    filed = board.post(
        "/p/acme-shop/filters",
        data={**csrf, "name": "Web or docs", "area": ["web", "docs"]},
        headers=FORM,
        follow_redirects=False,
    )
    assert filed.status_code == 303
    assert filed.headers["location"] == "/p/acme-shop?area=web&area=docs"
    (kept,) = run_in_app(board, service.saved_filters, "acme-shop")
    assert kept.areas == ("web", "docs") and kept.qs == "area=web&area=docs"
    shown = board.get("/p/acme-shop", params=[("area", "web"), ("area", "docs")]).text
    assert 'href="/p/acme-shop?area=web&amp;area=docs" aria-current="page"' in shown


def test_the_sidebar_nests_saved_filters_under_board(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(board, service.upsert_project, "demo-api", name="Demo API")
    run_in_app(board, service.save_filter, "acme-shop", "In dev", stage="dev")
    page = board.get("/p/acme-shop").text
    board_item = page.index('href="/p/acme-shop" aria-current="page">Board</a>')
    nested = page.index('<ul class="nav-list nav-sub nav-filters"', board_item)
    filter_item = page.index('href="/p/acme-shop?stage=dev"', nested)
    closes = page.index("</ul>", filter_item)
    backlog = page.index('href="/p/acme-shop/backlog"')
    assert board_item < nested < filter_item < closes < backlog
    assert "</li>" not in page[board_item:nested]  # the submenu sits inside Board's item
    assert page.count('class="nav-list nav-sub nav-filters"') == 1  # none for demo-api
    active = board.get("/p/acme-shop", params={"stage": "dev"}).text
    assert 'href="/p/acme-shop?stage=dev" aria-current="page"' in active


async def test_export_and_import_carry_several_areas(tmp_path: Path) -> None:
    source = Service(tmp_path / "a.sqlite")
    await source.start()
    try:
        await source.upsert_project("acme-shop")
        await source.create_task("acme-shop", "Checkout", area=["web", "api"])
        await source.create_task("acme-shop", "Docs", area="docs")
        wanted, problems = await gather(source)
    finally:
        await source.close()
    assert not problems
    registry = wanted["TASKS.md"].text
    assert "| acme-shop | acme-shop-1 | Checkout | web, api | plan |" in registry
    folder = tmp_path / "out"
    for rel, item in wanted.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(item.text, encoding="utf-8")

    db = tmp_path / "b.sqlite"
    target = Service(db)
    await target.start()
    try:
        await target.upsert_project("acme-shop")
        plan = await make_plan(target, folder)
        assert not plan.failed, [(i.item, i.detail) for i in plan.items if i.action == "error"]
        await apply_plan(target, plan)
        tasks = {t.title: t for t in await target.list_tasks(project="acme-shop")}
        assert tasks["Checkout"].area_names == ["web", "api"]
        assert tasks["Docs"].area_names == ["docs"]
        assert not (await make_plan(target, folder)).writes  # the round trip is unchanged
        # A cell that is a whole existing name holding a comma stays one area.
        await _rename_in_db(db, "docs", "docs, guides")
        (folder / "TASKS.md").write_text(
            registry.replace("| Docs | docs |", "| Docs | Docs, Guides |"), encoding="utf-8"
        )
        again = await make_plan(target, folder)
        assert not again.failed
        assert {i.action for i in again.items if i.kind == "task"} == {"unchanged"}
        await apply_plan(target, again)
        assert (await target.get_task(tasks["Docs"].key)).area_names == ["docs, guides"]
        (folder / "TASKS.md").write_text(
            registry.replace("| Docs | docs |", "| Docs | api, web |"), encoding="utf-8"
        )
        await apply_plan(target, await make_plan(target, folder))
        assert (await target.get_task(tasks["Docs"].key)).area_names == ["api", "web"]
    finally:
        await target.close()
