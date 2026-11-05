"""Ctrl+K: the fuzzy search and its endpoint; saved filters and the board's query parameters."""

import re
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.db.connection import migrate
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.domain.switcher import fuzzy_score
from relaybay.security import CSRF_FIELD, csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_migrations import LATEST

DESKTOP = "local_00000000-0000-0000-0000-00000000000d"
HTMX = {"HX-Request": "true"}
ORIGIN = {"Origin": BASE}


# --- fuzzy ------------------------------------------------------------------------------------


def test_fuzzy_prefers_closer_matches() -> None:
    text = "acme-shop-12"
    exact = fuzzy_score(text, text)
    start = fuzzy_score("acme", text)
    word = fuzzy_score("shop", text)
    inner = fuzzy_score("cme", text)
    scattered = fuzzy_score("acs12", text)
    assert exact > start > word > inner > scattered > 0
    assert fuzzy_score("zzz", text) is None
    assert fuzzy_score("21", text) is None  # letters have to be in order


def test_fuzzy_needs_every_word_in_some_text() -> None:
    assert fuzzy_score("cart acme", "acme-shop-1", "Fix the cart") is not None
    assert fuzzy_score("cart nope", "acme-shop-1", "Fix the cart") is None
    assert fuzzy_score("KART", "Kart") == 1000  # case does not matter
    assert fuzzy_score("", "anything") == 0
    assert fuzzy_score("x", None, "") is None


# --- search ------------------------------------------------------------------------------------


async def seed(service: Service) -> None:
    await service.upsert_project("acme-shop", name="Acme Shop")
    await service.upsert_project("demo-api", name="Demo API")
    await service.create_task("acme-shop", "Checkout page", session="checkout-agent")
    await service.create_task("demo-api", "Error bodies", session=DESKTOP, session_title="Errors")


async def test_search_finds_tasks_projects_and_sessions(service: Service) -> None:
    await seed(service)
    by_key = await service.switcher("acme-shop-1")
    assert (by_key[0].kind, by_key[0].key) == ("task", "acme-shop-1")
    assert by_key[0].url == "/p/acme-shop?task=acme-shop-1"

    by_title = await service.switcher("chkout")
    assert [i.key for i in by_title if i.kind == "task"] == ["acme-shop-1"]

    project = [i for i in await service.switcher("demo api") if i.kind == "project"]
    assert (project[0].title, project[0].url) == ("Demo API", "/p/demo-api")

    sessions = [i for i in await service.switcher("checkout-agent") if i.kind == "session"]
    assert sessions[0].title == "checkout-agent"
    assert re.fullmatch(r"/sessions#session-\d+", sessions[0].url)
    assert [i for i in await service.switcher(DESKTOP) if i.kind == "session"]

    assert await service.switcher("no such thing at all") == []


async def test_empty_search_shows_recent_tasks_and_projects_not_sessions(service: Service) -> None:
    await seed(service)
    items = await service.switcher("  ")
    assert {i.kind for i in items} == {"task", "project"}
    tasks = [i.key for i in items if i.kind == "task"]
    assert tasks == ["demo-api-1", "acme-shop-1"]  # the last changed first


async def test_empty_search_breaks_a_timestamp_tie_by_the_newer_task(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two writes in the same millisecond share updated_at; the later task must still come first.
    monkeypatch.setattr("relaybay.service.now_iso", lambda: "2026-11-05T10:00:00.000+00:00")
    await seed(service)
    tasks = [i.key for i in await service.switcher("") if i.kind == "task"]
    assert tasks == ["demo-api-1", "acme-shop-1"]


async def test_empty_search_keeps_the_projects_with_many_tasks(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    for n in range(30):
        await service.create_task("acme-shop", f"Task {n}")
    items = await service.switcher("")
    assert len([i for i in items if i.kind == "task"]) == 15
    assert {i.url for i in items if i.kind == "project"} == {"/p/acme-shop", "/p/demo-api"}


async def test_search_is_capped(service: Service) -> None:
    await service.upsert_project("acme-shop")
    for n in range(30):
        await service.create_task("acme-shop", f"Same title {n}")
    assert len(await service.switcher("same title")) == 20


def test_endpoint_is_behind_the_cookie(client: TestClient) -> None:
    answer = client.get("/switcher?q=x", headers={"Accept": "application/json"})
    assert answer.status_code == 401


def test_endpoint_returns_json_and_the_page_has_the_button(board: TestClient) -> None:
    run_in_app(board, seed, board.app.state.service)
    answer = board.get("/switcher", params={"q": "checkout"})
    assert answer.status_code == 200 and answer.headers["cache-control"] == "no-store"
    item = answer.json()["items"][0]
    assert item == {
        "kind": "task",
        "title": "Checkout page",
        "detail": "plan · acme-shop",
        "url": "/p/acme-shop?task=acme-shop-1",
        "key": "acme-shop-1",
    }
    page = board.get("/").text
    assert 'class="switcher-open"' in page and 'id="switcher"' in page
    assert "/static/switcher.js?v=" in page


def test_the_login_page_has_no_switcher(client: TestClient) -> None:
    page = client.get("/login").text
    assert "switcher" not in page


def test_script_writes_results_as_text_and_leaves_fields_alone(board: TestClient) -> None:
    """The contract of the script: no HTML from results; the shortcut stays out of fields."""
    # A checkout with CRLF line endings (git on Windows) must read the same.
    script = board.get("/static/switcher.js").text.replace("\r\n", "\n")
    assert "innerHTML" not in script and "textContent" in script
    guard = script.index('closest("input, textarea, select, [contenteditable]")')
    assert guard < script.index("event.preventDefault();\n    open();")


# --- saved filters: service ---------------------------------------------------------------------


async def test_save_list_and_delete(service: Service) -> None:
    await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    saved = await service.save_filter(
        "acme-shop", " Mine ", stage="DEV", waiting_on="owner", session="agent", q="cart page"
    )
    assert saved.name == "Mine"
    assert saved.query == {
        "stage": "dev",
        "waiting_on": "owner",
        "session": "agent",
        "q": "cart page",
    }
    assert saved.qs == "stage=dev&waiting_on=owner&session=agent&q=cart+page"
    assert [f.name for f in await service.saved_filters("acme-shop")] == ["Mine"]
    assert await service.saved_filters("demo-api") == []
    assert len(await service.saved_filters()) == 1

    with pytest.raises(ConflictError, match="already has"):
        await service.save_filter("acme-shop", "Mine", q="other")
    await service.save_filter("demo-api", "Mine", q="other")  # unique per project only

    with pytest.raises(NotFoundError):
        await service.delete_filter("demo-api", saved.id)  # not this project's
    await service.delete_filter("acme-shop", saved.id)
    assert await service.saved_filters("acme-shop") == []


@pytest.mark.parametrize(
    ("name", "query", "message"),
    [
        ("", {"q": "x"}, "needs a name"),
        ("x" * 61, {"q": "x"}, "longer"),
        ("Nothing", {}, "Set a stage"),
        ("Bad", {"stage": "qa"}, "Unknown stage"),
    ],
)
async def test_invalid_filters_are_refused(
    service: Service, name: str, query: dict[str, str], message: str
) -> None:
    await service.upsert_project("acme-shop")
    with pytest.raises(ValidationError, match=message):
        await service.save_filter("acme-shop", name, **query)


async def test_upgrade_adds_the_filter_table_and_project_removal_cascades(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    service = Service(db)
    await service.start()
    await service.upsert_project("acme-shop")
    await service.save_filter("acme-shop", "Mine", q="x")
    await service.close()
    with sqlite3.connect(db) as raw:
        raw.execute("PRAGMA foreign_keys=ON")
        raw.execute("DELETE FROM project")
        assert raw.execute("SELECT COUNT(*) FROM saved_filter").fetchone()[0] == 0
    raw.close()


async def test_migrate_is_idempotent(service: Service) -> None:
    assert await migrate(service._write_conn) == LATEST


# --- board -------------------------------------------------------------------------------------


async def board_tasks(service: Service) -> None:
    await service.upsert_project("acme-shop", name="Acme Shop")
    await service.create_task("acme-shop", "Cart page", stage="dev", waiting_on="owner")
    await service.create_task("acme-shop", "Payment", stage="dev", waiting_on="agent")
    await service.create_task("acme-shop", "Search", stage="live", session="search-agent")


def cards(html: str) -> list[str]:
    return re.findall(r'id="card-([a-z0-9-]+)"', html)


def test_board_filters_by_stage_waiting_on_session_and_text(board: TestClient) -> None:
    run_in_app(board, board_tasks, board.app.state.service)
    assert sorted(cards(board.get("/p/acme-shop").text)) == [
        "acme-shop-1",
        "acme-shop-2",
        "acme-shop-3",
    ]
    assert cards(board.get("/p/acme-shop?stage=live").text) == ["acme-shop-3"]
    assert cards(board.get("/p/acme-shop?waiting_on=OWNER").text) == ["acme-shop-1"]
    assert cards(board.get("/p/acme-shop?session=search-agent").text) == ["acme-shop-3"]
    assert cards(board.get("/p/acme-shop?session=nobody").text) == []
    assert cards(board.get("/p/acme-shop?stage=dev&q=pay").text) == ["acme-shop-2"]


@pytest.mark.parametrize(
    ("query", "reason"),
    [("stage=qa", "Unknown stage"), ("session=local_bad", "looks like a desktop session id")],
)
def test_a_bad_filter_shows_the_board_with_an_error_line(
    board: TestClient, query: str, reason: str
) -> None:
    run_in_app(board, board_tasks, board.app.state.service)
    page = board.get(f"/p/acme-shop?{query}")
    assert page.status_code == 200 and "Filter ignored:" in page.text and reason in page.text
    assert len(cards(page.text)) == 3 and 'data-filtered="1"' not in page.text
    column = board.get(f"/p/acme-shop/col/dev?{query}", headers=HTMX)
    assert column.status_code == 200 and len(cards(column.text)) == 2


def test_a_bad_filter_is_escaped_in_the_error_line(board: TestClient) -> None:
    run_in_app(board, board_tasks, board.app.state.service)
    page = board.get("/p/acme-shop", params={"stage": "<script>alert(1)</script>"})
    assert page.status_code == 200 and "<script>alert(1)" not in page.text


def test_a_stage_filter_opens_its_page_and_tab(board: TestClient) -> None:
    run_in_app(board, board_tasks, board.app.state.service)
    page = board.get("/p/acme-shop?stage=live").text
    assert 'id="board" class="board" data-page="2"' in page
    assert re.search(r'id="tab-live" value="live" checked', page)
    # An empty result still opens the stage asked for.
    page = board.get("/p/acme-shop?stage=review").text
    assert re.search(r'id="tab-review" value="review" checked', page)


def test_live_refetches_keep_the_filter(board: TestClient) -> None:
    run_in_app(board, board_tasks, board.app.state.service)
    page = board.get("/p/acme-shop?stage=dev&waiting_on=owner&session=&q=cart").text
    assert 'data-filtered="1"' in page
    query = "stage=dev&amp;waiting_on=owner&amp;q=cart"
    assert f'hx-get="/p/acme-shop/col/dev?{query}"' in page
    assert f'hx-get="/p/acme-shop/col/live?{query}"' in page

    dev = board.get("/p/acme-shop/col/dev?stage=dev&waiting_on=owner", headers=HTMX)
    assert cards(dev.text) == ["acme-shop-1"]
    # A column other than the filtered stage stays empty on a refetch.
    live = board.get("/p/acme-shop/col/live?stage=dev", headers=HTMX)
    assert cards(live.text) == []
    assert 'data-filtered="1"' not in board.get("/p/acme-shop").text
    assert 'data-filtered="1"' not in board.get("/p/acme-shop?archived=1").text


def test_saved_filter_create_list_apply_delete_on_the_board(board: TestClient, token: str) -> None:
    run_in_app(board, board_tasks, board.app.state.service)
    csrf = csrf_token(token)

    plain = board.get("/p/acme-shop").text
    assert "Save filter" not in plain and 'class="saved-filters"' not in plain

    filtered = board.get("/p/acme-shop?waiting_on=owner").text
    assert "Save filter" in filtered
    assert '<input type="hidden" name="waiting_on" value="owner">' in filtered

    saved = board.post(
        "/p/acme-shop/filters",
        data={CSRF_FIELD: csrf, "name": "Mine <b>", "waiting_on": "owner"},
        headers=ORIGIN,
        follow_redirects=True,
    )
    assert "Saved the filter Mine &lt;b&gt;." in saved.text
    assert str(saved.url) == f"{BASE}/p/acme-shop?waiting_on=owner"
    assert "Mine <b>" not in saved.text
    # In the sidebar under the project, marked current; the plain Board item is not.
    assert re.search(
        r'<a class="nav-item is-active" href="/p/acme-shop\?waiting_on=owner" aria-current="page">',
        saved.text,
    )
    assert '<a class="nav-item" href="/p/acme-shop">Board</a>' in saved.text
    # On other pages too.
    assert 'href="/p/acme-shop?waiting_on=owner"' in board.get("/inbox").text

    duplicate = board.post(
        "/p/acme-shop/filters",
        data={CSRF_FIELD: csrf, "name": "Mine <b>", "waiting_on": "owner"},
        headers=ORIGIN,
        follow_redirects=True,
    )
    assert "already has a saved filter" in duplicate.text

    gone = board.post(
        "/p/acme-shop/filters/1/delete",
        data={CSRF_FIELD: csrf},
        headers=ORIGIN,
        follow_redirects=True,
    )
    assert "Saved filter deleted." in gone.text
    assert 'href="/p/acme-shop?waiting_on=owner"' not in gone.text
    assert (
        board.post(
            "/p/acme-shop/filters/1/delete", data={CSRF_FIELD: csrf}, headers=ORIGIN
        ).status_code
        == 404
    )


def test_saved_filter_writes_need_the_csrf_token(board: TestClient, token: str) -> None:
    run_in_app(board, board_tasks, board.app.state.service)
    answer = board.post("/p/acme-shop/filters", data={"name": "x", "q": "y"}, headers=ORIGIN)
    assert answer.status_code == 403
    assert board.post("/p/acme-shop/filters/1/delete", data={}, headers=ORIGIN).status_code == 403
    assert (
        board.post(
            "/p/acme-shop/filters",
            data={CSRF_FIELD: csrf_token(token), "name": "x", "q": "y"},
            headers={**ORIGIN, "Authorization": f"Bearer {token}"},
        ).status_code
        == 403
    )


async def test_the_demo_has_rule_versions_a_proposal_and_a_filter(tmp_path: Path) -> None:
    from relaybay.demo import _seed_new

    db = tmp_path / "relaybay.sqlite"
    await _seed_new(db)
    service = Service(db)
    await service.start()
    try:
        assert service.global_rules.global_version == 2
        assert [p.scope for p in await service.rule_proposals()] == ["acme-shop"]
        assert [f.name for f in await service.saved_filters("acme-shop")] == ["Waiting on me"]
    finally:
        await service.close()
