"""Full-page board forms: POST → 303 → GET, so a refresh after saving does not resend the form;
the outcome shows once through a short-lived flash cookie."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from relaybay import machine_setup
from relaybay.config import Settings, load_settings
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, run_in_app

FORM = {"Origin": BASE}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # The Integrations page must target a temp folder, never the real home.
    home = tmp_path / "claude-home"
    (home / ".claude").mkdir(parents=True)
    return load_settings(home=tmp_path / "data", claude_home=home, environ={})


def post(board: TestClient, token: str, url: str, **data: str):
    return board.post(
        url, data={"csrf_token": csrf_token(token), **data}, headers=FORM, follow_redirects=False
    )


def once(board: TestClient, url: str, text: str) -> None:
    """The GET shows the flash, and a second GET (a refresh) no longer does."""
    assert text in board.get(url).text
    assert text not in board.get(url).text


def test_policy_saves_redirect_and_flash_once(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    saved = post(
        board,
        token,
        "/settings/policy/layer",
        scope="acme-shop",
        body='{"guard": {"mode": "warn"}}',
        action="save",
    )
    assert (
        saved.status_code == 303 and saved.headers["location"] == "/settings/policy?scope=acme-shop"
    )
    once(board, saved.headers["location"], "Saved the acme-shop policy")

    preset = post(board, token, "/settings/policy/preset", preset="strict")
    assert preset.status_code == 303 and preset.headers["location"] == "/settings/policy"
    once(board, "/settings/policy", "Preset set to strict")

    # Raising live asks first (a rendered page); the confirmed save redirects.
    confirm = post(board, token, "/settings/policy/preset", preset="trusted")
    assert confirm.status_code == 200 and 'name="confirm_live"' in confirm.text
    done = post(board, token, "/settings/policy/preset", preset="trusted", confirm_live="1")
    assert done.status_code == 303
    once(board, "/settings/policy", "Preset set to trusted")
    assert json.loads(service.policy_file.text) == {"preset": "trusted"}

    # A preview saves nothing and stays a rendered page.
    preview = post(
        board, token, "/settings/policy/layer", scope="acme-shop", body="{}", action="preview"
    )
    assert preview.status_code == 200


def test_stands_and_access_writes_redirect_and_flash_once(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop")
    page = "/p/acme-shop/environments"
    created = post(board, token, page, name="staging", kind="test")
    assert created.status_code == 303 and created.headers["location"] == page
    once(board, page, "Environment added.")
    _, [env] = run_in_app(board, service.environments, "acme-shop")
    updated = post(board, token, f"{page}/{env.id}", name="staging", kind="test", notes="x")
    assert updated.status_code == 303 and updated.headers["location"] == f"{page}#env-{env.id}"
    once(board, page, "Environment saved.")
    added = post(board, token, f"{page}/{env.id}/access", kind="ssh", fields="host: stage")
    assert added.status_code == 303
    once(board, page, "Saved.")
    deleted = post(board, token, f"{page}/{env.id}/delete")
    assert deleted.status_code == 303
    once(board, page, "Environment deleted.")
    refused = post(board, token, page, name="", kind="test")
    assert refused.status_code == 400  # nothing saved: the page with the reason


def test_integrations_plain_posts_redirect_and_flash_once(
    board: TestClient, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_check(**_):
        return [machine_setup.Check(True, "relaybay server", "running, same token")]

    monkeypatch.setattr(machine_setup, "run_check", fake_check)
    checked = post(board, token, "/settings/integrations/check", mode="command")
    assert checked.status_code == 303
    assert checked.headers["location"] == "/settings/integrations?mode=command"
    once(board, checked.headers["location"], "running, same token")
    stale = post(board, token, "/settings/integrations/apply", mode="command", expect="x")
    assert stale.status_code == 303
    once(board, stale.headers["location"], "not the one you approved")
    # htmx swaps the fragment in place and gets no redirect.
    fragment = board.post(
        "/settings/integrations/check",
        data={"csrf_token": csrf_token(token)},
        headers={**FORM, "HX-Request": "true"},
    )
    assert fragment.status_code == 200 and "running, same token" in fragment.text
