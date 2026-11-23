"""Board links of documents in MCP answers, a card's document field naming a file that a file
document reads, the file document a card gets from its doc_path, and GitHub-style anchors."""

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from relaybay.config import load_settings
from relaybay.domain import doc_index
from relaybay.domain import memory as mem
from relaybay.service import Service, _path_key
from relaybay.web.markdown import render_document
from tests.conftest import LiveServer
from tests.test_mcp import call, connect

SPEC = "# Shop\n\n## 2. Orders / Returns / Refunds\nChecks.\n\n## Who pays: fee = price\nFees.\n"
TASK_MD = "# acme-shop-1 Gift cards\n\n## Plan\nSteps.\n"


@pytest.fixture
async def acme(service: Service) -> Service:
    await service.upsert_project("acme-shop", name="Acme Shop")
    return service


@pytest.fixture
def outside(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("outside")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# --- settings ---------------------------------------------------------------------------------


def test_board_link_base_follows_host_and_port(tmp_path: Path) -> None:
    settings = load_settings(home=tmp_path, port=8800, environ={})
    assert settings.board_link_base == "http://127.0.0.1:8800"
    settings = load_settings(home=tmp_path, environ={"RELAYBAY_HOST": "0.0.0.0"})
    assert settings.board_link_base == "http://127.0.0.1:8765"


def test_board_url_overrides_the_address(tmp_path: Path) -> None:
    env = {"RELAYBAY_BOARD_URL": "http://board.lan:9000/"}
    assert load_settings(home=tmp_path, environ=env).board_link_base == "http://board.lan:9000"


def test_board_url_must_be_an_origin(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('board_url = "board.lan/p"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="board_url"):
        load_settings(home=tmp_path, environ={})


# --- anchors ----------------------------------------------------------------------------------


def test_a_github_style_anchor_names_the_same_heading() -> None:
    assert mem.github_anchor("2. Orders / Returns / Refunds") == "2-orders--returns--refunds"
    assert mem.find_section(SPEC, "2-orders--returns--refunds").anchor == "2-orders-returns-refunds"
    assert mem.find_section(SPEC, "who-pays-fee--price").anchor == "who-pays-fee-price"
    rows = doc_index.build(SPEC)[0]
    hit = doc_index.lookup(rows, "#2-Orders--Returns--Refunds")
    assert hit.anchor == "2-orders-returns-refunds"
    assert doc_index.lookup(rows, "2-orders-returns") is None


def test_the_page_carries_the_github_anchor_as_an_id() -> None:
    html = str(render_document(SPEC))
    assert 'id="2-orders-returns-refunds"' in html
    assert '<span class="doc-alias" id="2-orders--returns--refunds"></span>' in html
    # A heading whose GitHub anchor equals its own gets no extra element.
    assert html.count('id="shop"') == 1


# --- the document field ---------------------------------------------------------------------


def test_path_key_folds_absolute_paths_only(tmp_path: Path) -> None:
    assert _path_key("relaybay://doc/a/b") is None
    assert _path_key("docs/TASK.md") is None
    assert _path_key(str(tmp_path / "a" / ".." / "TASK.md")) == _path_key(str(tmp_path / "TASK.md"))


async def test_a_card_naming_a_file_leads_to_its_file_document(
    acme: Service, outside: Path
) -> None:
    path = write(outside / "tasks" / "1" / "TASK.md", TASK_MD)
    await acme.update_document("acme-shop", "task-x", "-", "task", 0, source_path=str(path))
    task = await acme.create_task("acme-shop", "Gift cards", doc_path=path.as_posix())
    detail = await acme.task_detail(task.key)
    assert detail.document is not None
    assert detail.document.ref.href == "/p/acme-shop/docs/task-x"
    context = await acme.task_context(task.key)
    assert "http://127.0.0.1:8765/p/acme-shop/docs/task-x" in context


async def test_doc_path_on_a_markdown_file_creates_the_cards_document(
    acme: Service, outside: Path
) -> None:
    path = write(outside / "TASK.md", TASK_MD)
    task = await acme.create_task("acme-shop", "Gift cards", doc_path=str(path))
    note = await acme.link_task_file(task.key)
    assert note is not None and f"task-{task.key}" in note
    doc = (await acme.get_document("acme-shop", f"task-{task.key}"))[0]
    assert doc.kind == "task" and doc.is_file and not doc.file_writable
    assert path.read_text(encoding="utf-8") == TASK_MD  # the file is only read
    assert await acme.link_task_file(task.key) is None  # once linked, nothing more to do


async def test_doc_path_links_nothing_it_should_not(acme: Service, outside: Path) -> None:
    other = write(outside / "notes.txt", "x")
    task = await acme.create_task("acme-shop", "A", doc_path=str(other))
    assert await acme.link_task_file(task.key) is None  # not Markdown
    task = await acme.create_task("acme-shop", "B", doc_path="relaybay://doc/acme-shop/spec")
    assert await acme.link_task_file(task.key) is None
    missing = await acme.create_task("acme-shop", "C", doc_path=str(outside / "gone.md"))
    note = await acme.link_task_file(missing.key)
    assert note is not None and "not a board document" in note
    assert (await acme.get_task(missing.key)).doc_path == str(outside / "gone.md")


async def test_a_taken_document_name_is_left_alone(acme: Service, outside: Path) -> None:
    task = await acme.create_task("acme-shop", "Gift cards")
    await acme.update_document("acme-shop", f"task-{task.key}", "# Own text", "notes", 0)
    path = write(outside / "TASK.md", TASK_MD)
    await acme.update_task(task.key, {"doc_path": str(path)})
    note = await acme.link_task_file(task.key)
    assert note is not None and "already has a document" in note
    assert not (await acme.get_document("acme-shop", f"task-{task.key}"))[0].is_file


# --- MCP answers ------------------------------------------------------------------------------


async def test_mcp_answers_carry_board_links(
    live_server: Callable[[], LiveServer], tmp_path_factory: pytest.TempPathFactory
) -> None:
    live = live_server()
    folder = tmp_path_factory.mktemp("files")
    spec = write(folder / "spec.md", SPEC)
    task_md = write(folder / "tasks" / "TASK.md", TASK_MD)
    board = f"{live.url}/p/acme-shop/docs"
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", name="Acme Shop")
        await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="spec",
            body="-",
            reason="from its file",
            base_version=0,
            source_path=str(spec),
        )
        outline = await call(client, "doc_outline", project="acme-shop", doc="spec")
        assert f"Board: {board}/spec · a section on the board: {board}/spec#<anchor>" in outline
        read = await call(
            client, "doc_get", project="acme-shop", doc="spec", section="2-orders--returns--refunds"
        )
        assert f"board {board}/spec#2-orders-returns-refunds" in read.split("\n")[0]
        hits = await call(client, "doc_search", project="acme-shop", query="fees")
        assert f"{board}/spec#who-pays-fee-price" in hits
        assert f"· board {board}/spec" in await call(client, "doc_list", project="acme-shop")
        created = await call(
            client, "task_create", project="acme-shop", title="Gift cards", doc_path=str(task_md)
        )
        assert f"the file is now the document task-acme-shop-1, {board}/task-acme-shop-1" in (
            created
        )
        detail = await call(client, "task_get", task="acme-shop-1")
        assert f"· board {board}/task-acme-shop-1" in detail
        other = write(folder / "tasks" / "2" / "TASK.md", TASK_MD)
        await call(client, "task_create", project="acme-shop", title="Second")
        patched = await call(
            client, "task_patch", task="acme-shop-2", fields={"doc_path": str(other)}
        )
        assert "the file is now the document task-acme-shop-2" in patched


# --- edge cases -------------------------------------------------------------------------------


def test_a_github_anchor_prefers_the_heading_github_names() -> None:
    body = "## A-B\nx\n\n## A / B\ny\n"
    assert [s.anchor for s in mem.sections(body)] == ["a-b", "a-b-2"]
    assert mem.find_section(body, "a--b").title == "A / B"
    rows = doc_index.build(body)[0]
    assert doc_index.lookup(rows, "a--b").title == "A / B"
    assert doc_index.lookup(rows, "a-b").title == "A-B"


async def test_a_capped_read_with_a_long_heading_fits_its_cap(acme: Service, outside: Path) -> None:
    from relaybay.mcp_server import format as fmt

    heading = "word " * 700
    path = write(outside / "long.md", f"# T\n\n## {heading}\n" + "line\n" * 50)
    await acme.update_document("acme-shop", "long", "-", "file", 0, source_path=str(path))
    anchor = mem.heading_anchor(heading)
    read = await acme.read_document("acme-shop", "long", section=anchor, max_chars=2000)
    answer = fmt.document_read(read, "http://127.0.0.1:8765")
    assert len(answer) <= 2000
    assert "board http://127.0.0.1:8765/p/acme-shop/docs/long " in answer.split("\n")[0] + " "


async def test_a_field_through_a_link_finds_the_document(acme: Service, outside: Path) -> None:
    target = write(outside / "real" / "TASK.md", TASK_MD)
    link = outside / "linked"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target.parent), str(link))
        else:
            link.symlink_to(target.parent, target_is_directory=True)
    except OSError:
        pytest.skip("cannot make a link here")
    task = await acme.create_task("acme-shop", "Gift cards", doc_path=str(link / "TASK.md"))
    note = await acme.link_task_file(task.key)
    assert note is not None and "is now the document" in note
    assert (await acme.task_detail(task.key)).document is not None
    assert await acme.link_task_file(task.key) is None
    other = await acme.create_task("acme-shop", "Same file", doc_path=str(link / "TASK.md"))
    assert await acme.link_task_file(other.key) is None  # the same file: no second document
    detail = await acme.task_detail(other.key)
    assert detail.document is not None
    assert detail.document.link.doc == f"task-{task.key}"


async def test_an_unreadable_file_leaves_the_card_alone(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(outside / "TASK.md", TASK_MD)

    def refuse(_: str) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(doc_index, "read_file", refuse)
    task = await acme.create_task("acme-shop", "Gift cards", doc_path=str(path))
    note = await acme.link_task_file(task.key)
    assert note is not None and "not a board document" in note
