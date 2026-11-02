"""Settings: defaults → `config.toml` in the home directory → `RELAYBAY_*` env → CLI flags."""

import copy
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import platformdirs

from relaybay.domain.environments import (
    DEFAULT_GUARD_MODE,
    DEFAULT_LEVELS,
    GUARD_MODES,
    KINDS,
    LEVELS,
)
from relaybay.domain.hooks import WAKE_HOLD_SECONDS
from relaybay.domain.reports import REPORT_HINTS, REPORT_LABELS
from relaybay.domain.rules import DEFAULT_RULES, RULES_MODES, merge_rules, validate_rules
from relaybay.domain.sessions import DEFAULT_DESKTOP_LINK_TEMPLATE
from relaybay.domain.stages import STAGES
from relaybay.domain.tasks import DEFAULT_DONE_WINDOW_DAYS
from relaybay.policy import PolicyFile

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
DEFAULT_OWNER_NAMES = ("owner", "me", "you")


def _doc(
    meaning: str,
    *,
    key: str | None = None,
    env: str | None = None,
    flag: str | None = None,
    default: str | None = None,
) -> dict[str, str | None]:
    """Field metadata for the configuration reference (`scripts/gen_reference.py`); `key` is
    the `config.toml` key or table, `env` the environment variable."""
    return {"meaning": meaning, "key": key, "env": env, "flag": flag, "default": default}


@dataclass(slots=True)
class Settings:
    home: Path = field(
        metadata=_doc(
            "Data directory: `relaybay.sqlite`, `token`, `config.toml`.",
            env="RELAYBAY_HOME",
            flag="--home",
            default="per-user data dir (`platformdirs`)",
        )
    )
    host: str = field(
        default=DEFAULT_HOST,
        metadata=_doc(
            "Bind address. Anything but loopback prints a warning.",
            key="host",
            env="RELAYBAY_HOST",
            flag="--host",
        ),
    )
    port: int = field(
        default=DEFAULT_PORT,
        metadata=_doc("Port.", key="port", env="RELAYBAY_PORT", flag="--port"),
    )
    allowed_hosts: list[str] = field(
        default_factory=list,
        metadata=_doc(
            "Extra names accepted in the `Host` header (LAN names); the environment variable is"
            " comma-separated.",
            key="allowed_hosts",
            env="RELAYBAY_ALLOWED_HOSTS",
            default="`[]`",
        ),
    )
    stage_labels: dict[str, str] = field(
        default_factory=dict,
        metadata=_doc(
            "Display names per stage (`plan`, `dev`, `test`, `live`, `done`).",
            key="[stages.labels]",
            default="the stage keys",
        ),
    )
    report_labels: dict[str, str] = field(
        default_factory=dict,
        metadata=_doc(
            "Display names of report states and sizes.", key="[reports.labels]", default="built-in"
        ),
    )
    report_hints: dict[str, str] = field(
        default_factory=dict,
        metadata=_doc("Tooltips of report badges.", key="[reports.hints]", default="built-in"),
    )
    desktop_link_template: str = field(
        default=DEFAULT_DESKTOP_LINK_TEMPLATE,
        metadata=_doc(
            "Link for a desktop session id; must contain `{id}`.",
            key="desktop_link_template",
            env="RELAYBAY_DESKTOP_LINK_TEMPLATE",
        ),
    )
    done_window_days: int = field(
        default=DEFAULT_DONE_WINDOW_DAYS,
        metadata=_doc(
            "Days a done task stays in the done column (whole number >= 1).",
            key="done_window_days",
            env="RELAYBAY_DONE_WINDOW_DAYS",
        ),
    )
    require_tracker_log: bool = field(
        default=False,
        metadata=_doc(
            'Done tasks without the tracker mark show "not logged" and are never archived.',
            key="require_tracker_log",
            env="RELAYBAY_REQUIRE_TRACKER_LOG",
        ),
    )
    owner_names: list[str] = field(
        default_factory=lambda: list(DEFAULT_OWNER_NAMES),
        metadata=_doc(
            "Names that mean you in a task's `waiting_on` (case-insensitive): those tasks are"
            " listed in the Inbox. The environment variable is comma-separated.",
            key="owner_names",
            env="RELAYBAY_OWNER_NAMES",
            default='`["owner", "me", "you"]`',
        ),
    )
    rules: dict[str, Any] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_RULES),
        metadata=_doc(
            "Global working rules merged over the defaults ([SPEC §6](../SPEC.md#spec-rules)).",
            key="[rules]",
            default="built-in rules",
        ),
    )
    rules_mode: str = field(
        default="warn",
        metadata=_doc(
            "`warn` or `strict`: what a stage move that misses a rule does.",
            key="rules_mode",
            env="RELAYBAY_RULES_MODE",
        ),
    )
    permission_defaults: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_LEVELS),
        metadata=_doc(
            "Legacy: agents' level per environment kind (`none`, `read`, `ask`, `full`); a"
            " layer under `policy.json` ([policy reference](policy.md)).",
            key="[permission_defaults]",
            default='`local = "full"`, `test = "full"`, `live = "read"`',
        ),
    )
    guard_mode: str = field(
        default=DEFAULT_GUARD_MODE,
        metadata=_doc(
            "Legacy: `off`, `log`, `warn` or `block`, the guard mode; a layer under"
            " `policy.json` ([policy reference](policy.md)).",
            key="guard_mode",
            env="RELAYBAY_GUARD_MODE",
        ),
    )
    policy_file: Path | None = field(
        default=None,  # None: policy.json in the data directory
        metadata=_doc(
            "The global policy layer (JSON, [policy reference](policy.md)); a relative path is"
            " read from the data directory. A missing file means the `default` preset.",
            key="policy_file",
            env="RELAYBAY_POLICY_FILE",
            default="`policy.json` in the data directory",
        ),
    )
    claude_home: Path | None = field(
        default=None,  # None: the real home directory
        metadata=_doc(
            "Folder with Claude Code's `.claude/` and `.claude.json` (setup, doctor,"
            " Integrations).",
            key="claude_home",
            env="RELAYBAY_CLAUDE_HOME",
            flag="--claude-home",
            default="your home directory",
        ),
    )
    # internal: the legacy guard_mode / [permission_defaults] as a policy layer, only the keys
    # that config.toml or the environment set (so a preset is not overridden by defaults).
    policy_config: dict[str, Any] = field(default_factory=dict)
    replay_limit: int = 5000  # internal: not configurable
    heartbeat_seconds: float = 15.0  # internal: not configurable
    wake_hold_seconds: float = WAKE_HOLD_SECONDS  # internal: wake-up hook long poll
    wake_poll_seconds: float = 1.0  # internal

    @property
    def db_path(self) -> Path:
        return self.home / "relaybay.sqlite"

    @property
    def token_path(self) -> Path:
        return self.home / "token"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    @property
    def policy_path(self) -> Path:
        path = self.policy_file or Path("policy.json")
        return path if path.is_absolute() else self.home / path

    @property
    def base_url(self) -> str:
        host = self.host
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def label(self, stage: str) -> str:
        return self.stage_labels.get(stage, stage)

    def report_label(self, key: str) -> str:
        """Display label of a report state (`verified`) or size (`M`)."""
        return self.report_labels.get(key, REPORT_LABELS.get(key, key))

    def report_hint(self, key: str) -> str:
        """Tooltip of a report state or size badge."""
        return self.report_hints.get(key, REPORT_HINTS.get(key, ""))

    def host_allow_list(self) -> list[str]:
        """Host names (without port) accepted in the Host header."""
        names = [*LOCAL_HOSTS, *self.allowed_hosts]
        if self.host not in ("0.0.0.0", "::", ""):
            names.append(f"[{self.host}]" if ":" in self.host else self.host)
        return list(dict.fromkeys(n.lower() for n in names))


# Settings read from `RELAYBAY_<NAME>`; RELAYBAY_HOME is read first, before config.toml.
ENV_KEYS = tuple(f.name for f in fields(Settings) if f.metadata.get("env") and f.name != "home")


def default_home() -> Path:
    return Path(platformdirs.user_data_dir("relaybay", appauthor=False))


def _split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


_BOOL_WORDS = {"1": True, "true": True, "yes": True, "on": True}
_BOOL_WORDS |= {"0": False, "false": False, "no": False, "off": False}


def _apply(settings: Settings, data: dict[str, Any], source: str) -> None:
    if "host" in data:
        settings.host = str(data["host"])
    if "port" in data:
        try:
            settings.port = int(data["port"])
        except (TypeError, ValueError):
            raise ValueError(f"{source}: port must be an integer, got {data['port']!r}") from None
    if "done_window_days" in data:
        value = data["done_window_days"]
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(
                f"{source}: done_window_days must be a whole number >= 1, got {value!r}"
            )
        settings.done_window_days = value
    if "require_tracker_log" in data:
        value = data["require_tracker_log"]
        if isinstance(value, str) and value.strip().lower() in _BOOL_WORDS:
            value = _BOOL_WORDS[value.strip().lower()]
        if not isinstance(value, bool):
            raise ValueError(f"{source}: require_tracker_log must be true or false, got {value!r}")
        settings.require_tracker_log = value
    if "rules_mode" in data:
        value = str(data["rules_mode"]).strip().lower()
        if value not in RULES_MODES:
            raise ValueError(
                f"{source}: rules_mode must be one of {', '.join(RULES_MODES)},"
                f" got {data['rules_mode']!r}"
            )
        settings.rules_mode = value
    if "guard_mode" in data:
        value = str(data["guard_mode"]).strip().lower()
        if value not in GUARD_MODES:
            raise ValueError(
                f"{source}: guard_mode must be one of {', '.join(GUARD_MODES)},"
                f" got {data['guard_mode']!r}"
            )
        settings.guard_mode = value
        settings.policy_config["guard_mode"] = value
    if "permission_defaults" in data:
        table = data["permission_defaults"]
        if not isinstance(table, dict):
            raise ValueError(f"{source}: [permission_defaults] must be a table")
        for kind, level in table.items():
            if kind not in KINDS or str(level).strip().lower() not in LEVELS:
                raise ValueError(
                    f"{source}: [permission_defaults] maps {', '.join(KINDS)} to one of"
                    f" {', '.join(LEVELS)}, got {kind} = {level!r}"
                )
            settings.permission_defaults[kind] = str(level).strip().lower()
            settings.policy_config.setdefault("permission_defaults", {})[kind] = (
                settings.permission_defaults[kind]
            )
    if "desktop_link_template" in data:
        template = str(data["desktop_link_template"])
        if "{id}" not in template:
            raise ValueError(f"{source}: desktop_link_template must contain '{{id}}'")
        settings.desktop_link_template = template
    if "claude_home" in data:
        settings.claude_home = Path(str(data["claude_home"])).expanduser()
    if "policy_file" in data:
        settings.policy_file = Path(str(data["policy_file"])).expanduser()
    if "allowed_hosts" in data:
        value = data["allowed_hosts"]
        settings.allowed_hosts = _split_list(value) if isinstance(value, str) else list(value)
    if "owner_names" in data:
        value = data["owner_names"]
        names = _split_list(value) if isinstance(value, str) else value
        if (
            not isinstance(names, list)
            or not names
            or not all(isinstance(n, str) and n.strip() for n in names)
        ):
            raise ValueError(
                f"{source}: owner_names must be a non-empty list of names, got {value!r}"
            )
        settings.owner_names = [n.strip() for n in names]


def load_settings(
    *,
    home: Path | None = None,
    host: str | None = None,
    port: int | None = None,
    claude_home: Path | None = None,
    environ: dict[str, str] | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    if home is None:
        home = Path(env["RELAYBAY_HOME"]).expanduser() if env.get("RELAYBAY_HOME") else None
    settings = Settings(home=(home or default_home()).resolve())

    if settings.config_path.is_file():
        with settings.config_path.open("rb") as fh:
            data = tomllib.load(fh)
        _apply(settings, data, str(settings.config_path))
        labels = data.get("stages", {}).get("labels", {})
        unknown = set(labels) - set(STAGES)
        if unknown:
            raise ValueError(
                f"{settings.config_path}: unknown stage(s) in [stages.labels]: "
                + ", ".join(sorted(unknown))
            )
        settings.stage_labels = {str(k): str(v) for k, v in labels.items()}
        reports = data.get("reports", {})
        for table in ("labels", "hints"):
            values = reports.get(table, {})
            unknown = set(values) - set(REPORT_LABELS)
            if unknown:
                raise ValueError(
                    f"{settings.config_path}: unknown key(s) in [reports.{table}]: "
                    + ", ".join(sorted(unknown))
                )
        settings.report_labels = {str(k): str(v) for k, v in reports.get("labels", {}).items()}
        settings.report_hints = {str(k): str(v) for k, v in reports.get("hints", {}).items()}
        if "rules" in data:
            override = validate_rules(data["rules"], f"{settings.config_path} [rules]")
            settings.rules = merge_rules(DEFAULT_RULES, override)

    _apply(
        settings,
        {
            key: env[f"RELAYBAY_{key.upper()}"]
            for key in ENV_KEYS
            if env.get(f"RELAYBAY_{key.upper()}")
        },
        "environment",
    )
    if host is not None:
        settings.host = host
    if port is not None:
        settings.port = port
    if claude_home is not None:
        settings.claude_home = claude_home
    # A broken global policy stops the start like a broken config.toml.
    PolicyFile(settings.policy_path).load()
    return settings
