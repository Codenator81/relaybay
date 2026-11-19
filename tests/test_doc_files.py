"""Project knowledge (design 0026, part A): file documents, the section index, explicit anchors,
doc_outline, doc_search, capped reads and line ranges, the card's pointer to a section, exact
section and line writes, and byte-identical import and export."""

import hashlib
import os
import random
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.domain import doc_index
from relaybay.domain import memory as mem
from relaybay.domain.environments import find_secrets, secret_spans
from relaybay.domain.errors import ConflictError, ValidationError
from relaybay.domain.markdown_layout import file_style
from relaybay.markdown_export import gather
from relaybay.markdown_import import apply_plan, make_plan
from relaybay.mcp_server import format as fmt
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_mcp import call, connect

PARTS = 20
ITEMS = 3
# Neutral filler: a shop's catalogue, generated so the document has about 900,000 characters.
WORDS = [
    "catalogue",
    "basket",
    "order",
    "invoice",
    "parcel",
    "courier",
    "refund",
    "voucher",
    "warehouse",
    "shelf",
    "label",
    "price",
    "discount",
    "coupon",
    "checkout",
    "receipt",
    "customer",
    "supplier",
    "delivery",
    "stock",
]
SECRET_LINE = "deploy_token = Zq8xK2mP9vL4wR7tY1uE"


def big_markdown(target_chars: int = 900_000) -> str:
    """A spec of PARTS second-level sections with ITEMS third-level ones each, an explicit
    anchor above every part, one line a secret detector matches, and filler up to the size."""
    rng = random.Random(26)
    per_item = target_chars // (PARTS * ITEMS)
    lines = ["# Shop handbook", "", "How the demo shop works.", ""]
    for p in range(PARTS):
        lines += [f'<a id="part-{p}"></a>', "", f"## Part {p}: {WORDS[p % len(WORDS)]} rules", ""]
        for i in range(ITEMS):
            lines += [f"### Rule {p}-{i}", ""]
            size = 0
            n = 0
            while size < per_item:
                words = " ".join(rng.choice(WORDS) for _ in range(12))
                line = f"Rule {p}-{i} line {n}: {words}."
                if p == 4 and i == 2 and n == 3:
                    line = SECRET_LINE
                if p in (7, 19) and i == 0 and n == 5:
                    line = f"The marmalade clause applies to part {p}."
                lines.append(line)
                size += len(line) + 1
                n += 1
            lines.append("")
    return "\n".join(lines) + "\n"


@pytest.fixture
async def acme(service: Service) -> Service:
    await service.upsert_project("acme-shop", name="Acme Shop")
    return service


@pytest.fixture
def outside(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder outside the service's data folder (which is the test's tmp_path)."""
    return tmp_path_factory.mktemp("outside")


@pytest.fixture
def spec_file(outside: Path) -> Path:
    path = outside / "notes" / "handbook.md"
    path.parent.mkdir()
    path.write_bytes(big_markdown().encode("utf-8"))
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def real(path: Path) -> str:
    return str(path.resolve())


async def add_file(service: Service, path: Path, doc: str = "handbook") -> mem.Document:
    return await service.update_document(
        "acme-shop", doc, "-", "the handbook, from its file", 0, source_path=str(path)
    )


# --- sections and anchors ---------------------------------------------------------------------


def test_an_explicit_anchor_names_the_section_below_it() -> None:
    body = '# T\n\n<a id="gc"></a>\n\n## Gift cards\nText.\n<a id="lost"></a>\nprose\n## B\nx'
    found = mem.sections(body)
    assert [(s.anchor, s.alias) for s in found] == [("t", None), ("gift-cards", "gc"), ("b", None)]
    assert mem.find_section(body, "GC").anchor == "gift-cards"
    assert mem.section_text(body, "gc").startswith("## Gift cards")
    assert mem.alias_lines(body) == {2}  # the second one is followed by prose, not a heading


def test_an_alias_equal_to_another_anchor_is_dropped() -> None:
    body = '## One\n\n<a id="one"></a>\n## Two'
    assert [s.alias for s in mem.sections(body)] == [None, None]


def random_doc(rng: random.Random) -> str:
    pieces = []
    for _ in range(rng.randint(1, 12)):
        pieces.append(rng.choice(["# A", "## B", "### C", "## B", "#### D"]))
        for _ in range(rng.randint(0, 4)):
            pieces.append(rng.choice(["text", "", "  ", "- item", "```", "  indented", "\t"]))
    return "\n".join(pieces).strip("\n")


def test_every_section_replaced_by_itself_leaves_the_text_byte_for_byte() -> None:
    rng = random.Random(53)
    checked = 0
    for _ in range(400):
        body = random_doc(rng)
        for s in mem.sections(body):
            for name in s.names():
                assert mem.replace_section(body, name, mem.section_text(body, name)) == body
                checked += 1
    assert checked > 1000


def test_a_one_line_change_rewrites_only_that_line() -> None:
    body = "# T\n\n## A\none\ntwo\n\n\n## B\nthree"
    text = mem.section_text(body, "a").replace("two", "TWO")
    new = mem.replace_section(body, "a", text)
    old_lines, new_lines = body.split("\n"), new.split("\n")
    assert len(new_lines) == len(old_lines)
    changed = [n for n, (x, y) in enumerate(zip(old_lines, new_lines, strict=True)) if x != y]
    assert changed == [4]
    under = mem.replace_section(body, "a", "only\n\n\n")  # the text under the heading
    assert under == "# T\n\n## A\nonly\n\n\n## B\nthree"


def test_replace_lines_needs_the_expected_text() -> None:
    body = "a\nb\nc\nd"
    assert mem.replace_lines(body, 2, 3, "b\nc\n", "  B\n  C") == "a\n  B\n  C\nd"
    assert mem.replace_lines(body, 2, 2, "b", None) == "a\nc\nd"
    with pytest.raises(ConflictError, match="no longer read") as caught:
        mem.replace_lines(body, 2, 2, "x", "y")
    assert caught.value.args[0].endswith("\n\nb")
    with pytest.raises(ValidationError, match="has 4 lines"):
        mem.parse_lines("9-10", 4)
    assert mem.parse_lines("3-99", 4) == (3, 4)


async def test_doc_update_by_lines_and_the_conflict(acme: Service) -> None:
    await acme.update_document("acme-shop", "status", "# Status\n\n- a\n- b\n- c", "first", 0)
    saved = await acme.update_document(
        "acme-shop", "status", "  - B", "one line", 1, lines="4", expected="- b"
    )
    assert saved.body == "# Status\n\n- a\n  - B\n- c" and saved.version == 2
    # A stale base_version is no conflict when the lines still read as expected.
    again = await acme.update_document(
        "acme-shop", "status", "-", "drop a line", 1, lines="5-5", expected="- c"
    )
    assert again.body == "# Status\n\n- a\n  - B" and again.version == 3
    with pytest.raises(ConflictError, match="no longer read"):
        await acme.update_document(
            "acme-shop", "status", "x", "late", 3, lines="3", expected="- old"
        )
    with pytest.raises(ValidationError, match="go together"):
        await acme.update_document("acme-shop", "status", "x", "half", 3, lines="3")


async def test_a_database_document_is_indexed_and_read_by_its_alias(acme: Service) -> None:
    body = '# Spec\n\n<a id="gc"></a>\n## Gift cards\nPartial payments.\n\n## Refunds\nBack.'
    await acme.update_document("acme-shop", "spec", body, "draft", 0)
    read = await acme.read_document("acme-shop", "spec", section="gc")
    assert read.text == "## Gift cards\nPartial payments." and read.cut is None
    found, rows, _ = await acme.document_outline("acme-shop", "spec")
    out = fmt.document_outline(found, rows, None, len(rows))
    assert "h2 #gift-cards (alias #gc) Gift cards · 4-6 · " in out
    _, hits = await acme.search_documents("acme-shop", "partial payment")
    assert [(h.doc.slug, h.section.anchor, h.line) for h in hits] == [("spec", "gift-cards", 5)]
    found_hits = await acme.search("partial", project="acme-shop", kinds=["doc"])
    assert found_hits[0].anchor == "gift-cards"
    assert "section=gift-cards" in fmt.search_hits(found_hits, "partial")


async def test_a_small_database_document_reads_whole_unless_capped(acme: Service) -> None:
    body = "# Spec\n\n" + "\n".join(f"## S{n}\n" + "word " * 400 for n in range(20))
    await acme.update_document("acme-shop", "spec", body, "draft", 0)
    whole = await acme.read_document("acme-shop", "spec")
    assert whole.cut is None and whole.text == whole.doc.body == body.strip()
    capped = await acme.read_document("acme-shop", "spec", max_chars=5_000)
    answer = fmt.document_read(capped)
    assert capped.cut is not None and len(answer) <= 5_000
    assert f'lines="{capped.cut.next_lines}"' in answer and "#s19" in answer


# --- file documents ---------------------------------------------------------------------------


async def test_a_large_file_document_reads_in_parts(acme: Service, spec_file: Path) -> None:
    before = sha(spec_file)
    text = text_of(spec_file)
    assert 850_000 < len(text) < 950_000
    saved = await add_file(acme, spec_file)
    assert saved.is_file and saved.version == 0 and saved.masked_lines == 1

    found, rows, _ = await acme.document_outline("acme-shop", "handbook")
    outline = fmt.document_outline(found, rows, None, len(rows))
    assert len(rows) == 1 + PARTS * (1 + ITEMS) and len(outline) < 10_000
    assert "(alias #part-7)" in outline and "1 line(s) masked" in outline

    head = fmt.document_read(await acme.read_document("acme-shop", "handbook"))
    assert len(head) <= doc_index.READ_CAP and 'lines="' in head and "#part-19" in head
    for r in rows:
        answer = fmt.document_read(
            await acme.read_document("acme-shop", "handbook", section=r.anchor)
        )
        assert len(answer) <= doc_index.READ_CAP, r.anchor
    small = await acme.read_document("acme-shop", "handbook", section="rule-3-1")
    assert small.cut is None and small.text.startswith("### Rule 3-1\n")
    part = await acme.read_document("acme-shop", "handbook", lines="5-7")
    assert part.text == text.split("\n")[4] + "\n\n" + text.split("\n")[6]

    _, hits = await acme.search_documents("acme-shop", "marmalade clause")
    assert {h.section.anchor for h in hits} == {"rule-7-0", "rule-19-0"}
    assert all(text.split("\n")[h.line - 1].startswith("The marmalade") for h in hits)
    answer = fmt.document_hits(found, hits, "marmalade")
    assert real(spec_file) in answer and "section lines" in answer

    # The index never holds the secret: it is masked, the file is passed through on a read.
    _, none = await acme.search_documents("acme-shop", "Zq8xK2mP9vL4wR7tY1uE")
    assert none == []
    rule = await acme.read_document("acme-shop", "handbook", section="rule-4-2")
    assert SECRET_LINE in rule.text
    assert sha(spec_file) == before


async def test_relaybay_never_writes_a_file_document(acme: Service, spec_file: Path) -> None:
    await add_file(acme, spec_file)
    with pytest.raises(ValidationError, match=r"lives in the file .*lines \d+-\d+"):
        await acme.update_document(
            "acme-shop", "handbook", "x", "edit", 0, section="rule-2-1", force=True
        )
    with pytest.raises(ValidationError, match="lives in the file"):
        await acme.update_document("acme-shop", "handbook", "# New", "edit", 0)
    wanted, _ = await gather(acme)
    assert not any("handbook" in path for path in wanted)  # export never copies the file


async def test_a_changed_file_is_indexed_again_and_named_in_its_history(
    acme: Service, outside: Path
) -> None:
    path = outside / "notes" / "status.md"
    path.parent.mkdir()
    path.write_text("# Status\n\n## Now\nWorking.\n\n## Later\nNothing.\n", encoding="utf-8")
    await add_file(acme, path, "status")
    path.write_text("# Status\n\n## Now\nShipping.\n\n## Next\nRelease.\n", encoding="utf-8")
    os.utime(path, (1_900_000_000, 1_900_000_000))
    found, rows, _ = await acme.document_outline("acme-shop", "status")
    assert [r.anchor for r in rows] == ["status", "now", "next"]
    found, events = await acme.file_history("acme-shop", "status")
    latest = events[0].payload
    assert latest["added"] == ["next"] and latest["removed"] == ["later"]
    assert latest["changed"] == ["now"] and latest["hash_before"]
    assert "changed: 3 section(s)" in fmt.file_history(found, events)


async def test_a_missing_file_keeps_its_document_and_index(acme: Service, outside: Path) -> None:
    path = outside / "notes" / "status.md"
    path.parent.mkdir()
    path.write_text("# Status\n\n## Now\nWorking.\n", encoding="utf-8")
    await add_file(acme, path, "status")
    path.unlink()
    found, rows, _ = await acme.document_outline("acme-shop", "status")
    assert [r.anchor for r in rows] == ["status", "now"] and found.file_missing_at
    read = await acme.read_document("acme-shop", "status", section="now")
    answer = fmt.document_read(read)
    assert "file missing since" in answer and "#now Now · 3-5" in answer and "Working" not in answer
    project, docs = await acme.list_documents("acme-shop")
    assert "file missing since" in fmt.documents(project, docs)


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda p: "notes/x.md", "absolute"),
        (lambda p: str(p), "not a regular file"),
        (lambda p: str(p / "nope.md"), "not found"),
    ],
)
async def test_a_file_document_needs_an_absolute_regular_file(
    acme: Service, outside: Path, make: Callable[[Path], str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        await acme.update_document("acme-shop", "x", "-", "add", 0, source_path=make(outside))


async def test_a_file_document_is_utf8_under_the_limit_and_outside_the_data(
    acme: Service, outside: Path
) -> None:
    (outside / "notes").mkdir()
    latin = outside / "notes" / "latin.md"
    latin.write_bytes(b"# Caf\xe9\n")
    with pytest.raises(ValidationError, match="not valid UTF-8"):
        await acme.update_document("acme-shop", "x", "-", "add", 0, source_path=str(latin))
    huge = outside / "notes" / "huge.md"
    with huge.open("wb") as fh:
        fh.truncate(doc_index.FILE_MAX_BYTES + 1)
    with pytest.raises(ValidationError, match="8 MB"):
        await acme.update_document("acme-shop", "x", "-", "add", 0, source_path=str(huge))
    inside = acme.db_path.parent / "notes.md"
    inside.write_text("# Notes\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="data folder"):
        await acme.update_document("acme-shop", "x", "-", "add", 0, source_path=str(inside))
    with pytest.raises(ValidationError, match="network share"):
        await acme.update_document(
            "acme-shop", "x", "-", "add", 0, source_path="//fileserver/share/spec.md"
        )
    with pytest.raises(ValidationError, match="body '-'"):
        await acme.update_document("acme-shop", "x", "# Text", "add", 0, source_path=str(latin))


async def test_a_link_out_of_the_folder_resolves_to_its_target(
    acme: Service, outside: Path
) -> None:
    real = outside / "elsewhere" / "spec.md"
    real.parent.mkdir()
    real.write_text("# Spec\n", encoding="utf-8")
    link = outside / "repo" / "spec.md"
    link.parent.mkdir()
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("this system does not let a test create a symbolic link")
    saved = await add_file(acme, link, "spec")
    assert saved.source_path == str(real.resolve())


# --- the card's pointer ---------------------------------------------------------------------


async def test_task_context_points_at_the_section_without_its_text(
    acme: Service, spec_file: Path
) -> None:
    before = sha(spec_file)
    await add_file(acme, spec_file)
    task = await acme.create_task("acme-shop", "Marmalade rules")
    await acme.update_task(
        task.key,
        {
            "doc_path": "relaybay://doc/acme-shop/handbook#part-7",
            "description": "Whole handbook: relaybay://doc/acme-shop/handbook",
        },
    )
    text = await acme.task_context(task.key)
    line = next(x for x in text.split("\n") if x.startswith("- handbook#part-7"))
    assert '"Part 7: voucher rules" · lines' in line and "chars (~" in line
    assert real(spec_file) in line and 'section="part-7"' in line
    assert any(
        x.startswith('- handbook "Shop handbook" ·') and "doc_outline" in x
        for x in text.split("\n")
    )
    assert "marmalade" not in text.casefold().replace("marmalade rules", "")
    assert len(text) <= 12_000
    assert sha(spec_file) == before


# --- the board ----------------------------------------------------------------------------------


def test_the_page_of_a_file_document_has_contents_and_search(
    board: TestClient, seeded: str, token: str, outside: Path
) -> None:
    path = outside / "notes" / "guide.md"
    path.parent.mkdir()
    path.write_text(
        '# Guide\n\n<a id="pay"></a>\n## Payments\nCards and vouchers.\n\n### Refunds\nBack.\n',
        encoding="utf-8",
    )
    form = {
        "csrf_token": csrf_token(token),
        "doc": "guide",
        "source_path": str(path),
        "reason": "the guide",
    }
    created = board.post(
        "/p/acme-shop/docs/new", data=form, headers={"Origin": BASE}, follow_redirects=False
    )
    assert created.status_code == 303
    page = board.get("/p/acme-shop/docs/guide?q=vouchers").text
    assert 'class="panel doc-toc"' in page and 'data-toc="payments"' in page
    assert 'id="pay"' in page and "&lt;a id" not in page  # the explicit anchor, not its line
    assert "Lives in <code>" in page and "/edit" not in page
    assert 'href="?q=vouchers#payments"' in page and "doc.js" in page
    assert board.get("/static/doc.js").status_code == 200
    missing = board.post(
        "/p/acme-shop/docs/new",
        data={**form, "doc": "other", "source_path": "relative.md"},
        headers={"Origin": BASE},
    )
    assert missing.status_code == 400 and "absolute" in missing.text


def test_a_docs_search_hit_links_to_its_section(board: TestClient, seeded: str) -> None:
    service: Service = board.app.state.service
    body = "# Spec\n\n## Checkout\nPay by card.\n\n## Gift cards\nPartial payments."
    run_in_app(board, service.update_document, "acme-shop", "spec", body, "draft", 0)
    page = board.get("/p/acme-shop/docs?q=partial").text
    assert 'href="/p/acme-shop/docs/spec#gift-cards"' in page


# --- the portfolio ------------------------------------------------------------------------------


async def test_the_portfolio_lists_open_tasks_once(acme: Service) -> None:
    task = await acme.create_task("acme-shop", "Retry webhooks", stage="test")
    await acme.update_task(
        task.key,
        {"stage_note": "on the test stand", "next_step": "check by eye", "doc_path": "notes.md"},
    )
    await acme.update_task(task.key, {"prs": ["https://git.example.com/acme-shop/pull/41"]})
    waiting = await acme.create_task("acme-shop", "Pick a colour", stage="dev")
    await acme.update_task(waiting.key, {"waiting_on": "owner"})
    text = await acme.portfolio_digest()
    section = text.split("## Open tasks", 1)[1].split("\n\n", 1)[0]
    assert f"- [test] {task.key} Retry webhooks · note: on the test stand" in section
    assert "next: check by eye · PR #41 · doc notes.md" in section
    assert waiting.key not in section


# --- byte-identical import and export ---------------------------------------------------------

FILES = {
    "crlf": b"# Status\r\n\r\n## Now\r\nWorking.\r\n",
    "bom": "\ufeff# Notes\n\nCaf\u00e9 menu.\n".encode(),
    "no-final-newline": b"# Plan\n\n- one\n- two",
    "blank-lines": b"\n\n# Runbook\n\nRestart it.\n\n\n\n",
    "bom-crlf-trailing-spaces": "\ufeff# Mixed\r\n\r\nText.  \r\n\r\n".encode(),
}


@pytest.mark.parametrize("name", sorted(FILES))
async def test_an_imported_file_exports_with_the_same_sha256(
    service: Service, tmp_path: Path, name: str
) -> None:
    data = FILES[name]
    root = tmp_path / "setup"
    path = root / "projects" / "acme-shop" / "docs" / f"{name}.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    plan = await make_plan(service, root)
    assert not plan.failed, plan.items
    await apply_plan(service, plan)
    wanted, problems = await gather(service)
    rel = f"projects/acme-shop/docs/{name}.md"
    assert not problems
    assert (
        hashlib.sha256(wanted[rel].text.encode("utf-8")).hexdigest()
        == hashlib.sha256(data).hexdigest()
    )
    again = await make_plan(service, root)
    assert not again.writes  # an import of the same file changes nothing


async def test_mixed_line_endings_export_as_lf_with_a_warning(
    service: Service, tmp_path: Path
) -> None:
    root = tmp_path / "setup"
    path = root / "projects" / "acme-shop" / "docs" / "mixed.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"# Mixed\r\n\nOne.\r\n")
    plan = await make_plan(service, root)
    assert any("mixed line endings" in w for w in plan.warnings)
    await apply_plan(service, plan)
    wanted, _ = await gather(service)
    assert wanted["projects/acme-shop/docs/mixed.md"].text == "# Mixed\n\nOne.\n"


def test_file_style_round_trips_its_text() -> None:
    for data in FILES.values():
        text = data.decode("utf-8-sig")
        style, _ = file_style(text, data.startswith(b"\xef\xbb\xbf"))
        body = text.replace("\r\n", "\n").strip()
        assert style.apply(body).encode("utf-8") == data


# --- over MCP ---------------------------------------------------------------------------------


async def test_the_tools_over_mcp(live_server: Callable[[], LiveServer], outside: Path) -> None:
    path = outside / "files" / "guide.md"
    path.parent.mkdir()
    path.write_text("# Guide\n\n## Payments\nCards.\n\n### Refunds\nBack in 14 days.\n", "utf-8")
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", name="Acme Shop")
        added = await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="guide",
            body="-",
            reason="the guide",
            base_version=0,
            source_path=str(path),
        )
        assert "is a file document" in added and "3 section(s)" in added
        outline = await call(client, "doc_outline", project="acme-shop", doc="guide", depth=2)
        assert "#payments" in outline and "#refunds" not in outline
        found = await call(client, "doc_search", project="acme-shop", query="14 days")
        assert "guide#refunds" in found and "line 7" in found
        part = await call(
            client, "doc_get", project="acme-shop", doc="guide", lines="6-7", max_chars=2000
        )
        assert part.split("\n")[-2:] == ["### Refunds", "Back in 14 days."]
        history = await call(client, "doc_history", project="acme-shop", doc="guide")
        assert "its text is not versioned here" in history


# --- review fixes: secrets, limits, paths -----------------------------------------------------

PEM = "\n".join(
    [
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEAuZm2TlpQkMv8wq3c",
        "Jx9oLkWq0aZ1yR7pTnE4bH2cV6mN8sQd",
        "-----END RSA PRIVATE KEY-----",
    ]
)
HEADING_SECRET = "Zq8xK2mP9vL4wR7tY1uE"


def test_a_private_key_is_masked_to_its_end_line() -> None:
    text = f"# Keys\n\nBefore.\n{PEM}\nAfter.\n"
    rows, masked = doc_index.build(text)
    assert masked == 4
    indexed = rows[0].text
    assert "MIIEow" not in indexed and "Jx9oLk" not in indexed
    assert "Before." in indexed and "After." in indexed


def test_a_secret_in_a_heading_never_reaches_the_index() -> None:
    text = f"# Ops\n\n## deploy_token = {HEADING_SECRET}\nRotate it.\n\n### Under it\nText.\n"
    rows, masked = doc_index.build(text)
    assert masked == 1
    for r in rows:
        seen = (r.title + r.heading_path + r.anchor + r.text).casefold()
        assert HEADING_SECRET.casefold() not in seen
    assert [r.level for r in rows] == [1, 2, 3]  # the structure is kept
    assert rows[2].heading_path == "Ops > [masked] > Under it"


async def test_a_secret_heading_stays_out_of_every_answer(acme: Service, outside: Path) -> None:
    path = outside / "ops.md"
    path.write_text("# Ops\n\n## Keys\nNone.\n", encoding="utf-8")
    await add_file(acme, path, "ops")
    task = await acme.create_task("acme-shop", "Ops")
    path.write_text(f"# Ops\n\n## deploy_token = {HEADING_SECRET}\nNone.\n", encoding="utf-8")
    os.utime(path, (1_900_000_000, 1_900_000_000))
    found, rows, _ = await acme.document_outline("acme-shop", "ops")
    await acme.update_task(task.key, {"doc_path": f"relaybay://doc/acme-shop/ops#{rows[1].anchor}"})
    project, hits = await acme.search_documents("acme-shop", "none")
    doc, events = await acme.file_history("acme-shop", "ops")
    answers = [
        fmt.document_outline(found, rows, None, len(rows)),
        fmt.document_hits(project, hits, "none"),
        await acme.task_context(task.key),
        fmt.file_history(doc, events),
        str(events[0].payload),
    ]
    assert events[0].payload.get("sections_changed")
    assert all(HEADING_SECRET.casefold() not in a.casefold() for a in answers)


def test_a_whole_text_secret_in_a_large_document_is_found_in_linear_time() -> None:
    clean = big_markdown().replace(SECRET_LINE, "A plain line.")
    text = clean + "\nRun it with --password\nQx7vB2nM8kL3pW9z\n"
    started = time.perf_counter()
    rows, masked = doc_index.build(text)
    found = find_secrets(text)
    elapsed = time.perf_counter() - started
    assert masked == 2 and "Qx7vB2nM8kL3pW9z" not in rows[-1].text
    assert found == [(len(text.split("\n")) - 1, "password-flag")]
    assert elapsed < 30, elapsed  # the old rescan of every prefix took many minutes here


def test_line_writes_keep_and_accept_trailing_blank_lines() -> None:
    body = "# T\n\na\nb\n\n## Next\nc"
    assert mem.replace_lines(body, 3, 5, "a\nb", "A\nB") == "# T\n\nA\nB\n\n## Next\nc"
    assert mem.replace_lines(body, 3, 5, "a\nb\n\n", "A") == "# T\n\nA\n\n## Next\nc"
    assert mem.replace_lines(body, 4, 5, "b\n", None) == "# T\n\na\n## Next\nc"


async def test_line_writes_store_normalised_text(acme: Service) -> None:
    await acme.update_document("acme-shop", "one", "Only line", "first", 0)
    with pytest.raises(ValidationError, match="empty"):
        await acme.update_document(
            "acme-shop", "one", "-", "drop", 1, lines="1", expected="Only line"
        )
    await acme.update_document("acme-shop", "two", "# T\n\nx", "first", 0)
    saved = await acme.update_document(
        "acme-shop", "two", "-", "drop the heading", 1, lines="1", expected="# T"
    )
    assert saved.body == "x"


def test_a_cut_outline_and_a_long_line_stay_under_the_cap() -> None:
    lines = ["# Big", ""]
    for n in range(3000):
        lines += [f"### Item number {n} with a long enough title", "text"]
    text = "\n".join(lines)
    rows, _ = doc_index.build(text)
    all_lines = text.split("\n")
    body, cut = doc_index.capped(all_lines, 1, len(all_lines), 40_000, rows)
    assert cut is not None and cut.more > 2000
    outline_size = sum(len(doc_index.outline_line(r)) + 1 for r in cut.inside)
    assert len(body) + outline_size + doc_index.CUT_RESERVE <= 40_000
    body, cut = doc_index.capped(["x" * 100_000, "y"], 1, 2, 5_000, [])
    assert cut is not None and cut.long_line == 1 and len(body) < 5_000
    assert cut.next_lines == "2-2"


async def test_capped_answers_never_pass_the_cap(acme: Service, outside: Path) -> None:
    lines = ["# Big", ""]
    for n in range(3000):
        lines += [f"### Item number {n} with a long enough title", "text " * 20]
    lines += ["z" * 300_000]
    path = outside / "big.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    await add_file(acme, path, "big")
    for cap in (None, 2_000, 10_000):
        whole = fmt.document_read(await acme.read_document("acme-shop", "big", max_chars=cap))
        assert len(whole) <= (cap or doc_index.READ_CAP)
        assert "more heading(s): doc_outline" in whole
    tail = fmt.document_read(await acme.read_document("acme-shop", "big", lines=f"{len(lines)}"))
    assert len(tail) <= doc_index.READ_CAP and "alone is longer than the cap" in tail


def path_rules(tmp: Path, network: bool = False) -> doc_index.PathRules:
    return doc_index.PathRules(
        data_dir=tmp / "data",
        protected=(tmp / "config" / "desktop.toml",),
        home=tmp / "home",
        network_ok=lambda text: network or not text.replace("/", "\\").startswith("\\\\"),
    )


@pytest.mark.parametrize(
    ("rel", "reason"),
    [
        ("data/relaybay.sqlite", "data folder"),
        ("data/attachments/acme/x.md", "data folder"),
        ("config/desktop.toml", "keeps a secret"),
        ("home/.ssh/notes.md", "~/.ssh"),
        ("home/.aws/notes.md", "~/.aws"),
        ("home/.config/gh/hosts.md", "~/.config/gh"),
        ("work/.env.local", ".env*"),
        ("work/server.PEM", "*.pem"),
        ("work/id_ed25519.pub", "id_ed25519*"),
        ("work/vault.kdbx", "*.kdbx"),
        ("work/credentials.md", "credentials*"),
    ],
)
def test_denied_places_by_the_resolved_path(tmp_path: Path, rel: str, reason: str) -> None:
    assert reason in (doc_index.denied(tmp_path / rel, path_rules(tmp_path)) or "")


def test_allowed_places_and_a_share_after_resolution(tmp_path: Path) -> None:
    rules = path_rules(tmp_path)
    assert doc_index.denied(tmp_path / "data" / "worktrees" / "x" / "spec.md", rules) is None
    assert doc_index.denied(tmp_path / "work" / "spec.md", rules) is None
    share = Path("\\\\fileserver\\share\\spec.md")  # where a junction may lead
    assert "network share" in (doc_index.denied(share, rules) or "")
    assert doc_index.denied(share, path_rules(tmp_path, network=True)) is None


def test_device_folders_are_denied() -> None:
    rules = path_rules(Path("/nowhere"))
    for path in ("/proc/self/environ", "/sys/kernel/x", "/dev/stdin"):
        assert doc_index.denied(Path(path), rules)


async def test_a_file_document_is_refused_in_a_denied_place(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: outside))
    secret = outside / ".ssh" / "notes.md"
    secret.parent.mkdir()
    secret.write_text("# Keys\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="credentials"):
        await add_file(acme, secret, "keys")
    deep = acme.db_path.parent / "sub" / "dir" / "x.md"
    deep.parent.mkdir(parents=True)
    deep.write_text("# X\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="data folder"):
        await add_file(acme, deep, "deep")
    env = outside / ".env"
    env.write_text("A=1\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="name matches"):
        await add_file(acme, env, "env")


async def test_a_file_swapped_for_a_link_into_a_denied_place_is_refused(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: outside))
    path = outside / "spec.md"
    path.write_text("# Spec\n\n## A\nText.\n", encoding="utf-8")
    await add_file(acme, path, "spec")
    target = outside / ".ssh" / "config"
    target.parent.mkdir()
    target.write_text("# Host\n", encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("this system does not let a test create a symbolic link")
    read = await acme.read_document("acme-shop", "spec")
    assert read.text == "" and "refused now" in (read.doc.file_problem or "")


async def test_a_file_that_vanishes_while_added_is_a_clear_error(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = outside / "spec.md"
    path.write_text("# Spec\n", encoding="utf-8")
    monkeypatch.setattr(doc_index, "file_stat", lambda _: None)
    with pytest.raises(ValidationError, match="is gone"):
        await add_file(acme, path, "spec")


async def test_a_rebuilt_index_is_not_a_change_of_the_file(acme: Service, outside: Path) -> None:
    path = outside / "spec.md"
    path.write_text("# Spec\n\n## A\nText.\n", encoding="utf-8")
    await add_file(acme, path, "spec")
    _, before = await acme.file_history("acme-shop", "spec")
    async with acme._tx() as tx:  # what a start does when the search index was lost
        await repo.clear_section_index(tx.conn, acme.search_fts)
    _, rows, _ = await acme.document_outline("acme-shop", "spec")
    assert [r.anchor for r in rows] == ["spec", "a"]
    _, after = await acme.file_history("acme-shop", "spec")
    assert len(after) == len(before)


async def test_the_task_dialog_shows_a_capped_part_of_a_file_document(
    acme: Service, spec_file: Path
) -> None:
    await add_file(acme, spec_file)
    task = await acme.create_task("acme-shop", "Read it all")
    await acme.update_task(task.key, {"doc_path": "relaybay://doc/acme-shop/handbook"})
    detail = await acme.task_detail(task.key)
    assert detail.document is not None
    assert 0 < len(detail.document.text) <= doc_index.READ_CAP


# --- review round 2 ----------------------------------------------------------------------------


def test_a_line_hidden_by_a_match_across_lines_keeps_nothing() -> None:
    text = "# T\n\nrun with --password\nhunter2secret token abcdefgh1\n"
    rows, masked = doc_index.build(text)
    assert masked == 2 and "hunter2secret" not in rows[0].text
    assert doc_index.MASKED_LINE in rows[0].text


def test_a_heading_hidden_by_a_match_across_lines_keeps_nothing() -> None:
    text = "# T\n\nrun with --password\n## hunter2secret token abcdefgh1\nText.\n"
    rows, _ = doc_index.build(text)
    heading = rows[1]
    assert heading.level == 2 and heading.title == "[masked]"
    seen = (heading.title + heading.anchor + heading.heading_path + heading.text).casefold()
    assert "hunter2secret" not in seen


def test_a_redacted_line_is_kept_only_when_nothing_is_found_again() -> None:
    line = "deploy_token = Zq8xK2mP9vL4wR7tY1uE and api_key: Wq8xK2mP9vL4wR7tY1uF"
    rows, _ = doc_index.build(f"# T\n\n{line}\n")
    assert "Zq8xK2mP9vL4wR7tY1uE" not in rows[0].text
    assert "Wq8xK2mP9vL4wR7tY1uF" not in rows[0].text


def test_many_private_key_headers_are_one_forward_pass() -> None:
    text = "\n".join(["-----BEGIN RSA PRIVATE KEY-----"] * 28_000)  # ~900k, no END
    started = time.perf_counter()
    spans = list(secret_spans(text))
    rows, masked = doc_index.build(f"# Keys\n{text}")
    elapsed = time.perf_counter() - started
    assert len(spans) == 1 and spans[0].end == len(text)
    assert masked == 28_000 and "BEGIN" not in rows[0].text
    assert elapsed < 30, elapsed  # each header used to scan to the end: minutes


def test_many_repeated_headings_get_their_anchors_in_linear_time() -> None:
    text = "\n".join(f"## Notes\nline {n}" for n in range(20_000))
    started = time.perf_counter()
    rows, _ = doc_index.build(text)
    elapsed = time.perf_counter() - started
    assert [r.anchor for r in rows[:3]] == ["notes", "notes-2", "notes-3"]
    assert rows[-1].anchor == "notes-20000"
    assert mem.unique_anchors(["a", "a-2", "a", "a"]) == ["a", "a-2", "a-3", "a-4"]
    assert elapsed < 20, elapsed  # counting up from 1 per repeat took tens of seconds


async def test_a_long_heading_never_pushes_an_answer_over_the_cap(
    acme: Service, outside: Path
) -> None:
    title = "Very long heading " + "word " * 360  # about 1800 characters
    lines = ["# Spec", "", f"## {title}", *[f"Line {n} of the text." for n in range(400)]]
    lines += [f"### {title} {n}" for n in range(5)]
    path = outside / "long.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    await add_file(acme, path, "long")
    _, rows, _ = await acme.document_outline("acme-shop", "long")
    anchor = rows[1].anchor
    assert len(anchor) > 1500
    for cap in (2_000, 3_000):
        for options in ({"section": anchor}, {"lines": "1-400"}, {}):
            read = await acme.read_document("acme-shop", "long", max_chars=cap, **options)
            answer = fmt.document_read(read)
            assert len(answer) <= cap, (cap, options, len(answer))
            assert "Read on:" in answer or "lines 1-" in answer
    small = "# Spec\n\n## Short\n" + "\n".join(f"line {n}" for n in range(300))
    await acme.update_document("acme-shop", "small", small, "first", 0)
    answer = fmt.document_read(await acme.read_document("acme-shop", "small", max_chars=2_000))
    assert len(answer) <= 2_000 and "Read on:" in answer


async def test_open_tasks_leave_out_an_archived_project(acme: Service) -> None:
    await acme.upsert_project("demo-api", name="Demo API")
    kept = await acme.create_task("acme-shop", "Kept card", stage="dev")
    gone = await acme.create_task("demo-api", "Card of an archived project", stage="dev")
    await acme.set_project_archived("demo-api", True)
    text = await acme.portfolio_digest()
    section = text.split("## Open tasks", 1)[1].split("\n\n", 1)[0]
    assert kept.key in section and gone.key not in text
