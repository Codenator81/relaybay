"""Ctrl+K: fuzzy matching of what the owner types against keys, titles and names."""

from dataclasses import dataclass

QUERY_MAX = 200
RESULTS_MAX = 20
EMPTY_QUERY_TASKS = 15
TASKS_SCANNED = 2000
SESSIONS_SCANNED = 500


def _token_score(token: str, text: str) -> int | None:
    """Higher is better: whole text, start, start of a word, anywhere, then the letters in order
    with as few gaps as possible. None when the letters are not all there in order."""
    if token == text:
        return 1000
    at = text.find(token)
    if at == 0:
        return 800
    if at > 0:
        return 600 if not text[at - 1].isalnum() else 500 - min(at, 100)
    position, gaps, last = 0, 0, -1
    for char in token:
        found = text.find(char, position)
        if found < 0:
            return None
        if last >= 0:
            gaps += found - last - 1
        last, position = found, found + 1
    return max(1, 200 - gaps)


def fuzzy_score(query: str, *texts: str | None) -> int | None:
    """Every word of `query` has to match one of `texts`: the sum of the words' best scores, or
    None when a word matches none of them."""
    folded = [t.casefold() for t in texts if t]
    total = 0
    for token in query.casefold().split():
        scores = [s for s in (_token_score(token, t) for t in folded) if s is not None]
        if not scores:
            return None
        total += max(scores)
    return total


@dataclass(frozen=True, slots=True)
class SwitcherItem:
    kind: str  # task, project or session
    title: str
    detail: str
    url: str
    key: str | None = None
