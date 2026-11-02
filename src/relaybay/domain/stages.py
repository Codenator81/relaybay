from enum import StrEnum

from relaybay.domain.errors import ValidationError


class Stage(StrEnum):
    PLAN = "plan"
    DEV = "dev"
    REVIEW = "review"
    TEST = "test"
    LIVE = "live"
    DONE = "done"


STAGES: tuple[Stage, ...] = tuple(Stage)
OPEN_STAGES: tuple[Stage, ...] = tuple(s for s in Stage if s is not Stage.DONE)
# Stages in which the owner accepts the work step by step (the Review queue, the notification).
ACCEPTANCE_STAGES: tuple[Stage, ...] = (Stage.REVIEW, Stage.TEST)
# The board shows six columns on two pages.
BOARD_PAGES: tuple[tuple[Stage, ...], ...] = (
    (Stage.PLAN, Stage.DEV, Stage.REVIEW, Stage.TEST),
    (Stage.LIVE, Stage.DONE),
)


def parse_stage(value: str) -> Stage:
    """Any transition is allowed; only the value itself is validated."""
    try:
        return Stage(value.strip().lower())
    except ValueError:
        allowed = ", ".join(STAGES)
        raise ValidationError(f"Unknown stage {value!r}; use one of: {allowed}.") from None
