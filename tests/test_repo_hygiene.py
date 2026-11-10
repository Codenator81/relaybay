"""The repository is English-only: no Cyrillic in any tracked text file."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CYRILLIC = re.compile(r"[\u0400-\u04ff]")
SKIP_DIRS = {".venv", ".dev-data", ".claude", "__pycache__"}
SKIP_FILES = {"CLAUDE.md", "CLAUDE.local.md"}
SUFFIXES = {".py", ".md", ".html", ".css", ".js", ".sql", ".toml", ".txt", ".yml", ".yaml"}


def _git_files() -> list[Path] | None:
    """Tracked and new (not ignored) files, or None when git or the repository is missing."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return [ROOT / name for name in out.decode("utf-8").split("\0") if name]


def _globbed_files() -> list[Path]:
    found = list(ROOT.glob("*.md"))
    for folder in ("src", "docs", "tests"):
        found.extend((ROOT / folder).rglob("*"))
    return found


def files() -> list[Path]:
    candidates = _git_files()
    if candidates is None:
        candidates = _globbed_files()
    return [
        path
        for path in candidates
        if path.is_file()
        and path.suffix in SUFFIXES
        and path.name not in SKIP_FILES
        and not SKIP_DIRS.intersection(path.relative_to(ROOT).parts)
    ]


def test_no_cyrillic_in_tracked_text() -> None:
    hits = []
    for path in files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CYRILLIC.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert len(files()) > 20
    assert not hits, "Cyrillic text found: " + ", ".join(hits)


# An HTML parser reads everything after these tags as raw text up to the closing tag, so one of them
# in a markdown page, in backticks too, can swallow the rest of the page in the docs build (it did,
# with a newer Python than CI's). Write the name another way: "a title placeholder", `<heading>`.
RAW_TEXT_TAGS = re.compile(
    r"<(title|textarea|script|style|xmp|plaintext|iframe|noembed|noframes)\b[^>]*>", re.IGNORECASE
)


def test_no_raw_text_tags_in_markdown() -> None:
    pages = [path for path in files() if path.suffix == ".md"]
    hits = []
    for path in pages:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if RAW_TEXT_TAGS.search(line):
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{number}")
    assert len(pages) > 10
    assert not hits, "raw-text HTML tags in markdown: " + ", ".join(hits)
