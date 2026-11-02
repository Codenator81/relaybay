"""Comments from the board to agents: limits and the text hooks hand to the agent."""

from relaybay.domain.models import Comment

COMMENT_MAX = 4000
# Claude Code moves `additionalContext` over 10,000 characters into a file; stay well below.
CONTEXT_MAX = 8000
# relaybay's own cap on Stop blocks per turn; Claude Code's own cap is 8 continuations.
STOP_BLOCK_CAP = 2
# How many Stop blocks one comment can cause in all, across turns, once delivered to a session.
STOP_COMMENT_CAP = 2
ACK_HINT = (
    "Handle it like a message from the user, then reply with comment_add(task, body,"
    " in_reply_to=<id>) or acknowledge it with comment_ack(comment_id=<id>)."
)


def comment_line(comment: Comment) -> str:
    return f"Owner comment on {comment.task_key} (#{comment.id}): {comment.body_md}"


def fit(comments: list[Comment], budget: int = CONTEXT_MAX) -> list[Comment]:
    """The oldest comments whose lines fit in `budget` characters; always at least one."""
    chosen, used = [], 0
    for comment in comments:
        size = len(comment_line(comment)) + 1
        if chosen and used + size > budget:
            break
        chosen.append(comment)
        used += size
    return chosen


def _more(shown: list[Comment], total: int) -> list[str]:
    left = total - len(shown)
    return [f"{left} more unread owner comment(s): call comments_list."] if left > 0 else []


def delivery_text(shown: list[Comment], total: int) -> str:
    """additionalContext of PreToolUse / PostToolUse / UserPromptSubmit, and the wake-up text."""
    return "\n".join([*map(comment_line, shown), *_more(shown, total), ACK_HINT])


def stop_reason(shown: list[Comment], total: int) -> str:
    return "\n".join(
        [
            "relaybay: the owner left comments you have not acknowledged yet. Handle them before"
            " you finish.",
            *map(comment_line, shown),
            *_more(shown, total),
            ACK_HINT,
        ]
    )


def unread_line(count: int) -> str:
    return (
        f"You have {count} unread owner comment(s) on your relaybay tasks: call comments_list,"
        " then comment_ack or comment_add."
    )
