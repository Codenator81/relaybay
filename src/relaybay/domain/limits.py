"""The brake and token budgets: what they are, when a budget crosses a threshold and the texts
that reach a session. Pure: the service reads the numbers and stores the marks."""

from dataclasses import dataclass

BRAKE_SCOPES = ("all", "project", "session", "task")
BUDGET_SCOPES = ("all", "project", "task")
BUDGET_PERIODS = ("day", "total")
BUDGET_ACTIONS = ("warn", "stop")
SOURCE_OWNER, SOURCE_COORDINATOR, SOURCE_BUDGET = "owner", "coordinator", "budget"
WARNED, EXCEEDED = "warned", "exceeded"
WARN_PERCENT = 80
TOTAL_KEY = "total"
REASON_MAX = 500
# Pass under the brake besides relaybay's own: without it a client that defers MCP schemas could
# never load the tools it reports with. It has no side effect.
FREE_TOOLS = ("ToolSearch",)
DEFAULT_BOARD_REASON = "Stopped by the owner on the board"
# Who released a daily budget's brake when the new UTC day did it.
NEW_DAY = "new day"
# While a budget or a concurrency cap exists, the usage loop looks at least this often.
CHECK_SECONDS = 60.0
# A session counts as running while `working` with activity in this window; also the shortest
# time between two `concurrency.over` events.
CONCURRENCY_MINUTES = 10


@dataclass(frozen=True, slots=True)
class Brake:
    id: int
    scope: str
    project_id: int | None
    project_slug: str | None
    session_id: int | None
    session_name: str | None
    task_id: int | None
    task_key: str | None
    reason: str
    source: str
    budget_id: int | None
    created_at: str
    created_by: str
    released_at: str | None = None
    released_by: str | None = None

    @property
    def target(self) -> str:
        if self.scope == "project":
            return f"project {self.project_slug}"
        if self.scope == "session":
            return f"session {self.session_name or '(removed)'}"
        if self.scope == "task":
            return f"task {self.task_key}"
        return "all agents"


@dataclass(frozen=True, slots=True)
class TokenBudget:
    id: int
    scope: str
    project_id: int | None
    project_slug: str | None
    task_id: int | None
    task_key: str | None
    period: str
    max_tokens: int
    action: str
    updated_at: str
    updated_by: str

    @property
    def target(self) -> str:
        if self.scope == "project":
            return f"project {self.project_slug}"
        if self.scope == "task":
            return f"task {self.task_key}"
        return "all projects"


@dataclass(frozen=True, slots=True)
class BudgetState:
    """A budget with what its period has used so far. `level` is the threshold marked in this
    period; it stays when the file behind the numbers shrinks."""

    budget: TokenBudget
    used: int
    level: str | None

    @property
    def percent(self) -> int:
        return self.used * 100 // self.budget.max_tokens

    @property
    def bar_value(self) -> int:
        return min(self.used, self.budget.max_tokens)


def period_key(period: str, today: str) -> str:
    return today if period == "day" else TOTAL_KEY


def level_for(used: int, max_tokens: int) -> str | None:
    if used >= max_tokens:
        return EXCEEDED
    return WARNED if used * 100 >= max_tokens * WARN_PERCENT else None


def period_text(period: str) -> str:
    return "this UTC day" if period == "day" else "in total"


def _brake_head(brake: Brake) -> str:
    return f"relaybay brake on ({brake.target}), set by {brake.created_by}: {brake.reason}."


def deny_reason(brake: Brake) -> str:
    """Why a tool call is refused and the session stops."""
    return (
        f"{_brake_head(brake)} Stop work: tool calls other than relaybay's are refused until the"
        " owner releases the brake on the board."
    )


def prompt_line(brake: Brake) -> str:
    """The owner's prompt goes through; the agent learns why its tools will not."""
    return (
        f"{_brake_head(brake)} Tool calls other than relaybay's are refused until the owner"
        " releases the brake on the board: say so in your answer."
    )


def budget_reason(state: BudgetState) -> str:
    """The reason on a brake a `stop` budget set."""
    budget = state.budget
    return (
        f"token budget of {budget.target} exceeded: {state.used:,} of {budget.max_tokens:,} tokens"
        f" {period_text(budget.period)}"
    )


def budget_line(state: BudgetState) -> str:
    """One context line per session about a budget that crossed a threshold."""
    budget = state.budget
    then = (
        "The brake stops tool calls at 100%."
        if budget.action == "stop"
        else "It only warns and stops nothing."
    )
    return (
        f"relaybay token budget of {budget.target}: {state.used:,} of {budget.max_tokens:,} tokens"
        f" {period_text(budget.period)} ({state.percent}%). {then} Tell the owner when you report."
    )
