"""Approvals the owner gives on a card for one guarded action: what needs one (policy data), how
a tool call is read as an action and its target, which approval covers which call, and the
texts that carry the owner's decision back. Only the owner's button decides; no MCP tool does,
and the guard reads only the approval row."""

import re
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from relaybay.domain import guard
from relaybay.domain.errors import ValidationError

ACTIONS = ("commit", "push", "merge", "deploy", "sql")
STATUSES = ("open", "approved", "refused", "used")
VIAS = ("agent", "guard", "denial")
OPEN_MAX = 10  # open requests per card: the guard stops opening more and only denies
SUMMARY_MAX = 2000
ROLLBACK_MAX = 1000
TARGET_MAX = 300
TEXT_MAX = 1000  # the owner's optional line
USED_BY_MAX = 200
QUOTE_MAX = 120
# The built-in action patterns, by id; a policy switches one off with approvals.disable.
RULE_IDS = ("git-push", "gh-pr-merge", "git-commit")
# Actions a project decides on; deploy and SQL follow the stand's kind (approvals.test, live).
PROJECT_ACTIONS = ("commit", "push", "merge")
DEFAULT_REQUIRED = {"commit": False, "push": True, "merge": True, "test": True, "live": True}
# A line that reads like the owner's decision: only a decision from the board carries one.
_DECISION_LINE_RE = re.compile(r"^\s*the owner (approved|refused)\b", re.I | re.M)
# A word the guard cannot read as a plain name: shell expansion, a glob, a substitution.
_UNSAFE_RE = re.compile(r"[$`*?(){}\[\]<>|;&!~\\]")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/@+-][A-Za-z0-9._/@+#-]{0,199}$")
_PR_URL_RE = re.compile(r"/pull/(\d{1,9})(?:[/?#]|$)")
_NUMBER_RE = re.compile(r"^#?(\d{1,9})$")
# git's options before the subcommand that take a value as the next word.
_GIT_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"})
# git push / gh pr merge options that take a value as the next word.
_PUSH_VALUE = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
_PUSH_SPECIAL = ("--all", "--branches", "--mirror", "--tags")
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
    """What the effective policy says: which actions need the owner's approval, the built-in
    patterns switched off, and the patterns of calls that are never an action."""

    required: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_REQUIRED))
    disabled: frozenset[str] = frozenset()
    ignore: tuple[Matcher, ...] = ()

    def needs(self, action: str) -> bool:
        return bool(self.required.get(action, DEFAULT_REQUIRED.get(action, True)))

    def stand_needs(self, kind: str) -> bool:
        """A change on a stand: always on live, on test as the policy says, never on local."""
        if kind == "live":
            return True
        return kind == "test" and self.needs("test")


@dataclass(frozen=True, slots=True)
class Need:
    """One action a tool call takes that may need an approval. `branch` or `pr` is CURRENT
    when the call names none: the service reads the current branch of `folders` (relative to
    the call's cwd, `git -C`)."""

    action: str
    branch: str | None = None
    pr: str | None = None
    repository: str | None = None
    environment_id: int | None = None
    environment: str | None = None
    folders: tuple[str, ...] = ()
    rule: str = ""

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
        return describe(self.action, self.branch, self.pr, self.environment, self.repository)


@dataclass(slots=True)
class Approval:
    id: int
    task_id: int
    run_id: int | None
    session_id: int | None
    requested_by: str
    requested_via: str
    action: str
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
    used_at: str | None
    used_by: str | None
    used_session: str | None
    turn_id: int | None
    comment_id: int | None
    told_at: str | None
    delivered_at: str | None
    task_key: str = ""
    task_title: str = ""
    project_slug: str = ""
    turn_seq: int | None = None
    turn_outcome: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    @property
    def what(self) -> str:
        return describe(self.action, self.branch, self.pr, self.environment, self.repository)

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
) -> str:
    """The action and its target in a few words: `push of feat/x in backend`."""
    target = describe_target(action, branch, pr, environment, repository)
    return {
        "push": f"push of {target}",
        "commit": f"commit on {target}",
        "merge": f"merge of {target}",
        "deploy": f"deploy on {target}",
        "sql": f"SQL change on {target}",
    }[action]


# --- reading a tool call -----------------------------------------------------------------------


def _git(words: list[str]) -> tuple[str, list[str], tuple[str, ...]] | None:
    """git's subcommand, its arguments and the `-C` folders before it."""
    if not words or words[0].rsplit("/", 1)[-1].lower() not in ("git", "git.exe"):
        return None
    folders: list[str] = []
    i = 1
    while i < len(words) and words[i].startswith("-"):
        word = words[i]
        if word in _GIT_VALUE:
            if word == "-C" and i + 1 < len(words):
                folders.append(words[i + 1])
            i += 2
            continue
        i += 1
    if i >= len(words):
        return None
    return words[i], words[i + 1 :], tuple(folders)


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
    if spec.upper() == "HEAD":
        return CURRENT
    return spec if _BRANCH_RE.match(spec) else None


def _push_branches(args: list[str]) -> list[str | None] | None:
    """The branches a `git push` pushes; None for a dry run (no push at all)."""
    positional: list[str] = []
    special: list[str] = []
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
        elif word in _PUSH_VALUE:
            i += 1
        elif word.startswith("--"):
            pass
        elif word.startswith("-") and len(word) > 1:
            for k, letter in enumerate(word[1:]):
                if letter == "n":
                    return None
                if letter == "o":
                    if k == len(word) - 2:
                        i += 1  # `-o value`; `-ovalue` has it attached
                    break
        else:
            positional.append(word)
        i += 1
    refspecs = positional[1:]
    if refspecs:
        return [_branch_of(r) for r in refspecs]
    if special:
        return list(special)
    return [CURRENT]


def _merge_target(args: list[str]) -> str | None:
    """What `gh pr merge` merges: a PR number, or a branch name; CURRENT without one."""
    i = 0
    while i < len(args):
        word = args[i]
        if word in _MERGE_VALUE:
            i += 2
            continue
        if word.startswith("-"):
            i += 1
            continue
        return clean_pr(word)
    return CURRENT


def clean_pr(text: str) -> str | None:
    """A pull request as the guard matches it: its number (from `12`, `#12` or its URL), else
    the branch name it was given by."""
    text = text.strip()
    if found := _NUMBER_RE.match(text):
        return found.group(1)
    if text.startswith(("http://", "https://")):
        found = _PR_URL_RE.search(urlsplit(text).path)
        return found.group(1) if found else None
    branch = _branch_of(text)
    return branch


def call_needs(call: guard.ToolCall, rules: ApprovalRules) -> list[Need]:
    """The git actions a Bash call takes (push, PR merge, commit), each needed or not by the
    policy, one need per branch pushed. A segment that matches an ignore pattern is skipped."""
    if call.is_mcp:
        return []
    found: list[Need] = []
    for segment, words in guard.commands(call.text):
        if any(p.search(segment) for p in rules.ignore):
            continue
        git = _git(words)
        if git is not None:
            sub, args, folders = git
            if sub == "push" and "git-push" not in rules.disabled:
                branches = _push_branches(args)
                for branch in branches or []:
                    found.append(Need("push", branch=branch, folders=folders, rule="git push"))
            elif sub == "commit" and "git-commit" not in rules.disabled and "--dry-run" not in args:
                found.append(Need("commit", branch=CURRENT, folders=folders, rule="git commit"))
            continue
        name = words[0].rsplit("/", 1)[-1].lower() if words else ""
        if (
            name in ("gh", "gh.exe")
            and words[1:3] == ["pr", "merge"]
            and "gh-pr-merge" not in rules.disabled
        ):
            target = _merge_target(words[3:])
            found.append(Need("merge", pr=target, rule="gh pr merge"))
    return [n for n in found if rules.needs(n.action)]


def stand_needs(
    call: guard.ToolCall,
    environments: list[Any],
    policy_of: Any,
) -> list[Need]:
    """Changes the call makes on a test or live stand, by the guard's mutating rules and the
    environment's identifiers, whatever the guard's mode and the agent's level: a deploy, or an
    SQL change for a database rule. One need per environment; segments the environment's
    policy ignores are skipped."""
    found: list[Need] = []
    for env in environments:
        policy = policy_of(env)
        rules: ApprovalRules = policy.approvals
        if not rules.stand_needs(env.kind):
            continue
        text = call.text
        if rules.ignore:
            kept = [
                s for s, _ in guard.commands(text) if not any(p.search(s) for p in rules.ignore)
            ]
            text = "\n".join(kept)
        if not text.strip():
            continue
        scoped = guard.ToolCall(call.tool, text, call.is_mcp)
        changes = guard.mutating_rules(scoped, policy.rules, policy.mcp_tool_names)
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
                    )
                )
                break
    return found


def covers(approval: Approval, need: Need) -> bool:
    """Whether an approved approval is for this action and target. A repository counts only
    when both name one."""
    if approval.action != need.action:
        return False
    if need.action in ("deploy", "sql"):
        if approval.environment_id is not None and need.environment_id is not None:
            return approval.environment_id == need.environment_id
        return (approval.environment or "").casefold() == (need.environment or "").casefold()
    if (
        approval.repository
        and need.repository
        and approval.repository.casefold() != need.repository.casefold()
    ):
        return False
    if need.action == "merge":
        return approval.pr == need.pr
    return approval.branch == need.branch


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


def parse_target(action: str, value: Any) -> tuple[str | None, str | None, str | None, str | None]:
    """(repository, branch, pr, environment) from the agent's target: for push and commit
    `<branch>` or `<repository>:<branch>`; for merge a PR number, `#12`, its URL or the
    branch, optionally `<repository>#12`; for deploy and sql an environment's name."""
    text = _text(value, "target", TARGET_MAX)
    if not text:
        raise ValidationError(
            "target is required: the branch (push, commit), the PR (merge) or the environment"
            " (deploy, sql). Nothing requested."
        )
    if action in ("deploy", "sql"):
        return None, None, None, text
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
        return repository, None, pr, None
    if ":" in text:
        repository, _, text = text.partition(":")
        repository = repository.strip() or None
    branch = _branch_of(text.strip())
    if not branch or branch == CURRENT:
        raise ValidationError(
            f"target for a {action} names its branch (feat/x, or backend:feat/x); got {text!r}."
        )
    return repository, branch, None, None


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
    line = f"The owner approved {a.what} (approval {a.id}): one matching call may go through now."
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
            f"relaybay guard: {need.what()} needs the owner's approval and no request could be"
            " opened (too many open on the card). Ask with approval_request, then end your turn."
        )
    return (
        f"relaybay guard: {need.what()} needs the owner's approval: waiting for the owner's"
        f" approval on the board (approval {approval_id}). End your turn; the decision comes as"
        f" {then}."
    )


def unknown_reason(need: Need) -> str:
    what = "branch" if need.action in ("push", "commit") else "pull request"
    return (
        f"relaybay guard: a {need.action} needs the owner's approval, and relaybay could not tell"
        f" which {what} it is for. Name it in the command (git push origin <branch>, gh pr merge"
        " <number>) and ask with approval_request."
    )


def requested_hint(a: Approval, in_run: bool) -> str:
    then = "it comes as your next turn" if in_run else "it comes as an owner comment"
    return (
        f"End your turn now; the owner decides on the board and {then}. An approval allows one"
        " matching call."
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
