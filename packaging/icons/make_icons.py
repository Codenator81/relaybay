"""Render the relaybay mark (relaybay.svg, a 64 x 64 viewBox) with Pillow's own primitives.

    uv run --extra desktop python packaging/icons/make_icons.py

The mark: a rounded square, a diagonal baton and two nodes (a filled circle where the baton
starts, a ring where it arrives). The numbers below are the SVG's; small sizes and the
monochrome tray icons use the thicker strokes of the design sheet. No SVG renderer is needed,
so the output is the same on every machine. Writes, and the generated files are committed:

- packaging/icons/relaybay.png (1024), relaybay.ico (16-256), relaybay.icns (16-1024)
- src/relaybay/web/static/favicon.png (32), the board's favicon
- src/relaybay/desktop/icons/relaybay.png (64, the colour tray icon) and tray-light.png /
  tray-dark.png (32) with tray-light-16.png / tray-dark-16.png: the baton and nodes only, dark
  for a light panel, white for a dark one
"""

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent / "src" / "relaybay"
STATIC = SRC / "web" / "static"
TRAY = SRC / "desktop" / "icons"

VIEWBOX = 64
BLUE = (0x3B, 0x5B, 0xDB, 255)
WHITE = (255, 255, 255, 255)
DARK = (0x1F, 0x23, 0x28, 255)
CLEAR = (0, 0, 0, 0)
BATON = ((20, 44), (44, 20))
START = (18, 46)  # filled node
END = (46, 18)  # ring


@dataclass(frozen=True)
class Strokes:
    radius: float  # corner of the square
    baton: float  # baton width
    node: float  # node radius
    ring: float  # ring stroke width (centred on the node radius, as in SVG)


REGULAR = Strokes(radius=14, baton=7, node=7, ring=4)
THICK = Strokes(radius=16, baton=10, node=10, ring=7)


def render(
    size: int,
    strokes: Strokes,
    figure: tuple[int, int, int, int] = WHITE,
    square: tuple[int, int, int, int] | None = BLUE,
) -> Image.Image:
    """The mark at `size` px; `square=None` leaves the background transparent (tray)."""
    scale = 8  # draw large, then downsample for smooth edges
    big = size * scale
    k = big / VIEWBOX
    image = Image.new("RGBA", (big, big), CLEAR)
    pen = ImageDraw.Draw(image)

    def circle(center: tuple[float, float], radius: float, fill: tuple[int, ...]) -> None:
        x, y = center[0] * k, center[1] * k
        pen.ellipse((x - radius * k, y - radius * k, x + radius * k, y + radius * k), fill=fill)

    if square is not None:
        pen.rounded_rectangle((0, 0, big - 1, big - 1), radius=strokes.radius * k, fill=square)
    (x0, y0), (x1, y1) = BATON
    pen.line((x0 * k, y0 * k, x1 * k, y1 * k), fill=figure, width=round(strokes.baton * k))
    for end in BATON:  # round caps
        circle(end, strokes.baton / 2, figure)
    circle(START, strokes.node, figure)
    # The ring: an outer disc, then its inside in the square's colour (or cleared), which also
    # hides the baton's end as the SVG's filled ring does.
    circle(END, strokes.node + strokes.ring / 2, figure)
    circle(END, strokes.node - strokes.ring / 2, square or CLEAR)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def colour(size: int) -> Image.Image:
    return render(size, THICK if size <= 16 else REGULAR)


def main() -> None:
    TRAY.mkdir(parents=True, exist_ok=True)
    big = colour(1024)
    outputs = {
        HERE / "relaybay.png": lambda path: big.save(path, optimize=True),
        HERE / "relaybay.ico": lambda path: big.save(
            path,
            sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)],
            append_images=[colour(s) for s in (16, 24, 32, 48, 64, 128, 256)],
        ),
        HERE / "relaybay.icns": lambda path: big.save(
            path, append_images=[colour(s) for s in (16, 32, 64, 128, 256, 512)]
        ),
        STATIC / "favicon.png": lambda path: colour(32).save(path, optimize=True),
        TRAY / "relaybay.png": lambda path: colour(64).save(path, optimize=True),
    }
    for theme, figure in (("light", DARK), ("dark", WHITE)):
        outputs[TRAY / f"tray-{theme}.png"] = lambda path, f=figure: render(
            32, THICK, f, square=None
        ).save(path, optimize=True)
        outputs[TRAY / f"tray-{theme}-16.png"] = lambda path, f=figure: render(
            16, THICK, f, square=None
        ).save(path, optimize=True)
    root = HERE.parent.parent
    for path, write in outputs.items():
        write(path)
        print(f"wrote {path.relative_to(root).as_posix()}")


if __name__ == "__main__":
    main()
