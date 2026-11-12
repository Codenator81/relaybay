"""Project environments, their access map (references only, never secrets) and the permission
level agents have there."""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from relaybay.domain.errors import ValidationError

KINDS = ("local", "test", "live")
LEVELS = ("none", "read", "ask", "full")  # in order: a later level allows more
CAPABILITIES = ("sign_in", "ssh", "kubectl", "db", "deploy")
DEFAULT_LEVELS = {"local": "full", "test": "full", "live": "read"}
GUARD_MODES = ("off", "log", "warn", "block")  # in order: a later mode says more
DEFAULT_GUARD_MODE = "warn"

# Access entry kind -> its fields (None: any field names). Values are references only.
ACCESS_FIELDS: dict[str, tuple[str, ...] | None] = {
    "ssh": ("alias", "host", "port", "user", "key_path_ref"),
    "kubectl": ("context", "namespace"),
    "db": ("type", "host", "port", "port_forward", "database", "user", "password_ref"),
    "gitops": ("repo", "path", "branch"),
    "logs": ("url",),
    "monitoring": ("url",),
    "other": None,
}
ACCESS_KINDS = tuple(ACCESS_FIELDS)
# The capability an access entry is used for, when the guard matches one of its identifiers.
ACCESS_CAPABILITY = {"ssh": "ssh", "kubectl": "kubectl", "db": "db", "gitops": "deploy"}
# Fields whose value names the environment in a command (matched as whole tokens by the guard).
IDENTIFIER_FIELDS = {
    "ssh": ("alias", "host"),
    "kubectl": ("context",),
    "db": ("host",),
    "gitops": ("repo",),
}

NAME_MAX = 60
VALUE_MAX = 500
NOTES_MAX = 4000
REASON_MAX = 2000
FIELDS_MAX = 20

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
_FIELD_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_HTTP_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

# --- secrets ---------------------------------------------------------------------------------

# A field whose name says it holds a secret may only hold where the secret lives.
_SECRET_NAME_RE = re.compile(r"pass|pwd|token|secret|key|credential|private", re.IGNORECASE)
# A field naming a user may not carry `user:password`.
_USER_NAME_RE = re.compile(r"user|login", re.IGNORECASE)
_REFERENCE_RES = (
    re.compile(r"^(~|\.{1,2})?[/\\]\S*$"),  # /etc/x, ~/.ssh/id_ed25519, ./key
    re.compile(r"^[A-Za-z]:[/\\]\S*$"),  # C:\keys\x
    re.compile(r"^\$\{?[A-Za-z_][A-Za-z0-9_]*\}?$"),  # $DB_PASSWORD, ${DB_PASSWORD}
    re.compile(r"^%[A-Za-z_][A-Za-z0-9_]*%$"),  # %DB_PASSWORD%
    re.compile(r"^env:[A-Za-z_][A-Za-z0-9_]*$", re.IGNORECASE),
    re.compile(r"^[A-Z][A-Z0-9]*_[A-Z0-9_]+$"),  # an environment variable name: DB_PASSWORD
    re.compile(
        r"^(vault|op|secret|secrets|keychain|keyring|ssm|aws-sm|gcp-sm|azure-kv|pass|sops|file)"
        r"://\S+$",
        re.IGNORECASE,
    ),
    re.compile(r"^(vault|keychain|keyring|ssm):\S+$", re.IGNORECASE),
    # A path, not a word: some `a/b` with text on both sides, not ending in `/`.
    re.compile(r"^(pass|file|sops):(?=\S*[^/\s]/[^/\s])\S*[^/\s]$", re.IGNORECASE),
    re.compile(r"^[\w.-]+\.(pem|key|pub|ppk|json|env|kdbx|age|gpg|asc|crt|p12|pfx)$", re.I),
)
# Values that look like a secret wherever they appear (notes, any field), by detector id (the
# ids are stable: a policy disables detectors by id).
_SECRET_VALUE_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}")),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
)
# A value after a flag or `=`: quoted, or up to whitespace (backslash escapes included).
_VALUE = r"(\"[^\"]*\"|'[^']*'|(?:\\.|[^\s\"'\\])+)"
# Places where a secret value follows: (pattern with group 1 = what stays, group 2 = the value,
# a command that must be in the text for the pattern to count). Validation refuses a value
# that is not a reference; redaction masks it. One table for both, so they never disagree.
_SECRET_SPOTS: tuple[tuple[str, re.Pattern[str], re.Pattern[str] | None], ...] = (
    # URL credentials, also with an empty user (redis://:pw@host)
    (
        "url-credentials",
        re.compile(r"((?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s/:@]*:)([^\s/@]+)(?=@)", re.I),
        None,
    ),
    (
        "password-flag",
        re.compile(
            r"((?<![\w-])--?(?:password|passwd|pass|token|secret|api-key)(?:=|\s+))" + _VALUE,
            re.I,
        ),
        None,
    ),
    ("curl-user", re.compile(r"((?<!\S)(?:-u|--user)(?:=|\s+)[^\s:/]*:)(?!//)(\S+)"), None),
    (
        "auth-header",
        re.compile(r"(\b(?:bearer|basic|digest)\s+)([A-Za-z0-9._~+/=-]{6,})", re.I),
        None,
    ),
    (
        "token-header",
        re.compile(r"(\bauthorization:\s*token\s+)([A-Za-z0-9._~+/=-]{6,})", re.I),
        None,
    ),
    ("sshpass", re.compile(r"(\bsshpass\s+-p\s*)" + _VALUE), None),
    (
        "mysql-password",
        re.compile(r"((?<!\S)-p)(?=\S)" + _VALUE),
        re.compile(r"\b(mysql|mariadb|mysqldump|mysqladmin)\b", re.I),
    ),
    (
        "docker-login-password",
        re.compile(r"((?<!\S)-p\s+)" + _VALUE),
        re.compile(r"\bdocker\s+login\b", re.I),
    ),
    (
        "redis-password",
        re.compile(r"((?<!\S)-a\s+)" + _VALUE),
        re.compile(r"\bredis-cli\b", re.I),
    ),
)
# `name=value` / `name: value` where the name says secret: the value must be a reference.
# The name's parts are bounded: unbounded `[\w-]*` on both sides of the keywords backtracks
# quadratically over a long run of word characters (a base64url blob).
_SECRET_NAME_PART = r"[\w-]{0,64}(?:pass|pwd|token|secret|key)[\w-]{0,64}"
_ASSIGNMENT_RE = re.compile(
    r"(?<![\w-])(" + _SECRET_NAME_PART + r")(\s*=\s*|\s*:\s*)" + _VALUE,
    re.IGNORECASE,
)
# For masking only: also a quoted name, as in JSON `"password": "x"`, a Python repr
# `'password': 'x'` or JSON inside a JSON string `\"password\":\"x\"`.
_REDACT_ASSIGNMENT_RE = re.compile(
    r"(?<![\w-])(" + _SECRET_NAME_PART + r")(\\?[\"']?\s*[=:]\s*)" + _VALUE,
    re.IGNORECASE,
)
# Field names whose string value is a secret wherever it appears (masking of structured data).
_SECRET_FIELD_RE = re.compile(
    r"pass|pwd|token|secret|key|credential|private|authorization|cookie", re.IGNORECASE
)
# `name: value` written as prose counts only for these names (so "key: rotate yearly" passes).
_PROSE_SECRET_RE = re.compile(r"pass|pwd|token|secret|api[_-]?key", re.IGNORECASE)
SECRET_HINT = (
    "relaybay never stores secrets, only where they live: a file path (~/.ssh/id_ed25519), an"
    " environment variable name ($DB_PASSWORD) or a vault path (vault://kv/acme/db)."
)


DETECTOR_IDS = (
    *(name for name, _ in _SECRET_VALUE_RES),
    *(name for name, _, _ in _SECRET_SPOTS),
    "secret-assignment",
    "secret-field-name",
    "user-password",
)


class FullMatcher(Protocol):
    def fullmatch(self, string: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class SecretRules:
    """What the policy says about secrets: detectors switched off (by id) and allow patterns (a
    value, or a detected secret, that one of them matches whole is accepted)."""

    disabled: frozenset[str] = frozenset()
    allow: tuple[FullMatcher, ...] = ()  # time-limited policy patterns

    def allows(self, value: str) -> bool:
        return any(p.fullmatch(value) for p in self.allow)


DEFAULT_SECRET_RULES = SecretRules()


def is_reference(value: str) -> bool:
    return any(r.match(value) for r in _REFERENCE_RES)


def _unquoted(value: str) -> str:
    return value[1:-1] if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'" else value


def _passes(value: str, rules: SecretRules) -> bool:
    return not value or is_reference(value) or rules.allows(value)


def _secret_found(text: str, rules: SecretRules) -> str | None:
    """The id of the first enabled detector that finds a secret in `text`."""
    for name, pattern in _SECRET_VALUE_RES:
        if name in rules.disabled:
            continue
        if any(not rules.allows(m.group(0)) for m in pattern.finditer(text)):
            return name
    for name, pattern, command in _SECRET_SPOTS:
        if name in rules.disabled or (command is not None and not command.search(text)):
            continue
        for match in pattern.finditer(text):
            if not _passes(_unquoted(match.group(2)), rules):
                return name
    if "secret-assignment" not in rules.disabled:
        for match in _ASSIGNMENT_RE.finditer(text):
            value = _unquoted(match.group(3))
            if _passes(value, rules):
                continue
            if "=" in match.group(2):
                return "secret-assignment"
            if len(value) >= 6 and _PROSE_SECRET_RE.search(match.group(1)):
                return "secret-assignment"
    return None


def find_secrets(text: str, rules: SecretRules = DEFAULT_SECRET_RULES) -> list[tuple[int, str]]:
    """(1-based line, detector id) for every line of `text` holding a secret, never the value.
    What only the whole text shows (a flag and its value on two lines, a command named elsewhere)
    is reported at the line that completes it, so this finds all that `check_secret` refuses."""
    lines = text.split("\n")
    found = [(n, name) for n, line in enumerate(lines, 1) if (name := _secret_found(line, rules))]
    if found:
        return found
    whole = _secret_found(text, rules)
    if not whole:
        return []
    for n in range(1, len(lines) + 1):
        if _secret_found("\n".join(lines[:n]), rules):
            return [(n, whole)]
    return [(1, whole)]


def check_secret(name: str, value: str, rules: SecretRules = DEFAULT_SECRET_RULES) -> None:
    """Refuse an obvious secret value in field `name`; references and values the policy allows
    pass. The refusal names the field and the detector, never the value."""
    if is_reference(value) or rules.allows(value):
        return
    found = _secret_found(value, rules)
    if found:
        raise ValidationError(
            f"{name} looks like it contains a secret value ({found}). {SECRET_HINT}"
        )
    if _SECRET_NAME_RE.search(name) and "secret-field-name" not in rules.disabled:
        raise ValidationError(
            f"{name} must be a reference, not the secret itself (got a value that is not a path,"
            f" an environment variable name or a vault path). {SECRET_HINT}"
        )
    if _USER_NAME_RE.search(name) and ":" in value and "user-password" not in rules.disabled:
        raise ValidationError(f"{name} holds a user only, not user:password. {SECRET_HINT}")


def _masked(value: str) -> str:
    quote = value[0] if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'" else ""
    return f"{quote}***{quote}"


def _mask_spot(match: re.Match[str]) -> str:
    return match.group(1) + _masked(match.group(2))


def _mask_assignment(match: re.Match[str]) -> str:
    return match.group(1) + match.group(2) + _masked(match.group(3))


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY(?: BLOCK)?-----[\s\S]*?"
    r"(?:-----END [A-Z ]*PRIVATE KEY(?: BLOCK)?-----|$)"
)
_TOKEN_SHAPES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(token\s+)[A-Za-z0-9._~+/=-]{6,}", re.I), r"\1***"),
    (re.compile(r"\b(gh[pousr]_|github_pat_|glpat-|sk-|xox[abprs]-)[A-Za-z0-9_-]{8,}"), r"\1***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA***"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*"), "***"),
)


def is_secret_field(name: str) -> bool:
    """A field name whose string value is masked whole in structured data (`api_key`,
    `password`, `Authorization`, `Set-Cookie`)."""
    return bool(_SECRET_FIELD_RE.search(name))


def redact(text: str) -> str:
    """`text` with obvious secret values masked (for the live-actions journal and a run's
    stored lines): everything `check_secret` refuses, plus `token <value>` and a quoted secret
    name before its value (`"password": "x"`)."""
    text = _PRIVATE_KEY_RE.sub("***", text)
    for _, pattern, command in _SECRET_SPOTS:
        if command is None or command.search(text):
            text = pattern.sub(_mask_spot, text)
    text = _REDACT_ASSIGNMENT_RE.sub(_mask_assignment, text)
    for pattern, replacement in _TOKEN_SHAPES:
        text = pattern.sub(replacement, text)
    return text


# --- model -----------------------------------------------------------------------------------


@dataclass(slots=True)
class AccessEntry:
    id: int
    environment_id: int
    kind: str
    fields: dict[str, str]
    created_at: str
    updated_at: str
    updated_by: str
    # Harmless test values a policy allows on local and test (`{"value": ..., "test": true}`
    # in the stored fields): never checked as secrets, masked on the board.
    test_secrets: dict[str, str] = field(default_factory=dict)

    @property
    def capability(self) -> str | None:
        return ACCESS_CAPABILITY.get(self.kind)

    def identifiers(self) -> list[str]:
        names = IDENTIFIER_FIELDS.get(self.kind, ())
        found = []
        for name in names:
            value = self.fields.get(name, "").strip()
            if name == "repo":
                value = repo_identifier(value)
            if value:
                found.append(value)
        return found


@dataclass(slots=True)
class Environment:
    id: int
    project_id: int
    name: str
    kind: str
    base_url: str | None
    sign_in_allowed: bool
    login_state_path: str | None
    notes: str | None
    level: str | None  # None: the default for the kind
    overrides: dict[str, str]
    position: int
    created_at: str
    updated_at: str
    updated_by: str
    project_slug: str = ""
    access: list[AccessEntry] = field(default_factory=list)

    def effective_level(self, defaults: dict[str, str], capability: str | None = None) -> str:
        if capability and capability in self.overrides:
            return self.overrides[capability]
        return self.level or defaults.get(self.kind, DEFAULT_LEVELS[self.kind])

    @property
    def host(self) -> str | None:
        if not self.base_url:
            return None
        return urlsplit(self.base_url).hostname

    def identifiers(self) -> list[tuple[str, str | None]]:
        """(identifier, capability) pairs the guard looks for in a tool call."""
        found: list[tuple[str, str | None]] = []
        if self.host:
            found.append((self.host, None))
        for entry in self.access:
            found.extend((ident, entry.capability) for ident in entry.identifiers())
        return found


@dataclass(slots=True)
class Proposal:
    id: int
    project_id: int
    project_slug: str
    change: dict[str, Any]
    reason: str
    proposed_by: str
    session_id: int | None
    status: str
    created_at: str
    decided_at: str | None
    decided_by: str | None

    @property
    def environment(self) -> str:
        return str(self.change.get("environment", ""))


@dataclass(slots=True)
class PolicyProposal:
    """An agent's proposed change to a policy layer: `scope` is `global`, `<project>` or
    `<project>/<environment>`."""

    id: int
    scope: str
    project_id: int | None
    patch: dict[str, Any]
    reason: str
    proposed_by: str
    session_id: int | None
    status: str
    created_at: str
    decided_at: str | None
    decided_by: str | None


def repo_identifier(value: str) -> str:
    """`https://git.example.com/acme/deploy.git` -> `git.example.com/acme/deploy`."""
    text = value.strip()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[^@/\s]+@", "", text)  # git@host:org/repo
    text = text.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", text) else text
    return text.removesuffix(".git").rstrip("/")


def level_rank(level: str) -> int:
    return LEVELS.index(level)


def raises_live(env_kind: str, before: str, after: str) -> bool:
    """Raising a live environment (or capability) above `read` needs a confirmation."""
    return env_kind == "live" and level_rank(after) > max(level_rank(before), level_rank("read"))


# --- validation ------------------------------------------------------------------------------


def _text(
    value: Any, name: str, limit: int, secrets: SecretRules = DEFAULT_SECRET_RULES
) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int):
        raise ValidationError(f"{name} must be a string.")
    text = str(value).replace("\r\n", "\n").strip()
    if len(text) > limit:
        raise ValidationError(f"{name} is longer than {limit} characters.")
    if text:
        check_secret(name, text, secrets)
    return text or None


def clean_name(value: Any) -> str:
    name = _text(value, "environment name", NAME_MAX)
    if not name or not _NAME_RE.match(name):
        raise ValidationError(
            "Environment name must be 1-60 letters, digits, spaces, dots, dashes or"
            " underscores, e.g. 'live' or 'staging-eu'."
        )
    return name


def clean_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in KINDS:
        raise ValidationError(f"Environment kind must be one of: {', '.join(KINDS)}.")
    return kind


def clean_level(value: Any, name: str = "level") -> str | None:
    """A level, or None for "the default"."""
    text = str(value or "").strip().lower()
    if text in ("", "default", "-"):
        return None
    if text not in LEVELS:
        raise ValidationError(f"{name} must be one of: {', '.join(LEVELS)} (or 'default').")
    return text


def clean_overrides(value: Any) -> dict[str, str | None]:
    """Capability -> level; None removes the override."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("overrides must be an object of capability -> level.")
    clean: dict[str, str | None] = {}
    for cap, level in value.items():
        if cap not in CAPABILITIES:
            raise ValidationError(
                f"Unknown capability {cap!r}; use one of: {', '.join(CAPABILITIES)}."
            )
        clean[cap] = clean_level(level, f"overrides.{cap}")
    return clean


def clean_base_url(value: Any, secrets: SecretRules = DEFAULT_SECRET_RULES) -> str | None:
    url = _text(value, "base_url", VALUE_MAX, secrets)
    if url is not None and not _HTTP_RE.fullmatch(url):
        raise ValidationError(f"base_url must be an http:// or https:// link, got {url!r}.")
    return url


def clean_notes(value: Any, secrets: SecretRules = DEFAULT_SECRET_RULES) -> str | None:
    return _text(value, "notes", NOTES_MAX, secrets)


def clean_path(value: Any, name: str, secrets: SecretRules = DEFAULT_SECRET_RULES) -> str | None:
    return _text(value, name, VALUE_MAX, secrets)


def clean_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValidationError(f"{name} must be true or false.")


def clean_access(
    kind: Any,
    fields: Any,
    secrets: SecretRules = DEFAULT_SECRET_RULES,
    *,
    allow_empty: bool = False,
) -> tuple[str, dict[str, str]]:
    """An access entry: its kind and fields (string references, no secrets). `allow_empty`:
    the entry has test secrets, so it may have no other fields."""
    kind = str(kind or "").strip().lower()
    if kind not in ACCESS_FIELDS:
        raise ValidationError(f"Access kind must be one of: {', '.join(ACCESS_KINDS)}.")
    if not isinstance(fields, dict):
        raise ValidationError("Access fields must be an object of name -> value.")
    if len(fields) > FIELDS_MAX:
        raise ValidationError(f"An access entry has at most {FIELDS_MAX} fields.")
    allowed = ACCESS_FIELDS[kind]
    clean: dict[str, str] = {}
    for name, value in fields.items():
        name = str(name).strip().lower()
        if not _FIELD_NAME_RE.match(name):
            raise ValidationError(f"Invalid access field name {name!r}: use lowercase letters.")
        if isinstance(value, dict):
            raise ValidationError(
                f"{kind}.{name}: test secrets are entered by a person on the board, on local and"
                " test environments only, when the policy allows them."
            )
        if allowed is not None and name not in allowed:
            raise ValidationError(
                f"{kind} access has no field {name!r}; use: {', '.join(allowed)}."
            )
        text = _text(value, f"{kind}.{name}", VALUE_MAX, secrets)
        if text is not None:
            clean[name] = text
    if not clean and not allow_empty:
        raise ValidationError(f"The {kind} access entry has no values.")
    if "url" in clean and not _HTTP_RE.fullmatch(clean["url"]):
        raise ValidationError(f"{kind}.url must be an http:// or https:// link.")
    return kind, clean


TEST_SECRET_KEEP = "***"  # what the board shows for a stored test secret; sent back, it keeps it


def split_test_secrets(fields: Any) -> tuple[Any, dict[str, Any]]:
    """`fields` without the values marked `{"value": ..., "test": true}`, and those values."""
    if not isinstance(fields, dict):
        return fields, {}
    plain, tests = {}, {}
    for name, value in fields.items():
        if isinstance(value, dict) and set(value) == {"value", "test"} and value["test"] is True:
            tests[name] = value["value"]
        else:
            plain[name] = value
    return plain, tests


def clean_test_secrets(
    values: dict[str, Any], kind: str, allowed: bool, current: dict[str, str] | None = None
) -> dict[str, str]:
    """Test secrets of an access entry on an environment of `kind`; `***` keeps the stored one.
    Never on live, whatever the policy says."""
    if not values:
        return {}
    if kind == "live":
        raise ValidationError("Test secrets are never allowed on live environments.")
    if not allowed:
        raise ValidationError(
            f"The policy does not allow test secrets on {kind} environments"
            f" (secrets.test_secrets.{kind})."
        )
    if len(values) > FIELDS_MAX:
        raise ValidationError(f"An access entry has at most {FIELDS_MAX} test secrets.")
    clean: dict[str, str] = {}
    for name, value in values.items():
        name = str(name).strip().lower()
        if not _FIELD_NAME_RE.match(name):
            raise ValidationError(f"Invalid test secret name {name!r}: use lowercase letters.")
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"Test secret {name} must be a non-empty string.")
        value = value.strip()
        if value == TEST_SECRET_KEEP:
            if not current or name not in current:
                raise ValidationError(
                    f"Test secret {name}: type the value, not {TEST_SECRET_KEEP}."
                )
            value = current[name]
        if len(value) > VALUE_MAX:
            raise ValidationError(f"Test secret {name} is longer than {VALUE_MAX} characters.")
        clean[name] = value
    return clean


def parse_fields_text(text: str) -> dict[str, str]:
    """`name: value` (or `name = value`) per line, as typed in the board's access form."""
    fields: dict[str, str] = {}
    for number, raw in enumerate(text.replace("\r\n", "\n").split("\n"), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z][\w-]*)\s*[:=]\s*(.*)$", line)
        if not match:
            raise ValidationError(f"Line {number}: write 'name: value', e.g. 'alias: acme-live'.")
        fields[match.group(1).lower().replace("-", "_")] = match.group(2).strip()
    return fields


def fields_text(fields: dict[str, str]) -> str:
    return "\n".join(f"{k}: {v}" for k, v in fields.items())


CHANGE_KEYS = (
    "environment",
    "kind",
    "base_url",
    "sign_in_allowed",
    "login_state_path",
    "notes",
    "level",
    "overrides",
    "access",
)


def clean_change(change: Any, secrets: SecretRules = DEFAULT_SECRET_RULES) -> dict[str, Any]:
    """An agent's proposed change: `environment` (name) plus any of the other CHANGE_KEYS;
    `access` is a list of `{kind, fields}` entries to add."""
    if not isinstance(change, dict):
        raise ValidationError("change must be an object, e.g. {'environment': 'live', ...}.")
    unknown = sorted(set(change) - set(CHANGE_KEYS))
    if unknown:
        raise ValidationError(
            f"Unknown key(s) in change: {', '.join(unknown)}; use: {', '.join(CHANGE_KEYS)}."
        )
    clean: dict[str, Any] = {"environment": clean_name(change.get("environment"))}
    if "kind" in change:
        clean["kind"] = clean_kind(change["kind"])
    if "base_url" in change:
        clean["base_url"] = clean_base_url(change["base_url"], secrets)
    if "sign_in_allowed" in change:
        clean["sign_in_allowed"] = clean_bool(change["sign_in_allowed"], "sign_in_allowed")
    if "login_state_path" in change:
        clean["login_state_path"] = clean_path(
            change["login_state_path"], "login_state_path", secrets
        )
    if "notes" in change:
        clean["notes"] = clean_notes(change["notes"], secrets)
    if "level" in change:
        clean["level"] = clean_level(change["level"])
    if "overrides" in change:
        clean["overrides"] = clean_overrides(change["overrides"])
    if "access" in change:
        entries = change["access"]
        if isinstance(entries, dict):
            entries = [entries]
        if not isinstance(entries, list) or not entries:
            raise ValidationError("access must be a list of {kind, fields} entries.")
        clean["access"] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValidationError("Each access entry must be an object {kind, fields}.")
            kind, fields = clean_access(entry.get("kind"), entry.get("fields"), secrets)
            clean["access"].append({"kind": kind, "fields": fields})
    if len(clean) == 1:
        raise ValidationError("change names an environment but nothing to change.")
    return clean


def change_summary(change: dict[str, Any]) -> str:
    """`live: level → full, overrides ssh → ask, +1 access (ssh)`."""
    parts = []
    for key in CHANGE_KEYS[1:]:
        if key not in change:
            continue
        value = change[key]
        if key == "overrides":
            parts.extend(f"{cap} → {lvl or 'default'}" for cap, lvl in value.items())
        elif key == "access":
            kinds = ", ".join(e["kind"] for e in value)
            parts.append(f"+{len(value)} access ({kinds})")
        elif key == "level":
            parts.append(f"level → {value or 'default'}")
        else:
            parts.append(f"{key} → {value if value not in (None, '') else '(empty)'}")
    return f"{change['environment']}: " + ", ".join(parts)
