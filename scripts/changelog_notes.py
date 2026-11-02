"""Print the CHANGELOG.md section of one version (the GitHub Release notes).

    python scripts/changelog_notes.py 0.1.0 > notes.md

Exits 1 when the version has no section or the section is empty.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def notes(text: str, version: str) -> str | None:
    """The body under `## [<version>]` up to the next `## ` heading, without the heading."""
    pattern = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)", re.M | re.S)
    match = pattern.search(text)
    if match is None:
        return None
    body = re.sub(r"^\[[^\]]+\]: \S+\s*$", "", match[1], flags=re.M)  # link definitions
    return body.strip() or None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: changelog_notes.py <version>", file=sys.stderr)
        return 2
    version = argv[0].removeprefix("v")
    body = notes(CHANGELOG.read_text(encoding="utf-8"), version)
    if body is None:
        print(f"CHANGELOG.md has no notes for {version}", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")  # the notes are UTF-8 on every OS
    sys.stdout.write(body + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
