"""`relaybay import --backlog <file> --project <slug>`: one project's rows, and rows that match an
open item already on the board are recorded against it instead of created (design 0027)."""

import asyncio
import io
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from relaybay import cli
from relaybay.domain.backlog import BacklogItem
from relaybay.domain.errors import NotFoundError
from relaybay.domain.memory import word_cover
from relaybay.markdown_import import (
    ImportProblem,
    apply_plan,
    backlog_hash,
    format_plan,
    make_plan,
    parse_matches,
)
from relaybay.service import Service

HEADER = "| Date | Project | Text | Source | Decision |\n|---|---|---|---|---|\n"
COVER_ROW = (
    "On the product list the search box ignores typo tolerance when the setting is on and"
    " filters reset after paging back"
)


async def board(tmp_path: Path) -> tuple[Service, dict[str, BacklogItem]]:
    service = Service(tmp_path / "relaybay.sqlite")
    await service.start()
    await service.upsert_project("acme-shop", name="Acme Shop")
    await service.upsert_project("demo-api")
    titles = {
        "slot": "Remember the last delivery slot",
        "refund": "Refund does not update the cart badge",
        "typo": "Search ignores the typo tolerance setting",
        "csv": "Export orders as CSV file",
        "dark": "Dark mode for the shop",
    }
    made = {
        key: await service.add_backlog_item("acme-shop", title, by_owner=True)
        for key, title in titles.items()
    }
    made["dark"] = await service.decide_backlog(made["dark"].id, "done")
    made["api"] = await service.add_backlog_item("demo-api", "Rate limits per key", by_owner=True)
    return service, made


def registry(tmp_path: Path, made: dict[str, BacklogItem]) -> Path:
    rows = [
        "| 2026-11-01 | acme-shop | Remember the last delivery slot | session | |",
        f"| 2026-11-02 | Acme Shop | Cart badge after a refund, see backlog #{made['refund'].id}"
        " | session | |",
        f"| 2026-11-03 | acme-shop | {COVER_ROW} | review | |",
        "| 2026-11-04 | acme-shop | Export orders as PDF report | owner | |",
        f"| 2026-11-05 | acme-shop | Cart total rounding, fixed in PR #{made['csv'].id} | session"
        " | |",
        "| 2026-11-06 | demo-api | Remember the last delivery slot | session | |",
        "| 2026-11-07 | acme-shop | Remember the last delivery slot for guests | session | |",
        "| 2026-11-08 | acme-shop | Dark mode for the shop | owner | |",
        "| 2026-11-09 | nowhere | Unknown project | owner | |",
    ]
    path = tmp_path / "FINDINGS.md"
    path.write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    modified = datetime(2026, 11, 19, 12).timestamp()
    os.utime(path, (modified, modified))
    return path


def test_word_cover_counts_the_title_words_found_in_the_text() -> None:
    assert word_cover("Search ignores the typo tolerance setting", COVER_ROW) == 1.0
    assert round(word_cover("Export orders as CSV file", "Export orders as PDF report"), 2) == 0.67
    # One word worth counting is no cover: it would match every text that holds it.
    assert word_cover("Logging", "Logging is slow") == 0.0


async def test_rows_of_one_project_match_open_items_and_are_not_created_again(
    tmp_path: Path,
) -> None:
    service, made = await board(tmp_path)
    try:
        path = registry(tmp_path, made)
        before = {b.id: backlog_hash(b) for b in await service.all_backlog()}
        plan = await make_plan(service, None, backlog_file=path, project="acme-shop")
        rows = {int(i.source.rsplit(":", 1)[1]): i for i in plan.items if i.kind == "backlog"}
        assert len(rows) == 9
        slot, refund, typo, csv = (made[k].id for k in ("slot", "refund", "typo", "csv"))
        assert (rows[3].action, rows[3].detail) == ("skipped", f"matches #{slot}: title 1.00")
        assert (rows[4].action, rows[4].detail) == ("skipped", f"matches #{refund}: names it")
        assert (rows[5].action, rows[5].detail) == ("skipped", f"matches #{typo}: cover 1.00")
        assert all(rows[n].record for n in (3, 4, 5))
        # A near miss is created; the warning below names the candidate, once.
        assert (rows[6].action, rows[6].detail) == ("add", "")
        # `PR #N` names a pull request, not the item.
        assert (rows[7].action, rows[7].detail) == ("add", "")
        assert (rows[8].action, rows[8].detail) == ("skipped", "other project")
        assert (rows[11].action, rows[11].detail) == ("skipped", "other project")
        # The same item a better row took: created, warned as near it.
        assert (rows[9].action, rows[9].detail) == ("add", "")
        # A done item is not open: the row is a new item.
        assert (rows[10].action, rows[10].detail) == ("add", "")
        warnings = "\n".join(plan.warnings)
        assert f"FINDINGS.md:6: created as a new item, but it is near #{csv} (0.67)" in warnings
        assert f"FINDINGS.md:9: created as a new item, but it is near #{slot} (1.00)" in warnings
        out = "\n".join(format_plan(plan))
        assert f"matches #{refund}: names it" in out
        assert out.count(f"near #{csv} ") == 1 and out.count(f"near #{slot} ") == 1
        assert plan.summary == ["FINDINGS.md: 9 rows read: add 4, skipped 5"]
        assert not plan.failed

        await apply_plan(service, plan)
        after = await service.all_backlog()
        assert len(after) == len(before) + 4
        # The matched items are not written.
        assert {b.id: backlog_hash(b) for b in after if b.id in before} == before
        assert {b.project_slug for b in after if b.id not in before} == {"acme-shop"}

        again = await make_plan(service, None, backlog_file=path, project="acme-shop")
        actions = {i.source: (i.action, i.detail) for i in again.items if i.kind == "backlog"}
        assert actions["FINDINGS.md:3"] == (
            "unchanged",
            f"matches #{slot} (recorded by an earlier import)",
        )
        assert {a for a, _ in actions.values()} == {"unchanged", "skipped"}
        assert not again.writes
        await apply_plan(service, again)
        assert len(await service.all_backlog()) == len(after)
    finally:
        await service.close()


async def test_the_plan_hash_follows_the_matches(tmp_path: Path) -> None:
    service, made = await board(tmp_path)
    try:
        path = registry(tmp_path, made)
        first = await make_plan(service, None, backlog_file=path, project="acme-shop")
        pdf = await service.add_backlog_item("acme-shop", "Export orders as PDF report")
        second = await make_plan(service, None, backlog_file=path, project="acme-shop")
        [row] = [i for i in second.items if i.source == "FINDINGS.md:6"]
        assert row.detail == f"matches #{pdf.id}: title 1.00"
        assert first.hash != second.hash
        # Without --project nothing is matched: every row of a known project is new.
        plain = await make_plan(service, None, backlog_file=path)
        assert not any(i.detail.startswith("matches") for i in plain.items)
    finally:
        await service.close()


async def test_an_unknown_project_is_refused(tmp_path: Path) -> None:
    service, made = await board(tmp_path)
    try:
        path = registry(tmp_path, made)
        with pytest.raises(NotFoundError, match="--project 'nowhere'"):
            await make_plan(service, None, backlog_file=path, project="nowhere")
        # By name, as a row's project cell is matched.
        plan = await make_plan(service, None, backlog_file=path, project="Acme Shop")
        assert sum(1 for i in plan.items if i.detail == "other project") == 2
    finally:
        await service.close()


def test_the_command_takes_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    async def seed() -> Path:
        service, made = await board(tmp_path)
        try:
            return registry(tmp_path, made)
        finally:
            await service.close()

    path = asyncio.run(seed())
    args = ["import", "--backlog", str(path), "--project", "acme-shop", "--home", str(tmp_path)]
    with ThreadPoolExecutor(1) as pool:
        code = pool.submit(cli.main, args).result()
    out = capsys.readouterr().out
    assert code == 0, out
    assert "skipped   backlog demo-api/2026-11-06/" in out and ": other project" in out
    assert "Nothing written." in out


async def test_a_match_by_hand_wins_over_the_automatic_one(tmp_path: Path) -> None:
    service, made = await board(tmp_path)
    try:
        path = registry(tmp_path, made)
        slot, csv = made["slot"].id, made["csv"].id
        auto = await make_plan(service, None, backlog_file=path, project="acme-shop")
        # Line 6 is only near #csv; line 3 matched #slot by title and is new by hand, so line 9
        # (all of #slot's title words) takes #slot.
        hand: dict[int, int | None] = {6: csv, 3: None}
        plan = await make_plan(service, None, backlog_file=path, project="acme-shop", matches=hand)
        rows = {int(i.source.rsplit(":", 1)[1]): i for i in plan.items if i.kind == "backlog"}
        assert (rows[6].action, rows[6].detail) == ("skipped", f"matches #{csv}: by hand")
        assert (rows[3].action, rows[3].detail) == ("add", "")
        assert (rows[9].action, rows[9].detail) == ("skipped", f"matches #{slot}: cover 1.00")
        assert not any("FINDINGS.md:6:" in w for w in plan.warnings)
        assert plan.hash != auto.hash and not plan.failed

        await apply_plan(service, plan)
        again = await make_plan(service, None, backlog_file=path, project="acme-shop", matches=hand)
        assert not again.writes and not again.failed
        [row] = [i for i in again.items if i.source == "FINDINGS.md:6"]
        assert row.detail == f"matches #{csv} (recorded by an earlier import)"
        # A row matched before and now marked new is created after all.
        later = await make_plan(
            service, None, backlog_file=path, project="acme-shop", matches={6: None}
        )
        [row] = [i for i in later.items if i.source == "FINDINGS.md:6"]
        assert (row.action, row.detail) == ("add", "")
    finally:
        await service.close()


async def test_a_match_by_hand_that_cannot_be_is_an_error_line(tmp_path: Path) -> None:
    service, made = await board(tmp_path)
    try:
        path = registry(tmp_path, made)
        api, dark, csv = made["api"].id, made["dark"].id, made["csv"].id
        hand: dict[int, int | None] = {3: 999, 4: api, 5: dark, 8: made["slot"].id, 1: None}
        plan = await make_plan(service, None, backlog_file=path, project="acme-shop", matches=hand)
        errors = {i.source: i.detail for i in plan.items if i.action == "error"}
        assert errors == {
            "FINDINGS.md:3": "--match 3=999: no backlog item #999",
            "FINDINGS.md:4": f"--match 4={api}: #{api} is an item of demo-api",
            "FINDINGS.md:5": f"--match 5={dark}: #{dark} is done, not open",
            "FINDINGS.md:8": "--match 8: the row is not of acme-shop",
            "FINDINGS.md:1": "--match 1: no row on that line",
        }
        assert plan.failed
        # Two rows cannot be the same item.
        twice = await make_plan(
            service, None, backlog_file=path, project="acme-shop", matches={6: csv, 7: csv}
        )
        details = {i.source: (i.action, i.detail) for i in twice.items}
        assert details["FINDINGS.md:6"] == ("skipped", f"matches #{csv}: by hand")
        assert details["FINDINGS.md:7"] == (
            "error",
            f"--match 7={csv}: #{csv} is already another row's item",
        )
    finally:
        await service.close()


def test_match_arguments() -> None:
    assert parse_matches(["12=4", " 13 = new ", "14=NEW"]) == {12: 4, 13: None, 14: None}
    for bad in ("12", "x=4", "12=four", "12=-1"):
        with pytest.raises(ImportProblem, match="use LINE=N"):
            parse_matches([bad])
    with pytest.raises(ImportProblem, match="line 12 is given twice"):
        parse_matches(["12=4", "12=5"])


def test_match_needs_backlog_and_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["import", str(tmp_path), "--match", "3=1", "--home", str(tmp_path)]
    assert cli.main(args) == 2
    assert "--match goes with --backlog and --project" in capsys.readouterr().err


def code_page_stream() -> tuple[io.TextIOWrapper, io.BytesIO]:
    """What a redirected stdout is on Windows: the ANSI code page."""
    raw = io.BytesIO()
    return io.TextIOWrapper(raw, encoding="cp1252"), raw


def test_output_to_a_code_page_stream_is_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    (out, raw), (err, _) = code_page_stream(), code_page_stream()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    cli.utf8_output()
    said = "Cart badge \u2026 \u0431\u044d\u043a\u043b\u043e\u0433"
    print(said)
    out.flush()
    assert (out.encoding, err.encoding) == ("utf-8", "utf-8")
    assert raw.getvalue().decode("utf-8").splitlines() == [said]


def test_the_command_prints_a_row_outside_the_code_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def seed() -> Path:
        service, made = await board(tmp_path)
        try:
            return registry(tmp_path, made)
        finally:
            await service.close()

    path = asyncio.run(seed())
    word = "\u0431\u044d\u043a\u043b\u043e\u0433"
    with path.open("a", encoding="utf-8") as registry_file:
        registry_file.write(f"| 2026-11-10 | acme-shop | {word} badge | owner | |\n")
    out, raw = code_page_stream()
    monkeypatch.setattr("sys.stdout", out)
    args = ["import", "--backlog", str(path), "--project", "acme-shop", "--home", str(tmp_path)]
    with ThreadPoolExecutor(1) as pool:
        assert pool.submit(cli.main, args).result() == 0
    out.flush()
    printed = raw.getvalue().decode("utf-8")
    assert f"acme-shop/2026-11-10/{word}-badge" in printed


async def test_an_empty_or_dash_decision_keeps_the_board_triage(tmp_path: Path) -> None:
    service, _ = await board(tmp_path)
    try:
        path = tmp_path / "FINDINGS.md"
        path.write_text(
            HEADER + "| 2026-11-10 | acme-shop | Gift cards at checkout | owner | — |\n",
            encoding="utf-8",
        )
        plan = await make_plan(service, None, backlog_file=path, project="acme-shop")
        await apply_plan(service, plan)
        [item] = [b for b in await service.all_backlog() if b.title == "Gift cards at checkout"]
        assert (item.status, item.triage_note) == ("new", None)  # a dash is an empty cell
        await service.decide_backlog(item.id, "later")
        path.write_text(
            HEADER + "| 2026-11-10 | acme-shop | Gift cards at checkout | owner | - |\n",
            encoding="utf-8",
        )
        again = await make_plan(service, None, backlog_file=path, project="acme-shop")
        await apply_plan(service, again)
        [item] = [b for b in await service.all_backlog() if b.id == item.id]
        assert item.status == "later"
    finally:
        await service.close()
