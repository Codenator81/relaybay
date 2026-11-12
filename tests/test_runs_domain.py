"""The run rules: status transitions and what a run may be started with."""

import pytest

from relaybay.domain import runs
from relaybay.domain.errors import ValidationError


def test_transitions() -> None:
    runs.check_transition(None, "running")
    for current, new in (
        ("running", "idle"),
        ("running", "stopped"),
        ("running", "lost"),
        ("idle", "running"),
        ("stopped", "running"),
        ("lost", "running"),
        ("idle", "ended"),
        ("stopped", "ended"),
        ("lost", "ended"),
    ):
        runs.check_transition(current, new)
    for current, new in (
        (None, "idle"),
        ("running", "ended"),  # a running run is stopped first
        ("running", "running"),
        ("ended", "running"),
        ("ended", "ended"),
        ("idle", "lost"),
    ):
        with pytest.raises(ValidationError):
            runs.check_transition(current, new)
    with pytest.raises(ValidationError, match="Unknown run status"):
        runs.check_transition("idle", "paused")
    assert set(runs.TRANSITIONS) - {None} == set(runs.RUN_STATUSES)


def test_cleaners() -> None:
    assert runs.clean_model("") == "opus" and runs.clean_model("haiku") == "haiku"
    assert runs.clean_effort(None) == "medium"
    assert runs.clean_mode("manual", owner=False) == "manual"
    assert runs.clean_mode("bypassPermissions", owner=True) == "bypassPermissions"
    with pytest.raises(ValidationError, match="permission mode"):
        runs.clean_mode("bypassPermissions", owner=False)  # never a project default
    with pytest.raises(ValidationError):
        runs.clean_model("gpt")
    with pytest.raises(ValidationError):
        runs.clean_effort("extreme")


def test_allowed_tools_are_split_outside_parentheses() -> None:
    text = "mcp__relaybay, Bash(git diff *),Read , Bash(npm run test, lint)"
    assert runs.split_rules(text) == [
        "mcp__relaybay",
        "Bash(git diff *)",
        "Read",
        "Bash(npm run test, lint)",
    ]
    assert runs.clean_allowed_tools(text) == (
        "mcp__relaybay, Bash(git diff *), Read, Bash(npm run test, lint)"
    )
    for bad in ("", " , ", "Bash(git", "rm -rf /", "Bash(a)\nRead", "a" * 201):
        with pytest.raises(ValidationError):
            runs.clean_allowed_tools(bad)


def test_prompt() -> None:
    assert runs.clean_prompt("  go\r\non  ") == "go\non"
    for bad in ("", "   ", "x" * (runs.BRIEF_MAX + 1), "a\x00b"):
        with pytest.raises(ValidationError):
            runs.clean_prompt(bad)
