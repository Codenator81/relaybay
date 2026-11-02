from datetime import datetime, timedelta

from relaybay.domain.models import Task
from relaybay.domain.stages import Stage

DEFAULT_DONE_WINDOW_DAYS = 7


def is_archived(
    task: Task, now: datetime, window_days: int, require_tracker_log: bool = False
) -> bool:
    """A done task closed more than `window_days` ago. With `require_tracker_log`, a task not
    marked as logged in the external tracker is never archived: it stays until someone marks it."""
    if task.stage != Stage.DONE or task.closed_at is None:
        return False
    if require_tracker_log and not task.tracker_logged:
        return False
    return datetime.fromisoformat(task.closed_at) < now - timedelta(days=window_days)
