"""Questions an agent asks the owner on a card: the rules of asking and the texts that carry the
owner's answer back. The owner answers only on the board; no MCP tool answers."""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from relaybay.domain.comments import COMMENT_MAX
from relaybay.domain.errors import ValidationError

KINDS = ("question", "look")
STATUSES = ("open", "answered", "defaulted", "withdrawn")
OPEN_MAX = 5  # open questions per card: an agent asks instead of working otherwise
TEXT_MAX = 2000
DEFAULT_MAX = 500
OPTION_MAX = 200
OPTIONS_MAX = 8
URL_MAX = 2000
ACCOUNT_MAX = 200
ANSWER_MAX = COMMENT_MAX
LOOK_OPTIONS = ("fine", "remarks")
# A non-blocking question without a deadline closes with its default after this.
DEFAULT_AFTER = timedelta(hours=1)
DEFAULT_AFTER_MIN = timedelta(minutes=1)
DEFAULT_AFTER_MAX = timedelta(days=30)
QUOTE_MAX = 300  # the question as quoted in the answer's line
CHECK_SECONDS = 15.0  # how often the server closes questions past their deadline
# A line that reads like the owner's answer: only an answer from the board may carry one.
_ANSWER_LINE_RE = re.compile(r"^\s*owner['\u2019]?s answer to question\b", re.I | re.M)
_DURATION_RE = re.compile(r"^(\d{1,6})\s*(m|min|minutes?|h|hours?|d|days?)$", re.IGNORECASE)


@dataclass(slots=True)
class Question:
    id: int
    task_id: int
    run_id: int | None
    session_id: int | None
    asked_by: str
    kind: str
    text: str
    options: list[str]
    default: str
    blocking: bool
    look_url: str | None
    look_account: str | None
    default_after: str | None
    asked_at: str
    status: str
    answer: str | None
    answered_by: str | None
    answered_at: str | None
    defaulted_at: str | None
    withdrawn_at: str | None
    withdrawn_by: str | None
    turn_id: int | None
    comment_id: int | None
    told_at: str | None
    delivered_at: str | None
    task_key: str = ""
    task_title: str = ""
    project_slug: str = ""
    turn_seq: int | None = None
    turn_outcome: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def answerable(self) -> bool:
        """Open, or closed with its default: a later answer is a change of course."""
        return self.status in ("open", "defaulted")

    @property
    def choices(self) -> list[str]:
        return list(LOOK_OPTIONS) if self.kind == "look" else self.options


@dataclass(slots=True)
class QuestionView:
    """The questions of a card, or of the Inbox, as the board shows them."""

    open: list[Question] = field(default_factory=list)
    closed: list[Question] = field(default_factory=list)


def _text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").strip()
    if text in ("", "-"):
        return None
    if len(text) > limit:
        raise ValidationError(f"{name} is longer than {limit} characters.")
    if "\x00" in text:
        raise ValidationError(f"{name} holds a NUL character.")
    return text


def clean_text(value: Any) -> str:
    text = _text(value, "The question", TEXT_MAX)
    if not text:
        raise ValidationError("The question is empty.")
    return text


def clean_default(value: Any) -> str:
    text = _text(value, "default", DEFAULT_MAX)
    if not text:
        raise ValidationError(
            "A question needs a default: what you do when the owner does not answer. Nothing asked."
        )
    return text


def clean_kind(value: Any) -> str:
    text = str(value or "").strip() or "question"
    if text in ("-",):
        text = "question"
    if text not in KINDS:
        raise ValidationError(f"kind must be question or look; got {text!r}.")
    return text


def clean_options(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value] if value.strip() not in ("", "-") else []
    if not isinstance(value, list):
        raise ValidationError("options must be a list of short answers.")
    options: list[str] = []
    for item in value:
        text = _text(item, "An option", OPTION_MAX)
        if text and text not in options:
            options.append(text)
    if len(options) > OPTIONS_MAX:
        raise ValidationError(f"A question takes at most {OPTIONS_MAX} options.")
    return options


def clean_url(value: Any) -> str:
    text = _text(value, "url", URL_MAX)
    if not text:
        raise ValidationError(
            "A look question needs the url of the screen to look at. Nothing asked."
        )
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc or any(c.isspace() for c in text):
        raise ValidationError(f"url must be an http or https address; got {text[:80]!r}.")
    if parts.username is not None or parts.password is not None:
        raise ValidationError(
            "url must not carry a user or password: name the test account in account."
        )
    return text


def clean_account(value: Any) -> str | None:
    text = _text(value, "account", ACCOUNT_MAX)
    if text is None:
        return None
    if "\n" in text:
        raise ValidationError("account is one line: the test account's sign-in name.")
    if ":" in text:
        raise ValidationError(
            "account names the test account only, never user:password. Nothing asked."
        )
    return text


def deadline(value: Any, blocking: bool, now: datetime) -> str | None:
    """`default_after` as an ISO time: a time with its zone, or a duration from now (30m, 2h,
    1d). A blocking question has none; a non-blocking one without it gets DEFAULT_AFTER."""
    text = _text(value, "default_after", 64)
    if blocking:
        if text is not None:
            raise ValidationError(
                "default_after is for non-blocking questions: a blocking one waits for the owner."
            )
        return None
    if text is None:
        at = now + DEFAULT_AFTER
    elif match := _DURATION_RE.match(text):
        number, unit = int(match.group(1)), match.group(2)[0].lower()
        at = now + {"m": timedelta(minutes=number), "h": timedelta(hours=number)}.get(
            unit, timedelta(days=number)
        )
    else:
        try:
            at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ValidationError(
                f"default_after is a time with its zone (2026-12-01T15:00:00Z) or a duration"
                f" (30m, 2h, 1d); got {text!r}."
            ) from None
        if at.tzinfo is None:
            raise ValidationError("default_after needs its time zone, e.g. 2026-12-01T15:00:00Z.")
    if at - now < DEFAULT_AFTER_MIN:
        raise ValidationError("default_after must be at least a minute from now.")
    if at - now > DEFAULT_AFTER_MAX:
        raise ValidationError("default_after may be at most 30 days from now.")
    return at.astimezone(UTC).isoformat(timespec="milliseconds")


def compose_answer(question: Question, choice: Any, text: Any) -> str:
    """The owner's answer from the board: an option, a text, or both (`option: text`). For a
    look question the remarks are the answer text."""
    picked = _text(choice, "The option", OPTION_MAX)
    written = _text(text, "The answer", ANSWER_MAX)
    if picked is not None and picked not in question.choices:
        raise ValidationError(f"{picked!r} is not an option of question {question.id}.")
    if question.kind == "look" and picked == "remarks":
        if not written:
            raise ValidationError("Write the remarks: they are the answer.")
        return written
    if picked and written:
        answer = f"{picked}: {written}"
        if len(answer) > ANSWER_MAX:
            raise ValidationError(f"The answer is longer than {ANSWER_MAX} characters.")
        return answer
    if picked or written:
        return picked or written or ""
    raise ValidationError("Pick an option or write an answer.")


def quoted(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= QUOTE_MAX else one[: QUOTE_MAX - 3].rstrip() + "..."


def answer_line(question: Question) -> str:
    """What the agent gets: the owner's answer verbatim after one line naming the question."""
    assert question.answer is not None
    head = f'Owner\'s answer to question {question.id} ("{quoted(question.text)}")'
    line = f"{head}: {question.answer}"
    if question.defaulted_at:
        line += (
            f"\nThis changes course: the question had closed with its default"
            f' ("{quoted(question.default)}") at {question.defaulted_at[:16].replace("T", " ")}'
            " UTC."
        )
    return line


def waiting_line(question: Question) -> str:
    """The hook line for a turn that runs while the owner's answer waits for it to end."""
    return (
        f'Owner\'s answer to question {question.id} ("{quoted(question.text)}") is waiting: it'
        " comes as your next turn when this one ends."
    )


def asked_hint(question: Question, in_run: bool) -> str:
    """What the asking agent does next, in question_ask's answer."""
    if question.blocking:
        then = (
            "it comes as your next turn"
            if in_run
            else "it comes as an owner comment through your hooks"
        )
        return f"Blocking: end your turn now; the owner answers on the board and {then}."
    at = (question.default_after or "")[:16].replace("T", " ")
    return (
        f'Go on with the default ("{quoted(question.default)}"); at {at} UTC the question closes'
        " with it. A later answer from the owner comes as a change of course."
    )


def check_no_answer_line(text: str, what: str) -> None:
    """Refuse a text not from the owner that holds a line the agent would read as the owner's
    answer to a question."""
    if _ANSWER_LINE_RE.search(text):
        raise ValidationError(
            f"{what} holds a line starting \"Owner's answer to question\": only the owner's"
            " answer on the board starts a turn with one. Nothing started."
        )
