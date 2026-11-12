"""`relaybay import` and `export`: the plan, idempotence, conflicts, secrets, paths, round trip."""

import asyncio
import contextlib
import hashlib
import json
import re
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path

import httpx
import pytest

from relaybay import cli
from relaybay.db.connection import connect, migrate
from relaybay.domain.environments import find_secrets
from relaybay.domain.markdown_layout import (
    Aliases,
    load_aliases,
    parse_note,
    parse_registry,
    place_of,
    render_registry,
    split_frontmatter,
    split_stage,
)
from relaybay.markdown_export import FileAction, Wanted, _claim, file_hash, read_manifest
from relaybay.markdown_import import FILE_MAX_BYTES, Plan, apply_plan, make_plan, scan
from relaybay.service import Service
from tests.test_migrations import LATEST

FIXTURES = Path(__file__).parent / "fixtures"
SETUP = FIXTURES / "markdown_setup"
ALIASES = FIXTURES / "markdown_aliases.json"
# Assembled here so no file in the repository looks like it holds a token.
FAKE_TOKEN = "ghp_" + "a1B2c3D4" * 5


@pytest.fixture
def root(tmp_path: Path) -> Path:
    target = tmp_path / "setup"
    shutil.copytree(SETUP, target)
    return target


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "data"


def aliases() -> Aliases:
    return load_aliases(json.loads(ALIASES.read_text(encoding="utf-8")))


def run_cli(home: Path, command: str, root: Path, *more: str) -> int:
    # In a thread of its own: the command starts an event loop, and async tests already run in one.
    args = [command, str(root), "--home", str(home), *more]
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(cli.main, args).result()


def plan_hash(out: str) -> str:
    found = re.search(r"Plan hash: ([0-9a-f]{16})", out)
    assert found, out
    return found.group(1)


def import_args() -> tuple[str, ...]:
    return ("--aliases", str(ALIASES), "--retro-project", "acme-shop")


def apply_fixture(home: Path, root: Path, capsys: pytest.CaptureFixture[str]) -> str:
    code = run_cli(home, "import", root, *import_args(), "--apply")
    out = capsys.readouterr().out
    assert code == 0, out
    return out


async def open_service(home: Path) -> Service:
    service = Service(home / "relaybay.sqlite", attachments_dir=home / "no-attachments")
    await service.start()
    return service


async def fresh_plan(service: Service, root: Path, **options: object) -> Plan:
    return await make_plan(service, root, aliases=aliases(), retro_project="acme-shop", **options)


def counts(plan: Plan) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in plan.items:
        found[item.action] = found.get(item.action, 0) + 1
    return found


def item(plan: Plan, kind: str, name: str):
    return next(i for i in plan.items if i.kind == kind and i.item == name)


def tree(folder: Path) -> dict[str, bytes]:
    return {
        p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()
    }


# --- the mapping ---------------------------------------------------------------------------


async def test_apply_maps_every_kind(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    service = await open_service(home)
    try:
        slugs = {p.project.slug for p in await service.list_projects()}
        assert slugs == {"acme-shop", "demo-api"}
        assert (await service.get_project("acme-shop")).name == "Acme Shop"

        _, brief = await service.get_document("acme-shop", "brief")
        assert "pat@example.com" in brief  # contacts stay in the brief
        spec, _ = await service.get_document("acme-shop", "spec")
        assert spec.kind == "spec"
        notes_doc, _ = await service.get_document("acme-shop", "deploy-notes")
        assert notes_doc.kind == "doc"
        runbook, _ = await service.get_document("demo-api", "runbook")
        assert runbook.kind == "runbook"
        retro, _ = await service.get_document("acme-shop", "retro")
        assert (retro.kind, retro.version) == ("retro", 1)

        tasks = {t.title: t for t in await service.list_tasks(include_done=True)}
        gift = tasks["Gift cards at checkout"]
        assert (gift.stage, gift.stage_note, gift.waiting_on) == (
            "dev",
            "refunds still open",
            "owner",
        )
        assert gift.next_step == "Decide how partial refunds work"
        assert gift.doc_path == "relaybay://doc/acme-shop/task-shop-51"
        assert gift.description == "Let shoppers pay part of an order with a gift card."
        task_doc, text = await service.get_document("acme-shop", "task-shop-51")
        assert task_doc.kind == "task" and "First draft" in text
        assert (tasks["Holiday banner"].stage, tasks["Holiday banner"].stage_note) == (
            "live",
            "until January 6",
        )
        # An unknown stage: plan, with the original text as the note.
        points = tasks["Loyalty points"]
        assert (points.stage, points.stage_note) == ("plan", "someday, if the shop grows")
        # A column and stage from the aliases file.
        assert tasks["Rate limiting per API key"].stage == "dev"
        assert tasks["Remove the v0 endpoints"].description.startswith("v0 has had no traffic")
        # A task folder without a registry row: a task in stage plan, first paragraph.
        saved = tasks["Saved addresses"]
        assert saved.stage == "plan"
        assert saved.description == (
            "Signed-in shoppers pick a saved address at checkout instead of typing it."
        )

        _, decisions = await service.list_decisions("acme-shop", limit=50)
        by_text = {d.text: d for d in decisions}
        assert (
            "Partial refunds of gift-card orders go back to the gift card first." in by_text
        )  # a wrapped bullet is one decision
        cents = by_text["Prices are stored in cents."]
        assert (cents.scope, cents.source_kind, cents.source_ref, cents.source_text) == (
            "project",
            "message",
            "import",
            "projects/acme-shop.md",
        )
        scoped = by_text["A gift card covers at most the order total."]
        assert (scoped.scope, scoped.task_key) == ("task", gift.key)

        notes = {n.title: n for n in await service.all_notes()}
        assert set(notes) == {
            "The owner reads on a phone",
            "First line format",
            "Payment provider test cards",
        }
        phone = notes["The owner reads on a phone"]
        assert (phone.scope, phone.kind, phone.links) == ("global", "user", "first-line-format")
        assert phone.hook == "Keep the first line of an update short."  # description first
        heading = notes["First line format"]
        assert heading.kind == "reference" and heading.body.startswith("The first line says")
        cards = notes["Payment provider test cards"]
        assert cards.kind == "reference"  # an unknown type
        assert cards.hook == "The provider publishes test card numbers in its docs."
    finally:
        await service.close()


def test_retro_is_skipped_without_a_project(home: Path, root: Path, capsys) -> None:
    code = run_cli(home, "import", root, "--aliases", str(ALIASES))
    out = capsys.readouterr().out
    assert code == 0
    assert "--retro-project" in out and "skipped   file retro.md" in out


def test_without_aliases_the_other_table_only_warns(home: Path, root: Path, capsys) -> None:
    assert run_cli(home, "import", root) == 0
    out = capsys.readouterr().out
    assert "a table without project and task columns was skipped" in out
    assert "demo-api/rate-limiting" not in out


def test_unknown_files_are_listed_and_ignored(home: Path, root: Path, capsys) -> None:
    (root / "projects" / "acme-shop" / "scratch.md").write_text("# Scratch\n", encoding="utf-8")
    (root / "README.md").write_text("# Readme\n", encoding="utf-8")
    (root / "elsewhere").mkdir()
    (root / "elsewhere" / "other.md").write_text("# Other\n", encoding="utf-8")
    assert run_cli(home, "import", root, *import_args()) == 0
    out = capsys.readouterr().out
    assert "skipped   file projects/acme-shop/scratch.md" in out
    assert "skipped   file README.md" in out
    assert "other.md" not in out


# --- dry run and hash ------------------------------------------------------------------------


def test_dry_run_writes_nothing(home: Path, root: Path, capsys) -> None:
    before = tree(root)
    assert run_cli(home, "import", root, *import_args()) == 0
    out = capsys.readouterr().out
    assert "(dry run)" in out and "Nothing written." in out
    assert not home.exists()  # not even the database
    assert tree(root) == before


async def test_dry_run_on_a_board_writes_nothing(home: Path, root: Path, capsys) -> None:
    service = await open_service(home)
    await service.upsert_project("demo-api")
    last = await service.last_event_id()
    await service.close()
    db = home / "relaybay.sqlite"
    before = db.read_bytes()
    assert run_cli(home, "import", root, *import_args()) == 0
    capsys.readouterr()
    assert db.read_bytes() == before
    service = await open_service(home)
    try:
        assert await service.last_event_id() == last
        assert await service.import_items() == []
    finally:
        await service.close()


def test_expect_writes_the_plan_it_saw(home: Path, root: Path, capsys) -> None:
    assert run_cli(home, "import", root, *import_args()) == 0
    expected = plan_hash(capsys.readouterr().out)
    code = run_cli(home, "import", root, *import_args(), "--apply", "--expect", expected)
    out = capsys.readouterr().out
    assert code == 0 and "(applied)" in out


async def test_stale_hash_is_refused(home: Path, root: Path, capsys) -> None:
    assert run_cli(home, "import", root, *import_args()) == 0
    expected = plan_hash(capsys.readouterr().out)
    spec = root / "projects" / "acme-shop" / "docs" / "spec.md"
    spec.write_text(spec.read_text(encoding="utf-8") + "\nMore.\n", encoding="utf-8")
    code = run_cli(home, "import", root, *import_args(), "--apply", "--expect", expected)
    out = capsys.readouterr().out
    assert code == 1 and "not the one you approved" in out
    service = await open_service(home)
    try:
        assert await service.list_projects() == []
    finally:
        await service.close()


def test_expect_needs_apply(home: Path, root: Path, capsys) -> None:
    assert run_cli(home, "import", root, "--expect", "abc") == 2


def test_missing_root_and_bad_aliases(home: Path, tmp_path: Path, root: Path, capsys) -> None:
    assert run_cli(home, "import", tmp_path / "nowhere") == 2
    broken = tmp_path / "aliases.json"
    broken.write_text('{"stages": {"x": "nowhere"}}', encoding="utf-8")
    assert run_cli(home, "import", root, "--aliases", str(broken)) == 2
    assert "nowhere" in capsys.readouterr().err


# --- idempotence -----------------------------------------------------------------------------


async def test_rerun_writes_nothing_and_no_events(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    service = await open_service(home)
    try:
        last = await service.last_event_id()
        items = await service.import_items()
        plan = await fresh_plan(service, root)
        assert set(counts(plan)) == {"unchanged", "skipped"}
        assert not plan.writes
        await apply_plan(service, plan)
        assert await service.last_event_id() == last
        assert await service.import_items() == items
    finally:
        await service.close()


async def test_a_changed_file_updates(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    spec = root / "projects" / "acme-shop" / "docs" / "spec.md"
    spec.write_text(spec.read_text(encoding="utf-8") + "\n## Refunds\n\nNew.\n", encoding="utf-8")
    note = root / "memory" / "first-line-format.md"
    note.write_text("# First line format\n\nA new rule.\n", encoding="utf-8")
    registry = root / "TASKS.md"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "| test | owner | Try the suggestions on a phone |", "| shipped | | |"
        ),
        encoding="utf-8",
    )
    service = await open_service(home)
    try:
        plan = await fresh_plan(service, root)
        assert counts(plan)["update"] == 3
        changed = item(plan, "document", "acme-shop/spec")
        assert any(line.startswith("+## Refunds") for line in changed.diff)
        await apply_plan(service, plan)
        found, history = await service.document_history("acme-shop", "spec")
        assert found.version == 2 and history[0].reason == "import"
        notes = {n.title: n for n in await service.all_notes()}
        assert notes["First line format"].body == "A new rule."
        assert notes["First line format"].version == 2
        tasks = {t.title: t for t in await service.list_tasks(include_done=True)}
        task = tasks["Search suggestions"]
        # Only where the file has a value: emptied cells leave the fields alone.
        assert (task.stage, task.waiting_on) == ("live", "owner")
        assert not (await fresh_plan(service, root)).writes
    finally:
        await service.close()


async def test_an_edited_bullet_supersedes(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    brief = root / "projects" / "acme-shop.md"
    text = brief.read_text(encoding="utf-8")
    brief.write_text(
        text.replace("Prices are stored in cents.", "Prices are stored in whole cents.")
        + "- A brand new decision about something else entirely.\n",
        encoding="utf-8",
    )
    service = await open_service(home)
    try:
        plan = await fresh_plan(service, root)
        await apply_plan(service, plan)
        assert not plan.failed
        _, decisions = await service.list_decisions("acme-shop", include_superseded=True, limit=50)
        by_text = {d.text: d for d in decisions}
        old = by_text["Prices are stored in cents."]
        new = by_text["Prices are stored in whole cents."]
        assert old.superseded_by == new.id and new.current
        assert by_text["A brand new decision about something else entirely."].current
        assert not (await fresh_plan(service, root)).writes
    finally:
        await service.close()


async def test_a_removed_bullet_leaves_the_decision(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    brief = root / "projects" / "acme-shop.md"
    brief.write_text(
        brief.read_text(encoding="utf-8").replace("- Prices are stored in cents.\n", ""),
        encoding="utf-8",
    )
    service = await open_service(home)
    try:
        plan = await fresh_plan(service, root)
        assert any(
            i.kind == "decision" and i.action == "skipped" and "stays" in i.detail
            for i in plan.items
        )
        assert not plan.failed
        await apply_plan(service, plan)
        _, decisions = await service.list_decisions("acme-shop", limit=50)
        assert "Prices are stored in cents." in {d.text for d in decisions}
    finally:
        await service.close()


async def test_decision_superseded_on_the_board_is_not_added_again(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    service = await open_service(home)
    try:
        _, decisions = await service.list_decisions("acme-shop", limit=50)
        old = next(d for d in decisions if d.text == "Prices are stored in cents.")
        await service.add_decision("acme-shop", "Prices are decimals.", supersedes=old.id)
        plan = await fresh_plan(service, root)
        skipped = [i for i in plan.items if i.kind == "decision" and i.action == "skipped"]
        assert len(skipped) == 1 and "superseded on the board" in skipped[0].detail
        assert not plan.failed and not any(
            i.kind == "decision" and i.action == "add" for i in plan.items
        )
    finally:
        await service.close()


# --- conflicts -------------------------------------------------------------------------------


async def test_conflict_and_overwrite(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    service = await open_service(home)
    try:
        await service.update_document("acme-shop", "spec", "# Board text\n", "by hand", 1)
        await service.set_stage("acme-shop-1", "review", actor="owner")
        await service.close()

        code = run_cli(home, "import", root, *import_args(), "--apply")
        out = capsys.readouterr().out
        assert code == 1
        assert "conflict  document acme-shop/spec" in out
        assert "changed on the board since the last import" in out

        service = await open_service(home)
        found, text = await service.get_document("acme-shop", "spec")
        assert (found.version, text) == (2, "# Board text")
        assert (await service.get_task("acme-shop-1")).stage == "review"
        await service.close()

        code = run_cli(home, "import", root, *import_args(), "--apply", "--overwrite")
        capsys.readouterr()
        assert code == 0

        service = await open_service(home)
        found, text = await service.get_document("acme-shop", "spec")
        assert found.version == 3 and text.startswith("# Acme Shop specification")
        _, old = await service.get_document("acme-shop", "spec", version=2)
        assert old == "# Board text"  # old versions stay
        assert not (await fresh_plan(service, root)).writes
    finally:
        await service.close()


async def test_target_not_from_an_import_is_a_conflict(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = await open_service(home)
    await service.upsert_project("acme-shop")
    await service.update_document("acme-shop", "spec", "# Mine\n", "by hand", 0)
    spec_body = (root / "projects" / "demo-api" / "docs" / "runbook.md").read_text(encoding="utf-8")
    await service.upsert_project("demo-api")
    await service.update_document("demo-api", "runbook", spec_body, "same text", 0)
    try:
        plan = await fresh_plan(service, root)
        assert item(plan, "document", "acme-shop/spec").action == "conflict"
        adopted = item(plan, "document", "demo-api/runbook")
        assert (adopted.action, adopted.record) == ("unchanged", True)
    finally:
        await service.close()


async def test_board_edit_of_a_description_alone_is_no_conflict(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    service = await open_service(home)
    try:
        tasks = {t.title: t for t in await service.list_tasks()}
        key = tasks["Gift cards at checkout"].key
        await service.update_task(key, {"description": "Rewritten on the board."}, actor="owner")
        plan = await fresh_plan(service, root)
        assert not plan.failed and not plan.writes
    finally:
        await service.close()


# --- refusals --------------------------------------------------------------------------------


async def test_secret_is_refused_and_the_rest_goes_on(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = root / "projects" / "acme-shop" / "docs" / "spec.md"
    spec.write_text(f"# Spec\n\nFirst.\nThe token is {FAKE_TOKEN} here.\n", encoding="utf-8")
    (root / "memory" / "leak.md").write_text(f"# Leak\n\n{FAKE_TOKEN}\n", encoding="utf-8")
    registry = root / "TASKS.md"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "Decide how partial refunds work", f"Use {FAKE_TOKEN}"
        ),
        encoding="utf-8",
    )
    code = run_cli(home, "import", root, *import_args(), "--apply")
    captured = capsys.readouterr()
    assert code == 1
    assert FAKE_TOKEN not in captured.out + captured.err
    assert "refused   document acme-shop/spec" in captured.out
    assert "line 4 (github-token)" in captured.out
    assert "refused   note global/leak" in captured.out
    assert "refused   task TASKS.md:7" in captured.out and "line 7" in captured.out
    # Its task folder waits with it; it must not come in as a task of its own.
    assert "skipped   file projects/acme-shop/tasks/SHOP-51/TASK.md" in captured.out
    service = await open_service(home)
    try:
        _, documents = await service.list_documents("acme-shop")
        slugs = {d.slug for d in documents}
        assert "spec" not in slugs and "brief" in slugs
        assert "Leak" not in {n.title for n in await service.all_notes()}
        titles = {t.title for t in await service.list_tasks(include_done=True)}
        assert "Gift cards at checkout" not in titles and "Search suggestions" in titles
    finally:
        await service.close()


def test_find_secrets_sees_what_only_the_whole_text_shows() -> None:
    text = "# Runbook\n\nConnect with\n--password\nabcdef123\nand go."
    assert find_secrets(text) == [(5, "password-flag")]
    assert find_secrets("nothing here") == []
    assert find_secrets(f"a\n{FAKE_TOKEN}\nb") == [(2, "github-token")]


def test_bad_encoding_is_reported_per_file(home: Path, root: Path, capsys) -> None:
    (root / "memory" / "broken.md").write_bytes(b"# Broken \xff\xfe text\n")
    code = run_cli(home, "import", root, *import_args(), "--apply")
    out = capsys.readouterr().out
    assert code == 1
    assert "error     file memory/broken.md: not valid UTF-8 (byte 9)" in out
    assert "add" in out  # the rest went on


def test_a_too_large_file_is_refused(home: Path, root: Path, capsys) -> None:
    (root / "memory" / "huge.md").write_bytes(b"x" * (FILE_MAX_BYTES + 1))
    code = run_cli(home, "import", root, *import_args())
    out = capsys.readouterr().out
    assert code == 1 and "refused   file memory/huge.md" in out and "the limit is" in out


def test_too_long_for_a_note_is_refused(home: Path, root: Path, capsys) -> None:
    (root / "memory" / "long.md").write_text("# Long\n\n" + "x" * 9000, encoding="utf-8")
    code = run_cli(home, "import", root, *import_args())
    assert code == 1 and "refused   note global/long" in capsys.readouterr().out


def test_bad_names_cannot_escape(home: Path, root: Path, capsys) -> None:
    (root / "projects" / "Bad Name.md").write_text("# Bad\n", encoding="utf-8")
    (root / "projects" / "acme-shop" / "docs" / "Not A Slug.md").write_text("x", encoding="utf-8")
    registry = root / "TASKS.md"
    registry.write_text(
        registry.read_text(encoding="utf-8")
        + "\n| Project | Task | Document |\n|---|---|---|\n"
        + "| ../../etc | Escape by project | |\n"
        + "| acme-shop | Escape by document | ../../../secret.md |\n",
        encoding="utf-8",
    )
    code = run_cli(home, "import", root, *import_args())
    out = capsys.readouterr().out
    assert code == 1
    assert "refused   file projects/Bad Name.md" in out
    assert "refused   file projects/acme-shop/docs/Not A Slug.md" in out
    assert "invalid project name" in out
    assert "climbs out of the root" in out


def _link_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symbolic links are not permitted here")


def test_symlinks_leaving_the_root_are_refused(tmp_path: Path, root: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("# Secret\n", encoding="utf-8")
    _link_or_skip(root / "memory" / "linked.md", outside / "secret.md")
    _link_or_skip(root / "projects" / "linked", outside, directory=True)
    files, items = scan(root)
    refused = {i.source: i.detail for i in items if i.action == "refused"}
    assert set(refused) == {"memory/linked.md", "projects/linked"}
    assert "out of the root" in refused["memory/linked.md"]
    assert not any("Secret" in f.text for f in files)


def test_a_link_inside_the_root_is_read(root: Path) -> None:
    _link_or_skip(root / "memory" / "alias.md", root / "memory" / "first-line-format.md")
    files, items = scan(root)
    assert "memory/alias.md" in {f.rel for f in files}
    assert not [i for i in items if i.action == "refused"]


# --- export ----------------------------------------------------------------------------------


def test_round_trip_changes_nothing(home: Path, root: Path, tmp_path: Path, capsys) -> None:
    apply_fixture(home, root, capsys)
    out_dir = tmp_path / "exported"
    code = run_cli(home, "export", out_dir)
    out = capsys.readouterr().out
    assert code == 0 and "(dry run)" in out and not out_dir.exists()

    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    written = tree(out_dir)
    assert "TASKS.md" in written and "projects/acme-shop/decisions.md" in written
    assert "projects/acme-shop/tasks/SHOP-51/TASK.md" in written
    assert "projects/acme-shop/tasks/saved-addresses/TASK.md" in written
    assert "memory/first-line-format.md" in written
    assert "retro.md" in written

    # Import from the export: nothing to write, no events.
    code = run_cli(home, "import", out_dir, "--retro-project", "acme-shop")
    out = capsys.readouterr().out
    assert code == 0
    assert not re.search(r"^  (add|update|conflict) ", out, re.M), out

    # The same data gives the same bytes.
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    assert tree(out_dir) == written


async def test_round_trip_after_board_work(
    home: Path, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    service = await open_service(home)
    await service.update_document("acme-shop", "spec", "# New spec\n", "by hand", 1)
    await service.set_stage("acme-shop-1", "review", actor="owner")
    await service.update_task("acme-shop-1", {"next_step": "See | pipes\nand lines"}, actor="owner")
    await service.create_task("demo-api", "Made on the board", stage="dev", actor="owner")
    await service.add_decision("demo-api", "A board decision.", decided_by="owner")
    await service.add_note("global", "feedback", "Board note", "Body [[x]].", op="add")
    last = await service.last_event_id()
    await service.close()

    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    decisions = (out_dir / "projects" / "demo-api" / "decisions.md").read_text(encoding="utf-8")
    assert "A board decision." in decisions and "Decided by owner." in decisions

    service = await open_service(home)
    try:
        plan = await make_plan(service, out_dir, retro_project="acme-shop")
        assert not plan.failed and not any(i.action in ("add", "update") for i in plan.items)
        await apply_plan(service, plan)  # at most bookkeeping
        assert await service.last_event_id() == last
        assert not (await make_plan(service, out_dir, retro_project="acme-shop")).writes
        assert (await service.get_task("acme-shop-1")).next_step == "See | pipes\nand lines"
    finally:
        await service.close()


async def test_export_into_the_imported_folder_keeps_handwritten_files(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A registry that names every task: one that lacks a row is a real difference.
    registry = root / "TASKS.md"
    loyalty = "| acme-shop | Loyalty points | someday, if the shop grows | | | |\n"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            loyalty, loyalty + "| acme-shop | Saved addresses | plan | | | |\n"
        ),
        encoding="utf-8",
    )
    apply_fixture(home, root, capsys)
    before = tree(root)
    code = run_cli(home, "export", root, "--apply", "--aliases", str(ALIASES))
    out = capsys.readouterr().out
    assert code == 0, out
    after = tree(root)
    new = set(after) - set(before)
    assert new == {
        ".relaybay-export.json",
        "projects/acme-shop/decisions.md",
        "projects/demo-api/decisions.md",
    }
    assert {k: after[k] for k in before} == before
    # And the import still sees nothing to do there.
    service = await open_service(home)
    try:
        assert not (await fresh_plan(service, root)).writes
    finally:
        await service.close()


async def test_superseded_decision_is_exported(
    home: Path, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    service = await open_service(home)
    _, decisions = await service.list_decisions("acme-shop", limit=50)
    old = next(d for d in decisions if d.text == "Prices are stored in cents.")
    new = await service.add_decision("acme-shop", "Prices are decimals.", supersedes=old.id)
    await service.close()
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    text = (out_dir / "projects" / "acme-shop" / "decisions.md").read_text(encoding="utf-8")
    current, superseded = text.split("## Superseded")
    assert "Prices are decimals." in current
    assert "Prices are stored in cents." in superseded and f"Replaced by #{new.id}" in superseded


async def test_export_manifest_protects_hand_changes(
    home: Path, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    manifest = read_manifest(out_dir)
    spec = out_dir / "projects" / "acme-shop" / "docs" / "spec.md"
    assert manifest["projects/acme-shop/docs/spec.md"]["written"] is True

    service = await open_service(home)
    await service.update_document("acme-shop", "spec", "# From the board\n", "by hand", 1)
    await service.close()

    # Not touched by hand: the board's new text goes in.
    assert run_cli(home, "export", out_dir, "--apply") == 0
    assert "update    projects/acme-shop/docs/spec.md" in capsys.readouterr().out
    assert spec.read_text(encoding="utf-8") == "# From the board\n"

    service = await open_service(home)
    await service.update_document("acme-shop", "spec", "# Board again\n", "by hand", 2)
    await service.close()
    spec.write_text("# My own words\n", encoding="utf-8")
    assert run_cli(home, "export", out_dir, "--apply") == 1
    assert "changed by hand since the last export" in capsys.readouterr().out
    assert spec.read_text(encoding="utf-8") == "# My own words\n"

    assert run_cli(home, "export", out_dir, "--apply", "--force") == 0
    capsys.readouterr()
    assert spec.read_text(encoding="utf-8") == "# Board again\n"


async def test_export_removes_only_what_it_wrote(
    home: Path, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    foreign = out_dir / "memory" / "mine.md"
    foreign.write_text("# Mine\n\nMy own note.\n", encoding="utf-8")
    note_file = out_dir / "memory" / "first-line-format.md"
    other_file = out_dir / "memory" / "payment-test-cards.md"
    other_file.write_text(other_file.read_text(encoding="utf-8") + "A hand edit.\n", "utf-8")

    service = await open_service(home)
    notes = {n.title: n for n in await service.all_notes()}
    for title in ("First line format", "Payment provider test cards"):
        await service.add_note(
            "global",
            "reference",
            f"{title} v2",
            "Replaced.",
            op="supersede",
            target=notes[title].id,
        )
    await service.close()

    code = run_cli(home, "export", out_dir, "--apply")
    out = capsys.readouterr().out
    assert code == 1
    assert "remove    memory/first-line-format.md" in out
    assert "conflict  memory/payment-test-cards.md" in out
    assert not note_file.exists() and other_file.exists() and foreign.exists()
    assert run_cli(home, "export", out_dir, "--apply", "--force") == 0
    capsys.readouterr()
    assert not other_file.exists() and foreign.exists()


def test_export_refuses_a_file_it_did_not_write(home: Path, root: Path, tmp_path: Path, capsys):
    apply_fixture(home, root, capsys)
    out_dir = tmp_path / "exported"
    target = out_dir / "projects" / "acme-shop" / "docs"
    target.mkdir(parents=True)
    (target / "spec.md").write_text("# Somebody else's file\n", encoding="utf-8")
    assert run_cli(home, "export", out_dir, "--apply") == 1
    assert "not written by export" in capsys.readouterr().out
    assert (target / "spec.md").read_text(encoding="utf-8") == "# Somebody else's file\n"


def test_export_broken_manifest_stops(home: Path, root: Path, tmp_path: Path, capsys) -> None:
    apply_fixture(home, root, capsys)
    out_dir = tmp_path / "exported"
    out_dir.mkdir()
    (out_dir / ".relaybay-export.json").write_text("{not json", encoding="utf-8")
    assert run_cli(home, "export", out_dir, "--apply") == 1
    assert "cannot be read" in capsys.readouterr().err
    assert len(tree(out_dir)) == 1


def test_export_will_not_write_through_a_link(home: Path, root: Path, tmp_path: Path, capsys):
    apply_fixture(home, root, capsys)
    outside = tmp_path / "outside"
    outside.mkdir()
    out_dir = tmp_path / "exported"
    out_dir.mkdir()
    _link_or_skip(out_dir / "memory", outside, directory=True)
    assert run_cli(home, "export", out_dir, "--apply") == 1
    assert "leads out of the root" in capsys.readouterr().out
    assert list(outside.iterdir()) == []


# --- upgrade ---------------------------------------------------------------------------------


async def test_upgrade_from_0014_with_data(home: Path, root: Path, capsys) -> None:
    await asyncio.to_thread(home.mkdir)
    db = home / "relaybay.sqlite"
    folder = resources.files("relaybay.db").joinpath("migrations")
    conn = await connect(db)
    now = "2026-01-01T00:00:00.000+00:00"
    try:
        for entry in sorted(folder.iterdir(), key=lambda e: e.name):
            if entry.name.endswith(".sql") and int(entry.name[:4]) <= 14:
                await conn.executescript(entry.read_text(encoding="utf-8"))
        await conn.execute("PRAGMA user_version = 14")
        await conn.execute(
            "INSERT INTO project (slug, name, task_seq, created_at, updated_at)"
            " VALUES ('acme-shop', 'Old name', 1, ?, ?)",
            (now, now),
        )
        await conn.execute(
            "INSERT INTO task (project_id, key, title, stage, created_at, updated_at)"
            " VALUES (1, 'acme-shop-1', 'Search suggestions', 'dev', ?, ?)",
            (now, now),
        )
    finally:
        await conn.close()

    # A dry run leaves an old database as it is.
    assert run_cli(home, "import", root, *import_args()) == 1  # the task is not from an import
    out = capsys.readouterr().out
    assert "conflict  task acme-shop/search-suggestions" in out
    conn = await connect(db)
    try:
        async with conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 14
        assert await migrate(conn) == LATEST
        async with conn.execute("SELECT COUNT(*) FROM import_item") as cur:
            assert (await cur.fetchone())[0] == 0
        async with conn.execute("SELECT name FROM project") as cur:
            assert (await cur.fetchone())[0] == "Old name"
    finally:
        await conn.close()


# --- review findings ---------------------------------------------------------------------------


def write_registry(root: Path, header: str, *rows: str) -> None:
    rule = "|" + "---|" * header.count("|")
    (root / "TASKS.md").write_text(
        "\n".join([f"| {header} |", rule, *(f"| {r} |" for r in rows), ""]), encoding="utf-8"
    )


def rows_of_item_table(home: Path) -> list[tuple]:
    with contextlib.closing(sqlite3.connect(home / "relaybay.sqlite")) as conn:
        return conn.execute("SELECT kind, item FROM import_item").fetchall()


@pytest.mark.parametrize(
    "row",
    [
        ("project | ref | task", f"acme-shop | {FAKE_TOKEN} | Plain title"),
        ("project | task", f"acme-shop | Rotate {FAKE_TOKEN}"),
        ("project | task | document", f"acme-shop | Plain | tasks/{FAKE_TOKEN}/TASK.md"),
        ("project | task | document", f"acme-shop | Plain | ../../{FAKE_TOKEN}"),
        ("project | task | stage", f"acme-shop | Plain | {FAKE_TOKEN}"),
        ("project | task", f"BAD_{FAKE_TOKEN} | Plain"),
    ],
)
def test_secret_in_a_cell_never_names_anything(
    home: Path, tmp_path: Path, capsys, row: tuple[str, str]
) -> None:
    root = tmp_path / "secret-cell"
    root.mkdir()
    write_registry(root, row[0], row[1])
    code = run_cli(home, "import", root, "--apply")
    captured = capsys.readouterr()
    assert code == 1 and "task TASKS.md:3" in captured.out
    assert FAKE_TOKEN.casefold() not in (captured.out + captured.err).casefold()
    assert rows_of_item_table(home) == []
    assert run_cli(home, "export", tmp_path / "out", "--apply") == 0
    capsys.readouterr()
    exported = b"".join(tree(tmp_path / "out").values()).decode("utf-8")
    assert FAKE_TOKEN.casefold() not in exported.casefold()


def test_secret_in_a_path_is_hidden(home: Path, tmp_path: Path, capsys) -> None:
    root = tmp_path / "secret-path"
    (root / "memory").mkdir(parents=True)
    (root / "memory" / f"{FAKE_TOKEN}.md").write_text("Plain body\n", encoding="utf-8")
    folder = root / "projects" / "acme-shop" / "tasks" / FAKE_TOKEN
    folder.mkdir(parents=True)
    (folder / "TASK.md").write_text("# Plain\n\nPlain.\n", encoding="utf-8")
    code = run_cli(home, "import", root, "--apply")
    captured = capsys.readouterr()
    assert code == 1
    assert "refused   file memory/(hidden name)" in captured.out
    assert "refused   file projects/acme-shop/tasks/(hidden name)" in captured.out
    assert FAKE_TOKEN.casefold() not in (captured.out + captured.err).casefold()
    assert rows_of_item_table(home) == []


async def test_export_hides_the_diff_of_a_file_with_a_secret(
    home: Path, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    spec = out_dir / "projects" / "acme-shop" / "docs" / "spec.md"
    spec.write_text(f"# Spec\n\nThe token is {FAKE_TOKEN}.\n", encoding="utf-8")
    assert run_cli(home, "export", out_dir) == 1
    out = capsys.readouterr().out
    assert FAKE_TOKEN not in out and "diff hidden: line 3 (github-token)" in out


async def test_a_hand_edit_that_says_the_same_stays_protected(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = await open_service(home)
    await service.upsert_project("acme-shop")
    task = await service.create_task("acme-shop", "First")
    await service.close()
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    registry = out_dir / "TASKS.md"
    registry.write_text(registry.read_text(encoding="utf-8") + "\nA hand note.\n", encoding="utf-8")
    base = read_manifest(out_dir)["TASKS.md"]["sha256"]
    assert run_cli(home, "export", out_dir, "--apply") == 0  # says the same
    assert read_manifest(out_dir)["TASKS.md"]["sha256"] == base
    capsys.readouterr()

    service = await open_service(home)
    await service.update_task(task.key, {"next_step": "From the board"})
    await service.close()
    assert run_cli(home, "export", out_dir, "--apply") == 1
    assert "changed by hand" in capsys.readouterr().out
    assert "A hand note." in registry.read_text(encoding="utf-8")
    assert run_cli(home, "export", out_dir, "--apply", "--force") == 0
    assert "A hand note." not in registry.read_text(encoding="utf-8")


async def test_only_line_endings_move_the_manifest_base(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = await open_service(home)
    await service.upsert_project("acme-shop")
    await service.update_document("acme-shop", "spec", "# Spec\n\nOne.\n", "first", 0)
    await service.close()
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    spec = out_dir / "projects" / "acme-shop" / "docs" / "spec.md"
    spec.write_bytes(spec.read_bytes().replace(b"\n", b"\r\n"))
    assert run_cli(home, "export", out_dir, "--apply") == 0
    assert (
        read_manifest(out_dir)["projects/acme-shop/docs/spec.md"]["sha256"]
        == hashlib.sha256(spec.read_bytes()).hexdigest()
    )
    service = await open_service(home)
    await service.update_document("acme-shop", "spec", "# Spec\n\nTwo.\n", "second", 1)
    await service.close()
    assert run_cli(home, "export", out_dir, "--apply") == 0  # nothing of the owner's was lost
    capsys.readouterr()
    assert b"Two." in spec.read_bytes()


async def test_a_file_export_did_not_write_needs_force(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    assert run_cli(home, "export", root, "--apply", "--aliases", str(ALIASES)) == 1
    capsys.readouterr()
    entry = read_manifest(root)["projects/acme-shop/docs/spec.md"]
    assert entry["written"] is False
    spec = root / "projects" / "acme-shop" / "docs" / "spec.md"
    service = await open_service(home)
    await service.update_document("acme-shop", "spec", "# From the board\n", "by hand", 1)
    await service.close()
    assert file_hash(spec.read_bytes()) == entry["sha256"]  # a matching hash changes nothing
    assert run_cli(home, "export", root, "--apply", "--aliases", str(ALIASES)) == 1
    assert "not written by export" in capsys.readouterr().out
    assert spec.read_text(encoding="utf-8").startswith("# Acme Shop specification")

    assert run_cli(home, "export", root, "--apply", "--force", "--aliases", str(ALIASES)) == 0
    capsys.readouterr()
    assert spec.read_text(encoding="utf-8") == "# From the board\n"
    assert read_manifest(root)["projects/acme-shop/docs/spec.md"]["written"] is True


@pytest.mark.parametrize("force", [False, True])
async def test_a_hand_edited_manifest_deletes_nothing_outside_the_layout(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], force: bool
) -> None:
    service = await open_service(home)
    await service.upsert_project("acme-shop")
    await service.close()
    root = tmp_path / "root"
    (root / "src").mkdir(parents=True)
    outside = tmp_path / "outside.md"
    inside = root / "src" / "main.py"
    registry = root / "TASKS.md"
    victims = {
        "../outside.md": outside,
        "src/main.py": inside,
        "src/../TASKS.md": registry,
        "./TASKS.md": registry,
        "projects//../../outside.md": outside,
        "memory/..\\..\\outside.md": outside,
        str(outside).replace("\\", "/"): outside,
    }
    for path in victims.values():
        path.write_text("# Mine\n", encoding="utf-8")
    files = {
        rel: {"sha256": file_hash(path.read_bytes()), "written": True}
        for rel, path in victims.items()
    }
    (root / ".relaybay-export.json").write_text(
        json.dumps({"version": 1, "files": files}), encoding="utf-8"
    )
    code = run_cli(home, "export", root, "--apply", *(["--force"] if force else []))
    out = capsys.readouterr().out
    assert code == 1 and out.count("refused") >= len(victims)
    assert all(path.exists() for path in victims.values())


async def test_export_refuses_a_wrong_home(
    home: Path, root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apply_fixture(home, root, capsys)
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    before = tree(out_dir)

    nowhere = tmp_path / "nowhere"
    assert run_cli(nowhere, "export", out_dir, "--apply") == 1
    assert "there is no board" in capsys.readouterr().err
    assert run_cli(nowhere, "export", out_dir) == 1
    capsys.readouterr()
    assert not nowhere.exists()

    empty = tmp_path / "empty"
    service = await open_service(empty)
    await service.close()
    assert run_cli(empty, "export", out_dir, "--apply") == 1
    assert "has no projects" in capsys.readouterr().err
    assert tree(out_dir) == before
    assert run_cli(empty, "export", out_dir) == 0  # a dry run removes nothing anyway
    capsys.readouterr()


async def test_export_brings_tags_and_document_up_to_the_board(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    (root / "memory").mkdir(parents=True)
    note = root / "memory" / "n.md"
    note.write_text(
        "---\nname: Note one\nmetadata:\n  type: user\n  tags: alpha\n---\n\nBody text\n",
        encoding="utf-8",
    )
    write_registry(root, "project | ref | task | document", "acme-shop | t1 | First | docs/a.md")
    assert run_cli(home, "import", root, "--apply") == 0
    assert run_cli(home, "export", root, "--apply") == 0  # says the same: nothing rewritten
    capsys.readouterr()
    assert "alpha" in note.read_text(encoding="utf-8")

    service = await open_service(home)
    found = await service.all_notes()
    await service.update_note(found[0].id, found[0].version, tags="beta")
    task = (await service.list_tasks(project="acme-shop"))[0]
    await service.update_task(task.key, {"doc_path": "docs/b.md"})
    last = await service.last_event_id()
    await service.close()

    # The import names the way out, and the way out works.
    assert run_cli(home, "import", root) == 1
    assert "'relaybay export' brings the file up to date" in capsys.readouterr().out
    assert run_cli(home, "export", root, "--apply") == 1  # hand-written files: not without --force
    capsys.readouterr()
    assert run_cli(home, "export", root, "--apply", "--force") == 0
    capsys.readouterr()
    assert "beta" in note.read_text(encoding="utf-8")
    assert "docs/b.md" in (root / "TASKS.md").read_text(encoding="utf-8")
    assert run_cli(home, "import", root, "--apply") == 0
    assert not re.search(r"^  (add|update|conflict) ", capsys.readouterr().out, re.M)

    # From then on the files are export's own: no --force needed.
    service = await open_service(home)
    await service.update_task(task.key, {"doc_path": "docs/c.md"})
    await service.close()
    assert run_cli(home, "export", root, "--apply") == 0
    assert run_cli(home, "import", root) == 0
    capsys.readouterr()
    service = await open_service(home)
    try:
        assert await service.last_event_id() > last
    finally:
        await service.close()


async def test_explicit_ref_is_never_matched_by_title(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_registry(root, "project | ref | task", "acme-shop | a | Deploy")
    assert run_cli(home, "import", root, "--apply") == 0
    for rows in (
        ("acme-shop | a | Deploy", "acme-shop | b | Deploy"),
        ("acme-shop | c | Deploy", "acme-shop | a | Deploy", "acme-shop | b | Deploy"),
    ):
        write_registry(root, "project | ref | task", *rows)
        assert run_cli(home, "import", root, "--apply") == 0
    capsys.readouterr()
    service = await open_service(home)
    try:
        tasks = await service.list_tasks(project="acme-shop")
        items = [i for i in await service.import_items() if i.kind == "task"]
        assert len(tasks) == 3
        assert len({i.target_id for i in items}) == 3
    finally:
        await service.close()


async def test_title_match_does_not_depend_on_row_order(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = ["acme-shop | Deploy | tasks/one/", "acme-shop | Deploy | tasks/two/"]
    plans = []
    for name, order in (("one", rows), ("two", rows[::-1])):
        root = tmp_path / name
        root.mkdir()
        write_registry(root, "project | task | document", *order)
        data = tmp_path / f"home-{name}"
        service = await open_service(data)
        await service.upsert_project("acme-shop")
        await service.create_task("acme-shop", "Deploy")
        plan = await make_plan(service, root)
        await service.close()
        plans.append(sorted((i.kind, i.item, i.action) for i in plan.items if i.kind == "task"))
    assert plans[0] == plans[1]
    assert {action for _, _, action in plans[0]} == {"add"}


def test_plan_hash_covers_kind_and_role_of_a_new_note(home: Path, tmp_path: Path, capsys) -> None:
    root = tmp_path / "root"
    (root / "memory").mkdir(parents=True)
    note = root / "memory" / "n.md"
    note.write_text("---\nname: N\nmetadata:\n  type: user\n---\n\nBody\n", encoding="utf-8")
    assert run_cli(home, "import", root) == 0
    first = plan_hash(capsys.readouterr().out)
    note.write_text(
        "---\nname: N\nmetadata:\n  type: feedback\n  role: dev\n---\n\nBody\n", encoding="utf-8"
    )
    assert run_cli(home, "import", root) == 0
    assert plan_hash(capsys.readouterr().out) != first
    assert run_cli(home, "import", root, "--apply", "--expect", first) == 1
    capsys.readouterr()


async def test_two_items_on_one_export_path_lose_nothing(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    retro = root / "retro.md"
    retro.write_text("# Retro one\n", encoding="utf-8")
    assert run_cli(home, "import", root, "--apply", "--retro-project", "acme-shop") == 0
    retro.write_text("# Retro two\n", encoding="utf-8")
    assert run_cli(home, "import", root, "--apply", "--retro-project", "demo-api") == 0
    capsys.readouterr()
    service = await open_service(home)
    await service.add_note("global", "reference", "Same", "First body", op="add", force_new=True)
    await service.add_note("global", "reference", "Same", "Second body", op="add", force_new=True)
    await service.close()

    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    capsys.readouterr()
    files = {k: v.decode("utf-8") for k, v in tree(out_dir).items()}
    assert "Retro one" in files["retro.md"]
    assert "Retro two" in files["projects/demo-api/docs/retro.md"]
    same = [text for rel, text in files.items() if rel.startswith("memory/same")]
    assert len(same) == 2
    assert {"First body" in t for t in same} == {True, False}


def test_a_path_with_no_place_left_is_an_error() -> None:
    wanted: dict[str, Wanted] = {"memory/a.md": Wanted("x")}
    problems: list[FileAction] = []
    _claim(wanted, ["MEMORY/A.md"], Wanted("y"), "note #1", problems)
    assert list(wanted) == ["memory/a.md"]
    assert [(p.rel, p.action) for p in problems] == [("MEMORY/A.md", "error")]
    assert "note #1" in problems[0].detail


async def test_literal_br_survives_the_round_trip(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = await open_service(home)
    await service.upsert_project("acme-shop")
    title = "A literal <br> tag"
    await service.create_task("acme-shop", title, next_step="One\nTwo <br> three")
    last = await service.last_event_id()
    await service.close()
    out_dir = tmp_path / "exported"
    assert run_cli(home, "export", out_dir, "--apply") == 0
    rows, _ = parse_registry((out_dir / "TASKS.md").read_text(encoding="utf-8"), Aliases())
    assert (rows[0].title, rows[0].next_step) == (title, "One\nTwo <br> three")
    assert run_cli(home, "import", out_dir, "--apply") == 0
    capsys.readouterr()
    service = await open_service(home)
    try:
        assert await service.last_event_id() == last
    finally:
        await service.close()


async def test_a_stale_hash_does_not_migrate(
    home: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    service = await open_service(home)
    await service.close()
    db = home / "relaybay.sqlite"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        for table in ("backlog_item", "intake_thread", "intake_message", "intake_source"):
            conn.execute(f"DROP TABLE {table}")
        for table, column in (
            ("task", "autonomy"),
            ("task", "answer_received_at"),
            ("task", "answer_message_id"),
            ("task", "archived_at"),
            ("task", "archived_by"),
            ("task", "unarchived_at"),
            ("project", "autonomy"),
        ):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        for column in ("added_after_acceptance_at", "added_by", "added_reason"):
            conn.execute(f"ALTER TABLE plan_step DROP COLUMN {column}")
        conn.execute("ALTER TABLE document DROP COLUMN source_url")
        conn.execute("DROP TABLE saved_filter")
        conn.execute("DROP TABLE rule_seen")
        conn.execute("DROP TABLE rule_proposal")
        conn.execute("DROP TABLE rule_version")
        conn.execute("DROP TABLE rule_set")
        conn.execute("DROP TABLE budget_mark")
        conn.execute("DROP TABLE brake")
        conn.execute("DROP TABLE token_budget")
        conn.execute("DROP TABLE usage_task_day")
        conn.execute("DROP TABLE usage_message")
        conn.execute("DROP TABLE usage_day")
        conn.execute("DROP TABLE usage_file")
        conn.execute("ALTER TABLE session DROP COLUMN transcript_path")
        conn.execute("DROP TABLE session_message")
        conn.execute("ALTER TABLE session DROP COLUMN no_live_at")
        conn.execute("ALTER TABLE session DROP COLUMN no_live_by")
        conn.execute("DROP TABLE annotation_reply")
        conn.execute("DROP TABLE annotation")
        conn.execute("ALTER TABLE attachment DROP COLUMN annotated_name")
        conn.execute("ALTER TABLE attachment DROP COLUMN annotated_size")
        conn.execute("ALTER TABLE attachment DROP COLUMN annotated_sha256")
        conn.execute("ALTER TABLE attachment DROP COLUMN annotated_sent_sha256")
        conn.execute("DROP INDEX task_board_order")
        conn.execute("ALTER TABLE task DROP COLUMN rank")
        conn.execute("DROP TABLE import_item")
        for table in ("task_pr", "task_repo", "project_repo"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DROP INDEX task_session_open_session")
        for table in (
            "question",
            "report_check",
            "run_event",
            "run_worktree",
            "run_turn",
            "run",
            "app_setting",
            "project_run_defaults",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("PRAGMA user_version = 14")
        conn.commit()
    code = run_cli(home, "import", root, "--apply", "--expect", "deadbeefdeadbeef")
    assert code == 1 and "not the one you approved" in capsys.readouterr().out
    with contextlib.closing(sqlite3.connect(db)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 14


def test_a_secret_behind_json_escapes_is_refused_in_the_dry_run(
    home: Path, tmp_path: Path, capsys
) -> None:
    root = tmp_path / "root"
    (root / "memory").mkdir(parents=True)
    escaped = "".join(chr(92) + f"u{ord(c):04x}" for c in FAKE_TOKEN)
    (root / "memory" / "a.md").write_text(
        f'---\nname: "{escaped}"\n---\n\nBody\n', encoding="utf-8"
    )
    (root / "memory" / "b.md").write_text(
        f'---\nname: B\ndescription: "{escaped}"\n---\n\nBody\n', encoding="utf-8"
    )
    assert run_cli(home, "import", root) == 1
    out = capsys.readouterr().out
    assert "refused   note global/a" in out and "refused   note global/b" in out
    assert FAKE_TOKEN not in out


# --- next to a running server -----------------------------------------------------------------


def test_import_next_to_a_running_server(live_server, tmp_path: Path, root: Path, capsys) -> None:
    server = live_server()
    assert run_cli(tmp_path, "import", root, *import_args(), "--apply") == 0
    capsys.readouterr()
    with httpx.Client(base_url=server.url, follow_redirects=True) as client:
        client.get(f"/login?token={server.token}")
        page = client.get("/p/acme-shop")
    assert page.status_code == 200 and "Gift cards at checkout" in page.text


# --- parsing ---------------------------------------------------------------------------------


def test_stage_cell() -> None:
    known = Aliases()
    assert split_stage("", known) == (None, None, True)
    assert split_stage("dev", known) == ("dev", None, True)
    assert split_stage("In Progress - nearly", known) == ("dev", "nearly", True)
    assert split_stage("done: merged", known) == ("done", "merged", True)
    assert split_stage("review waiting for the owner", known) == (
        "review",
        "waiting for the owner",
        True,
    )
    assert split_stage("whatever else", known) == ("plan", "whatever else", False)
    # A non-English word from an aliases file; the words are escapes: the repository is English.
    word = "\u0442\u0435\u0441\u0442"
    column = "\u0437\u0430\u0434\u0430\u0447\u0430"
    other = load_aliases({"stages": {word: "test"}, "columns": {column: "task"}})
    assert split_stage(f"{word.upper()} - later", other) == ("test", "later", True)
    table = f"| project | {column.upper()} |\n|---|---|\n| demo-api | One |\n"
    rows, _ = parse_registry(table, other)
    assert rows[0].title == "One"


def test_registry_cells_survive_rendering() -> None:
    from relaybay.domain.models import Task
    from relaybay.domain.stages import Stage

    task = Task(
        id=1,
        project_id=1,
        key="demo-api-1",
        title=r"A | pipe and a \ backslash",
        stage=Stage.DEV,
        stage_note="- a note",
        waiting_on=None,
        next_step="First\nSecond",
        branch=None,
        doc_path=None,
        description=None,
        progress=None,
        session_id=None,
        priority=0,
        version=1,
        created_at="",
        updated_at="",
        closed_at=None,
    )
    text = render_registry([("demo-api", "demo-api-1", task)])
    rows, warnings = parse_registry(text, Aliases())
    assert warnings == []
    row = rows[0]
    assert (row.title, row.stage, row.stage_note, row.next_step, row.ref) == (
        task.title,
        "dev",
        "- a note",
        "First\nSecond",
        "demo-api-1",
    )


def test_frontmatter() -> None:
    fields, rest = split_frontmatter(
        "---\nname: 'It''s'\ndescription: >\n  two\n  lines\nmetadata:\n  type: user\n"
        'other: "a: b"\n---\nBody\n'
    )
    assert fields == {
        "name": "It's",
        "description": "two lines",
        "metadata.type": "user",
        "other": "a: b",
    }
    assert rest == "Body\n"
    assert split_frontmatter("---\nno end\n")[0] == {}
    assert parse_note("Just text\n", "stem").title == "stem"


def test_places() -> None:
    assert place_of("TASKS.md").kind == "registry"
    assert place_of("projects/a.md").kind == "brief"
    assert place_of("projects/a/docs/spec.md").name == "spec"
    assert place_of("projects/a/tasks/x/TASK.md").name == "x"
    assert place_of("projects/a/memory/n.md").project == "a"
    assert place_of("projects/a/decisions.md").kind == "decisions"
    assert place_of("memory/n.md").name == "n"
    assert place_of("projects/a/tasks/x/other.md") is None
    assert place_of("projects/../x.md") is None
    assert place_of("elsewhere/x.md") is None


async def test_rank_column_orders_the_cards_and_a_board_reorder_is_a_conflict(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "ranked"
    root.mkdir()
    header = "project | ref | task | stage | order"
    write_registry(
        root, header, "acme-shop | a | Deploy | dev | 2", "acme-shop | b | Docs | dev | 1"
    )
    assert run_cli(home, "import", root, "--apply") == 0
    capsys.readouterr()
    service = await open_service(home)
    try:
        tasks = await service.list_tasks(project="acme-shop", by_rank=True)
        assert [(t.title, t.rank) for t in tasks] == [("Docs", 1), ("Deploy", 2)]
        assert not (await fresh_plan(service, root)).writes
        deploy = next(t for t in tasks if t.title == "Deploy")
        docs = next(t for t in tasks if t.title == "Docs")
        await service.move_task(deploy.key, before=docs.key)
        plan = await fresh_plan(service, root)
        assert {i.action for i in plan.items if i.kind == "task"} == {"conflict"}
        assert "rank: 2 -> 1" in item(plan, "task", "acme-shop/b").detail
    finally:
        await service.close()

    # Export brings the file up to the board's order; the import then changes nothing.
    assert run_cli(home, "export", root, "--apply", "--force") == 0
    capsys.readouterr()
    text = (root / "TASKS.md").read_text(encoding="utf-8")
    assert "| document | rank |" in text and "| Deploy | dev |  |  |  | 1 |" in text
    service = await open_service(home)
    try:
        plan = await fresh_plan(service, root)
        assert {i.action for i in plan.items} == {"unchanged"}  # at most bookkeeping
    finally:
        await service.close()


async def test_a_stage_change_keeps_the_rank_the_file_gives(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "moved"
    root.mkdir()
    header = "project | ref | task | stage | rank"
    write_registry(root, header, "acme-shop | a | Deploy | plan | 5")
    assert run_cli(home, "import", root, "--apply") == 0
    service = await open_service(home)
    try:
        await service.create_task("acme-shop", "Docs", stage="dev", actor="owner")
    finally:
        await service.close()
    # Only the stage changes in the file; the rank cell is the one already stored.
    write_registry(root, header, "acme-shop | a | Deploy | dev | 5")
    assert run_cli(home, "import", root, "--apply") == 0
    capsys.readouterr()
    service = await open_service(home)
    try:
        dev = await service.list_tasks(project="acme-shop", stage="dev", by_rank=True)
        assert [(t.title, t.rank) for t in dev] == [("Docs", 0), ("Deploy", 5)]
        assert {i.action for i in (await fresh_plan(service, root)).items} == {"unchanged"}
    finally:
        await service.close()


async def test_a_registry_without_ranks_leaves_the_order_to_the_board(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    write_registry(root, "project | ref | task | stage", "acme-shop | a | Deploy | dev")
    assert run_cli(home, "import", root, "--apply") == 0
    capsys.readouterr()
    service = await open_service(home)
    try:
        other = await service.create_task("acme-shop", "Docs", stage="dev", actor="owner")
        await service.move_task("acme-shop-1", before=other.key)
        assert not (await fresh_plan(service, root)).writes
    finally:
        await service.close()
    # Docs is a row the file lacks: export rewrites the file, ranks included.
    assert run_cli(home, "export", root, "--apply", "--force") == 0
    capsys.readouterr()
    exported = (root / "TASKS.md").read_text(encoding="utf-8")
    assert "| Docs | dev |  |  |  | 2 |" in exported


def test_a_rank_that_is_no_number_is_an_error(home: Path, tmp_path: Path, capsys) -> None:
    root = tmp_path / "bad-rank"
    root.mkdir()
    write_registry(
        root, "project | task | rank", "acme-shop | Deploy | first", "acme-shop | Docs | 3"
    )
    assert run_cli(home, "import", root, "--apply") == 1
    out = capsys.readouterr().out
    assert "rank must be a whole number from -1000000000 to 1000000000" in out
    assert re.search(r"^  add +task acme-shop/docs", out, re.M), out
