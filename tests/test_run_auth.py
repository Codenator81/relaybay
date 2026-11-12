"""The login check reads the sign-in method of `claude auth status`, nothing else."""

import json
import subprocess
from typing import Any

from relaybay.runner.auth import (
    FIX_COMMAND,
    LoginState,
    check_login,
    login_line,
    parse_auth_status,
    refusal,
)

IDENTITY = {"email": "owner@example.invalid", "orgName": "Acme Org", "orgId": "org-123"}


def answer(code: int = 0, **fields: Any) -> tuple[str, int]:
    return json.dumps({**fields, **IDENTITY}), code


def state(code: int = 0, **fields: Any) -> LoginState:
    return parse_auth_status(*answer(code, **fields))


def test_each_answer_gives_its_line_and_start_rule() -> None:
    subscription = state(loggedIn=True, authMethod="claude.ai", subscriptionType="max")
    assert login_line(subscription) == "claude: signed in with a subscription (max)"
    assert refusal(subscription, False) is None

    signed_out = state(1, loggedIn=False, authMethod="none")
    assert login_line(signed_out) == "claude: not signed in"
    assert FIX_COMMAND in (refusal(signed_out, True) or "")  # the switch never helps here

    api_key = state(loggedIn=True, authMethod="api_key")
    assert login_line(api_key) == "claude: signed in with an API key (usage is billed to the key)"
    assert "Settings → Agents" in (refusal(api_key, False) or "")
    assert refusal(api_key, True) is None

    third = state(loggedIn=True, authMethod="third_party")
    assert login_line(third) == "claude: third-party provider"
    assert refusal(third, False) and refusal(third, True) is None

    token = state(loggedIn=True, authMethod="oauth_token")
    assert login_line(token).endswith("oauth_token, not a subscription sign-in")
    assert refusal(token, False)

    unreadable = parse_auth_status("Signed in.", 0)
    assert "cannot read" in login_line(unreadable) and refusal(unreadable, False)
    assert login_line(parse_auth_status("", 1)) == "claude: not signed in"
    # The exit code decides "signed in", whatever the JSON says.
    assert login_line(state(1, loggedIn=True, authMethod="claude.ai")) == "claude: not signed in"


def test_the_identity_fields_are_dropped() -> None:
    parsed = state(loggedIn=True, authMethod="claude.ai", subscriptionType="pro")
    shown = repr(parsed) + login_line(parsed)
    for value in IDENTITY.values():
        assert value not in shown
    odd = state(loggedIn=True, authMethod="owner@example.invalid")
    assert odd.method is None  # a field with an unexpected shape is not kept either


def test_check_login_runs_auth_status_and_survives_a_missing_binary() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        seen.append(argv)
        out, code = answer(loggedIn=True, authMethod="claude.ai")
        return subprocess.CompletedProcess(argv, code, out, "owner@example.invalid")

    assert check_login("/bin/claude", run).is_subscription
    assert seen == [["/bin/claude", "auth", "status"]]

    def missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
        raise FileNotFoundError(2, "not found", argv[0])

    failed = check_login("/bin/claude", missing)
    assert failed.error == "FileNotFoundError" and not failed.logged_in
    assert "did not run" in login_line(failed) and refusal(failed, True)
