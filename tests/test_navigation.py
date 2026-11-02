"""Sidebar navigation, Inbox, Sessions and Activity pages."""

import re
import uuid
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay import machine_setup
from relaybay.config import Settings, load_settings
from relaybay.service import Service
from tests.conftest import BASE, run_in_app
from tests.test_hooks import CWD, hook
from tests.test_setup import NEW

DESKTOP = f"local_{uuid.UUID(int=7)}"
CC_OTHER = "5d6e7f80-1a2b-4c3d-9e8f-0a1b2c3d4e5f"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # The Integrations page reads Claude Code files: point it at a temporary home.
    claude_home = tmp_path / "claude-home"
    (claude_home / ".claude").mkdir(parents=True)
    return load_settings(home=tmp_path / "data", claude_home=claude_home, environ={})


@pytest.fixture(autouse=True)
def fake_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test runs the real `claude` binary."""
    monkeypatch.setattr(machine_setup, "detect", lambda **_: NEW)


def svc(client: TestClient) -> Service:
    return client.app.state.service


def current(page: str) -> list[str]:
    """The hrefs of the sidebar items marked as the current page."""
    return re.findall(r'href="([^"]+)" aria-current="page"', page)


def test_sidebar_on_every_page(board: TestClient, seeded: str) -> None:
    pages = {
        "/": ["/"],
        "/p/acme-shop": ["/p/acme-shop"],
        f"/p/acme-shop?task={seeded}": ["/p/acme-shop"],
        f"/t/{seeded}": ["/p/acme-shop"],
        "/p/acme-shop/sessions": ["/p/acme-shop/sessions"],
        "/sessions": [],
        "/inbox": ["/inbox"],
        "/activity": ["/activity"],
        "/settings/integrations": ["/settings/integrations"],
    }
    for url, active in pages.items():
        page = board.get(url)
        assert page.status_code == 200, url
        text = page.text
        assert 'id="sidebar"' in text and 'class="nav-toggle"' in text, url
        for href in ("/inbox", "/", "/activity", "/settings/integrations"):
            assert f'href="{href}"' in text, (url, href)
        assert 'href="/p/acme-shop/sessions"' in text, url
        assert current(text) == active, url
        # Every page listens to all projects, for the Inbox count.
        assert 'sse-connect="/events"' in text, url
    # The project of the page is expanded, the others are not.
    assert '<details class="nav-project" open>' in board.get("/p/acme-shop").text
    assert '<details class="nav-project" open>' not in board.get("/inbox").text
    # Items without a working feature are not shown.
    for absent in ("Manager", "Schedules", "Map", "Usage"):
        assert absent not in board.get("/").text
    assert "Integrations</a>" not in board.get("/").text.split("</nav>")[1]  # no header link


def test_login_page_has_no_sidebar(client: TestClient) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    assert 'id="sidebar"' not in page.text and "nav-toggle" not in page.text


def test_settings_redirects_to_integrations(board: TestClient) -> None:
    response = board.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/settings/integrations"


def test_new_pages_need_sign_in(client: TestClient) -> None:
    for url in ("/inbox", "/activity", "/sessions", "/p/acme-shop/sessions"):
        response = client.get(url, headers={"Accept": "text/html"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"].startswith("/login")
    for url in ("/fragments/inbox", "/fragments/inbox-badge", "/fragments/activity"):
        assert client.get(url, headers={"HX-Request": "true"}).status_code == 401


# --- Inbox -----------------------------------------------------------------


async def test_inbox_waiting_on_the_owner(service: Service) -> None:
    await service.upsert_project("acme-shop")
    mine = await service.create_task("acme-shop", "Pick a gateway", waiting_on="Owner ")
    me = await service.create_task("acme-shop", "Review copy", waiting_on="ME")
    await service.create_task("acme-shop", "Write tests", waiting_on="reviewer")
    done = await service.create_task("acme-shop", "Old", waiting_on="owner", stage="done")
    inbox = await service.inbox(["owner", "me", "you"])
    assert {t.key for t in inbox.waiting} == {mine.key, me.key}
    assert done.key not in {t.key for t in inbox.waiting}  # a done task waits on nobody
    assert inbox.count == 2
    # Other names: only those match.
    assert [t.title for t in (await service.inbox(["Reviewer"])).waiting] == ["Write tests"]
    assert (await service.inbox(["alex"])).count == 0


async def test_inbox_rule_warnings_until_the_next_move(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="dev")
    await service.set_stage(task.key, "test")  # the default rules want a report first
    inbox = await service.inbox(["owner"])
    assert [(t.key, w) for t, w in inbox.rule_warnings] == [
        (
            task.key,
            ["Warning (rules): moving to test usually needs a report since the last stage change."],
        )
    ]
    await service.add_report(task.key, "changed · S · tested")
    await service.set_stage(task.key, "live")  # no requirement: the warning is gone
    assert (await service.inbox(["owner"])).rule_warnings == []


async def test_inbox_not_logged_only_with_require_tracker_log(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Shipped", stage="done", progress="all")
    logged = await service.create_task(
        "acme-shop", "Logged", stage="done", progress="all", tracker_ref="T-1"
    )
    await service.update_task(logged.key, {"tracker_logged": True})
    assert (await service.inbox(["owner"])).not_logged == []
    inbox = await service.inbox(["owner"], require_tracker_log=True)
    assert [t.key for t in inbox.not_logged] == [task.key]


async def test_inbox_unlinked_hook_sessions(service: Service) -> None:
    # A folder of two projects cannot be placed: the hook session stays unlinked.
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.upsert_project("demo-api", repo_path=CWD)
    await service.record_hook(hook("SessionStart"))
    inbox = await service.inbox(["owner"])
    assert len(inbox.unlinked) == 1 and "several projects" in inbox.unlinked[0].unlinked_reason


def test_owner_names_from_config_and_environment(tmp_path: Path) -> None:
    assert load_settings(home=tmp_path, environ={}).owner_names == ["owner", "me", "you"]
    (tmp_path / "config.toml").write_text('owner_names = ["Alex", "lead"]\n', encoding="utf-8")
    assert load_settings(home=tmp_path, environ={}).owner_names == ["Alex", "lead"]
    env = {"RELAYBAY_OWNER_NAMES": "sam, owner"}
    assert load_settings(home=tmp_path, environ=env).owner_names == ["sam", "owner"]
    (tmp_path / "config.toml").write_text("owner_names = []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="owner_names"):
        load_settings(home=tmp_path, environ={})


def test_inbox_page_badge_and_empty_state(board: TestClient, seeded: str) -> None:
    empty = board.get("/inbox")
    assert "Nothing needs you right now." in empty.text
    assert 'id="inbox-badge"' in empty.text and re.search(
        r'id="inbox-badge"[^>]* hidden', empty.text
    )

    run_in_app(board, svc(board).update_task, seeded, {"waiting_on": "you"})
    page = board.get("/inbox")
    assert "Waiting on you" in page.text and "Nothing needs you" not in page.text
    assert f'href="/p/acme-shop?task={seeded}"' in page.text  # opens the task dialog
    assert 'hx-get="/fragments/inbox"' in page.text
    badge = board.get("/fragments/inbox-badge", headers={"HX-Request": "true"})
    assert badge.text.lstrip().startswith('<span class="nav-badge" id="inbox-badge"')
    assert ">1<" in badge.text and " hidden" not in badge.text
    assert ">1<" in board.get("/p/acme-shop").text.split('id="inbox-badge"')[1][:200]
    fragment = board.get("/fragments/inbox", headers={"HX-Request": "true"})
    assert fragment.text.lstrip().startswith('<div id="inbox"') and seeded in fragment.text


def test_inbox_uses_the_configured_owner_names(tmp_path: Path) -> None:
    from relaybay.app import create_app
    from relaybay.security import load_or_create_token

    home = tmp_path / "data"
    home.mkdir()
    (home / "config.toml").write_text('owner_names = ["alex"]\n', encoding="utf-8")
    settings = load_settings(home=home, environ={})
    token = load_or_create_token(settings.token_path)
    with TestClient(create_app(settings), base_url=BASE) as client:
        client.get(f"/login?token={token}")
        service = svc(client)
        run_in_app(client, service.upsert_project, "acme-shop")
        alex = run_in_app(client, service.create_task, "acme-shop", "A", waiting_on="Alex")
        run_in_app(client, service.create_task, "acme-shop", "B", waiting_on="owner")
        page = client.get("/inbox").text
        assert alex.key in page and "acme-shop-2" not in page


# --- Sessions --------------------------------------------------------------


def test_sessions_pages(board: TestClient, seeded: str) -> None:
    service = svc(board)
    run_in_app(board, service.upsert_project, "demo-api")
    run_in_app(board, service.bind_session, seeded, DESKTOP, session_title="Checkout dev")
    other = run_in_app(board, service.create_task, "demo-api", "Rate limits", session="api-worker")

    page = board.get("/p/acme-shop/sessions")
    assert page.status_code == 200
    assert "Checkout dev" in page.text and "api-worker" not in page.text
    assert f'href="/p/acme-shop?task={seeded}"' in page.text  # the bound task
    assert f'href="claude://claude.ai/epitaxy/{DESKTOP}"' in page.text  # open in desktop
    assert "desktop" in page.text and "no hook status" in page.text
    assert 'href="/sessions"' in page.text

    everything = board.get("/sessions").text
    assert "Checkout dev" in everything and "api-worker" in everything and other.key in everything
    fragment = board.get("/fragments/sessions?project=demo-api", headers={"HX-Request": "true"})
    assert fragment.text.lstrip().startswith('<div id="sessions"')
    assert "api-worker" in fragment.text and "Checkout dev" not in fragment.text
    assert board.get("/p/nope/sessions").status_code == 404


async def test_hook_sessions_of_a_project(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.upsert_project("demo-api")
    await service.record_hook(hook("SessionStart"))  # no bound desktop session: a plain one
    await service.record_hook(hook("PreToolUse", CC_OTHER, cwd="/elsewhere"))
    rows = await service.list_sessions("acme-shop")
    assert [(r.session.kind, r.session.status) for r in rows] == [("hook", "working")]
    assert await service.list_sessions("demo-api") == []
    assert len(await service.list_sessions()) == 2


# --- Activity --------------------------------------------------------------


def rows(text: str) -> list[int]:
    return [int(x) for x in re.findall(r'class="activity-row" data-id="(\d+)"', text)]


def test_activity_pages_newest_first(board: TestClient, seeded: str) -> None:
    service = svc(board)
    run_in_app(board, service.upsert_project, "demo-api")
    for n in range(60):
        run_in_app(board, service.update_task, seeded, {"next_step": f"step {n}"})
    page = board.get("/activity")
    first = rows(page.text)
    assert len(first) == 50 and first == sorted(first, reverse=True)
    assert 'data-live="1"' in page.text
    more = re.search(r'hx-get="/fragments/activity\?before=(\d+)"', page.text)
    assert more and int(more.group(1)) == first[-1]
    rest = board.get(f"/fragments/activity?before={first[-1]}", headers={"HX-Request": "true"})
    second = rows(rest.text)
    assert second and max(second) < first[-1] and "Load more" not in rest.text
    # The no-JS page of the same link, not live.
    paged = board.get(f"/activity?before={first[-1]}")
    assert rows(paged.text) == second and 'data-live="1"' not in paged.text
    # Live prepend asks only for what is newer than the newest row.
    run_in_app(board, service.add_report, seeded, "verified · S · done")
    newer = board.get(f"/fragments/activity?after={first[0]}", headers={"HX-Request": "true"})
    assert len(rows(newer.text)) == 1 and "report.added" in newer.text
    assert "verified · S · done" not in newer.text and "done" in newer.text  # the summary


def test_activity_filters(board: TestClient, seeded: str) -> None:
    service = svc(board)
    run_in_app(board, service.upsert_project, "demo-api")
    api = run_in_app(board, service.create_task, "demo-api", "Rate limits")
    run_in_app(board, service.set_stage, seeded, "test")
    run_in_app(board, service.record_hook, hook("SessionStart", cwd="/elsewhere"))
    run_in_app(board, service.record_hook, hook("Stop", cwd="/elsewhere"))

    page = board.get("/activity").text
    assert "dev → test" in page and api.key in page
    assert f'href="/p/acme-shop?task={seeded}"' in page
    assert "session.status_changed" not in page.split("</select>")[-1]  # hook noise hidden

    acme = board.get("/activity?project=acme-shop").text
    assert "dev → test" in acme and api.key not in acme.split("</form>")[1]
    stages = board.get("/activity?kind=task.stage_changed").text
    assert len(rows(stages)) == 1
    statuses = board.get("/activity?kind=session.status_changed").text
    assert rows(statuses) and "working → idle" in statuses
    assert board.get("/activity?kind=nope").status_code == 400
    assert board.get("/activity?project=nope").status_code == 404
