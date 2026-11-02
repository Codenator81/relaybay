"""Short, model-friendly markdown for tool results."""

from datetime import UTC, datetime

from relaybay.domain.models import Decision, Event, Project, Report, Task
from relaybay.domain.stages import STAGES
from relaybay.service import ProjectSummary, Snapshot, TaskDetail


def age(ts: str, now: datetime | None = None) -> str:
    then = datetime.fromisoformat(ts)
    seconds = int(((now or datetime.now(UTC)) - then).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def _who(name: str, external_id: str | None) -> str:
    """`name (local_…)` for a desktop session, the plain name otherwise."""
    if external_id and external_id != name:
        return f"{name} ({external_id})"
    return name


def session_label(task: Task) -> str:
    return _who(task.session_name, task.session_external_id) if task.session_name else ""


def report_line(report: Report) -> str:
    head = " · ".join(x for x in (report.state, report.size) if x)
    prefix = f"{head} · " if head else ""
    return f"{prefix}{report.summary} ({report.author}, {age(report.created_at)})"


def task_line(task: Task, last: Report | None = None) -> str:
    parts = [f"**{task.key}** {task.title}"]
    extras = []
    if task.waiting_on:
        extras.append(f"waiting on: {task.waiting_on}")
    if task.next_step:
        extras.append(f"next: {task.next_step}")
    if task.branch:
        extras.append(f"branch `{task.branch}`")
    if task.session_name:
        extras.append(f"session: {session_label(task)}")
    extras.append(f"v{task.version}")
    line = f"- {parts[0]} — " + " · ".join(extras)
    if task.stage_note:
        line += f"\n  note: {task.stage_note}"
    if last:
        line += f"\n  last report: {report_line(last)}"
    return line


def project_list(items: list[ProjectSummary]) -> str:
    if not items:
        return "No projects yet. Create one with project_upsert."
    lines = ["| project | name | " + " | ".join(STAGES) + " |", "|---|---|" + "---|" * len(STAGES)]
    for item in items:
        counts = " | ".join(str(item.counts.get(s, 0)) for s in STAGES)
        lines.append(f"| {item.project.slug} | {item.project.name} | {counts} |")
    return "\n".join(lines)


def project(p: Project, created: bool) -> str:
    verb = "Created" if created else "Updated"
    repo = f" · repo `{p.repo_path}`" if p.repo_path else ""
    return f"{verb} project **{p.slug}** ({p.name}){repo}."


def snapshot(snap: Snapshot) -> str:
    p = snap.project
    lines = [f"# {p.slug} — {p.name}"]
    if p.description:
        lines.append(p.description)
    by_stage: dict[str, list[Task]] = {}
    for task in snap.tasks:
        by_stage.setdefault(task.stage.value, []).append(task)
    counts = " · ".join(f"{s} {len(by_stage.get(s, []))}" for s in STAGES if s in by_stage)
    lines.append(f"Tasks: {len(snap.tasks)}" + (f" ({counts})" if counts else ""))
    for stage in STAGES:
        tasks = by_stage.get(stage)
        if not tasks:
            continue
        lines.append(f"\n## {stage}")
        lines.extend(task_line(t, snap.last_reports.get(t.id)) for t in tasks)
    if snap.decisions:
        lines.append("\n## Recent decisions")
        lines.extend(decision_line(d) for d in snap.decisions)
    return "\n".join(lines)


def decision_line(d: Decision) -> str:
    who = f" — {d.decided_by}" if d.decided_by else ""
    task = f" [{d.task_key}]" if d.task_key else ""
    return f"- {d.created_at[:10]}{task}: {d.text}{who}"


def task_list(tasks: list[Task]) -> str:
    if not tasks:
        return "No matching tasks."
    return "\n".join(f"{task_line(t)} · stage {t.stage}" for t in tasks)


def task_detail(detail: TaskDetail) -> str:
    t = detail.task
    lines = [
        f"# {t.key} — {t.title}",
        f"- project: {t.project_slug}",
        f"- stage: {t.stage}" + (f" ({t.stage_note})" if t.stage_note else ""),
        f"- version: {t.version}",
    ]
    for label, value in (
        ("waiting on", t.waiting_on),
        ("next step", t.next_step),
        ("branch", t.branch),
        ("doc", t.doc_path),
        ("session", session_label(t)),
        ("open in desktop", t.session_link),
    ):
        if value:
            lines.append(f"- {label}: {value}")
    lines.append(f"- updated: {age(t.updated_at)}")
    if detail.bindings:
        lines.append("\n## Sessions (newest first)")
        for b in detail.bindings:
            until = f"until {b.unbound_at[:16]}Z" if b.unbound_at else "current"
            who = _who(b.session_name, b.session_external_id)
            lines.append(f"- {who} — bound {b.bound_at[:16]}Z by {b.bound_by}, {until}")
    if detail.reports:
        lines.append("\n## Reports (newest first)")
        for r in detail.reports:
            lines.append(f"\n### {report_line(r)}\n{r.body_md}")
    if detail.decisions:
        lines.append("\n## Decisions")
        lines.extend(decision_line(d) for d in detail.decisions)
    return "\n".join(lines)


def task_saved(task: Task, verb: str) -> str:
    return f"{verb} **{task.key}** “{task.title}” · stage {task.stage} · version {task.version}."


def events(items: list[Event], since_id: int) -> str:
    if not items:
        return f"No events after #{since_id}."
    lines = []
    for e in items:
        key = e.payload.get("key") or e.payload.get("project") or ""
        detail = ""
        if e.kind == "task.stage_changed":
            detail = f"{e.payload.get('from')} → {e.payload.get('to')}"
        elif e.kind in ("report.added", "task.created"):
            detail = str(e.payload.get("summary") or e.payload.get("title") or "")
        elif e.kind == "task.updated":
            detail = ", ".join(sorted(e.payload.get("changes", {})))
        elif e.kind == "task.session_bound":
            detail = f"→ {e.payload.get('session')}"
        elif e.kind == "decision.added":
            detail = str(e.payload.get("text", ""))
        lines.append(f"- #{e.id} {e.ts[:19]}Z {e.kind} {key} {detail} ({e.actor})".rstrip())
    lines.append(f"\nNext call: events_since(since_id={items[-1].id})")
    return "\n".join(lines)
