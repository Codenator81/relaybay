"""Short, model-friendly markdown for tool results."""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from relaybay.domain import approvals as arules
from relaybay.domain import questions as qrules
from relaybay.domain import timelog as tl
from relaybay.domain.annotations import Annotation, pin_line, shape_line
from relaybay.domain.approvals import Approval
from relaybay.domain.areas import area_field
from relaybay.domain.backlog import BacklogItem
from relaybay.domain.blockers import blocker_line, blocks_text
from relaybay.domain.change_doc import ChangeMark
from relaybay.domain.code import ProjectRepo, TaskCode
from relaybay.domain.digest import age, board_link, report_items_count_line
from relaybay.domain.digest_limits import PULSE_CHARS_MAX
from relaybay.domain.doc_index import (
    SNIPPET_MAX,
    IndexedSection,
    outline_line,
    outside_mark,
    outside_note,
    size_label,
)
from relaybay.domain.environments import PolicyProposal
from relaybay.domain.memory import (
    STRONG_MATCH,
    Document,
    DocumentVersion,
    MemoryBlock,
    MemoryNote,
    SearchHit,
    Source,
    sections,
)
from relaybay.domain.merge_order import MERGE_ORDER_TITLE
from relaybay.domain.models import (
    Comment,
    Decision,
    Event,
    Project,
    Report,
    Task,
    TrackerEntry,
)
from relaybay.domain.plan import Plan, PlanStep, TodoItem
from relaybay.domain.questions import Question
from relaybay.domain.report_items import ReportItem, item_line
from relaybay.domain.review import Attachment, Review, ReviewStep
from relaybay.domain.rules import (
    RuleProposal,
    Rules,
    requirement_label,
    rules_line,
    rules_markdown,
)
from relaybay.domain.stages import STAGES, Stage
from relaybay.domain.task_fields import (
    APPEND_FIELDS,
    APPEND_NAMES,
    FIELD_NAMES,
    LINK_FIELDS,
    LINK_NAMES,
    SESSION_FIELD_NAMES,
    SESSION_FIELDS,
    TASK_FIELDS,
)
from relaybay.policy import EffectivePolicy
from relaybay.service import (
    DocHit,
    DocRead,
    NoteResult,
    ProjectSummary,
    ReportSaved,
    Snapshot,
    TaskDetail,
)


def who_label(name: str, external_id: str | None) -> str:
    """`name (local_…)` for a desktop session, the plain name otherwise."""
    if external_id and external_id != name:
        return f"{name} ({external_id})"
    return name


def session_label(task: Task) -> str:
    return who_label(task.session_name, task.session_external_id) if task.session_name else ""


SHORT_MAX = 160
STATUS_KEYS_SHOWN = 3  # task keys a session status line names before "+N"


def short(text: str, limit: int = SHORT_MAX) -> str:
    """One line: whitespace collapsed, cut at `limit` characters."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def tracker_label(task: Task, require_log: bool = False) -> str:
    """`Linear ENG-12 <url> · work log ✓`. With `require_log`, a done task without the work log
    is marked `not logged`."""
    issue = " ".join(x for x in (task.tracker_name, task.tracker_ref, task.tracker_url) if x)
    if task.tracker_logged:
        mark = "work log ✓"
    elif require_log and task.stage == Stage.DONE:
        mark = "not logged"
    else:
        mark = "work log not entered" if issue else ""
    return " · ".join(x for x in (issue, mark) if x)


def next_step_label(task: Task) -> str:
    """The next step, marked `(stale)` when it was set before the last stage change."""
    return f"{task.next_step} (stale)" if task.next_step_stale else task.next_step or ""


def tracker_entry_line(entry: TrackerEntry) -> str:
    """`- Linear ENG-12 <url> — logged 2026-01-02 · unlogged 2026-01-03`."""
    issue = " ".join(x for x in (entry.tracker_name, entry.tracker_ref, entry.tracker_url) if x)
    marks = []
    if entry.logged_at:
        marks.append(f"logged {entry.logged_at[:10]}")
    if entry.unlogged_at:
        marks.append(f"unlogged {entry.unlogged_at[:10]}")
    return f"- {issue} — {' · '.join(marks) or 'not logged'}"


def report_line(report: Report) -> str:
    head = " · ".join(x for x in (report.state, report.size) if x)
    prefix = f"{head} · " if head else ""
    return f"{prefix}{report.summary} ({report.author}, {age(report.created_at)})"


# Whether `require_tracker_log` applies to a task (only in a project that keeps a tracker).
LogRule = Callable[[Task], bool] | None


def task_line(
    task: Task, last: Report | None = None, *, texts: bool = False, require_log: LogRule = None
) -> str:
    parts = [f"**{task.key}** {task.title}"]
    extras = []
    area_label, area_text = area_field(task.area_names)
    if area_text:
        extras.append(f"{area_label}: {area_text}")
    if task.waiting_on:
        extras.append(f"waiting on: {task.waiting_on}")
    if task.next_step:
        extras.append(f"next: {next_step_label(task)}")
    if task.branch:
        extras.append(f"branch `{task.branch}`")
    if task.session_name:
        extras.append(f"session: {session_label(task)}")
    if tracker := tracker_label(task, bool(require_log and require_log(task))):
        extras.append(f"tracker: {tracker}")
    if task.review_total:
        extras.append(f"review {task.review_accepted}/{task.review_total}")
    if task.size:
        extras.append(f"size {task.size}")
    if task.plan_total:
        extras.append(plan_label(task))
    extras.append(f"v{task.version}")
    line = f"- {parts[0]} — " + " · ".join(extras)
    if task.stage_note:
        line += f"\n  note: {task.stage_note}"
    if texts and task.description:
        line += f"\n  about: {short(task.description)}"
    if texts and task.progress:
        line += f"\n  done: {short(task.progress)}"
    if last:
        line += f"\n  last report: {report_line(last)}"
    return line


def plan_label(task: Task) -> str:
    """`plan 2/5 (draft) · now: 3 Payment step` for a task line."""
    text = f"plan {task.plan_done}/{task.plan_total}"
    if task.plan_status != "accepted":
        text += " (draft)"
    if task.plan_deviations:
        text += f", {task.plan_deviations} deviation(s)"
    if task.plan_now:
        text += f" · now: {task.plan_now_position} {task.plan_now}"
    return text


def plan_step_line(step: PlanStep) -> str:
    line = f"{step.position}. [{step.status}] {step.title}"
    if step.added_after_acceptance_at:
        line += f" (added after acceptance by {step.added_by}: {step.added_reason})"
    if step.status == "done":
        when = f"done {step.done_at[:16]}Z" if step.done_at else "done"
        bits = [when, step.conformance, step.outcome, step.link]
        line += " — " + " · ".join(b for b in bits if b)
    elif step.outcome:
        line += f" — {step.outcome}"
    if step.details_md:
        line += f"\n   what and why: {short(step.details_md, 400)}"
    return line


def todo_line(item: TodoItem) -> str:
    now = f" (now: {item.active_form})" if item.status == "in_progress" and item.active_form else ""
    return f"- [{item.status}] {item.subject}{now}"


def plan_text(task: Task, plan: Plan, todo: list[TodoItem]) -> str:
    """A task's plan for `plan_get`, with the bound session's todo as a separate list."""
    todo_lines = [
        "",
        "Your todo (Claude Code's own list; a second layer, not the plan):",
        *map(todo_line, todo),
    ]
    if not plan.steps:
        lines = [
            f"**{task.key}** has no plan yet. Write 2-5 steps with plan_set (S tasks: at least"
            " a todo)."
        ]
        if todo:
            lines += [*todo_lines, "It shows on the board as the agent's plan, not accepted."]
        return "\n".join(lines)
    if plan.status == "accepted":
        state = "accepted" + (f" by {plan.accepted_by}" if plan.accepted_by else "")
        state += f" {plan.accepted_at[:16]}Z" if plan.accepted_at else ""
    else:
        state = "draft (not accepted yet: the owner on the board, the coordinator with plan_accept)"
    summary = f"{plan.done}/{len(plan.steps)} done"
    if plan.deviations:
        summary += f", {plan.deviations} deviation(s)"
    lines = [
        f"# Plan of {task.key} — {task.title}",
        f"Status: {state}; {summary}.",
        *map(plan_step_line, plan.steps),
    ]
    if todo:
        lines += todo_lines
    return "\n".join(lines)


def plan_saved(task: Task, plan: Plan, reverted: bool) -> str:
    if not plan.steps:
        return f"Plan of **{task.key}** removed."
    text = (
        f"Plan of **{task.key}** saved: {len(plan.steps)} step(s), {plan.done} done, {plan.status}."
    )
    if reverted:
        text += (
            " The steps changed, so the accepted plan is a draft again: the owner accepts it on"
            " the board, the coordinator with plan_accept."
        )
    elif plan.status == "draft":
        text += " The owner accepts it on the board, the coordinator with plan_accept."
    return text


def plan_step_saved(step: PlanStep) -> str:
    text = f"Step {step.position} '{step.title}' is {step.status}"
    if step.status == "done" and step.conformance:
        text += f" ({step.conformance})"
    return text + "."


def plan_step_added(task: Task, step: PlanStep) -> str:
    text = f"Step {step.position} '{step.title}' added to the plan of **{task.key}** (pending)"
    if step.added_after_acceptance_at:
        return text + "; the plan stays accepted, the step is marked as added after acceptance."
    return text + f"; the plan is {task.plan_status}."


def attachment_line(item: Attachment) -> str:
    caption = f": {short(item.caption_md)}" if item.caption_md else ""
    return f"  - screenshot #{item.id} ({item.mime}, {item.size // 1024} KB){caption}"


def step_line(step: ReviewStep) -> str:
    line = f"{step.position}. [{step.status}] {step.title}"
    if step.decided_by and step.status != "open":
        when = f" {step.decided_at[:16]}Z" if step.decided_at else ""
        line += f" — {step.status} by {step.decided_by}{when}"
    if step.details_md:
        line += f"\n   what, how, why: {short(step.details_md, 400)}"
    return line


def annotation_lines(annotations: list[Annotation]) -> list[str]:
    """A screenshot's pins with their threads, then the rest of its markup."""
    lines = []
    for a in annotations:
        if a.kind != "pin":
            continue
        lines.append(f"    pin {pin_line(a)}")
        lines.extend(f"      {r.author}: {r.body_md}" for r in a.replies)
    shapes = [shape_line(a) for a in annotations if a.kind != "pin"]
    if shapes:
        lines.append("    also marked: " + "; ".join(shapes))
    return lines


def _shot_lines(
    item: Attachment,
    annotations: dict[int, list[Annotation]],
    images: dict[int, bytes],
    notes: dict[int, str],
) -> list[str]:
    lines = [attachment_line(item)]
    if item.id in images:
        lines.append(f"    annotated image of #{item.id}: attached to this answer")
    elif item.id in notes:
        lines.append(f"    annotated image of #{item.id}: {notes[item.id]}")
    lines.extend(annotation_lines(annotations.get(item.id, [])))
    return lines


def review_text(
    task: Task,
    review: Review,
    comments: list[Comment],
    annotations: dict[int, list[Annotation]] | None = None,
    images: dict[int, bytes] | None = None,
    notes: dict[int, str] | None = None,
) -> str:
    """A task's checklist for `review_get`."""
    marked, shown, noted = annotations or {}, images or {}, notes or {}
    if not review.steps and not review.loose:
        return (
            f"**{task.key}** has no review checklist yet. Write one with review_steps_set before"
            " you move the task to review or test."
        )
    lines = [
        f"# Review of {task.key} — {task.title}",
        f"Stage {task.stage.value}; {review.accepted}/{len(review.steps)} accepted"
        + (f", {review.rejected} rejected" if review.rejected else "")
        + ".",
    ]
    for step in review.steps:
        lines.append(step_line(step))
        for item in step.attachments:
            lines.extend(_shot_lines(item, marked, shown, noted))
        notes = [c for c in comments if c.step_id == step.id]
        lines.extend(f"   owner #{c.id}: {short(c.body_md, 400)}" for c in notes)
    if review.loose:
        lines.append("Screenshots of no step:")
        for item in review.loose:
            lines.extend(_shot_lines(item, marked, shown, noted))
    return "\n".join(lines)


def review_saved(task: Task, review: Review) -> str:
    return (
        f"Checklist of **{task.key}** saved: {len(review.steps)} step(s),"
        f" {review.accepted} accepted. The owner decides each step on the board."
    )


def attachment_saved(item: Attachment, new: bool) -> str:
    if not new:
        where = f"step {item.step_position}" if item.step_position else "the task (no step)"
        return (
            f"The same image is already attached to **{item.task_key}** as #{item.id}, on"
            f" {where}; reused, nothing was stored again. An image is kept once per task: to"
            f" show it on another step, refer to #{item.id} in that step's details."
        )
    return (
        f"Attached screenshot #{item.id} to **{item.task_key}** ({item.mime}, {item.size} bytes)."
    )


def project_list(items: list[ProjectSummary]) -> str:
    if not items:
        return "No projects yet. Create one with project_upsert."
    lines = ["| project | name | " + " | ".join(STAGES) + " |", "|---|---|" + "---|" * len(STAGES)]
    for item in items:
        counts = " | ".join(str(item.counts.get(s, 0)) for s in STAGES)
        lines.append(f"| {item.project.slug} | {item.project.name} | {counts} |")
    return "\n".join(lines)


def _repo_label(r: ProjectRepo) -> str:
    kind = f" ({r.kind})" if r.kind else ""
    return f"{r.name}{kind} `{r.path}`"


def project(p: Project, created: bool, repos: list[ProjectRepo] | None = None) -> str:
    verb = "Created" if created else "Updated"
    if repos:
        where = " · repos: " + ", ".join(_repo_label(r) for r in repos)
    else:
        where = f" · repo `{p.repo_path}`" if p.repo_path else ""
    archived = f" · in the archive since {_minute(p.archived_at)}" if p.archived_at else ""
    return f"{verb} project **{p.slug}** ({p.name}){where}{archived}."


def code_lines(code: TaskCode, branch: str | None) -> list[str]:
    """The task's Code section of `task_get`: its repositories with their pull requests, or all of
    the project's when it names none, and the pull requests tied to no repository."""
    if not code.shown:
        return []
    lines = ["\n## Code"]
    for item in code.repos:
        own = item.branch or branch
        on = f" · branch `{own}`" if own else ""
        folder = f" · worktree `{item.worktree}`" if item.worktree else ""
        prs = "".join(f" · PR {pr.label} {pr.url}" for pr in item.prs)
        lines.append(f"- {_repo_label(item.repo)}{on}{folder}{prs} (linked: {item.how})")
    if not code.repos and code.project_repos:
        names = ", ".join(r.name for r in code.project_repos)
        lines.append(
            f"- no repository linked yet (the project has {names}); link the ones the task"
            " touches, when a branch or an edit appears there, with task_update(repos=…)"
        )
    lines.extend(f"- PR {pr.label} {pr.url}" for pr in code.loose_prs)
    return lines


MODE_HELP = {
    "warn": "a stage move that misses a requirement succeeds and the response carries a warning",
    "strict": "a stage move that misses a requirement is refused with the reason",
}


def pulse_saved(p: Project) -> str:
    text = p.pulse or ""
    return (
        f"Pulse of **{p.slug}** saved ({len(text.splitlines())} line(s),"
        f" {len(text)}/{PULSE_CHARS_MAX} characters)."
    )


def rules_text(rules: Rules) -> str:
    """The merged rules as markdown (resource `relaybay://rules[/{project}]`, `rules_get`)."""
    if rules.project is None:
        scope = "global"
    elif rules.override_error:
        scope = f"project {rules.project} (global rules only)"
    else:
        scope = f"project {rules.project} (global + project override)"
    lines = [
        f"# Rules (hash {rules.hash})",
        *([f"- version: {rules.version_text}"] if rules.version else []),
        f"- scope: {scope}",
        f"- mode: {rules.mode} — {MODE_HELP[rules.mode]}",
        *([f"- project override ignored: {rules.override_error}"] if rules.override_error else []),
        "",
        rules_markdown(rules),
    ]
    return "\n".join(lines)


def rules_saved(scope: str, number: int, rules: Rules) -> str:
    return f"Rules of {scope} are now version {number}: {rules.version_text}."


def rule_proposal_saved(p: RuleProposal) -> str:
    return (
        f"Rules proposal #{p.id} for {p.scope} recorded. Nothing changed yet: the owner accepts or"
        " rejects it on the board (Settings → Rules or the Inbox)."
    )


def _next_stage(stage: str) -> str | None:
    at = STAGES.index(stage)
    return STAGES[at + 1] if at + 1 < len(STAGES) else None


def _prompt_task(task: Task | None) -> list[str]:
    if task is None:
        return []
    return [f"Task: **{task.key}** — {task.title} (stage {task.stage.value})", ""]


def start_task_prompt(rules: Rules, task: Task | None) -> str:
    target = (
        f'task_context("{task.key}")'
        if task is not None
        else "project_digest, then pick a task or create one with task_create,"
    )
    lines = [
        "Start this task under the project's working rules.",
        "",
        *_prompt_task(task),
        "Checklist:",
        f"1. Read {target} and the rules below.",
        "2. Pass `session` (your own id or name) on every write so the board shows who works.",
        "3. Write the plan with plan_set (2-5 steps); S tasks need at least a todo.",
        "4. Move the task with task_update(stage=..., next_step=..., waiting_on=...).",
        "5. Fix every line starting with 'Warning (rules)'; re-read the rules when a response says"
        " they were updated.",
        "",
        rules_text(rules),
    ]
    return "\n".join(lines)


def finish_stage_prompt(rules: Rules, task: Task | None) -> str:
    next_stage = _next_stage(task.stage.value) if task is not None else None
    needs = rules.requirements(next_stage) if next_stage else []
    moving = (
        f"3. Moving to {next_stage} needs: {' and '.join(map(requirement_label, needs))}."
        if needs
        else "3. Check what the next stage needs under Stage requirements below."
    )
    report = rules.body["report_format"]
    lines = [
        "Finish the current stage under the project's working rules.",
        "",
        *_prompt_task(task),
        "Checklist:",
        "1. Mark the plan steps you finished with plan_step_update: 'as planned' or"
        " 'deviation: why'.",
        f"2. Add a report with report_add whose first line is `{report['first_line']}`, and keep"
        " progress, next_step and waiting_on current in the same call.",
        moving,
        "4. Before review or test, write what the owner checks by eye with review_steps_set and"
        " attach screenshots with attach_file.",
        "5. Move the task with task_update(stage=...) and fix any 'Warning (rules)' line.",
        "",
        rules_text(rules),
    ]
    return "\n".join(lines)


def with_warnings(text: str, warnings: list[str]) -> str:
    return "\n".join([text, *warnings])


def waiting_on_hint(value: object) -> list[str]:
    """A soft note for a `waiting_on` that reads like a sentence; never a rule warning."""
    if isinstance(value, str) and len(value.split()) > 4:
        return ["Hint: waiting_on is who acts next; put what happens into next_step."]
    return []


def snapshot(snap: Snapshot, require_log: LogRule = None) -> str:
    p = snap.project
    lines = [rules_line(snap.rules), f"# {p.slug} — {p.name}"]
    if p.description:
        lines.append(p.description)
    by_stage: dict[str, list[Task]] = {}
    for task in snap.tasks:
        by_stage.setdefault(task.stage.value, []).append(task)
    if snap.areas:
        names = ", ".join(f"{a.name} ({a.tasks})" for a in snap.areas)
        lines.append(f"Areas (tasks carrying each; task_update(area=...) sets a card's): {names}")
    counts = " · ".join(f"{s} {len(by_stage.get(s, []))}" for s in STAGES if s in by_stage)
    lines.append(f"Tasks: {len(snap.tasks)}" + (f" ({counts})" if counts else ""))
    for stage in STAGES:
        tasks = by_stage.get(stage)
        if not tasks:
            continue
        lines.append(f"\n## {stage}")
        lines.extend(
            task_line(t, snap.last_reports.get(t.id), texts=True, require_log=require_log)
            for t in tasks
        )
    if snap.merge_order and snap.merge_order.shown:
        lines.append(f"\n{MERGE_ORDER_TITLE}")
        lines.extend(snap.merge_order.lines())
    if snap.decisions:
        lines.append("\n## Recent decisions")
        lines.extend(decision_line(d) for d in snap.decisions)
    return "\n".join(lines)


def policy_text(policy: EffectivePolicy, scope: str) -> str:
    """The effective policy of a scope with the layer that set each value (`policy_get`)."""
    chain = " → ".join(layer.name for layer in policy.layers)
    lines = [
        f"# Effective policy: {scope}",
        f"Layers (a later one wins): {chain}. An environment's own level and overrides win"
        " over the policy. Propose changes with policy_propose.",
        "",
    ]
    for path, value, source in policy.flat():
        lines.append(f"- {path} = {json.dumps(value, ensure_ascii=False)}  ({source})")
    for name, error in policy.errors:
        lines.append(f"\nIgnored layer {name}: {error}")
    return "\n".join(lines)


def policy_proposal_saved(p: PolicyProposal) -> str:
    return (
        f"Policy proposal #{p.id} for {p.scope} recorded. Nothing changed yet: the owner accepts"
        " or rejects it on the board (Settings → Policy)."
    )


def decision_line(d: Decision) -> str:
    who = f" — {d.decided_by}" if d.decided_by else ""
    task = f" [{d.task_key}]" if d.task_key else ""
    return f"- #{d.id} {d.created_at[:10]}{task}: {d.text}{who}"


def source_label(kind: str | None, ref: str | None, text: str | None) -> str:
    return Source(kind, ref, text).label()


def decision_entry(d: Decision) -> str:
    """A decision_list line: the decision plus its source and supersede links."""
    extra = []
    if d.area:
        extra.append(f"area: {d.area}")
    source = source_label(d.source_kind, d.source_ref, d.source_text)
    if source:
        extra.append(f"source: {source}")
    if d.superseded_by is not None:
        extra.append(f"superseded by #{d.superseded_by} on {(d.superseded_at or '')[:10]}")
    return decision_line(d) + (f" ({'; '.join(extra)})" if extra else "")


def decisions(project: Project, items: list[Decision], query: str | None) -> str:
    if not items:
        what = f" matching {query!r}" if query else ""
        return f"No decisions{what} in {project.slug}."
    head = f"# Decisions of {project.slug}" + (f" matching {query!r}" if query else "")
    return "\n".join([head, *(decision_entry(d) for d in items)])


def decision_saved(d: Decision) -> str:
    area = f", area {d.area}" if d.area else ""
    text = f"Decision #{d.id} recorded for {d.project_slug} (scope {d.scope}{area})."
    if d.source_kind is None and not d.source_text:
        text += " Tip: pass source (e.g. 'comment:12 owner in chat') so later readers see why."
    return text


# --- documents, notes, blocks, search -------------------------------------------------------


def documents(project: Project, items: list[Document], board: str | None = None) -> str:
    if not items:
        return f"{project.slug} has no documents yet. Create one with doc_update(base_version=0)."
    lines = [f"# Documents of {project.slug}"]
    lines.extend(
        f"- {d.slug} — {d.title} ({d.kind}, version {d.version},"
        f" updated {d.updated_at[:16]}Z) relaybay://doc/{project.slug}/{d.slug}"
        + (f" · board {board_link(board, project.slug, d.slug)}" if board else "")
        + (f" · lives in the repository: {d.source_url}" if d.source_url else "")
        + (f" · lives in the file {d.source_path}" if d.source_path else "")
        + (f" · file missing since {d.file_missing_at[:16]}Z" if d.file_missing_at else "")
        for d in items
    )
    return "\n".join(lines)


def document(
    doc: Document, text: str, section: str | None, link: str = "", marks: Sequence[str] = ()
) -> str:
    """doc_get: a header line with the version to pass back as base_version (and `link`, the
    board link part made by `_board_part`), the `marks` of the cards changing the section, then
    the text."""
    part = f" section #{section}" if section else ""
    current = (
        f"current version {doc.version}"
        if doc.shown_version == doc.version
        else f"version {doc.shown_version} (current is {doc.version})"
    )
    head = (
        f"<!-- {doc.project_slug}/{doc.slug}{part} · {doc.kind} · {current} · by"
        f" {doc.author} · pass base_version={doc.version} to doc_update{link} -->"
    )
    anchors = [s.anchor for s in sections(doc.body)] if not section else []
    tail = [f"\n<!-- sections: {', '.join(anchors)} -->"] if anchors else []
    source = [f"<!-- lives in the repository: {doc.source_url} · doc_update needs force -->"]
    return "\n".join(
        [head, *(source if doc.source_url else []), *_mark_comments(marks), text, *tail]
    )


def _mark_comments(marks: Sequence[str]) -> list[str]:
    return [f"<!-- {m} -->" for m in marks]


def document_saved(doc: Document) -> str:
    return (
        f"{doc.project_slug}/{doc.slug} saved as version {doc.version} ({doc.title})."
        f" Next base_version: {doc.version}."
    )


BROKEN_LINKS_SHOWN = 10


def broken_links(names: Sequence[str]) -> str | None:
    """`Warning: links without a target: spec#a, relaybay://doc/p/d#b (2)`, None when all lead
    somewhere; at most BROKEN_LINKS_SHOWN are named."""
    if not names:
        return None
    shown = ", ".join(names[:BROKEN_LINKS_SHOWN])
    more = len(names) - BROKEN_LINKS_SHOWN
    rest = f" … and {more} more" if more > 0 else ""
    return f"Warning: links without a target: {shown}{rest} ({len(names)})"


def document_deleted(doc: Document, export: str) -> str:
    kept = f" Its file stays on disk: {doc.source_path}." if doc.is_file else ""
    return (
        f"Deleted the document {doc.project_slug}/{doc.slug} ({doc.title}, version"
        f" {doc.version}); the export is `deleted/{export}` in the server's data folder.{kept}"
        " Cards that pointed at it show the link as broken."
    )


def document_history(doc: Document, versions: list[DocumentVersion], diff: list[str]) -> str:
    lines = [f"# History of {doc.project_slug}/{doc.slug} (current version {doc.version})"]
    for v in versions:
        part = f" #{v.section}" if v.section else ""
        lines.append(
            f"- v{v.version} {v.created_at[:16]}Z by {v.author}{part}: {v.reason}"
            f" ({v.size} chars, from v{v.base_version})"
        )
    if diff:
        lines += ["", "```diff", *diff, "```"]
    return "\n".join(lines)


_outline_line = outline_line


def _file_head(doc: Document) -> list[str]:
    """The file line of a file document's answer, and the outside-change note under it."""
    seen = f" · hash {doc.file_hash[:12]}" if doc.file_hash else ""
    masked = f" · {doc.masked_lines} line(s) masked in the index" if doc.masked_lines else ""
    writes = "on: doc_update writes the file" if doc.file_writable else "off: edit the file itself"
    head = [
        f"<!-- file {_clip_name(doc.place or doc.source_path or '', 200)}{seen} · version"
        f" {doc.version}{masked} · writes through relaybay: {writes} -->"
    ]
    note = outside_note(doc)
    return [*head, note] if note else head


def _clip_name(name: str, limit: int = 80) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def _board_part(board: str | None, doc: Document, section: IndexedSection | None) -> str:
    """` · board <link>` for a doc_get header: the document, or the section by its explicit
    anchor when it has one (it outlives a reworded heading; the page carries it as an id)."""
    if not board:
        return ""
    anchor = (section.alias or section.anchor) if section else None
    if anchor and len(anchor) > 100:
        # The header's names are clipped so a capped read always fits; a link cannot be
        # clipped, so an overlong anchor links the document instead.
        anchor = None
    return f" · board {board_link(board, doc.project_slug, doc.slug, anchor)}"


def document_read(read: DocRead, board: str | None = None, marks: Sequence[str] = ()) -> str:
    """doc_get: the header (version or file and hash), the text, and for a capped read the
    outline of what was not shown and the call that reads on. With a cap the whole answer is
    measured as it is written and never longer than the cap: the outline loses headings first,
    then the text loses lines from its end (a single line its end)."""
    doc = read.doc
    args = f'project="{doc.project_slug}", doc="{doc.slug}"'
    anchor = read.section.anchor if read.section else None
    link = _board_part(board, doc, read.section)
    if not doc.is_file and read.cap is None:
        whole = read.first == 1 and read.last == len(doc.body.split("\n"))
        if read.cut is None and (anchor is not None or whole):
            return document(doc, read.text, anchor, link, marks)
    if doc.is_file:
        named = f" section #{_clip_name(anchor)}" if anchor else ""
        head = [
            f"<!-- {doc.project_slug}/{doc.slug}{named} · {doc.kind}{link} -->",
            *_file_head(doc),
        ]
    else:
        head = [document(doc, "", _clip_name(anchor) if anchor else None, link).split("\n")[0]]
    head.extend(_mark_comments(marks))
    if doc.file_problem:
        tail = [_outline_line(read.section)] if read.section else []
        return "\n".join(
            [*head, f"<!-- {doc.file_problem} -->", *tail, f"Outline: doc_outline({args})"]
        )
    cut = read.cut
    body = read.text.split("\n")
    inside = list(cut.inside) if cut else []
    more = cut.more if cut else 0
    shown_last = cut.shown_last if cut else read.last
    next_lines = cut.next_lines if cut else ""
    long_line = cut.long_line if cut else None
    range_chars = cut.total_chars if cut else len(read.text)
    under = f', under="{anchor}"' if anchor and len(anchor) <= 100 else ""

    def render() -> str:
        lines = [
            *head,
            f"<!-- lines {read.first}-{shown_last} shown · the document has {read.total_lines}"
            f" lines, {read.total_chars} characters -->",
            "\n".join(body),
        ]
        if shown_last < read.last or long_line is not None:
            lines.append("")
            lines.append(
                f"<!-- cut: lines {read.first}-{read.last} have {range_chars} characters, over"
                f" the cap; shown {read.first}-{shown_last}. Inside the range: -->"
            )
            if long_line is not None:
                lines.append(
                    f"<!-- line {long_line} alone is longer than the cap: only its start is"
                    " shown -->"
                )
            lines.extend(_outline_line(r) for r in inside)
            if more:
                lines.append(f"… {more} more heading(s): doc_outline({args}{under})")
            if next_lines:
                lines.append(
                    f'Read on: doc_get({args}, lines="{next_lines}"), or a section above by its'
                    " anchor; max_chars sets the cap."
                )
        return "\n".join(lines)

    answer = render()
    while read.cap is not None and len(answer) > read.cap:
        excess = len(answer) - read.cap
        if inside:
            while inside and excess > 0:
                excess -= len(_outline_line(inside.pop())) + 1
                more += 1
        elif len(body) > 1:
            while len(body) > 1 and excess > 0:
                excess -= len(body.pop()) + 1
            end = next_lines.split("-")[1] if next_lines else str(read.last)
            shown_last = read.first + len(body) - 1
            next_lines = f"{shown_last + 1}-{end}"
        elif body[0]:
            body[0] = body[0][: max(len(body[0]) - excess - 200, 0)]
            long_line = read.first
        else:
            break  # only the header is left; its names are clipped so this does not happen
        answer = render()
    return answer


def document_outline(
    doc: Document,
    rows: list[IndexedSection],
    under: IndexedSection | None,
    all_rows: int,
    board: str | None = None,
    marks: Mapping[str, list[ChangeMark]] | None = None,
) -> str:
    """doc_outline; a heading an open card changes ends with ` · changing: <key>, …`."""
    where = f" under #{under.anchor} (lines {under.first_line}-{under.last_line})" if under else ""
    head = [
        f"# Outline of {doc.project_slug}/{doc.slug}{where} · {len(rows)} heading(s) · each:"
        " level, #anchor, title, lines a-b, characters/tokens (~bytes/3.5)"
    ]
    if board:
        # One line, not a link per heading: the outline of a large document stays its size.
        head.append(
            f"Board: {board_link(board, doc.project_slug, doc.slug)} · a section on the board:"
            f" {board_link(board, doc.project_slug, doc.slug, '<anchor>')} (the alias when it"
            " has one)"
        )
    if doc.is_file:
        head.extend(_file_head(doc))
    if doc.file_problem:
        head.append(f"<!-- {doc.file_problem} -->")
    if not rows:
        head.append("No headings." if not all_rows else "No headings at this depth.")
    marks = marks or {}
    head.extend(
        _outline_line(r)
        + (f" · changing: {', '.join(m.key for m in marks[r.anchor])}" if r.anchor in marks else "")
        for r in rows
    )
    head.append(
        f'Read: doc_get(project="{doc.project_slug}", doc="{doc.slug}", section=anchor) or'
        ' lines="a-b".'
    )
    return "\n".join(head)


def document_hits(
    project: Project, hits: list[DocHit], query: str, board: str | None = None
) -> str:
    if not hits:
        return f"Nothing found in the documents of {project.slug} for {query!r}."
    lines = [
        f"# doc_search {project.slug}: {query} · {len(hits)} section(s)",
        "Read a hit: doc_get(project, doc, section=anchor), or lines around the line.",
    ]
    changed: dict[int, Document] = {}
    for h in hits:
        r = h.section
        name = (
            f"{h.doc.slug}#{r.anchor}" if r.anchor else f"{h.doc.slug} (before the first heading)"
        )
        where = f" · {h.doc.place}" if h.doc.place else ""
        link = (
            f" · {board_link(board, project.slug, h.doc.slug, r.alias or r.anchor or None)}"
            if board
            else ""
        )
        lines.append(
            f'- {name} "{r.heading_path or h.doc.title}" · line {h.line} · section lines'
            f" {r.first_line}-{r.last_line} · {size_label(r.chars, r.bytes)}{where}{link}"
        )
        lines.extend(f"    {n}: {_short(text)}" for n, text in h.around)
        if outside_note(h.doc):
            changed.setdefault(h.doc.id, h.doc)
    # One short line per changed document after the hits; doc_get gives the full note.
    lines.extend(outside_mark(d) for d in changed.values())
    return "\n".join(lines)


def _short(text: str, limit: int = SNIPPET_MAX) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def file_history(doc: Document, events: list[Event]) -> str:
    lines = [
        f"# History of {doc.project_slug}/{doc.slug} (the file {doc.place or doc.source_path};"
        f" version {doc.version}; its text is not versioned here, git keeps it where the file is"
        " in a repository)"
    ]
    for e in events:
        p = e.payload
        if p.get("created"):
            what = f"added by {e.actor}: {p.get('reason') or ''}".rstrip(": ")
        elif p.get("file_writes") and not p.get("hash_after"):
            what = f"writes through relaybay turned {p['file_writes']} by {e.actor}"
        else:
            parts = [f"{k} {', '.join(p[k])}" for k in ("added", "removed", "changed") if p.get(k)]
            what = f"changed: {p.get('sections_changed', 0)} section(s)" + (
                f" ({'; '.join(parts)})" if parts else ""
            )
            if p.get("outside"):
                what += " outside relaybay"
            elif p.get("reason"):
                what += f" by {e.actor}: {p['reason']}"
        before = f"{p['hash_before'][:12]} → " if p.get("hash_before") else ""
        after = (p.get("hash_after") or "")[:12]
        lines.append(f"- {e.ts[:16]}Z {what} · hash {before}{after}")
    return "\n".join(lines)


def file_document_saved(doc: Document, rows: int) -> str:
    return (
        f"{doc.project_slug}/{doc.slug} is a file document: {doc.source_path} ({rows} section(s)"
        f" indexed), version {doc.version}. relaybay reads it on demand; writes through"
        f" relaybay are {'on' if doc.file_writable else 'off'}. Read it with"
        f' doc_outline(project="{doc.project_slug}", doc="{doc.slug}") and doc_get.'
    )


def file_document_written(doc: Document) -> str:
    named = [a for a in (doc.file_changed_sections or "").split(",") if a]
    sections = f"; sections changed: {', '.join(named)}" if named else ""
    return (
        f"{doc.project_slug}/{doc.slug} written to its file {doc.place or doc.source_path}:"
        f" version {doc.version}{sections}. Next base_version: {doc.version}."
    )


def file_writes_set(doc: Document) -> str:
    return (
        f"Writes through relaybay into the file of {doc.project_slug}/{doc.slug} are"
        f" {'on' if doc.file_writable else 'off'} (version {doc.version})."
    )


def note_line(n: MemoryNote, similarity: float | None = None) -> str:
    where = n.project_slug or n.role or "global"
    score = f" · similarity {similarity:.2f}" if similarity is not None else ""
    return f"- #{n.id} v{n.version} [{n.scope}: {where} · {n.kind}] {n.title} — {n.hook}{score}"


def note(n: MemoryNote) -> str:
    where = n.project_slug or n.role or "all projects"
    valid_to = f" to {n.valid_to[:10]}" if n.valid_to else ""
    lines = [
        f"# Note #{n.id}: {n.title}",
        f"- scope: {n.scope} ({where}) · kind: {n.kind} · version {n.version} · by {n.author}",
        f"- recorded {n.recorded_at[:16]}Z · valid from {n.valid_from[:10]}{valid_to}",
    ]
    if n.superseded_by is not None:
        lines.append(f"- superseded by #{n.superseded_by} on {(n.superseded_at or '')[:10]}")
    if n.tags:
        lines.append(f"- tags: {n.tags}")
    source = source_label(n.source_kind, n.source_ref, n.source_text)
    if source:
        lines.append(f"- source: {source}")
    if n.review_at:
        lines.append(f"- review at: {n.review_at}")
    lines.append(f"- read {n.read_count} time(s)")
    lines += ["", n.body]
    return "\n".join(lines)


def note_result(result: NoteResult) -> str:
    if result.op == "candidates":
        lines = [
            "Nothing written yet: notes like this exist. Call memory_add again with the same"
            " fields and op = add (a separate note), update or supersede (with target = the id),"
            " or noop (keep what is there); op=update also needs base_version = the"
            " candidate's v<n>:",
            *(note_line(n, s) for n, s in result.candidates),
        ]
        strong = [n for n, s in result.candidates if s >= STRONG_MATCH]
        if strong:
            lines.append(
                f"#{strong[0].id} is a strong match (title similarity at least {STRONG_MATCH}):"
                " op=add next to it needs force_new=true."
            )
        return "\n".join(lines)
    if result.note is None:
        return "Nothing written (noop)."
    n = result.note
    verb = {
        "add": "added",
        "update": "updated",
        "supersede": "added; it supersedes the target",
        "noop": "kept as it is (noop)",
    }[result.op]
    return f"Note #{n.id} {verb} (version {n.version}): {n.title}"


def note_updated(n: MemoryNote) -> str:
    return f"Note #{n.id} updated to version {n.version}: {n.title}"


def search_hits(hits: list[SearchHit], query: str) -> str:
    if not hits:
        return f"Nothing found for {query!r}."
    lines = [f"# Search: {query}"]
    for h in hits:
        where = " · ".join(x for x in (h.project_slug, h.task_key) if x)
        if h.kind == "doc" and h.anchor:
            ref = (
                f"doc {h.name}#{h.anchor} (doc_get project={h.project_slug} doc={h.name}"
                f" section={h.anchor})"
            )
        elif h.kind == "doc":
            ref = f"doc {h.name} (doc_get project={h.project_slug} doc={h.name})"
        elif h.kind == "note":
            ref = f"note #{h.id} (memory_get)"
        else:
            ref = f"{h.kind} #{h.id}"
        lines.append(f"- {ref} [{where or 'global'}] {h.title}")
        lines.append(f"  line {h.line}: {h.snippet}")
    return "\n".join(lines)


def block(b: MemoryBlock) -> str:
    where = b.project_slug or "global"
    flags = " · owner-only (read-only here)" if b.owner_only else ""
    head = (
        f"<!-- block {b.label} ({where}) · version {b.version} · {len(b.body)}/{b.char_limit}"
        f" chars{flags} · pass base_version={b.version} to block_set -->"
    )
    about = [f"<!-- {b.description} -->"] if b.description else []
    return "\n".join([head, *about, b.body])


def blocks(items: list[MemoryBlock], project: str | None) -> str:
    where = project or "global scope"
    if not items:
        return f"No blocks in {where}. block_set(label, body, base_version=0) creates one."
    lines = [f"# Blocks of {where}"]
    for b in items:
        about = f" — {b.description}" if b.description else ""
        flags = ", owner-only" if b.owner_only else ""
        size = f"{len(b.body)}/{b.char_limit}"
        lines.append(f"- {b.label}{about} (version {b.version}, {size}{flags})")
    return "\n".join(lines)


def block_saved(b: MemoryBlock) -> str:
    return (
        f"Block {b.label} ({b.project_slug or 'global'}) saved as version {b.version}"
        f" ({len(b.body)}/{b.char_limit} chars)."
    )


def task_list(tasks: list[Task], require_log: LogRule = None) -> str:
    if not tasks:
        return "No matching tasks."
    return "\n".join(f"{task_line(t, require_log=require_log)} · stage {t.stage}" for t in tasks)


def task_detail(detail: TaskDetail, require_log: LogRule = None, board: str | None = None) -> str:
    t = detail.task
    d = detail.document
    # The document field with its board link, when it leads to a document (a relaybay link,
    # or the file of a file document).
    doc_line = (
        f"{t.doc_path} · board {board.rstrip('/')}{d.ref.href}"
        if t.doc_path and board and d is not None and d.ref.href
        else t.doc_path
    )
    lines = [
        f"# {t.key} — {t.title}",
        f"- project: {t.project_slug}",
        f"- stage: {t.stage}" + (f" ({t.stage_note})" if t.stage_note else ""),
        f"- version: {t.version}",
    ]
    for label, value in (
        area_field(t.area_names),
        ("waiting on", t.waiting_on),
        ("next step", next_step_label(t)),
        ("branch", t.branch),
        ("doc", doc_line),
        ("session", session_label(t)),
        ("open in desktop", t.session_link),
        ("tracker", tracker_label(t, bool(require_log and require_log(t)))),
        ("size", t.size),
        ("plan", plan_label(t) if t.plan_total else None),
        ("archived", f"{_minute(t.archived_at)} by {t.archived_by}" if t.archived_at else None),
        ("cancelled", cancelled_label(t)),
        ("kind", "inspect-only: no accepted plan needed" if t.inspect_only else None),
        ("blocks", blocks_text(detail.blocks)),
    ):
        if value:
            lines.append(f"- {label}: {value}")
    lines.append(f"- updated: {age(t.updated_at)}")
    unread = sum(1 for c in detail.comments if c.to_agent and not c.read_at)
    if unread:
        lines.append(f"- unread owner comments: {unread} (call comments_list)")
    if t.description:
        lines.append(f"\n## Description\n{t.description}")
    if t.progress:
        lines.append(f"\n## Progress (done so far)\n{t.progress}")
    if t.deploy_notes:
        lines.append(f"\n## Deploy notes ({deploy_state(t)})\n{t.deploy_notes}")
    lines.extend(code_lines(detail.code, t.branch))
    if detail.blocked_by:
        lines.append("\n## Blocked by")
        lines.extend(blocker_line(b, board or "") for b in detail.blocked_by)
    if detail.sections:
        lines.append("\n## Linked document sections")
        lines.extend(s.line for s in detail.sections)
    if detail.bindings:
        lines.append("\n## Sessions (newest first)")
        for b in detail.bindings:
            until = f"until {b.unbound_at[:16]}Z" if b.unbound_at else "current"
            who = who_label(b.session_name, b.session_external_id)
            lines.append(f"- {who} — bound {b.bound_at[:16]}Z by {b.bound_by}, {until}")
    if detail.tracker_history:
        lines.append("\n## Tracker history (newest first)")
        lines.extend(tracker_entry_line(e) for e in detail.tracker_history)
    if detail.report_items_total:
        lines.append("\n## Open findings and questions of its reports")
        lines.append(report_items_count_line(detail.report_items_total))
        lines.extend(item_line(i) for i in detail.report_items)
    if detail.reports:
        lines.append("\n## Reports (newest first)")
        for r in detail.reports:
            lines.append(f"\n### {report_line(r)}\n{r.body_md}")
    if detail.decisions:
        lines.append("\n## Decisions")
        lines.extend(decision_line(d) for d in detail.decisions)
    editable = (*FIELD_NAMES, *APPEND_NAMES, *LINK_NAMES, *SESSION_FIELD_NAMES)
    lines.append(f"\nEditable fields: {', '.join(editable)}")
    return "\n".join(lines)


def deploy_state(t: Task) -> str:
    return "done" if t.deploy_done else "not done yet"


def cancelled_label(t: Task) -> str | None:
    """The reason with the time it was set, for a cancelled card; else None."""
    if not t.cancelled:
        return None
    return t.cancelled + (f" ({_minute(t.cancelled_at)})" if t.cancelled_at else "")


def task_schema() -> str:
    """The editable task fields as a markdown table (resource `relaybay://schema/task`)."""
    lines = [
        "# Task fields",
        "Set any of them with task_update(task, fields={...}); omitted fields stay as they are.",
        "",
        "| field | type | description | clearing |",
        "|---|---|---|---|",
    ]
    for f in (*TASK_FIELDS, *APPEND_FIELDS, *LINK_FIELDS, *SESSION_FIELDS):
        limit = f" Up to {f.max_length} characters." if f.max_length else ""
        lines.append(f"| `{f.name}` | {f.type} | {f.description}{limit} | {f.clear} |")
    return "\n".join(lines)


def task_section_saved(task: Task, line: str, remove: bool, changed: bool) -> str:
    """task_doc_link's answer: what changed, and the section's pointer line."""
    if remove:
        what = "Unlinked from" if changed else "Was not linked to"
        return f"{what} **{task.key}**:\n{line}"
    what = "Linked to" if changed else "Already linked to"
    return f"{what} **{task.key}**:\n{line}"


def task_saved(task: Task, verb: str) -> str:
    return f"{verb} **{task.key}** “{task.title}” · stage {task.stage} · version {task.version}."


def _minute(ts: str) -> str:
    return f"{ts[:16]}Z"


def task_archive_saved(task: Task) -> str:
    if task.archived_at:
        closed = " (closed from live; task_unarchive returns it)" if task.archived_from else ""
        return (
            f"**{task.key}** “{task.title}” is in the archive since {_minute(task.archived_at)}"
            f" (by {task.archived_by}){closed}: it is off the done column."
        )
    return f"**{task.key}** “{task.title}” is in the {task.stage.value} column, not in the archive."


def project_deleted(project: Project, export: str) -> str:
    return (
        f"Deleted the project **{project.slug}** with all its tasks, documents, backlog,"
        f" decisions and notes; the export is `deleted/{export}` in the server's data folder."
    )


def comment_marks(c: Comment) -> str:
    """`sent … · delivered … to <session> · read …` for an owner comment; `reply · unread` for
    an agent's."""
    if not c.to_agent:
        return (
            "reply"
            + (f" to #{c.in_reply_to}" if c.in_reply_to else "")
            + ("" if c.read_at else " · unread by the owner")
        )
    marks = [f"sent {_minute(c.created_at)}"]
    if c.delivered_at:
        to = f" to {c.delivered_to_name}" if c.delivered_to_name else ""
        marks.append(f"delivered {_minute(c.delivered_at)}{to}")
    marks.append(f"read {_minute(c.read_at)}" if c.read_at else "unread")
    return " · ".join(marks)


def comments(task: Task, items: list[Comment], unread_only: bool = False) -> str:
    if not items:
        return f"No {'unread ' if unread_only else ''}comments on **{task.key}**."
    lines = [f"# Comments on {task.key} (oldest first)"]
    for c in items:
        who = "owner" if c.from_owner else c.author
        if c.author_kind == "external":
            who = f"external via {c.author}"
        lines.append(f"\n## #{c.id} {who} · {comment_marks(c)}\n{c.body_md}")
    if any(c.to_agent and not c.read_at for c in items):
        lines.append(
            "\nHandle the unread owner comments, then comment_ack(comment_id=…) or reply with"
            " comment_add(task, body, in_reply_to=…)."
        )
    return "\n".join(lines)


def event_subject(e: Event) -> str:
    """The task key of an event, or its project; "coordinator" for one of 0.11's coordinator
    run."""
    p = e.payload
    if p.get("run_kind") == "coordinator":
        return "coordinator"
    return str(p.get("key") or p.get("project") or "")


def profile_change(p: dict[str, Any]) -> str:
    """A profile's Audit line: its name and version, then each field before and after."""
    name = f"{p.get('profile')}"
    if p.get("version") is not None:
        name += f" v{p.get('version')}"
    before, after = p.get("before") or {}, p.get("after") or {}
    if not before or not after:
        return name
    moves = []
    for field_name in after:
        if before.get(field_name) != after.get(field_name):
            moves.append(f"{field_name} {before.get(field_name)} to {after.get(field_name)}")
    return f"{name}: {'; '.join(moves)}" if moves else name


def _event_clock(at: Any) -> str:
    """An event's stored UTC time as the server's local HH:MM."""
    try:
        return tl.clock(datetime.fromisoformat(str(at)), None)
    except ValueError:
        return str(at or "")


def _event_mark(state: Any) -> str:
    if not isinstance(state, Mapping):
        return "?"
    target = state.get("key") or state.get("label") or ""
    return f"{_event_clock(state.get('at'))} {state.get('kind')} {target}".rstrip()


def event_detail(e: Event) -> str:
    """What changed, in a few words: `dev → test`, a report's summary, the changed fields."""
    p = e.payload
    if e.kind == "task.stage_changed":
        return f"{p.get('from')} → {p.get('to')}"
    if e.kind in ("report.added", "task.created"):
        return str(p.get("summary") or p.get("title") or "")
    if e.kind == "task.updated":
        changes = p.get("changes", {})
        # `areas` comes with `area` (its first name): one name keeps a change reading as before.
        return ", ".join(sorted(k for k in changes if k != "areas" or "area" not in changes))
    if e.kind == "task.reordered":
        where = f"position {p['position']}" if p.get("position") else f"rank {p.get('rank')}"
        return f"{p.get('stage')}, {where}"
    if e.kind == "task.session_bound":
        return f"→ {p.get('session')}"
    if e.kind == "task.session_unbound":
        return f"✕ {p.get('session')}"
    if e.kind == "session.alias_added":
        return f"'{p.get('alias')}' = {p.get('session')}"
    if e.kind in ("session.no_live_on", "session.no_live_off"):
        carried = f" (from {p['carried_from']})" if p.get("carried_from") else ""
        reason = f": {p['reason']}" if p.get("reason") else ""
        return f"{p.get('session')}{carried}{reason}"
    if e.kind == "task.tracker_unlogged":
        return f"{p.get('tracker_ref') or ''} ({p.get('reason')})".strip()
    if e.kind == "task.unarchived":
        by = p.get("archived_by")
        return f"archived by {by}" if by else "archived by the done window"
    if e.kind in ("task.purged", "project.deleted"):
        return f"{p.get('key') or p.get('project')} deleted, export {p.get('export')}"
    if e.kind == "document.deleted":
        return f"{p.get('doc')} deleted: {p.get('reason') or ''}, export {p.get('export')}"
    if e.kind == "decision.added":
        supersedes = f" (supersedes #{p['supersedes']})" if p.get("supersedes") else ""
        return f"#{p.get('decision_id')} {p.get('text', '')}{supersedes}"
    if e.kind == "document.updated" and p.get("file"):
        if p.get("created"):
            return f"{p.get('doc')} (a file document) added: {p.get('reason') or ''}".rstrip(": ")
        if p.get("file_writes") and not p.get("hash_after"):
            return f"{p.get('doc')}: writes through relaybay turned {p['file_writes']}"
        how = " outside relaybay" if p.get("outside") else " through relaybay"
        if "version" not in p:
            how = ""
        return f"{p.get('doc')} changed in its file{how}: {p.get('sections_changed', 0)} section(s)"
    if e.kind == "document.updated":
        part = f" #{p['section']}" if p.get("section") else ""
        return f"{p.get('doc')} v{p.get('version')}{part}: {p.get('reason') or ''}".rstrip()
    if e.kind in ("memory.added", "memory.updated"):
        supersedes = f" (supersedes #{p['supersedes']})" if p.get("supersedes") else ""
        return f"#{p.get('note_id')} {p.get('title') or ''}{supersedes}"
    if e.kind == "block.updated":
        return f"{p.get('label')} v{p.get('version')}"
    if e.kind == "timelog.marked":
        target = p.get("key") or p.get("label") or ""
        return f"#{p.get('id')} {p.get('mark_kind')} {_event_clock(p.get('at'))} {target}".rstrip()
    if e.kind in ("timelog.fixed", "timelog.removed"):
        before = _event_mark(p.get("before"))
        change = f"{before} -> {_event_mark(p['after'])}" if p.get("after") else f"{before} removed"
        return f"#{p.get('id')} {change}: {p.get('reason') or ''}".rstrip(": ")
    if e.kind == "worklog.added":
        ref = f" {p['ref']}" if p.get("ref") else ""
        return f"{p.get('key')} {p.get('day')} {tl.hm_minutes(int(p.get('minutes') or 0))}{ref}"
    if e.kind == "project.pulse_set":
        return str(p.get("text") or "")
    if e.kind == "session.status_changed":
        change = f"{p['previous']} → {p.get('status')}" if p.get("previous") else p.get("status")
        keys = [str(k) for k in p.get("keys") or []]
        more = f" +{len(keys) - STATUS_KEYS_SHOWN}" if len(keys) > STATUS_KEYS_SHOWN else ""
        tasks = f" · {', '.join(keys[:STATUS_KEYS_SHOWN])}{more}" if keys else ""
        return f"{p.get('session')}: {change}{tasks}"
    if e.kind.startswith("annotation."):
        return f"pin #{p['pin_number']}" if p.get("pin_number") else str(p.get("annotation_kind"))
    if e.kind == "intake.wake":
        message = f"message #{p.get('message_id')} from {p.get('source')}"
        return f"{message} woke the agent: {p.get('reason')}"
    if e.kind == "intake.received":
        reason = f" ({p['reason']})" if p.get("reason") else ""
        return f"message #{p.get('message_id')} from {p.get('source')}: {p.get('status')}{reason}"
    if e.kind.startswith("intake.source_"):
        return str(p.get("source") or "")
    if e.kind.startswith("intake."):
        return f"message #{p.get('message_id')} from {p.get('source')}"
    if e.kind.startswith("backlog."):
        status = f" → {p['status']}" if p.get("status") else ""
        reason = f": {p['reason']}" if p.get("reason") else ""
        link = f" ({p['link']})" if p.get("link") else ""
        return f"#{p.get('backlog_id')} {p.get('title') or ''}{status}{reason}{link}"
    if e.kind == "comment.added":
        return f"#{p.get('comment_id')} {p.get('excerpt') or ''}".rstrip()
    if e.kind == "comment.delivered":
        return f"#{p.get('comment_id')} → {p.get('session')}"
    if e.kind == "comment.read":
        return f"#{p.get('comment_id')} read"
    if e.kind == "plan.set":
        return f"{p.get('steps')} step(s), {p.get('status') or 'removed'}"
    if e.kind == "plan.step_added":
        return f"step {p.get('position')} {p.get('title') or ''} added: {p.get('reason') or ''}"
    if e.kind == "plan.step_updated":
        return f"step {p.get('position')} {p.get('previous')} → {p.get('status')}"
    if e.kind == "plan.accepted":
        return f"{p.get('steps')} step(s) accepted"
    if e.kind == "plan.acceptance_delivered":
        return f"→ {p.get('session')}"
    if e.kind == "session.todo_updated":
        return f"todo {p.get('completed')}/{p.get('items')} done"
    if e.kind == "message.sent":
        return f"#{p.get('message_id')} {p.get('sender')} → {p.get('recipient')}"
    if e.kind == "message.delivered":
        return f"#{p.get('message_id')} → {p.get('recipient')}"
    if e.kind == "message.read":
        return f"#{p.get('message_id')} read"
    if e.kind == "message.loop_stopped":
        return (
            f"#{p.get('message_id')} a reply over the depth limit,"
            f" {p.get('sender')} and {p.get('recipient')}"
        )
    if e.kind == "message.loop_dismissed":
        return f"#{p.get('message_id')} seen"
    if e.kind == "session.message":
        return f"{p.get('session')} → {p.get('to')}"
    if e.kind == "session.launched":
        return f"{p.get('session')} started {p.get('child')}"
    if e.kind == "session.surface_set":
        return f"{p.get('session')} runs in: {p.get('surface') or 'not known'}"
    if e.kind == "session.terminal_opened":
        return f"{p.get('session')} resumed in a terminal in {p.get('cwd')}"
    if e.kind == "live.action":
        return f"{p.get('environment')} · {p.get('decision')} · {p.get('command') or ''}".rstrip()
    if e.kind in ("environment.created", "environment.deleted"):
        return f"{p.get('environment')} ({p.get('env_kind')})"
    if e.kind == "environment.updated":
        return f"{p.get('environment')}: {', '.join(p.get('changes', []))}"
    if e.kind.startswith("environment.access_"):
        return f"{p.get('environment')}: {p.get('access_kind')}"
    if e.kind.startswith("environment.proposal") or e.kind == "environment.proposed":
        return f"#{p.get('proposal_id')} {p.get('change') or ''}".rstrip()
    if e.kind == "policy.updated":
        return str(p.get("scope", ""))
    if e.kind.startswith("policy.propos"):
        return f"#{p.get('proposal_id')} {p.get('scope')}: {p.get('change') or ''}".rstrip()
    if e.kind == "brake.set":
        return f"{p.get('target')}: {p.get('reason')}"
    if e.kind == "brake.released":
        return str(p.get("target", ""))
    if e.kind in ("budget.warned", "budget.exceeded"):
        return f"{p.get('target')}: {p.get('used')} of {p.get('max_tokens')} tokens"
    if e.kind in ("budget.set", "budget.removed"):
        return str(p.get("target", ""))
    if e.kind == "run.defaults_set":
        profile = f" · profile {p['profile']}" if p.get("profile") else ""
        return f"{p.get('model')} · {p.get('effort')} · {p.get('permission_mode')}{profile}"
    if e.kind.startswith("profile."):
        return profile_change(p)
    if e.kind == "settings.run_login_any":
        return "other sign-ins allowed" if p.get("on") else "subscription sign-ins only"
    if e.kind in ("coordinator.started", "coordinator.new_session"):
        return f"run {p.get('run_id')} in {p.get('folder')} · profile {p.get('profile')}"
    if e.kind in ("coordinator.stopped", "coordinator.ended"):
        return f"run {p.get('run_id')}"
    if e.kind == "coordinator.woken":
        return (
            f"run {p.get('run_id')}: woken by {p.get('events')} events"
            f" ({p.get('first_event_id')}..{p.get('last_event_id')})"
        )
    if e.kind.startswith("schedule."):
        return schedule_detail(p)
    if e.kind == "coordinator.compacting":
        return f"run {p.get('run_id')}: /compact after {p.get('context')} context tokens"
    if e.kind == "settings.coordinator":
        folder = p.get("folder") or "the default folder"
        head = f"{folder} · profile {p.get('profile')} · {p.get('model')} · {p.get('effort')}"
        if p.get("wake_gap_seconds") is None:
            return head
        return (
            f"{head} · wakes {p.get('wake_gap_seconds')} s apart · /compact above"
            f" {p.get('compact_at_tokens')} · {p.get('daily_tokens')} tokens a day,"
            f" {p.get('budget_action')}"
        )
    if e.kind == "settings.terminal_enabled":
        return "Terminal tab on" if p.get("on") else "Terminal tab off"
    if e.kind == "run.terminal_closed":
        return f"run {p.get('run_id')}: {p.get('reason')}"
    if e.kind == "run.turn_ended":
        turn = f"run {p.get('run_id')} turn {p.get('seq')}"
        return f"{turn}: {p.get('outcome')}, {p.get('tokens')} tokens"
    if e.kind == "run.settings_changed":
        changed = p.get("changed") or {}
        moves = ", ".join(
            f"{name.replace('permission_mode', 'mode')} {v.get('from')} to {v.get('to')}"
            for name, v in changed.items()
            if isinstance(v, dict)
        )
        return f"run {p.get('run_id')}: {moves}"
    if e.kind in ("run.turn_started", "run.turn_queued"):
        reason = f": {p['reason']}" if p.get("reason") else ""
        return f"run {p.get('run_id')} turn {p.get('seq')}{reason}"
    if e.kind == "run.start_anyway":
        return f"run {p.get('run_id')} turn {p.get('seq')} started past: {p.get('reason')}"
    if e.kind in ("run.worktree_removed", "run.worktree_kept"):
        what = f"run {p.get('run_id')}: {p.get('repository')} {p.get('path')}"
        if e.kind == "run.worktree_kept":
            return f"{what} kept: {p.get('reason')}"
        how = " with --force" if p.get("forced") else f" ({p['note']})" if p.get("note") else ""
        return f"{what} removed{how}"
    if e.kind == "run.traces_deleted":
        files = len(p.get("transcripts") or [])
        failed = f", {len(p['failed'])} not deleted" if p.get("failed") else ""
        return (
            f"run {p.get('run_id')} ({p.get('reason')}): {p.get('lines')} lines,"
            f" {files} transcript file{'s' if files != 1 else ''}{failed}"
        )
    if e.kind == "run.events_expired":
        kept = "retention off" if p.get("retention") == "off" else f"{p.get('retention')} days"
        return f"{p.get('lines')} lines of runs ({kept})"
    if e.kind == "settings.run_limits":
        return (
            f"{p.get('max_running_turns')} turns at once,"
            f" {p.get('min_free_memory_mb')} MB free before a turn"
        )
    if e.kind.startswith("run."):
        reason = f": {p['reason']}" if p.get("reason") else ""
        return f"run {p.get('run_id')}{reason}"
    if e.kind.startswith("question."):
        what = f"#{p.get('question_id')}: {qrules.quoted(str(p.get('text') or ''))[:120]}"
        if e.kind == "question.answered" and p.get("answer") is not None:
            return f"{what} -> {qrules.quoted(str(p['answer']))[:120]}"
        if e.kind == "question.defaulted":
            return f"{what} -> default {qrules.quoted(str(p.get('default') or ''))[:120]}"
        if e.kind == "question.overdue":
            return f"{what} open {p.get('open_minutes')} min"
        return what
    if e.kind.startswith("approval."):
        what = f"#{p.get('approval_id')}: {p.get('what') or p.get('action')}"
        if e.kind == "approval.decided":
            line = f": {arules.quoted(str(p['text']))}" if p.get("text") else ""
            return f"{what} -> {p.get('decision')}{line}"
        if e.kind == "approval.expired":
            return f"{what} expired: {p.get('reason')}"
        if e.kind == "approval.released":
            return f"{what} released: {p.get('reason')}"
        if e.kind == "approval.used":
            given = " (allow given)" if p.get("decision") == "allow" else ""
            return f"{what} used by {arules.quoted(str(p.get('used_by') or ''))}{given}"
        return f"{what} ({p.get('via') or 'agent'})"
    if e.kind == "concurrency.over":
        return f"{p.get('running')} sessions running, cap {p.get('max_sessions')}"
    if e.kind == "rules.notice_read":
        return f"{p.get('scope')} v{p.get('version')}"
    if e.kind == "rules.updated":
        return f"{p.get('scope')} v{p.get('version')}: {p.get('reason') or ''}".rstrip()
    if e.kind.startswith("rules.propos"):
        return f"#{p.get('proposal_id')} {p.get('scope')}: {p.get('change') or ''}".rstrip()
    if e.kind.startswith("filter."):
        return str(p.get("name", ""))
    if e.kind == "session.seen":
        reason = p.get("unlinked_reason")
        return f"{p.get('session')}" + (f" (unlinked: {reason})" if reason else "")
    return ""


def schedule_detail(p: dict[str, Any]) -> str:
    """`schedule 3 (check) <its text>: ran, <reason>, run 12`."""
    head = f"schedule {p.get('schedule_id')} ({p.get('schedule_kind')}) {p.get('title') or ''}"
    extra = [str(p[k]) for k in ("outcome", "reason", "when", "done") if p.get(k)]
    if "enabled" in p:
        extra.append("enabled" if p["enabled"] else "disabled")
    if p.get("run_id"):
        extra.append(f"run {p['run_id']}")
    return head.rstrip() + (": " + ", ".join(extra) if extra else "")


def events(items: list[Event], since_id: int) -> str:
    if not items:
        return f"No events after #{since_id}."
    lines = []
    for e in items:
        key, detail = event_subject(e), event_detail(e)
        lines.append(f"- #{e.id} {e.ts[:19]}Z {e.kind} {key} {detail} ({e.actor})".rstrip())
    lines.append(f"\nNext call: events_since(since_id={items[-1].id})")
    return "\n".join(lines)


def audit(items: list[Event], days: int) -> str:
    """The journal of actions on live stands, one line per action, newest first."""
    note = "Only tool calls that touch a live stand are journaled."
    if not items:
        return f"No actions on live in the last {days} day(s). {note}"
    lines = []
    for e in items:
        p = e.payload
        parts = [
            e.actor,
            f"{p.get('project')}/{p.get('environment')}",
            str(p.get("tool")),
            f"`{p.get('command') or ''}`",
            str(p.get("decision")),
            f"level {p.get('level')}",
            *([f"rule {p['rule']}"] if p.get("rule") else []),
            *([str(p["key"])] if p.get("key") else []),
        ]
        lines.append(f"- #{e.id} {e.ts[:19]}Z " + " · ".join(parts))
    lines.append(f"\n{note}")
    return "\n".join(lines)


# --- backlog -------------------------------------------------------------------------------


def _backlog_queue(b: BacklogItem) -> list[str]:
    parts = [f"priority {b.priority}"] if b.priority else []
    return parts + ([f"for {b.assignee}"] if b.assignee else [])


def backlog_line(b: BacklogItem) -> str:
    area = f" [{b.area}]" if b.area else ""
    kind = f" ({b.kind})" if b.kind else ""
    origin = b.origin_text or b.origin
    if b.origin_task_key:
        origin += f" from {b.origin_task_key}"
    if b.origin_session_name:
        origin += f" by {b.origin_session_name}"
    status = b.status
    if b.status in ("declined", "done") and b.decline_reason:
        status += f": {b.decline_reason}"
    if b.link:
        status += f" ({b.link})"
    if b.task_key:
        status += f" as {b.task_key}"
    queue = "".join(f"{part} · " for part in _backlog_queue(b))
    extra = "".join(
        f" · {part}"
        for part in (
            f"at {b.location}" if b.location else "",
            f"note: {b.triage_note}" if b.triage_note else "",
        )
        if part
    )
    return (
        f"#{b.id} {b.project_slug}{area}{kind} {b.title} · {queue}{origin} · {status}"
        f" · {(b.found_on or b.created_at)[:10]}{extra}"
    )


def backlog_items(items: list[BacklogItem]) -> str:
    if not items:
        return "No backlog items."
    return "\n".join(
        ["# Backlog (new first, then by priority)", *(f"- {backlog_line(b)}" for b in items)]
    )


def report_saved(saved: ReportSaved, stage: str | None) -> list[str]:
    """report_add's answer: the report line, the items it carries, the stage move it made (with
    `stage`) or what the usual next move needs (without)."""
    report = saved.report
    parsed = " · ".join(x for x in (report.state, report.size) if x)
    extra = f" ({parsed})" if parsed else ""
    lines = [f"Report #{report.id} added to **{saved.task.key}**{extra}: {report.summary}"]
    carried = [
        f"{n} {what}{'' if n == 1 else 's'}"
        for n, what in ((saved.findings, "finding"), (saved.questions, "question"))
        if n
    ]
    if carried:
        lines.append(f"Stored {' and '.join(carried)} for the coordinator to triage.")
    if stage is not None:
        current = saved.task.stage.value
        lines.append(f"Moved to {current}." if saved.moved else f"Already in {current}.")
        lines.extend(saved.warnings)
    elif saved.next_stage:
        move = f'Next: task_update(stage="{saved.next_stage}")'
        if saved.needs:
            needs = " and ".join(
                f"{requirement_label(r)} ({'done' if met else 'missing'})" for r, met in saved.needs
            )
            lines.append(f"{move} needs {needs}.")
        else:
            lines.append(f"{move}; no rule asks anything for it.")
    return lines


def report_item_dismissed(item: ReportItem) -> str:
    return f"Dismissed {item.kind} {item.n} of report #{item.report_id} on {item.task_key}."


def backlog_saved(item: BacklogItem, verb: str) -> str:
    state = ", ".join([item.status, *([item.kind] if item.kind else []), *_backlog_queue(item)])
    text = f"{verb} backlog item #{item.id} in {item.project_slug}: {item.title} ({state})."
    if item.moved_duplicates:
        names = ", ".join(f"#{n}" for n in item.moved_duplicates)
        text += f" {names}, duplicate of #{item.id}, now duplicate of #{item.duplicate_of} as well."
    return text


def backlog_similar(item: BacklogItem, similar: list[tuple[BacklogItem, float]]) -> str:
    """The line backlog_add appends when open items have a similar title; empty without."""
    if not similar:
        return ""
    names = ", ".join(f"#{b.id} ({score:.2f}) {b.title}" for b, score in similar)
    return (
        f"\nNote: similar open items: {names}; if it is the same,"
        f" backlog_update(item={item.id}, duplicate_of={similar[0][0].id})."
    )


def _question_state(q: Question) -> str:
    if q.status == "open":
        if q.blocking:
            return "open, blocking"
        return f"open, goes on with the default at {(q.default_after or '')[:16]}Z"
    if q.status == "withdrawn":
        return f"withdrawn by {q.withdrawn_by} {(q.withdrawn_at or '')[:16]}Z"
    if q.status == "defaulted":
        return f"closed with the default {(q.defaulted_at or '')[:16]}Z"
    delivered = f", delivered {q.delivered_at[:16]}Z" if q.delivered_at else ", not delivered yet"
    return f"answered by the owner {(q.answered_at or '')[:16]}Z{delivered}"


def question_line(q: Question) -> str:
    """One question as question_list shows it; the answer verbatim when there is one."""
    head = (
        f"- #{q.id} {q.origin} ({q.kind}, asked by {q.asked_by}"
        f" {q.asked_at[:16]}Z): {_question_state(q)}"
    )
    lines = [head, f"  {qrules.quoted(q.text)}"]
    if q.choices:
        lines.append(f"  Options: {', '.join(q.choices)}; default: {qrules.quoted(q.default)}")
    else:
        lines.append(f"  Default: {qrules.quoted(q.default)}")
    if q.look_url:
        account = f"; test account {q.look_account}" if q.look_account else ""
        lines.append(f"  Look at: {q.look_url}{account}")
    if q.answer is not None:
        lines.append(f"  Answer: {q.answer}")
    return "\n".join(lines)


def questions(task: Task | None, items: list[Question], status: str | None) -> str:
    where = task.key if task else "all cards"
    what = (status or "open").strip().lower() or "open"
    if not items:
        return f"No {what} questions on {where}." if what != "all" else f"No questions on {where}."
    head = f"{len(items)} question(s) on {where} ({what}), newest first:"
    return "\n".join([head, *map(question_line, items)])


def question_asked(task: Task | None, q: Question, hint: str) -> str:
    where = f"on **{task.key}**" if task is not None else "by the coordinator, without a card"
    return f"Question #{q.id} asked {where} ({q.kind}). {hint}"


def question_withdrawn(q: Question) -> str:
    return f"Question #{q.id} on {q.origin} withdrawn."


def _approval_state(a: Approval) -> str:
    if a.status == "open":
        return "open, waits for the owner"
    when = (a.decided_at or "")[:16]
    if a.status == "expired":
        return f"expired {(a.expired_at or '')[:16]}Z: {a.expired_reason}"
    if a.status == "refused":
        return f"refused by the owner {when}Z"
    if a.status == "used":
        return f"approved {when}Z, used {(a.used_at or '')[:16]}Z by {a.used_by}"
    if a.reserved:
        return f"approved by the owner {when}Z, held for a running call: {a.reserved_call}"
    return f"approved by the owner {when}Z, not used yet"


def approval_line(a: Approval) -> str:
    """One approval as approval_list shows it; the owner's line verbatim when there is one."""
    head = (
        f"- #{a.id} {a.task_key}: {a.what} (requested by {a.requested_by} via"
        f" {a.requested_via} {a.requested_at[:16]}Z): {_approval_state(a)}"
    )
    lines = [head, f"  Summary: {arules.quoted(a.summary, 300)}"]
    if a.rollback:
        lines.append(f"  Rollback: {arules.quoted(a.rollback, 300)}")
    if a.decision_text:
        lines.append(f"  The owner's line: {a.decision_text}")
    return "\n".join(lines)


def approvals(task: Task | None, items: list[Approval], status: str | None) -> str:
    where = task.key if task else "all cards"
    what = (status or "open").strip().lower() or "open"
    if not items:
        return f"No {what} approvals on {where}." if what != "all" else f"No approvals on {where}."
    head = f"{len(items)} approval(s) on {where} ({what}), newest first:"
    return "\n".join([head, *map(approval_line, items)])


def approval_withdrawn(a: Approval) -> str:
    return f"Approval #{a.id} on {a.task_key} withdrawn: {a.what} ({a.expired_reason})."


def approval_requested(task: Task, a: Approval, hint: str, updated: bool) -> str:
    verb = "updated" if updated else "requested"
    text = f"Approval #{a.id} {verb} on **{task.key}**: {a.what}. {hint}"
    if a.action != "deploy":
        return text
    if not task.deploy_notes:
        return (
            f"{text}\nThe card has no deploy notes: write what the deploy needs (migration, flag,"
            " script, rollback) with task_update(task, deploy_notes=...)."
        )
    state = deploy_state(task)
    return (
        f"{text}\nDeploy notes of the card ({state}), shown with the request:\n{task.deploy_notes}"
    )


# --- the day time log ------------------------------------------------------------------------

TIMELOG_NEXT = (
    "Next: timelog_mark(kind='switch' or 'pause') at the next change; timelog_list shows the"
    " day, timelog_fix(mark, reason, ...) corrects a mark."
)


def unclosed_hint(log: tl.DayLog) -> str:
    """The hint for the day it is shown for: HH:MM would land on today, so a past day names
    its date."""
    at = "HH:MM" if log.today else f"{log.day.isoformat()}THH:MM"
    return (
        f"counts 0: add the pause at the time you stopped, timelog_mark(kind='pause', at='{at}'),"
        " or timelog_fix the mark"
    )


def _segment_line(s: tl.Segment) -> str:
    start = s.start.strftime("%H:%M")
    where = f" ({s.project})" if s.key and s.project else ""
    if s.unclosed:
        return f"- {start}-?  unclosed  {s.name}{where} (#{s.mark_id} {s.kind})"
    end = "now" if s.running else (s.end.strftime("%H:%M") if s.end else "?")
    return f"- {start}-{end}  {tl.hm(s.seconds)}  {s.name}{where} (#{s.mark_id} {s.kind})"


def _totals(log: tl.DayLog) -> list[str]:
    out = []
    if log.by_project:
        parts = [f"{p or 'labels'} {tl.hm(s)}" for p, s in log.by_project]
        out.append("By project: " + " · ".join(parts))
    if log.by_card:
        out.append("By card: " + " · ".join(f"{k} {tl.hm(s)}" for k, _, s in log.by_card))
    if log.by_label:
        out.append("By label: " + " · ".join(f"{k} {tl.hm(s)}" for k, s in log.by_label))
    return out


def _now_line(log: tl.DayLog, word: str = "Now") -> str | None:
    c = log.current
    if c is None:
        return None
    return f"{word}: {c.name} since {c.start.strftime('%H:%M')} ({tl.hm(c.seconds)})"


def day_log(log: tl.DayLog, tz: Any, project: str | None = None) -> str:
    """The day view of timelog_list: the spans and pauses in time order, then the totals."""
    when = "today" if log.today else log.day.strftime("%a")
    scope = f" of {project}" if project else ""
    head = (
        f"**Time log{scope} {log.day.isoformat()}** ({when}, server time {log.offset}):"
        f" total **{tl.hm(log.total)}**"
    )
    if not log.segments and not log.marks:
        return "\n".join(
            [head, "No marks on this day. Start: timelog_mark(kind='start', task='KEY')."]
        )
    rows: list[tuple[Any, int, str]] = [
        (s.start, s.mark_id, _segment_line(s)) for s in log.segments
    ]
    for m in log.marks:
        if m.kind == tl.PAUSE:
            rows.append((tl.local(m.at, tz), m.id, f"- {tl.clock(m.at, tz)}  {m.target} (#{m.id})"))
    lines = [head]
    now = _now_line(log)
    if now:
        lines.append(now)
    lines += [r[2] for r in sorted(rows, key=lambda r: (r[0], r[1]))]
    lines += _totals(log)
    if log.unclosed:
        lines.append(f"Unclosed: {len(log.unclosed)} ({unclosed_hint(log)}).")
    return "\n".join(lines)


def time_marked(result: tl.MarkResult, tz: Any) -> str:
    m = result.mark
    what = f"#{m.id} {m.kind} at {tl.clock(m.at, tz)}: {m.target}"
    lines = [f"Marked {what}." if result.created else f"Nothing written. {result.warnings[0]}"]
    warnings = result.warnings if result.created else result.warnings[1:]
    if result.closed is not None and result.closed.end is not None:
        c = result.closed
        lines.append(
            f"Closed: {c.name} {c.start.strftime('%H:%M')}-{c.end.strftime('%H:%M')}"
            f" {tl.hm(c.seconds)}."
        )
    log = result.day
    if log is not None:
        day = "Today" if log.today else log.day.isoformat()
        now = _now_line(log, "now")
        lines.append(f"{day}: total {tl.hm(log.total)}" + (f" · {now}" if now else "") + ".")
        others = [u for u in log.unclosed if u.mark_id != m.id]
        if any(u.mark_id == m.id for u in log.unclosed):
            lines.append("This span counts 0 until a later mark (a switch or a pause) ends it.")
        if others:
            lines.append(f"Unclosed on that day: {len(others)} ({unclosed_hint(log)}).")
    lines += warnings
    lines.append(TIMELOG_NEXT)
    return "\n".join(lines)


def _mark_state(state: Mapping[str, Any], tz: Any) -> str:
    at = tl.clock(datetime.fromisoformat(state["at"]), tz)
    target = state.get("key") or state.get("label") or ""
    return f"{at} {state['kind']} {target}".rstrip()


def time_fixed(
    mark: tl.TimeMark | None, before: Mapping[str, Any], log: tl.DayLog, reason: str, tz: Any
) -> str:
    if mark is None:
        head = f"Mark removed (was {_mark_state(before, tz)}): {reason}."
    else:
        after = {"at": mark.at.isoformat(), "kind": mark.kind, "key": mark.task_key}
        after["label"] = mark.label
        head = (
            f"Mark #{mark.id} fixed: {_mark_state(before, tz)} -> {_mark_state(after, tz)}"
            f" ({reason})."
        )
    return "\n".join([head, day_log(log, tz)])


def card_time(task: Task, time: tl.CardTime, worklog: Sequence[Mapping[str, Any]]) -> str:
    """timelog_list(task=...): today, all days and the tracker's minutes, then the days and
    the tracker rows."""
    now = f" · now since {time.since.strftime('%H:%M')}" if time.current and time.since else ""
    lines = [
        f"**{task.key}** time: today {tl.hm(time.today)} · total {tl.hm(time.total)} · in"
        f" tracker {tl.hm_minutes(time.tracker)}{now}"
    ]
    if time.days:
        lines.append("Days: " + " · ".join(f"{d.isoformat()} {tl.hm(s)}" for d, s in time.days))
    if worklog:
        lines.append("Tracker rows (newest first):")
        for row in worklog:
            ref = f" {row['ref']}" if row.get("ref") else ""
            why = f" ({row['reason']})" if row.get("reason") else ""
            lines.append(
                f"- #{row['id']} {row['day']} {tl.hm_minutes(row['minutes'])}{ref}{why}"
                f" by {row['actor']}"
            )
    else:
        lines.append("No minutes entered in the tracker yet: worklog_add(task, minutes).")
    return "\n".join(lines)


def project_time(project: str, log: tl.DayLog, time: tl.CardTime) -> str:
    """timelog_list(project=...): the day's time per card, and the project's numbers."""
    lines = [
        f"**{project}**: today {tl.hm(time.today)} · total {tl.hm(time.total)} · in tracker"
        f" {tl.hm_minutes(time.tracker)}",
        f"On {log.day.isoformat()}: {tl.hm(log.total)}",
    ]
    lines += [f"- {k} {tl.hm(s)}" for k, _, s in log.by_card]
    if log.unclosed:
        lines.append(f"Unclosed: {len(log.unclosed)} ({unclosed_hint(log)}).")
    return "\n".join(lines)


def worklog_added(task: Task, row_id: int, minutes: int, day: str, time: tl.CardTime) -> str:
    return (
        f"Tracker row #{row_id} for {task.key} on {day}: {tl.hm_minutes(minutes)}. In tracker"
        f" {tl.hm_minutes(time.tracker)} · today {tl.hm(time.today)} · total {tl.hm(time.total)}."
    )
