"""The Audit page and `audit_list`: the journal with filters, the changes toggle, the switch on the
Sessions rows (cookie and CSRF only), and the upgrade that adds the switch."""

import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.security import csrf_token
from relaybay.service import AUDIT_CHANGE_KINDS, Service
from relaybay.web.routes import EVENT_KINDS
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_environments import SECRET, answer, bash, live_project
from tests.test_hooks import CC_A, CC_B, DESKTOP_A
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import make_old_db

FORM = {"Origin": BASE}


async def journal_two(service: Service) -> None:
    """Under the default warn mode: a read that goes on (allow), a change that warns."""
    await live_project(service)
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    await answer(service, bash("ssh prod-alias uptime"))
    await answer(service, bash(f"PGPASSWORD={SECRET} ssh prod-alias 'systemctl restart app'"))


def row_ids(text: str) -> list[int]:
    return [int(x) for x in re.findall(r'class="audit-row" data-id="(\d+)"', text)]


async def test_audit_filters(service: Service) -> None:
    await journal_two(service)
    everything = await service.audit()
    assert [e.payload["decision"] for e in everything] == ["warn", "allow"]
    assert SECRET not in everything[0].payload["command"]
    assert everything[0].payload["key"] == "acme-shop-1"

    assert [e.id for e in await service.audit(decision="allow")] == [everything[1].id]
    assert await service.audit(environment="test") == []
    assert len(await service.audit(environment="live", project="acme-shop")) == 2
    ids = await service.usage_session_ids(DESKTOP_A)
    assert len(await service.audit(session_ids=ids)) == 2
    assert await service.audit(session_ids=[ids[0] + 1000]) == []
    assert [e.id for e in await service.audit(before_id=everything[0].id)] == [everything[1].id]
    assert [e.id for e in await service.audit(after_id=everything[1].id)] == [everything[0].id]
    assert [e.id for e in await service.audit(limit=1)] == [everything[0].id]

    with pytest.raises(ValidationError, match="decision"):
        await service.audit(decision="maybe")
    with pytest.raises(ValidationError, match="days"):
        await service.audit(days=-1)
    with pytest.raises(NotFoundError):
        await service.audit(project="nope")


async def test_audit_days_limit_old_events(service: Service) -> None:
    await journal_two(service)
    await service._write_conn.execute(
        "UPDATE event SET ts = '2020-01-01T00:00:00.000+00:00' WHERE kind = 'live.action'"
    )
    assert await service.audit(days=7) == []
    assert len(await service.audit(days=0)) == 2  # events are never pruned


async def test_changes_toggle_and_switch_events(service: Service) -> None:
    await journal_two(service)
    session = (await service.list_sessions())[0].session
    assert {e.kind for e in await service.audit()} == {"live.action"}

    on = await service.set_session_no_live(session.id, True, actor="owner")
    assert on.no_live_at is not None and on.no_live_by == "owner"
    assert on.last_seen_at == session.last_seen_at  # the owner's click is not the agent's activity
    again = await service.set_session_no_live(session.id, True, actor="owner")
    assert again.no_live_at == on.no_live_at  # a repeat changes nothing and records nothing
    off = await service.set_session_no_live(session.id, False, actor="owner")
    assert off.no_live_at is None and off.no_live_by is None

    kinds = [e.kind for e in await service.audit(changes=True)]
    assert kinds.count("session.no_live_on") == 1 and kinds.count("session.no_live_off") == 1
    assert "environment.created" in kinds and "live.action" in kinds
    assert not [k for k in kinds if k not in ("live.action", *AUDIT_CHANGE_KINDS)]
    assert {e.kind for e in await service.audit()} == {"live.action"}
    with pytest.raises(NotFoundError):
        await service.set_session_no_live(session.id + 1000, True, actor="owner")


def test_every_audited_kind_is_a_known_event_kind() -> None:
    assert set(AUDIT_CHANGE_KINDS) <= set(EVENT_KINDS)
    app_js = (Path(__file__).parents[1] / "src/relaybay/web/static/app.js").read_text(
        encoding="utf-8"
    )
    for kind in AUDIT_CHANGE_KINDS:
        assert f'"{kind}"' in app_js, kind


def test_audit_page(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, journal_two, service)
    assert 'href="/audit"' in board.get("/").text
    page = board.get("/audit")
    assert page.status_code == 200
    assert re.search(r'href="/audit" aria-current="page"', page.text)
    assert "Only tool calls that touch a live stand are journaled" in page.text
    assert "No live now" not in page.text.split("</form>")[1]  # the switch lives on Sessions
    assert 'id="audit"' in page.text and 'data-live="1"' in page.text
    ids = row_ids(page.text)
    assert len(ids) == 2
    assert "ssh prod-alias uptime" in page.text and SECRET not in page.text
    assert '<a class="card-key" href="/p/acme-shop?task=acme-shop-1">' in page.text

    assert row_ids(board.get("/audit?decision=allow").text) == [ids[1]]
    assert row_ids(board.get("/audit?environment=test").text) == []
    assert "Nothing in this view." in board.get("/audit?environment=test").text
    assert row_ids(board.get(f"/fragments/audit?after={ids[1]}").text) == [ids[0]]
    assert board.get("/audit?project=nope").status_code == 404
    assert board.get("/audit?decision=maybe").status_code == 400
    assert board.get("/audit?session=abc").status_code == 400
    assert board.get("/audit?days=-1").status_code == 400
    paged = board.get(f"/audit?before={ids[0]}")
    assert row_ids(paged.text) == [ids[1]] and 'data-live="1"' not in paged.text


def test_audit_load_more_keeps_the_filters(board: TestClient, monkeypatch) -> None:
    service: Service = board.app.state.service
    monkeypatch.setattr("relaybay.web.routes.AUDIT_PAGE", 1)
    run_in_app(board, journal_two, service)
    page = board.get("/audit?days=0").text
    more = re.search(r'hx-get="(/fragments/audit\?[^"]+)"', page)
    assert more is not None and "days=0" in more.group(1)
    rest = board.get(more.group(1).replace("&amp;", "&"))
    assert len(row_ids(rest.text)) == 1
    assert "days=0" in re.search(r'data-filters="([^"]*)"', page).group(1)


def test_changes_rows_on_the_page(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, journal_two, service)
    assert "environment.created" not in board.get("/audit").text
    page = board.get("/audit?changes=1").text
    assert "environment.created" in page and 'data-changes="1"' in page


def test_switch_on_the_sessions_row(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, journal_two, service)
    session = run_in_app(board, service.list_sessions)[0].session
    url = f"/sessions/{session.id}/no-live"
    assert f'action="{url}"' in board.get("/sessions").text
    assert "No live now" in board.get("/sessions").text

    form = {"csrf_token": csrf_token(token), "on": "true", "back": "/p/acme-shop/sessions"}
    assert board.post(url, data={**form, "csrf_token": "x"}, headers=FORM).status_code == 403
    bearer = board.post(url, data=form, headers={**FORM, "Authorization": f"Bearer {token}"})
    assert bearer.status_code == 403
    assert run_in_app(board, service.list_sessions)[0].session.no_live_at is None

    response = board.post(url, data=form, headers=FORM, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/p/acme-shop/sessions"
    page = board.get("/p/acme-shop/sessions").text
    assert "Live is off for" in page and "live off" in page and "Allow live again" in page
    assert run_in_app(board, service.list_sessions)[0].session.no_live_by == "owner"

    outside = {**form, "on": "false", "back": "https://example.com/"}
    response = board.post(url, data=outside, headers=FORM, follow_redirects=False)
    assert response.headers["location"] == "/sessions"
    assert "Live is back for" in board.get("/sessions").text
    assert run_in_app(board, service.list_sessions)[0].session.no_live_at is None
    gone = board.post("/sessions/99999/no-live", data=form, headers=FORM)
    assert gone.status_code == 404


def test_switch_on_a_row_without_a_hook_link_says_so(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    run_in_app(board, service.create_task, "acme-shop", "Named", session="named-agent")
    session = run_in_app(board, service.list_sessions)[0].session
    assert session.cc_session_id is None
    form = {"csrf_token": csrf_token(token), "on": "true"}
    response = board.post(
        f"/sessions/{session.id}/no-live", data=form, headers=FORM, follow_redirects=False
    )
    assert "relaybay_flash" in response.headers["set-cookie"]
    page = board.get("/sessions")
    assert "not linked to hooks yet: this applies once it is linked." in page.text
    assert "live off: not linked to hooks yet, applies once it is linked" in page.text
    assert "any touch of a live stand is denied." not in page.text
    # Shown once, like the Inbox.
    assert "this applies once it is linked." not in board.get("/sessions").text
    assert "live off: not linked to hooks yet" in board.get("/sessions").text


def test_huge_event_ids_are_clamped(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, journal_two, service)
    huge = 10**30
    assert len(row_ids(board.get(f"/audit?before={huge}").text)) == 2
    assert row_ids(board.get(f"/audit?before=-{huge}").text) == []
    assert len(row_ids(board.get(f"/fragments/audit?before={huge}").text)) == 2
    assert row_ids(board.get(f"/fragments/audit?after={huge}").text) == []
    assert len(row_ids(board.get(f"/fragments/audit?after=-{huge}").text)) == 2


async def test_mcp_audit_list_reads_and_there_is_no_write_tool(
    live_server: Callable[[], LiveServer],
) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert "audit_list" in names and not [n for n in names if "no_live" in n]
        empty = await call(client, "audit_list")
        assert "No actions on live in the last 7 day(s)." in empty
        assert "Only tool calls that touch a live stand are journaled." in empty
        await call(client, "project_upsert", slug="acme-shop")
        refused = await client.call_tool("audit_list", {"decision": "maybe"})
        assert refused.is_error and "decision must be one of" in refused.content[0].text
        missing = await client.call_tool("audit_list", {"session": "nobody"})
        assert missing.is_error and "not found" in missing.content[0].text


async def test_mcp_format_of_a_row(service: Service) -> None:
    from relaybay.mcp_server import format as fmt

    await journal_two(service)
    text = fmt.audit(await service.audit(), 7)
    line = text.splitlines()[0]
    assert "acme-shop/live · Bash · `" in line
    assert "· warn · level read · rule remote change over ssh · acme-shop-1" in line
    assert SECRET not in text


async def test_upgrade_adds_the_switch_columns(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=15)
    with sqlite3.connect(db) as raw:
        raw.execute(
            "INSERT INTO session (name, last_seen_at, created_at) VALUES ('old', 'x', 'x')",
        )
    svc = Service(db)
    await svc.start()
    try:
        row = next(r.session for r in await svc.list_sessions() if r.session.name == "old")
        assert (row.no_live_at, row.no_live_by) == (None, None)
        saved = await svc.set_session_no_live(row.id, True, actor="owner")
        assert saved.no_live_at is not None
    finally:
        await svc.close()


async def test_second_session_keeps_its_own_switch(service: Service) -> None:
    await live_project(service)
    await answer(service, bash("ssh prod-alias uptime"))
    await answer(service, bash("ssh prod-alias uptime", CC_B))
    sessions = {r.session.cc_session_id: r.session for r in await service.list_sessions()}
    await service.set_session_no_live(sessions[CC_A].id, True, actor="owner")
    denied = await answer(service, bash("ssh prod-alias uptime"))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert await answer(service, bash("ssh prod-alias uptime", CC_B)) is None
