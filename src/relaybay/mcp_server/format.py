"""Short, model-friendly markdown for tool results."""

import json
from datetime import UTC, datetime

from relaybay.domain.environments import (
    AccessEntry,
    Environment,
    PolicyProposal,
    Proposal,
    change_summary,
)
from relaybay.domain.models import (
    Comment,
    Decision,
    Event,
    Project,
    Report,
    Task,
    TrackerEntry,
)
from relaybay.domain.review import Attachment, Review, ReviewStep
from relaybay.domain.rules import Rules, requirement_label, requirement_short
from relaybay.domain.stages import STAGES, Stage
from relaybay.domain.task_fields import FIELD_NAMES, TASK_FIELDS
from relaybay.policy import EffectivePolicy
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


def who_label(name: str, external_id: str | None) -> str:
    """`name (local_…)` for a desktop session, the plain name otherwise."""
    if external_id and external_id != name:
        return f"{name} ({external_id})"
    return name


def session_label(task: Task) -> str:
    return who_label(task.session_name, task.session_external_id) if task.session_name else ""


SHORT_MAX = 160


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


def task_line(
    task: Task, last: Report | None = None, *, texts: bool = False, require_log: bool = False
) -> str:
    parts = [f"**{task.key}** {task.title}"]
    extras = []
    if task.waiting_on:
        extras.append(f"waiting on: {task.waiting_on}")
    if task.next_step:
        extras.append(f"next: {next_step_label(task)}")
    if task.branch:
        extras.append(f"branch `{task.branch}`")
    if task.session_name:
        extras.append(f"session: {session_label(task)}")
    if tracker := tracker_label(task, require_log):
        extras.append(f"tracker: {tracker}")
    if task.review_total:
        extras.append(f"review {task.review_accepted}/{task.review_total}")
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


def review_text(task: Task, review: Review, comments: list[Comment]) -> str:
    """A task's checklist for `review_get`."""
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
        lines.extend(attachment_line(a) for a in step.attachments)
        notes = [c for c in comments if c.step_id == step.id]
        lines.extend(f"   owner #{c.id}: {short(c.body_md, 400)}" for c in notes)
    if review.loose:
        lines.append("Screenshots of no step:")
        lines.extend(attachment_line(a) for a in review.loose)
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


def project(p: Project, created: bool) -> str:
    verb = "Created" if created else "Updated"
    repo = f" · repo `{p.repo_path}`" if p.repo_path else ""
    return f"{verb} project **{p.slug}** ({p.name}){repo}."


def rules_line(rules: Rules) -> str:
    """`Rules 1a2b3c4d: report before test, progress before done (read relaybay://rules/x)`."""
    needs = [
        f"{requirement_short(req)} before {stage}"
        for stage in STAGES
        for req in rules.requirements(stage)
    ]
    line = f"Rules {rules.hash}: {', '.join(needs) or 'no stage requirements'} (read {rules.uri})"
    if rules.override_error:
        line += f"; project override ignored: {rules.override_error}"
    return line


MODE_HELP = {
    "warn": "a stage move that misses a requirement succeeds and the response carries a warning",
    "strict": "a stage move that misses a requirement is refused with the reason",
}


def rules_text(rules: Rules) -> str:
    """The merged rules as markdown (resource `relaybay://rules[/{project}]`, `rules_get`)."""
    body = rules.body
    if rules.project is None:
        scope = "global"
    elif rules.override_error:
        scope = f"project {rules.project} (global rules only)"
    else:
        scope = f"project {rules.project} (global + project override)"
    report_format = body["report_format"]
    lines = [
        f"# Rules (hash {rules.hash})",
        f"- scope: {scope}",
        f"- mode: {rules.mode} — {MODE_HELP[rules.mode]}",
        *([f"- project override ignored: {rules.override_error}"] if rules.override_error else []),
        "",
        "## Report format",
        f"- first line: `{report_format['first_line']}`",
        f"- states: {', '.join(report_format['states'])}",
        f"- sizes: {', '.join(report_format['sizes'])}",
        "",
        "## Stage requirements",
    ]
    reqs = [(stage, rules.requirements(stage)) for stage in STAGES]
    reqs = [(stage, items) for stage, items in reqs if items]
    if not reqs:
        lines.append("- none")
    for stage, items in reqs:
        lines.append(f"- moving to {stage} needs {' and '.join(map(requirement_label, items))}")
    if body.get("description_template"):
        lines += ["", "## Description template", body["description_template"]]
    if body.get("guidance"):
        lines += ["", "## Guidance", body["guidance"]]
    return "\n".join(lines)


def with_warnings(text: str, warnings: list[str]) -> str:
    return "\n".join([text, *warnings])


def snapshot(snap: Snapshot, require_log: bool = False) -> str:
    p = snap.project
    lines = [rules_line(snap.rules), f"# {p.slug} — {p.name}"]
    if p.description:
        lines.append(p.description)
    by_stage: dict[str, list[Task]] = {}
    for task in snap.tasks:
        by_stage.setdefault(task.stage.value, []).append(task)
    if snap.environments:
        lines.append("Environments (full map: environments_get):")
        lines.extend(environment_line(e, snap.policies[e.id]) for e in snap.environments)
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
    if snap.decisions:
        lines.append("\n## Recent decisions")
        lines.extend(decision_line(d) for d in snap.decisions)
    return "\n".join(lines)


def level_label(env: Environment, policy: EffectivePolicy) -> str:
    """`read (default)` from the default preset, `full (policy: preset:trusted)` from another
    layer, or `ask` when the environment sets it."""
    level = policy.level_for(env)
    origin = policy.level_origin(env)
    if origin == "environment":
        return level
    return f"{level} (default)" if origin == "preset:default" else f"{level} (policy: {origin})"


def environment_line(env: Environment, policy: EffectivePolicy) -> str:
    """`- live — live · agents: read (default) · https://shop.example.com`."""
    parts = [env.kind, f"agents: {level_label(env, policy)}"]
    if env.base_url:
        parts.append(env.base_url)
    return f"- {env.name} — " + " · ".join(parts)


LEVEL_HELP = (
    "Levels: none (do not touch), read (look only), ask (ask the owner before each action),"
    " full (act as needed). Only the owner changes them on the board; propose a change with"
    " environment_propose (an environment) or policy_propose (the policy)."
)


def _test_secrets_line(entry: AccessEntry, policy: EffectivePolicy) -> str | None:
    if not entry.test_secrets:
        return None
    if policy.expose_test_secrets:
        values = ", ".join(f"{k}={v}" for k, v in entry.test_secrets.items())
        return f"  test secrets (harmless test values): {values}"
    names = ", ".join(entry.test_secrets)
    return f"  test secrets (values not shared with agents; ask the owner): {names}"


def environments_text(
    slug: str,
    items: list[Environment],
    policies: dict[int, EffectivePolicy],
    project_policy: EffectivePolicy,
) -> str:
    """The full environments map of a project (`environments_get`, resource
    `relaybay://project/{slug}/environments`). It holds references only, never secrets; test
    secrets only when the policy exposes them."""
    if not items:
        return (
            f"No environments for {slug} yet. The owner adds them on the board (Stands &"
            " access); propose one with environment_propose."
        )
    mode = project_policy.guard_mode
    lines = [
        f"# Environments of {slug}",
        f"Policy: preset {project_policy.preset} (details: policy_get). Guard: {mode}."
        f" {LEVEL_HELP}",
        "Secrets are never stored here, only where they live (a file, a variable, a vault).",
    ]
    for env in items:
        policy = policies[env.id]
        lines.append(f"\n## {env.name} ({env.kind})")
        if env.base_url:
            lines.append(f"- base URL: {env.base_url}")
        level = f"- agents: {level_label(env, policy)}"
        if env.overrides:
            level += "; overrides: " + ", ".join(f"{k} {v}" for k, v in env.overrides.items())
        lines.append(level)
        if policy.guard_mode != mode:
            lines.append(f"- guard: {policy.guard_mode}")
        sign_in = "yes" if env.sign_in_allowed else "no"
        if env.login_state_path:
            sign_in += f" · login state: `{env.login_state_path}`"
        lines.append(f"- agent may sign in and remember the login: {sign_in}")
        for entry in env.access:
            values = ", ".join(f"{k}={v}" for k, v in entry.fields.items())
            lines.append(f"- {entry.kind}: {values}".rstrip())
            tests = _test_secrets_line(entry, policy)
            if tests:
                lines.append(tests)
        if env.notes:
            lines.append(f"- notes: {short(env.notes, 500)}")
        lines.append(f"- updated {env.updated_at[:16]}Z by {env.updated_by}")
    return "\n".join(lines)


def policy_text(policy: EffectivePolicy, scope: str) -> str:
    """The effective policy of a scope with the layer that set each value (`policy_get`)."""
    chain = " → ".join(layer.name for layer in policy.layers)
    lines = [
        f"# Effective policy: {scope}",
        f"Layers (a later one wins): {chain}. The environment's own level and overrides on the"
        " board win over the policy. Propose changes with policy_propose.",
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


def proposal_saved(p: Proposal) -> str:
    return (
        f"Proposal #{p.id} for {p.project_slug} recorded: {change_summary(p.change)}. Nothing"
        " changed yet: the owner accepts or rejects it on the board (Stands & access)."
    )


def decision_line(d: Decision) -> str:
    who = f" — {d.decided_by}" if d.decided_by else ""
    task = f" [{d.task_key}]" if d.task_key else ""
    return f"- {d.created_at[:10]}{task}: {d.text}{who}"


def task_list(tasks: list[Task], require_log: bool = False) -> str:
    if not tasks:
        return "No matching tasks."
    return "\n".join(f"{task_line(t, require_log=require_log)} · stage {t.stage}" for t in tasks)


def task_detail(detail: TaskDetail, require_log: bool = False) -> str:
    t = detail.task
    lines = [
        f"# {t.key} — {t.title}",
        f"- project: {t.project_slug}",
        f"- stage: {t.stage}" + (f" ({t.stage_note})" if t.stage_note else ""),
        f"- version: {t.version}",
    ]
    for label, value in (
        ("waiting on", t.waiting_on),
        ("next step", next_step_label(t)),
        ("branch", t.branch),
        ("doc", t.doc_path),
        ("session", session_label(t)),
        ("open in desktop", t.session_link),
        ("tracker", tracker_label(t, require_log)),
    ):
        if value:
            lines.append(f"- {label}: {value}")
    lines.append(f"- updated: {age(t.updated_at)}")
    unread = sum(1 for c in detail.comments if c.from_owner and not c.read_at)
    if unread:
        lines.append(f"- unread owner comments: {unread} (call comments_list)")
    if t.description:
        lines.append(f"\n## Description\n{t.description}")
    if t.progress:
        lines.append(f"\n## Progress (done so far)\n{t.progress}")
    if detail.bindings:
        lines.append("\n## Sessions (newest first)")
        for b in detail.bindings:
            until = f"until {b.unbound_at[:16]}Z" if b.unbound_at else "current"
            who = who_label(b.session_name, b.session_external_id)
            lines.append(f"- {who} — bound {b.bound_at[:16]}Z by {b.bound_by}, {until}")
    if detail.tracker_history:
        lines.append("\n## Tracker history (newest first)")
        lines.extend(tracker_entry_line(e) for e in detail.tracker_history)
    if detail.reports:
        lines.append("\n## Reports (newest first)")
        for r in detail.reports:
            lines.append(f"\n### {report_line(r)}\n{r.body_md}")
    if detail.decisions:
        lines.append("\n## Decisions")
        lines.extend(decision_line(d) for d in detail.decisions)
    lines.append(f"\nEditable fields: {', '.join(FIELD_NAMES)}")
    return "\n".join(lines)


def task_schema() -> str:
    """The editable task fields as a markdown table (resource `relaybay://schema/task`)."""
    lines = [
        "# Task fields",
        "Set any of them with task_patch(task, fields={...}); omitted fields stay as they are.",
        "",
        "| field | type | description | clearing |",
        "|---|---|---|---|",
    ]
    for f in TASK_FIELDS:
        limit = f" Up to {f.max_length} characters." if f.max_length else ""
        lines.append(f"| `{f.name}` | {f.type} | {f.description}{limit} | {f.clear} |")
    return "\n".join(lines)


def task_saved(task: Task, verb: str) -> str:
    return f"{verb} **{task.key}** “{task.title}” · stage {task.stage} · version {task.version}."


def _minute(ts: str) -> str:
    return f"{ts[:16]}Z"


def comment_marks(c: Comment) -> str:
    """`sent … · delivered … to <session> · read …` for an owner comment; `reply · unread` for
    an agent's."""
    if not c.from_owner:
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
        lines.append(f"\n## #{c.id} {who} · {comment_marks(c)}\n{c.body_md}")
    if any(c.from_owner and not c.read_at for c in items):
        lines.append(
            "\nHandle the unread owner comments, then comment_ack(comment_id=…) or reply with"
            " comment_add(task, body, in_reply_to=…)."
        )
    return "\n".join(lines)


def event_subject(e: Event) -> str:
    """The task key of an event, or its project."""
    return str(e.payload.get("key") or e.payload.get("project") or "")


def event_detail(e: Event) -> str:
    """What changed, in a few words: `dev → test`, a report's summary, the changed fields."""
    p = e.payload
    if e.kind == "task.stage_changed":
        return f"{p.get('from')} → {p.get('to')}"
    if e.kind in ("report.added", "task.created"):
        return str(p.get("summary") or p.get("title") or "")
    if e.kind == "task.updated":
        return ", ".join(sorted(p.get("changes", {})))
    if e.kind == "task.session_bound":
        return f"→ {p.get('session')}"
    if e.kind == "task.session_unbound":
        return f"✕ {p.get('session')}"
    if e.kind == "session.alias_added":
        return f"'{p.get('alias')}' = {p.get('session')}"
    if e.kind == "task.tracker_unlogged":
        return f"{p.get('tracker_ref') or ''} ({p.get('reason')})".strip()
    if e.kind == "decision.added":
        return str(p.get("text", ""))
    if e.kind == "session.status_changed":
        change = f"{p['previous']} → {p.get('status')}" if p.get("previous") else p.get("status")
        return f"{p.get('session')}: {change}"
    if e.kind == "comment.added":
        return f"#{p.get('comment_id')} {p.get('excerpt') or ''}".rstrip()
    if e.kind == "comment.delivered":
        return f"#{p.get('comment_id')} → {p.get('session')}"
    if e.kind == "comment.read":
        return f"#{p.get('comment_id')} read"
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
    if e.kind == "session.seen":
        reason = p.get("unlinked_reason")
        return f"{p.get('session')}" + (f" (unlinked: {reason})" if reason else "")
    return ""


def events(items: list[Event], since_id: int) -> str:
    if not items:
        return f"No events after #{since_id}."
    lines = []
    for e in items:
        key, detail = event_subject(e), event_detail(e)
        lines.append(f"- #{e.id} {e.ts[:19]}Z {e.kind} {key} {detail} ({e.actor})".rstrip())
    lines.append(f"\nNext call: events_since(since_id={items[-1].id})")
    return "\n".join(lines)
