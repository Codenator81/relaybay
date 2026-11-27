"""The editable task fields: one list that drives write validation, the `relaybay://schema/task`
resource and the "Editable fields" line of `task_get`, so a new field reaches all three."""

from dataclasses import dataclass

from relaybay.domain.areas import AREA_NAME_MAX, AREAS_MAX, CARD_AREAS_MAX
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
    kind: str  # title, stage, text, markdown, url, bool, integer, size, area, list
    description: str
    clear: str
    max_length: int | None = None

    @property
    def type(self) -> str:
        return {
            "bool": "boolean",
            "integer": "integer",
            "list": "list of strings",
            "area": "string or list of strings",
        }.get(self.kind, "string")


TASK_FIELDS: tuple[TaskField, ...] = (
    TaskField("title", "title", "Short task title.", "cannot be cleared", TITLE_MAX),
    TaskField(
        "stage",
        "stage",
        f"One of: {', '.join(STAGES)}. A change is a stage move.",
        "cannot be cleared",
    ),
    TaskField("stage_note", "text", "Short note next to the stage.", TEXT_CLEARS, TEXT_MAX),
    TaskField(
        "waiting_on",
        "text",
        "Only who acts next: a name or role, no text after it; what happens goes to next_step.",
        TEXT_CLEARS,
        TEXT_MAX,
    ),
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
        "description",
        "markdown",
        "What to do and why, short; description_add appends a paragraph without overwriting.",
        TEXT_CLEARS,
        MARKDOWN_MAX,
    ),
    TaskField(
        "progress",
        "markdown",
        "Running summary of everything done so far (rewritten, not appended).",
        TEXT_CLEARS,
        MARKDOWN_MAX,
    ),
    TaskField(
        "priority",
        "integer",
        "Higher lists first in task_list; default 0. On the board the owner's order of the"
        " cards decides instead.",
        "0 resets it",
    ),
    TaskField(
        "size",
        "size",
        "Task size: S, M or L. A report whose first line names a size sets it when the task has"
        " none. S tasks may go without an accepted plan.",
        TEXT_CLEARS,
    ),
    TaskField(
        "area",
        "area",
        "What parts of the project the task belongs to, by name (e.g. 'web shop'), matched"
        " without regard to case: one name, several as a list or comma-separated, in order"
        f" (the first is the card's first area), at most {CARD_AREAS_MAX} a card and"
        f" {AREA_NAME_MAX} characters a name. A list replaces the card's areas. A new name is"
        f" added to the project's areas (at most {AREAS_MAX}); project_snapshot lists them.",
        "'-', '', [] or null clears them",
    ),
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
        "True once the work log is entered in the tracker. New work clears it again: a report"
        " or a progress change on a card before live, a move back from live or done. A new"
        " tracker_ref starts as not logged unless tracker_logged is passed in the same write.",
        "false clears it",
    ),
    TaskField(
        "deploy_notes",
        "markdown",
        "What a deploy of this card needs: migration, feature flag, script, rollback. Writing it"
        " clears deploy_done unless deploy_done is passed in the same write.",
        TEXT_CLEARS,
        MARKDOWN_MAX,
    ),
    TaskField(
        "deploy_done",
        "bool",
        "True once the steps in deploy_notes have been carried out.",
        "false clears it",
    ),
    TaskField(
        "inspect_only",
        "bool",
        "True for a card that only reads: an analysis, a repro, a question answered by reading."
        " It needs no accepted plan before dev and may go straight to done; a report on it that"
        " says changed, committed or pushed gets a warning.",
        "false clears it",
    ),
    TaskField(
        "cancelled",
        "text",
        "Why the card was given up on; setting it stamps the time. The card stays in its stage"
        " (move it to done yourself).",
        "'-' clears it and its time",
        TEXT_MAX,
    ),
)

BY_NAME: dict[str, TaskField] = {f.name: f for f in TASK_FIELDS}
# `fields={"areas": ...}` is the same field as `area`.
AREAS_ALIAS = "areas"
FIELD_NAMES: tuple[str, ...] = tuple(BY_NAME)

# Not columns of the task: links to the project's repositories, to pull requests and to the cards
# blocking it, added or removed entry by entry in the same writes (task_update).
BLOCKED_BY = "blocked_by"
LINK_FIELDS: tuple[TaskField, ...] = (
    TaskField(
        "repos",
        "list",
        "Repositories of the task's project it touches, by name; '-name' removes one. An unknown"
        " name fails with the project's names (project_upsert(repos=...) sets them)."
        " 'name <absolute path>' also sets the folder (worktree) the task works in there;"
        " 'name' alone keeps it.",
        "'-name' removes one",
    ),
    TaskField(
        "prs",
        "list",
        "Pull request links (http:// or https:// only), optionally after a repository name:"
        " 'web https://host/acme/web/pull/12'. '-url' removes one. Without a name, a link whose"
        " path names a project repository is tied to it.",
        "'-url' removes one",
    ),
    TaskField(
        BLOCKED_BY,
        "list",
        "Cards this task waits for, by key, of any project on the board: 'acme-shop-12';"
        " '-key' removes one. A blocker blocks while it is in plan, dev, review or test; at live"
        " or done it no longer does. A card blocking itself or a cycle is refused.",
        "'-key' removes one",
    ),
)
LINK_NAMES: tuple[str, ...] = tuple(f.name for f in LINK_FIELDS)

# Not a column either: appends a paragraph to `description` instead of replacing it, so two
# writers do not lose each other's lines.
DESCRIPTION_ADD = "description_add"
APPEND_FIELDS: tuple[TaskField, ...] = (
    TaskField(
        DESCRIPTION_ADD,
        "markdown",
        "A paragraph appended to description (after a blank line) instead of overwriting it.",
        "nothing to clear",
        MARKDOWN_MAX,
    ),
)
APPEND_NAMES: tuple[str, ...] = tuple(f.name for f in APPEND_FIELDS)

# Not a field of the task either: where the calling session runs, stored on that session. Asked
# for at session start when the hook could not tell (design 0025).
SESSION_SURFACE = "session_surface"
SESSION_FIELDS: tuple[TaskField, ...] = (
    TaskField(
        SESSION_SURFACE,
        "surface",
        "Where you, the calling session, run: 'desktop' (the Claude desktop app) or 'terminal'."
        " Stored on your session, not on the task; pass hook_session so relaybay knows which"
        " session you are. Send it once when your session start asks for it.",
        TEXT_CLEARS,
    ),
)
SESSION_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in SESSION_FIELDS)
