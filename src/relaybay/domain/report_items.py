"""Findings and questions a report carries (`report_add(findings=, questions=)`): data the
coordinator triages, never a backlog item by itself. A finding becomes one with
`backlog_add(from_report=, finding=)`; any item can be dismissed with `report_item_dismiss`."""

from dataclasses import dataclass
from typing import Any

from relaybay.domain import backlog, questions
from relaybay.domain.errors import ValidationError
from relaybay.domain.plan import one_line

FINDING = "finding"
QUESTION = "question"
KINDS = (FINDING, QUESTION)
ITEMS_MAX = 10  # per report and kind
DISMISS_REASON_MAX = backlog.REASON_MAX
DIGEST_MAX = 8  # open items a digest lists; the rest is counted
CARD_MAX = 5  # open items task_context and task_get list of their card
LINE_TEXT_MAX = 160

_FINDING_FIELDS = {
    "title": backlog.TITLE_MAX,
    "body": backlog.BODY_MAX,
    "location": backlog.LOCATION_MAX,
    "kind": 20,
    "area": backlog.AREA_MAX,
}
_QUESTION_FIELDS = {"text": questions.TEXT_MAX, "default": questions.DEFAULT_MAX}

TRIAGE_LINE = (
    "Triage: backlog_add(project, title, from_report=<id>, finding=<n>) or dismiss with"
    " report_item_dismiss(report=<id>, n=<n>, kind, reason)."
)


@dataclass(frozen=True, slots=True)
class ReportItem:
    id: int
    report_id: int
    task_id: int
    task_key: str
    project_id: int
    kind: str
    n: int
    title: str  # a finding's title, a question's text
    body: str | None
    location: str | None
    item_kind: str | None
    area: str | None
    question_default: str | None
    backlog_id: int | None
    dismissed_at: str | None
    dismissed_by: str | None
    dismiss_reason: str | None
    created_at: str

    @property
    def is_open(self) -> bool:
        return self.backlog_id is None and self.dismissed_at is None


def _text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be text.")
    text = value.replace("\r\n", "\n").strip()
    if len(text) > limit:
        raise ValidationError(f"{name} is longer than {limit} characters.")
    return text or None


def _items(value: Any, kind: str, fields: dict[str, int], required: tuple[str, ...]) -> list[dict]:
    """The items as {field: clean text}; any wrong shape fails naming the item (1-based, as `n`)
    and the field, before anything is written."""
    if value is None:
        return []
    plural = f"{kind}s"
    if not isinstance(value, list):
        raise ValidationError(f"{plural} must be a list of objects.")
    if len(value) > ITEMS_MAX:
        raise ValidationError(f"{plural}: at most {ITEMS_MAX} per report, got {len(value)}.")
    out = []
    for n, entry in enumerate(value, start=1):
        where = f"{plural}[{n}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{where} must be an object.")
        unknown = sorted(set(entry) - set(fields))
        if unknown:
            raise ValidationError(
                f"{where}: unknown field(s) {', '.join(unknown)}; allowed: {', '.join(fields)}."
            )
        clean = {name: _text(entry.get(name), f"{where}.{name}", fields[name]) for name in fields}
        for name in required:
            if not clean[name]:
                raise ValidationError(f"{where}.{name} is required.")
        out.append(clean)
    return out


def parse_findings(value: Any) -> list[dict]:
    found = _items(value, FINDING, _FINDING_FIELDS, ("title",))
    for n, item in enumerate(found, start=1):
        if item["kind"] is not None:
            item["kind"] = item["kind"].lower()
            if item["kind"] not in backlog.KINDS:
                raise ValidationError(
                    f"findings[{n}].kind must be one of {', '.join(backlog.KINDS)}."
                )
    return found


def parse_questions(value: Any) -> list[dict]:
    return _items(value, QUESTION, _QUESTION_FIELDS, ("text", "default"))


def parse_kind(value: Any) -> str:
    text = str(value or FINDING).strip().lower()
    if text not in KINDS:
        raise ValidationError(f"kind must be {' or '.join(KINDS)}, got {value!r}.")
    return text


def item_line(item: ReportItem) -> str:
    """`- report #12 finding 1 on acme-shop-3: Cart total rounds down · bug` or
    `- report #12 question 1 on acme-shop-3: Keep the old route? (default: keep it)`."""
    head = f"- report #{item.report_id} {item.kind} {item.n} on {item.task_key}"
    text = one_line(item.title, LINE_TEXT_MAX) or ""
    if item.kind == QUESTION:
        default = one_line(item.question_default, LINE_TEXT_MAX) or ""
        return f"{head}: {text} (default: {default})"
    return f"{head}: {text}" + (f" · {item.item_kind}" if item.item_kind else "")
