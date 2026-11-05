"""The desktop program: a tray icon, a board window and an embedded server (`relaybay desktop`).

The rest of relaybay imports only `settings` (the data folder, the external server) and
`add_run_arguments`; the GUI libraries of the `desktop` extra load lazily.
"""

import argparse


def add_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Options of `relaybay desktop` and `relaybay-desktop` (kept here, import-light: the CLI
    parser is built on every `relaybay` call, hooks included)."""
    parser.add_argument(
        "--background",
        action="store_true",
        help="Start with the window hidden, only the tray icon (the autostart entry uses it).",
    )
    parser.add_argument(
        "--quit",
        action="store_true",
        help="Ask the running program to quit (stops its server); exit 1 when none answers.",
    )
    # For CI smoke tests: quit by itself after this many seconds.
    parser.add_argument("--exit-after", type=float, metavar="SECONDS", help=argparse.SUPPRESS)
