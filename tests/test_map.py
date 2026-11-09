"""The Map: message edges from hooks, the graph as JSON, the page and the vendored library."""

import re
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.domain.hooks import (
    LAUNCH_TEXT_MAX,
    LAUNCH_TOOLS,
    parse_launched_session,
    parse_message_recipient,
)
from relaybay.domain.map_graph import COLLAPSE_OVER, OTHER_BOX
from relaybay.security import CSP
from relaybay.service import MESSAGE_RECIPIENTS_MAX, MESSAGE_WINDOW, Service
from relaybay.web.routes import STATIC_DIR
from relaybay.web.sse import format_event
from tests.conftest import run_in_app

SECRET = "MESSAGE-TEXT-THAT-MUST-NOT-BE-KEPT"
DESKTOP = "local_00000000-0000-0000-0000-00000000000a"


def message_hook(to: object = "demo-agent", cc: str = "cc-1", **extra: object) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "session_id": cc,
        "cwd": "/d/acme",
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": SECRET, "summary": SECRET, **extra},
    }


async def messages(service: Service) -> list:
    return await service.activity(kind="session.message", limit=100)


def edges_of(graph: dict, kind: str) -> list[dict]:
    return [e for e in graph["edges"] if e["kind"] == kind]


def node(graph: dict, node_id: str) -> dict:
    return next(n for n in graph["nodes"] if n["id"] == node_id)


# --- message edges ----------------------------------------------------------


@pytest.mark.parametrize(
    ("to", "expected"),
    [
        ("demo-agent", "demo-agent"),
        ("  demo\n agent\x00 ", "demo agent"),
        ("x" * 500, "x" * 120),
        ("worker@demo-team", "worker@demo-team"),
        ("someone@example.com", None),
        ("Ann Lee <ann@example.com>", None),
        ("a@example.com, b@example.com", None),
        ("a@example.com;b@example.com", None),
        ("write to ann@example.com please", None),
        ("<script>alert(1)</script>", None),
        ("first, second", None),
        ("", None),
        (["demo-agent"], None),
        (None, None),
    ],
)
def test_recipient_is_cleaned_and_capped(to: object, expected: str | None) -> None:
    assert parse_message_recipient(message_hook(to)) == expected


def test_only_pre_tool_use_with_a_to_field_counts() -> None:
    assert parse_message_recipient({**message_hook(), "hook_event_name": "PostToolUse"}) is None
    assert parse_message_recipient({"hook_event_name": "PreToolUse", "tool_input": {}}) is None
    assert parse_message_recipient({"hook_event_name": "PreToolUse"}) is None
    for tool in ("Mail", "mcp__mail__send", "sendmessage", None):
        assert parse_message_recipient({**message_hook(), "tool_name": tool}) is None
    assert parse_message_recipient("not a dict") is None


async def test_message_event_holds_no_text(
    service: Service, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("DEBUG"):
        await service.record_hook(message_hook())
    [event] = await messages(service)
    assert event.payload == {"session": "acme", "to": "demo-agent"}
    assert SECRET not in format_event(event)
    assert SECRET not in caplog.text
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        assert SECRET not in "\n".join(conn.iterdump())
    finally:
        conn.close()
    graph = await service.map_graph()
    assert SECRET not in str(graph)


async def test_one_event_per_pair_per_minute(service: Service) -> None:
    for _ in range(5):
        await service.record_hook(message_hook())
    assert len(await messages(service)) == 1
    await service.record_hook(message_hook("other-agent"))
    assert len(await messages(service)) == 2
    await service.record_hook(message_hook(cc="cc-2") | {"cwd": "/d/other"})
    assert len(await messages(service)) == 3


async def test_window_passing_writes_again(service: Service) -> None:
    await service.record_hook(message_hook())
    session = (await service.list_sessions())[0].session
    service._message_written[session.id]["demo-agent"] -= MESSAGE_WINDOW + 1
    await service.record_hook(message_hook())
    assert len(await messages(service)) == 2


async def test_a_sender_has_a_ceiling_of_recipients(service: Service) -> None:
    for n in range(MESSAGE_RECIPIENTS_MAX + 30):
        await service.record_hook(message_hook(f"peer-{n}"))
    assert len(await messages(service)) == MESSAGE_RECIPIENTS_MAX
    session = (await service.list_sessions())[0].session
    assert len(service._message_written[session.id]) == MESSAGE_RECIPIENTS_MAX
    # Once the window passed there is room again.
    for name in service._message_written[session.id]:
        service._message_written[session.id][name] -= MESSAGE_WINDOW + 1
    await service.record_hook(message_hook("after-the-window"))
    assert len(await messages(service)) == MESSAGE_RECIPIENTS_MAX + 1


async def test_message_memo_has_a_hard_cap(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("relaybay.service.HOOK_MEMO_MAX", 3)
    for n in range(6):
        await service.record_hook(message_hook(cc=f"cc-{n}") | {"cwd": f"/d/s{n}"})
    assert len(service._message_written) == 3
    assert len(await messages(service)) == 6


async def test_other_tools_write_no_message(service: Service) -> None:
    await service.record_hook(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "cc-1",
            "cwd": "/d/acme",
            "tool_name": "Edit",
            "tool_input": {"file_path": "a.py"},
        }
    )
    assert await messages(service) == []


async def test_failed_message_write_never_stops_the_hook(
    service: Service, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr(service, "_record_message", broken)
    assert await service.record_hook(message_hook()) is not None
    assert "message edge failed" in caplog.text


# --- the graph --------------------------------------------------------------


async def seed(service: Service) -> str:
    await service.upsert_project("acme-shop", name="Acme Shop", repo_path="/d/acme")
    await service.upsert_project("demo-api", name="Demo API")
    task = await service.create_task(
        "acme-shop", "Checkout", stage="dev", session=DESKTOP, session_title="checkout"
    )
    await service.create_task("demo-api", "Limits", waiting_on="owner")
    done = await service.create_task("acme-shop", "Old thing", stage="dev")
    await service.set_stage(done.key, "done")
    return task.key


async def test_structure_nodes_and_edges(service: Service) -> None:
    key = await seed(service)
    await service.add_owner_comment(key, "Please rename the button")
    env = await service.save_environment(
        "acme-shop", {"name": "prod", "kind": "live"}, confirm_live=True
    )

    graph = await service.map_graph(view="structure")
    assert graph["view"] == "structure" and graph["minutes"] == 60 and not graph["collapsed"]
    kinds = {n["kind"] for n in graph["nodes"]}
    assert kinds == {"owner", "project", "task", "session", "environment"}
    # Roles the data does not hold are not invented.
    assert not {"coordinator", "manager", "worker", "intake"} & {str(k) for k in kinds}

    assert node(graph, f"task:{key}")["stage"] == "dev"
    assert node(graph, "task:demo-api-1")["waiting_owner"] is True
    assert "task:acme-shop-2" not in [n["id"] for n in graph["nodes"]]  # done: not open
    session = next(n for n in graph["nodes"] if n["kind"] == "session")
    assert session["label"] == "checkout" and session["project"] == "acme-shop"
    assert session["unread"] == 1
    assert node(graph, "owner")["waiting_count"] == 1

    assert [e["target"] for e in edges_of(graph, "binding")] == [f"task:{key}"]
    [comment] = edges_of(graph, "comment")
    assert (comment["source"], comment["target"], comment["count"]) == ("owner", f"task:{key}", 1)
    [access] = edges_of(graph, "access")
    assert access["level"] == "read"  # live is read-only by default
    assert access["target"] == f"environment:{env.id}"
    environment = node(graph, f"environment:{env.id}")
    assert (environment["env_kind"], environment["level"]) == ("live", "read")
    assert all(not e["recent"] for e in graph["edges"])


async def test_environment_node_shows_no_access_details(service: Service) -> None:
    await seed(service)
    env = await service.save_environment(
        "acme-shop",
        {"name": "prod", "kind": "live", "base_url": "https://shop.example.com"},
        confirm_live=True,
    )
    graph = await service.map_graph(view="structure")
    item = node(graph, f"environment:{env.id}")
    assert "shop.example.com" not in str(graph)
    assert set(item) == {"id", "kind", "label", "project", "env_kind", "level", "href"}


async def test_session_nodes_keep_paths_out(service: Service, tmp_path: Path) -> None:
    await service.record_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": "cc-1",
            "cwd": "/very/private/acme",
            "transcript_path": str(tmp_path / "t.jsonl"),
        }
    )
    for view in ("agents", "structure"):
        graph = str(await service.map_graph(view=view))
        assert "/very/private" not in graph and "t.jsonl" not in graph and "cc-1" not in graph


async def test_message_edges_resolve_known_sessions(service: Service) -> None:
    await seed(service)
    await service.record_hook(message_hook("checkout", cc="cc-a") | {"cwd": "/d/x"})
    await service.record_hook(message_hook("nobody-known", cc="cc-a") | {"cwd": "/d/x"})
    await service.record_hook(message_hook(DESKTOP, cc="cc-b") | {"cwd": "/d/y"})
    graph = await service.map_graph(view="structure")
    edges = edges_of(graph, "message")
    assert len(edges) == 3
    desktop = next(n for n in graph["nodes"] if n.get("label") == "checkout")
    targets = {e["target"] for e in edges}
    assert desktop["id"] in targets and "peer:nobody-known" in targets
    peer = node(graph, "peer:nobody-known")
    assert peer["kind"] == "session" and peer["external"] is True
    assert all(e["count"] == 1 and e["last_at"] for e in edges)


async def test_now_view_marks_recent_edges(service: Service) -> None:
    key = await seed(service)
    await service.record_hook(message_hook("checkout", cc="cc-a") | {"cwd": "/d/x"})
    await service.add_owner_comment(key, "Look at this")
    await service.add_report(key, "changed · S · first pass", session=DESKTOP)

    now = await service.map_graph(view="now", minutes=5)
    assert now["view"] == "now" and now["minutes"] == 5
    assert all(e["recent"] for e in edges_of(now, "message"))
    assert all(e["recent"] for e in edges_of(now, "comment"))
    assert all(e["recent"] for e in edges_of(now, "binding"))
    assert not any(e["recent"] for e in edges_of(now, "hierarchy"))

    # Old rows are left outside a short window.
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute("UPDATE event SET ts = '2020-01-01T00:00:00.000+00:00'")
        conn.execute("UPDATE comment SET created_at = '2020-01-01T00:00:00.000+00:00'")
        conn.commit()
    finally:
        conn.close()
    old = await service.map_graph(view="now", minutes=5)
    assert not any(e["recent"] for e in old["edges"])
    assert edges_of(old, "message") == []  # older than the week messages are kept on the map
    assert edges_of(old, "comment")  # still a comment on the task


async def test_project_filter(service: Service) -> None:
    key = await seed(service)
    graph = await service.map_graph(view="structure", project="acme-shop")
    assert graph["project"] == "acme-shop"
    ids = {n["id"] for n in graph["nodes"]}
    assert f"task:{key}" in ids and "project:acme-shop" in ids
    assert "project:demo-api" not in ids and "task:demo-api-1" not in ids
    # Every edge has both ends: the page would refuse it otherwise.
    for edge in graph["edges"]:
        assert edge["source"] in ids and edge["target"] in ids


async def test_every_edge_has_both_ends(service: Service) -> None:
    key = await seed(service)
    await service.add_owner_comment(key, "Please look")
    await service.save_environment("acme-shop", {"name": "prod", "kind": "live"}, confirm_live=True)
    await service.record_hook(message_hook("checkout", cc="cc-a") | {"cwd": "/d/acme"})
    graph = await service.map_graph(view="now")
    ids = {n["id"] for n in graph["nodes"]}
    assert len(ids) == len(graph["nodes"])
    assert len({e["id"] for e in graph["edges"]}) == len(graph["edges"])
    for edge in graph["edges"]:
        assert edge["source"] in ids and edge["target"] in ids


async def test_large_graph_starts_collapsed(service: Service) -> None:
    await service.upsert_project("acme-shop")
    for n in range(COLLAPSE_OVER):
        await service.create_task("acme-shop", f"Task {n}")
    assert (await service.map_graph(view="structure"))["collapsed"] is True


async def test_bad_parameters(service: Service) -> None:
    from relaybay.domain.errors import NotFoundError, ValidationError

    with pytest.raises(ValidationError):
        await service.map_graph(view="sideways")
    with pytest.raises(ValidationError):
        await service.map_graph(minutes=0)
    with pytest.raises(NotFoundError):
        await service.map_graph(project="nope")


# --- the Agents view: who started whom --------------------------------------

CHILD = "local_00000000-0000-0000-0000-0000000000c1"
OTHER_CHILD = "local_00000000-0000-0000-0000-0000000000c2"


def launch_hook(response: object, cc: str = "cc-coord", **extra: object) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": cc,
        "cwd": "/d/coordinator",
        "tool_name": LAUNCH_TOOLS[0],
        "tool_input": {"prompt": SECRET},
        "tool_response": response,
        **extra,
    }


@pytest.mark.parametrize(
    "response",
    [
        f"Started {CHILD} ({SECRET})",
        f"Started {CHILD.upper()}",
        {"session": {"id": CHILD}, "note": SECRET},
        [{"type": "text", "text": f"Session {CHILD} started"}],
        [{"type": "text", "text": "no id here"}, {"type": "text", "text": CHILD}],
        f"{CHILD} then {OTHER_CHILD}",
    ],
)
def test_launched_session_is_read_from_any_response_shape(response: object) -> None:
    assert parse_launched_session(launch_hook(response)) == CHILD


@pytest.mark.parametrize(
    "payload",
    [
        launch_hook("no session id"),
        launch_hook(None),
        launch_hook(42),
        launch_hook("local_zzzzzzzz-0000-0000-0000-000000000000"),
        launch_hook("local_------------------------------------"),
        launch_hook(f"{CHILD}", hook_event_name="PreToolUse"),
        launch_hook(f"{CHILD}", tool_name="SendMessage"),
        launch_hook(f"{CHILD}", tool_name="mcp__other__start_session"),
        launch_hook("x" * LAUNCH_TEXT_MAX + CHILD),
        "not a dict",
    ],
)
def test_no_launch_without_the_tool_and_an_id(payload: object) -> None:
    assert parse_launched_session(payload) is None


async def launches(service: Service) -> list:
    return await service.activity(kind="session.launched", limit=100)


async def test_launch_event_holds_the_child_id_only(service: Service) -> None:
    await service.record_hook(launch_hook({"text": f"Started {CHILD}", "prompt": SECRET}))
    [event] = await launches(service)
    assert event.payload == {"session": "coordinator", "child": CHILD}
    assert SECRET not in format_event(event)
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        assert SECRET not in "\n".join(conn.iterdump())
    finally:
        conn.close()


async def test_failed_launch_write_never_stops_the_hook(
    service: Service, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(*_: object, **__: object) -> None:
        raise RuntimeError("no")

    monkeypatch.setattr(service, "_record_launch", broken)
    assert await service.record_hook(launch_hook(CHILD)) is not None
    assert "session launch failed" in caplog.text


async def test_agents_is_the_default_view(service: Service) -> None:
    await seed(service)
    await service.save_environment("acme-shop", {"name": "prod", "kind": "live"}, confirm_live=True)
    graph = await service.map_graph()
    assert graph["view"] == "agents"
    kinds = {n["kind"] for n in graph["nodes"]}
    assert kinds == {"owner", "project", "session"}  # no task or environment nodes
    session = next(n for n in graph["nodes"] if n.get("label") == "checkout")
    assert session["parent"] == "project:acme-shop"
    assert session["task_line"] == "acme-shop-1 · dev"
    assert session["tasks"] == [
        {
            "key": "acme-shop-1",
            "title": "Checkout",
            "stage": "dev",
            "href": "/p/acme-shop?task=acme-shop-1",
        }
    ]
    assert node(graph, "project:acme-shop")["label"] == "Acme Shop"


async def test_a_launched_child_resolves_now_or_later(service: Service) -> None:
    await seed(service)  # DESKTOP is known: the checkout task's session
    await service.record_hook(launch_hook(f"Started {DESKTOP}"))
    await service.record_hook(launch_hook([{"type": "text", "text": CHILD}]))
    await service.record_hook(launch_hook([{"type": "text", "text": CHILD}]))  # a replay
    graph = await service.map_graph()
    coordinator = next(n for n in graph["nodes"] if n.get("label") == "coordinator")
    assert coordinator["parent"] == OTHER_BOX
    assert node(graph, OTHER_BOX)["label"] == "Other"
    checkout = next(n for n in graph["nodes"] if n.get("label") == "checkout")
    pending = node(graph, f"launch:{CHILD}")
    assert pending["pending"] is True and pending["parent"] == OTHER_BOX
    assert pending["label"] == "local_00000000…"
    edges = edges_of(graph, "launched")
    assert {(e["source"], e["target"]) for e in edges} == {
        (coordinator["id"], checkout["id"]),
        (coordinator["id"], f"launch:{CHILD}"),
    }
    assert checkout["launched_by"] == "coordinator"

    # The child appears with its desktop id: the same edge now reaches its session.
    await service.create_task("demo-api", "Docs", session=CHILD, session_title="docs")
    graph = await service.map_graph()
    docs = next(n for n in graph["nodes"] if n.get("label") == "docs")
    assert f"launch:{CHILD}" not in {n["id"] for n in graph["nodes"]}
    assert (coordinator["id"], docs["id"]) in {
        (e["source"], e["target"]) for e in edges_of(graph, "launched")
    }
    assert docs["parent"] == "project:demo-api" and docs["launched_by"] == "coordinator"


async def test_agents_messages_and_comments(service: Service) -> None:
    key = await seed(service)
    for _ in range(3):
        await service.record_hook(message_hook("checkout", cc="cc-a") | {"cwd": "/d/x"})
        service._message_written.clear()
    await service.record_hook(message_hook("nobody-known", cc="cc-a") | {"cwd": "/d/x"})
    await service.add_owner_comment(key, "Please rename the button")
    await service.add_owner_comment(key, "And the title")
    graph = await service.map_graph()
    checkout = next(n for n in graph["nodes"] if n.get("label") == "checkout")
    [desktop] = [e for e in edges_of(graph, "message") if e["target"] == checkout["id"]]
    assert desktop["count"] == 3 and len(desktop["moments"]) == 3
    assert desktop["moments"] == sorted(desktop["moments"], reverse=True)
    assert node(graph, "peer:nobody-known")["external"] is True
    [comment] = edges_of(graph, "comment")
    assert (comment["source"], comment["target"]) == ("owner", checkout["id"])
    assert comment["count"] == 2 and comment["tasks"] == [key] and len(comment["moments"]) == 2
    assert "Please rename" not in str(graph)


async def test_agents_edges_older_than_a_week_are_left_out(service: Service) -> None:
    key = await seed(service)
    await service.record_hook(message_hook("checkout", cc="cc-a") | {"cwd": "/d/x"})
    await service.record_hook(launch_hook(DESKTOP))
    await service.add_owner_comment(key, "Old note")
    conn = sqlite3.connect(service.db_path)
    try:
        conn.execute("UPDATE event SET ts = '2020-01-01T00:00:00.000+00:00'")
        conn.execute("UPDATE comment SET created_at = '2020-01-01T00:00:00.000+00:00'")
        conn.commit()
    finally:
        conn.close()
    graph = await service.map_graph()
    assert graph["edges"] == []


async def test_agents_filter_keeps_linked_sessions_faded(service: Service) -> None:
    await seed(service)
    await service.create_task("demo-api", "Docs", session=CHILD, session_title="docs")
    await service.create_task("demo-api", "Alone", session=OTHER_CHILD, session_title="alone")
    await service.record_hook(launch_hook(CHILD))  # coordinator (no project) -> docs
    await service.record_hook(message_hook(DESKTOP, cc="cc-coord") | {"cwd": "/d/coordinator"})
    graph = await service.map_graph(project="demo-api")
    assert graph["project"] == "demo-api"
    labels = {n.get("label"): n for n in graph["nodes"]}
    assert labels["docs"]["faded"] is False and labels["alone"]["faded"] is False
    assert labels["coordinator"]["faded"] is True  # linked to docs
    assert "checkout" not in labels  # only linked to the coordinator, another outsider
    assert node(graph, "project:demo-api")["faded"] is False
    assert node(graph, OTHER_BOX)["faded"] is True
    assert "project:acme-shop" not in {n["id"] for n in graph["nodes"]}
    [edge] = graph["edges"]
    assert edge["kind"] == "launched"


async def test_agents_graph_is_well_formed(service: Service, tmp_path: Path) -> None:
    key = await seed(service)
    await service.record_hook(
        {
            "hook_event_name": "SessionStart",
            "session_id": "cc-coord",
            "cwd": "/very/private/coordinator",
            "transcript_path": str(tmp_path / "t.jsonl"),
        }
    )
    await service.record_hook(launch_hook(DESKTOP, cwd="/very/private/coordinator"))
    await service.record_hook(launch_hook(CHILD, cwd="/very/private/coordinator"))
    await service.record_hook(message_hook("nobody", cc="cc-coord"))
    await service.add_owner_comment(key, "Look")
    for project in (None, "acme-shop"):
        graph = await service.map_graph(project=project)
        text = str(graph)
        assert "/very/private" not in text and "t.jsonl" not in text and "cc-coord" not in text
        ids = [n["id"] for n in graph["nodes"]]
        assert len(ids) == len(set(ids))
        assert len({e["id"] for e in graph["edges"]}) == len(graph["edges"])
        for item in graph["nodes"]:
            assert item.get("parent") is None or item["parent"] in ids
        for edge in graph["edges"]:
            assert edge["source"] in ids and edge["target"] in ids


# --- HTTP -------------------------------------------------------------------


def test_graph_needs_the_cookie(client: TestClient) -> None:
    assert client.get("/map/graph").status_code == 401
    page = client.get("/map", headers={"Accept": "text/html"}, follow_redirects=False)
    assert page.status_code == 303 and "/login" in page.headers["location"]


def test_graph_json(board: TestClient, seeded: str) -> None:
    response = board.get("/map/graph?view=now&minutes=30&project=acme-shop")
    assert response.status_code == 200 and response.headers["content-type"].startswith(
        "application/json"
    )
    graph = response.json()
    assert (graph["view"], graph["minutes"], graph["project"]) == ("now", 30, "acme-shop")
    assert f"task:{seeded}" in [n["id"] for n in graph["nodes"]]
    assert board.get("/map/graph?view=sideways").status_code == 400
    assert board.get("/map/graph?minutes=0").status_code == 400
    assert board.get("/map/graph?project=nope").status_code == 404


def test_map_page(board: TestClient, seeded: str) -> None:
    page = board.get("/map?view=now&minutes=15&project=acme-shop")
    assert page.status_code == 200
    assert "/static/cytoscape.min.js?v=" in page.text and "/static/map.js?v=" in page.text
    assert 'data-graph-url="/map/graph?view=now&amp;minutes=15&amp;project=acme-shop"' in page.text
    assert 'id="__________cytoscape_stylesheet"' in page.text
    assert 'href="/map"' in page.text and 'aria-current="page"' in page.text
    assert board.get("/map?view=sideways").status_code == 400
    plain = board.get("/map").text
    assert '<option value="agents" selected>Agents</option>' in plain
    assert 'data-view="agents"' in plain and "started a session" in plain
    assert 'name="minutes"' not in plain  # the Agents view has no time window
    # The library is loaded only where it is used.
    assert "cytoscape" not in board.get("/inbox").text


def test_message_event_reaches_activity_filter(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.record_hook, message_hook())
    page = board.get("/activity?kind=session.message")
    assert page.status_code == 200 and "acme → demo-agent" in page.text
    assert SECRET not in page.text


# --- the vendored library ---------------------------------------------------


def test_cytoscape_is_vendored_with_its_notice() -> None:
    text = (STATIC_DIR / "cytoscape.min.js").read_text(encoding="utf-8")
    assert text.startswith("/**")
    assert "Copyright (c) 2016-2026, The Cytoscape Consortium." in text[:200]
    assert "Permission is hereby granted, free of charge" in text[:400]
    assert '"3.34.4"' in text


def test_cytoscape_needs_nothing_the_csp_forbids() -> None:
    assert "unsafe" not in CSP
    text = (STATIC_DIR / "cytoscape.min.js").read_text(encoding="utf-8")
    assert not re.search(r"\beval\(|new Function\(", text)
    assert 'setAttribute("style"' not in text and "cssText" not in text
    # Its only inline style: skipped when an element with this id exists (map.html has one).
    assert '"__________cytoscape_stylesheet"' in text


def test_map_js_loads_nothing_from_outside() -> None:
    text = (STATIC_DIR / "map.js").read_text(encoding="utf-8")
    assert "http://" not in text and "https://" not in text
    assert "innerHTML" not in text


def test_demo_has_messages_and_usage(tmp_path: Path) -> None:
    import asyncio

    from relaybay.demo import create_demo

    db = tmp_path / "relaybay.sqlite"
    create_demo(db)

    async def read() -> tuple[dict, object]:
        service = Service(db)
        await service.start()
        try:
            agents = await service.map_graph()
            return await service.map_graph(view="now"), await service.usage_report(), agents
        finally:
            await service.close()

    graph, report, agents = asyncio.run(read())
    # The coordinator started two sessions: one known before, one bound to a task after.
    coordinator = next(n for n in agents["nodes"] if n.get("label") == "coordinator")
    started = [e["target"] for e in edges_of(agents, "launched")]
    assert len(started) == 2 and all(
        e["source"] == coordinator["id"] for e in edges_of(agents, "launched")
    )
    assert {node(agents, t)["parent"] for t in started} == {"project:acme-shop", "project:demo-api"}
    assert len(edges_of(agents, "comment")) == 1
    # Four desktop edges, two more from the demo's session messages.
    assert len(edges_of(graph, "message")) == 6
    assert any(e["recent"] for e in edges_of(graph, "message"))
    assert report.total.messages > 20
    assert len(report.periods) == 14
    assert len(report.by_session) == 3 and len(report.by_model) == 2
