import re
from dataclasses import dataclass

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


CHECK_WHERE_MAX = 200
CHECK_CLAIM_MAX = 300
CHECKS_MAX = 100
# `verified: <where>` on a claim line, at the line's start or after a space, "(", "[", a
# backtick or markdown emphasis (`**Verified:**`), so not inside a URL or a word like
# "unverified:"; never after "not ", "no ", "yet " or "never ". The
# `where` runs to the end of the line or to a closing bracket, a semicolon or a backtick, so
# "(verified: pytest on Windows)" reads "pytest on Windows"; a "(…)" inside it stays whole.
_CHECK = re.compile(
    r"(?:^|(?<=[\s(\[`*_]))(?<!not )(?<!no )(?<!yet )(?<!never )verified:[*_]*\s*"
    r"(?P<where>(?:[^()\]\n;`]|\([^()\n]*\))+)",
    re.IGNORECASE,
)
_NOT_A_PLACE = frozenset({"true", "false", "yes", "no", "none", "-", "n/a"})
_FENCE = re.compile(r"^\s*(```|~~~)")
_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|``")


@dataclass(frozen=True, slots=True)
class ReportCheck:
    line: int  # 1-based line of the body
    where: str
    claim: str


def parse_checks(body: str) -> list[ReportCheck]:
    """The `verified: <where>` marks of a report body, in order, outside fenced code blocks; an
    empty `where`, or a value such as `true`, is no mark."""
    found: list[ReportCheck] = []
    fenced = False
    for number, line in enumerate(body.splitlines(), start=1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        for match in _CHECK.finditer(line):
            where = match.group("where").strip(" *_").rstrip(".,").strip(" *_")
            if not where or where.lower() in _NOT_A_PLACE:
                continue
            claim = _EMPTY_BRACKETS.sub("", line[: match.start()] + line[match.end() :]).strip()
            claim = claim.strip("-*()[]`;:, ").strip() or line.strip()
            found.append(
                ReportCheck(number, _cut(where, CHECK_WHERE_MAX), _cut(claim, CHECK_CLAIM_MAX))
            )
            if len(found) >= CHECKS_MAX:
                return found
    return found


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
