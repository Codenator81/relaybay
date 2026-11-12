"""Project documents in the database (design 0011): kinds, editing on the board with conflicts,
a task's document in the task dialog, links to sections, documents that live in a repository,
and the upgrade to 0020."""

import re
import sqlite3
from importlib import resources
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain import memory as mem
from relaybay.domain.errors import ValidationError
from relaybay.markdown_export import gather
from relaybay.markdown_import import make_plan
from relaybay.security import csrf_token
from relaybay.service import Service
from relaybay.web.markdown import render_document
from relaybay.web.routes import http_url
from tests.conftest import BASE, run_in_app
from tests.test_migrations import LATEST, NOW

FORM = {"Origin": BASE}
SPEC = """# Acme spec

## Checkout
Pay by card.

## Gift cards
Partial payments.
"""
REPO_URL = "https://git.example.com/acme-shop/blob/main/docs/deploy.md"


def write_doc(board: TestClient, doc: str, body: str, base: int = 0, **options: object):
    service: Service = board.app.state.service
    return run_in_app(
        board, service.update_document, "acme-shop", doc, body, "test", base, **options
    )


@pytest.fixture
def spec(board: TestClient, seeded: str) -> str:
    write_doc(board, "spec", SPEC, kind="spec")
    return seeded


def edit_form(token: str, **fields: str) -> dict[str, str]:
    return {"csrf_token": csrf_token(token), "reason": "owner edit", **fields}


# --- kinds ------------------------------------------------------------------------------------


def test_documents_are_grouped_by_kind_known_kinds_first() -> None:
    def doc(slug: str, kind: str) -> mem.Document:
        return mem.Document(1, 1, "acme-shop", slug, slug, kind, 1, NOW, NOW)

    items = [doc("zeta", "notes"), doc("t1", "task"), doc("s", "spec"), doc("a", "alpha")]
    groups = mem.group_documents(items)
    assert [kind for kind, _ in groups] == ["spec", "task", "alpha", "notes"]


def test_the_docs_page_groups_documents_by_kind(board: TestClient, spec: str) -> None:
    write_doc(board, "task-gift-cards", "# Gift cards task\n\nNotes.", kind="task")
    write_doc(board, "glossary", "# Glossary\n\nWords.", kind="glossary")
    page = board.get("/p/acme-shop/docs").text
    kinds = re.findall(r'<h3 class="doc-kind">(\w+)', page)
    assert kinds == ["spec", "task", "glossary"]
    assert 'action="/p/acme-shop/docs/new"' in page and "New document" in page


# --- editing on the board -----------------------------------------------------------------------


def test_edit_the_whole_document(board: TestClient, token: str, spec: str) -> None:
    page = board.get("/p/acme-shop/docs/spec")
    assert 'href="/p/acme-shop/docs/spec/edit"' in page.text
    assert 'href="/p/acme-shop/docs/spec/edit?section=gift-cards"' in page.text
    form = board.get("/p/acme-shop/docs/spec/edit").text
    assert 'name="base_version" value="1"' in form and "Pay by card." in form
    saved = board.post(
        "/p/acme-shop/docs/spec/edit",
        data=edit_form(token, base_version="1", body=SPEC + "\n## Search\nFast.\n"),
        headers=FORM,
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/p/acme-shop/docs/spec"
    service: Service = board.app.state.service
    found, text = run_in_app(board, service.get_document, "acme-shop", "spec")
    assert (found.version, found.author) == (2, "owner") and "## Search" in text
    _, versions = run_in_app(board, service.document_history, "acme-shop", "spec")
    assert versions[0].reason == "owner edit"
    assert "Saved as version 2." in board.get(saved.headers["location"]).text


def test_an_older_version_has_no_edit_link(board: TestClient, spec: str) -> None:
    write_doc(board, "spec", SPEC + "\nMore.\n", base=1)
    old = board.get("/p/acme-shop/docs/spec", params={"version": 1}).text
    assert "/docs/spec/edit" not in old


def test_edit_one_section(board: TestClient, token: str, spec: str) -> None:
    form = board.get("/p/acme-shop/docs/spec/edit", params={"section": "Gift-Cards"}).text
    assert 'name="section" value="gift-cards"' in form
    assert "Partial payments." in form and "Pay by card." not in form
    saved = board.post(
        "/p/acme-shop/docs/spec/edit",
        data=edit_form(token, base_version="1", section="gift-cards", body="Whole payments."),
        headers=FORM,
        follow_redirects=False,
    )
    assert saved.headers["location"] == "/p/acme-shop/docs/spec#gift-cards"
    service: Service = board.app.state.service
    _, text = run_in_app(board, service.get_document, "acme-shop", "spec")
    assert "## Gift cards\nWhole payments." in text and "Pay by card." in text
    _, versions = run_in_app(board, service.document_history, "acme-shop", "spec")
    assert versions[0].section == "gift-cards"
    missing = board.get("/p/acme-shop/docs/spec/edit", params={"section": "nope"})
    assert missing.status_code == 400 and "No section" in missing.text


def test_a_conflict_keeps_the_typed_text_and_shows_the_current_one(
    board: TestClient, token: str, spec: str
) -> None:
    write_doc(board, "spec", SPEC.replace("Partial payments.", "Agent wrote this."), base=1)
    typed = "My owner text."
    conflict = board.post(
        "/p/acme-shop/docs/spec/edit",
        data=edit_form(token, base_version="1", section="gift-cards", body=typed),
        headers=FORM,
    )
    assert conflict.status_code == 409
    assert "is at version 2, not 1" in conflict.text
    assert f">{typed}</textarea>" in conflict.text  # the typed text is kept
    assert 'class="doc-current-text">## Gift cards\nAgent wrote this.</pre>' in conflict.text
    assert 'name="base_version" value="2"' in conflict.text  # the next save merges onto v2
    assert 'value="owner edit"' in conflict.text
    service: Service = board.app.state.service
    found, _ = run_in_app(board, service.get_document, "acme-shop", "spec")
    assert found.version == 2


def test_a_refused_save_keeps_the_text(board: TestClient, token: str, spec: str) -> None:
    refused = board.post(
        "/p/acme-shop/docs/spec/edit",
        data={**edit_form(token, base_version="1", body="Kept text."), "reason": " "},
        headers=FORM,
    )
    assert refused.status_code == 400 and "reason must say" in refused.text
    assert ">Kept text.</textarea>" in refused.text
    assert 'name="base_version" value="1"' in refused.text  # not moved: no merge was shown


def test_a_conflict_reads_the_document_once(
    board: TestClient, token: str, spec: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_doc(board, "spec", SPEC + "\nMore.\n", base=1)
    service: Service = board.app.state.service
    reads = []
    original = service.get_document

    async def counting(*args, **kwargs):
        reads.append(args)
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "get_document", counting)
    conflict = board.post(
        "/p/acme-shop/docs/spec/edit",
        data=edit_form(token, base_version="1", body="Mine."),
        headers=FORM,
    )
    assert conflict.status_code == 409 and len(reads) == 1
    assert "is at version 2, not 1" in conflict.text and "Current text, version 2" in conflict.text
    assert 'name="base_version" value="2"' in conflict.text and "More." in conflict.text


def test_a_conflict_on_a_section_that_is_gone_edits_the_whole_document(
    board: TestClient, token: str, spec: str
) -> None:
    write_doc(board, "spec", "# Acme spec\n\n## Checkout\nPay by card.\n", base=1)
    conflict = board.post(
        "/p/acme-shop/docs/spec/edit",
        data=edit_form(token, base_version="1", section="gift-cards", body="Typed section."),
        headers=FORM,
    )
    assert conflict.status_code == 409
    assert "Section #gift-cards is no longer in the document" in conflict.text
    assert 'name="section"' not in conflict.text and "Current text, version 2" in conflict.text
    assert 'class="doc-current-text"># Acme spec' in conflict.text
    assert ">Typed section.</textarea>" in conflict.text
    saved = board.post(
        "/p/acme-shop/docs/spec/edit",
        data=edit_form(token, base_version="2", body="# Acme spec\n\n## Gift cards\nBack.\n"),
        headers=FORM,
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/p/acme-shop/docs/spec"


def test_board_writes_need_csrf_and_the_cookie(
    board: TestClient, token: str, client: TestClient, spec: str
) -> None:
    data = edit_form(token, base_version="1", body="x")
    for url in ("/p/acme-shop/docs/spec/edit", "/p/acme-shop/docs/new"):
        bad = board.post(url, data={**data, "csrf_token": "nope"}, headers=FORM)
        assert bad.status_code == 403
        bearer = board.post(
            url, data={**data, "doc": "other"}, headers={**FORM, "Authorization": f"Bearer {token}"}
        )
        assert bearer.status_code == 403
    service: Service = board.app.state.service
    found, _ = run_in_app(board, service.get_document, "acme-shop", "spec")
    assert found.version == 1


def test_new_document_on_the_board(board: TestClient, token: str, seeded: str) -> None:
    fields = {"doc": "runbook-db", "kind": "Runbook", "title": "", "source_url": ""}
    refused = board.post(
        "/p/acme-shop/docs/new",
        data=edit_form(token, **fields, body="# DB runbook\n\nSteps.", reason=""),
        headers=FORM,
    )
    assert refused.status_code == 400 and "reason must say" in refused.text
    assert (
        "# DB runbook\n\nSteps.</textarea>" in refused.text and 'value="runbook-db"' in refused.text
    )
    made = board.post(
        "/p/acme-shop/docs/new",
        data=edit_form(token, **fields, body="# DB runbook\n\nSteps."),
        headers=FORM,
        follow_redirects=False,
    )
    assert made.status_code == 303 and made.headers["location"] == "/p/acme-shop/docs/runbook-db"
    service: Service = board.app.state.service
    found, _ = run_in_app(board, service.get_document, "acme-shop", "runbook-db")
    assert (found.title, found.kind, found.author) == ("DB runbook", "runbook", "owner")
    again = board.post(
        "/p/acme-shop/docs/new", data=edit_form(token, **fields, body="Other."), headers=FORM
    )
    assert again.status_code == 400 and "is at version 1, not 0" in again.text


@pytest.mark.parametrize(
    ("body", "wanted", "lines"),
    [
        ("Intro\n=====\n\n# Intro\n\ntext\n", ["intro"], [3]),  # setext: not a section
        ("> # Notes\n\n# Notes\n\nreal\n", ["notes"], [2]),  # in a quote: not a section
        (
            "# Steps\n\n- item\n  # Notes\n\n# Notes\n\nreal\n",
            ["steps", "notes", "notes-2"],
            [0, 3, 5],
        ),
        ("    # Code\n\n# Real\n", ["real"], [2]),  # indented code: not a section
    ],
)
def test_heading_ids_and_edit_links_follow_the_sections(
    body: str, wanted: list[str], lines: list[int]
) -> None:
    found = mem.sections(body)
    assert [(s.anchor, s.start) for s in found] == list(zip(wanted, lines, strict=True))
    html = str(render_document(body, "/e"))
    assert re.findall(r'<h\d id="([^"]*)"', html) == wanted
    assert re.findall(r'section=([^"&]*)', html) == wanted


# --- links to sections --------------------------------------------------------------------------


def test_links_in_task_texts_point_at_sections(board: TestClient, spec: str) -> None:
    service: Service = board.app.state.service
    run_in_app(
        board,
        service.update_task,
        spec,
        {
            "description": "See relaybay://doc/acme-shop/spec#gift-cards.",
            "progress": "Old: [refunds](relaybay://doc/acme-shop/spec#refunds), gone:"
            " relaybay://doc/acme-shop/nothing#x, `relaybay://doc/acme-shop/spec#checkout`",
        },
    )
    run_in_app(board, service.add_report, spec, "done · S · per relaybay://doc/acme-shop/spec")
    run_in_app(
        board,
        service.add_owner_comment,
        spec,
        "Check relaybay://doc/acme-shop/spec#Checkout",
        actor="owner",
    )
    view = board.get(f"/p/acme-shop/t/{spec}/view").text
    assert (
        '<a href="/p/acme-shop/docs/spec#gift-cards" class="doc-link">'
        "relaybay://doc/acme-shop/spec#gift-cards</a>." in view
    )
    broken = '<a href="/p/acme-shop/docs/spec" class="doc-link">refunds</a>'
    assert broken + ' <span class="doc-link-mark">no such section</span>' in view
    assert (
        'relaybay://doc/acme-shop/nothing#x <span class="doc-link-mark">no such document</span>'
        in view
    )
    assert "<code>relaybay://doc/acme-shop/spec#checkout</code>" in view  # code stays code
    assert (
        'href="/p/acme-shop/docs/spec" class="doc-link">relaybay://doc/acme-shop/spec</a>' in view
    )
    thread = board.get(f"/p/acme-shop/t/{spec}/comments").text
    assert 'href="/p/acme-shop/docs/spec#checkout" class="doc-link"' in thread
    page = board.get(f"/t/{spec}").text
    assert 'href="/p/acme-shop/docs/spec#checkout" class="doc-link"' in page


async def test_links_read_each_document_once(
    service: Service, monkeypatch: pytest.MonkeyPatch
) -> None:
    await service.upsert_project("acme-shop")
    await service.update_document("acme-shop", "spec", SPEC, "first", 0)
    calls = []
    original = repo.get_document

    async def counting(conn, project_id, slug):
        calls.append(slug)
        return await original(conn, project_id, slug)

    monkeypatch.setattr(repo, "get_document", counting)
    texts = [f"relaybay://doc/acme-shop/spec#{a}" for a in ("checkout", "gift-cards", "x")] * 5
    refs = await service.resolve_doc_links(*texts, "relaybay://doc/acme-shop/spec")
    assert calls == ["spec"]
    assert refs["relaybay://doc/acme-shop/spec#x"] == mem.DocRef(
        "/p/acme-shop/docs/spec", "no such section"
    )
    assert (
        refs["relaybay://doc/acme-shop/spec#gift-cards"].href == "/p/acme-shop/docs/spec#gift-cards"
    )


def test_the_task_dialog_shows_the_linked_section(board: TestClient, spec: str) -> None:
    service: Service = board.app.state.service
    run_in_app(
        board,
        service.update_task,
        spec,
        {"doc_path": "relaybay://doc/acme-shop/spec#gift-cards"},
    )
    view = board.get(f"/p/acme-shop/t/{spec}/view").text
    assert (
        '<a class="doc-link" href="/p/acme-shop/docs/spec#gift-cards">Acme spec · #gift-cards</a>'
        in view
    )
    panel = view.split('<details class="panel fold task-doc" data-fold="document">', 1)[1].split(
        "</details>", 1
    )[0]
    assert "Partial payments." in panel and "Pay by card." not in panel
    assert 'href="/p/acme-shop/docs/spec#history"' in panel

    run_in_app(board, service.update_task, spec, {"doc_path": "relaybay://doc/acme-shop/spec#gone"})
    view = board.get(f"/p/acme-shop/t/{spec}/view").text
    assert (
        'href="/p/acme-shop/docs/spec">Acme spec · #gone</a>'
        ' <span class="doc-link-mark">no such section</span>' in view
    )
    assert "Pay by card." in view  # the whole document instead

    run_in_app(board, service.update_task, spec, {"doc_path": "relaybay://doc/acme-shop/nope"})
    view = board.get(f"/p/acme-shop/t/{spec}/view").text
    assert "<code>relaybay://doc/acme-shop/nope</code>" in view and "no such document" in view
    assert "task-doc" not in view


@pytest.mark.parametrize(
    "doc_path",
    [
        "docs/tasks/gift-cards/TASK.md",
        "https://evil.example.com/x",
        "relaybay://doc/acme-shop/spec#a b",
    ],
)
def test_other_document_paths_stay_text(board: TestClient, spec: str, doc_path: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.update_task, spec, {"doc_path": doc_path})
    view = board.get(f"/p/acme-shop/t/{spec}/view").text
    assert f"<dt>Document</dt><dd><code>{doc_path}</code></dd>" in view
    assert "task-doc" not in view and 'class="doc-link"' not in view


# --- XSS ----------------------------------------------------------------------------------------


def test_markup_in_documents_and_links_is_escaped(
    board: TestClient, token: str, seeded: str
) -> None:
    evil = (
        "# <script>alert(1)</script>\n\n<img src=x onerror=alert(2)>\n\n"
        '[x](javascript:alert(3)) relaybay://doc/acme-shop/evil#"><script>alert(4)</script>\n'
    )
    write_doc(board, "evil", evil, title="<b>title</b>")
    page = board.get("/p/acme-shop/docs/evil").text
    assert "<script>alert" not in page and "<img src=x" not in page
    assert 'href="javascript' not in page and "&lt;b&gt;title&lt;/b&gt;" in page
    service: Service = board.app.state.service
    run_in_app(
        board,
        service.update_task,
        seeded,
        {
            "doc_path": "relaybay://doc/acme-shop/evil",
            "description": 'See relaybay://doc/acme-shop/evil#"onmouseover=alert(5) and'
            " [y](relaybay://doc/acme-shop/evil#<script>)",
        },
    )
    view = board.get(f"/p/acme-shop/t/{seeded}/view").text
    assert "<script>alert" not in view and "<img src=x" not in view
    assert (
        'href="/p/acme-shop/docs/evil" class="doc-link">relaybay://doc/acme-shop/evil</a>' in view
    )
    assert "onmouseover=alert(5)" in view and '"onmouseover' not in view.replace("&quot;", "")
    assert "&lt;b&gt;title&lt;/b&gt;" in view
    edit = board.get("/p/acme-shop/docs/evil/edit").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in edit and "<script>alert" not in edit


# --- documents that live in a repository ------------------------------------------------------


async def test_source_url_takes_http_links_only(service: Service) -> None:
    await service.upsert_project("acme-shop")
    for bad in ("javascript:alert(1)", "ftp://host/x", "file:///etc/passwd", "git.example.com/x"):
        with pytest.raises(ValidationError, match="source_url must be an http"):
            await service.update_document("acme-shop", "deploy", "-", "x", 0, source_url=bad)
    with pytest.raises(ValidationError, match="longer than 500"):
        await service.update_document(
            "acme-shop", "deploy", "-", "x", 0, source_url="https://x.example.com/" + "a" * 500
        )
    with pytest.raises(ValidationError, match="Only a document linked"):
        await service.update_document("acme-shop", "deploy", "-", "x", 0)


@pytest.mark.parametrize(
    "bad",
    [
        "https://a.example.com/\x00x",
        "https://git.example.com/x\u202etxt.md",
        "http://ex\u200bample.com",
    ],
)
async def test_links_refuse_control_and_invisible_characters(service: Service, bad: str) -> None:
    await service.upsert_project("acme-shop")
    with pytest.raises(ValidationError, match="control or invisible"):
        await service.update_document("acme-shop", "deploy", "-", "x", 0, source_url=bad)
    task = await service.create_task("acme-shop", "Gift cards")
    with pytest.raises(ValidationError, match="control or invisible"):
        await service.update_task(task.key, {"tracker_url": bad})
    assert http_url(bad) is None  # a hand-edited row is not shown as a link either
    assert http_url("https://git.example.com/x") == "https://git.example.com/x"


async def test_linking_an_existing_document_without_its_text_needs_force(
    service: Service,
) -> None:
    text = "# P\n\nText."
    await service.upsert_project("acme-shop")
    await service.update_document("acme-shop", "plain", text, "x", 0)
    with pytest.raises(ValidationError, match=r"drops the text.*force"):
        await service.update_document("acme-shop", "plain", "-", "x", 1, source_url=REPO_URL)
    kept = await service.update_document("acme-shop", "plain", text, "link", 1, source_url=REPO_URL)
    assert (kept.version, kept.source_url, kept.body) == (2, REPO_URL, text)
    emptied = await service.update_document("acme-shop", "plain", "-", "empty", 2, force=True)
    assert (emptied.version, emptied.body) == (3, "")
    await service.update_document("acme-shop", "other", "# O", "x", 0)
    forced = await service.update_document(
        "acme-shop", "other", "-", "link", 1, source_url=REPO_URL, force=True
    )
    assert (forced.source_url, forced.body) == (REPO_URL, "")


async def test_a_linked_document_changes_only_with_force(service: Service) -> None:
    await service.upsert_project("acme-shop")
    made = await service.update_document(
        "acme-shop", "deploy", "-", "in the repo", 0, kind="runbook", source_url=REPO_URL
    )
    assert (made.version, made.source_url, made.body) == (1, REPO_URL, "")
    found, text = await service.get_document("acme-shop", "deploy")
    assert (found.source_url, text) == (REPO_URL, "")
    with pytest.raises(ValidationError, match="lives in the repository: https://git"):
        await service.update_document("acme-shop", "deploy", "# Deploy\n\nCopy.", "copy", 1)
    with pytest.raises(ValidationError, match="force"):
        await service.update_document("acme-shop", "deploy", "-", "unlink", 1, source_url="-")
    copy = await service.update_document(
        "acme-shop", "deploy", "# Deploy\n\nCopy.", "copy", 1, force=True
    )
    assert (copy.version, copy.source_url) == (2, REPO_URL)
    unlinked = await service.update_document(
        "acme-shop", "deploy", "# Deploy\n\nCopy.", "now here", 2, source_url="-", force=True
    )
    assert (unlinked.version, unlinked.source_url) == (3, None)
    again = await service.update_document("acme-shop", "deploy", "# Deploy\n\nMore.", "edit", 3)
    assert again.version == 4
    with pytest.raises(ValidationError, match="Only a document linked"):
        await service.update_document("acme-shop", "deploy", "-", "empty", 4)


def test_the_board_links_to_the_repository_and_does_not_edit(
    board: TestClient, token: str, seeded: str
) -> None:
    write_doc(board, "deploy", "-", kind="runbook", title="Deploy", source_url=REPO_URL)
    page = board.get("/p/acme-shop/docs/deploy").text
    assert f'Lives in the repository: <a href="{REPO_URL}" target="_blank"' in page
    assert "/docs/deploy/edit" not in page and "doc-body" not in page
    assert "lives in the repository" in board.get("/p/acme-shop/docs").text
    edit = board.get("/p/acme-shop/docs/deploy/edit", follow_redirects=False)
    assert edit.status_code == 303 and edit.headers["location"] == "/p/acme-shop/docs/deploy"
    refused = board.post(
        "/p/acme-shop/docs/deploy/edit",
        data=edit_form(token, base_version="1", body="Board text."),
        headers=FORM,
    )
    assert refused.status_code == 400 and "lives in the repository" in refused.text
    made = board.post(
        "/p/acme-shop/docs/new",
        data=edit_form(
            token,
            doc="ops",
            kind="runbook",
            title="Ops",
            source_url="javascript:alert(1)",
            body="-",
        ),
        headers=FORM,
    )
    assert made.status_code == 400 and "source_url must be an http" in made.text


async def test_export_and_import_of_linked_documents(service: Service, tmp_path: Path) -> None:
    await service.upsert_project("acme-shop")
    await service.update_document("acme-shop", "deploy", "-", "x", 0, source_url=REPO_URL)
    await service.update_document(
        "acme-shop", "ops", "# Ops\n\nA copy.", "x", 0, kind="runbook", source_url=REPO_URL
    )
    wanted, _ = await gather(service)
    paths = sorted(p for p in wanted if p.startswith("projects/acme-shop/docs/"))
    assert paths == ["projects/acme-shop/docs/ops.md"]

    root = tmp_path / "notes"
    docs = root / "projects" / "acme-shop" / "docs"
    docs.mkdir(parents=True)
    (docs / "ops.md").write_text("# Ops\n\nChanged in the folder.\n", encoding="utf-8")
    (docs / "deploy.md").write_text("# Deploy\n\nA file.\n", encoding="utf-8")
    plan = await make_plan(service, root, overwrite=True)
    by_item = {i.item: i for i in plan.items if i.kind == "document"}
    for name in ("acme-shop/ops", "acme-shop/deploy"):
        assert by_item[name].action == "skipped"
        assert "lives in the repository" in by_item[name].detail


async def test_mcp_text_names_the_repository(service: Service) -> None:
    from relaybay.mcp_server import format as fmt

    await service.upsert_project("acme-shop")
    await service.update_document("acme-shop", "deploy", "-", "x", 0, source_url=REPO_URL)
    project, items = await service.list_documents("acme-shop")
    assert f"lives in the repository: {REPO_URL}" in fmt.documents(project, items)
    found, text = await service.get_document("acme-shop", "deploy")
    assert f"lives in the repository: {REPO_URL}" in fmt.document(found, text, None)


# --- upgrade --------------------------------------------------------------------------------


async def test_upgrade_from_0019_keeps_documents(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(
        e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name[:4] <= "0019"
    )
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 19;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 0, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO document (project_id, slug, title, kind, version, created_at, updated_at)"
            " VALUES (1, 'spec', 'Acme spec', 'spec', 1, ?, ?)",
            (NOW, NOW),
        )
        await conn.execute(
            "INSERT INTO document_version (document_id, version, body, author, reason,"
            " base_version, created_at) VALUES (1, 1, ?, 'agent', 'first', 0, ?)",
            (SPEC, NOW),
        )
        await conn.commit()
    finally:
        await conn.close()

    service = Service(db)
    await service.start()
    try:
        found, text = await service.get_document("acme-shop", "spec")
        assert (found.version, found.source_url, text) == (1, None, SPEC)
        saved = await service.update_document(
            "acme-shop", "spec", "Whole.", "edit", 1, section="checkout"
        )
        assert saved.version == 2
    finally:
        await service.close()
    conn = await connect(db)
    try:
        assert await migrate(conn) == LATEST == 40
    finally:
        await conn.close()
    with sqlite3.connect(db) as raw:
        assert raw.execute("SELECT source_url FROM document").fetchall() == [(None,)]
    raw.close()
