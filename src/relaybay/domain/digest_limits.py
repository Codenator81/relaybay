"""Limits and defaults of the digests and the pulse. Apart from `digest.py` because `config.py`
reads them and `relaybay hook` must start fast: no search or stemming imports here."""

PULSE_LINES_MAX = 5
PULSE_CHARS_MAX = 600
DEFAULT_PULSE_STALE_DAYS = 7
DEFAULT_TASK_STALE_DAYS = 5
DEFAULT_BUDGET = 12_000  # characters, about 3k tokens
BUDGET_MIN = 500
BUDGET_MAX = 40_000
DEFAULT_SESSION_PROJECT_CHARS = 1_500
SESSION_CONTEXT_MODES = ("auto", "off")
