import re

REPORT_STATES = ("inspected", "changed", "verified", "committed", "pushed", "blocked")
REPORT_SIZES = ("S", "M", "L")
SUMMARY_MAX = 200

# Display labels and tooltips for report badges; overridable via [reports.labels] / [reports.hints].
REPORT_LABELS = {
    "inspected": "Inspected",
    "changed": "Changed",
    "verified": "Verified",
    "committed": "Committed",
    "pushed": "Pushed",
    "blocked": "Blocked",
    "S": "S",
    "M": "M",
    "L": "L",
}
REPORT_HINTS = {
    "inspected": "Investigated, nothing changed",
    "changed": "Changed locally, not verified yet",
    "verified": "Changed and verified locally",
    "committed": "Committed, not pushed yet",
    "pushed": "Pushed to the remote",
    "blocked": "Blocked: needs a decision or access",
    "S": "Small",
    "M": "Medium",
    "L": "Large",
}

# "verified locally · M · gist": the state may carry trailing words, the separator is a middle dot.
_HEADER = re.compile(
    r"^\s*(?P<state>[A-Za-z]+)(?:\s+[A-Za-z]+)?\s*·\s*(?P<size>[SMLsml])\s*·\s*(?P<gist>.+)$"
)


def parse_report(body: str) -> tuple[str | None, str | None, str]:
    """Return (state, size, summary) from a report body.

    The first non-empty line is the summary. When it has the form `state · size · gist` with a
    known state and size, state and size are extracted and the gist becomes the summary.
    """
    first = next((line.strip() for line in body.splitlines() if line.strip()), "")
    first = first.lstrip("#").strip()
    state = size = None
    match = _HEADER.match(first)
    if match and match.group("state").lower() in REPORT_STATES:
        state = match.group("state").lower()
        size = match.group("size").upper()
        first = match.group("gist").strip()
    if len(first) > SUMMARY_MAX:
        first = first[: SUMMARY_MAX - 1].rstrip() + "…"
    return state, size, first
