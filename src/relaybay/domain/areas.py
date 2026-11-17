"""Areas: what part of a project a card belongs to ("web shop", "mobile app", "api"). One area
per task or none, from a list per project that grows from use."""

from dataclasses import dataclass

AREA_NAME_MAX = 32
AREAS_MAX = 24
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
