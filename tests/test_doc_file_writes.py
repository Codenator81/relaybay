"""File documents written through relaybay (design 0027, part A): opt-in per document, spliced
into the bytes so only the replaced lines change, atomic and checked twice by hash, and a change
made outside relaybay is a new version that every read names until the next write."""

import asyncio
import hashlib
import os
import stat
import time
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain import doc_index
from relaybay.domain import memory as mem
from relaybay.domain.errors import ConflictError, ValidationError
from relaybay.mcp_server import format as fmt
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app

LEAD = "lead"
SPEC = (
    "# Spec\n\n## Cart\nThe cart keeps items.\n\n## Checkout\nPay by card.\n\n## Refunds\n"
    "In 14 days."
)


@pytest.fixture
async def acme(service: Service) -> Service:
    service.coordinators = [LEAD]
    await service.upsert_project("acme-shop", name="Acme Shop")
    return service


@pytest.fixture
def outside(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("outside")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def later(path: Path) -> None:
    """A new modification time, so a read does not trust size and time of the last one."""
    stamp = path.stat().st_mtime + 5
    os.utime(path, (stamp, stamp))


async def add(service: Service, path: Path, writes: bool = True) -> mem.Document:
    return await service.update_document(
        "acme-shop",
        "spec",
        "-",
        "the spec, from its file",
        0,
        source_path=str(path),
        file_writes="on" if writes else None,
        session=LEAD,
    )


async def write(service: Service, body: str, base: int, **kw: str) -> mem.Document:
    return await service.update_document("acme-shop", "spec", body, "edit", base, **kw)


def changed_lines(before: bytes, after: bytes) -> list[int]:
    old, new = before.split(b"\n"), after.split(b"\n")
    assert len(old) == len(new)
    return [n for n, (a, b) in enumerate(zip(old, new, strict=True)) if a != b]


# --- splice -----------------------------------------------------------------------------------


def test_splice_keeps_crlf_bom_and_no_final_newline() -> None:
    data = b"\xef\xbb\xbf" + SPEC.replace("\n", "\r\n").encode()
    old = SPEC
    new = old.replace("Pay by card.", "Pay by card or voucher.")
    out = doc_index.splice(data, old, new)
    assert out.startswith(b"\xef\xbb\xbf") and not out.endswith(b"\n")
    assert changed_lines(data, out) == [6]
    assert out.split(b"\n")[6] == b"Pay by card or voucher.\r"
    assert doc_index.splice(data, old, old) == data


def test_splice_new_lines_take_the_ending_where_the_change_starts() -> None:
    data = b"# A\nlf line\r\ncrlf line\r\nend\n"
    old = "# A\nlf line\ncrlf line\nend\n"
    new = "# A\nlf line\ncrlf changed\nadded\nend\n"
    assert doc_index.splice(data, old, new) == b"# A\nlf line\r\ncrlf changed\r\nadded\r\nend\n"
    # Inserted before `end`, whose ending is LF: the new lines are LF too.
    new = "# A\nlf line\ncrlf line\nadded\nend\n"
    assert doc_index.splice(data, old, new) == b"# A\nlf line\r\ncrlf line\r\nadded\nend\n"
    # Lines added at the end of a file without a final newline get the file's ending.
    data = b"a\r\nb"
    assert doc_index.splice(data, "a\nb", "a\nb\nc") == b"a\r\nb\r\nc"
    # The final newlines are the file's, whatever the new text ends with.
    data = b"a\nb\n\n"
    assert doc_index.splice(data, "a\nb\n\n", "a\nc") == b"a\nc\n\n"
    assert doc_index.splice(data, "a\nb\n\n", "a") == b"a\n\n"


# --- writes -----------------------------------------------------------------------------------


async def test_a_section_write_changes_only_that_sections_lines(
    acme: Service, outside: Path
) -> None:
    path = outside / "spec.md"
    before = b"\xef\xbb\xbf" + SPEC.replace("\n", "\r\n").encode()
    path.write_bytes(before)
    saved = await add(acme, path)
    assert saved.version == 1 and saved.file_writable
    done = await write(acme, "## Checkout\nPay by card or voucher.", 1, section="checkout")
    after = path.read_bytes()
    assert changed_lines(before, after) == [6] and after.startswith(b"\xef\xbb\xbf")
    assert not after.endswith(b"\n") and after.count(b"\r\n") == before.count(b"\r\n")
    assert done.version == 2 and done.file_changed_by == "relaybay"
    assert done.file_hash == sha(after) and done.file_changed_sections == "checkout"
    _, events = await acme.file_history("acme-shop", "spec")
    p = events[0].payload
    assert p["changed"] == ["checkout"] and p["version"] == 2 and not p.get("outside")
    assert "by mcp: edit" in fmt.file_history(done, events)
    # The same text again writes nothing.
    again = await write(acme, "## Checkout\nPay by card or voucher.", 2, section="checkout")
    assert again.version == 2 and path.read_bytes() == after
    read = await acme.read_document("acme-shop", "spec", section="checkout")
    assert read.text == "## Checkout\nPay by card or voucher."


async def test_an_identity_section_write_keeps_the_sha256(acme: Service, outside: Path) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.replace("\n", "\r\n").encode() + b"\r\n\r\n")
    before = path.read_bytes()
    await add(acme, path)
    for anchor in ("spec", "cart", "checkout", "refunds"):
        section = mem.section_text(SPEC, anchor)
        saved = await write(acme, section, 1, section=anchor)
        assert saved.version == 1 and sha(path.read_bytes()) == sha(before), anchor


async def test_lines_and_whole_writes(acme: Service, outside: Path) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode() + b"\n")
    await add(acme, path)
    saved = await write(acme, "Refunds within 30 days.", 1, lines="10", expected="In 14 days.")
    assert path.read_bytes() == SPEC.replace("In 14 days.", "Refunds within 30 days.").encode() + (
        b"\n"
    )
    # lines + expected write whatever the version; a wrong expected is the conflict.
    with pytest.raises(ConflictError, match="no longer read as"):
        await write(acme, "x", 1, lines="10", expected="In 14 days.")
    stale = await write(acme, "Within 30 days.", 1, lines="10", expected="Refunds within 30 days.")
    assert stale.version == saved.version + 1
    whole = await write(acme, "# Spec\n\nOne line.", stale.version)
    assert path.read_bytes() == b"# Spec\n\nOne line.\n" and whole.version == stale.version + 1
    _, rows, _ = await acme.document_outline("acme-shop", "spec")
    assert [r.anchor for r in rows] == ["spec"]


async def test_an_outside_change_is_a_version_named_until_the_next_write(
    acme: Service, outside: Path
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    path.write_bytes(SPEC.replace("Pay by card.", "Pay by cash.").encode())
    later(path)
    found, rows, parent = await acme.document_outline("acme-shop", "spec")
    assert found.version == 2 and found.file_changed_by == "outside"
    note = "Note: spec was changed outside relaybay at"
    outline = fmt.document_outline(found, rows, parent, len(rows))
    assert note in outline and ": sections checkout (version 2)." in outline
    assert "writes through relaybay: on" in outline
    assert note in fmt.document_read(await acme.read_document("acme-shop", "spec"))
    proj, hits = await acme.search_documents("acme-shop", "cash")
    assert fmt.document_hits(proj, hits, "cash").count(note) == 1
    task = await acme.create_task("acme-shop", "Checkout")
    await acme.update_task(task.key, {"doc_path": "relaybay://doc/acme-shop/spec#checkout"})
    context = await acme.task_context(task.key)
    assert any(x.startswith(note) for x in context.split("\n"))
    _, events = await acme.file_history("acme-shop", "spec")
    assert events[0].payload["outside"] and events[0].payload["changed"] == ["checkout"]
    # A second outside change adds its sections to the note.
    path.write_bytes(SPEC.replace("Pay by card.", "Pay by cash.").replace("14", "30").encode())
    later(path)
    found, _, _ = await acme.document_outline("acme-shop", "spec")
    assert found.version == 3 and found.file_changed_sections == "checkout,refunds"

    # A writer holding the version from before the outside change gets the conflict with the
    # current section; nothing is written.
    current = path.read_bytes()
    with pytest.raises(ConflictError, match="at version 3, not 1") as caught:
        await write(acme, "## Checkout\nPay by card or voucher.", 1, section="checkout")
    assert "Pay by cash." in str(caught.value) and path.read_bytes() == current
    saved = await write(acme, "## Checkout\nPay by card or voucher.", 3, section="checkout")
    assert saved.version == 4 and saved.file_changed_by == "relaybay"
    found, rows, parent = await acme.document_outline("acme-shop", "spec")
    assert note not in fmt.document_outline(found, rows, parent, len(rows))
    assert not any(x.startswith(note) for x in (await acme.task_context(task.key)).split("\n"))


async def test_an_outside_change_seen_first_by_the_write_is_a_conflict(
    acme: Service, outside: Path
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    path.write_bytes(SPEC.replace("Pay by card.", "Pay by cash.").encode())
    later(path)
    with pytest.raises(ConflictError, match="at version 2, not 1"):
        await write(acme, "## Checkout\nPay by card.", 1, section="checkout")
    assert b"cash" in path.read_bytes()


async def test_a_change_between_the_read_and_the_replace_writes_nothing(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    real_open = Path.open
    meddled = SPEC.replace("Pay by card.", "Pay by cheque.").encode()
    done: list[bool] = []

    def open_and_meddle(self: Path, mode: str = "r", *args: Any, **kw: Any) -> Any:
        # The hash right before the replace reads the file (a temporary one is there by then):
        # an editor saved it just at that moment.
        pending = any(p.name.endswith(".tmp") for p in path.parent.iterdir())
        if self.name == "spec.md" and mode == "rb" and pending and not done:
            done.append(True)
            path.write_bytes(meddled)
        return real_open(self, mode, *args, **kw)

    monkeypatch.setattr(Path, "open", open_and_meddle)
    with pytest.raises(ConflictError, match="while it was being written") as caught:
        await write(acme, "## Checkout\nPay by voucher.", 1, section="checkout")
    monkeypatch.undo()
    assert path.read_bytes() == meddled and "Pay by cheque." in str(caught.value)
    assert [p.name for p in path.parent.iterdir()] == ["spec.md"]  # no temporary file left
    found, _, _ = await acme.document_outline("acme-shop", "spec")
    assert found.version == 2 and found.file_changed_by == "outside"


async def test_a_secret_is_refused_and_the_file_untouched(acme: Service, outside: Path) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    with pytest.raises(ValidationError, match="body"):
        await write(acme, "## Cart\ndeploy_token = Zq8xK2mP9vL4wR7tY1uE", 1, section="cart")
    assert path.read_bytes() == SPEC.encode()


async def test_a_missing_or_refused_file_is_not_written(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    path.unlink()
    with pytest.raises(ValidationError, match="is missing"):
        await write(acme, "## Cart\nNew.", 1, section="cart")
    path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(ValidationError, match="not valid UTF-8"):
        await write(acme, "## Cart\nNew.", 1, section="cart")
    path.write_bytes(SPEC.encode())
    monkeypatch.setattr(doc_index, "recheck", lambda p, rules: f"{p} is refused now: test")
    with pytest.raises(ValidationError, match="refused now"):
        await write(acme, "## Cart\nNew.", 1, section="cart")
    assert path.read_bytes() == SPEC.encode()


async def test_a_result_over_the_size_limit_is_refused(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    monkeypatch.setattr(doc_index, "FILE_MAX_BYTES", len(SPEC) + 5)
    with pytest.raises(ValidationError, match="nothing was written"):
        await write(acme, "## Cart\nThe cart keeps items for a week.", 1, section="cart")
    assert path.read_bytes() == SPEC.encode()


# --- opt-in -----------------------------------------------------------------------------------


async def test_writes_are_off_until_a_coordinator_turns_them_on(
    acme: Service, outside: Path
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    saved = await add(acme, path, writes=False)
    assert not saved.file_writable
    found, rows, parent = await acme.document_outline("acme-shop", "spec")
    assert "writes through relaybay: off" in fmt.document_outline(found, rows, parent, len(rows))
    with pytest.raises(ValidationError, match=r"lines 6-8.*Writes through relaybay are off"):
        await write(acme, "## Checkout\nNew.", 1, section="checkout")
    with pytest.raises(ValidationError, match="only for the coordinator"):
        await write(acme, "-", 1, file_writes="on", session="worker")
    with pytest.raises(ValidationError, match="only for the coordinator"):
        await acme.update_document(
            "acme-shop", "other", "-", "add", 0, source_path=str(path), file_writes="on"
        )
    on = await write(acme, "-", 1, file_writes="on", session=LEAD)
    assert on.file_writable and path.read_bytes() == SPEC.encode()
    await write(acme, "## Checkout\nNew.", 1, section="checkout")
    await write(acme, "-", 2, file_writes="off", session=LEAD)
    with pytest.raises(ValidationError, match="are off"):
        await write(acme, "## Checkout\nNewer.", 2, section="checkout")
    _, events = await acme.file_history("acme-shop", "spec")
    assert events[0].payload["file_writes"] == "off"
    with pytest.raises(ValidationError, match="for a file document"):
        await acme.update_document(
            "acme-shop", "notes", "# Notes", "add", 0, file_writes="on", session=LEAD
        )


async def test_upgrade_to_0054_keeps_file_documents_read_only(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    names = sorted(e.name for e in folder.iterdir() if e.name.endswith(".sql") and e.name < "0054")
    sql = "\n".join(folder.joinpath(n).read_text(encoding="utf-8") for n in names)
    now = "2026-11-19T10:00:00.000+00:00"
    conn = await connect(db)
    try:
        await conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = 53;\nCOMMIT;")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Acme Shop', 0, ?, ?)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO document (project_id, slug, title, kind, version, created_at,"
            " updated_at, source_path) VALUES (1, 'spec', 'Spec', 'spec', 0, ?, ?, '/x/spec.md')",
            (now, now),
        )
        await conn.commit()
        await migrate(conn)
        found = await repo.get_document(conn, 1, "spec")
        assert found is not None and found.version == 1 and not found.file_writable
        assert found.file_changed_by is None
    finally:
        await conn.close()


# --- bytes the body must not change -----------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "body", "section"),
    [
        # A markdown hard break: two spaces at the end of a section's last line.
        (b"# S\n\n## A\nline one  \nline two  \n\n## B\nx\n", "## A\nline one  \nline two  ", "a"),
        # A whole write with a leading blank line, and one with an indented first line.
        (b"\n# T\n  indented\n", "\n# T\n  indented", None),
        (b"    code first\nmore\n", "    code first\nmore", None),
        # No final newline, trailing spaces or a no-break space at the end.
        (b"# T\nx   ", "# T\nx   ", None),
        ("# T\nx\u00a0".encode(), "# T\nx\u00a0", None),
        (b"# T\n\n## A\nx   ", "## A\nx   ", "a"),
        # A CR CR LF line ending, sent back as the reader got it (its CRLF turned into LF).
        (b"# A\r\r\nx\n", "# A\r\nx\n", None),
        (b"## A\r\r\nx\r\n\r\n## B\r\ny", "## A\r\nx", "a"),
        # Empty lines at the end with a CR CR LF ending (read as lone CRs).
        (b"x\r\r\n\r\r\n", "x\r\n\r\n", None),
        (b"# T\r\nx\r\r\n\r\r\n", "# T\nx\r\n\r\n", None),
        (b"# T\n\n## A\nx\r\r\n\r\r\n", "## A\nx\r\n\r\n", "a"),
    ],
)
async def test_an_identity_write_keeps_every_byte(
    acme: Service, outside: Path, data: bytes, body: str, section: str | None
) -> None:
    path = outside / "spec.md"
    path.write_bytes(data)
    await add(acme, path)
    kw = {"section": section} if section else {}
    saved = await write(acme, body, 1, **kw)
    assert path.read_bytes() == data and saved.version == 1


async def test_a_hard_break_survives_an_edit_of_its_section(acme: Service, outside: Path) -> None:
    path = outside / "spec.md"
    before = b"# S\n\n## A\nline one  \nline two  \n\n## B\nx\n"
    path.write_bytes(before)
    await add(acme, path)
    await write(acme, "## A\nline one  \nline 2  ", 1, section="a")
    after = path.read_bytes()
    assert changed_lines(before, after) == [4] and after.split(b"\n")[4] == b"line 2  "


# --- read-only files and concurrent writes ----------------------------------------------------


async def test_a_read_only_file_is_refused_and_nothing_is_left(
    acme: Service, outside: Path
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    path.chmod(stat.S_IREAD)
    try:
        with pytest.raises(ValidationError, match="read-only; nothing was written"):
            await write(acme, "## Cart\nNew.", 1, section="cart")
        assert await asyncio.to_thread(os.listdir, outside) == ["spec.md"]
        assert path.read_bytes() == SPEC.encode()
    finally:
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


async def test_a_read_during_a_write_is_no_outside_change(
    acme: Service, outside: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    replaced = asyncio.Event()
    loop = asyncio.get_running_loop()
    build = doc_index.build

    def slow_build(text: str, *args: Any) -> Any:
        # The write's index is built after the replace and before its commit: read right then.
        if "for a week" in text and not replaced.is_set():
            loop.call_soon_threadsafe(replaced.set)
            time.sleep(0.3)
        return build(text, *args)

    monkeypatch.setattr(doc_index, "build", slow_build)

    async def read() -> None:
        await replaced.wait()
        await acme.read_document("acme-shop", "spec")

    await asyncio.gather(
        write(acme, "## Cart\nThe cart keeps items for a week.", 1, section="cart"), read()
    )
    found = await acme.document_meta("acme-shop", "spec")
    assert found.version == 2 and found.file_changed_by == "relaybay"
    _, events = await acme.file_history("acme-shop", "spec")
    assert not any(e.payload.get("outside") for e in events)


async def test_two_writes_at_once_never_lose_one(acme: Service, outside: Path) -> None:
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    await add(acme, path)
    results = await asyncio.gather(
        write(acme, "## Cart\nThe cart keeps items for a week.", 1, section="cart"),
        write(acme, "## Refunds\nIn 30 days.", 1, section="refunds"),
        return_exceptions=True,
    )
    conflicts = [r for r in results if isinstance(r, ConflictError)]
    saved = [r for r in results if isinstance(r, mem.Document)]
    assert len(conflicts) == 1 and len(saved) == 1 and saved[0].version == 2
    text = path.read_text(encoding="utf-8")
    assert ("for a week" in text) != ("In 30 days." in text)
    # Line writes do not depend on the version: all of them apply, one after the other.
    lines = text.split("\n")
    both = await asyncio.gather(
        write(acme, "Pay by voucher.", 2, lines="7", expected=lines[6]),
        write(acme, "Kept.", 2, lines="4", expected=lines[3]),
    )
    assert all(isinstance(d, mem.Document) for d in both)
    final = path.read_text(encoding="utf-8").split("\n")
    assert final[6] == "Pay by voucher." and final[3] == "Kept."


# --- the board ----------------------------------------------------------------------------------


def test_the_board_refuses_to_save_a_file_document(
    board: TestClient, seeded: str, token: str, outside: Path
) -> None:
    service: Service = board.app.state.service
    path = outside / "spec.md"
    path.write_bytes(SPEC.encode())
    service.coordinators = [LEAD]
    run_in_app(
        board,
        service.update_document,
        "acme-shop",
        "spec",
        "-",
        "add",
        0,
        source_path=str(path),
        file_writes="on",
        session=LEAD,
    )
    form = {
        "csrf_token": csrf_token(token),
        "body": "# Spec\n\nReplaced.",
        "reason": "edit on the board",
        "base_version": "1",
    }
    saved = board.post(
        "/p/acme-shop/docs/spec/edit", data=form, headers={"Origin": BASE}, follow_redirects=False
    )
    assert saved.status_code == 303 and path.read_bytes() == SPEC.encode()
