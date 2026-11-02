"""The editable task fields: one list that drives write validation, the `relaybay://schema/task`
resource and the "Editable fields" line of `task_get`, so a new field reaches all three."""

from dataclasses import dataclass

from relaybay.domain.stages import STAGES

TITLE_MAX = 200
TEXT_MAX = 2000
NAME_MAX = 100
MARKDOWN_MAX = 20000
TRACKER_REF_MAX = 100

TEXT_CLEARS = "'-', '' or null clears it"


@dataclass(frozen=True, slots=True)
class TaskField:
    name: str
    kind: str  # title, stage, text, markdown, url, bool, integer
    description: str
    clear: str
    max_length: int | None = None

    @property
    def type(self) -> str:
        return {"bool": "boolean", "integer": "integer"}.get(self.kind, "string")


TASK_FIELDS: tuple[TaskField, ...] = (
    TaskField("title", "title", "Short task title.", "cannot be cleared", TITLE_MAX),
    TaskField(
        "stage",
        "stage",
        f"One of: {', '.join(STAGES)}. A change is a stage move (same as task_set_stage).",
        "cannot be cleared",
    ),
    TaskField("stage_note", "text", "Short note next to the stage.", TEXT_CLEARS, TEXT_MAX),
    TaskField("waiting_on", "text", "Who acts next.", TEXT_CLEARS, TEXT_MAX),
    TaskField(
        "next_step",
        "text",
        "What happens next, one line. Writing it confirms it as current.",
        TEXT_CLEARS,
        TEXT_MAX,
    ),
    TaskField("branch", "text", "Git branch.", TEXT_CLEARS, TEXT_MAX),
    TaskField("doc_path", "text", "Path to a task document.", TEXT_CLEARS, TEXT_MAX),
    TaskField(
        "description", "markdown", "What to do and why, 1-5 sentences.", TEXT_CLEARS, MARKDOWN_MAX
    ),
    TaskField(
        "progress",
        "markdown",
        "Running summary of everything done so far (rewritten, not appended).",
        TEXT_CLEARS,
        MARKDOWN_MAX,
    ),
    TaskField("priority", "integer", "Higher sorts first; default 0.", "0 resets it"),
    TaskField(
        "tracker_ref",
        "text",
        "Issue key in the external tracker, e.g. 'ENG-12'.",
        TEXT_CLEARS,
        TRACKER_REF_MAX,
    ),
    TaskField(
        "tracker_name",
        "text",
        "Name of the external tracker, e.g. 'Linear'.",
        TEXT_CLEARS,
        NAME_MAX,
    ),
    TaskField(
        "tracker_url",
        "url",
        "Link to the issue (http:// or https:// only).",
        TEXT_CLEARS,
        TEXT_MAX,
    ),
    TaskField(
        "tracker_logged",
        "bool",
        "True once the work log is entered in the tracker. New work (a report, a progress"
        " change, a move back from live or done) clears it again. A new tracker_ref starts as"
        " not logged unless tracker_logged is passed in the same write.",
        "false clears it",
    ),
)

BY_NAME: dict[str, TaskField] = {f.name: f for f in TASK_FIELDS}
FIELD_NAMES: tuple[str, ...] = tuple(BY_NAME)
