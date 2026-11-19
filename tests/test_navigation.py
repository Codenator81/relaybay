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
    assert "Manager" not in board.get("/").text
    for gone in ("/schedules", "/coordinator"):
        assert f'href="{gone}"' not in board.get("/").text
    assert "Integrations</a>" not in board.get("/").text.split("</nav>")[1]  # no header link


def test_the_removed_map_page_is_gone(board: TestClient) -> None:
    assert 'href="/map"' not in board.get("/").text
    for url in ("/map", "/map/graph", "/static/map.js", "/static/cytoscape.min.js"):
        assert board.get(url).status_code == 404, url


def test_the_pages_removed_in_0_12_are_gone(board: TestClient) -> None:
    for url in (
        "/schedules",
        "/coordinator",
        "/fragments/coordinator-badge",
        "/settings/agents",
        "/settings/profiles",
        "/coordinator/terminal",
        "/static/terminal.js",
        "/static/vendor/xterm/xterm.js",
        "/usage",
        "/fragments/budgets",
        "/fragments/brake-banner",
        "/settings/intake",
        "/messages",
        "/fragments/messages",
    ):
        assert board.get(url).status_code == 404, url


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


async def test_inbox_lists_an_open_card_once_with_its_marks(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="dev", waiting_on="owner")
    await service.set_review_steps(task.key, ["Look at the page"])
    await service.set_stage(task.key, "test")  # no report since the move: a rule warning
    await service.add_agent_comment(task.key, "Ready for a look")
    other = await service.create_task("acme-shop", "Address book", waiting_on="owner")
    inbox = await service.inbox(["owner"])
    # Before: one row per list, the same card four times.
    assert (
        len(inbox.waiting) + len(inbox.review) + len(inbox.replies) + len(inbox.rule_warnings) == 5
    )
    assert [(c.task.key, c.marks) for c in inbox.cards] == [
        (task.key, ["waiting", "review", "reply", "warning"]),
        (other.key, ["waiting"]),
    ]
    assert inbox.count == inbox.badge == 2


async def test_done_cards_leave_the_inbox_and_closing_reads_replies(service: Service) -> None:
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "Checkout", stage="dev", waiting_on="owner")
    reply, _ = await service.add_agent_comment(task.key, "Done")
    assert [c.task.key for c in (await service.inbox(["owner"])).cards] == [task.key]
    await service.set_stage(task.key, "done")  # warns: done usually needs a report
    inbox = await service.inbox(["owner"])
    assert inbox.cards == [] and inbox.rule_warnings == [] and inbox.replies == []
    assert inbox.count == 0
    _, unread = await service.list_comments(task.key, unread_only=True)
    assert reply.id not in [c.id for c in unread]  # read by the close, still on the card
    [read] = [e for e in await service.activity(kind="comment.read") if e.task_id == task.id]
    assert read.payload["stage"] == "done"  # the event names the stage the card went to

    # A reply written after the close still needs the owner: the card comes back for it.
    late, _ = await service.add_agent_comment(task.key, "One more thing")
    inbox = await service.inbox(["owner"])
    assert [(c.task.key, c.marks) for c in inbox.cards] == [(task.key, ["reply"])]
    assert [c.id for c in inbox.cards[0].replies] == [late.id]
    assert inbox.badge == 1


async def test_old_replies_on_done_cards_do_not_crowd_out_open_ones(service: Service) -> None:
    await service.upsert_project("acme-shop")
    done = await service.create_task("acme-shop", "Closed", stage="dev")
    first, _ = await service.add_agent_comment(done.key, "Old")
    await service.set_stage(done.key, "done")
    # 200 replies older than the close and never marked read (as before 0.14).
    async with service._tx() as tx:
        cols = [r[1] for r in await tx.conn.execute_fetchall("PRAGMA table_info(comment)")]
        copy = ", ".join(c for c in cols if c != "id")
        for _ in range(200):
            await tx.conn.execute(
                f"INSERT INTO comment ({copy}) SELECT {copy} FROM comment WHERE id = ?",
                (first.id,),
            )
        await tx.conn.execute("UPDATE comment SET read_at = NULL WHERE task_id = ?", (done.id,))
    open_task = await service.create_task("acme-shop", "Open", stage="dev")
    await service.add_agent_comment(open_task.key, "New")
    inbox = await service.inbox(["owner"])
    assert [c.task.key for c in inbox.cards] == [open_task.key]


async def test_tracker_rule_warning_at_done_only_with_a_tracker(tmp_path: Path) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", require_tracker_log=True)
    await svc.start()
    try:
        await svc.upsert_project("acme-shop")
        await svc.upsert_project("demo-api")
        plain = await svc.create_task("acme-shop", "No tracker", progress="All")
        saved = await svc.set_stage_checked(plain.key, "done")
        assert not [w for w in saved.warnings if "work log" in w]
        await svc.create_task("demo-api", "Has an issue", tracker_ref="API-1")
        other = await svc.create_task("demo-api", "No key here", progress="All")
        saved = await svc.set_stage_checked(other.key, "done")
        assert [w for w in saved.warnings if "work log" in w]
    finally:
        await svc.close()


async def test_work_log_line_only_for_projects_with_a_tracker(service: Service) -> None:
    await service.upsert_project("acme-shop")  # no card has a tracker reference
    await service.upsert_project("demo-api")
    await service.create_task("acme-shop", "Shipped", stage="done", progress="all")
    ref = await service.create_task(
        "demo-api", "With issue", stage="done", progress="all", tracker_ref="API-1"
    )
    plain = await service.create_task("demo-api", "Without issue", stage="done", progress="all")
    inbox = await service.inbox(["owner"], require_tracker_log=True)
    assert sorted(t.key for t in inbox.not_logged) == sorted([ref.key, plain.key])
    assert inbox.lines == [("not_logged", 2)]
    assert inbox.count == 1 and inbox.badge == 0  # one line, not counted in the badge
    # A tracker reference that was removed still marks the project as keeping a tracker.
    await service.update_task(ref.key, {"tracker_ref": None})
    inbox = await service.inbox(["owner"], require_tracker_log=True)
    assert len(inbox.not_logged) == 2


async def test_ended_unlinked_sessions_are_not_listed(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.upsert_project("demo-api", repo_path=CWD)
    await service.record_hook(hook("SessionStart", cc_id=str(uuid.UUID(int=91))))
    await service.record_hook(hook("SessionStart", cc_id=str(uuid.UUID(int=92))))
    await service.record_hook(hook("SessionEnd", cc_id=str(uuid.UUID(int=92))))
    inbox = await service.inbox(["owner"])
    assert [s.cc_session_id for s in inbox.unlinked] == [str(uuid.UUID(int=91))]
    assert inbox.lines == [("unlinked", 1)] and inbox.count == 1
    assert len(await service.unlinked_hook_sessions()) == 2  # Integrations lists both


async def test_a_live_unlinked_session_behind_many_ended_ones_is_listed(service: Service) -> None:
    await service.upsert_project("acme-shop", repo_path=CWD)
    await service.upsert_project("demo-api", repo_path=CWD)
    live = str(uuid.UUID(int=500))
    await service.record_hook(hook("SessionStart", cc_id=live))
    for n in range(55):  # more than one page of 50, all seen later than the live one
        cc_id = str(uuid.UUID(int=600 + n))
        await service.record_hook(hook("SessionStart", cc_id=cc_id))
        await service.record_hook(hook("SessionEnd", cc_id=cc_id))
    inbox = await service.inbox(["owner"])
    assert [s.cc_session_id for s in inbox.unlinked] == [live]


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
    assert "waits for you" in page.text and "Nothing needs you" not in page.text
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
