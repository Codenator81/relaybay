"""The login check: ask the user's own `claude auth status` how it is signed in.

relaybay never signs in for the user and never touches credentials. Of the answer it reads exactly
`loggedIn`, `authMethod` and `subscriptionType`; the rest (an e-mail, an organization) is dropped
with the parsed object and is never stored, shown or logged.
"""

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from relaybay.machine_setup import run_quiet

FIX_COMMAND = "claude, then /login inside it"
SUBSCRIPTION_METHODS = ("claude.ai",)
API_KEY_METHODS = ("api_key", "api_key_helper")
THIRD_PARTY_METHODS = ("third_party",)
_WORD = re.compile(r"[A-Za-z0-9_.-]{1,40}")
TIMEOUT_SECONDS = 20


@dataclass(frozen=True, slots=True)
class LoginState:
    logged_in: bool
    method: str | None
    subscription: str | None
    exit_code: int | None
    readable: bool
    error: str | None = None  # the check itself could not run

    @property
    def is_subscription(self) -> bool:
        return self.logged_in and self.method in SUBSCRIPTION_METHODS


def _word(value: Any) -> str | None:
    return value if isinstance(value, str) and _WORD.fullmatch(value) else None


def parse_auth_status(stdout: str, code: int) -> LoginState:
    """The three fields relaybay uses; the parsed answer goes out of scope here."""
    try:
        answer = json.loads(stdout or "")
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        # Exit 0 without the JSON answer: some sign-in, but not one known to be a subscription.
        return LoginState(code == 0, None, None, code, readable=False)
    logged_in = answer.get("loggedIn") is True and code == 0
    method = _word(answer.get("authMethod"))
    subscription = _word(answer.get("subscriptionType"))
    return LoginState(logged_in, method, subscription, code, readable=True)


def check_login(claude: str, run: Callable[..., Any] = run_quiet) -> LoginState:
    """`claude auth status`; stderr is not read (it may hold account details)."""
    try:
        done = run(
            [claude, "auth", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return LoginState(False, None, None, None, readable=False, error=type(exc).__name__)
    return parse_auth_status(done.stdout or "", done.returncode)


def login_line(state: LoginState) -> str:
    """The line of `relaybay doctor` and of the card before Start."""
    if state.error is not None:
        return f"claude: the sign-in check did not run ({state.error})"
    if not state.logged_in:
        return "claude: not signed in"
    if not state.readable:
        return "claude: an answer relaybay cannot read, not a subscription sign-in"
    if state.method in SUBSCRIPTION_METHODS:
        plan = f" ({state.subscription})" if state.subscription else ""
        return f"claude: signed in with a subscription{plan}"
    if state.method in API_KEY_METHODS:
        return "claude: signed in with an API key (usage is billed to the key)"
    if state.method in THIRD_PARTY_METHODS:
        return "claude: third-party provider"
    return (
        f"claude: signed in with {state.method or 'an unknown method'}, not a subscription sign-in"
    )


def refusal(state: LoginState, allow_other: bool) -> str | None:
    """Why a run may not start under this sign-in, or None. Signed out never starts; anything
    but a subscription starts only when the owner allowed it in Settings."""
    if state.error is not None or not state.logged_in:
        return f"{login_line(state)}. Sign in in a terminal: {FIX_COMMAND}."
    if state.is_subscription or allow_other:
        return None
    return (
        f"{login_line(state)}. Runs start only under a subscription sign-in unless Settings →"
        f" Agents allows other methods; or sign in in a terminal: {FIX_COMMAND}."
    )
