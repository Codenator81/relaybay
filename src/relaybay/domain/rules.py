"""Working rules: a fixed set of rule kinds as one structured JSON document.

Layers: built-in defaults ⊕ the global `[rules]` table of `config.toml` ⊕ a project's row in
`project_rules`. Each layer is a partial document validated by `validate_rules`; `merge_rules`
applies one on top of another. The merged document is what agents read and what stage moves are
checked against.
"""

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from relaybay.domain.reports import REPORT_SIZES, REPORT_STATES
from relaybay.domain.stages import STAGES
from relaybay.domain.task_fields import FIELD_NAMES, MARKDOWN_MAX

RULES_MODES = ("warn", "strict")
REPORT_SINCE_STAGE_CHANGE = "report_since_stage_change"
FIELD_PRESENT = "field_present"
PLAN_ACCEPTED = "plan_accepted"
REQUIREMENT_KINDS = (REPORT_SINCE_STAGE_CHANGE, FIELD_PRESENT, PLAN_ACCEPTED)
TOP_KEYS = ("report_format", "stage_requirements", "description_template", "guidance")
REPORT_FORMAT_KEYS = ("first_line", "states", "sizes")

DEFAULT_RULES: dict[str, Any] = {
    "report_format": {
        "first_line": "state · size · gist",
        "states": list(REPORT_STATES),
        "sizes": list(REPORT_SIZES),
    },
    "stage_requirements": {
        "dev": [{"kind": PLAN_ACCEPTED, "unless_size": ["S"]}],
        "test": [{"kind": REPORT_SINCE_STAGE_CHANGE}],
        "done": [{"kind": FIELD_PRESENT, "field": "progress"}],
    },
    "description_template": None,
    "guidance": (
        "Report at the end of every stage. Keep `progress` a current summary of everything done"
        " so far, and `next_step` / `waiting_on` true after every stage change."
    ),
}


def _strings(value: Any, where: str, allowed: tuple[str, ...]) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{where} must be a non-empty list of strings.")
    unknown = [v for v in value if v not in allowed]
    if unknown:
        raise ValueError(
            f"{where}: unknown value(s) {', '.join(unknown)}; allowed: {', '.join(allowed)}."
        )
    return list(dict.fromkeys(value))


def _requirement(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a table/object with a 'kind'.")
    kind = value.get("kind")
    if kind not in REQUIREMENT_KINDS:
        raise ValueError(f"{where}.kind must be one of: {', '.join(REQUIREMENT_KINDS)}.")
    allowed = {FIELD_PRESENT: {"kind", "field"}, PLAN_ACCEPTED: {"kind", "unless_size"}}
    extra = set(value) - allowed.get(kind, {"kind"})
    if extra:
        raise ValueError(f"{where}: unknown key(s) for {kind}: {', '.join(sorted(extra))}.")
    if kind == PLAN_ACCEPTED:
        sizes = value.get("unless_size", [])
        if not isinstance(sizes, list) or not all(isinstance(v, str) for v in sizes):
            raise ValueError(f"{where}.unless_size must be a list of sizes (may be empty).")
        unknown = [v for v in sizes if v not in REPORT_SIZES]
        if unknown:
            raise ValueError(
                f"{where}.unless_size: unknown size(s) {', '.join(unknown)};"
                f" allowed: {', '.join(REPORT_SIZES)}."
            )
        return {"kind": kind, "unless_size": list(dict.fromkeys(sizes))}
    if kind == FIELD_PRESENT:
        field = value.get("field")
        if field not in FIELD_NAMES:
            raise ValueError(f"{where}.field must be a task field: {', '.join(FIELD_NAMES)}.")
        return {"kind": kind, "field": field}
    return {"kind": kind}


def validate_rules(data: Any, source: str) -> dict[str, Any]:
    """A partial rules document, checked and normalized; errors name `source` and the key."""
    if not isinstance(data, dict):
        raise ValueError(f"{source}: rules must be a table/object.")
    unknown = set(data) - set(TOP_KEYS)
    if unknown:
        raise ValueError(
            f"{source}: unknown rules key(s) {', '.join(sorted(unknown))};"
            f" allowed: {', '.join(TOP_KEYS)}."
        )
    out: dict[str, Any] = {}
    if "report_format" in data:
        fmt = data["report_format"]
        where = f"{source}: report_format"
        if not isinstance(fmt, dict):
            raise ValueError(f"{where} must be a table/object.")
        extra = set(fmt) - set(REPORT_FORMAT_KEYS)
        if extra:
            raise ValueError(f"{where}: unknown key(s) {', '.join(sorted(extra))}.")
        clean: dict[str, Any] = {}
        if "first_line" in fmt:
            if not isinstance(fmt["first_line"], str) or not fmt["first_line"].strip():
                raise ValueError(f"{where}.first_line must be a non-empty string.")
            clean["first_line"] = fmt["first_line"].strip()
        if "states" in fmt:
            clean["states"] = _strings(fmt["states"], f"{where}.states", REPORT_STATES)
        if "sizes" in fmt:
            clean["sizes"] = _strings(fmt["sizes"], f"{where}.sizes", REPORT_SIZES)
        out["report_format"] = clean
    if "stage_requirements" in data:
        reqs = data["stage_requirements"]
        where = f"{source}: stage_requirements"
        if not isinstance(reqs, dict):
            raise ValueError(f"{where} must be a table/object keyed by stage.")
        clean_reqs: dict[str, list[dict[str, Any]]] = {}
        for stage, items in reqs.items():
            if stage not in STAGES:
                allowed = ", ".join(STAGES)
                raise ValueError(f"{where}: unknown stage {stage!r}; use one of: {allowed}.")
            if not isinstance(items, list):
                raise ValueError(f"{where}.{stage} must be a list (empty drops the requirements).")
            clean_reqs[stage] = [
                _requirement(item, f"{where}.{stage}[{i}]") for i, item in enumerate(items)
            ]
        out["stage_requirements"] = clean_reqs
    for key in ("description_template", "guidance"):
        if key in data:
            value = data[key]
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{source}: {key} must be a string.")
            if value is not None and len(value) > MARKDOWN_MAX:
                raise ValueError(f"{source}: {key} is longer than {MARKDOWN_MAX} characters.")
            out[key] = (value or "").strip() or None
    return out


def merge_rules(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """`override` on top of `base`: report_format per key, stage_requirements per stage (a
    stage's list replaces the base list), description_template and guidance replace."""
    merged = copy.deepcopy(base)
    merged["report_format"] = {**base["report_format"], **override.get("report_format", {})}
    merged["stage_requirements"] = {
        **base["stage_requirements"],
        **copy.deepcopy(override.get("stage_requirements", {})),
    }
    for key in ("description_template", "guidance"):
        if key in override:
            merged[key] = override[key]
    return merged


def merge_project_rules(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Like `merge_rules`, but a project's guidance is appended to the global guidance."""
    merged = merge_rules(base, {k: v for k, v in override.items() if k != "guidance"})
    parts = [p for p in (base.get("guidance"), override.get("guidance")) if p]
    merged["guidance"] = "\n\n".join(parts) or None
    return merged


def rules_hash(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True, slots=True)
class Rules:
    """The merged rules for the global scope (`project` None) or one project. `override_error`
    says why a project's own override was ignored (the global rules apply instead)."""

    body: dict[str, Any]
    project: str | None = None
    mode: str = "warn"
    override_error: str | None = None

    @property
    def hash(self) -> str:
        # The mode is part of what an agent must know: warn → strict changes the hash.
        return rules_hash({**self.body, "rules_mode": self.mode})

    @property
    def uri(self) -> str:
        return f"relaybay://rules/{self.project}" if self.project else "relaybay://rules"

    def requirements(self, stage: str) -> list[dict[str, Any]]:
        return list(self.body["stage_requirements"].get(stage, []))


def requirement_label(req: dict[str, Any]) -> str:
    if req["kind"] == REPORT_SINCE_STAGE_CHANGE:
        return "a report since the last stage change"
    if req["kind"] == PLAN_ACCEPTED:
        unless = req.get("unless_size") or []
        return "an accepted plan" + (
            f" (not needed for size {' or '.join(unless)})" if unless else ""
        )
    return f"the {req['field']} field"


def requirement_short(req: dict[str, Any]) -> str:
    if req["kind"] == PLAN_ACCEPTED:
        return "accepted plan"
    return "report" if req["kind"] == REPORT_SINCE_STAGE_CHANGE else req["field"]


def unmet(
    rules: Rules,
    stage: str,
    *,
    report_since_stage_change: bool,
    values: dict[str, Any],
    plan_status: str | None = None,
) -> list[dict[str, Any]]:
    """Requirements of moving to `stage` that the task does not meet; `values` are the task's
    fields as they will be after the write. A task of unknown size needs a plan."""
    missing = []
    for req in rules.requirements(stage):
        if req["kind"] == REPORT_SINCE_STAGE_CHANGE:
            ok = report_since_stage_change
        elif req["kind"] == PLAN_ACCEPTED:
            ok = plan_status == "accepted" or values.get("size") in req.get("unless_size", [])
        else:
            value = values.get(req["field"])
            ok = bool(value.strip()) if isinstance(value, str) else bool(value)
        if not ok:
            missing.append(req)
    return missing


def warning_text(stage: str, req: dict[str, Any], plan_status: str | None = None) -> str:
    text = f"Warning (rules): moving to {stage} usually needs {requirement_label(req)}."
    if req["kind"] == PLAN_ACCEPTED:
        if plan_status == "draft":
            text += (
                " The plan is not accepted yet: the owner accepts it on the board, the"
                " coordinator with plan_accept."
            )
        else:
            text += " Write it with plan_set (2-5 steps), then ask for acceptance."
    return text


def refusal_text(stage: str, missing: list[dict[str, Any]]) -> str:
    needs = " and ".join(requirement_label(r) for r in missing)
    return f"Rules: moving to {stage} requires {needs}. Nothing changed."


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
