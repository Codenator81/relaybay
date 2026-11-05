"""`python -m relaybay.desktop`: the desktop program (the autostart entry uses it from a venv)."""

import sys

from relaybay.desktop.app import main

sys.exit(main())
