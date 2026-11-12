"""Task plan: ordered steps with checkpoints and the plan's history, plus the agent's Claude Code
todo list read from PostToolUse hooks (a second layer, never merged into the plan)."""

import json
from dataclasses import dataclass, field
from typing import Any

from relaybay.domain.errors import ValidationError

PLAN_STATUSES = ("draft", "accepted")
STEP_STATUSES = ("pending", "in_progress", "done", "blocked")
PLAN_STEPS_MAX = 30
STEP_TITLE_MAX = 200
STEP_DETAILS_MAX = 2000
OUTCOME_MAX = 300
CONFORMANCE_MAX = 300
REASON_MAX = 300
AS_PLANNED = "as planned"
DEVIATION = "deviation:"

TODO_TOOLS = ("TaskCreate", "TaskUpdate", "TodoWrite")
TODO_STATUSES = ("pending", "in_progress", "completed")
TODO_ITEMS_MAX = 50
TODO_TEXT_MAX = 200
TODO_ID_MAX = 40

DEFAULT_REMINDER_EVERY = 30
REMINDER_DETAILS_MAX = 300
REMINDER_TASKS_MAX = 3
NO_PLAN_HINT = "write down what you are going to do (todo or 2-3 steps with plan_set)"
ADDED_MARK = "added after acceptance"
CLOSED_PLAN = (
    "the plan is closed ({done}/{total} done) while work goes on: add a step with plan_step_add"
    " or reopen one"
)
DEFAULT_DRAFT_HINT_HOURS = 1


@dataclass(slots=True)
class PlanStep:
    id: int
    task_id: int
    position: int
    title: str
    details_md: str | None
    status: str
    done_at: str | None
    link: str | None
    outcome: str | None
    conformance: str | None
    created_by: str
    created_at: str
    updated_at: str
    added_after_acceptance_at: str | None = None  # set by plan_step_add on an accepted plan
    added_by: str | None = None
    added_reason: str | None = None

    @property
    def deviation(self) -> bool:
        return bool(self.conformance and self.conformance.startswith(DEVIATION))


@dataclass(slots=True)
class Plan:
    """A task's plan; `status` is None while it has no steps."""

    status: str | None
    accepted_at: str | None
    accepted_by: str | None
    steps: list[PlanStep] = field(default_factory=list)

    @property
    def done(self) -> int:
        return sum(1 for s in self.steps if s.status == "done")

    @property
    def deviations(self) -> int:
        return sum(1 for s in self.steps if s.deviation)

    @property
    def current(self) -> PlanStep | None:
        """The step in progress, else the first one not done yet."""
        active = [s for s in self.steps if s.status == "in_progress"]
        rest = [s for s in self.steps if s.status != "done"]
        return (active or rest or [None])[0]


@dataclass(slots=True)
class PlanRevision:
    id: int
    task_id: int
    revision: int
    body: dict[str, Any]
    author: str
    reason: str
    created_at: str

    @property
    def titles(self) -> list[str]:
        return [str(s.get("title", "")) for s in self.body.get("steps", [])]

    @property
    def added(self) -> set[int]:
        """Positions (1, 2, …) of the steps marked as added after acceptance."""
        steps = self.body.get("steps", [])
        return {i for i, s in enumerate(steps, 1) if s.get("added_after_acceptance_at")}


@dataclass(slots=True)
class TodoItem:
    item_id: str
    subject: str
    status: str
    active_form: str | None
    position: int = 0
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class TodoChange:
    """What one todo tool call did: `replace` the whole list (TodoWrite), `create` one item or
    `update` one (a `status` of `deleted` removes it)."""

    kind: str
    items: tuple[TodoItem, ...] = ()
    item_id: str | None = None
    status: str | None = None
    subject: str | None = None
    active_form: str | None = None


def one_line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or None


def clean_step_title(value: object) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        raise ValidationError("A plan step needs a title.")
    if len(text) > STEP_TITLE_MAX:
        raise ValidationError(f"A step title is longer than {STEP_TITLE_MAX} characters.")
    return text


def clean_conformance(value: object) -> str:
    """'as planned', or 'deviation: <why>' with a reason."""
    text = " ".join(str(value or "").split())
    if text.lower() == AS_PLANNED:
        return AS_PLANNED
    if text.lower().startswith(DEVIATION):
        why = text[len(DEVIATION) :].strip()
        if why:
            if len(why) > CONFORMANCE_MAX:
                raise ValidationError(
                    f"The deviation reason is longer than {CONFORMANCE_MAX} characters."
                )
            return f"{DEVIATION} {why}"
    raise ValidationError(
        "A done step needs conformance: 'as planned' or 'deviation: <why it went otherwise>'."
    )


def clean_outcome(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("outcome must be a string.")
    text = " ".join(value.split())
    if len(text) > OUTCOME_MAX:
        raise ValidationError(f"outcome is longer than {OUTCOME_MAX} characters (one line).")
    return text or None


def step_body(step: PlanStep) -> dict[str, Any]:
    body = {
        "title": step.title,
        "details": step.details_md,
        "status": step.status,
        "done_at": step.done_at,
        "link": step.link,
        "outcome": step.outcome,
        "conformance": step.conformance,
    }
    if step.added_after_acceptance_at:
        body["added_after_acceptance_at"] = step.added_after_acceptance_at
        body["added_by"] = step.added_by
        body["added_reason"] = step.added_reason
    return body


def plan_body(status: str | None, steps: list[PlanStep]) -> dict[str, Any]:
    """The whole plan as kept in a revision."""
    return {"status": status, "steps": [step_body(s) for s in steps]}


def title_diff(before: list[str], after: list[str], added: set[int] | None = None) -> list[str]:
    """`- title` / `+ title` lines for the step titles that changed, in plan order; a new step
    whose position is in `added` is marked as added after acceptance."""
    if before == after:
        return []
    lines = []
    for i in range(max(len(before), len(after))):
        old = before[i] if i < len(before) else None
        new = after[i] if i < len(after) else None
        if old == new:
            continue
        if old is not None:
            lines.append(f"- {i + 1}. {old}")
        if new is not None:
            mark = f" ({ADDED_MARK})" if added and i + 1 in added else ""
            lines.append(f"+ {i + 1}. {new}{mark}")
    return lines


# --- Claude Code todo tools -----------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    """A tool input or response as a dict; some builds send the response as a JSON string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _item_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    text = str(value).strip()
    return text[:TODO_ID_MAX] or None


def parse_todo(payload: Any) -> TodoChange | None:
    """The todo change of a PostToolUse payload of TaskCreate, TaskUpdate (current Claude Code)
    or TodoWrite (older builds); None for anything else or a shape it does not know."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostToolUse":
        return None
    tool = payload.get("tool_name")
    if tool not in TODO_TOOLS:
        return None
    given = _mapping(payload.get("tool_input"))
    answer = _mapping(payload.get("tool_response"))
    if tool == "TodoWrite":
        todos = given.get("todos")
        if not isinstance(todos, list):
            return None
        items = []
        for entry in todos[:TODO_ITEMS_MAX]:
            if not isinstance(entry, dict):
                continue
            subject = one_line(entry.get("content"), TODO_TEXT_MAX)
            status = entry.get("status")
            if subject is None or status not in TODO_STATUSES:
                continue
            item_id = _item_id(entry.get("id")) or str(len(items) + 1)
            active = one_line(entry.get("activeForm"), TODO_TEXT_MAX)
            items.append(TodoItem(item_id, subject, status, active, len(items) + 1))
        return TodoChange("replace", items=tuple(items))
    if tool == "TaskCreate":
        created = _mapping(answer.get("task"))
        item_id = _item_id(created.get("id"))
        subject = one_line(given.get("subject") or created.get("subject"), TODO_TEXT_MAX)
        if item_id is None or subject is None:
            return None
        active = one_line(given.get("activeForm"), TODO_TEXT_MAX)
        return TodoChange("create", item_id=item_id, subject=subject, active_form=active)
    item_id = _item_id(given.get("taskId") or answer.get("taskId"))
    if item_id is None:
        return None
    status = given.get("status")
    if status is None:
        status = _mapping(answer.get("statusChange")).get("to")
    if status not in (*TODO_STATUSES, "deleted"):
        status = None
    subject = one_line(given.get("subject"), TODO_TEXT_MAX)
    active = one_line(given.get("activeForm"), TODO_TEXT_MAX)
    if status is None and subject is None and active is None:
        return None
    return TodoChange("update", item_id=item_id, status=status, subject=subject, active_form=active)


def todo_now(items: list[TodoItem]) -> TodoItem | None:
    return next((i for i in items if i.status == "in_progress"), None)


def todo_label(item: TodoItem) -> str:
    return item.active_form or item.subject


# --- reminders through hooks ----------------------------------------------------------------


def reminder_line(
    task_key: str, stage: str, plan: Plan, todo: list[TodoItem], details: bool = True
) -> str | None:
    """What the agent should keep in mind about one task: its current plan step (N/M, title,
    what and why) and the todo item in progress; a hint when a task in dev has neither."""
    now = todo_now(todo)
    doing = f" Now doing: {todo_label(now)}." if now else ""
    if plan.steps:
        state = "accepted" if plan.status == "accepted" else "draft, not accepted yet"
        step = plan.current
        if step is None and stage != "dev":
            return f"Plan of {task_key} ({state}): all {len(plan.steps)} steps done.{doing}"
        if step is None:
            closed = CLOSED_PLAN.format(done=plan.done, total=len(plan.steps))
            return f"Plan of {task_key} ({state}): {closed}.{doing}"
        verb = "step" if step.status == "in_progress" else "next step"
        line = (
            f"Plan of {task_key} ({state}): {verb} {step.position}/{len(plan.steps)} '{step.title}'"
        )
        if step.status == "blocked":
            line += " (blocked)"
        if details and step.details_md:
            line += f" — {one_line(step.details_md, REMINDER_DETAILS_MAX)}"
        return (line if line.endswith(".") else line + ".") + doing
    if todo:
        done = sum(1 for i in todo if i.status == "completed")
        return (
            f"{task_key} has no plan; your todo ({done}/{len(todo)} done) is shown as the"
            f" agent's plan, not accepted.{doing}"
        )
    if stage == "dev":
        return f"{task_key} is in dev without a plan: {NO_PLAN_HINT}."
    return None


REMINDER_TAIL = (
    "Mark plan steps with plan_step_update (in_progress, done with 'as planned' or"
    " 'deviation: why')."
)


# Written when a hook told the bound session that the owner accepted its plan on the board.
ACCEPTANCE_TOLD = "plan.acceptance_delivered"
# Hours: an older acceptance is not told any more (after an upgrade, or to a long idle session).
ACCEPTANCE_TELL_HOURS = 2
# Characters of one hook answer the acceptance lines may take; the rest waits for the next hook,
# so a coordinator told of many acceptances still gets its owner comments.
ACCEPTANCE_ROOM = 2000


def acceptance_line(task_key: str, plan: Plan) -> str:
    """What the bound session's next hook says once the owner accepted its plan on the board:
    the first step not done yet, or that all are done."""
    total = len(plan.steps)
    head = f"Owner accepted the plan of {task_key} ({total} step{'' if total == 1 else 's'})"
    step = next((s for s in plan.steps if s.status != "done"), None)
    if step is None:
        return f"{head}: all steps are done."
    return f"{head}: go on with step {step.position}."


def coordinator_acceptance_line(task_key: str, plan: Plan, bound_to: str | None) -> str:
    """What a coordinator session's next hook says when the owner accepted a plan on the board."""
    total = len(plan.steps)
    bound = f", bound to {bound_to}" if bound_to else ", no session bound"
    return (
        f"Owner accepted the plan of {task_key} ({total} step{'' if total == 1 else 's'})"
        f" on the board{bound}."
    )


def reminder_text(lines: list[str]) -> str | None:
    if not lines:
        return None
    return "\n".join(["relaybay plan reminder:", *lines[:REMINDER_TASKS_MAX], REMINDER_TAIL])
