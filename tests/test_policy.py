"""Policy as data: presets, layers, schema errors, custom guard patterns, secret rules, test
secrets, proposals, `log` mode, Settings → Policy and the MCP tools."""

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from relaybay.config import load_settings
from relaybay.db import repo
from relaybay.domain import guard
from relaybay.domain.environments import DETECTOR_IDS, check_secret
from relaybay.domain.errors import ConfirmationNeeded, ConflictError, ValidationError
from relaybay.policy import (
    PRESETS,
    EffectivePolicy,
    Layer,
    PolicyError,
    hot_pattern,
    merge,
    parse_layer,
    preset,
    schema,
    validate_layer,
)
from relaybay.security import csrf_token
from relaybay.service import Service
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_environments import answer, bash, live_project, output
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect

FORM = {"Origin": BASE}
RESTART = "ssh prod-alias 'systemctl restart app'"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZW1vIn0.c2lnbmF0dXJl"


@pytest.fixture
async def svc(tmp_path: Path) -> AsyncIterator[Service]:
    """A service with a global policy file (not written yet)."""
    service = Service(tmp_path / "relaybay.sqlite", policy_file=tmp_path / "policy.json")
    await service.start()
    try:
        yield service
    finally:
        await service.close()


def write_policy(service: Service, body: dict[str, Any]) -> None:
    assert service.policy_file.path is not None
    service.policy_file.path.write_text(json.dumps(body), encoding="utf-8")


async def project_layer(service: Service, scope: str, body: dict[str, Any]) -> None:
    await service.save_policy_layer(scope, json.dumps(body), confirm_live=True)


# --- schema, presets, merging ---------------------------------------------------------------


def test_presets_and_schema_ids_match_the_code() -> None:
    for name in PRESETS:
        validate_layer(preset(name), name, "preset")
    defs = schema()["$defs"]
    assert defs["ruleIds"]["items"]["enum"] == list(guard.RULE_IDS)
    assert defs["detectorIds"]["items"]["enum"] == list(DETECTOR_IDS)
    assert len(set(guard.RULE_IDS)) == len(guard.RULE_IDS)
    trusted = EffectivePolicy([Layer("preset:trusted", "preset", preset("trusted"))])
    assert (trusted.level("live"), trusted.guard_mode) == ("full", "log")
    assert trusted.test_secrets_allowed("test") and not trusted.test_secrets_allowed("live")
    assert not trusted.expose_test_secrets
    strict = EffectivePolicy([Layer("preset:strict", "preset", preset("strict"))])
    assert (strict.level("live"), strict.level("test"), strict.guard_mode) == (
        "none",
        "ask",
        "block",
    )


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ([], "must be a JSON object"),
        ({"guard": {"mode": "loud"}}, "guard.mode: 'loud' is not one of"),
        ({"levels": {"live": {"default": "root"}}}, "levels.live.default"),
        ({"secrets": {"test_secrets": {"live": True}}}, "'live' was unexpected"),
        ({"guard": {"disable": ["no-such-rule"]}}, "guard.disable[0]"),
        ({"colour": "red"}, "'colour' was unexpected"),
        ({"preset": "trusted"}, "only the global policy names a preset"),
        (
            {"guard": {"patterns": [{"id": "git-push", "change": "x", "description": "d"}]}},
            "is a built-in rule id",
        ),
        (
            {"guard": {"patterns": [{"id": "slow", "change": "(a+)+$", "description": "d"}]}},
            "can be slow",
        ),
        (
            {"guard": {"patterns": [{"id": "back", "change": r"(a)\1", "description": "d"}]}},
            "back-references",
        ),
        (
            {"guard": {"patterns": [{"id": "bad", "change": "([", "description": "d"}]}},
            "not a valid regular expression",
        ),
        (
            {"guard": {"patterns": [{"id": "long", "change": "a" * 201, "description": "d"}]}},
            "is too long",
        ),
        ({"secrets": {"allow": ["(x*)*"]}}, "secrets.allow[0]"),
    ],
)
def test_schema_errors_name_the_path(body: Any, message: str) -> None:
    with pytest.raises(PolicyError, match=r"^project acme: ") as err:
        validate_layer(body, "project acme", "project")
    assert message in str(err.value)


def test_parse_errors_say_where() -> None:
    with pytest.raises(PolicyError, match=r"line 1, column 2"):
        parse_layer("{oops}", "policy.json", "global")
    assert parse_layer("  ", "policy.json", "global") == {}


def test_lower_layers_win_with_explicit_list_semantics() -> None:
    layers = [
        Layer("preset:default", "preset", preset("default")),
        Layer(
            "global",
            "global",
            {
                "guard": {
                    "disable": ["git-push"],
                    "patterns": [{"id": "deploy-sh", "change": "deploy\\.sh", "description": "d"}],
                },
                "secrets": {"allow": ["demo-[a-z]+"]},
            },
        ),
        Layer(
            "project:acme-shop",
            "project",
            {
                "levels": {"live": {"ssh": "ask"}},
                "guard": {
                    "disable+": ["helm-change"],
                    "patterns+": [
                        {"id": "deploy-sh", "change": "deploy2\\.sh", "description": "e"},
                        {"id": "migrate", "change": "migrate", "description": "m"},
                    ],
                },
                "secrets": {"allow-": ["demo-[a-z]+"]},
            },
        ),
        Layer("environment:acme-shop/live", "environment", {"guard": {"disable-": ["git-push"]}}),
    ]
    data, sources = merge(layers)
    assert data["guard"]["disable"] == ["helm-change"]
    assert [p["description"] for p in data["guard"]["patterns"]] == ["e", "m"]
    assert data["secrets"]["allow"] == []
    assert data["levels"]["live"] == {"default": "read", "ssh": "ask"}
    assert sources["levels.live.ssh"] == ["project:acme-shop"]
    assert sources["guard.disable"] == [
        "global",
        "project:acme-shop",
        "environment:acme-shop/live",
    ]
    pol = EffectivePolicy(layers)
    assert pol.level("live", "ssh") == "ask" and pol.level("live", "db") == "read"
    assert pol.level_source("live", "ssh") == "project:acme-shop"
    assert pol.level_source("live", "db") == "preset:default"
    ids = [r.id for r in pol.rules]
    assert "helm-change" not in ids and "git-push" in ids and ids[-2:] == ["deploy-sh", "migrate"]


# --- the global file -------------------------------------------------------------------------


async def test_no_policy_file_is_the_default_preset(svc: Service) -> None:
    pol = svc.global_policy()
    assert pol.preset == "default" and pol.guard_mode == "warn"
    assert svc.permission_defaults == {"local": "full", "test": "full", "live": "read"}
    assert all(source == "preset:default" for _, _, source in pol.flat())


async def test_broken_global_file_stops_the_start_and_later_keeps_the_last_good(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "policy.json").write_text('{"guard": {"mode": "loud"}}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"policy.json: guard.mode"):
        load_settings(home=tmp_path, environ={})
    broken = Service(tmp_path / "a.sqlite", policy_file=tmp_path / "policy.json")
    with pytest.raises(PolicyError):
        await broken.start()
    await broken.close()

    (tmp_path / "policy.json").write_text('{"guard": {"mode": "block"}}', encoding="utf-8")
    assert load_settings(home=tmp_path, environ={}).policy_path == tmp_path / "policy.json"
    service = Service(tmp_path / "b.sqlite", policy_file=tmp_path / "policy.json")
    await service.start()
    try:
        assert service.guard_mode == "block"
        (tmp_path / "policy.json").write_text('{"guard": {"mode": "loud"}', encoding="utf-8")
        with caplog.at_level(logging.ERROR, logger="relaybay"):
            assert service.guard_mode == "block"  # the last good file stays
        assert "not valid JSON" in (service.policy_file.error or "")
        assert "last valid global policy" in caplog.text
        write_policy(service, {"preset": "trusted"})
        assert service.guard_mode == "log" and service.policy_file.error is None
    finally:
        await service.close()


def test_policy_file_setting_is_relative_to_the_home(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text('policy_file = "team/policy.json"\n', encoding="utf-8")
    settings = load_settings(home=tmp_path, environ={})
    assert settings.policy_path == tmp_path / "team" / "policy.json"
    other = load_settings(home=tmp_path, environ={"RELAYBAY_POLICY_FILE": str(tmp_path / "x")})
    assert other.policy_path == tmp_path / "x"


async def test_legacy_config_keys_are_a_layer_over_the_preset(tmp_path: Path) -> None:
    (tmp_path / "policy.json").write_text('{"preset": "trusted"}', encoding="utf-8")
    service = Service(
        tmp_path / "x.sqlite",
        guard_mode="block",
        permission_defaults={"test": "ask"},
        policy_file=tmp_path / "policy.json",
    )
    await service.start()
    try:
        pol = service.global_policy()
        assert [layer.name for layer in pol.layers] == ["preset:trusted", "config", "global"]
        assert (pol.guard_mode, pol.level("test"), pol.level("live")) == ("block", "ask", "full")
        assert pol.source("guard.mode") == "config"
    finally:
        await service.close()


# --- levels and the guard from the policy ---------------------------------------------------


async def test_trusted_preset_only_journals_live(svc: Service) -> None:
    await live_project(svc)
    write_policy(svc, {"preset": "trusted"})
    assert await answer(svc, bash(RESTART)) is None
    journal = await svc.live_journal("acme-shop")
    assert [e.payload["decision"] for e in journal] == ["allow"]
    _, items = await svc.environments("acme-shop")
    pol = (await svc.environment_policies(items))[items[0].id]
    assert pol.level_origin(items[0]) == "preset:trusted"


async def test_log_mode_journals_without_messages(svc: Service) -> None:
    await live_project(svc)
    write_policy(svc, {"guard": {"mode": "log"}})
    assert await answer(svc, bash(RESTART)) is None
    await project_layer(svc, "acme-shop", {"levels": {"live": {"default": "ask"}}})
    assert await answer(svc, bash("ssh prod-alias uptime")) is None
    decisions = [e.payload["decision"] for e in await svc.live_journal("acme-shop")]
    assert decisions == ["log", "log"]


async def test_project_layer_levels_and_the_environment_wins(svc: Service) -> None:
    env_id = await live_project(svc)
    await project_layer(svc, "acme-shop", {"levels": {"live": {"default": "none"}}})
    out = output(await answer(svc, bash("ssh prod-alias uptime")))
    assert "off limits" in out["additionalContext"]
    # The environment's own level on Stands & access wins over the policy.
    await svc.save_environment("acme-shop", {"level": "full"}, env_id=env_id, confirm_live=True)
    assert await answer(svc, bash(RESTART)) is None
    snapshot = await svc.snapshot("acme-shop")
    assert snapshot.policies[env_id].level_origin(snapshot.environments[0]) == "environment"


async def test_custom_pattern_in_a_project_layer_warns(svc: Service) -> None:
    await live_project(svc)
    write_policy(svc, {"preset": "trusted", "levels": {"live": {"default": "read"}}})
    command = "ssh prod-alias ./deploy.sh --now"
    assert await answer(svc, bash(command)) is None  # trusted: guard log
    await project_layer(
        svc,
        "acme-shop",
        {
            "guard": {
                "mode": "warn",
                "patterns+": [
                    {
                        "id": "deploy-script",
                        "capability": "ssh",
                        "command": "\\bssh\\b",
                        "change": "deploy\\.sh",
                        "description": "deploy script run",
                    }
                ],
            }
        },
    )
    out = output(await answer(svc, bash(command)))
    assert "this looks like a deploy script run" in out["additionalContext"]
    assert "(warn mode: not blocked)" in out["additionalContext"]
    assert await answer(svc, bash("ssh prod-alias ./status.sh")) is None


async def test_built_in_rules_are_disabled_by_id(svc: Service) -> None:
    await live_project(svc)
    assert output(await answer(svc, bash(RESTART)))["additionalContext"]
    await project_layer(svc, "acme-shop", {"guard": {"disable": ["ssh-remote-change"]}})
    assert await answer(svc, bash(RESTART)) is None
    await project_layer(svc, "acme-shop/live", {"guard": {"mode": "off"}})
    assert await answer(svc, bash("ssh prod-alias 'rm -rf /x'")) is None
    # Newest first; guard off on that environment journals nothing.
    assert [e.payload["decision"] for e in await svc.live_journal("acme-shop")] == [
        "allow",
        "warn",
    ]


async def test_broken_database_layer_is_logged_and_ignored(
    svc: Service, caplog: pytest.LogCaptureFixture
) -> None:
    await live_project(svc)
    project = await svc.get_project("acme-shop")
    async with svc._tx() as tx:
        await repo.set_policy_layer(tx.conn, project.id, None, '{"guard": 1}', "hand", tx.now)
    with caplog.at_level(logging.WARNING, logger="relaybay"):
        pol = await svc.effective_policy("acme-shop")
    assert pol.guard_mode == "warn"
    assert pol.errors and "project:acme-shop" in pol.errors[0][1]
    assert "The layer is ignored" in caplog.text
    assert output(await answer(svc, bash(RESTART)))["additionalContext"]


# --- secret rules ----------------------------------------------------------------------------


async def test_disabled_detectors_and_allow_patterns(svc: Service) -> None:
    await svc.upsert_project("acme-shop")
    with pytest.raises(ValidationError, match=r"\(jwt\)"):
        await svc.save_environment("acme-shop", {"name": "t", "kind": "test", "notes": JWT})
    await project_layer(svc, "acme-shop", {"secrets": {"disable": ["jwt"]}})
    env = await svc.save_environment("acme-shop", {"name": "t", "kind": "test", "notes": JWT})
    assert env.notes == JWT
    with pytest.raises(ValidationError, match="must be a reference"):
        await svc.save_access("acme-shop", env.id, "db", {"password_ref": "demo-pass"})
    await project_layer(svc, "acme-shop", {"secrets": {"allow": ["demo-[a-z]+"]}})
    saved = await svc.save_access("acme-shop", env.id, "db", {"password_ref": "demo-pass"})
    assert saved.access[0].fields == {"password_ref": "demo-pass"}
    # Other projects keep the default checks.
    await svc.upsert_project("demo-api")
    with pytest.raises(ValidationError):
        await svc.save_environment("demo-api", {"name": "t", "kind": "test", "notes": JWT})
    check_secret("notes", "plain words")


# --- test secrets ----------------------------------------------------------------------------

TEST_PW = "demo-pass-123"


async def test_test_secrets_need_the_policy_and_never_go_live(svc: Service) -> None:
    await svc.upsert_project("acme-shop")
    test = await svc.save_environment("acme-shop", {"name": "test", "kind": "test"})
    live = await svc.save_environment("acme-shop", {"name": "live", "kind": "live"})
    secret = {"host": "db.test.example.com", "password": {"value": TEST_PW, "test": True}}
    with pytest.raises(ValidationError, match=r"secrets.test_secrets.test"):
        await svc.save_access("acme-shop", test.id, "db", secret)
    write_policy(svc, {"preset": "trusted"})
    saved = await svc.save_access("acme-shop", test.id, "db", secret)
    entry = saved.access[0]
    assert entry.fields == {"host": "db.test.example.com"}
    assert entry.test_secrets == {"password": TEST_PW}
    with pytest.raises(ValidationError, match="never allowed on live"):
        await svc.save_access("acme-shop", live.id, "db", secret)
    with pytest.raises(ValidationError, match="never allowed on live"):
        await svc.save_environment("acme-shop", {"kind": "live"}, env_id=test.id, confirm_live=True)
    # `***` keeps the stored value; replacing fields alone keeps the test secrets.
    kept = await svc.save_access(
        "acme-shop",
        test.id,
        "db",
        {"host": "db2.test.example.com"},
        test_secrets={"password": "***"},
        access_id=entry.id,
    )
    assert kept.access[0].test_secrets == {"password": TEST_PW}
    kept = await svc.save_access(
        "acme-shop", test.id, "db", {"host": "db3.test.example.com"}, access_id=entry.id
    )
    assert kept.access[0].test_secrets == {"password": TEST_PW}
    with pytest.raises(ValidationError, match="test secrets are entered by a person"):
        await svc.propose_environment_change(
            "acme-shop", {"environment": "test", "access": [{"kind": "db", "fields": secret}]}, "x"
        )


async def test_test_secrets_are_hidden_from_agents_and_the_journal(svc: Service) -> None:
    await live_project(svc)
    write_policy(svc, {"preset": "trusted"})
    test = await svc.save_environment("acme-shop", {"name": "test", "kind": "test"})
    await svc.save_access(
        "acme-shop", test.id, "db", {"password": {"value": TEST_PW, "test": True}}
    )
    await answer(svc, bash(f"ssh prod-alias 'mysql-tool {TEST_PW}'"))
    command = (await svc.live_journal("acme-shop"))[0].payload["command"]
    assert TEST_PW not in command and "***" in command


async def test_policy_proposals(svc: Service) -> None:
    await live_project(svc)
    with pytest.raises(ValidationError, match=r"guard\.mode"):
        await svc.propose_policy_change("acme-shop", {"guard": {"mode": "loud"}}, "why")
    with pytest.raises(ValidationError, match="patch must be"):
        await svc.propose_policy_change("acme-shop", {}, "why")
    with pytest.raises(ValidationError, match="reason"):
        await svc.propose_policy_change("acme-shop", {"guard": {"mode": "block"}}, " ")
    widen = await svc.propose_policy_change(
        "global", {"preset": "trusted"}, "A trusted laptop", session="dev-1"
    )
    narrow = await svc.propose_policy_change(
        "acme-shop/live", {"guard": {"mode": "block"}}, "Block on live"
    )
    assert (widen.scope, widen.proposed_by, narrow.scope) == ("global", "dev-1", "acme-shop/live")
    inbox = await svc.inbox(["owner"])
    assert [p.id for p in inbox.policy_proposals] == [widen.id, narrow.id] and inbox.count == 2
    assert svc.guard_mode == "warn"  # nothing changed yet

    with pytest.raises(ConfirmationNeeded, match="live level read → full"):
        await svc.decide_policy_proposal(widen.id, True)
    accepted = await svc.decide_policy_proposal(widen.id, True, confirm_live=True)
    assert accepted.status == "accepted" and svc.global_policy().preset == "trusted"
    assert (await svc.decide_policy_proposal(narrow.id, True)).status == "accepted"
    assert (await svc.effective_policy("acme-shop", "live")).guard_mode == "block"
    with pytest.raises(ConflictError, match="already accepted"):
        await svc.decide_policy_proposal(narrow.id, False)
    kinds = [e.kind for e in await svc.activity()]
    for kind in ("policy.proposed", "policy.proposal_accepted", "policy.updated"):
        assert kind in kinds


async def test_widening_live_needs_a_confirmation(svc: Service) -> None:
    await live_project(svc)
    with pytest.raises(ConfirmationNeeded, match="guard warn → off"):
        await svc.save_policy_layer("acme-shop", '{"guard": {"mode": "off"}}')
    with pytest.raises(ConfirmationNeeded, match="live ssh read → full"):
        await svc.save_policy_layer("global", '{"levels": {"live": {"ssh": "full"}}}')
    await svc.set_preset("strict")  # narrows: no confirmation
    with pytest.raises(ConfirmationNeeded):
        await svc.set_preset("default")
    test = await svc.save_environment("acme-shop", {"name": "test", "kind": "test"})
    assert test.kind == "test"
    # An environment layer of a test environment does not touch live.
    assert await svc.save_policy_layer("acme-shop/test", '{"guard": {"mode": "off"}}') == (
        "acme-shop/test"
    )
    assert await svc.save_policy_layer("acme-shop/test", "") == "acme-shop/test"
    assert await svc.policy_layer_text("acme-shop/test") == ""
    with pytest.raises(ValidationError, match="Missing scope"):
        await svc.save_policy_layer(" ", "{}")


# --- board -----------------------------------------------------------------------------------


def test_settings_policy_page(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, live_project, service)
    page = board.get("/settings/policy")
    assert page.status_code == 200 and "Effective policy" in page.text
    assert "levels.live.default" in page.text and "preset:default" in page.text
    assert 'href="/settings/policy"' in board.get("/settings/integrations").text
    csrf = {"csrf_token": csrf_token(token)}

    bearer = board.post(
        "/settings/policy/preset",
        data={**csrf, "preset": "trusted"},
        headers={**FORM, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403 and "policy_propose" in bearer.text
    confirm = board.post(
        "/settings/policy/preset", data={**csrf, "preset": "trusted"}, headers=FORM
    )
    assert confirm.status_code == 200 and "Confirm and save" in confirm.text
    assert service.global_policy().preset == "default"
    done = board.post(
        "/settings/policy/preset",
        data={**csrf, "preset": "trusted", "confirm_live": "1"},
        headers=FORM,
    )
    assert done.status_code == 200 and "Preset set to trusted" in done.text
    assert service.global_policy().preset == "trusted"
    assert json.loads(service.policy_file.text) == {"preset": "trusted"}

    preview = board.post(
        "/settings/policy/layer",
        data={
            **csrf,
            "scope": "acme-shop",
            "body": '{"guard": {"mode": "loud"}}',
            "action": "preview",
        },
        headers=FORM,
    )
    assert preview.status_code == 400 and "guard.mode" in preview.text
    body = '{"levels": {"live": {"default": "read"}}, "guard": {"mode": "warn"}}'
    preview = board.post(
        "/settings/policy/layer",
        data={**csrf, "scope": "acme-shop", "body": body, "action": "preview"},
        headers=FORM,
    )
    assert preview.status_code == 200 and "Does not widen" in preview.text
    saved = board.post(
        "/settings/policy/layer",
        data={**csrf, "scope": "acme-shop", "body": body, "action": "save"},
        headers=FORM,
    )
    assert saved.status_code == 200 and "Saved the acme-shop policy" in saved.text
    stands = board.get("/p/acme-shop/environments").text
    assert "agents: read (policy: project:acme-shop)" in stands
    page = board.get("/settings/policy?scope=acme-shop/live").text
    assert "project:acme-shop" in page and "acme-shop/live" in page

    proposal = run_in_app(
        board, service.propose_policy_change, "acme-shop", {"guard": {"mode": "block"}}, "Safer"
    )
    assert f"#policy-proposal-{proposal.id}" in board.get("/inbox").text
    assert f'id="policy-proposal-{proposal.id}"' in board.get("/settings/policy").text
    accepted = board.post(
        f"/settings/policy/proposals/{proposal.id}/accept", data=csrf, headers=FORM
    )
    assert accepted.status_code == 200 and "accepted" in accepted.text
    assert run_in_app(board, service.effective_policy, "acme-shop").guard_mode == "block"


def test_test_secrets_are_masked_on_the_board(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    env = run_in_app(board, service.save_environment, "acme-shop", {"name": "qa", "kind": "test"})
    url = f"/p/acme-shop/environments/{env.id}/access"
    form = {"csrf_token": csrf_token(token), "kind": "db", "fields": "host: db.qa.example.com"}
    assert 'name="test_secrets"' not in board.get("/p/acme-shop/environments").text
    refused = board.post(url, data={**form, "test_secrets": f"password: {TEST_PW}"}, headers=FORM)
    assert refused.status_code == 400 and "does not allow test secrets" in refused.text
    run_in_app(board, service.set_preset, "trusted", confirm_live=True)
    assert 'name="test_secrets"' in board.get("/p/acme-shop/environments").text
    added = board.post(
        url,
        data={**form, "test_secrets": f"password: {TEST_PW}"},
        headers=FORM,
        follow_redirects=False,
    )
    assert added.status_code == 303
    page = board.get("/p/acme-shop/environments").text
    assert TEST_PW not in page and "(test secret)" in page and "password: ***" in page


# --- MCP -------------------------------------------------------------------------------------


async def test_mcp_policy_tools(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop")
        text = await call(client, "policy_get")
        assert "# Effective policy: global" in text
        assert '- levels.live.default = "read"  (preset:default)' in text
        saved = await call(
            client,
            "policy_propose",
            scope="acme-shop",
            patch={"guard": {"mode": "block"}},
            reason="Block on this project",
        )
        assert "Policy proposal #1 for acme-shop recorded" in saved
        refused = await client.call_tool(
            "policy_propose",
            {"scope": "acme-shop", "patch": {"guard": {"mode": "x"}}, "reason": "r"},
        )
        assert refused.is_error
        async with httpx.AsyncClient(base_url=live.url, follow_redirects=True) as browser:
            await browser.get(f"/login?token={live.token}")
            accepted = await browser.post(
                "/settings/policy/proposals/1/accept",
                data={"csrf_token": csrf_token(live.token)},
                headers={"Origin": live.url},
            )
            assert accepted.status_code == 200
        text = await call(client, "policy_get", project="acme-shop")
        assert '- guard.mode = "block"  (project:acme-shop)' in text
        missing = await client.call_tool("policy_get", {"project": "acme-shop", "environment": "x"})
        assert missing.is_error


async def test_environments_get_hides_test_secrets_unless_exposed(svc: Service) -> None:
    from relaybay.mcp_server import format as fmt

    await svc.upsert_project("acme-shop")
    write_policy(svc, {"preset": "trusted"})
    env = await svc.save_environment("acme-shop", {"name": "test", "kind": "test"})
    await svc.save_access(
        "acme-shop", env.id, "db", {"host": "db.x", "password": {"value": TEST_PW, "test": True}}
    )

    async def text() -> str:
        proj, items = await svc.environments("acme-shop")
        return fmt.environments_text(
            proj.slug,
            items,
            await svc.environment_policies(items),
            await svc.effective_policy("acme-shop"),
        )

    hidden = await text()
    assert TEST_PW not in hidden and "values not shared with agents" in hidden
    assert "agents: full (policy: preset:trusted)" in hidden and "Guard: log" in hidden
    await project_layer(svc, "acme-shop", {"secrets": {"expose_test_secrets_to_agents": True}})
    assert f"password={TEST_PW}" in await text()


# --- review fixes: hostile patterns, staged layers, guard weakening, the file ------------------

HOSTILE = (r"(a|a)*b", r".*.*.*.*=", r"[a ]*a[a ]*a[a ]*b", r"(a|aa)*!")


@pytest.mark.parametrize("pattern", HOSTILE)
def test_hostile_patterns_are_refused_quickly(pattern: str) -> None:
    started = time.perf_counter()
    for body in (
        {"guard": {"patterns": [{"id": "x1", "change": pattern, "description": "d"}]}},
        {
            "guard": {
                "patterns": [{"id": "x1", "command": pattern, "change": "y", "description": "d"}]
            }
        },
        {"secrets": {"allow": [pattern]}},
    ):
        with pytest.raises(PolicyError, match=r"can be slow|unbounded quantifiers"):
            validate_layer(body, "project acme", "project")
    assert time.perf_counter() - started < 1


def test_slow_patterns_time_out_on_validation_and_on_the_hot_path() -> None:
    slow = r"[a ]*a[a ]*b"  # passes the static checks, backtracks on "a a a ..."
    started = time.perf_counter()
    with pytest.raises(PolicyError, match="too slow"):
        validate_layer({"secrets": {"allow": [slow]}}, "global", "global")
    hot = hot_pattern(slow)
    # Timed out: a guard search fails closed (a match), an allow pattern does not allow.
    assert hot.search("a " * 4000) is not None
    assert hot.fullmatch("a " * 4000) is None
    assert hot.search("ab") is not None and hot.search("xyz") is None  # not switched off
    assert time.perf_counter() - started < 1
    # A stored pattern that does not compile any more is skipped, never raised into the guard.
    layer = {"guard": {"patterns": [{"id": "bad", "change": "([", "description": "d"}]}}
    pol = EffectivePolicy(
        [Layer("preset:default", "preset", preset("default")), Layer("g", "global", layer)]
    )
    assert [r.id for r in pol.rules] == [r.id for r in guard.MUTATING_RULES]


async def test_a_staged_environment_layer_cannot_skip_the_live_confirmation(svc: Service) -> None:
    await svc.upsert_project("acme-shop")
    test = await svc.save_environment("acme-shop", {"name": "stage", "kind": "test"})
    await project_layer(
        svc, "acme-shop/stage", {"guard": {"mode": "off"}, "levels": {"live": {"default": "full"}}}
    )
    with pytest.raises(ConfirmationNeeded, match=r"level full.*guard warn → off"):
        await svc.save_environment("acme-shop", {"kind": "live"}, env_id=test.id)
    proposal = await svc.propose_environment_change(
        "acme-shop", {"environment": "stage", "kind": "live"}, "It is live now"
    )
    with pytest.raises(ConfirmationNeeded):
        await svc.decide_proposal(proposal.id, True)
    moved = await svc.save_environment(
        "acme-shop", {"kind": "live"}, env_id=test.id, confirm_live=True
    )
    assert moved.kind == "live"


async def test_weakening_the_guard_on_live_needs_a_confirmation(svc: Service) -> None:
    await live_project(svc)
    every = list(guard.RULE_IDS)
    with pytest.raises(ConfirmationNeeded, match=r"guard rules off: .*mcp-tool-name"):
        await svc.save_policy_layer("acme-shop", json.dumps({"guard": {"disable": every}}))
    pattern = {"id": "deploy-script", "change": r"deploy\.sh", "description": "deploy"}
    await svc.save_policy_layer("acme-shop", json.dumps({"guard": {"patterns": [pattern]}}))
    with pytest.raises(ConfirmationNeeded, match="guard patterns removed: deploy-script"):
        await svc.save_policy_layer("acme-shop", "{}")
    # Secret detectors are not access: switching one off does not ask.
    await svc.save_policy_layer(
        "acme-shop",
        json.dumps({"guard": {"patterns": [pattern]}, "secrets": {"disable": ["jwt"]}}),
    )


async def test_global_file_errors_never_lose_the_owners_edit(svc: Service, monkeypatch) -> None:
    write_policy(svc, {"guard": {"mode": "block"}})
    assert svc.guard_mode == "block"
    assert svc.policy_file.path is not None
    svc.policy_file.path.write_text('{"guard": ', encoding="utf-8")
    with pytest.raises(PolicyError, match="broken, so it was not changed"):
        await svc.set_preset("strict")
    assert svc.policy_file.path.read_text(encoding="utf-8") == '{"guard": '
    write_policy(svc, {"guard": {"mode": "block"}})

    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(PolicyError, match=r"Could not write the policy file .*Access is denied"):
        await svc.set_preset("strict")


async def test_short_test_secrets_are_masked_in_the_journal(svc: Service) -> None:
    await live_project(svc)
    write_policy(svc, {"preset": "trusted"})
    test = await svc.save_environment("acme-shop", {"name": "test", "kind": "test"})
    await svc.save_access("acme-shop", test.id, "db", {"pin": {"value": "x9q", "test": True}})
    await answer(svc, bash("ssh prod-alias 'unlock x9q'"))
    command = (await svc.live_journal("acme-shop"))[0].payload["command"]
    assert "x9q" not in command and "unlock ***" in command


# --- second review: fail closed, odd flags, changed patterns, reopened proposals -------------


def test_a_timed_out_guard_pattern_counts_as_a_change_and_stays_on() -> None:
    pattern = {
        "id": "prod-x",
        "capability": "kubectl",
        "command": "kubectl",
        "change": "[a-z ]*prod[a-z ]*x",
        "description": "prod x change",
    }
    layer = {"guard": {"patterns": [pattern], "disable": ["kubectl-change"]}}
    validate_layer(layer, "global", "global")  # passes the static checks and the probes
    pol = EffectivePolicy(
        [Layer("preset:default", "preset", preset("default")), Layer("global", "global", layer)]
    )

    def rules(text: str) -> list[str]:
        call = guard.ToolCall("Bash", text, False)
        return [name for name, _ in guard.mutating_rules(call, pol.rules, pol.mcp_tool_names)]

    started = time.perf_counter()
    assert rules("kubectl prod " * 620) == ["prod x change"]  # timed out: fail closed
    assert rules("kubectl prod apply") == []  # fast, no match: the pattern still runs
    assert rules("kubectl prod apply x") == ["prod x change"]
    assert time.perf_counter() - started < 1
    default = EffectivePolicy([Layer("preset:default", "preset", preset("default"))])
    call = guard.ToolCall("Bash", "kubectl prod apply", False)
    assert ("kubectl change", "kubectl") in guard.mutating_rules(call, default.rules)


@pytest.mark.parametrize("bad", ["(?u)(?a)x", "(?V1)a"])
async def test_patterns_with_conflicting_flags_are_refused(
    svc: Service, bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    item = {"id": "odd-flags", "change": bad, "description": "d"}
    with pytest.raises(PolicyError, match=r"pattern 'odd-flags'.*not a valid regular expression"):
        validate_layer({"guard": {"patterns": [item]}}, "global", "global")
    with pytest.raises(PolicyError, match=r"secrets\.allow\[0\]"):
        validate_layer({"secrets": {"allow": [bad]}}, "global", "global")
    await svc.upsert_project("acme-shop")
    with pytest.raises(ValidationError, match="odd-flags"):
        await svc.propose_policy_change("acme-shop", {"guard": {"patterns": [item]}}, "why")
    write_policy(svc, {"guard": {"mode": "block"}})
    assert svc.guard_mode == "block"
    write_policy(svc, {"guard": {"mode": "warn", "patterns": [item]}})
    with caplog.at_level(logging.ERROR, logger="relaybay"):
        assert svc.guard_mode == "block"  # the last good file stays
    assert "odd-flags" in (svc.policy_file.error or "")
    # On the hot path (a layer that skipped validation) the pattern is skipped, not raised.
    pol = EffectivePolicy(
        [
            Layer("preset:default", "preset", preset("default")),
            Layer("g", "global", {"guard": {"patterns": [item]}}),
        ]
    )
    assert "odd-flags" not in [r.id for r in pol.rules]


async def test_a_changed_pattern_with_the_same_id_is_a_weakening(svc: Service) -> None:
    await live_project(svc)
    item = {"id": "deploy-script", "change": r"deploy\.sh", "description": "deploy"}
    await svc.save_policy_layer("acme-shop", json.dumps({"guard": {"patterns": [item]}}))
    weaker = {**item, "change": r"deploy-never\.sh"}
    with pytest.raises(ConfirmationNeeded, match="guard patterns changed: deploy-script"):
        await svc.save_policy_layer("acme-shop", json.dumps({"guard": {"patterns": [weaker]}}))
    other_cap = {**item, "capability": "db"}
    with pytest.raises(ConfirmationNeeded, match="guard patterns changed"):
        await svc.save_policy_layer("acme-shop", json.dumps({"guard": {"patterns": [other_cap]}}))


async def test_a_failed_global_write_reopens_the_proposal(svc: Service, monkeypatch) -> None:
    await svc.upsert_project("acme-shop")
    proposal = await svc.propose_policy_change("global", {"guard": {"mode": "block"}}, "Safer")

    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(Path, "replace", refuse)
    with pytest.raises(PolicyError, match="Could not write the policy file"):
        await svc.decide_policy_proposal(proposal.id, True)
    again = await repo.get_policy_proposal(svc._r, proposal.id)
    assert again is not None and (again.status, again.decided_by) == ("pending", None)
    assert svc.guard_mode == "warn"
    assert "policy.proposal_reopened" in [e.kind for e in await svc.activity()]
    monkeypatch.undo()
    assert (await svc.decide_policy_proposal(proposal.id, True)).status == "accepted"
    assert svc.guard_mode == "block"
