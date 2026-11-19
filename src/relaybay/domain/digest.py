"""Digests per level: gathered data -> sections -> budgeted markdown. No I/O: the service gathers
the data and passes the time; only `age`, which the board also calls, can read the clock itself.

A section is index lines plus how to drill down. Over budget, the lowest priority sections lose
whole lines first and say how many are left and where to read them.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from relaybay.domain.digest_limits import PULSE_CHARS_MAX, PULSE_LINES_MAX
from relaybay.domain.doc_index import IndexedSection, size_label, whole_size
from relaybay.domain.environments import Environment
from relaybay.domain.errors import ValidationError
from relaybay.domain.memory import Document, MemoryBlock, MemoryNote
from relaybay.domain.models import Comment, Decision, Project, Task
from relaybay.domain.plan import Plan, TodoItem, one_line, reminder_line
from relaybay.domain.stages import STAGES

if TYPE_CHECKING:
    from relaybay.service import Inbox, InboxCard

RECENT_DECISION_DAYS = 7
PULSE_DAYS = 7
PULSE_LATEST_MAX = 120
FIRST_READ_HOURS = 24
# A SessionStart source after which the agent remembers nothing it read before.
NEW_CONTEXT_SOURCES = ("startup", "clear")

DECISIONS_MAX = 20
TASK_DECISIONS_MAX = 10
NOTES_MAX = 100
RELEVANT_NOTES_MAX = 8
DOC_LINKS_MAX = 5
CHANGES_MAX = 30
COMMENT_IDS_MAX = 20
LINE_MAX = 300
CHANGES_KEY = "changes"
PLAN_KEY = "plan"
OTHERS_KEY = "others"
FIRST_SECTION_LINE_MAX = 1_600
ABOUT_MAX = 600  # the description in a task's head line; the brief adds the rest
PLAN_LINE_MAX = 500

# Left out of "changes since the last read": session status and activity, delivery marks, the
# owner's order of the cards.
NOISE_KINDS = (
    "task.reordered",
    "session.status_changed",
    "session.seen",
    "session.todo_updated",
    "session.message",
    "comment.delivered",
    "comment.read",
    "plan.acceptance_delivered",
    "message.sent",
    "message.delivered",
    "message.read",
    "message.loop_stopped",
    "message.loop_dismissed",
)
# What a session's own hooks write besides the noise; a task digest leaves it out for that session.
OWN_HOOK_KINDS = ("live.action",)
# Stages under work first: a budget cut then takes the backlog before the work in progress.
_STAGE_ORDER = ("dev", "review", "test", "live", "plan")


# --- time ----------------------------------------------------------------------------------


def age(ts: str, now: datetime | None = None) -> str:
    then = datetime.fromisoformat(ts)
    seconds = int(((now or datetime.now(UTC)) - then).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


@dataclass(frozen=True, slots=True)
class Pulse:
    """Where a project stands, derived from its cards: cards moved to another stage and reports
    written in the last PULSE_DAYS days, open cards waiting on the owner, the newest report."""

    moved: int = 0
    reports: int = 0
    waiting: int = 0
    latest_key: str | None = None
    latest: str | None = None  # the newest report's first line
    latest_at: str | None = None


def pulse_line(pulse: Pulse | None, now: datetime) -> str:
    """`last 7 days: 3 cards moved, 12 reports · 1 waits for the owner · latest: acme-shop-4,
    2h ago: verified · M · checkout`."""
    p = pulse or Pulse()
    cards = f"{p.moved} card{'' if p.moved == 1 else 's'} moved"
    reports = f"{p.reports} report{'' if p.reports == 1 else 's'}"
    bits = [f"last {PULSE_DAYS} days: {cards}, {reports}"]
    if p.waiting:
        bits.append(f"{p.waiting} wait{'s' if p.waiting == 1 else ''} for the owner")
    if p.latest_key and p.latest_at:
        bits.append(
            f"latest: {p.latest_key}, {age(p.latest_at, now)}: {_clip(p.latest, PULSE_LATEST_MAX)}"
        )
    return " · ".join(bits)


def pulse_note_head(project: Project, now: datetime) -> str | None:
    """The hand-written note's head (its age and author), None without a note. No stale mark:
    the derived line says where the project stands."""
    if not project.pulse or not project.pulse_at:
        return None
    by = f", by {_clip(project.pulse_by, 40)}" if project.pulse_by else ""
    return f"Note ({age(project.pulse_at, now)}{by}):"


def check_pulse(text: str) -> None:
    """At most PULSE_LINES_MAX lines and PULSE_CHARS_MAX characters."""
    lines, chars = len(text.split("\n")), len(text)
    if lines > PULSE_LINES_MAX or chars > PULSE_CHARS_MAX:
        raise ValidationError(
            f"A pulse takes at most {PULSE_LINES_MAX} lines and {PULSE_CHARS_MAX} characters;"
            f" this text has {lines} line(s) and {chars} character(s). Shorten it."
        )


# --- sections and the budget ---------------------------------------------------------------


@dataclass(slots=True)
class Section:
    """`lines` are one line each, longest `line_max`. `drill` names the tool that shows more;
    `priority` 0 is kept longest, a larger number shrinks earlier; `extra` counts items that
    were not even loaded; `cap` limits the lines shown whatever the budget; `key` names a section
    whose kept lines the caller wants to know."""

    title: str
    lines: list[str]
    drill: str
    priority: int
    extra: int = 0
    cap: int | None = None
    line_max: int = LINE_MAX
    key: str | None = None
    _sizes: list[int] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.lines = [x for x in (one_line(y, self.line_max) for y in self.lines) if x]
        self.title = one_line(self.title, self.line_max) or ""
        self._sizes = [0]
        for line in self.lines:
            self._sizes.append(self._sizes[-1] + len(line) + 1)

    @property
    def most(self) -> int:
        return min(len(self.lines), self.cap) if self.cap is not None else len(self.lines)

    def tail(self, kept: int) -> str:
        hidden = len(self.lines) - kept + self.extra
        return f"… {hidden} more: {self.drill}" if hidden else f"More: {self.drill}"

    def size(self, kept: int) -> int:
        """Characters of the section with `kept` lines, the blank line after it included."""
        return len(self.title) + 1 + self._sizes[kept] + len(self.tail(kept)) + 2

    def text(self, kept: int) -> str:
        return "\n".join([self.title, *self.lines[:kept], self.tail(kept)])


def _shrink(section: Section, kept: int, over: int) -> tuple[int, int]:
    """Whole lines off the end until `over` is gone or no line is left."""
    while over > 0 and kept > 0:
        over += section.size(kept - 1) - section.size(kept)
        kept -= 1
    return kept, over


@dataclass(frozen=True, slots=True)
class Rendered:
    text: str
    changes_complete: bool  # no budget cut touched the changes (the line cap does not count)
    kept: dict[str, int]  # lines kept per section `key`; 0 for a dropped section


def render(sections: list[Section], footer: Callable[[bool], str], budget: int) -> Rendered:
    """The sections in order, then the footer, in at most `budget` characters. The first section
    is the last to shrink and is never dropped; the others are dropped only when even their
    `… N more` line does not fit any more. `footer` gets whether the changes survived whole."""
    room_for_footer = max(len(footer(True)), len(footer(False)))
    kept = [s.most for s in sections]
    used = sum(s.size(k) for s, k in zip(sections, kept, strict=True)) + room_for_footer
    over = used - budget
    rest = sorted(range(1, len(sections)), key=lambda i: (-sections[i].priority, -i))
    for i in rest:
        kept[i], over = _shrink(sections[i], kept[i], over)
    dropped: list[int] = []
    for i in rest:
        if over <= 0:
            break
        over -= sections[i].size(0)
        dropped.append(i)
    kept[0], over = _shrink(sections[0], kept[0], over)
    for i in dropped:
        kept[i] = 0
    complete = all(
        i not in dropped and kept[i] == s.most
        for i, s in enumerate(sections)
        if s.key == CHANGES_KEY
    )
    parts = [s.text(kept[i]) for i, s in enumerate(sections) if i not in dropped]
    end = footer(complete)
    if dropped:
        names = ", ".join(sections[i].title.lstrip("# ") for i in sorted(dropped))
        cut = f"Cut for the budget: {names}.\n{end}"
        if over + len(cut) - room_for_footer <= 0:
            end = cut
    text = "\n\n".join([*parts, end])
    return Rendered(
        text if len(text) <= budget else text[: budget - 1].rstrip() + "…",
        complete,
        {s.key: kept[i] for i, s in enumerate(sections) if s.key},
    )


def footer_text(
    reached: int, reader: str | None, since_event: int | None, moves: bool, complete: bool
) -> str:
    """Names the event id the digest reached and what happened to the reader's cursor."""
    name = one_line(reader, 60)
    if since_event is not None:
        how = "since_event given, no cursor moved"
    elif name is None:
        how = "pass session to see only what is new next time"
    elif not moves:
        how = f"nothing new for {name}"
    elif complete:
        how = f"read position of {name} saved"
    else:
        how = f"changes cut for the budget, read position of {name} not moved"
    return f"Events up to #{reached} ({how})."


# --- shared lines --------------------------------------------------------------------------


def _clip(text: str | None, limit: int) -> str:
    return one_line(text, limit) or ""


def _task_ref(task: Task, title_max: int = 80) -> str:
    return f"{task.key} {_clip(task.title, title_max)}"


def _task_line(task: Task) -> str:
    """`- [dev] acme-shop-2 Checkout page — area: web shop · next: ... · waiting on: owner`."""
    bits = []
    if task.area:
        bits.append(f"area: {task.area}")
    if task.next_step:
        stale = " (stale)" if task.next_step_stale else ""
        bits.append(f"next: {_clip(task.next_step, 120)}{stale}")
    if task.waiting_on:
        bits.append(f"waiting on: {_clip(task.waiting_on, 40)}")
    tail = " — " + " · ".join(bits) if bits else ""
    return f"- [{task.stage.value}] {_task_ref(task)}{tail}"


def _decision_line(d: Decision) -> str:
    where = f" [{d.task_key}]" if d.task_key else ""
    who = f" — {_clip(d.decided_by, 40)}" if d.decided_by else ""
    return f"- #{d.id} {d.created_at[:10]}{where}: {_clip(d.text, 200)}{who}"


def _note_flags(note: MemoryNote, now: datetime) -> list[str]:
    flags = []
    if note.review_at and note.review_at[:10] < now.date().isoformat():
        flags.append("verify")
    if note.last_read_at is None:
        flags.append("never read")
    return flags


def _note_line(note: MemoryNote, now: datetime) -> str:
    where = note.project_slug or note.role or "global"
    flags = "".join(f" [{f}]" for f in _note_flags(note, now))
    return f"- #{note.id} [{where} · {note.kind}] {_clip(note.title, 80)} — {note.hook}{flags}"


def _block_line(block: MemoryBlock) -> str:
    return f"- **{block.label}** (v{block.version}): {_clip(block.body, 240)}"


def _env_line(env: Environment, level: str, url: bool = False) -> str:
    base = f" · {_clip(env.base_url, 120)}" if url and env.base_url else ""
    return f"- {_clip(env.name, 60)} ({env.kind}): agents {level}{base}"


# --- changes since the last read -----------------------------------------------------------


@dataclass(slots=True)
class ChangeGroup:
    """What happened to one task, or to a project or the global scope outside any task."""

    subject: str
    title: str | None
    kinds: dict[str, int]
    last_id: int

    def line(self) -> str:
        title = f" {_clip(self.title, 60)}" if self.title else ""
        counts = ", ".join(f"{n} {kind}" for kind, n in sorted(self.kinds.items()))
        return f"- {self.subject}{title}: {counts} (to #{self.last_id})"


@dataclass(slots=True)
class Changes:
    after: int
    reached: int
    label: str  # "since your last read", "since event #12", "in the last 24 hours"
    groups: list[ChangeGroup]


def group_changes(
    rows: list[tuple[int | None, str | None, str | None, str, int, int]],
) -> list[ChangeGroup]:
    """Rows of (task id, subject, title, kind, count, last id) to groups, newest change first."""
    groups: dict[tuple[int | None, str | None], ChangeGroup] = {}
    for task_id, subject, title, kind, count, last_id in rows:
        group = groups.setdefault(
            (task_id, subject), ChangeGroup(subject or "global", title, {}, last_id)
        )
        group.kinds[kind] = count
        group.last_id = max(group.last_id, last_id)
    return sorted(groups.values(), key=lambda g: -g.last_id)


def _changes_section(changes: Changes, priority: int, project: str | None = None) -> Section:
    scope = f'project="{project}", ' if project else ""
    return Section(
        f"## Changes {changes.label} (events after #{changes.after})",
        [g.line() for g in changes.groups] or ["No changes."],
        f"events_since({scope}since_id={changes.after})",
        priority,
        cap=CHANGES_MAX,
        key=CHANGES_KEY if changes.groups else None,  # "No changes." loses nothing when cut
    )


def _section(
    sections: list[Section],
    title: str,
    lines: list[str],
    drill: str,
    priority: int,
    extra: int = 0,
    line_max: int = LINE_MAX,
    key: str | None = None,
) -> None:
    """Adds the section when it has anything to say."""
    if lines or extra:
        sections.append(Section(title, lines, drill, priority, extra, line_max=line_max, key=key))


# --- portfolio -----------------------------------------------------------------------------


@dataclass(slots=True)
class PortfolioData:
    projects: list[tuple[Project, dict[str, int]]]
    inbox: "Inbox"
    decisions: list[Decision]  # current, of the last RECENT_DECISION_DAYS days
    decisions_total: int
    stale_tasks: list[Task]
    blocks: list[MemoryBlock]
    changes: Changes
    task_stale_days: int
    now: datetime
    rules_line: str = ""
    pulses: dict[int, Pulse] = field(default_factory=dict)  # by project id
    # Open cards not listed under "Waiting for the owner" or as stale, with their pull requests.
    open_tasks: list[tuple[Task, list[str]]] = field(default_factory=list)


def open_task_line(task: Task, prs: list[str]) -> str:
    """`- [test] demo-api-7 Retry webhooks · note: on test since 12.11 · waiting on: QA · next:
    ... · PR #41 · doc relaybay://doc/demo-api/spec#retries`: one row of a task registry."""
    bits = []
    if task.stage_note:
        bits.append(f"note: {_clip(task.stage_note, 80)}")
    if task.waiting_on:
        bits.append(f"waiting on: {_clip(task.waiting_on, 40)}")
    if task.next_step:
        bits.append(f"next: {_clip(task.next_step, 100)}")
    if prs:
        bits.append(f"PR {', '.join(prs[:3])}" + (f" +{len(prs) - 3}" if len(prs) > 3 else ""))
    if task.doc_path:
        bits.append(f"doc {_clip(task.doc_path, 80)}")
    tail = " · " + " · ".join(bits) if bits else ""
    return f"- [{task.stage.value}] {_task_ref(task, 60)}{tail}"


def open_task_order(tasks: list[Task]) -> list[Task]:
    """By project, work in progress first (dev, review, test, live, then plan)."""
    rank = {stage: n for n, stage in enumerate(_STAGE_ORDER)}
    return sorted(tasks, key=lambda t: (t.project_slug, rank.get(t.stage.value, 9)))


def _project_line(
    project: Project, counts: dict[str, int], pulse: Pulse | None, now: datetime
) -> str:
    stages = " · ".join(f"{s} {counts[s]}" for s in STAGES if counts.get(s)) or "no tasks"
    line = f"- **{project.slug}** ({_clip(project.name, 40)}): {stages} — {pulse_line(pulse, now)}"
    note = pulse_note_head(project, now)
    first = next((x.strip() for x in (project.pulse or "").split("\n") if x.strip()), "")
    # Only the word "Note" is lowercased: the author's name stays as written.
    return f"{line} · n{note[1:]} {_clip(first, 120)}" if note and first else line


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _card_line(card: "InboxCard") -> str:
    """`- [test] acme-shop-4 Checkout — waits for the owner · to accept 1/3 · next: ...`."""
    t = card.task
    marks = []
    if card.waiting:
        marks.append("waits for the owner")
    if card.review:
        marks.append(f"to accept {t.review_accepted}/{t.review_total}")
    if card.replies:
        marks.append(_plural(len(card.replies), "agent reply", "agent replies") + " unread")
    if card.undelivered:
        why = f" ({_clip(card.wait_reason, 80)})" if card.wait_reason else ""
        marks.append(_plural(len(card.undelivered), "comment", "comments") + f" not delivered{why}")
    if card.warnings:
        marks.append(f"rule warning: {_clip(card.warnings[0], 120)}")
    if t.next_step:
        marks.append(f"next: {_clip(t.next_step, 120)}")
    return f"- [{t.stage.value}] {_task_ref(t)} — {' · '.join(marks)}"


def inbox_lines(inbox: "Inbox") -> list[str]:
    """The rows the board's Inbox shows: open questions and approval requests, each open card
    once with its marks, then one line per proposal and notice, then the grouped lines."""
    lines = [
        f"- question on {q.task_key or 'no card'} from {_clip(q.asked_by, 40)}:"
        f" {_clip(q.text, 120)}"
        for q in inbox.questions
    ]
    lines += [
        f"- approval request on {a.task_key} from {_clip(a.requested_by, 40)}: {a.action}"
        f" — {_clip(a.summary, 120)}"
        for a in inbox.approvals
    ]
    lines += [_card_line(c) for c in inbox.cards]
    lines += [f"- environment proposal #{p.id} in {p.project_slug}" for p in inbox.proposals]
    lines += [f"- policy proposal #{p.id} on {p.scope}" for p in inbox.policy_proposals]
    lines += [f"- rules proposal #{p.id} on {p.scope}" for p in inbox.rule_proposals]
    lines += [f"- rules changed directly: {v.scope} v{v.number}" for v in inbox.rule_notices]
    if inbox.not_logged:
        done = _plural(len(inbox.not_logged), "done card", "done cards")
        lines.append(f"- {done} without the work-log mark (the Inbox on the board lists them)")
    if inbox.unlinked:
        sessions = _plural(len(inbox.unlinked), "hook session", "hook sessions")
        lines.append(f"- {sessions} not linked (Sessions on the board)")
    if inbox.running_over is not None:
        lines.append(f"- {inbox.running_over} sessions running, cap {inbox.running_cap}")
    return lines


def portfolio_text(data: PortfolioData, footer: Callable[[bool], str], budget: int) -> Rendered:
    now = data.now
    open_tasks = sum(n for _, c in data.projects for s, n in c.items() if s != "done")
    sections = [
        Section(
            "# Portfolio",
            [
                f"{len(data.projects)} project(s) · {open_tasks} open task(s)"
                f" · {data.inbox.badge} waiting for the owner",
                *([data.rules_line] if data.rules_line else []),
            ],
            "project_digest(project) for one project",
            0,
        )
    ]
    _section(
        sections,
        "## Projects",
        [_project_line(p, c, data.pulses.get(p.id), now) for p, c in data.projects],
        "project_digest(project)",
        1,
    )
    _section(
        sections,
        "## Waiting for the owner",
        inbox_lines(data.inbox),
        "task_get(task), or the Inbox on the board",
        2,
    )
    _section(
        sections,
        "## Open tasks",
        [open_task_line(t, prs) for t, prs in data.open_tasks],
        "task_list(project)",
        4,
    )
    _section(
        sections,
        f"## Decisions of the last {RECENT_DECISION_DAYS} days",
        [_decision_line(d) for d in data.decisions],
        "decision_list(project)",
        3,
        extra=data.decisions_total - len(data.decisions),
    )
    stale = [
        f"- [{t.stage.value}] {_task_ref(t)} — last change {age(t.updated_at, now)}"
        for t in data.stale_tasks
    ]
    _section(
        sections,
        f"## Tasks without progress for {data.task_stale_days} days or more",
        stale,
        "task_context(task)",
        4,
    )
    sections.append(_changes_section(data.changes, 5))
    _section(sections, "## Global blocks", [_block_line(b) for b in data.blocks], "block_get()", 6)
    return render(sections, footer, budget)


# --- project -------------------------------------------------------------------------------


@dataclass(slots=True)
class ProjectData:
    project: Project
    counts: dict[str, int]
    tasks: list[Task]  # open
    owner_names: set[str]  # casefolded
    decisions: list[Decision]
    decisions_total: int
    environments: list[tuple[Environment, str]]  # with the agents' level
    blocks: list[MemoryBlock]
    notes: list[MemoryNote]  # current, project and global
    changes: Changes
    now: datetime
    rules_line: str = ""
    pulse: Pulse | None = None


def waits_for_owner(task: Task, owner_names: set[str]) -> bool:
    return bool(task.waiting_on and task.waiting_on.strip().casefold() in owner_names)


def project_text(data: ProjectData, footer: Callable[[bool], str], budget: int) -> Rendered:
    p, now = data.project, data.now
    slug = p.slug
    stages = " · ".join(f"{s} {data.counts[s]}" for s in STAGES if data.counts.get(s))
    head = [
        f"Tasks: {stages}" if stages else "No tasks yet.",
        *([_clip(p.description, 200)] if p.description else []),
        f"Pulse: {pulse_line(data.pulse, now)}",
        *([note, *(p.pulse or "").split("\n")] if (note := pulse_note_head(p, now)) else []),
        *([data.rules_line] if data.rules_line else []),
    ]
    sections = [Section(f"# {slug} — {p.name}", head, f'project_snapshot("{slug}")', 0)]
    rank = {stage: n for n, stage in enumerate(_STAGE_ORDER)}
    tasks = sorted(data.tasks, key=lambda t: rank.get(t.stage.value, len(rank)))
    _section(
        sections, "## Tasks", [_task_line(t) for t in tasks], f'task_list(project="{slug}")', 2
    )
    waiting = [t for t in data.tasks if waits_for_owner(t, data.owner_names)]
    _section(
        sections,
        "## Open questions (tasks waiting on the owner)",
        [f"- {_task_ref(t)} — {_clip(t.next_step, 160) or 'no next step'}" for t in waiting],
        "task_context(task)",
        1,
    )
    _section(
        sections,
        "## Recent decisions",
        [_decision_line(d) for d in data.decisions],
        f'decision_list("{slug}")',
        3,
        extra=data.decisions_total - len(data.decisions),
    )
    _section(
        sections,
        "## Environments",
        [_env_line(env, level) for env, level in data.environments],
        f'environments_get("{slug}")',
        6,
    )
    _section(
        sections,
        "## Blocks",
        [_block_line(b) for b in data.blocks],
        f'block_get(project="{slug}")',
        5,
    )
    # Flagged notes first: they are the ones a budget cut should keep.
    notes = sorted(data.notes, key=lambda n: not _note_flags(n, now))
    _section(
        sections,
        "## Notes",
        [_note_line(n, now) for n in notes],
        "memory_get(id), memory_search(query)",
        7,
    )
    sections.append(_changes_section(data.changes, 4, slug))
    return render(sections, footer, budget)


# --- task ----------------------------------------------------------------------------------

_DOC_LINK_RE = re.compile(r"relaybay://doc/([a-z0-9][a-z0-9-]*)/([a-z0-9][a-z0-9-]*)(?:#([\w-]+))?")


@dataclass(frozen=True, slots=True)
class DocLink:
    project: str
    doc: str
    anchor: str | None

    @property
    def uri(self) -> str:
        section = f"#{self.anchor}" if self.anchor else ""
        return f"relaybay://doc/{self.project}/{self.doc}{section}"

    def pointer(self, document: Document | None, rows: list[IndexedSection]) -> str:
        """Where the link points, without the text: the section's title, lines and size, the
        file of a file document and the call that reads it; or why the link is broken. A link
        without an anchor names the document's size and its outline."""
        if document is None:
            return f"- broken: {self.uri} (no such document)"
        where = f" · {document.place}" if document.place else ""
        problem = " · file missing" if document.file_missing_at else ""
        args = f'"{self.project}", "{self.doc}"'
        if not self.anchor:
            chars, size = whole_size(rows)
            return (
                f'- {self.doc} "{_clip(document.title, 80)}" · {size_label(chars, size)}'
                f"{where}{problem} · doc_outline({args})"
            )
        wanted = self.anchor.casefold()
        row = next((r for r in rows if r.anchor and wanted in (r.anchor, r.alias)), None)
        if row is None:
            return f"- broken: {self.uri} (no such section)"
        # The card's own name for the section: an explicit anchor outlives a reworded heading.
        return (
            f'- {self.doc}#{wanted} "{_clip(row.title, 80)}" · lines'
            f" {row.first_line}-{row.last_line} · {size_label(row.chars, row.bytes)}{where}"
            f'{problem} · doc_get({args}, section="{wanted}")'
        )


def doc_links(*texts: str | None) -> list[DocLink]:
    """The document links in `texts`, each once, in order."""
    found = [m for text in texts for m in _DOC_LINK_RE.finditer(text or "")]
    return list(dict.fromkeys(DocLink(m[1], m[2], m[3]) for m in found))


def doc_link(value: str | None) -> DocLink | None:
    """`value` as a document link when it is exactly one (a task's document field)."""
    m = _DOC_LINK_RE.fullmatch((value or "").strip())
    return DocLink(m[1], m[2], m[3]) if m else None


def doc_link_spans(text: str) -> list[tuple[int, int, DocLink]]:
    """(start, end, link) of each document link in `text`, in order."""
    return [(m.start(), m.end(), DocLink(m[1], m[2], m[3])) for m in _DOC_LINK_RE.finditer(text)]


@dataclass(slots=True)
class TaskData:
    task: Task
    plan: Plan
    todo: list[TodoItem]
    task_decisions: list[Decision]
    project_decisions: list[Decision]
    decisions_total: int
    doc_lines: list[str]
    doc_links_total: int
    environments: list[tuple[Environment, str]]
    rules_line: str
    comments: list[Comment]  # unread owner comments
    reader_session_id: int | None
    notes: list[MemoryNote]
    changes: Changes
    now: datetime
    others: list[tuple[Task, Plan]] = field(default_factory=list)  # other bound tasks


def _task_head(task: Task) -> list[str]:
    state = [f"stage {task.stage.value}"]
    if task.size:
        state.append(f"size {task.size}")
    if task.plan_total:
        state.append(f"plan {task.plan_done}/{task.plan_total} {task.plan_status or ''}".rstrip())
    if task.review_total:
        state.append(f"review {task.review_accepted}/{task.review_total}")
    state.append(f"v{task.version}")
    lines = [" · ".join(state)]
    stale = " (stale)" if task.next_step_stale else ""
    for label, value in (
        ("Area", task.area),
        ("Next step", f"{task.next_step}{stale}" if task.next_step else None),
        ("Waiting on", task.waiting_on),
        ("Note", task.stage_note),
        ("Branch", task.branch),
        ("Doc", task.doc_path),
        ("About", _clip(task.description, ABOUT_MAX) or None),
        ("Done so far", _clip(task.progress, 1_500) or None),
    ):
        if value:
            lines.append(f"{label}: {value}")
    return lines


def _comment_lines(comments: list[Comment], reader_session_id: int | None) -> list[str]:
    """Count and ids; the text only of comments the existing channels already handed to this
    reader. Everything else still arrives through its hooks or comments_list."""
    if not comments:
        return []
    ids = ", ".join(f"#{c.id}" for c in comments[:COMMENT_IDS_MAX])
    more = f" and {len(comments) - COMMENT_IDS_MAX} more" if len(comments) > COMMENT_IDS_MAX else ""
    lines = [f"{len(comments)} unread: {ids}{more}"]
    for c in comments:
        if reader_session_id is not None and c.delivered_to_session_id == reader_session_id:
            kind = "external, " if c.author_kind == "external" else ""
            lines.append(f"- #{c.id} ({kind}delivered to you): {_clip(c.body_md, 240)}")
    return lines


def task_text(data: TaskData, footer: Callable[[bool], str], budget: int) -> Rendered:
    task, now = data.task, data.now
    key, slug = task.key, task.project_slug
    sections = [
        Section(
            f"# {key} — {task.title}",
            _task_head(task),
            f'task_get("{key}")',
            0,
            line_max=FIRST_SECTION_LINE_MAX,
        )
    ]
    remind = reminder_line(key, task.stage.value, data.plan, data.todo)
    steps = [f"{s.position}. [{s.status}] {_clip(s.title, 120)}" for s in data.plan.steps]
    _section(
        sections,
        "## Plan",
        [*([remind] if remind else []), *steps],
        f'plan_get("{key}"); mark steps with plan_step_update (in_progress, done with'
        " 'as planned' or 'deviation: why')",
        1,
        line_max=PLAN_LINE_MAX,
        key=PLAN_KEY,
    )
    others = []
    for other, plan in data.others:
        step = reminder_line(other.key, other.stage.value, plan, [], False)
        others.append(_task_line(other) + (f" · {_clip(step, 160)}" if step else ""))
    _section(sections, "## Your other bound tasks", others, "task_context(task)", 2, key=OTHERS_KEY)
    _section(
        sections,
        "## Unread owner comments",
        _comment_lines(data.comments, data.reader_session_id),
        f'comments_list("{key}"), then comment_ack or comment_add',
        3,
    )
    decisions = [*data.task_decisions, *data.project_decisions]
    _section(
        sections,
        "## Decisions",
        [_decision_line(d) for d in decisions],
        f'decision_list("{slug}")',
        4,
        extra=data.decisions_total - len(decisions),
    )
    _section(
        sections,
        "## Linked document sections",
        data.doc_lines,
        f'doc_get("{slug}", doc, section)',
        6,
        extra=data.doc_links_total - len(data.doc_lines),
    )
    _section(
        sections,
        "## Environments",
        [_env_line(env, level, url=True) for env, level in data.environments],
        f'environments_get("{slug}")',
        8,
    )
    sections.append(Section("## Rules", [data.rules_line], f'rules_get("{slug}")', 7))
    _section(
        sections,
        "## Relevant notes",
        [_note_line(n, now) for n in data.notes],
        "memory_get(id), memory_search(query)",
        9,
    )
    sections.append(_changes_section(data.changes, 5, slug))
    return render(sections, footer, budget)


def plan_shown(data: TaskData, rendered: Rendered) -> bool:
    """Every line the plan reminder would send about these tasks is in the rendered text."""
    task = data.task
    if reminder_line(task.key, task.stage.value, data.plan, data.todo) and not rendered.kept.get(
        PLAN_KEY
    ):
        return False
    others_say = any(reminder_line(t.key, t.stage.value, p, [], False) for t, p in data.others)
    return not others_say or rendered.kept.get(OTHERS_KEY, 0) == len(data.others)
