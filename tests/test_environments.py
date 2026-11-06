"""Environments, access map without secrets, permission levels, proposals, guard and journal."""

import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient

from relaybay.config import load_settings
from relaybay.db import repo
from relaybay.db.connection import connect, migrate
from relaybay.domain import guard
from relaybay.domain.environments import (
    DEFAULT_LEVELS,
    AccessEntry,
    Environment,
    check_secret,
    clean_access,
    parse_fields_text,
    redact,
    repo_identifier,
)
from relaybay.domain.errors import ConfirmationNeeded, ConflictError, ValidationError
from relaybay.policy import EffectivePolicy, Layer, preset
from relaybay.security import csrf_token
from relaybay.service import Service, collect_notes
from tests.conftest import BASE, LiveServer, run_in_app
from tests.test_hooks import CC_A, CWD, DESKTOP_A, hook
from tests.test_mcp import call
from tests.test_mcp import connect as mcp_connect
from tests.test_migrations import LATEST, make_old_db

FORM = {"Origin": BASE}
# The default preset with the guard in block mode, as the pure guard tests use it.
BLOCK = EffectivePolicy(
    [
        Layer("preset:default", "preset", preset("default")),
        Layer("config", "config", {"guard": {"mode": "block"}}),
    ]
)
SECRET = "hunter2-very-secret"
TOKEN_LIKE = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2"


async def live_project(svc: Service, **values: Any) -> int:
    """acme-shop (repo at the hook fixture's cwd) with a live environment reached over ssh."""
    await svc.upsert_project("acme-shop", repo_path=CWD)
    env = await svc.save_environment(
        "acme-shop", {"name": "live", "kind": "live", **values}, confirm_live=True
    )
    await svc.save_access("acme-shop", env.id, "ssh", {"alias": "prod-alias", "user": "deploy"})
    return env.id


def bash(command: str, cc_id: str = CC_A) -> dict[str, Any]:
    return hook("PreToolUse", cc_id, tool_input={"command": command})


async def answer(svc: Service, payload: dict[str, Any]) -> dict[str, Any] | None:
    await svc.record_hook(payload)
    return await svc.hook_answer(payload)


def output(result: dict[str, Any] | None) -> dict[str, Any]:
    assert result is not None
    return result["hookSpecificOutput"]


# --- validation: references only ----------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "~/.ssh/id_ed25519",
        "C:\\keys\\acme.pem",
        "$DB_PASSWORD",
        "${DB_PASSWORD}",
        "DB_PASSWORD",
        "env:DB_PASSWORD",
        "vault://kv/acme/db",
        "op://Private/acme-db/password",
        "acme_live.pem",
        "pass:acme/db",
        "file:/etc/acme/db.pass",
        "keychain:acme-db",
    ],
)
def test_secret_fields_accept_references(value: str) -> None:
    check_secret("password_ref", value)
    check_secret("key_path_ref", value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("password_ref", SECRET),
        ("key_path_ref", "s3cr3tpassw0rd"),
        ("token", "abc123def456"),
        ("notes", "-----BEGIN OPENSSH PRIVATE KEY-----\nabc"),
        ("notes", f"the token is {TOKEN_LIKE}"),
        ("url", "https://deploy:hunter2@git.example.com/acme/deploy"),
        ("notes", "db password=hunter2x"),
        ("host", "AKIAABCDEFGHIJKLMNOP"),
        ("notes", "postgres://app:S3cr3tPw@db.acme:5432/x"),
        ("notes", "redis://:S3cr3tPw@redis:6379"),
        ("notes", "mysql -uroot -pS3cr3tPw -h db"),
        ("notes", "PGPASSWORD=S3cr3tPw psql -h db"),
        ("notes", "kubectl create secret generic x --from-literal=password=S3cr3tPw"),
        ("notes", "--password=S3cr3tPw"),
        ("notes", "pass: hunter2xx"),
        ("notes", "Authorization: Bearer abcdefghijklmnop1234567890"),
        ("notes", "Authorization: Basic dXNlcjpwYXNzd29yZA=="),
        ("notes", "curl -u admin:S3cr3tPw https://x"),
        ("notes", "-----BEGIN PRIVATE KEY-----\nabc"),
        ("user", "admin:S3cr3tPw"),
        ("password_ref", "S3CR3TPW"),
        ("password_ref", "pass:hunter2"),
        ("password_ref", "file:hunter2"),
    ],
)
def test_secret_values_are_refused_naming_the_field(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match=name) as error:
        check_secret(name, value)
    assert "never stores secrets" in str(error.value)
    assert value not in str(error.value)


def test_access_fields_per_kind() -> None:
    assert clean_access("ssh", {"alias": "acme-live", "port": 22}) == (
        "ssh",
        {"alias": "acme-live", "port": "22"},
    )
    with pytest.raises(ValidationError, match="no field 'password'"):
        clean_access("ssh", {"password": "x"})
    with pytest.raises(ValidationError, match="Access kind"):
        clean_access("ftp", {"host": "x"})
    with pytest.raises(ValidationError, match="no values"):
        clean_access("kubectl", {"context": " "})
    assert clean_access("other", {"vpn_profile": "acme-office"})[1] == {
        "vpn_profile": "acme-office"
    }
    assert parse_fields_text("alias: acme-live\r\n# comment\nkey-path-ref = ~/.ssh/x\n") == {
        "alias": "acme-live",
        "key_path_ref": "~/.ssh/x",
    }
    with pytest.raises(ValidationError, match="Line 1"):
        parse_fields_text("just text")
    assert repo_identifier("git@git.example.com:acme/deploy.git") == "git.example.com/acme/deploy"
    assert repo_identifier("https://git.example.com/acme/deploy/") == "git.example.com/acme/deploy"


def test_redact_masks_obvious_secrets() -> None:
    text = redact(
        f"PGPASSWORD=hunter2 psql postgres://app:pw@db.example.com/x -H 'Authorization:"
        f" Bearer abc.def' --token {TOKEN_LIKE}"
    )
    for leaked in ("hunter2", ":pw@", "abc.def", TOKEN_LIKE):
        assert leaked not in text
    assert "psql" in text and "db.example.com" in text


@pytest.mark.parametrize(
    ("command", "kept"),
    [
        ("mysql -uroot -pS3cr3tPw -h db", "-h db"),
        ("PGPASSWORD=S3cr3tPw psql -h db", "psql -h db"),
        ("redis-cli -a S3cr3tPw ping", "ping"),
        ("mysqldump --password S3cr3tPw shop", "shop"),
        ("tool --pass=S3cr3tPw", "tool"),
        ("curl -H 'Authorization: Basic dXNlcjpTM2NyM3RQdw==' https://x", "https://x"),
        ("curl -H 'Authorization: Bearer S3cr3tPwToken' https://x", "https://x"),
        ("curl -u admin:S3cr3tPw https://x", "admin:"),
        ("redis-cli -u redis://:S3cr3tPw@h", "@h"),
        ('export DB_PASS="S3cr3tPw a b"', "DB_PASS="),
        ("export API_TOKEN='S3cr3tPw'; GH_SECRET=S3cr3tPw SSH_KEY=S3cr3tPw run", "run"),
        ("psql 'host=db password=S3cr3tPw'", "host=db"),
        ("echo '-----BEGIN RSA PRIVATE KEY-----\nS3cr3tPw\n-----END RSA PRIVATE KEY-----'", "echo"),
    ],
)
def test_redact_masks_the_password_family(command: str, kept: str) -> None:
    text = redact(command)
    assert "S3cr3tPw" not in text and "TM2NyM3RQdw" not in text, text
    assert kept in text and "***" in text


# --- service: defaults, levels, confirmation ----------------------------------------------


async def test_defaults_and_sign_in_flag(service: Service) -> None:
    await service.upsert_project("acme-shop")
    test = await service.save_environment("acme-shop", {"name": "test", "kind": "test"})
    live = await service.save_environment(
        "acme-shop", {"name": "live", "kind": "live", "sign_in_allowed": True}
    )
    assert (test.level, test.effective_level(DEFAULT_LEVELS), test.sign_in_allowed) == (
        None,
        "full",
        True,
    )
    assert (live.effective_level(DEFAULT_LEVELS), live.sign_in_allowed) == ("read", False)
    with pytest.raises(ValidationError, match="already has an environment"):
        await service.save_environment("acme-shop", {"name": "LIVE", "kind": "test"})
    with pytest.raises(ValidationError, match="name and a kind"):
        await service.save_environment("acme-shop", {"name": "staging"})
    with pytest.raises(ValidationError, match="base_url"):
        await service.save_environment(
            "acme-shop", {"name": "x", "kind": "test", "base_url": "javascript:alert(1)"}
        )
    with pytest.raises(ValidationError, match="never stores secrets"):
        await service.save_environment(
            "acme-shop", {"name": "y", "kind": "test", "notes": f"use {TOKEN_LIKE}"}
        )


async def test_raising_live_above_read_needs_a_confirmation(service: Service) -> None:
    await service.upsert_project("acme-shop")
    live = await service.save_environment("acme-shop", {"name": "live", "kind": "live"})
    with pytest.raises(ConfirmationNeeded, match="above read"):
        await service.save_environment("acme-shop", {"level": "full"}, env_id=live.id)
    with pytest.raises(ConfirmationNeeded, match="ssh ask"):
        await service.save_environment("acme-shop", {"overrides": {"ssh": "ask"}}, env_id=live.id)
    lowered = await service.save_environment("acme-shop", {"level": "none"}, env_id=live.id)
    assert lowered.level == "none"
    raised = await service.save_environment(
        "acme-shop",
        {"level": "full", "overrides": {"db": "read"}},
        env_id=live.id,
        confirm_live=True,
    )
    assert (raised.level, raised.overrides) == ("full", {"db": "read"})
    assert raised.effective_level(DEFAULT_LEVELS, "db") == "read"
    cleared = await service.save_environment(
        "acme-shop", {"level": "default", "overrides": {"db": "default"}}, env_id=live.id
    )
    assert (cleared.level, cleared.overrides) == (None, {})


async def test_changing_the_kind_does_not_skip_the_confirmation(service: Service) -> None:
    await service.upsert_project("acme-shop")
    live = await service.save_environment("acme-shop", {"name": "live", "kind": "live"})
    # Away from live with a raised level in the same write: still a raise of live.
    with pytest.raises(ConfirmationNeeded):
        await service.save_environment(
            "acme-shop", {"kind": "test", "level": "full"}, env_id=live.id
        )
    with pytest.raises(ConfirmationNeeded):
        await service.save_environment(
            "acme-shop", {"kind": "test", "overrides": {"ssh": "full"}}, env_id=live.id
        )
    # A test environment with level full that becomes live.
    test = await service.save_environment(
        "acme-shop", {"name": "staging", "kind": "test", "level": "full"}
    )
    with pytest.raises(ConfirmationNeeded):
        await service.save_environment("acme-shop", {"kind": "live"}, env_id=test.id)
    _, items = await service.environments("acme-shop")
    assert [(e.kind, e.level) for e in items] == [("live", None), ("test", "full")]
    # The same through an accepted proposal.
    proposal = await service.propose_environment_change(
        "acme-shop", {"environment": "live", "kind": "test", "level": "full"}, "why"
    )
    with pytest.raises(ConfirmationNeeded):
        await service.decide_proposal(proposal.id, True)
    assert (await service.decide_proposal(proposal.id, True, confirm_live=True)).status == (
        "accepted"
    )


async def test_permission_defaults_from_config(tmp_path: Path) -> None:
    (tmp_path / "config.toml").write_text(
        'guard_mode = "block"\n\n[permission_defaults]\nlive = "full"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="guard_mode"):
        load_settings(home=tmp_path, environ={"RELAYBAY_GUARD_MODE": "loud"})
    settings = load_settings(home=tmp_path, environ={})
    assert settings.permission_defaults == {"local": "full", "test": "full", "live": "full"}
    assert settings.guard_mode == "block"
    assert load_settings(home=tmp_path / "fresh", environ={}).permission_defaults == DEFAULT_LEVELS
    (tmp_path / "config.toml").write_text('[permission_defaults]\nlive = "root"\n', "utf-8")
    with pytest.raises(ValueError, match="permission_defaults"):
        load_settings(home=tmp_path, environ={})
    svc = Service(tmp_path / "x.sqlite", permission_defaults={"live": "full"})
    await svc.start()
    try:
        await svc.upsert_project("acme-shop")
        # The configured default is not a raise by this write: no confirmation.
        live = await svc.save_environment("acme-shop", {"name": "live", "kind": "live"})
        assert live.effective_level(svc.permission_defaults) == "full"
    finally:
        await svc.close()
    with pytest.raises(ValueError, match="permission_defaults"):
        Service(tmp_path / "y.sqlite", permission_defaults={"live": "root"})


# --- proposals ------------------------------------------------------------------------------


async def test_proposals_change_nothing_until_accepted(service: Service) -> None:
    env_id = await live_project(service)
    proposal = await service.propose_environment_change(
        "acme-shop",
        {
            "environment": "LIVE",
            "level": "full",
            "access": {"kind": "kubectl", "fields": {"context": "acme-prod"}},
        },
        "Deploys need kubectl",
        session="dev-1",
    )
    assert (proposal.environment, proposal.proposed_by, proposal.status) == (
        "live",
        "dev-1",
        "pending",
    )
    _, items = await service.environments("acme-shop")
    assert (items[0].level, len(items[0].access)) == (None, 1)
    inbox = await service.inbox(["owner"])
    assert [p.id for p in inbox.proposals] == [proposal.id] and inbox.count == 1

    with pytest.raises(ConfirmationNeeded):
        await service.decide_proposal(proposal.id, True)
    accepted = await service.decide_proposal(proposal.id, True, confirm_live=True)
    assert accepted.status == "accepted" and accepted.decided_by == "board"
    _, items = await service.environments("acme-shop")
    assert items[0].id == env_id and items[0].level == "full"
    assert [a.kind for a in items[0].access] == ["ssh", "kubectl"]
    with pytest.raises(ConflictError, match="already accepted"):
        await service.decide_proposal(proposal.id, False)

    new = await service.propose_environment_change(
        "acme-shop", {"environment": "staging", "kind": "test"}, "A staging stand exists"
    )
    rejected = await service.decide_proposal(new.id, False)
    assert rejected.status == "rejected"
    assert [e.name for e in (await service.environments("acme-shop"))[1]] == ["live"]
    assert (await service.inbox(["owner"])).proposals == []
    kinds = [e.kind for e in await service.activity(project="acme-shop")]
    for kind in (
        "environment.proposed",
        "environment.proposal_accepted",
        "environment.proposal_rejected",
    ):
        assert kind in kinds


async def test_proposal_validation(service: Service) -> None:
    await live_project(service)
    cases = [
        ({"environment": "qa"}, "nothing to change"),
        ({"environment": "qa", "level": "full"}, "needs kind"),
        ({"environment": "live", "colour": "red"}, "Unknown key"),
        ({"environment": "live", "level": "root"}, "level must be one of"),
        ({"environment": "live", "overrides": {"shell": "full"}}, "Unknown capability"),
        (
            {"environment": "live", "access": [{"kind": "db", "fields": {"password_ref": SECRET}}]},
            "never stores secrets",
        ),
    ]
    for change, message in cases:
        with pytest.raises(ValidationError, match=message):
            await service.propose_environment_change("acme-shop", change, "why")
    with pytest.raises(ValidationError, match="reason"):
        await service.propose_environment_change(
            "acme-shop", {"environment": "live", "level": "ask"}, " "
        )
    assert await service.proposals() == []


# --- guard ----------------------------------------------------------------------------------


def test_guard_matches_whole_tokens_and_mutating_heuristics() -> None:
    assert guard.mentions("ssh deploy@prod-alias uptime", "prod-alias")
    assert guard.mentions("curl https://shop.example.com/health.", "shop.example.com")
    assert not guard.mentions("ssh prod-alias-eu uptime", "prod-alias")
    assert not guard.mentions("cd notprod-alias", "prod-alias")
    assert not guard.mentions("ssh my.prod-alias", "prod-alias")
    assert not guard.mentions("ab", "ab")  # too short to match safely

    def rule(text: str) -> str | None:
        found = guard.mutating_rule(guard.ToolCall("Bash", text, False))
        return found[0] if found else None

    assert rule("ssh prod-alias 'systemctl restart app'") == "remote change over ssh"
    assert rule("ssh prod-alias 'systemctl status app'") is None
    assert rule("kubectl --context acme-prod get pods") is None
    assert rule("kubectl --context acme-prod rollout restart deploy/web") == "kubectl change"
    assert rule("psql -h db.example.com -c 'select 1'") is None
    assert rule("psql -h db.example.com -c 'DELETE FROM orders'") == "SQL change"
    assert rule("helm upgrade web ./chart --kube-context acme-prod") == "helm change"
    assert rule("git push git@git.example.com:acme/deploy.git") == "git push"
    mcp = guard.ToolCall("mcp__cloud__restart_service", "acme-prod", True)
    assert guard.mutating_rule(mcp) == ("MCP tool restart_service", None)
    assert (
        guard.tool_call({"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {}})
        is None
    )
    own = {
        "hook_event_name": "PreToolUse",
        "tool_name": "mcp__relaybay__environment_propose",
        "tool_input": {"x": "prod-alias"},
    }
    assert guard.tool_call(own) is None
    # Reads from a host and DB words outside a database client are not changes.
    assert rule("scp acme-live:/var/log/x .") is None
    assert rule("scp build.tgz acme-live:/srv/") == "file copy to a host"
    assert rule("grep -r grant src/") is None
    assert rule("git log --grep truncate") is None
    assert rule("mysql -h db.example.com -e 'GRANT ALL ON x TO y'") == "SQL change"
    assert rule("ssh prod-alias uptime; rm -rf build") is None  # rm is another command


def _live_env() -> Environment:
    env = Environment(1, 1, "live", "live", None, False, None, None, None, {}, 0, "", "", "")
    env.project_slug = "acme"
    env.access = [
        AccessEntry(1, 1, "ssh", {"alias": "app"}, "", "", ""),
        AccessEntry(2, 1, "db", {"host": "postgres"}, "", "", ""),
        AccessEntry(3, 1, "ssh", {"alias": "acme"}, "", "", ""),
        AccessEntry(4, 1, "kubectl", {"context": "acme-prod"}, "", "", ""),
    ]
    return env


def _touches(command: str) -> list[tuple[str, str, str | None]]:
    call = guard.tool_call(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert call is not None
    found = guard.check(call, [_live_env()], lambda env: BLOCK)
    return [(t.identifier, t.decision, t.mutating) for t in found]


@pytest.mark.parametrize(
    "command",
    [
        "npm run app",
        "cat app.py",
        "docker compose up app",
        "ls ./app/",
        "pip install app",
        "psql -h localhost postgres",
        'git commit -m "acme fix" && git push',
    ],
)
def test_single_word_identifiers_match_only_as_a_host(command: str) -> None:
    assert _touches(command) == []


def test_guard_rules_follow_the_identifiers_capability() -> None:
    assert _touches("ssh app uptime; rm -rf build") == [("app", "allow", None)]
    assert _touches("ssh -p 2222 deploy@app 'systemctl restart web'") == [
        ("app", "deny", "remote change over ssh")
    ]
    assert _touches("psql -h postgres -c 'DROP TABLE x'") == [("postgres", "deny", "SQL change")]
    assert _touches("curl https://app/health") == [("app", "allow", None)]
    # A kubectl change aimed at the cluster is not an ssh change of the host named `acme`.
    assert _touches("kubectl --context acme-prod delete pod x && git push") == [
        ("acme-prod", "deny", "kubectl change")
    ]
    # git push concerns deploy, not the ssh host that the commit message mentions.
    assert _touches("ssh acme uptime && git push") == [("acme", "allow", None)]


def test_guard_is_fast_on_long_input() -> None:
    for text in ("ssh " * 5000, "a" * 20_000, "kubectl " * 2500, "mysql -p" * 3000):
        call = guard.tool_call(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": text}}
        )
        assert call is not None and len(call.text) <= guard.TEXT_MAX
        best = float("inf")
        for _ in range(3):  # the best of three: a busy CI runner can stall any single run
            started = time.perf_counter()
            guard.check(call, [_live_env()] * 50, lambda env: BLOCK)
            redact(call.text)
            best = min(best, time.perf_counter() - started)
        assert best < 0.25, text[:10]


@pytest.mark.parametrize(
    ("level", "mode", "command", "decision"),
    [
        (None, "warn", "ssh prod-alias 'systemctl restart app'", "warn"),
        (None, "block", "ssh prod-alias 'systemctl restart app'", "deny"),
        (None, "block", "ssh prod-alias uptime", None),
        ("none", "block", "ssh prod-alias uptime", "deny"),
        ("none", "warn", "ssh prod-alias uptime", "warn"),
        ("ask", "warn", "ssh prod-alias uptime", "ask"),
        ("full", "block", "ssh prod-alias 'rm -rf /tmp/x'", None),
        (None, "off", "ssh prod-alias 'rm -rf /tmp/x'", None),
        (None, "block", "ssh other-host 'rm -rf /tmp/x'", None),
    ],
)
async def test_guard_decisions_per_level_and_mode(
    tmp_path: Path, level: str | None, mode: str, command: str, decision: str | None
) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", guard_mode=mode)
    await svc.start()
    try:
        await live_project(svc, **({"level": level} if level else {}))
        result = await answer(svc, bash(command))
        if decision is None:
            assert result is None
        elif decision == "warn":
            out = output(result)
            assert "permissionDecision" not in out
            assert (
                "read-only" in out["additionalContext"] or "off limits" in out["additionalContext"]
            )
            assert "warn mode: not blocked" in out["additionalContext"]
        else:
            out = output(result)
            assert out["hookEventName"] == "PreToolUse"
            assert out["permissionDecision"] == decision
            assert "acme-shop live (live)" in out["permissionDecisionReason"]
            assert "'prod-alias'" in out["permissionDecisionReason"]
    finally:
        await svc.close()


async def test_capability_override_and_test_environment(service: Service) -> None:
    env_id = await live_project(service)
    await service.save_environment(
        "acme-shop", {"overrides": {"ssh": "full"}}, env_id=env_id, confirm_live=True
    )
    assert await answer(service, bash("ssh prod-alias 'systemctl restart app'")) is None
    test = await service.save_environment(
        "acme-shop", {"name": "test", "kind": "test", "base_url": "https://test.example.com"}
    )
    await service.save_access("acme-shop", test.id, "db", {"host": "db.test.example.com"})
    assert await answer(service, bash("psql -h db.test.example.com -c 'DROP TABLE x'")) is None
    await service.save_environment("acme-shop", {"level": "none"}, env_id=test.id)
    out = output(await answer(service, bash("curl https://test.example.com/api")))
    assert "off limits" in out["additionalContext"]


async def test_guard_applies_to_the_sessions_project_only(service: Service) -> None:
    await live_project(service)
    other = hook(
        "PreToolUse",
        "cc-other",
        cwd="/srv/elsewhere",
        tool_input={"command": "ssh prod-alias 'rm x'"},
    )
    assert await answer(service, other) is None
    mcp = hook(
        "PreToolUse",
        tool_name="mcp__cloud__restart_service",
        tool_input={"target": {"host": "prod-alias"}},
    )
    assert "MCP tool restart_service" in output(await answer(service, mcp))["additionalContext"]


def test_mentions_prefilter_changes_no_answer() -> None:
    # A single word goes by the host only, after quotes are stripped: never prefiltered.
    assert guard.mentions('ssh pr"o"d uptime', "prod")
    assert guard.mentions("ssh PROD uptime", "prod")
    long_name = "api-" + "x" * 300 + "-prod.example.com"
    assert guard.mentions(f"curl https://{long_name.upper()}/health", long_name)
    assert not guard.mentions(f"curl https://{long_name}.eu/health", long_name)
    assert not guard.mentions("curl https://other.example.com/health", long_name)
    # Past the regex cache an absent identifier compiles nothing.
    guard._identifier_re.cache_clear()
    for number in range(2000):
        assert not guard.mentions("ls -la", f"host-{number}.example.com")
    assert guard._identifier_re.cache_info().misses == 0


# --- "no live now" ----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["warn", "log", "block", "off"])
@pytest.mark.parametrize("level", ["read", "full"])
@pytest.mark.parametrize("command", ["ssh prod-alias uptime", "ssh prod-alias 'rm -rf /x'"])
async def test_no_live_denies_under_every_mode_and_level(
    tmp_path: Path, mode: str, level: str, command: str
) -> None:
    svc = Service(tmp_path / "relaybay.sqlite", guard_mode=mode)
    await svc.start()
    try:
        await live_project(svc, level=level)
        await svc.create_task(
            "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
        )
        session = (await svc.list_sessions())[0].session
        await svc.set_session_no_live(session.id, True, actor="owner")

        out = output(await answer(svc, bash(command)))
        assert out["permissionDecision"] == "deny"
        assert "the owner switched live off for this session" in out["permissionDecisionReason"]
        action = (await svc.live_journal("acme-shop"))[0]
        assert (action.payload["decision"], action.payload["level"], action.payload["rule"]) == (
            "deny",
            "none",
            guard.NO_LIVE_RULE,
        )
        assert guard.NO_LIVE_RULE not in guard.RULE_IDS  # a policy cannot switch it off

        await svc.set_session_no_live(session.id, False, actor="owner")
        again = await answer(svc, bash(command))
        if again is not None and "permissionDecision" in again["hookSpecificOutput"]:
            assert (
                "switched live off" not in again["hookSpecificOutput"]["permissionDecisionReason"]
            )
    finally:
        await svc.close()


async def test_no_live_leaves_other_stands_and_commands_alone(service: Service) -> None:
    await live_project(service)
    env = await service.save_environment("acme-shop", {"name": "test", "kind": "test"})
    await service.save_access("acme-shop", env.id, "ssh", {"alias": "test-alias", "user": "ci"})
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    session = (await service.list_sessions())[0].session
    await service.set_session_no_live(session.id, True, actor="owner")
    assert await answer(service, bash("ssh test-alias uptime")) is None
    assert await answer(service, bash("npm test")) is None
    assert await service.live_journal("acme-shop") == []
    # A command naming both: the deny on live wins and only live is journaled.
    both = output(await answer(service, bash("ssh test-alias uptime; ssh prod-alias uptime")))
    assert both["permissionDecision"] == "deny"
    assert [e.payload["environment"] for e in await service.live_journal("acme-shop")] == ["live"]


async def test_no_live_reaches_live_stands_outside_the_sessions_project(service: Service) -> None:
    await live_project(service)
    await answer(service, hook("PreToolUse", "cc-other", cwd="/srv/elsewhere", tool_input={}))
    other = next(
        r.session for r in await service.list_sessions() if r.session.cc_session_id == "cc-other"
    )
    command = {"command": "ssh prod-alias uptime"}
    away = hook("PreToolUse", "cc-other", cwd="/srv/elsewhere", tool_input=command)
    assert await answer(service, away) is None  # not this session's project: not its stand
    await service.set_session_no_live(other.id, True, actor="owner")
    assert output(await answer(service, away))["permissionDecision"] == "deny"
    journal = await service.live_journal("acme-shop")
    assert journal[0].session_id == other.id


async def test_no_live_survives_the_guessed_row_being_merged(service: Service) -> None:
    await live_project(service)
    guessed = await service.record_hook(hook("SessionStart"))
    assert guessed is not None and guessed.kind == "hook"
    await service.set_session_no_live(guessed.id, True, actor="owner")
    assert output(await answer(service, bash("ssh prod-alias uptime")))["permissionDecision"] == (
        "deny"
    )

    # The agent's own write links the Claude Code id to its desktop session: the row goes away.
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    rows = [r.session for r in await service.list_sessions()]
    assert [s.external_id is not None for s in rows] == [True]
    assert rows[0].no_live_by == "owner" and rows[0].no_live_at == (await _on_event(service)).ts
    out = output(await answer(service, bash("ssh prod-alias uptime")))
    assert out["permissionDecision"] == "deny"
    carried = [e for e in await service.events_since(0) if e.kind == "session.no_live_on"][-1]
    assert carried.payload["carried_from"] == guessed.name
    assert carried.session_id == rows[0].id


async def test_no_live_merge_keeps_the_desktop_rows_switch(service: Service) -> None:
    await live_project(service)
    await service.create_task("acme-shop", "Restart", stage="dev", session=DESKTOP_A)
    desktop = (await service.list_sessions())[0].session
    await service.set_session_no_live(desktop.id, True, actor="owner")
    guessed = hook("SessionStart", "cc-guessed", cwd="/srv/elsewhere")
    await service.record_hook(guessed)
    await service.create_task(
        "acme-shop", "Second", stage="dev", session=DESKTOP_A, hook_session="cc-guessed"
    )
    row = next(r.session for r in await service.list_sessions() if r.session.id == desktop.id)
    assert row.no_live_at is not None and row.cc_session_id == "cc-guessed"
    away = hook(
        "PreToolUse", "cc-guessed", cwd="/srv/elsewhere", tool_input={"command": "ssh prod-alias x"}
    )
    assert output(await answer(service, away))["permissionDecision"] == "deny"


async def test_no_live_merge_clears_the_row_that_stays(service: Service) -> None:
    await live_project(service)
    # A row with a binding is kept by the merge; it hands the switch over and keeps none.
    await service.create_task("acme-shop", "Bound", stage="dev", session="named-agent")
    await service.record_hook(hook("SessionStart", "cc-named", cwd="/srv/elsewhere"))
    guessed = await repo.get_session_by_cc(service._r, "cc-named")
    named = await repo.get_session_by_name(service._r, "named-agent")
    await service._write_conn.execute(
        "UPDATE task_session SET session_id = ? WHERE session_id = ?", (guessed.id, named.id)
    )
    await service.set_session_no_live(guessed.id, True, actor="owner")
    await service.create_task(
        "acme-shop", "Link", stage="dev", session=DESKTOP_A, hook_session="cc-named"
    )
    old = await repo.get_session_by_id(service._r, guessed.id)
    assert old is not None and old.no_live_at is None and old.cc_session_id is None
    new = await repo.get_session_by_cc(service._r, "cc-named")
    assert new is not None and new.external_id == DESKTOP_A and new.no_live_by == "owner"


async def test_no_live_an_agents_write_cannot_move_the_row(service: Service) -> None:
    await live_project(service)
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    row = (await service.list_sessions())[0].session
    await service.set_session_no_live(row.id, True, actor="owner")

    with collect_notes() as notes:
        await service.create_task(
            "acme-shop", "Escape", stage="dev", session=DESKTOP_A, hook_session="bogus"
        )
    assert any("hook_session bogus not linked" in n for n in notes)
    kept = await repo.get_session_by_id(service._r, row.id)
    assert kept is not None and kept.cc_session_id == CC_A and kept.no_live_at is not None
    assert output(await answer(service, bash("ssh prod-alias uptime")))["permissionDecision"] == (
        "deny"
    )


async def test_no_live_a_made_up_id_does_not_move_a_guessed_row(service: Service) -> None:
    await live_project(service)
    guessed = await service.record_hook(hook("SessionStart"))
    assert guessed is not None and not guessed.cc_exact
    await service.set_session_no_live(guessed.id, True, actor="owner")
    with collect_notes() as notes:
        await service.create_task(
            "acme-shop", "Escape", stage="dev", session=guessed.name, hook_session="made-up"
        )
    assert any("hook_session made-up not linked" in n for n in notes)
    kept = await repo.get_session_by_id(service._r, guessed.id)
    assert kept is not None and kept.cc_session_id == CC_A and kept.no_live_at is not None
    assert output(await answer(service, bash("ssh prod-alias uptime")))["permissionDecision"] == (
        "deny"
    )


async def test_no_live_an_ended_exact_row_moves_to_a_real_new_id(service: Service) -> None:
    await live_project(service)
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    row = (await service.list_sessions())[0].session
    await service.set_session_no_live(row.id, True, actor="owner")
    await service.record_hook(hook("SessionEnd", CC_A))
    # A /clear in a terminal: no host id, so the hooks make a row of its own for the new id.
    fresh = await service.record_hook(hook("SessionStart", "cc-real", cwd="/srv/elsewhere"))
    assert fresh is not None and fresh.id != row.id and fresh.no_live_at is None
    await service.add_report(
        "acme-shop-1", "changed · S · on", session=DESKTOP_A, hook_session="cc-real"
    )
    moved = await repo.get_session_by_id(service._r, row.id)
    assert moved is not None and moved.cc_session_id == "cc-real" and moved.no_live_at is not None
    away = hook(
        "PreToolUse", "cc-real", cwd="/srv/elsewhere", tool_input={"command": "ssh prod-alias x"}
    )
    assert output(await answer(service, away))["permissionDecision"] == "deny"


async def test_no_live_an_ended_exact_row_refuses_a_made_up_id(service: Service) -> None:
    await live_project(service)
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    row = (await service.list_sessions())[0].session
    await service.set_session_no_live(row.id, True, actor="owner")
    await service.record_hook(hook("SessionEnd", CC_A))
    await service.add_report(
        "acme-shop-1", "changed · S · on", session=DESKTOP_A, hook_session="made-up"
    )
    kept = await repo.get_session_by_id(service._r, row.id)
    assert kept is not None and kept.cc_session_id == CC_A


async def test_no_live_an_unlinked_row_stays_unlinked_on_a_made_up_id(service: Service) -> None:
    await live_project(service)
    await service.create_task("acme-shop", "Named", stage="dev", session="named-agent")
    row = (await service.list_sessions())[0].session
    assert row.cc_session_id is None
    await service.set_session_no_live(row.id, True, actor="owner")
    with collect_notes() as notes:
        await service.add_report(
            "acme-shop-1", "changed · S · on", session="named-agent", hook_session="made-up"
        )
    assert any("not linked" in n for n in notes)
    kept = await repo.get_session_by_id(service._r, row.id)
    assert kept is not None and kept.cc_session_id is None and kept.no_live_at is not None
    # A real id the hooks saw still links it, and the switch then applies.
    await service.record_hook(hook("SessionStart", "cc-real", cwd="/srv/elsewhere"))
    await service.add_report(
        "acme-shop-1", "changed · S · two", session="named-agent", hook_session="cc-real"
    )
    linked = await repo.get_session_by_id(service._r, row.id)
    assert linked is not None and linked.cc_session_id == "cc-real"


async def test_no_live_stays_through_a_clear_in_the_desktop_window(service: Service) -> None:
    await live_project(service)
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    row = (await service.list_sessions())[0].session
    await service.set_session_no_live(row.id, True, actor="owner")
    await service.record_hook(hook("SessionStart", CC_A))
    cleared = hook(
        "SessionStart",
        "cc-after-clear",
        source="clear",
        relaybay_host_session=row.external_id,
        relaybay_host_attended="1",
    )
    await service.record_hook(cleared)
    moved = await repo.get_session_by_id(service._r, row.id)
    assert moved is not None and moved.cc_session_id == "cc-after-clear"
    assert moved.no_live_at is not None
    assert output(await answer(service, bash("ssh prod-alias uptime", "cc-after-clear")))[
        "permissionDecision"
    ] == ("deny")


async def test_no_live_ends_with_the_session_when_its_folder_row_is_reused(
    service: Service,
) -> None:
    await live_project(service)
    first = await service.record_hook(hook("SessionStart", CC_A))
    assert first is not None
    await service.set_session_no_live(first.id, True, actor="owner")
    await service.record_hook(hook("SessionEnd", CC_A))
    reused = await service.record_hook(hook("SessionStart", "cc-next"))
    assert reused is not None and reused.id == first.id and reused.no_live_at is None
    off = [e for e in await service.events_since(0) if e.kind == "session.no_live_off"]
    assert [e.actor for e in off] == ["relaybay"] and "new one" in off[0].payload["reason"]
    out = output(await answer(service, bash("ssh prod-alias 'rm x'", "cc-next")))
    assert "permissionDecision" not in out  # back to warn mode


async def test_no_live_set_on_an_unlinked_folder_row_survives_its_linking(
    service: Service,
) -> None:
    await live_project(service)
    first = await service.record_hook(hook("SessionStart", CC_A))
    assert first is not None
    await service._write_conn.execute(
        "UPDATE session SET cc_session_id = NULL, status = NULL WHERE id = ?", (first.id,)
    )
    await service.set_session_no_live(first.id, True, actor="owner")
    reused = await service.record_hook(hook("SessionStart", "cc-next"))
    assert reused is not None and reused.id == first.id and reused.no_live_at is not None
    assert output(await answer(service, bash("ssh prod-alias uptime", "cc-next")))[
        "permissionDecision"
    ] == ("deny")


async def test_a_failing_journal_does_not_lose_the_decision(
    service: Service, monkeypatch, caplog
) -> None:
    await live_project(service)
    await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    row = (await service.list_sessions())[0].session
    await service.set_session_no_live(row.id, True, actor="owner")

    async def broken(*_: Any) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(service, "_journal", broken)
    assert output(await answer(service, bash("ssh prod-alias uptime")))["permissionDecision"] == (
        "deny"
    )
    assert "Writing the live journal failed" in caplog.text


async def _on_event(service: Service) -> Any:
    return next(e for e in await service.events_since(0) if e.kind == "session.no_live_on")


async def test_guard_failure_lets_the_call_through(service: Service, monkeypatch) -> None:
    await live_project(service)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "check", broken)
    assert await answer(service, bash("ssh prod-alias 'systemctl restart app'")) is None


# --- journal --------------------------------------------------------------------------------


async def test_live_actions_are_journaled_on_the_bound_task(service: Service) -> None:
    await live_project(service)
    task = await service.create_task(
        "acme-shop", "Restart", stage="dev", session=DESKTOP_A, hook_session=CC_A
    )
    await answer(service, bash(f"PGPASSWORD={SECRET} ssh prod-alias 'systemctl restart app'"))
    await answer(service, bash("ssh prod-alias uptime"))
    await answer(service, bash("npm test"))
    journal = await service.live_journal("acme-shop")
    assert [e.payload["decision"] for e in journal] == ["allow", "warn"]
    first = journal[-1]
    assert (first.task_id, first.payload["key"], first.payload["environment"]) == (
        task.id,
        task.key,
        "live",
    )
    assert first.payload["tool"] == "Bash" and first.payload["rule"] == "remote change over ssh"
    assert (
        SECRET not in first.payload["command"]
        and "systemctl restart app" in first.payload["command"]
    )
    detail = await service.task_detail(task.key)
    assert [e.id for e in detail.live_actions] == [e.id for e in journal]


async def test_common_words_leave_no_journal(service: Service) -> None:
    env_id = await live_project(service)
    await service.save_access("acme-shop", env_id, "ssh", {"alias": "app"})
    for command in ("npm run app", "ls ./app/", 'git commit -m "app fix" && git push'):
        assert await answer(service, bash(command)) is None
    assert await service.live_journal("acme-shop") == []
    await answer(service, bash("ssh app uptime"))
    assert [e.payload["decision"] for e in await service.live_journal("acme-shop")] == ["allow"]


async def test_hook_endpoint_answers_with_the_decision(client: TestClient, token: str) -> None:
    service: Service = client.app.state.service
    run_in_app(client, live_project, service)
    auth = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/hooks/claude-code", json=bash("ssh prod-alias 'systemctl restart app'"), headers=auth
    )
    assert response.status_code == 200
    out = response.json()["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and "permissionDecision" not in out
    assert "read-only for agents" in out["additionalContext"]
    quiet = client.post("/hooks/claude-code", json=bash("npm test"), headers=auth)
    assert quiet.status_code == 204


# --- migration ------------------------------------------------------------------------------


async def test_upgrade_from_0008_adds_environments(tmp_path: Path) -> None:
    db = tmp_path / "relaybay.sqlite"
    await make_old_db(db, version=8)
    svc = Service(db)
    await svc.start()
    try:
        assert (await svc.environments("acme-shop"))[1] == []
        env = await svc.save_environment("acme-shop", {"name": "live", "kind": "live"})
        assert env.effective_level(svc.permission_defaults) == "read"
        assert (await svc.task_detail("acme-shop-1")).live_actions == []
    finally:
        await svc.close()
    conn = await connect(db)
    try:
        assert await migrate(conn) == LATEST
        with pytest.raises(Exception, match="CHECK"):
            await conn.execute("UPDATE environment SET sign_in_allowed = 1")
        async with conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'") as cur:
            indexes = {row[0] for row in await cur.fetchall()}
        assert {"event_kind", "event_task_kind"} <= indexes
    finally:
        await conn.close()


# --- board ------------------------------------------------------------------------------------


def test_stands_page_writes_need_the_board(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, service.upsert_project, "acme-shop", name="Acme Shop")
    page = board.get("/p/acme-shop/environments")
    assert page.status_code == 200 and "Stands &amp; access" in page.text
    assert 'href="/p/acme-shop/environments"' in board.get("/p/acme-shop").text

    form = {"csrf_token": csrf_token(token), "name": "live", "kind": "live", "level": "default"}
    assert (
        board.post(
            "/p/acme-shop/environments", data={**form, "csrf_token": "x"}, headers=FORM
        ).status_code
        == 403
    )
    bearer = board.post(
        "/p/acme-shop/environments",
        data=form,
        headers={**FORM, "Authorization": f"Bearer {token}"},
    )
    assert bearer.status_code == 403 and "environment_propose" in bearer.text

    created = board.post(
        "/p/acme-shop/environments", data=form, headers=FORM, follow_redirects=False
    )
    assert created.status_code == 303
    env = run_in_app(board, service.environments, "acme-shop")[1][0]
    access = board.post(
        f"/p/acme-shop/environments/{env.id}/access",
        data={
            "csrf_token": csrf_token(token),
            "kind": "ssh",
            "fields": "alias: prod-alias\nuser: deploy",
        },
        headers=FORM,
        follow_redirects=False,
    )
    assert access.status_code == 303
    refused = board.post(
        f"/p/acme-shop/environments/{env.id}/access",
        data={"csrf_token": csrf_token(token), "kind": "db", "fields": f"password_ref: {SECRET}"},
        headers=FORM,
    )
    assert refused.status_code == 400 and "never stores secrets" in refused.text
    assert SECRET not in refused.text

    raise_form = {**form, "level": "full"}
    confirm = board.post(f"/p/acme-shop/environments/{env.id}", data=raise_form, headers=FORM)
    assert confirm.status_code == 200 and "Confirm and save" in confirm.text
    assert run_in_app(board, service.environments, "acme-shop")[1][0].level is None
    done = board.post(
        f"/p/acme-shop/environments/{env.id}",
        data={**raise_form, "confirm_live": "1"},
        headers=FORM,
        follow_redirects=False,
    )
    assert done.status_code == 303
    page = board.get("/p/acme-shop/environments").text
    assert "agents: full" in page and "prod-alias" in page


def test_proposals_on_the_board_and_in_the_inbox(board: TestClient, token: str) -> None:
    service: Service = board.app.state.service
    run_in_app(board, live_project, service)
    proposal = run_in_app(
        board,
        service.propose_environment_change,
        "acme-shop",
        {"environment": "live", "notes": "Read logs at https://logs.example.com"},
        "Logs are useful <script>x()</script>",
        session="dev-1",
    )
    inbox = board.get("/inbox").text
    assert "Proposed by agents" in inbox and "&lt;script&gt;" in inbox
    assert f"#proposal-{proposal.id}" in inbox
    page = board.get("/p/acme-shop/environments").text
    assert f'id="proposal-{proposal.id}"' in page
    url = f"/p/acme-shop/proposals/{proposal.id}/accept"
    assert board.post(url, data={}, headers=FORM).status_code == 403
    accepted = board.post(
        url, data={"csrf_token": csrf_token(token)}, headers=FORM, follow_redirects=False
    )
    assert accepted.status_code == 303
    assert "logs.example.com" in run_in_app(board, service.environments, "acme-shop")[1][0].notes
    again = board.post(url, data={"csrf_token": csrf_token(token)}, headers=FORM)
    assert again.status_code == 400 and "already accepted" in again.text
    assert (
        board.post(
            "/p/demo-api/proposals/1/accept", data={"csrf_token": csrf_token(token)}, headers=FORM
        ).status_code
        == 404
    )


def test_journal_on_activity_and_task(board: TestClient) -> None:
    service: Service = board.app.state.service
    run_in_app(board, live_project, service)
    task = run_in_app(
        board,
        service.create_task,
        "acme-shop",
        "Restart",
        stage="dev",
        session=DESKTOP_A,
        hook_session=CC_A,
    )
    run_in_app(
        board, answer, service, bash(f"ssh prod-alias 'systemctl restart app' --token={SECRET}")
    )
    activity = board.get("/activity").text
    assert "Actions on live" in activity and "systemctl restart app" in activity
    assert SECRET not in activity
    assert "systemctl restart app" in board.get("/fragments/journal?project=acme-shop").text
    assert "live.action" in board.get("/activity?kind=live.action").text
    view = board.get(f"/p/acme-shop/t/{task.key}/view").text
    assert "Actions on live" in view and "warn" in view


# --- MCP --------------------------------------------------------------------------------------


async def test_mcp_environment_tools(live_server: Callable[[], LiveServer]) -> None:
    live = live_server()
    async with mcp_connect(live) as client:
        await call(client, "project_upsert", slug="acme-shop")
        empty = await call(client, "environments_get", project="acme-shop")
        assert "No environments for acme-shop" in empty
        secret_change = {
            "environment": "live",
            "kind": "live",
            "access": [{"kind": "db", "fields": {"password_ref": SECRET}}],
        }
        refused = await client.call_tool(
            "environment_propose",
            {"project": "acme-shop", "change": secret_change, "reason": "db"},
        )
        assert refused.is_error
        text = "\n".join(c.text for c in refused.content if hasattr(c, "text"))
        assert "never stores secrets" in text and SECRET not in text
        change = {
            "environment": "live",
            "kind": "live",
            "base_url": "https://shop.example.com",
            "access": [{"kind": "ssh", "fields": {"alias": "prod-alias"}}],
        }
        saved = await call(
            client,
            "environment_propose",
            project="acme-shop",
            change=change,
            reason="The shop runs there",
            session="dev-1",
        )
        assert "Nothing changed yet" in saved
        assert "No environments" in await call(client, "environments_get", project="acme-shop")

        # The owner accepts it on the board, signed in with the cookie.
        async with httpx.AsyncClient(base_url=live.url, follow_redirects=True) as browser:
            await browser.get(f"/login?token={live.token}")
            accepted = await browser.post(
                "/p/acme-shop/proposals/1/accept",
                data={"csrf_token": csrf_token(live.token)},
                headers={"Origin": live.url},
            )
            assert accepted.status_code == 200 and "prod-alias" in accepted.text

        full = await call(client, "environments_get", project="acme-shop")
        assert "## live (live)" in full and "agents: read (default)" in full
        assert "ssh: alias=prod-alias" in full and "Guard: warn" in full
        snapshot = await call(client, "project_snapshot", project="acme-shop")
        assert "- live — live · agents: read (default) · https://shop.example.com" in snapshot
        resource = await client.read_resource("relaybay://project/acme-shop/environments")
        assert "prod-alias" in resource.contents[0].text
        events = await call(client, "events_since", project="acme-shop")
        assert "environment.proposed" in events and "environment.proposal_accepted" in events


# --- second review: hosts as shell words, the password family, kind changes --------------


async def test_leaving_live_compares_effective_levels(service: Service) -> None:
    await service.upsert_project("acme-shop")
    live = await service.save_environment("acme-shop", {"name": "live", "kind": "live"})
    for values in (
        {"kind": "test"},
        {"kind": "local"},
        {"kind": "test", "level": None},
        {"level": "ask"},
        {"overrides": {"db": "full"}},
    ):
        with pytest.raises(ConfirmationNeeded):
            await service.save_environment("acme-shop", values, env_id=live.id)
    proposal = await service.propose_environment_change(
        "acme-shop", {"environment": "live", "kind": "test"}, "why"
    )
    with pytest.raises(ConfirmationNeeded):
        await service.decide_proposal(proposal.id, True)
    # Lowering on the way out of live needs nothing.
    lowered = await service.save_environment(
        "acme-shop", {"kind": "test", "level": "read"}, env_id=live.id
    )
    assert (lowered.kind, lowered.level) == ("test", "read")


def _probe_env() -> Environment:
    env = Environment(1, 1, "live", "live", None, False, None, None, None, {}, 0, "", "", "")
    env.project_slug = "acme"
    env.access = [
        AccessEntry(1, 1, "ssh", {"alias": "app"}, "", "", ""),
        AccessEntry(2, 1, "ssh", {"alias": "prod-alias"}, "", "", ""),
        AccessEntry(3, 1, "db", {"host": "postgres"}, "", "", ""),
        AccessEntry(4, 1, "kubectl", {"context": "prod"}, "", "", ""),
    ]
    return env


def _probe(command: str) -> list[tuple[str, str, str | None]]:
    call = guard.tool_call(
        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}
    )
    assert call is not None
    found = guard.check(call, [_probe_env()], lambda env: BLOCK)
    return [(t.identifier, t.decision, t.mutating) for t in found]


SSH_CHANGE = "remote change over ssh"
COPY = "file copy to a host"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("ssh -p 2222 app rm -rf /x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -i ~/.ssh/k deploy@app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -o StrictHostKeyChecking=no app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -o X=Y -p 22 app sudo reboot", [("app", "deny", SSH_CHANGE)]),
        ("ssh -A app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -l deploy app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -tt app sudo x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -J bastion app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh app -- rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -p2222 app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -vvv -p 22 -i k app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -F cfg app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -oStrictHostKeyChecking=no app rm x", [("app", "deny", SSH_CHANGE)]),
        ("ssh -p 2222 prod-alias rm x", [("prod-alias", "deny", SSH_CHANGE)]),
        ("ssh -i key user@prod-alias rm x", [("prod-alias", "deny", SSH_CHANGE)]),
        ("kubectl --context prod delete pod x", [("prod", "deny", "kubectl change")]),
        ("kubectl --context=prod delete pod x", [("prod", "deny", "kubectl change")]),
        ("kubectl delete pod x --context prod", [("prod", "deny", "kubectl change")]),
        ("psql -h postgres -c 'drop table x'", [("postgres", "deny", "SQL change")]),
        ("psql -hpostgres -c 'drop table x'", [("postgres", "deny", "SQL change")]),
        ("psql --host=postgres -c 'drop table x'", [("postgres", "deny", "SQL change")]),
        (
            "psql postgresql://u@postgres/db -c 'delete from t'",
            [("postgres", "deny", "SQL change")],
        ),
        ("psql -U postgres -h localhost -c 'drop table x'", []),
        ('git commit -m "acme fix" && git push', []),
        ("npm run app", []),
        ("ls ./app/", []),
        ("ssh app uptime; rm -rf build", [("app", "allow", None)]),
        ("ssh app 'uptime; rm -rf /'", [("app", "deny", SSH_CHANGE)]),
        ('ssh app "sudo reboot"', [("app", "deny", SSH_CHANGE)]),
        ("scp ./x app:/tmp/", [("app", "deny", COPY)]),
        ("scp app:/var/log/x .", [("app", "allow", None)]),
        ("rsync -a app:/x ./y", [("app", "allow", None)]),
        ("rsync -a ./y app:/x", [("app", "deny", COPY)]),
        ("scp -P 22 ./x deploy@app:/tmp", [("app", "deny", COPY)]),
        ("scp ./x prod-alias:/tmp/", [("prod-alias", "deny", COPY)]),
        ("scp ./x prod-alias:", [("prod-alias", "deny", COPY)]),
        ("echo ssh app; rm x", []),
        ("cat ~/.ssh/config | grep app", []),
        ("ssh app && psql -h postgres -c 'drop table x'", [("postgres", "deny", "SQL change")]),
        ("mysql -h postgres -e 'drop table x'", [("postgres", "deny", "SQL change")]),
        ("ssh -h", []),
        ("curl https://app/api -X DELETE", [("app", "allow", None)]),
    ],
)
def test_guard_finds_hosts_in_shell_words(
    command: str, expected: list[tuple[str, str, str | None]]
) -> None:
    assert _probe(command) == expected


@pytest.mark.parametrize(
    ("command", "masked"),
    [
        ("mysql -u root --password=hunter22 db", "mysql -u root --password=*** db"),
        ("mysql -u root -p'hunter 22' db", "mysql -u root -p'***' db"),
        ('mysql -p"a b" db', 'mysql -p"***" db'),
        ("curl -H 'Authorization: Token abcdefgh' x", "curl -H 'Authorization: Token ***' x"),
        ('curl -H "X-Api-Key: abcdef123456" x', 'curl -H "X-Api-Key: ***" x'),
        ("export API_KEY='abc def'", "export API_KEY='***'"),
        ("AWS_SECRET_ACCESS_KEY=abcd1234 aws s3 ls", "AWS_SECRET_ACCESS_KEY=*** aws s3 ls"),
        ("git clone https://user:tok3n@github.com/x", "git clone https://user:***@github.com/x"),
        ('psql "postgres://:pw@h/db"', 'psql "postgres://:***@h/db"'),
        ("sshpass -p hunter22 ssh app", "sshpass -p *** ssh app"),
        ("redis-cli -h r -a hunter22 ping", "redis-cli -h r -a *** ping"),
        ("redis-cli --pass hunter22", "redis-cli --pass ***"),
        ("PGPASSWORD='a b' psql", "PGPASSWORD='***' psql"),
        ("mysql -phunter22", "mysql -p***"),
        ("docker login -u me -p hunter22", "docker login -u me -p ***"),
        ("docker login -u me --password hunter22", "docker login -u me --password ***"),
        ("--token=abc", "--token=***"),
        ("curl -u admin:pw https://x", "curl -u admin:*** https://x"),
        ("curl --user admin:pw x", "curl --user admin:*** x"),
        (r"psql 'password=a\ b'", "psql 'password=***'"),
        ('client_secret: "abc def"', 'client_secret: "***"'),
    ],
)
def test_redaction_masks_whole_values(command: str, masked: str) -> None:
    assert redact(command) == masked


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("notes", "sshpass -p hunter22 ssh app"),
        ("notes", "docker login -u me -p hunter22"),
        ("notes", "redis-cli -a hunter22"),
        ("notes", "curl -u admin:pw x"),
        ("notes", "--token abcdefgh"),
        ("notes", "--password hunter22"),
        ("notes", "--pass hunter22"),
        ("notes", "X-Api-Key: abcdef123456"),
        ("notes", "API_KEY='abc def'"),
        ("notes", "Authorization: Token abcdefgh123"),
        ("password_ref", "pass:/"),
        ("password_ref", "sops:x/"),
    ],
)
def test_validation_refuses_what_redaction_masks(name: str, value: str) -> None:
    with pytest.raises(ValidationError, match="secret"):
        check_secret(name, value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("password_ref", "pass:a/b"),
        ("password_ref", "file:./k"),
        ("notes", 'secret_key = ""'),
        ("notes", "--password $DB_PASSWORD"),
        ("notes", "echo x | docker login --password-stdin"),
    ],
)
def test_validation_keeps_references(name: str, value: str) -> None:
    check_secret(name, value)


@pytest.mark.parametrize(
    "text",
    [
        "ssh -p " * 3000,
        "ssh -o x " * 2000,
        "a://" * 3000,
        "x" * 8000 + "@",
        "'" * 9000,
        "ssh -a" * 2000,
        "psql " + "insert " * 2000,
        "-----BEGIN PRIVATE KEY-----" + " a" * 4000,
        "pass=" * 3000,
        "u:" * 5000 + "@",
    ],
)
def test_guard_redaction_and_validation_are_fast_on_adversarial_input(text: str) -> None:
    env = _probe_env()
    env.access += [
        AccessEntry(10 + i, 1, "ssh", {"alias": f"host{i}"}, "", "", "") for i in range(46)
    ]
    best = float("inf")
    for _ in range(3):  # the best of three: a busy CI runner can stall any single run
        started = time.perf_counter()
        call = guard.tool_call(
            {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": text}}
        )
        assert call is not None
        guard.check(call, [env] * 20, lambda env: BLOCK)
        redact(call.text)
        with contextlib.suppress(ValidationError):
            check_secret("notes", text[:4000])
        best = min(best, time.perf_counter() - started)
    assert best < 0.25
