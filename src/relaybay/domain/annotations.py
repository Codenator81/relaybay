"""Annotations: markup on a screenshot in fractions of the image size, numbered pins with a
thread, and the owner comment that carries them to the agent."""

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Any

from relaybay.domain.comments import COMMENT_MAX
from relaybay.domain.errors import ValidationError

KINDS = ("rect", "arrow", "freehand", "pin")
# The viewer's toolbar is rendered from this list.
COLOURS = ("#e03131", "#f59f00", "#2f9e44", "#1971c2", "#212529")
DEFAULT_STROKE = 0.004  # a fraction of the image's width
STROKE_MAX = 0.05
TOLERANCE = 0.01  # a pointer released just past the edge of the image
BODY_MAX = 2_000
ANNOTATIONS_MAX = 200  # per screenshot
FREEHAND_POINTS_IN_MAX = 5_000
FREEHAND_POINTS_MAX = 500
SIMPLIFY_TOLERANCE = 0.001
COMMENT_PIN_TEXT_MAX = 300
# From Claude's vision guide: 5 MB of base64 per image on some platforms, so 3.5 MB of file, and
# 8000x8000 px at most per image (a margin below it). Whether every other MCP client holds to the
# same limits is unverified. An answer of review_get holds at most two full images.
IMAGE_TOOL_MAX_BYTES = 3_500_000
IMAGE_TOOL_MAX_SIDE = 7_990
REVIEW_IMAGES_MAX_BYTES = 2 * IMAGE_TOOL_MAX_BYTES
PNG_HEADER_BYTES = 24


@dataclass(slots=True)
class AnnotationReply:
    id: int
    annotation_id: int
    author_kind: str
    author: str
    body_md: str
    created_at: str


@dataclass(slots=True)
class Annotation:
    id: int
    attachment_id: int
    kind: str
    colour: str
    stroke_width: float
    geometry: dict[str, Any]
    pin_number: int | None
    body_md: str | None
    created_by: str
    created_at: str
    updated_at: str
    deleted_at: str | None
    replies: list[AnnotationReply] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "colour": self.colour,
            "stroke_width": self.stroke_width,
            "geometry": self.geometry,
            "pin_number": self.pin_number,
            "body": self.body_md,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "replies": [
                {
                    "id": r.id,
                    "author_kind": r.author_kind,
                    "author": r.author,
                    "body": r.body_md,
                    "created_at": r.created_at,
                }
                for r in self.replies
            ],
        }


def _fraction(value: Any, name: str) -> float:
    """A coordinate: a finite number in 0..1 give or take TOLERANCE, held to 0..1."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError(f"{name} must be a number.")
    # Compared before any float(): a JSON integer may be too large for one (OverflowError).
    if value != value or value < -TOLERANCE or value > 1 + TOLERANCE:
        raise ValidationError(f"{name} must be between 0 and 1 (a fraction of the image size).")
    return round(min(1.0, max(0.0, float(value))), 5)


def _fields(value: Any, names: tuple[str, ...], kind: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValidationError(f"The geometry of a {kind} has exactly: {', '.join(names)}.")
    return {name: _fraction(value[name], name) for name in names}


def _distance(point: list[float], a: list[float], b: list[float]) -> float:
    """From `point` to the line through `a` and `b`."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if not length:
        return math.hypot(point[0] - a[0], point[1] - a[1])
    return abs(dy * (point[0] - a[0]) - dx * (point[1] - a[1])) / length


def thin(points: list[list[float]], limit: int = FREEHAND_POINTS_MAX) -> list[list[float]]:
    """Down to `limit` points in linear time, along the line: each even slice keeps its first point
    and the one farthest from its chord, so a fine zigzag keeps its height (every n-th point could
    fall on one side of it only)."""
    if len(points) <= limit:
        return points
    slices = (limit - 2) // 2
    inner = len(points) - 2
    kept = [points[0]]
    for i in range(slices):
        start = 1 + i * inner // slices
        end = 1 + (i + 1) * inner // slices
        part = points[start:end]
        far = max(part, key=lambda p: _distance(p, part[0], part[-1]))
        kept.append(part[0])
        if far is not part[0]:
            kept.append(far)
    kept.append(points[-1])
    return kept


def simplify(points: list[list[float]], tolerance: float = SIMPLIFY_TOLERANCE) -> list[list[float]]:
    """Ramer-Douglas-Peucker, without recursion: long strokes must not hit the recursion limit."""
    if len(points) < 3:
        return points
    keep = {0, len(points) - 1}
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        worst, worst_at = 0.0, None
        for i in range(start + 1, end):
            distance = _distance(points[i], points[start], points[end])
            if distance > worst:
                worst, worst_at = distance, i
        if worst_at is not None and worst > tolerance:
            keep.add(worst_at)
            stack += [(start, worst_at), (worst_at, end)]
    return [points[i] for i in sorted(keep)]


def _clean_points(value: Any) -> dict[str, Any]:
    points = value.get("points") if isinstance(value, dict) and set(value) == {"points"} else None
    if not isinstance(points, list) or len(points) < 2:
        raise ValidationError("The geometry of a pen line is points: [[x, y], ...], at least two.")
    if len(points) > FREEHAND_POINTS_IN_MAX:
        raise ValidationError(f"A pen line has at most {FREEHAND_POINTS_IN_MAX} points.")
    clean = []
    for point in points:
        if not isinstance(point, list | tuple) or len(point) != 2:
            raise ValidationError("A pen line's point is a pair [x, y].")
        clean.append([_fraction(point[0], "x"), _fraction(point[1], "y")])
    # Thinned first: simplifying is quadratic at worst, and on at most this many points it stays
    # fast and needs no second pass with a coarser tolerance, which could flatten a real shape.
    return {"points": simplify(thin(clean))}


def clean_geometry(kind: str, value: Any) -> dict[str, Any]:
    if kind == "freehand":
        return _clean_points(value)
    if kind == "pin":
        return _fields(value, ("x", "y"), kind)
    if kind == "arrow":
        arrow = _fields(value, ("x1", "y1", "x2", "y2"), kind)
        if (arrow["x1"], arrow["y1"]) == (arrow["x2"], arrow["y2"]):
            raise ValidationError("An arrow needs two different ends.")
        return arrow
    rect = _fields(value, ("x", "y", "w", "h"), kind)
    if rect["x"] + rect["w"] > 1 + TOLERANCE or rect["y"] + rect["h"] > 1 + TOLERANCE:
        raise ValidationError("A rectangle must stay on the image.")
    rect["w"] = round(min(rect["w"], 1 - rect["x"]), 5)
    rect["h"] = round(min(rect["h"], 1 - rect["y"]), 5)
    if rect["w"] <= 0 or rect["h"] <= 0:
        raise ValidationError("A rectangle needs a width and a height on the image.")
    return rect


def clean_kind(value: Any) -> str:
    if value not in KINDS:
        raise ValidationError(f"kind must be one of: {', '.join(KINDS)}.")
    return str(value)


def clean_colour(value: Any) -> str:
    if value not in COLOURS:
        raise ValidationError(f"colour must be one of: {', '.join(COLOURS)}.")
    return str(value)


def clean_stroke(value: Any) -> float:
    if value is None:
        return DEFAULT_STROKE
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError("stroke_width must be a number.")
    if value != value or value <= 0 or value > STROKE_MAX:
        raise ValidationError(f"stroke_width must be above 0 and at most {STROKE_MAX}.")
    return round(float(value), 5)


def geometry_json(geometry: dict[str, Any]) -> str:
    return json.dumps(geometry, separators=(",", ":"))


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG's IHDR chunk; None when the data has no such header."""
    if len(data) < PNG_HEADER_BYTES or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _pin_head(pin: Annotation) -> str:
    where = f"x {pin.geometry['x']:.2f}, y {pin.geometry['y']:.2f}, annotation {pin.id}"
    return f"#{pin.pin_number} ({where})"


def pin_line(pin: Annotation) -> str:
    """A pin with its whole text, for the tools that show the pins."""
    return f"{_pin_head(pin)} " + (f"'{pin.body_md}'" if pin.body_md else "(no comment)")


def _comment_pin_line(pin: Annotation) -> str:
    """A pin in the owner comment, on one line and cut when long: review_get has it whole."""
    line = " ".join((pin.body_md or "").split())
    if not line:
        return f"{_pin_head(pin)} (no comment)"
    if len(line) > COMMENT_PIN_TEXT_MAX:
        line = line[: COMMENT_PIN_TEXT_MAX - 1].rstrip() + "…"
    return f"{_pin_head(pin)} '{line}'"


def shape_line(shape: Annotation) -> str:
    g = shape.geometry
    if shape.kind == "rect":
        return f"rectangle (x {g['x']:.2f}, y {g['y']:.2f}, w {g['w']:.2f}, h {g['h']:.2f})"
    if shape.kind == "arrow":
        return f"arrow (from x {g['x1']:.2f}, y {g['y1']:.2f} to x {g['x2']:.2f}, y {g['y2']:.2f})"
    xs = [p[0] for p in g["points"]]
    ys = [p[1] for p in g["points"]]
    return f"pen line (within x {min(xs):.2f}-{max(xs):.2f}, y {min(ys):.2f}-{max(ys):.2f})"


def send_text(where: str, attachment_id: int, annotations: list[Annotation]) -> str:
    """The owner comment of "Send to agent", e.g. "Screenshot 3 of step 2 — 2 pins: #1 (x 0.42,
    y 0.18, annotation 12) 'Button text is cut off'; … Annotated image: relaybay://…". Kept
    within a comment's size: a long pin text is cut and what does not fit is counted, and it
    says that review_get has it all."""
    pins = sorted((a for a in annotations if a.kind == "pin"), key=lambda a: a.pin_number or 0)
    count = f"{len(pins)} pin" + ("" if len(pins) == 1 else "s")
    head = f"{where} — {count}: " if pins else f"{where} — no pins. "
    tail = f" Annotated image: relaybay://attachments/{attachment_id}/annotated."
    if pins:
        tail += " Answer a pin with annotation_reply."
    if any(len(" ".join((p.body_md or "").split())) > COMMENT_PIN_TEXT_MAX for p in pins):
        tail += " Long pin texts are cut here: review_get has them whole."
    room = COMMENT_MAX - len(head) - len(tail) - 60  # 60: the "Also marked" and "more" wording
    kept_pins: list[str] = []
    kept_shapes: list[str] = []
    wanted = [(_comment_pin_line(p), kept_pins) for p in pins]
    wanted += [(shape_line(a), kept_shapes) for a in annotations if a.kind != "pin"]
    left = len(wanted)
    for line, into in wanted:
        if len(line) + 2 > room:
            break
        room -= len(line) + 2
        into.append(line)
        left -= 1
    text = head + "; ".join(kept_pins)
    if kept_pins:
        text += "."
    if kept_shapes:
        text += (" " if kept_pins else "") + "Also marked: " + "; ".join(kept_shapes) + "."
    if left:
        text += f" And {left} more: see review_get."
    return text + tail
