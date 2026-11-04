"""Working rules: a fixed set of rule kinds as one structured JSON document.

Layers: built-in defaults ⊕ the current global version (first made from the `[rules]` table of
`config.toml`) ⊕ a project's current version, stored in `project_rules`. Each layer is a partial
document validated by `validate_rules`; `merge_rules` applies one on top of another. The merged
document is what agents read and what stage moves are checked against.
"""

import copy
import difflib
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from relaybay.domain.reports import REPORT_SIZES, REPORT_STATES, parse_report
from relaybay.domain.stages import STAGES
from relaybay.domain.task_fields import FIELD_NAMES, MARKDOWN_MAX

RULES_MODES = ("warn", "strict")
REPORT_SINCE_STAGE_CHANGE = "report_since_stage_change"
FIELD_PRESENT = "field_present"
PLAN_ACCEPTED = "plan_accepted"
REQUIRE_TRACKER_LOG = "require_tracker_log"
TRACKER_LOG_LABEL = "the work log entered in the tracker (tracker_logged)"
REQUIREMENT_KINDS = (REPORT_SINCE_STAGE_CHANGE, FIELD_PRESENT, PLAN_ACCEPTED, REQUIRE_TRACKER_LOG)
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


def check_patch_keys(patch: dict[str, Any], source: str) -> None:
    """Refuse a patch's unknown keys before it is merged: `null` would drop them from the result,
    so validating the result alone would let any text through as a stored key. The message does
    not repeat the key, which may be the very secret."""
    unknown = set(patch) - set(TOP_KEYS)
    if unknown:
        raise ValueError(f"{source}: unknown rules key; allowed: {', '.join(TOP_KEYS)}.")
    fmt = patch.get("report_format")
    if isinstance(fmt, dict) and set(fmt) - set(REPORT_FORMAT_KEYS):
        allowed = ", ".join(REPORT_FORMAT_KEYS)
        raise ValueError(f"{source}: report_format: unknown key; allowed: {allowed}.")
    reqs = patch.get("stage_requirements")
    if isinstance(reqs, dict) and set(reqs) - set(STAGES):
        allowed = ", ".join(STAGES)
        raise ValueError(f"{source}: stage_requirements: unknown stage; use one of: {allowed}.")


def stored_global_rules(body: Any, source: str) -> dict[str, Any]:
    """A stored global version as a whole document. Only the shape is filled in: a stage's
    requirements a new release added to the defaults do not creep into an old version."""
    clean = validate_rules(body, source)
    bare = {
        "report_format": DEFAULT_RULES["report_format"],
        "stage_requirements": {},
        "description_template": None,
        "guidance": None,
    }
    return merge_rules(bare, clean)


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
    says why a project's own override was ignored (the global rules apply instead).
    `require_tracker_log` is the config key: it makes the work-log mark a requirement of `done`
    without being part of the versioned document."""

    body: dict[str, Any]
    project: str | None = None
    mode: str = "warn"
    override_error: str | None = None
    global_version: int = 0
    project_version: int = 0
    require_tracker_log: bool = False

    @property
    def hash(self) -> str:
        # The mode is part of what an agent must know: warn → strict changes the hash.
        return rules_hash({**self.body, "rules_mode": self.mode})

    @property
    def uri(self) -> str:
        return f"relaybay://rules/{self.project}" if self.project else "relaybay://rules"

    @property
    def version(self) -> int:
        """Grows with every change of either layer: the sum of the two version numbers."""
        return self.global_version + self.project_version

    @property
    def version_text(self) -> str:
        if self.project is None:
            return f"Rules v{self.version} (global v{self.global_version})"
        return (
            f"Rules v{self.version} (global v{self.global_version},"
            f" project v{self.project_version})"
        )

    def requirements(self, stage: str) -> list[dict[str, Any]]:
        found = list(self.body["stage_requirements"].get(stage, []))
        if (
            self.require_tracker_log
            and stage == "done"
            and not any(r["kind"] == REQUIRE_TRACKER_LOG for r in found)
        ):
            found.append({"kind": REQUIRE_TRACKER_LOG})
        return found


def requirement_label(req: dict[str, Any]) -> str:
    if req["kind"] == REPORT_SINCE_STAGE_CHANGE:
        return "a report since the last stage change"
    if req["kind"] == PLAN_ACCEPTED:
        unless = req.get("unless_size") or []
        return "an accepted plan" + (
            f" (not needed for size {' or '.join(unless)})" if unless else ""
        )
    if req["kind"] == REQUIRE_TRACKER_LOG:
        return TRACKER_LOG_LABEL
    return f"the {req['field']} field"


def requirement_short(req: dict[str, Any]) -> str:
    if req["kind"] == PLAN_ACCEPTED:
        return "accepted plan"
    if req["kind"] == REQUIRE_TRACKER_LOG:
        return "tracker log"
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
        elif req["kind"] == REQUIRE_TRACKER_LOG:
            ok = bool(values.get("tracker_logged"))
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
    """`Rules v3 (global v2, project v1), hash 1a2b3c4d: report before test, progress before done
    (read relaybay://rules/x)`."""
    needs = [
        f"{requirement_short(req)} before {stage}"
        for stage in STAGES
        for req in rules.requirements(stage)
    ]
    head = f"{rules.version_text}, hash {rules.hash}" if rules.version else f"Rules {rules.hash}"
    line = f"{head}: {', '.join(needs) or 'no stage requirements'} (read {rules.uri})"
    if rules.override_error:
        line += f"; project override ignored: {rules.override_error}"
    return line


def report_format_problem(body: str, report_format: dict[str, Any]) -> str | None:
    """What is wrong with a report's first line under `report_format`; None when it fits. Only
    the standard `state · size · gist` has a structure to check: a custom `first_line` is free
    text for agents, and refusing against it would make every report fail."""
    if report_format["first_line"] != DEFAULT_RULES["report_format"]["first_line"]:
        return None
    state, size, _ = parse_report(body)
    wanted = (
        f"`{report_format['first_line']}` (state: {', '.join(report_format['states'])};"
        f" size: {', '.join(report_format['sizes'])})"
    )
    if state is None or size is None:
        return f"the first line of a report should be {wanted}."
    if state not in report_format["states"] or size not in report_format["sizes"]:
        return f"the first line of a report says {state} · {size}; it should be {wanted}."
    return None


def rules_markdown(rules: Rules) -> str:
    """The merged document as markdown, without the heading a caller puts on top."""
    body = rules.body
    report_format = body["report_format"]
    lines = [
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


def rules_diff(old: dict[str, Any], new: dict[str, Any]) -> str:
    """A unified diff of two rule documents as sorted, indented JSON."""

    def text(body: dict[str, Any]) -> list[str]:
        return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False).splitlines()

    return "\n".join(
        difflib.unified_diff(text(old), text(new), "before", "after", lineterm="", n=2)
    )


@dataclass(frozen=True, slots=True)
class RuleVersion:
    id: int
    scope: str
    number: int
    body: dict[str, Any]
    rendered: str
    author: str
    session_id: int | None
    reason: str
    diff: str
    created_at: str
    notice_open: bool = False
    body_error: str | None = None


@dataclass(frozen=True, slots=True)
class RuleProposal:
    id: int
    scope: str
    project_id: int | None
    patch: dict[str, Any]
    reason: str
    proposed_by: str
    session_id: int | None
    status: str
    version: int | None
    created_at: str
    decided_at: str | None
    decided_by: str | None
