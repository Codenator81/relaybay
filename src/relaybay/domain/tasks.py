from datetime import datetime, timedelta

from relaybay.domain.models import Task
from relaybay.domain.stages import Stage

DEFAULT_DONE_WINDOW_DAYS = 7


def is_archived(
    task: Task, now: datetime, window_days: int, require_tracker_log: bool = False
) -> bool:
    """A done task sent to the archive by hand, or closed more than `window_days` ago. With
    `require_tracker_log`, a task not marked as logged in the external tracker is never archived
    by age: it stays until someone marks it (the hand mark does not wait for it). "Back from the
    archive" starts the window again: it counts from the later of closed_at and unarchived_at."""
    if task.stage != Stage.DONE or task.closed_at is None:
        return False
    if task.archived_at is not None:
        return True
    if require_tracker_log and not task.tracker_logged:
        return False
    since = max(task.closed_at, task.unarchived_at or "")
    return datetime.fromisoformat(since) < now - timedelta(days=window_days)
