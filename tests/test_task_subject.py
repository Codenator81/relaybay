"""A task context that fits the task (design 0027, part B): sections linked to a card, the card's
subject, its open backlog items and the project decisions related to it, and the budget order."""

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay.domain import digest
from relaybay.domain import subject as subj
from relaybay.domain.doc_index import IndexedSection
from relaybay.domain.errors import NotFoundError, ValidationError
from relaybay.domain.memory import stem
from relaybay.service import Service
from tests.conftest import LiveServer, run_in_app
from tests.test_mcp import call, connect

SPEC = "\n".join(
    [
        "# Shop spec",  # 1
        "",
        "## Payments",  # 3
        "How money moves.",
        "",
        '<a id="payouts"></a>',  # 6
        "",
        "### 9.6a Payout provider",  # 8
        "Payouts go to the provider in batches.",
        "A failed batch is retried.",
        "",
        "### 9.7 Refunds",  # 12
        "Refunds go back to the card.",
        "",
        "## Registration form",  # 15
        "Fields of the sign-up form.",
        "",
    ]
)


def row(title: str, path: str, first: int = 1, last: int = 9) -> IndexedSection:
    return IndexedSection(0, "a", None, 2, title, path, first, last, last, 10, 10, "h")


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    service = Service(tmp_path / "relaybay.sqlite", coordinators=["lead"])
    await service.start()
    await service.upsert_project("acme-shop", name="Acme Shop")
    try:
        yield service
    finally:
        await service.close()


@pytest.fixture
def spec_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("repo") / "docs" / "spec.md"
    path.parent.mkdir()
    path.write_text(SPEC, encoding="utf-8")
    return path


async def payout_card(svc: Service, spec_file: Path) -> str:
    await svc.update_document(
        "acme-shop", "spec", "-", "from the file", 0, source_path=str(spec_file)
    )
    task = await svc.create_task(
        "acme-shop", "Retry provider calls", area="payouts", sections=["spec#payouts"]
    )
    return task.key


def section_of(text: str, title: str) -> str:
    return text.split(f"## {title}\n", 1)[1].split("\n\n", 1)[0]


# --- the subject --------------------------------------------------------------------------------


def test_the_subject_is_stems_of_area_sections_and_title() -> None:
    path = "Acme shop specification > Payments > 9.6a Payout provider"
    section = row("9.6a Payout provider", path)
    roots = frozenset({"Acme shop specification"})
    linked = [subj.LinkedSection("spec", "payouts", section, roots=roots)]
    found = subj.card_subject("Payouts", "Fix the retry of a batch", linked)
    assert {stem("payout"), stem("provider"), stem("payments"), stem("retry")} <= found
    assert stem("batch") in found
    # The document's own title names the document, not the card's subject.
    assert not {stem("acme"), stem("shop"), stem("specification")} & found
    # Short words, common words (in any form) and bare numbers are left out.
    assert not {"the", "of", "a", "fix", "9", "6a"} & found
    common = "should work without it, others having been doing"
    assert subj.stems(common) == {stem("work")}
    # A decision about another part of the same document shares no stem through its title.
    form = "Acme shop registration form asks for a phone"
    assert len(subj.stems(form) & found) < subj.DECISION_STEMS_MIN
    # Linked to the document's title itself: that title is the section's own title.
    top = subj.LinkedSection(
        "spec", "acme", row("Acme shop", "Acme shop"), roots=frozenset({"Acme shop"})
    )
    assert subj.card_subject(None, "", [top]) == {stem("acme"), stem("shop")}


def test_every_top_level_heading_is_left_out_of_the_path() -> None:
    roots = frozenset({"Acme shop guide", "Acme shop appendix"})
    under_second = row("Payout limits", "Acme shop appendix > Payout limits")
    linked = [subj.LinkedSection("spec", "payout-limits", under_second, roots=roots)]
    assert subj.card_subject(None, "", linked) == {stem("payout"), stem("limits")}


def test_russian_words_stem_and_common_ones_are_left_out() -> None:
    payouts = "\u0432\u044b\u043f\u043b\u0430\u0442\u044b"  # "payouts"
    only = "\u0442\u043e\u043b\u044c\u043a\u043e"  # "only", a common word
    found = subj.stems(f"{payouts} {only}")
    assert found == {stem(payouts)}


def test_a_section_is_named_by_its_anchor_or_number() -> None:
    linked = [subj.LinkedSection("spec", "payouts", row("9.6a Payout provider", "p"))]
    assert subj.section_number("9.6a Payout provider") == "9.6a"
    assert subj.section_number("12. Refunds") == "12"
    assert subj.section_number("Refunds") is None
    assert subj.names_section("See 9.6a.", linked)
    assert subj.names_section("see spec#payouts", linked)
    assert subj.names_section("relaybay://doc/acme-shop/spec#payouts", linked)
    assert not subj.names_section("see 9.6 and 19.6a and 9.6a1 and 9.6a.2", linked)
    assert not subj.names_section("the payouts-v2 flag", linked)
    # A one-word anchor is a plain word too: only `doc#anchor` names the section.
    assert not subj.names_section("Payouts go out weekly", linked)
    assert not subj.names_section("myspec#payouts", linked)
    root = [subj.LinkedSection("spec", "spec", row("Spec", "Spec"))]
    assert not subj.names_section("the spec says so", root)
    # An anchor with a dash stands alone.
    dashed = [subj.LinkedSection("spec", "gift-cards", row("Gift cards", "p"))]
    assert subj.names_section("see gift-cards", dashed)


def test_a_section_is_named_by_any_of_its_names() -> None:
    gift = IndexedSection(0, "gift-cards", "gc", 2, "Gift cards", "p", 1, 9, 9, 10, 10, "h")
    by_alias = [subj.LinkedSection("spec", "gc", gift)]
    by_heading = [subj.LinkedSection("spec", "gift-cards", gift)]
    for linked in (by_alias, by_heading):
        assert subj.names_section("see spec#gift-cards", linked)
        assert subj.names_section("see gift-cards", linked)
        assert subj.names_section("see spec#gc", linked)
        assert not subj.names_section("the gc runs nightly", linked)


def test_a_bare_number_names_a_section_only_after_a_prefix() -> None:
    linked = [subj.LinkedSection("spec", "3-payouts", row("3. Payouts", "p"))]
    for text in ("Releases go out 3 times", "priority 3", "step 3 of 30"):
        assert not subj.names_section(text, linked), text
    for text in ("see \u00a73", "\u00a7 3 says", "Section 3 stays", "sec. 3", "spec#3-payouts"):
        assert subj.names_section(text, linked), text
    assert not subj.names_section("section 31", linked)


def test_a_path_in_a_text_is_the_file_by_name_or_suffix() -> None:
    real = "C:/work/acme-shop/docs/spec.md"
    assert subj.same_file("spec.md", real) and subj.same_file("docs/spec.md", real)
    assert subj.same_file("./docs\\SPEC.md", real)
    assert not subj.same_file("other/spec.md", real) and not subj.same_file("spec.txt", real)
    assert subj.same_file("../docs/spec.md", real) and subj.same_file("../../spec.md", real)
    linked = [subj.LinkedSection("spec", "payouts", row("t", "p", 6, 10), real)]
    assert subj.points_inside("acme-shop:docs/spec.md:9", linked)
    assert not subj.points_inside("docs/spec.md:11", linked)
    assert not subj.points_inside("docs/api.md:9", linked)


def test_a_windows_absolute_path_points_inside() -> None:
    real = "C:\\work\\acme-shop\\docs\\spec.md"
    linked = [subj.LinkedSection("spec", "payouts", row("t", "p", 6, 10), real)]
    for text in (
        "C:\\work\\acme-shop\\docs\\spec.md:7",
        "at c:/Work/acme-shop/docs/SPEC.md:8 it loops",
        "../docs/spec.md:9",
        "shop:docs\\spec.md:6",
    ):
        assert subj.points_inside(text, linked), text
    for text in ("D:\\work\\acme-shop\\docs\\spec.md:7", "C:/other/spec.md:7"):
        assert not subj.points_inside(text, linked), text


# --- task_doc_link ------------------------------------------------------------------------------


async def test_link_remove_broken_and_unknown_anchor(svc: Service) -> None:
    body = "# Spec\n\n## Gift cards\nOne.\n\n## Other\nTwo."
    await svc.update_document("acme-shop", "spec", body, "draft", 0)
    task = await svc.create_task("acme-shop", "Gift card refunds")

    _, line, changed = await svc.link_task_section(task.key, "spec", "#Gift-Cards", session="dev")
    assert changed and line.startswith('- spec#gift-cards "Gift cards" · lines 3-5')
    _, _, again = await svc.link_task_section(task.key, "spec", "gift-cards")
    assert not again
    with pytest.raises(ValidationError, match="No section 'nope'; sections: spec, gift-cards"):
        await svc.link_task_section(task.key, "spec", "nope")
    with pytest.raises(NotFoundError, match="no document 'gone'"):
        await svc.link_task_section(task.key, "gone", "x")
    events = [e for e in await svc.events_since(0) if e.kind == "task.updated"]
    assert events[-1].payload["changes"] == {"sections": {"added": ["spec#gift-cards"]}}
    assert events[-1].actor == "dev"
    assert len(events) == 1

    detail = await svc.task_detail(task.key)
    assert [(s.link.anchor, s.title, s.ref.broken) for s in detail.sections] == [
        ("gift-cards", "Gift cards", None)
    ]
    text = await svc.task_context(task.key)
    assert section_of(text, "Linked document sections").startswith('- spec#gift-cards "Gift cards"')

    # The section goes away: the link stays, marked broken.
    await svc.update_document("acme-shop", "spec", "# Spec\n\n## Other\nTwo.", "drop", 1)
    text = await svc.task_context(task.key)
    assert "- broken: relaybay://doc/acme-shop/spec#gift-cards (no such section)" in text
    detail = await svc.task_detail(task.key)
    assert detail.sections[0].ref.broken == "no such section"

    # A broken link can still be removed; a second removal changes nothing.
    _, _, removed = await svc.link_task_section(task.key, "spec", "gift-cards", remove=True)
    _, _, twice = await svc.link_task_section(task.key, "spec", "gift-cards", remove=True)
    assert removed and not twice
    assert "Linked document sections" not in await svc.task_context(task.key)


async def test_a_section_is_linked_once_by_any_of_its_names(svc: Service) -> None:
    body = '# Spec\n\n<a id="gc"></a>\n## Gift cards\nOne.\n\n## Other\nTwo.'
    await svc.update_document("acme-shop", "spec", body, "draft", 0)
    task = await svc.create_task("acme-shop", "Gift cards")
    _, line, first = await svc.link_task_section(task.key, "spec", "gift-cards")
    _, _, second = await svc.link_task_section(task.key, "spec", "GC")
    assert first and not second and line.startswith('- spec#gc "Gift cards"')
    assert [s.link.anchor for s in (await svc.task_detail(task.key)).sections] == ["gc"]
    _, _, removed = await svc.link_task_section(task.key, "spec", "gift-cards", remove=True)
    assert removed and not (await svc.task_detail(task.key)).sections


async def test_an_alias_added_later_does_not_link_the_section_again(svc: Service) -> None:
    await svc.update_document("acme-shop", "spec", "# Spec\n\n## Gift cards\nOne.", "draft", 0)
    task = await svc.create_task("acme-shop", "Gift cards")
    _, _, first = await svc.link_task_section(task.key, "spec", "gift-cards")
    body = '# Spec\n\n<a id="gc"></a>\n## Gift cards\nOne.'
    await svc.update_document("acme-shop", "spec", body, "alias", 1)
    _, line, second = await svc.link_task_section(task.key, "spec", "gc")
    assert first and not second and line.startswith('- spec#gift-cards "Gift cards"')
    assert [s.link.anchor for s in (await svc.task_detail(task.key)).sections] == ["gift-cards"]


async def test_task_create_links_sections_and_refuses_a_bad_one(svc: Service) -> None:
    await svc.update_document("acme-shop", "spec", "# Spec\n\n## Gift cards\nOne.", "draft", 0)
    task = await svc.create_task(
        "acme-shop",
        "Gift cards",
        sections=["spec#gift-cards", "relaybay://doc/acme-shop/spec#spec", "spec#gift-cards"],
    )
    detail = await svc.task_detail(task.key)
    assert [s.link.anchor for s in detail.sections] == ["gift-cards", "spec"]
    created = next(e for e in await svc.events_since(0) if e.kind == "task.created")
    assert created.payload["sections"] == ["spec#gift-cards", "spec#spec"]
    for bad, error in (
        (["spec"], "is not 'doc#anchor'"),
        (["spec#nope"], "No section 'nope'"),
        (["relaybay://doc/demo-api/spec#x"], "is a document of demo-api"),
    ):
        with pytest.raises(ValidationError, match=error):
            await svc.create_task("acme-shop", "Broken", sections=bad)
    assert len(await svc.list_tasks(project="acme-shop")) == 1


# --- backlog items ------------------------------------------------------------------------------


async def test_backlog_items_of_the_card(svc: Service, spec_file: Path) -> None:
    key = await payout_card(svc, spec_file)
    add = svc.add_backlog_item
    by_area = await add("acme-shop", "Batch size too small", area="Payouts", kind="idea")
    by_line = await add("acme-shop", "Retry loops", location="shop:docs/spec.md:9", priority=2)
    by_text = await add("acme-shop", "Unclear wording", body="In docs/spec.md:10 the word.")
    by_number = await add("acme-shop", "Check 9.6a wording", kind="question")
    by_anchor = await add("acme-shop", "Rename the anchor", body="spec#payouts is long")
    other_line = await add("acme-shop", "Form label", location="docs/spec.md:16", priority=9)
    other = await add("acme-shop", "Registration form label", area="registration", priority=9)
    other_file = await add("acme-shop", "Other file", location="docs/api.md:9")
    # Its area shares a word with the card's title ("Retry provider calls"): not the card's area.
    title_word = await add("acme-shop", "Rotate the keys", area="provider", priority=9)
    linked = await add("acme-shop", "Found while planning", priority=-3)
    await svc.update_backlog_item(linked.id, task=key, session="lead")
    done = await add("acme-shop", "Payout fixed", area="payouts")
    await svc.update_backlog_item(done.id, status="done", session="lead")

    text = await svc.task_context(key)
    lines = section_of(text, "Open backlog items").split("\n")
    ids = [int(x.split()[1][1:]) for x in lines if x.startswith("- #")]
    # Linked first, then by priority, then newest.
    assert ids == [linked.id, by_line.id, by_anchor.id, by_number.id, by_text.id, by_area.id]
    assert f"- #{by_area.id} [payouts] Batch size too small · idea · priority 0" in lines
    assert f"- #{by_line.id} Retry loops · priority 2" in lines
    for item in (other_line, other, other_file, title_word, done):
        assert f"#{item.id} " not in text
    assert lines[-1] == 'More: backlog_list(project="acme-shop")'


async def test_backlog_items_are_capped_and_counted(svc: Service, spec_file: Path) -> None:
    key = await payout_card(svc, spec_file)
    for n in range(11):
        await svc.add_backlog_item("acme-shop", f"Payout item {n}", area="payouts")
    lines = section_of(await svc.task_context(key), "Open backlog items").split("\n")
    assert len([x for x in lines if x.startswith("- #")]) == digest.BACKLOG_ITEMS_MAX
    assert lines[-1] == '… 3 more: backlog_list(project="acme-shop")'


# --- decisions ----------------------------------------------------------------------------------


async def test_only_related_project_decisions_are_listed(svc: Service, spec_file: Path) -> None:
    key = await payout_card(svc, spec_file)
    own = await svc.add_decision("acme-shop", "Retry three times", task=key)
    form = await svc.add_decision("acme-shop", "The registration form asks for a phone number")
    release = await svc.add_decision("acme-shop", "Releases go out on Tuesdays")
    by_area = await svc.add_decision("acme-shop", "Weekly cut-off at noon", area="PAYOUTS")
    by_number = await svc.add_decision("acme-shop", "Section 9.6a stays as written")
    by_words = await svc.add_decision("acme-shop", "The payout provider gets one batch a day")
    one_word = await svc.add_decision("acme-shop", "The provider list is public")
    other = await svc.create_task("acme-shop", "Sign-up form", area="registration")
    of_other = await svc.add_decision("acme-shop", "Phone is optional", task=other.key)

    text = await svc.task_context(key)
    mine = section_of(text, "Decisions").split("\n")
    assert mine[0].startswith(f"- #{own.id} ") and len(mine) == 2
    related = section_of(text, "Related project decisions").split("\n")
    shown = [int(x.split()[1][1:]) for x in related if x.startswith("- #")]
    assert set(shown) == {by_area.id, by_number.id, by_words.id}
    for d in (form, release, one_word, of_other):
        assert f"#{d.id} " not in text
    assert related[-1] == (
        '… 3 project decisions not related to this card: decision_list("acme-shop")'
    )
    # The registration card sees the form decision and not the payout ones.
    text = await svc.task_context(other.key)
    assert f"#{form.id} " in text and f"#{by_words.id} " not in text and f"#{of_other.id} " in text

    _, listed = await svc.list_decisions("acme-shop")
    assert next(d for d in listed if d.id == by_area.id).area == "PAYOUTS"


async def test_mcp_task_doc_link_and_decision_area(
    live_server: Callable[[], LiveServer], tmp_path: Path
) -> None:
    live = live_server()
    async with connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop", name="Acme Shop")
        await call(
            client,
            "doc_update",
            project="acme-shop",
            doc="spec",
            body="# Spec\n\n## Payouts\nBatches.",
            reason="draft",
            base_version=0,
        )
        created = await call(
            client,
            "task_create",
            project="acme-shop",
            title="Payouts",
            area="payouts",
            sections=["spec#payouts"],
        )
        assert "**acme-shop-1**" in created
        linked = await call(client, "task_doc_link", task="acme-shop-1", doc="spec", section="spec")
        assert linked.startswith("Linked to **acme-shop-1**:\n- spec#spec")
        again = await call(client, "task_doc_link", task="acme-shop-1", doc="spec", section="spec")
        assert again.startswith("Already linked to")
        removed = await call(
            client, "task_doc_link", task="acme-shop-1", doc="spec", section="spec", remove=True
        )
        assert removed.startswith("Unlinked from **acme-shop-1**:")
        got = await call(client, "task_get", task="acme-shop-1")
        assert "## Linked document sections\n- spec#payouts" in got
        saved = await call(
            client,
            "decision_add",
            project="acme-shop",
            text="Payouts go out in weekly batches",
            area="payouts",
        )
        assert "area payouts" in saved
        listed = await call(client, "decision_list", project="acme-shop")
        assert "area: payouts" in listed
        context = await call(client, "task_context", task="acme-shop-1")
        assert "weekly batches" in context  # the card's area


# --- the budget ---------------------------------------------------------------------------------


def test_the_new_parts_are_cut_first_in_their_order() -> None:
    def sections() -> list[digest.Section]:
        return [
            digest.Section("# Head", ["state"], "task_get", 0),
            digest.Section("## Decisions", ["- own 1", "- own 2"], "decision_list", 4),
            digest.Section(
                "## Related project decisions",
                ["- related 1", "- related 2"],
                "decision_list",
                11,
                aside=5,
                aside_label="project decisions not related to this card",
            ),
            digest.Section("## Rules", ["- the rules line"], "rules_get", 7),
            digest.Section(
                "## Linked document sections",
                ["- first section", "- second section"],
                "doc_get",
                10,
                floor=1,
                floor_priority=6,
            ),
            digest.Section("## Relevant notes", ["- a note"], "memory_get", 9),
            digest.Section("## Open backlog items", ["- #1 item", "- #2 item"], "backlog_list", 12),
        ]

    full = digest.render(sections(), END, 10_000).text
    assert all(x in full for x in ("#2 item", "related 2", "second section", "a note"))
    assert "… 5 project decisions not related to this card: decision_list" in full
    markers = (
        *("#2 item", "#1 item", "related 2", "related 1", "not related", "second", "a note"),
        *("the rules line", "own 2"),
    )
    gone_at: dict[str, int] = {}
    for budget in range(len(full), 0, -1):
        text = digest.render(sections(), END, budget).text
        assert len(text) <= budget
        for x in markers:
            if x not in text:
                gone_at.setdefault(x, budget)
    order = sorted(gone_at, key=lambda x: -gone_at[x])
    # Backlog items go first, then related decisions, then the sections after the first, and
    # only then the older parts (notes) and the card's own decisions.
    assert order[:6] == ["#2 item", "#1 item", "not related", "related 2", "related 1", "second"]
    assert order.index("a note") < order.index("the rules line") < order.index("own 2")
    # The count of unrelated decisions goes with the related ones, before the rules.
    assert order.index("not related") < order.index("the rules line")
    # Once its line is cut, the count joins the section's "… N more".
    texts = [digest.render(sections(), END, b).text for b in range(len(full), 0, -1)]
    cut = next(t for t in texts if "not related" not in t and "related 2" in t)
    assert "… 5 more: decision_list" in cut


def END(_complete: bool) -> str:
    return "End."


async def test_session_start_context_keeps_its_budget(svc: Service, spec_file: Path) -> None:
    key = await payout_card(svc, spec_file)
    for n in range(30):
        await svc.add_backlog_item("acme-shop", f"Payout item {n} " + "x" * 150, area="payouts")
        await svc.add_decision("acme-shop", f"Payout provider rule {n} " + "y" * 200)
    for budget in (600, 1500, 4000):
        text, _ = await svc._task_context(await svc.get_task(key), None, None, budget)
        assert len(text) <= budget and text.startswith(f"# {key} — Retry provider calls")


def test_the_card_page_lists_the_linked_sections(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    body = "# Spec\n\n## Gift cards\nOne.\n\n## Other\nTwo."
    run_in_app(board, service.update_document, "acme-shop", "spec", body, "draft", 0)
    task = run_in_app(
        board, service.create_task, "acme-shop", "Cards", sections=["spec#gift-cards", "spec#other"]
    )
    run_in_app(
        board, service.update_document, "acme-shop", "spec", "# Spec\n\n## Other\nTwo.", "x", 1
    )
    page = board.get(f"/p/acme-shop/t/{task.key}/view").text
    assert '<a class="doc-link" href="/p/acme-shop/docs/spec#other">spec#other</a>' in page
    assert "spec#gift-cards" in page and "no such section" in page


def test_the_more_line_under_the_unrelated_count_says_what_it_counts() -> None:
    related = digest.Section(
        "## Related project decisions",
        [f"- #{n} related" for n in range(10)],
        'decision_list("acme-shop")',
        11,
        extra=3,
        aside=4,
        aside_label="project decisions not related to this card",
        more_label="related",
    )
    whole = related.text(related.most).split("\n")
    assert whole[-2:] == [
        '… 4 project decisions not related to this card: decision_list("acme-shop")',
        '… 3 more related: decision_list("acme-shop")',
    ]
    # With the count line cut, one line counts everything left out.
    assert related.text(9).split("\n")[-1] == '… 8 more: decision_list("acme-shop")'
