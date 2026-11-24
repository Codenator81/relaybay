"""`waiting_on` in coordinator mode: only who acts next, as one of a few fixed values, so the
owner's Inbox, the coordinator's triage and the board's filters read the same word. The owner is
the first of the server's `owner_names` (what the Inbox matches); the others are `coordinator`,
`session`, `ci` and `customer:<name>`. Anything else stays as written: this tidies, it never
refuses a write."""

import re

COORDINATOR = "coordinator"
SESSION = "session"
CI = "ci"
CUSTOMER = "customer"

# "owner" in Russian, as owners type it; written as escapes because sources are English-only.
OWNER_WORDS = ("owner", "\u0432\u043b\u0430\u0434\u0435\u043b\u0435\u0446")
COORDINATOR_WORDS = (COORDINATOR, "master")
SESSION_WORDS = (SESSION, "agent", "developer", "reviewer", "dev")

# `who: text`, `who (text)`, `who - text` with an em dash; the who itself holds none of these marks.
_SPLIT = re.compile(
    r"(?P<who>[^:(\u2014]+?)\s*(?::(?P<colon>.*)|\((?P<paren>.*)\)|\u2014(?P<dash>.*))", re.S
)
_CUSTOMER = re.compile(r"customer\s*:\s*(?P<rest>.+)", re.S | re.I)


def _split(text: str) -> tuple[str, str]:
    """The who and the text after it ('' when there is none)."""
    found = _SPLIT.fullmatch(text)
    if found is None:
        return text, ""
    rest = found["colon"] if found["colon"] is not None else found["paren"] or found["dash"] or ""
    return found["who"].strip(), rest.strip()


def _canonical(who: str, owner_names: list[str], coordinator_names: list[str]) -> str | None:
    word = who.casefold()
    owners = {n.strip().casefold() for n in owner_names if n.strip()} | set(OWNER_WORDS)
    if word in owners:
        first = next((n.strip() for n in owner_names if n.strip()), OWNER_WORDS[0])
        return first
    coordinators = {n.strip().casefold() for n in coordinator_names if n.strip()}
    if word in coordinators | set(COORDINATOR_WORDS):
        return COORDINATOR
    if word in SESSION_WORDS:
        return SESSION
    if word == CI:
        return CI
    return None


def normalise(
    value: str | None,
    *,
    owner_names: list[str],
    coordinator_names: list[str],
    keep_rest: bool = True,
) -> tuple[str | None, str | None, str | None]:
    """(who, moved_text, note): the value to save, the text after the who for next_step (None
    when there is none or `keep_rest` is false: the caller's next_step is taken) and the line
    the answer gets when the saved value differs from the given one."""
    given = (value or "").strip()
    if not given:
        return value, None, None
    who: str | None
    customer = _CUSTOMER.fullmatch(given)
    if customer is not None:
        name, rest = _split(customer["rest"].strip())
        who = f"{CUSTOMER}:{name}" if name else None
    else:
        who = _canonical(given, owner_names, coordinator_names)
        rest = ""
        if who is None:
            head, rest = _split(given)
            who = _canonical(head, owner_names, coordinator_names) if head != given else None
    if who is None:
        return value, None, None
    moved = rest if rest and keep_rest else None
    if who == given:
        return who, None, None
    note = f'waiting_on: "{given}" saved as "{who}"'
    if moved:
        note += " \u2014 the rest went to next_step"
    elif rest:
        note += " \u2014 the rest was not kept: the card already has a next_step"
    return who, moved, note
