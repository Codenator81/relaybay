"""Memory and documents: document versions, sections and conflicts, decision supersede chains,
memory notes with candidate operations, blocks, the secret scan, full-text search (and its LIKE
fallback), the MCP tools and the Docs & rules page."""

from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.db.connection import connect
from relaybay.domain import memory as mem
from relaybay.domain.errors import ConflictError, NotFoundError, ValidationError
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import NOW, make_old_db

FORM = {"Origin": BASE}
SPEC = """# Acme spec

## Checkout
Pay by card.

```
## not a heading
```

## Gift cards
Partial payments.

### Refunds
Back to the card.

## Gift cards
A second section with the same title.
"""
# "invitations" and "invitation" in Russian, as code points (the repository is English-only).
RU_PLURAL = "".join(
    map(chr, (0x43F, 0x440, 0x438, 0x433, 0x43B, 0x430, 0x448, 0x435, 0x43D, 0x438, 0x44F))
)
RU_SINGULAR = "".join(
    map(chr, (0x43F, 0x440, 0x438, 0x433, 0x43B, 0x430, 0x448, 0x435, 0x43D, 0x438, 0x435))
)
TOKEN = "ghp_" + "a1b2c3d4e5" * 4


@pytest.fixture(params=["fts", "like"])
async def svc(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A started service with acme-shop, once with FTS5 and once forced onto the LIKE path."""
    if request.param == "like":

        async def no_fts(conn: object) -> bool:
            return False

        monkeypatch.setattr(repo, "ensure_search_index", no_fts)
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    assert service.search_fts is (request.param == "fts")
    await service.upsert_project("acme-shop")
    try:
        yield service
    finally:
        await service.close()


# --- sections -------------------------------------------------------------------------------


def test_section_anchors_follow_headings_outside_code() -> None:
    found = mem.sections(SPEC)
    assert [(s.anchor, s.level) for s in found] == [
        ("acme-spec", 1),
        ("checkout", 2),
        ("gift-cards", 2),
        ("refunds", 3),
        ("gift-cards-2", 2),
    ]
    assert mem.heading_anchor("Foo **bar**: baz_qux!") == "foo-bar-baz-qux"
    assert mem.heading_anchor("!!!") == "section"
    text = mem.section_text(SPEC, "gift-cards")
    assert text.startswith("## Gift cards\nPartial payments.") and "### Refunds" in text
    assert "second section" not in text
    assert mem.section_text(SPEC, "#Refunds") == "### Refunds\nBack to the card."
    with pytest.raises(ValidationError, match="sections: acme-spec, checkout"):
        mem.find_section(SPEC, "payments")


def test_replacing_a_section_keeps_or_renames_its_heading() -> None:
    kept = mem.replace_section(SPEC, "checkout", "Pay by card or gift card.")
    assert "## Checkout\nPay by card or gift card.\n\n## Gift cards" in kept  # the code too
    renamed = mem.replace_section(SPEC, "refunds", "### Refunds and returns\nTo the card.")
    assert "### Refunds and returns\nTo the card.\n\n## Gift cards" in renamed
    assert mem.replace_section(SPEC, "refunds", "Back to the card.") == SPEC


# --- documents ------------------------------------------------------------------------------


async def test_document_versions_sections_and_conflicts(svc: Service) -> None:
    created = await svc.update_document("acme-shop", "spec", SPEC, "First draft", 0, kind="spec")
    assert (created.version, created.title, created.kind) == (1, "Acme spec", "spec")
    with pytest.raises(ConflictError, match="version 1, not 0"):
        await svc.update_document("acme-shop", "spec", "x", "again", 0)

    second = await svc.update_document(
        "acme-shop", "spec", "Partial payments up to 100%.", "Limit", 1, section="gift-cards"
    )
    assert second.version == 2 and "## Gift cards\nPartial payments up to 100%." in second.body
    with pytest.raises(ConflictError) as stale:
        await svc.update_document("acme-shop", "spec", "x", "stale", 1, section="checkout")
    message = str(stale.value)
    assert "version 2, not 1" in message and "base_version 2" in message
    # The section to merge (its code block included), not the whole document.
    assert message.endswith("\n\n## Checkout\nPay by card.\n\n```\n## not a heading\n```")

    doc, text = await svc.get_document("acme-shop", "spec", section="gift-cards")
    assert doc.shown_version == 2 and text.startswith("## Gift cards\nPartial payments up to")
    old, text = await svc.get_document("acme-shop", "spec", version=1)
    assert old.shown_version == 1 and text == SPEC.strip()
    unchanged = await svc.update_document("acme-shop", "spec", second.body, "same", 2)
    assert unchanged.version == 2

    doc, versions = await svc.document_history("acme-shop", "spec")
    assert [(v.version, v.reason, v.section, v.base_version) for v in versions] == [
        (2, "Limit", "gift-cards", 1),
        (1, "First draft", None, 0),
    ]
    _, lines = await svc.document_diff("acme-shop", "spec", 1, 2)
    assert "-Partial payments." in lines and "+Partial payments up to 100%." in lines
    with pytest.raises(NotFoundError, match="no document 'brief'"):
        await svc.get_document("acme-shop", "brief")
    with pytest.raises(NotFoundError, match="create it with base_version 0"):
        await svc.update_document("acme-shop", "brief", "x", "y", 3)
    with pytest.raises(ValidationError, match="Invalid document name"):
        await svc.update_document("acme-shop", "Bad Name", "x", "y", 0)
    with pytest.raises(ValidationError, match="reason"):
        await svc.update_document("acme-shop", "notes", "x", " ", 0)
    events = [e.kind for e in await svc.events_since(0, "acme-shop")]
    assert events.count("document.updated") == 2


# --- decisions ------------------------------------------------------------------------------


async def test_decision_supersede_chain(svc: Service) -> None:
    await svc.upsert_project("demo-api")
    task = await svc.create_task("acme-shop", "Invites")
    _, report = await svc.add_report(task.key, "changed · S · invites\n\nOwner asked for it.")
    first = await svc.add_decision(
        "acme-shop",
        "Invites auto-accept for existing users",
        task=task.key,
        source=f"report:{report.id} owner in chat",
    )
    assert (first.scope, first.source_kind, first.source_ref) == ("task", "report", str(report.id))
    assert first.source_text == "owner in chat" and first.recorded_at == first.valid_from
    second = await svc.add_decision(
        "acme-shop", "Invites always need an explicit accept", supersedes=first.id
    )
    assert second.scope == "project" and second.current
    with pytest.raises(ConflictError, match=f"already superseded by #{second.id}"):
        await svc.add_decision("acme-shop", "Third", supersedes=first.id)
    other = await svc.add_decision("demo-api", "Rate limits per key")
    with pytest.raises(ValidationError, match="belongs to demo-api"):
        await svc.add_decision("acme-shop", "Mine now", supersedes=other.id)
    with pytest.raises(NotFoundError):
        await svc.add_decision("acme-shop", "x", supersedes=999)
    with pytest.raises(NotFoundError, match="comment #77"):
        await svc.add_decision("acme-shop", "x", source="comment:77")
    with pytest.raises(ValidationError, match="another project"):
        await svc.add_decision("demo-api", "x", source=f"report:{report.id}")

    _, current = await svc.list_decisions("acme-shop")
    assert [d.id for d in current] == [second.id]
    _, everything = await svc.list_decisions("acme-shop", include_superseded=True)
    old = next(d for d in everything if d.id == first.id)
    assert (old.superseded_by, old.valid_to) == (second.id, old.superseded_at)
    _, found = await svc.list_decisions("acme-shop", query="accepting", include_superseded=True)
    assert {d.id for d in found} == {first.id, second.id}
    _, task_only = await svc.list_decisions("acme-shop", task=task.key, include_superseded=True)
    assert [d.id for d in task_only] == [first.id]
    snapshot = await svc.snapshot("acme-shop")
    assert [d.id for d in snapshot.decisions] == [second.id]
    added = [e for e in await svc.events_since(0, "acme-shop") if e.kind == "decision.added"]
    assert added[-1].payload["supersedes"] == first.id


# --- memory notes ---------------------------------------------------------------------------


async def test_memory_add_returns_candidates_before_writing(svc: Service) -> None:
    first = await svc.add_note(
        "global", "user", "Owner prefers short reports", "Keep reports short.\nWhy: phone."
    )
    assert first.op == "add" and first.note is not None
    note_id = first.note.id

    asked = await svc.add_note("global", "user", "Owner prefers short reports always", "Short.")
    assert asked.op == "candidates" and asked.note is None
    (candidate, similarity), *_ = asked.candidates
    assert candidate.id == note_id and similarity >= mem.STRONG_MATCH
    assert len(await svc.list_notes()) == 1  # nothing written

    with pytest.raises(ConflictError, match="force_new=true"):
        await svc.add_note("global", "user", "Owner prefers short reports always", "x", op="add")
    forced = await svc.add_note(
        "global", "user", "Owner prefers short reports always", "x", op="add", force_new=True
    )
    assert forced.op == "add"

    updated = await svc.add_note(
        "global",
        "user",
        "Owner prefers short reports",
        "Short, first line only.",
        op="update",
        target=note_id,
        base_version=1,
    )
    assert updated.op == "update" and updated.note.version == 2
    with pytest.raises(ValidationError, match="base_version"):
        await svc.add_note("global", "user", "x", "y", op="update", target=note_id)
    with pytest.raises(ConflictError, match="at version 2, not 1"):
        await svc.add_note("global", "user", "x", "y", op="update", target=note_id, base_version=1)
    replaced = await svc.add_note(
        "global",
        "user",
        "Owner prefers one-line reports",
        "One line.",
        op="supersede",
        target=note_id,
    )
    assert replaced.op == "supersede"
    old = await svc.get_note(note_id)
    assert old.superseded_by == replaced.note.id and old.valid_to == old.superseded_at
    with pytest.raises(ConflictError, match=f"use #{replaced.note.id} as target"):
        await svc.add_note("global", "user", "x", "y", op="update", target=note_id, base_version=2)
    noop = await svc.add_note("global", "user", "x", "y", op="noop", target=replaced.note.id)
    assert noop.op == "noop"

    # A note of another scope is not a candidate; an unrelated title adds directly.
    other = await svc.add_note(
        "project", "project", "Owner prefers short reports", "Here too.", project="acme-shop"
    )
    assert other.op == "add" and other.note.project_slug == "acme-shop"
    with pytest.raises(ValidationError, match="needs role"):
        await svc.add_note("role", "feedback", "t", "b")
    with pytest.raises(ValidationError, match="has no project"):
        await svc.add_note("global", "user", "t", "b", project="acme-shop")
    with pytest.raises(ValidationError, match="kind must be one of"):
        await svc.add_note("global", "fact", "t", "b")
    kinds = [e.kind for e in await svc.events_since(0)]
    assert kinds.count("memory.added") == 4 and kinds.count("memory.updated") == 1


async def test_memory_update_get_and_tags(svc: Service) -> None:
    added = await svc.add_note(
        "role",
        "feedback",
        "Coordinator checks CI",
        "Wait for green CI.",
        role="Coordinator",
        tags="CI, release ci",
        review_at="2026-12-31",
        source="message:chat-7 owner",
    )
    note = added.note
    assert (note.role, note.tags, note.review_at) == ("coordinator", "ci, release", "2026-12-31")
    assert (note.source_kind, note.source_ref, note.source_text) == ("message", "chat-7", "owner")
    saved = await svc.update_note(note.id, 1, body="Wait for green CI on 3 OSes.", tags="")
    assert (saved.version, saved.tags) == (2, None)
    with pytest.raises(ConflictError) as stale:
        await svc.update_note(note.id, 1, body="x")
    assert "version 2, not 1" in str(stale.value) and "green CI on 3 OSes" in str(stale.value)
    with pytest.raises(ValidationError, match="date like"):
        await svc.update_note(note.id, 2, review_at="soon")
    read = await svc.get_note(note.id)
    read = await svc.get_note(note.id)
    assert read.read_count == 2 and read.last_read_at is not None


# --- blocks ---------------------------------------------------------------------------------


async def test_blocks_limit_versions_and_owner_only(svc: Service) -> None:
    block = await svc.set_block("owner-preferences", "Short reports.", 0, project="acme-shop")
    assert (block.version, block.char_limit, block.scope) == (1, 2000, "project")
    with pytest.raises(ValidationError, match="at most 2000 characters; this text has 2001"):
        await svc.set_block("owner-preferences", "x" * 2001, 1, project="acme-shop")
    with pytest.raises(ConflictError, match="version 1, not 0"):
        await svc.set_block("owner-preferences", "again", 0, project="acme-shop")
    with pytest.raises(NotFoundError, match="base_version 0"):
        await svc.set_block("pulse", "x", 2, project="acme-shop")
    with pytest.raises(ValidationError, match="Only the owner"):
        await svc.set_block("owner-preferences", "x", 1, project="acme-shop", owner_only=True)
    locked = await svc.set_block(
        "owner-preferences", "Short.", 1, project="acme-shop", owner_only=True, by_owner=True
    )
    assert locked.owner_only and locked.updated_by == "owner"
    with pytest.raises(ValidationError, match="owner-only"):
        await svc.set_block("owner-preferences", "Long.", 2, project="acme-shop")
    glob = await svc.set_block("house-style", "Plain English.", 0, char_limit=50)
    assert glob.scope == "global" and glob.project_slug is None
    assert [b.label for b in await svc.list_blocks()] == ["house-style"]
    assert (await svc.get_block("owner-preferences", "acme-shop")).body == "Short."
    with pytest.raises(NotFoundError, match="blocks: owner-preferences"):
        await svc.get_block("pulse", "acme-shop")


# --- secret scan ----------------------------------------------------------------------------


async def test_secrets_are_refused_everywhere(svc: Service) -> None:
    writes = [
        svc.add_note("global", "reference", "GitHub", f"Use {TOKEN} for the API."),
        svc.add_decision("acme-shop", f"Deploy with {TOKEN}"),
        svc.set_block("access", f"token: {TOKEN}", 0, project="acme-shop"),
        svc.update_document("acme-shop", "runbook", f"# Deploy\n{TOKEN}", "first", 0),
        # Not only the texts: the source id, who decided, a role and a document kind too.
        svc.add_decision("acme-shop", "Deploy on Fridays", source=f"message:{TOKEN}"),
        svc.add_note("global", "user", "Chat", "From chat.", source=f"message:{TOKEN} chat"),
        svc.add_decision("acme-shop", "Deploy on Mondays", decided_by=TOKEN),
        svc.add_note("role", "user", "Role", "Body.", role=TOKEN[:40]),
        svc.update_document("acme-shop", "notes", "# Notes", "first", 0, kind=TOKEN[:40]),
    ]
    for write in writes:
        with pytest.raises(ValidationError) as refused:
            await write
        assert "(github-token)" in str(refused.value) and TOKEN not in str(refused.value)
    with pytest.raises(ValidationError, match="secret-assignment"):
        await svc.add_note("global", "reference", "Stand", "password: hunter2hunter2")
    ok = await svc.add_note("global", "reference", "Stand", "password lives in $DB_PASSWORD")
    assert ok.op == "add"


# --- search ---------------------------------------------------------------------------------


async def test_search_word_forms_sanitising_and_scopes(svc: Service) -> None:
    await svc.upsert_project("demo-api")
    task = await svc.create_task("acme-shop", "Invites")
    await svc.add_report(task.key, "changed · S · invite flow\n\nThe invite email goes out.")
    await svc.add_owner_comment(task.key, "Please check the cafe page too")
    await svc.update_document("acme-shop", "spec", SPEC, "First", 0)
    await svc.add_decision("demo-api", "Invites for API keys are manual")
    await svc.add_note("global", "user", "Owner writes in Russian", f"{RU_PLURAL} go by mail")

    hits = await svc.search("invites", project="acme-shop")
    assert {h.kind for h in hits} == {"report"}  # demo-api's decision is another project
    report = hits[0]
    assert (report.task_key, report.line, report.snippet) == (
        task.key,
        1,
        "changed · S · invite flow",
    )
    assert {h.kind for h in await svc.search("invited")} == {"report", "decision"}
    assert [h.kind for h in await svc.search("payments", kinds=["doc"])] == ["doc"]
    doc_hit = (await svc.search("back card"))[0]
    assert (doc_hit.name, doc_hit.line) == ("spec", 14)
    assert [h.kind for h in await svc.search("cafe")] == ["comment"]
    if svc.search_fts:  # stems and diacritics need the index; LIKE matches substrings only
        assert [h.kind for h in await svc.search(RU_SINGULAR)] == ["note"]
        await svc.add_note("global", "reference", "Café page", "Menu layout.")
        assert {h.kind for h in await svc.search("cafe")} == {"comment", "note"}
    for raw in ('"invite', "invite AND (", "NEAR(x y", "title:invite*", "-invite ^"):
        assert await svc.search(raw) is not None  # never an FTS syntax error
    with pytest.raises(ValidationError, match="no words"):
        await svc.search("*** ()")
    with pytest.raises(ValidationError, match="Unknown kind"):
        await svc.search("invite", kinds=["task"])
    notes = await svc.search("russian", scope="global")
    assert [h.kind for h in notes] == ["note"]
    assert await svc.search("russian", scope="project") == []


async def test_search_index_rebuilds_on_upgrade(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=11)
    conn = await connect(db)
    try:
        await conn.execute(
            "INSERT INTO report (task_id, author, summary, body_md, created_at)"
            " VALUES (1, 'agent', 'gift cards', 'Partial refunds decided', ?)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO decision (task_id, project_id, text, decided_by, created_at)"
            " VALUES (1, 1, 'Refunds go to the card', 'owner', ?)",
            (NOW,),
        )
        await conn.execute(
            "INSERT INTO decision (project_id, text, created_at) VALUES (1, 'No coupons', ?)",
            (NOW,),
        )
    finally:
        await conn.close()

    svc = Service(db)
    await svc.start()
    try:
        detail = await svc.task_detail("acme-shop-1")
        assert [r.summary for r in detail.reports] == ["gift cards"]
        _, items = await svc.list_decisions("acme-shop")
        assert [(d.text, d.scope, d.recorded_at, d.valid_from, d.decided_by) for d in items] == [
            ("No coupons", "project", NOW, NOW, None),
            ("Refunds go to the card", "task", NOW, NOW, "owner"),
        ]
        assert {h.kind for h in await svc.search("refund")} == {"report", "decision"}
    finally:
        await svc.close()

    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA foreign_key_check") as cur:
            assert await cur.fetchall() == []
        async with conn.execute("SELECT count(*) FROM search_index") as cur:
            assert (await cur.fetchone())[0] == 3
    finally:
        await conn.close()


# --- MCP ------------------------------------------------------------------------------------


async def test_mcp_memory_tools(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        for name in ("decision_add", "memory_add", "memory_update", "doc_update", "block_set"):
            assert "hook_session" in tools[name].input_schema["properties"], name
        for name in (
            "decision_list",
            "memory_search",
            "memory_get",
            "doc_get",
            "doc_list",
            "doc_history",
            "block_get",
        ):
            assert name in tools

        await call(client, "project_upsert", slug="acme-shop")
        saved = await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="spec",
            body=SPEC,
            reason="First",
            base_version=0,
            kind="spec",
            session="spec-writer",
        )
        assert "version 1" in saved
        got = await call(client, "doc_get", project="acme-shop", doc="spec", section="refunds")
        assert "pass base_version=1" in got and "### Refunds\nBack to the card." in got
        stale = await client.call_tool(
            "doc_update",
            {"project": "acme-shop", "doc": "spec", "body": "x", "reason": "r", "base_version": 0},
        )
        assert stale.is_error and "version 1, not 0" in stale.content[0].text
        resource = await client.read_resource("relaybay://doc/acme-shop/spec")
        assert resource.contents[0].text == SPEC.strip()
        assert "spec — Acme spec (spec, version 1" in await call(
            client, "doc_list", project="acme-shop"
        )
        await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="spec",
            body="Card only.",
            reason="No gift cards",
            base_version=1,
            section="checkout",
        )
        history = await call(
            client, "doc_history", project="acme-shop", doc="spec", from_version=1, to_version=2
        )
        assert "v2" in history and "#checkout" in history and "+Card only." in history

        first = await call(
            client, "decision_add", project="acme-shop", text="Refunds go to the card"
        )
        assert "Tip: pass source" in first
        await call(
            client,
            "decision_add",
            project="acme-shop",
            text="Refunds go to the gift card",
            supersedes=1,
            source="message:chat-1 owner",
        )
        listing = await call(client, "decision_list", project="acme-shop", include_superseded=True)
        assert "superseded by #2" in listing and "source: message chat-1 · owner" in listing

        await call(
            client,
            "memory_add",
            scope="global",
            kind="user",
            title="Owner prefers short reports",
            body="Short.",
        )
        asked = await call(
            client,
            "memory_add",
            scope="global",
            kind="user",
            title="Owner prefers short reports",
            body="Shorter.",
        )
        assert "Nothing written yet" in asked and "#1" in asked and "strong match" in asked
        done = await call(
            client,
            "memory_add",
            scope="global",
            kind="user",
            title="Owner prefers short reports",
            body="Shorter.",
            op="update",
            target=1,
            base_version=1,
        )
        assert "Note #1 updated (version 2)" in done
        assert "version 3" in await call(client, "memory_update", id=1, base_version=2, tags="-")
        assert "read 1 time(s)" in await call(client, "memory_get", id=1)
        found = await call(client, "memory_search", query="report")
        assert "note #1 (memory_get)" in found and "line 1: Shorter." in found

        assert "version 1" in await call(
            client,
            "block_set",
            label="pulse",
            body="All green.",
            base_version=0,
            project="acme-shop",
        )
        assert "All green." in await call(client, "block_get", label="pulse", project="acme-shop")
        assert "- pulse" in await call(client, "block_get", project="acme-shop")
        over = await client.call_tool(
            "block_set",
            {"label": "pulse", "body": "x" * 2001, "base_version": 1, "project": "acme-shop"},
        )
        assert over.is_error and "this text has 2001" in over.content[0].text


# --- board ----------------------------------------------------------------------------------


def test_docs_page(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    run_in_app(board, service.update_document, "acme-shop", "spec", SPEC, "First", 0)
    run_in_app(
        board,
        service.update_document,
        "acme-shop",
        "spec",
        "<b>Cards</b> only.",
        "Cards",
        1,
        section="checkout",
    )
    first = run_in_app(board, service.add_decision, "acme-shop", "Refunds to the card")
    run_in_app(
        board, service.add_decision, "acme-shop", "Refunds to the gift card", supersedes=first.id
    )
    run_in_app(board, service.add_note, "global", "user", "Owner prefers short reports", "Short.")
    run_in_app(board, service.set_block, "pulse", "All green.", 0, project="acme-shop")

    page = board.get("/p/acme-shop/docs")
    assert page.status_code == 200
    html = page.text
    assert 'href="/p/acme-shop/docs" aria-current="page">Docs &amp; rules' in html
    assert 'href="/p/acme-shop/docs/spec"' in html and "version 2" in html
    assert 'superseded by <a href="#decision-2">#2</a>' in html
    assert "Owner prefers short reports" in html and "All green." in html
    assert "## Report format" not in html and "<h2>Report format</h2>" in html  # rules render
    assert 'hx-get="/p/acme-shop/docs-live"' in html
    assert board.get("/p/acme-shop/docs-live").text.count('id="docs-live"') == 1

    doc = board.get("/p/acme-shop/docs/spec").text
    assert '<h2 id="gift-cards">' in doc and '<h2 id="gift-cards-2">' in doc
    assert "&lt;b&gt;Cards&lt;/b&gt;" in doc  # raw HTML stays escaped
    assert 'href="#refunds"' in doc and "?version=1" in doc
    old = board.get("/p/acme-shop/docs/spec?version=1").text
    assert "<strong>version 1</strong> of 2" in old
    diff = board.get("/p/acme-shop/docs/spec/diff?from=1&to=2").text
    assert '<span class="diff-del">-Pay by card.</span>' in diff
    assert '<span class="diff-add">+&lt;b&gt;Cards&lt;/b&gt; only.</span>' in diff
    assert board.get("/p/acme-shop/docs/brief").status_code == 404
    assert board.get("/p/acme-shop/docs/spec/diff?from=1&to=9").status_code == 404

    found = board.get("/p/acme-shop/docs", params={"q": "refunds"}).text
    assert 'id="search"' in found and "line 1: Refunds to the gift card" in found
    assert "no words" in board.get("/p/acme-shop/docs", params={"q": "()"}).text

    form = {"csrf_token": csrf_token(token), "label": "pulse", "scope": "project"}
    refused = board.post("/p/acme-shop/docs/blocks", data={**form, "csrf_token": "x"}, headers=FORM)
    assert refused.status_code == 403
    over = board.post(
        "/p/acme-shop/docs/blocks",
        data={**form, "base_version": "1", "body": "y" * 2001, "char_limit": "2000"},
        headers=FORM,
    )
    assert over.status_code == 400 and "this text has 2001" in over.text
    assert "y" * 2001 in over.text  # the typed text is kept
    saved = board.post(
        "/p/acme-shop/docs/blocks",
        data={**form, "base_version": "1", "body": "Checkout in dev.", "owner_only": "1"},
        headers=FORM,
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"].endswith("#block-project-pulse")
    block = run_in_app(board, service.get_block, "pulse", "acme-shop")
    assert (block.body, block.owner_only, block.version) == ("Checkout in dev.", True, 2)
    new = board.post(
        "/p/acme-shop/docs/blocks",
        data={
            **form,
            "label": "house-style",
            "scope": "global",
            "base_version": "0",
            "body": "Plain English.",
        },
        headers=FORM,
        follow_redirects=False,
    )
    assert new.status_code == 303
    assert run_in_app(board, service.get_block, "house-style").scope == "global"
