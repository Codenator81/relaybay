"""The run rules: status transitions and what a run may be started with."""

import itertools

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


def test_the_order_of_permission_modes() -> None:
    assert set(runs.MODE_RANK) == set(runs.PERMISSION_MODES + runs.OWNER_ONLY_MODES)
    order = ["plan", "default", "acceptEdits", "auto", "bypassPermissions"]
    for low, high in itertools.pairwise(order):
        assert runs.raises_mode(low, high) and not runs.raises_mode(high, low)
    for same in ("manual", "dontAsk"):
        assert not runs.raises_mode("default", same) and not runs.raises_mode(same, "default")
    assert runs.raises_mode("plan", "someNewMode")  # unknown counts as a raise


def test_the_desktop_link_needs_an_opened_session() -> None:
    run = runs.Run(
        1, 1, 1, None, "/w", None, "opus", "medium", "auto", "mcp__relaybay", False,
        "11111111-2222-3333-4444-555555555555", "idle", None, None, "user", "user", "owner",
        "owner", "t", "t", None, None,
    )  # fmt: skip
    assert run.desktop_link is None and not run.with_owner
    run.desktop_opened_at = "t"
    assert (
        run.desktop_link == "claude://claude.ai/epitaxy/local_11111111-2222-3333-4444-555555555555"
    )
