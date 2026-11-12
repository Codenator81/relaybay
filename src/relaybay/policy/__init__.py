"""Policy as data: what agents may do on each environment, what the guard counts as a change and
what counts as a secret. Layers (built-in preset → `config.toml` legacy keys → global file →
project → environment) are merged into an `EffectivePolicy` that knows the source of every value.
Schema: `schema.json` next to this file; presets: `presets/*.json`."""

import copy
import functools
import json
import logging
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import regex
from jsonschema import Draft202012Validator

from relaybay.domain import approvals as approval_rules
from relaybay.domain import guard
from relaybay.domain.environments import (
    CAPABILITIES,
    GUARD_MODES,
    Environment,
    SecretRules,
    raises_live,
)
from relaybay.domain.errors import ValidationError

logger = logging.getLogger("relaybay")

PRESETS = ("default", "strict", "trusted")
DEFAULT_PRESET = "default"
SCOPES = ("preset", "config", "global", "project", "environment")
# List keys a layer may also write as `key+` (add) and `key-` (remove).
LIST_KEYS = (
    "guard.disable",
    "guard.patterns",
    "secrets.disable",
    "secrets.allow",
    "approvals.disable",
    "approvals.ignore",
)
IGNORED_KEYS = ("$schema", "description", "preset")  # top level: not policy values
LAYER_MAX = 64 * 1024  # characters of one layer's JSON
REGEX_MAX = 200
UNBOUNDED_MAX = 2  # `*`, `+` and `{n,}` per custom pattern
INPUT_MAX = 8 * 1024  # characters a custom pattern ever sees (the guard reads at most 8 KB)
PROBE_SECONDS = 0.05  # all probes of one pattern together, when a layer is validated
MATCH_SECONDS = 0.02  # one match on the hot path; a pattern that times out is switched off
# Hostile inputs a custom pattern must get through quickly when a layer is validated.
_PROBES = (
    "a" * 4000 + "!",
    "a " * 2000 + "!",
    "-" * 4000 + "!",
    "ssh " * 1000 + "x",
    "/" * 4000,
    "0" * 4000 + "x",
)
# Shapes that make a backtracking engine slow, refused before anything runs: back-references,
# a quantified group that holds a quantifier ((a+)+, (\w*)*), a quantified group with an
# alternation ((a|aa)*), and more than UNBOUNDED_MAX unbounded quantifiers (.*.*.*=).
_BACKREF_RE = re.compile(r"\\[1-9]|\\g|\(\?P=|\\k<")
_NESTED_RE = re.compile(r"\((?:[^()\\]|\\.)*[*+}](?:[^()\\]|\\.)*\)[*+{]")
_ALTERNATION_RE = re.compile(r"\((?:[^()\\]|\\.)*\|(?:[^()\\]|\\.)*\)[*+?{]")
_OPEN_RANGE_RE = re.compile(r"\{\d*,\}")


class PolicyError(ValidationError, ValueError):
    """A policy layer that does not validate: `source: path: message`. A ValueError too, so a
    broken global file stops the start like a broken config.toml."""


# --- schema and presets ------------------------------------------------------------------------


@functools.cache
def schema() -> dict[str, Any]:
    text = resources.files("relaybay.policy").joinpath("schema.json").read_text(encoding="utf-8")
    return json.loads(text)


@functools.cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(schema())


@functools.cache
def _preset_text(name: str) -> str:
    folder = resources.files("relaybay.policy").joinpath("presets")
    return folder.joinpath(f"{name}.json").read_text(encoding="utf-8")


def preset(name: str) -> dict[str, Any]:
    """A built-in preset as a full policy layer."""
    if name not in PRESETS:
        raise PolicyError(f"Unknown preset {name!r}; use one of: {', '.join(PRESETS)}.")
    return json.loads(_preset_text(name))


def _path(parts: Any) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(top level)"


def _unbounded(text: str) -> int:
    """`*`, `+` and `{n,}` outside character classes and escapes."""
    count, i, in_class = 0, 0, False
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
            if text[i + 1 : i + 2] == "]":
                i += 1  # a `]` right after `[` is a literal
        elif (char in "*+" and not (i > 0 and text[i - 1] in "*+?}")) or (
            char == "{" and _OPEN_RANGE_RE.match(text, i)
        ):
            count += 1  # a `+` right after a quantifier makes it possessive: not counted
        i += 1
    return count


class _TimedOut:
    """What a guard search that ran out of time returns: a match (fail closed), ending where
    the search started."""

    __slots__ = ("pos",)

    def __init__(self, pos: int) -> None:
        self.pos = pos

    def start(self) -> int:
        return self.pos

    def end(self) -> int:
        return self.pos

    def group(self, index: int = 0) -> str:
        return ""


TIMEOUT_LOG_SECONDS = 60.0


class SafePattern:
    """A custom regular expression with a hard time limit on every match (the `regex` module;
    Python's `re` cannot be interrupted). It sees at most INPUT_MAX characters. A call that
    times out fails closed: a guard search counts as a match (the call looks like a change), an
    allow pattern does not allow. Logged at most once a minute per pattern."""

    __slots__ = ("_logged_at", "_pattern", "text")

    def __init__(self, text: str) -> None:
        self.text = text
        self._pattern = regex.compile(text, regex.IGNORECASE | regex.VERSION0)
        self._logged_at = -TIMEOUT_LOG_SECONDS

    def _timed_out(self) -> None:
        now = time.monotonic()
        if now - self._logged_at >= TIMEOUT_LOG_SECONDS:
            self._logged_at = now
            logger.error(
                "relaybay policy: the pattern %r took longer than %s s; that call was treated as"
                " a match (guard) or as not allowed (secrets). Simplify the pattern.",
                self.text,
                MATCH_SECONDS,
            )

    def search(self, string: str, pos: int = 0) -> Any:
        try:
            return self._pattern.search(string[:INPUT_MAX], pos, timeout=MATCH_SECONDS)
        except TimeoutError:
            self._timed_out()
            return _TimedOut(pos)

    def fullmatch(self, string: str) -> Any:
        try:
            return self._pattern.fullmatch(string[:INPUT_MAX], timeout=MATCH_SECONDS)
        except TimeoutError:
            self._timed_out()
            return None

    def probe_search(self, string: str, timeout: float) -> Any:
        """A search that raises TimeoutError (validation probes)."""
        return self._pattern.search(string, timeout=timeout)


def _static_check(text: str, where: str) -> None:
    if len(text) > REGEX_MAX:
        raise PolicyError(f"{where}: longer than {REGEX_MAX} characters.")
    if _BACKREF_RE.search(text):
        raise PolicyError(f"{where}: back-references are not allowed.")
    if _NESTED_RE.search(text):
        raise PolicyError(
            f"{where}: a quantified group with a quantifier inside, such as (a+)+, can be slow;"
            " rewrite it without nesting."
        )
    if _ALTERNATION_RE.search(text):
        raise PolicyError(
            f"{where}: a quantified group with an alternation, such as (a|aa)*, can be slow;"
            " use a character class or drop the quantifier."
        )
    if _unbounded(text) > UNBOUNDED_MAX:
        raise PolicyError(
            f"{where}: at most {UNBOUNDED_MAX} unbounded quantifiers (*, +, {{n,}}) per pattern."
        )


# Probe results by pattern text: None when it passed, else the problem.
_PROBED: dict[str, str | None] = {}


def compile_pattern(text: str, where: str) -> SafePattern:
    """A custom regular expression from a policy layer being validated: refused when its shape
    can be slow, when it does not compile, or when it is slow on hostile input."""
    _static_check(text, where)
    try:
        pattern = hot_pattern(text)
    except Exception as exc:  # regex.error, and ValueError / KeyError for conflicting flags
        raise PolicyError(f"{where}: not a valid regular expression ({exc}).") from None
    if text not in _PROBED:
        if len(_PROBED) > 1024:
            _PROBED.clear()
        _PROBED[text] = _probe(pattern)
    if _PROBED[text]:
        raise PolicyError(f"{where}: {_PROBED[text]}")
    return pattern


def _probe(pattern: SafePattern) -> str | None:
    deadline = time.perf_counter() + PROBE_SECONDS
    for probe in _PROBES:
        left = deadline - time.perf_counter()
        try:
            if left <= 0:
                raise TimeoutError
            pattern.probe_search(probe, left)
        except TimeoutError:
            return "too slow on long input; simplify it."
    return None


@functools.lru_cache(maxsize=512)
def hot_pattern(text: str) -> SafePattern:
    """Compile without probing (the layer was validated when it was loaded)."""
    return SafePattern(text)


def _pattern_or_none(text: str | None) -> SafePattern | None:
    """Hot path: a pattern that no longer compiles is skipped and logged, never raised."""
    if not text:
        return None
    try:
        return hot_pattern(text)
    except Exception as exc:  # regex.error, ValueError, KeyError: never into the guard
        logger.error("relaybay policy: skipping the pattern %r: %s", text, exc)
        return None


def validate_layer(body: Any, source: str, scope: str) -> dict[str, Any]:
    """`body` when it is a valid policy layer of `scope`; otherwise PolicyError naming the
    source, the path and what is wrong (up to five problems)."""
    if not isinstance(body, dict):
        raise PolicyError(f"{source}: a policy layer must be a JSON object.")
    errors = sorted(_validator().iter_errors(body), key=lambda e: list(map(str, e.absolute_path)))
    if errors:
        problems = [f"{_path(e.absolute_path)}: {e.message}" for e in errors[:5]]
        raise PolicyError(f"{source}: " + "; ".join(problems))
    if "preset" in body and scope not in ("global", "preset"):
        raise PolicyError(f"{source}: preset: only the global policy names a preset.")
    for key in ("patterns", "patterns+"):
        items = body.get("guard", {}).get(key, [])
        ids = [item["id"] for item in items]
        for i, item in enumerate(items):
            where = f"{source}: guard.{key}[{i}]"
            if item["id"] in guard.RULE_IDS:
                raise PolicyError(f"{where}.id: {item['id']!r} is a built-in rule id.")
            if ids.count(item["id"]) > 1:
                raise PolicyError(f"{where}.id: {item['id']!r} appears twice.")
            for part in ("command", "change"):
                if part in item:
                    compile_pattern(item[part], f"{where}.{part} (pattern {item['id']!r})")
    for key in ("allow", "allow+"):
        for i, text in enumerate(body.get("secrets", {}).get(key, [])):
            compile_pattern(text, f"{source}: secrets.{key}[{i}]")
    approvals = body.get("approvals", {})
    if approvals.get("live", True) is not True:
        raise PolicyError(
            f"{source}: approvals.live: anything touching a live stand needs the owner's"
            " approval; a policy cannot drop it."
        )
    for key in ("ignore", "ignore+"):
        for i, text in enumerate(approvals.get(key, [])):
            compile_pattern(text, f"{source}: approvals.{key}[{i}]")
    return body


def parse_layer(text: str, source: str, scope: str) -> dict[str, Any]:
    """A layer from its JSON text (empty text: an empty layer)."""
    if not text.strip():
        return {}
    if len(text) > LAYER_MAX:
        raise PolicyError(f"{source}: longer than {LAYER_MAX} characters.")
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PolicyError(
            f"{source}: not valid JSON (line {exc.lineno}, column {exc.colno}: {exc.msg})."
        ) from None
    return validate_layer(body, source, scope)


def dump(body: dict[str, Any]) -> str:
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


# --- merging -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Layer:
    # "preset:default", "config", "global", "project:acme-shop", "environment:acme-shop/live"
    name: str
    scope: str
    body: dict[str, Any]
    error: str | None = None  # a broken layer is ignored; the reason is shown with the policy


def _ident(item: Any) -> Any:
    return item["id"] if isinstance(item, dict) else item


def _merge_into(
    target: dict[str, Any],
    body: dict[str, Any],
    layer: str,
    prefix: str,
    sources: dict[str, list[str]],
) -> None:
    plain = [k for k in body if not k.endswith(("+", "-"))]
    for key in plain:
        if not prefix and key in IGNORED_KEYS:
            continue
        path = f"{prefix}{key}"
        value = body[key]
        if isinstance(value, dict):
            if not isinstance(target.get(key), dict):
                target[key] = {}
            _merge_into(target[key], value, layer, f"{path}.", sources)
        else:
            target[key] = copy.deepcopy(value)
            sources[path] = [layer]
    for key in (k for k in body if k.endswith("+")):
        name, path = key[:-1], f"{prefix}{key[:-1]}"
        items = list(target.get(name, []))
        for item in body[key]:
            index = next((i for i, old in enumerate(items) if _ident(old) == _ident(item)), None)
            if index is None:
                items.append(copy.deepcopy(item))
            else:
                items[index] = copy.deepcopy(item)
        target[name] = items
        sources[path] = [*sources.get(path, []), layer]
    for key in (k for k in body if k.endswith("-")):
        name, path = key[:-1], f"{prefix}{key[:-1]}"
        removed = set(body[key])
        target[name] = [item for item in target.get(name, []) if _ident(item) not in removed]
        sources[path] = [*sources.get(path, []), layer]


def merge(layers: list[Layer]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The merged policy and, per value path, the layers that set it (last one wins)."""
    data: dict[str, Any] = {}
    sources: dict[str, list[str]] = {}
    for layer in layers:
        if layer.error is None:
            _merge_into(data, layer.body, layer.name, "", sources)
    return data, sources


def apply_patch(body: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """A proposal's patch on a layer's JSON: objects merge key by key, any other value (lists
    included) replaces, `null` removes the key."""
    out = copy.deepcopy(body)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = apply_patch(out[key], value)
        elif isinstance(value, dict):
            out[key] = apply_patch({}, value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _custom_rule(item: dict[str, Any]) -> guard.Rule | None:
    """A policy pattern as a guard rule; None (logged) when it does not compile."""
    change = _pattern_or_none(item.get("change"))
    command = _pattern_or_none(item.get("command"))
    if change is None or (item.get("command") and command is None):
        return None
    return guard.Rule(item["id"], item["description"], item.get("capability"), command, change)


def _flatten(value: Any, prefix: str) -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict) and value:
        for key, item in value.items():
            yield from _flatten(item, f"{prefix}.{key}" if prefix else key)
    else:
        yield prefix, value


class EffectivePolicy:
    """The merged policy of one scope (global, a project or an environment)."""

    def __init__(self, layers: list[Layer]) -> None:
        self.layers = tuple(layers)
        self.data, self.sources = merge(list(layers))
        guard_data = self.data.get("guard", {})
        self.guard_mode: str = guard_data.get("mode", "warn")
        disabled = set(guard_data.get("disable", []))
        custom = tuple(
            rule
            for rule in (_custom_rule(p) for p in guard_data.get("patterns", []))
            if rule is not None
        )
        self.rules: tuple[guard.Rule, ...] = (
            tuple(r for r in guard.MUTATING_RULES if r.id not in disabled) + custom
        )
        self.disabled_rules = frozenset(disabled)
        self.patterns: dict[str, dict[str, Any]] = {
            p["id"]: p for p in guard_data.get("patterns", [])
        }
        self.mcp_tool_names = guard.MCP_TOOL_RULE_ID not in disabled
        secrets = self.data.get("secrets", {})
        allow = (_pattern_or_none(p) for p in secrets.get("allow", []))
        self.secret_rules = SecretRules(
            frozenset(secrets.get("disable", [])), tuple(p for p in allow if p is not None)
        )
        self.expose_test_secrets = bool(secrets.get("expose_test_secrets_to_agents", False))
        self._test_secrets = secrets.get("test_secrets", {})
        approvals = self.data.get("approvals", {})
        required = {
            key: bool(approvals.get(key, default))
            for key, default in approval_rules.DEFAULT_REQUIRED.items()
        }
        required["live"] = True  # never off, whatever a broken layer says
        ignore = (_pattern_or_none(p) for p in approvals.get("ignore", []))
        self.approvals = approval_rules.ApprovalRules(
            required,
            frozenset(approvals.get("disable", [])),
            tuple(p for p in ignore if p is not None),
            int(approvals.get("valid_minutes", approval_rules.VALID_MINUTES)),
        )

    @property
    def preset(self) -> str:
        return next(
            (layer.name.split(":", 1)[1] for layer in self.layers if layer.scope == "preset"),
            DEFAULT_PRESET,
        )

    @property
    def errors(self) -> list[tuple[str, str]]:
        return [(layer.name, layer.error) for layer in self.layers if layer.error]

    def source(self, path: str) -> str:
        return ", ".join(dict.fromkeys(self.sources.get(path, ["preset:default"])))

    def level(self, kind: str, capability: str | None = None) -> str:
        levels = self.data["levels"][kind]
        if capability is not None and capability in levels:
            return levels[capability]
        return levels["default"]

    def level_source(self, kind: str, capability: str | None = None) -> str:
        levels = self.data["levels"][kind]
        key = capability if capability is not None and capability in levels else "default"
        return self.source(f"levels.{kind}.{key}")

    def level_for(self, env: Environment, capability: str | None = None) -> str:
        """The environment's own level and overrides (Stands & access) win over the policy."""
        if capability is not None and capability in env.overrides:
            return env.overrides[capability]
        if env.level is not None:
            return env.level
        return self.level(env.kind, capability)

    def level_origin(self, env: Environment, capability: str | None = None) -> str:
        """Where `level_for` comes from: `environment`, or the policy layer."""
        if (capability is not None and capability in env.overrides) or env.level is not None:
            return "environment"
        return self.level_source(env.kind, capability)

    def test_secrets_allowed(self, kind: str) -> bool:
        return kind != "live" and bool(self._test_secrets.get(kind, False))

    def flat(self) -> list[tuple[str, Any, str]]:
        """(path, value, source) of every value, for the board and `policy_get`."""
        return [(path, value, self.source(path)) for path, value in _flatten(self.data, "")]


def config_layer(permission_defaults: dict[str, str] | None, guard_mode: str | None) -> dict:
    """The legacy `[permission_defaults]` and `guard_mode` of `config.toml` as a layer."""
    body: dict[str, Any] = {}
    if permission_defaults:
        body["levels"] = {kind: {"default": lvl} for kind, lvl in permission_defaults.items()}
    if guard_mode is not None:
        body["guard"] = {"mode": guard_mode}
    return body


def guard_weakened(before: EffectivePolicy, after: EffectivePolicy) -> list[str]:
    """What a change takes away from the guard: a weaker mode, more built-in rules switched off
    (`mcp-tool-name` included), custom patterns removed or changed (compared by id). Secret
    detectors are not access."""
    found = []
    if GUARD_MODES.index(after.guard_mode) < GUARD_MODES.index(before.guard_mode):
        found.append(f"guard {before.guard_mode} → {after.guard_mode}")
    disabled = sorted(after.disabled_rules - before.disabled_rules)
    if disabled:
        found.append(f"guard rules off: {', '.join(disabled)}")
    removed = sorted(set(before.patterns) - set(after.patterns))
    if removed:
        found.append(f"guard patterns removed: {', '.join(removed)}")
    changed = sorted(
        pid
        for pid, item in before.patterns.items()
        if pid in after.patterns and after.patterns[pid] != item
    )
    if changed:
        found.append(f"guard patterns changed: {', '.join(changed)}")
    return found


def widened_on_live(before: EffectivePolicy, after: EffectivePolicy) -> list[str]:
    """What a change gives agents on live: a level above `read` that rises, a weaker guard."""
    found = []
    explicit = {*before.data["levels"]["live"], *after.data["levels"]["live"]}
    for cap in (None, *(c for c in CAPABILITIES if c in explicit)):
        old, new = before.level("live", cap), after.level("live", cap)
        if raises_live("live", old, new):
            found.append(f"live {cap or 'level'} {old} → {new}")
    return found + guard_weakened(before, after)


# --- the global file ---------------------------------------------------------------------------


class PolicyFile:
    """The global layer in `policy.json`. Read on start (a broken file stops it) and again when
    the file changes; a file that breaks later is logged and the last good one stays."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.body: dict[str, Any] = {}
        self.text = ""
        self.error: str | None = None
        self._stamp: tuple[int, int] | None = None

    def _read(self) -> tuple[tuple[int, int] | None, str]:
        if self.path is None:
            return None, ""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None, ""
        return (stat.st_mtime_ns, stat.st_size), self.path.read_text(encoding="utf-8-sig")

    def load(self) -> None:
        """Read the file; PolicyError when it is broken."""
        stamp, text = self._read()
        self.body = parse_layer(text, str(self.path), "global")
        self.text, self._stamp, self.error = text, stamp, None

    def refresh(self) -> None:
        if self.path is None:
            return
        try:
            stat = self.path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return
        try:
            self.load()
        except (PolicyError, OSError, UnicodeDecodeError, ValueError) as exc:
            self._stamp = stamp
            if str(exc) != self.error:
                logger.error("%s The last valid global policy stays in effect.", exc)
            self.error = str(exc)

    def write(self, body: dict[str, Any]) -> None:
        """Replace the file (atomically) with a validated layer."""
        if self.path is None:
            raise PolicyError("No policy file is configured.")
        validate_layer(body, str(self.path), "global")
        text = dump(body)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(text, encoding="utf-8", newline="\n")
            tmp.replace(self.path)
        except OSError as exc:
            raise PolicyError(
                f"Could not write the policy file {self.path}: {exc.strerror or exc}."
            ) from None
        self.load()
