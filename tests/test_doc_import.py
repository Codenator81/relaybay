"""doc_update from_path (a local file's text imported once into a database document) and the
warning that lists links without a target after a write and on a whole doc_get."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from relaybay.domain import memory as mem
from relaybay.domain.errors import ConflictError, ValidationError
from relaybay.mcp_server import format as fmt
from relaybay.service import Service
from tests.conftest import LiveServer
from tests.test_mcp import call, connect

SPEC = "# Spec\n\n## Payouts\nWeekly.\n\n## Refunds & returns\nIn 14 days.\n"
SECRET_LINE = "deploy_token = Zq8xK2mP9vL4wR7tY1uE"


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "data" / "relaybay.sqlite")
    await service.start()
    await service.upsert_project("acme-shop")
    await service.upsert_project("demo-api")
    try:
        yield service
    finally:
        await service.close()


@pytest.fixture
def outside(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A folder outside the service's data folder."""
    return tmp_path_factory.mktemp("outside")


def write(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    path.write_bytes(text.encode("utf-8"))
    return path


async def imported(svc: Service, path: Path, base: int = 0, doc: str = "notes") -> mem.Document:
    return await svc.update_document(
        "acme-shop", doc, "-", "imported", base, from_path=str(path), session="worker"
    )


# --- import ------------------------------------------------------------------------------------


async def test_import_creates_a_database_document_and_replaces_it_whole(
    svc: Service, outside: Path
) -> None:
    first = write(outside, "spec.md", "﻿" + SPEC.replace("\n", "\r\n"))
    saved = await imported(svc, first)
    assert saved.version == 1 and saved.title == "Spec" and not saved.is_file
    assert saved.source_path is None and saved.source_url is None
    _, text = await svc.get_document("acme-shop", "notes")
    assert text == SPEC.strip()

    second = write(outside, "spec-v2.md", SPEC + "\n## Vouchers\nNever.\n")
    with pytest.raises(ConflictError, match="at version 1, not 0"):
        await imported(svc, second, base=0)
    saved = await imported(svc, second, base=1)
    assert saved.version == 2
    _, text = await svc.get_document("acme-shop", "notes")
    assert text.endswith("## Vouchers\nNever.")
    # The file is read once: a later change to it does not reach the document.
    second.write_text("# Other\n", encoding="utf-8")
    _, text = await svc.get_document("acme-shop", "notes")
    assert "Vouchers" in text


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"section": "payouts"}, "whole database document"),
        ({"lines": "1-2", "expected": "# Spec\n"}, "whole database document"),
        ({"source_url": "https://example.com/spec.md"}, "whole database document"),
        ({"body": "# Spec"}, "body must be '-' with from_path"),
    ],
)
async def test_import_refuses_other_kinds_of_write(
    svc: Service, outside: Path, extra: dict, message: str
) -> None:
    await svc.update_document("acme-shop", "notes", SPEC, "first", 0)
    path = write(outside, "spec.md", SPEC)
    args = {"body": "-", **extra}
    body = args.pop("body")
    with pytest.raises(ValidationError, match=message):
        await svc.update_document("acme-shop", "notes", body, "x", 1, from_path=str(path), **args)
    with pytest.raises(ValidationError, match="whole database document"):
        await svc.update_document(
            "acme-shop", "other", "-", "x", 0, from_path=str(path), source_path=str(path)
        )


async def test_import_refuses_a_file_document(svc: Service, outside: Path) -> None:
    path = write(outside, "spec.md", SPEC)
    await svc.update_document("acme-shop", "handbook", "-", "file", 0, source_path=str(path))
    with pytest.raises(ValidationError, match="lives in a file"):
        await imported(svc, path, base=1, doc="handbook")


async def test_import_refuses_paths_the_file_rules_refuse(svc: Service, outside: Path) -> None:
    with pytest.raises(ValidationError, match="from_path must be an absolute path"):
        await svc.update_document("acme-shop", "notes", "-", "x", 0, from_path="spec.md")
    with pytest.raises(ValidationError, match="not found"):
        await imported(svc, outside / "missing.md")
    inside = write(svc.db_path.parent, "copy.md", SPEC)
    with pytest.raises(ValidationError, match="data folder"):
        await imported(svc, inside)
    env = write(outside, ".env", "A=1\n")
    with pytest.raises(ValidationError, match=r"\.env\*"):
        await imported(svc, env)
    with pytest.raises(ValidationError, match="network share"):
        await imported(svc, Path("\\\\fileserver\\share\\spec.md"))
    _, items = await svc.list_documents("acme-shop")
    assert items == []


async def test_import_keeps_the_body_limits_and_the_secret_scan(
    svc: Service, outside: Path
) -> None:
    big = write(outside, "big.md", "# Big\n\n" + "x" * mem.DOC_BODY_MAX)
    with pytest.raises(ValidationError, match=f"at most {mem.DOC_BODY_MAX}"):
        await imported(svc, big)
    empty = write(outside, "empty.md", "\n\n")
    with pytest.raises(ValidationError, match="nothing to import"):
        await imported(svc, empty)
    latin1 = outside / "latin1.md"
    latin1.write_bytes("# Caf\xe9\n".encode("latin-1"))
    with pytest.raises(ValidationError, match="not valid UTF-8"):
        await imported(svc, latin1)
    secret = write(outside, "secret.md", f"# Deploy\n\n{SECRET_LINE}\n")
    with pytest.raises(ValidationError, match="body looks like it contains a secret"):
        await imported(svc, secret)
    _, items = await svc.list_documents("acme-shop")
    assert items == []


# --- link check --------------------------------------------------------------------------------


async def test_links_without_a_target_are_listed(svc: Service) -> None:
    await svc.update_document("demo-api", "spec", "# API\n\n## Retries\nThree.\n", "first", 0)
    body = (
        SPEC + "\nSee [payouts](#payouts), [refunds](#refunds--returns), [gone](#vouchers),"
        " relaybay://doc/demo-api/spec#retries, relaybay://doc/demo-api/spec#timeouts,"
        " relaybay://doc/demo-api/missing, relaybay://doc/no-project/spec#x and"
        " [self](relaybay://doc/acme-shop/notes#payouts).\n"
        "\n`relaybay://doc/demo-api/example#x` and [code](#in-code) in `[x](#span)`.\n"
        "\n```\n[fenced](#fenced) relaybay://doc/demo-api/fenced\n```\n"
    )
    saved = await svc.update_document("acme-shop", "notes", body, "first", 0)
    _, whole = await svc.get_document("acme-shop", "notes")
    broken = await svc.broken_links(saved, whole)
    assert broken == [
        "relaybay://doc/demo-api/spec#timeouts",
        "relaybay://doc/demo-api/missing",
        "relaybay://doc/no-project/spec#x",
        "notes#vouchers",
        "notes#in-code",
    ]
    assert fmt.broken_links(broken) == (
        "Warning: links without a target: relaybay://doc/demo-api/spec#timeouts,"
        " relaybay://doc/demo-api/missing, relaybay://doc/no-project/spec#x, notes#vouchers,"
        " notes#in-code (5)"
    )
    assert await svc.broken_links(saved, SPEC + "\n[ok](#payouts)\n") == []
    assert fmt.broken_links([]) is None


def test_the_warning_names_at_most_ten() -> None:
    names = [f"notes#gone-{n}" for n in range(13)]
    text = fmt.broken_links(names) or ""
    assert text.startswith("Warning: links without a target: notes#gone-0, ")
    assert "notes#gone-9 … and 3 more (13)" in text and "gone-10" not in text


def test_prose_leaves_out_code() -> None:
    text = "a `[x](#one)` b\n~~~\n[y](#two)\n~~~\n[z](#three)"
    assert mem.local_links(mem.prose(text)) == ["three"]


async def test_mcp_import_and_link_warnings(
    tmp_path: Path, outside: Path, live_server: Callable[[], LiveServer]
) -> None:
    path = write(outside, "spec.md", SPEC + "\n[gone](#vouchers) [ok](#payouts)\n")
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", session="worker")
        base = {"project": "acme-shop", "doc": "notes", "reason": "import", "session": "worker"}
        text = await call(
            client, "doc_update", **base, body="-", base_version=0, from_path=str(path)
        )
        size = len(path.read_text(encoding="utf-8").strip())
        assert "acme-shop/notes saved as version 1" in text
        assert f"Imported from {path} ({size} characters)." in text
        assert "Warning: links without a target: notes#vouchers (1)" in text
        read = await call(client, "doc_get", project="acme-shop", doc="notes")
        assert read.endswith("Warning: links without a target: notes#vouchers (1)")
        part = await call(client, "doc_get", project="acme-shop", doc="notes", section="payouts")
        assert "Warning" not in part
        fixed = await call(client, "doc_update", **base, body=SPEC, base_version=1)
        assert "Warning" not in fixed
        refused = await client.call_tool(
            "doc_update",
            {**base, "body": "-", "base_version": 2, "from_path": str(path), "delete": True},
        )
        assert refused.is_error and "does not go with delete" in refused.content[0].text
