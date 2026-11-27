"""Areas: what part of a project a card belongs to ("web shop", "mobile app", "api"). A card
carries a list of them in order (none to CARD_AREAS_MAX), from a list per project that grows from
use; the first is the card's first area."""

from collections.abc import Collection, Sequence
from dataclasses import dataclass

AREA_NAME_MAX = 32
AREAS_MAX = 24
CARD_AREAS_MAX = 8
# Palette keys, not CSS values: the board maps each to a pair of tokens per theme.
AREA_COLORS = ("blue", "green", "amber", "violet", "teal", "rose", "slate", "orange")
CLEARS = ("", "-")


@dataclass(slots=True)
class ProjectArea:
    id: int
    project_id: int
    name: str
    color: str
    position: int
    created_at: str
    tasks: int = 0  # how many tasks carry it, for the settings page


@dataclass(frozen=True, slots=True)
class CardArea:
    """One area as a card carries it."""

    id: int
    name: str
    color: str


def area_key(name: str) -> str:
    """The form two names are compared in: "Web shop " and "web shop" are one area."""
    return " ".join(name.split()).casefold()


def clean_area_name(value: str) -> str | None:
    """The name as stored: outer spaces trimmed, inner runs of spaces made one; None clears."""
    name = " ".join(value.split())
    if name in CLEARS:
        return None
    return name


def color_for(position: int) -> str:
    return AREA_COLORS[position % len(AREA_COLORS)]


def find_area(areas: list[ProjectArea], name: str) -> ProjectArea | None:
    key = area_key(name)
    return next((a for a in areas if area_key(a.name) == key), None)


def names_line(areas: list[ProjectArea]) -> str:
    return ", ".join(a.name for a in areas) if areas else "none yet"


def split_area_text(text: str, known_keys: Collection[str]) -> list[str]:
    """The names in one text, split on commas; adjacent parts that together form a known name
    stay one (an older name may hold a comma), the longest run first. Empty parts and clears
    drop out."""
    parts = text.split(",")
    names: list[str] = []
    at = 0
    while at < len(parts):
        end = next(
            (
                end
                for end in range(len(parts), at + 1, -1)
                if area_key(",".join(parts[at:end])) in known_keys
            ),
            at + 1,
        )
        if name := clean_area_name(",".join(parts[at:end])):
            names.append(name)
        at = end
    return names


def area_field(names: Sequence[str]) -> tuple[str, str | None]:
    """The label and text of a card's areas in plain output: `Area: web` for one, `Areas: web,
    docs` for several, (area, None) for none."""
    if len(names) > 1:
        return "areas", ", ".join(names)
    return "area", names[0] if names else None
