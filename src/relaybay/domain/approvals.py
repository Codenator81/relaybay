"""Approvals the owner gives on a card for one guarded action: what needs one (policy data), how
a tool call is read as an action and its target, which approval covers which call, and the
texts that carry the owner's decision back. Only the owner's button decides; no MCP tool does,
and the guard reads only the approval row."""

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from relaybay.domain import guard
from relaybay.domain.errors import ValidationError

ACTIONS = ("commit", "push", "merge", "deploy", "sql")
STATUSES = ("open", "approved", "refused", "used", "expired")
VIAS = ("agent", "guard", "denial")
# A push: plain, forced (`-f`, `--force-with-lease`, `+ref`) or deleting a branch.
PUSH_MODES = ("plain", "force", "delete")
VALID_MINUTES = 120  # an approval not used within this expires (policy approvals.valid_minutes)
OPEN_MAX = 10  # open requests per card: the guard stops opening more and only denies
# A reservation for a call that did not run goes back; this many times, and the approval expires.
RELEASES_MAX = 3
SUMMARY_MAX = 2000
ROLLBACK_MAX = 1000
TARGET_MAX = 300
TEXT_MAX = 1000  # the owner's optional line
USED_BY_MAX = 200
QUOTE_MAX = 120
# The built-in action patterns, by id; a policy switches one off with approvals.disable.
RULE_IDS = ("git-push", "gh-pr-merge", "git-commit")
DEFAULT_REQUIRED = {
    "commit": False,
    "push": True,
    "merge": True,
    "test": True,
    "live": True,
    "desktop": False,
}
# A line that reads like the owner's decision: only a decision from the board carries one.
_DECISION_LINE_RE = re.compile(r"^\s*the owner (approved|refused)\b", re.I | re.M)
# A word the guard cannot read as a plain name: shell expansion, a glob, a substitution.
_UNSAFE_RE = re.compile(r"[$`*?(){}\[\]<>|;&!~\\]")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/@+-][A-Za-z0-9._/@+#-]{0,199}$")
_PR_URL_RE = re.compile(r"/pull/(\d{1,9})(?:[/?#]|$)")
_NUMBER_RE = re.compile(r"^#?(\d{1,9})$")
# A run's call that looks like a push or a merge the guard could not read (a wrapper, a
# subshell, a script): denied as "could not tell". Only where code can hide: a segment whose
# command is a shell, an interpreter or a wrapper, a subshell, or `gh api`.
_PUSH_HINT_RE = re.compile(r"\bgit(\.exe)?\b.*\bpush\b", re.I | re.S)
_MERGE_HINT_RE = re.compile(r"\bgh(\.exe)?\b.*\b(merge|pulls/\d+/merge)\b", re.I | re.S)
_HIDES = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "pwsh",
        "powershell",
        "cmd",
        "python",
        "python3",
        "py",
        "node",
        "deno",
        "ruby",
        "perl",
        "sudo",
        "doas",
        "su",
        "env",
        "xargs",
        "nice",
        "nohup",
        "eval",
        "exec",
        "command",
        "wsl",
        "runas",
    }
)
_SUBSHELL_RE = re.compile(r"\$\(|`|^\s*[({]")
_FIRST_WORD_RE = re.compile(r"""^\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:"([^"]+)"|'([^']+)'|(\S+))""")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w.-]*)\1")
_COMMAND_START_RE = re.compile(r"\$\(|[|;&(]")
# A segment that moves the shell elsewhere: the current branch is not the cwd's after it.
_MOVES = frozenset({"cd", "pushd", "popd", "chdir", "set-location", "sl"})
_LOCATION_RE = re.compile(r"(^|\s)GIT_(DIR|WORK_TREE)=", re.I)
# `2>&1`, `&>file`: a redirection, not a command separator.
_REDIRECT_AMP_RE = re.compile(r"\d*>&\d+|&>>?")
_REDIRECT_WORD_RE = re.compile(r"^\d*(>>?|<)(.*)$")
# git's options before the subcommand that take a value as the next word.
_GIT_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})
# git subcommands the hint above must not catch: "push" in a message or a path.
_GIT_READ = frozenset(
    {
        "add",
        "blame",
        "branch",
        "checkout",
        "commit",
        "diff",
        "fetch",
        "grep",
        "log",
        "ls-files",
        "pull",
        "remote",
        "rev-parse",
        "show",
        "stash",
        "status",
        "switch",
    }
)
# git push / gh pr merge options that take a value as the next word.
_PUSH_VALUE = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_PUSH_SPECIAL = ("--all", "--branches", "--mirror", "--tags", "--follow-tags")
_PUSH_FORCE = ("--force", "--force-with-lease", "--force-if-includes")
_MERGE_VALUE = frozenset(
    {
        "-R",
        "--repo",
        "-b",
        "--body",
        "-F",
        "--body-file",
        "-t",
        "--subject",
        "-A",
        "--author-email",
        "--match-head-commit",
    }
)
CURRENT = "HEAD"  # a target that is the current branch of the call's folder


class Matcher(Protocol):
    def search(self, string: str, pos: int = 0) -> Any: ...


@dataclass(frozen=True, slots=True)
class ApprovalRules:
    """What the effective policy says: which actions need the owner's approval (and whether
    desktop sessions are checked at all), the built-in patterns switched off, and the patterns
    of calls that are never an action (never on live)."""

    required: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_REQUIRED))
    disabled: frozenset[str] = frozenset()
    ignore: tuple[Matcher, ...] = ()
    valid_minutes: int = VALID_MINUTES

    def needs(self, action: str) -> bool:
        return bool(self.required.get(action, DEFAULT_REQUIRED.get(action, True)))

    @property
    def desktop(self) -> bool:
        return self.needs("desktop")

    def stand_needs(self, kind: str) -> bool:
        """A change on a stand: always on live, on test as the policy says, never on local."""
        if kind == "live":
            return True
        return kind == "test" and self.needs("test")


@dataclass(frozen=True, slots=True)
class Need:
    """One action a tool call takes that may need an approval. `branch` or `pr` is CURRENT
    when the call names none: the service reads the current branch of `folders` (relative to
    the call's cwd, `git -C`), unless the call moved elsewhere (`located` false). None: the
    guard cannot tell. `segment`: the command segment of the call it came from."""

    action: str
    branch: str | None = None
    pr: str | None = None
    repository: str | None = None
    environment_id: int | None = None
    environment: str | None = None
    folders: tuple[str, ...] = ()
    rule: str = ""
    mode: str = "plain"
    located: bool = True
    segment: int = 0

    @property
    def resolved(self) -> bool:
        return CURRENT not in (self.branch, self.pr)

    @property
    def known(self) -> bool:
        """False when the guard cannot tell the branch or pull request."""
        if self.action in ("push", "commit"):
            return bool(self.branch)
        if self.action == "merge":
            return bool(self.pr)
        return True

    def what(self) -> str:
        return describe(
            self.action, self.branch, self.pr, self.environment, self.repository, self.mode
        )


@dataclass(slots=True)
class Approval:
    id: int
    task_id: int
    run_id: int | None
    session_id: int | None
    requested_by: str
    requested_via: str
    action: str
    mode: str
    repository: str | None
    branch: str | None
    pr: str | None
    environment_id: int | None
    environment: str | None
    summary: str
    rollback: str | None
    requested_at: str
    status: str
    decision: str | None
    decided_by: str | None
    decided_at: str | None
    decision_text: str | None
    valid_until: str | None
    used_at: str | None
    used_by: str | None
    used_session: str | None
    expired_at: str | None
    expired_reason: str | None
    turn_id: int | None
    comment_id: int | None
    told_at: str | None
    delivered_at: str | None
    reserved_at: str | None = None
    reserved_for: str | None = None  # the tool_use_id of the call it is held for
    reserved_call: str | None = None
    reserved_session_id: int | None = None
    released_count: int = 0
    task_key: str = ""
    task_title: str = ""
    project_slug: str = ""
    turn_seq: int | None = None
    turn_outcome: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def reserved(self) -> bool:
        """Held for one call that has not finished yet: no other call may use it."""
        return self.status == "approved" and self.reserved_at is not None

    @property
    def what(self) -> str:
        return describe(
            self.action, self.branch, self.pr, self.environment, self.repository, self.mode
        )

    @property
    def target(self) -> str:
        return describe_target(self.action, self.branch, self.pr, self.environment, self.repository)

    @property
    def needs_rollback(self) -> bool:
        return self.action == "deploy" and not self.rollback


@dataclass(slots=True)
class ApprovalView:
    """The approvals of a card, or of the Inbox, as the board shows them."""

    open: list[Approval] = field(default_factory=list)
    closed: list[Approval] = field(default_factory=list)


def describe_target(
    action: str,
    branch: str | None,
    pr: str | None,
    environment: str | None,
    repository: str | None,
) -> str:
    """Where the action goes: the branch, the pull request or the environment."""
    if action in ("deploy", "sql"):
        return environment or "?"
    if action == "merge":
        where = f"PR {pr}" if pr and pr.isdigit() else f"the PR of {pr}"
    else:
        where = branch or "?"
    return f"{where} in {repository}" if repository else where


def describe(
    action: str,
    branch: str | None,
    pr: str | None,
    environment: str | None,
    repository: str | None,
    mode: str = "plain",
) -> str:
    """The action and its target in a few words: `push of feat/x in backend`."""
    target = describe_target(action, branch, pr, environment, repository)
    if action == "push" and mode == "force":
        return f"force push of {target}"
    if action == "push" and mode == "delete":
        return f"push deleting {target}"
    return {
        "push": f"push of {target}",
        "commit": f"commit on {target}",
        "merge": f"merge of {target}",
        "deploy": f"deploy on {target}",
        "sql": f"SQL change on {target}",
    }[action]


# --- reading a tool call -----------------------------------------------------------------------


def program(word: str) -> str:
    """A command word without its folder (`/usr/bin/git`, `C:\\Git\\cmd\\git.exe` → `git`)."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _plain(words: list[str]) -> list[str]:
    """The words without redirections (`2>/dev/null`, `> log`, `< in`)."""
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        found = _REDIRECT_WORD_RE.match(word)
        if found:
            skip = not found.group(2)  # the target is the next word
            continue
        out.append(word)
    return out


def _git(words: list[str]) -> tuple[str, list[str], tuple[str, ...], bool] | None:
    """git's subcommand, its arguments, the `-C` folders before it, and whether it was pointed
    at another repository (`--git-dir`, `--work-tree`)."""
    if not words or program(words[0]) != "git":
        return None
    folders: list[str] = []
    elsewhere = False
    i = 1
    while i < len(words) and words[i].startswith("-"):
        word = words[i]
        if word.startswith(("--git-dir", "--work-tree")):
            elsewhere = True
        if word in _GIT_VALUE:
            if word == "-C" and i + 1 < len(words):
                folders.append(words[i + 1])
            i += 2
            continue
        if word.startswith("-C") and len(word) > 2:
            folders.append(word[2:])
        i += 1
    if i >= len(words):
        return None
    return words[i], words[i + 1 :], tuple(folders), elsewhere


def _branch_of(refspec: str) -> str | None:
    """The remote branch a refspec pushes to; None when the guard cannot tell."""
    spec = refspec.lstrip("+")
    if _UNSAFE_RE.search(spec):
        return None
    if ":" in spec:
        source, _, dest = spec.partition(":")
        spec = dest or source
    for prefix in ("refs/heads/", "refs/tags/"):
        if spec.startswith(prefix):
            spec = spec[len(prefix) :]
    if spec.upper() == "HEAD" or spec == "@":
        return CURRENT
    return spec if _BRANCH_RE.match(spec) else None


def _refspec_mode(refspec: str, force: bool, delete: bool) -> str:
    if delete or (refspec.startswith(":") and len(refspec) > 1):
        return "delete"
    return "force" if force or refspec.startswith("+") else "plain"


def _push_targets(args: list[str]) -> list[tuple[str | None, str]] | None:
    """(branch, mode) of each thing a `git push` pushes; None for a dry run (no push at all).
    `--tags`, `--follow-tags`, `--all`, `--mirror` are targets of their own, also next to
    refspecs."""
    positional: list[str] = []
    special: list[str] = []
    force = delete = False
    i = 0
    while i < len(args):
        word = args[i]
        if word == "--":
            positional += args[i + 1 :]
            break
        if word == "--dry-run":
            return None
        if word in _PUSH_SPECIAL:
            special.append(word)
        elif word.startswith(_PUSH_FORCE):
            force = True
        elif word == "--delete":
            delete = True
        elif word in _PUSH_VALUE:
            i += 1
        elif word.startswith("--"):
            pass
        elif word.startswith("-") and len(word) > 1:
            for k, letter in enumerate(word[1:]):
                if letter == "n":
                    return None
                if letter == "f":
                    force = True
                if letter == "d":
                    delete = True
                if letter == "o":
                    if k == len(word) - 2:
                        i += 1  # `-o value`; `-ovalue` has it attached
                    break
        else:
            positional.append(word)
        i += 1
    targets: list[tuple[str | None, str]] = [
        (_branch_of(r), _refspec_mode(r, force, delete)) for r in positional[1:]
    ]
    targets += [(s, "force" if s == "--mirror" else "plain") for s in special]
    if not targets:
        targets.append((CURRENT, "delete" if delete else "force" if force else "plain"))
    return targets


def _merge_target(args: list[str]) -> tuple[str | None, str | None]:
    """What `gh pr merge` merges (a PR number or a branch name; CURRENT without one) and the
    repository its `-R` names."""
    repository: str | None = None
    target: str | None = CURRENT
    picked = False
    i = 0
    while i < len(args):
        word = args[i]
        if word in ("-R", "--repo") and i + 1 < len(args):
            repository = args[i + 1]
        elif word.startswith("--repo="):
            repository = word.split("=", 1)[1]
        if word in _MERGE_VALUE:
            i += 2
            continue
        if word.startswith("-"):
            i += 1
            continue
        if not picked:
            target, picked = clean_pr(word), True
        i += 1
    return target, repository


def clean_pr(text: str) -> str | None:
    """A pull request as the guard matches it: its number (from `12`, `#12` or its URL), else
    the branch name it was given by."""
    text = text.strip()
    if found := _NUMBER_RE.match(text):
        return found.group(1)
    if text.startswith(("http://", "https://")):
        found = _PR_URL_RE.search(urlsplit(text).path)
        return found.group(1) if found else None
    return _branch_of(text)


def _gh(words: list[str]) -> list[str] | None:
    """`gh`'s words after its global `-R` / `--repo`, which stay as `-R value` at the end."""
    if not words or program(words[0]) != "gh":
        return None
    rest: list[str] = []
    repo: list[str] = []
    i = 1
    while i < len(words):
        word = words[i]
        if word in ("-R", "--repo") and i + 1 < len(words) and not rest:
            repo = ["-R", words[i + 1]]
            i += 2
            continue
        rest.append(word)
        i += 1
    return rest + repo


def _first_program(text: str) -> str:
    found = _FIRST_WORD_RE.match(text)
    return program(next((g for g in found.groups() if g), "")) if found else ""


def _may_hide(segment: str) -> bool:
    """A segment whose command can run code the guard does not read as commands."""
    return bool(_SUBSHELL_RE.search(segment)) or _first_program(segment) in _HIDES


def _without_heredocs(text: str) -> str:
    """Here-document bodies are data (a commit message, a file), not commands: dropped, except
    when the command they feed is a shell or an interpreter, whose line then carries them."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        found = _HEREDOC_RE.search(line)
        if found is None:
            out.append(line)
            i += 1
            continue
        end = i + 1
        while end < len(lines) and lines[end].strip() != found.group(2):
            end += 1
        head = _COMMAND_START_RE.split(line[: found.start()])[-1]
        if _first_program(head) in _HIDES:
            # On the interpreter's line, without separators or quotes that would split it.
            body = re.sub(r"[;|&'\"`]", " ", " ".join(lines[i + 1 : end]))
            line = f"{line} {body}"
        out.append(line)
        i = end + 1
    return "\n".join(out)


def segments(call: guard.ToolCall) -> list[tuple[str, list[str]]]:
    """The command segments of a call with their words: `2>&1` and `&>` read as redirections,
    here-document bodies as data; an MCP call is one segment."""
    if call.is_mcp:
        return [(call.text, [])]
    return guard.commands(_REDIRECT_AMP_RE.sub(" ", _without_heredocs(call.text)))


def segment_needs(
    call: guard.ToolCall, rules: ApprovalRules, strict: bool = False
) -> list[tuple[str, list[Need]]]:
    """Each command segment of a Bash call with the git actions it takes (push, PR merge,
    commit), needed or not by the policy, one need per thing pushed. A segment that matches an
    ignore pattern is skipped. `strict` (a run): a segment that looks like a push or a merge the
    guard cannot read gives a need it cannot tell (denied)."""
    if call.is_mcp:
        return []
    out: list[tuple[str, list[Need]]] = []
    moved = False
    for index, (segment, raw) in enumerate(segments(call)):
        words = _plain(raw)
        found: list[Need] = []
        if any(p.search(segment) for p in rules.ignore):
            out.append((segment, found))
            continue
        here = not moved and not _LOCATION_RE.search(segment)
        read = False
        hides = _may_hide(segment)
        git = _git(words)
        gh = _gh(words)
        if git is not None:
            sub, args, folders, elsewhere = git
            located = here and not elsewhere
            read = sub in _GIT_READ
            hides = hides or (sub not in ("push", "commit") and not read and "alias." in segment)
            if sub == "push":
                read = True
                for branch, mode in _push_targets(args) or []:
                    if "git-push" not in rules.disabled:
                        found.append(
                            Need(
                                "push",
                                branch=branch,
                                folders=folders,
                                rule="git push",
                                mode=mode,
                                located=located,
                                segment=index,
                            )
                        )
            elif sub == "commit" and "git-commit" not in rules.disabled and "--dry-run" not in args:
                found.append(
                    Need(
                        "commit",
                        branch=CURRENT,
                        folders=folders,
                        rule="git commit",
                        located=located,
                        segment=index,
                    )
                )
        elif gh is not None and gh[:2] == ["pr", "merge"]:
            read = True
            if "gh-pr-merge" not in rules.disabled:
                target, repository = _merge_target(gh[2:])
                found.append(
                    Need(
                        "merge",
                        pr=target,
                        repository=repository,
                        rule="gh pr merge",
                        located=here,
                        segment=index,
                    )
                )
        elif gh is not None:
            read = True
            hides = hides or gh[:1] == ["api"]
        elif words and program(words[0]) in _MOVES:
            moved = True
        found = [n for n in found if rules.needs(n.action)]
        if strict and not found and hides:
            if _PUSH_HINT_RE.search(segment) and "git-push" not in rules.disabled:
                found.append(Need("push", rule="git push", segment=index))
            elif _MERGE_HINT_RE.search(segment) and "gh-pr-merge" not in rules.disabled:
                found.append(Need("merge", rule="gh pr merge", segment=index))
            found = [n for n in found if rules.needs(n.action)]
        out.append((segment, found))
    return out


def call_needs(call: guard.ToolCall, rules: ApprovalRules, strict: bool = False) -> list[Need]:
    """The git needs of every segment of a Bash call, in order."""
    return [n for _, needs in segment_needs(call, rules, strict) for n in needs]


def stand_needs(
    call: guard.ToolCall,
    environments: list[Any],
    policy_of: Any,
) -> list[Need]:
    """Changes the call makes on a test or live stand, by the guard's mutating rules and the
    environment's identifiers, whatever the guard's mode and the agent's level: a deploy, or an
    SQL change for a database rule. One need per environment and segment (an MCP call is one
    segment). On test, segments the environment's policy ignores are skipped and the rules the
    guard switched off do not count; on live neither narrows anything."""
    found: list[Need] = []
    texts = [s for s, _ in segments(call)]
    for env in environments:
        policy = policy_of(env)
        rules: ApprovalRules = policy.approvals
        if not rules.stand_needs(env.kind):
            continue
        live = env.kind == "live"
        mutating = policy.rules
        if live:
            custom = tuple(r for r in policy.rules if r.id not in guard.RULE_IDS)
            mutating = guard.MUTATING_RULES + custom
        for index, text in enumerate(texts):
            if not live and any(p.search(text) for p in rules.ignore):
                continue
            scoped = guard.ToolCall(call.tool, text, call.is_mcp)
            changes = guard.mutating_rules(scoped, mutating, live or policy.mcp_tool_names)
            if not changes:
                continue
            for identifier, capability in env.identifiers()[: guard.IDENTIFIERS_MAX]:
                if not guard.mentions(text, identifier):
                    continue
                applicable = [
                    (name, cap)
                    for name, cap in changes
                    if capability is None or cap is None or cap == capability
                ]
                if applicable:
                    name, cap = applicable[0]
                    found.append(
                        Need(
                            "sql" if cap == "db" or capability == "db" else "deploy",
                            environment_id=env.id,
                            environment=f"{env.project_slug}/{env.name}",
                            rule=name,
                            segment=index,
                        )
                    )
                    break
    return found


def covers(approval: Approval, need: Need) -> bool:
    """Whether an approval is for this action and target. An approval that names a repository
    covers only a call in that repository; a push covers only the same mode (a plain push
    approval allows no force push or delete)."""
    if approval.action != need.action:
        return False
    if need.action in ("deploy", "sql"):
        if approval.environment_id is not None and need.environment_id is not None:
            return approval.environment_id == need.environment_id
        return (approval.environment or "").casefold() == (need.environment or "").casefold()
    if approval.repository and (
        not need.repository or approval.repository.casefold() != need.repository.casefold()
    ):
        return False
    if need.action == "merge":
        return approval.pr == need.pr
    return approval.branch == need.branch and approval.mode == need.mode


def same_target(approval: Approval, need: Need) -> bool:
    """An open request for the same thing as `need` (a request is filled in, not repeated):
    `covers` with a repository that only one of them names counting as the same."""
    if approval.repository and need.repository is None:
        need = dataclasses.replace(need, repository=approval.repository)
    return covers(approval, need)


# --- the agent's request -----------------------------------------------------------------------


def _text(value: Any, name: str, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r\n", "\n").strip()
    if text in ("", "-"):
        return None
    if len(text) > limit:
        raise ValidationError(f"{name} is longer than {limit} characters.")
    if "\x00" in text:
        raise ValidationError(f"{name} holds a NUL character.")
    return text


def clean_action(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in ACTIONS:
        raise ValidationError(f"action must be one of {', '.join(ACTIONS)}; got {text!r}.")
    return text


def clean_summary(value: Any) -> str:
    text = _text(value, "summary", SUMMARY_MAX)
    if not text:
        raise ValidationError(
            "An approval request needs a summary: what goes where (commits, files, the CI"
            " state). Nothing requested."
        )
    return text


def clean_rollback(value: Any, action: str) -> str | None:
    text = _text(value, "rollback", ROLLBACK_MAX)
    if action == "deploy" and not text:
        raise ValidationError(
            "A deploy request needs its rollback: the command that undoes it. Nothing requested."
        )
    return text


def clean_decision_text(value: Any) -> str | None:
    text = _text(value, "The line", TEXT_MAX)
    if text is not None and "\n" in text:
        text = " ".join(text.split())
    return text


@dataclass(frozen=True, slots=True)
class Target:
    repository: str | None = None
    branch: str | None = None
    pr: str | None = None
    environment: str | None = None
    mode: str = "plain"


def parse_target(action: str, value: Any) -> Target:
    """The agent's target: for push and commit `<branch>` or `<repository>:<branch>` (for a
    push `+<branch>` is a force push and `:<branch>` deletes it, as in a refspec); for merge a
    PR number, `#12`, its URL or the branch, optionally `<repository>#12`; for deploy and sql an
    environment's name."""
    text = _text(value, "target", TARGET_MAX)
    if not text:
        raise ValidationError(
            "target is required: the branch (push, commit), the PR (merge) or the environment"
            " (deploy, sql). Nothing requested."
        )
    if action in ("deploy", "sql"):
        return Target(environment=text)
    repository: str | None = None
    if action == "merge":
        if not text.startswith(("http://", "https://")) and "#" in text[1:]:
            repository, _, text = text.partition("#")
            repository, text = repository.strip() or None, "#" + text.strip()
        pr = clean_pr(text)
        if not pr or pr == CURRENT:
            raise ValidationError(
                f"target for a merge is a PR number, #12, its URL or its branch; got {text!r}."
            )
        return Target(repository=repository, pr=pr)
    mode = "plain"
    head, sep, tail = text.partition(":")
    if sep and head.strip() and not head.strip().startswith("+"):
        repository, text = head.strip(), tail.strip()
    if action == "push" and text.startswith(":") and len(text) > 1:
        mode, text = "delete", text[1:]
    elif action == "push" and text.startswith("+"):
        mode, text = "force", text[1:]
    branch = _branch_of(text.strip())
    if not branch or branch == CURRENT or text.startswith(("+", ":")):
        raise ValidationError(
            f"target for a {action} names its branch (feat/x, backend:feat/x; +feat/x for a force"
            f" push, :feat/x to delete it); got {text!r}."
        )
    return Target(repository=repository, branch=branch, mode=mode)


# --- texts to the agent ------------------------------------------------------------------------


def quoted(text: str, limit: int = QUOTE_MAX) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 3].rstrip() + "..."


def decision_line(a: Approval) -> str:
    """What the agent gets: the owner's decision and his line verbatim."""
    if a.decision == "refused":
        return (
            f"The owner refused {a.what} (approval {a.id}): {a.decision_text or 'no reason given'}"
        )
    until = f" until {a.valid_until[:16].replace('T', ' ')} UTC" if a.valid_until else ""
    line = (
        f"The owner approved {a.what} (approval {a.id}): one matching call may go through"
        f"{until}; run it alone, as its own command, from your run's folder."
    )
    if a.decision_text:
        line += f" The owner's note: {a.decision_text}"
    return line


def waiting_line(a: Approval) -> str:
    """The hook line for a turn that runs while the owner's decision waits for it to end."""
    return (
        f"The owner's decision on approval {a.id} ({a.what}) is waiting: it comes as your next"
        " turn when this one ends."
    )


def deny_reason(need: Need, approval_id: int | None, in_run: bool) -> str:
    """The guard's reason when a call needs an approval it does not have."""
    then = "your next turn" if in_run else "an owner comment through your hooks"
    if approval_id is None:
        return (
            f"relaybay guard: {need.what()} needs the owner's approval, and the card already has"
            f" {OPEN_MAX} open requests. End your turn and say in your report what waits for the"
            " owner."
        )
    return (
        f"relaybay guard: {need.what()} needs the owner's approval: waiting for the owner's"
        f" approval on the board (approval {approval_id}). End your turn; the decision comes as"
        f" {then}."
    )


def alone_reason(approvals: list[Approval]) -> str:
    """An approved action run together with other commands: denied, the approval kept."""
    named = ", ".join(f"approval {a.id} ({a.what})" for a in approvals)
    return (
        f"relaybay guard: the owner approved {named}, for that action only: run the approved"
        " action alone, as its own command, from your run's folder or worktree (no other"
        " commands, no cd before it). The approval is kept."
    )


def done_reason(key: str) -> str:
    return (
        f"relaybay guard: {key} is done: nothing is approved on it any more. Ask the owner"
        " for a new card."
    )


def released_note(a: Approval) -> str:
    """A refusal of a call an approval was held for: Claude Code refused it (a classifier, its
    mode, or relaybay's answer came too late), so the approval goes back for one more try."""
    return (
        f"approved call refused by Claude Code (approval {a.id}, {a.what}): the approval stays"
        " for one more try; run the same command again, alone"
    )


def exhausted_note(a: Approval) -> str:
    return (
        f"approved call refused by Claude Code (approval {a.id}, {a.what}): released"
        f" {RELEASES_MAX} times, approval exhausted, a new request is needed"
    )


EXHAUSTED_REASON = f"released {RELEASES_MAX} times"


def held_reason(approvals: list[Approval]) -> str:
    """The call needs an approval that is held for another call still running."""
    named = ", ".join(f"approval {a.id} ({a.what})" for a in approvals)
    return (
        f"relaybay guard: {named} is held for a call that has not finished yet. Wait for its"
        " result; if Claude Code refused that call, the approval comes back when the turn ends."
    )


SLOW_REASON = (
    "relaybay guard: could not tell the branch, run it again (git did not answer in time)."
    " The approval is kept."
)


def unknown_reason(need: Need) -> str:
    what = "branch" if need.action in ("push", "commit") else "pull request"
    return (
        f"relaybay guard: a {need.action} needs the owner's approval, and relaybay could not tell"
        f" which {what} it is for. Run it as a plain command from the repository's folder that"
        " names it (git push origin <branch>, gh pr merge <number>), and ask with"
        " approval_request."
    )


UNAVAILABLE_REASON = (
    "relaybay guard: approval check unavailable (an internal error): a push, a merge or a change"
    " on a stand is denied until it works again. Tell the owner."
)


def guarded_hint(call: guard.ToolCall) -> bool:
    """A call that may be a guarded action, read without the database (the check failed)."""
    if _PUSH_HINT_RE.search(call.text) or _MERGE_HINT_RE.search(call.text):
        return True
    return bool(guard.mutating_rules(call))


def requested_hint(a: Approval, in_run: bool) -> str:
    then = "it comes as your next turn" if in_run else "it comes as an owner comment"
    return (
        f"End your turn now; the owner decides on the board and {then}. An approval allows one"
        " matching call, run alone."
    )


def check_no_decision_line(text: str, what: str) -> None:
    """Refuse a text not from the owner that holds a line the agent would read as the owner's
    decision on an approval."""
    if _DECISION_LINE_RE.search(text):
        raise ValidationError(
            f'{what} holds a line starting "The owner approved" or "The owner refused": only the'
            " owner's decision on the board starts a turn with one. Nothing started."
        )


def used_by(tool: str, text: str) -> str:
    """The call that used an approval: its tool and short input (already masked)."""
    one = " ".join(text.split())
    shown = f"{tool}: {one}"
    return shown if len(shown) <= USED_BY_MAX else shown[: USED_BY_MAX - 1] + "…"
