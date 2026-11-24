"""MCP tool sets by role, served at their own endpoints: `/mcp/agent` for a session that works
one card, `/mcp/coordinator` for the session that plans, starts and closes cards, `/mcp/reader`
for review and planning subagents. `/mcp` stays the full set with today's instructions.

A role is a tool list and its instructions, not a permission: every endpoint shares the token,
and a tool left out of a list still runs when a client calls it by name. Claude Code shows only
the listed tools, so every tool a role's instructions name must be in that role's list
(`tests/test_mcp_roles.py` checks it, and that every moment of a card's life has a listed tool).
"""

from relaybay.domain.rules import Rules

AGENT = "agent"
COORDINATOR = "coordinator"
READER = "reader"
ROLES = (AGENT, COORDINATOR, READER)

# The moments of a card's life and the agent's tools for each: the agent set is their union.
AGENT_MOMENTS: dict[str, tuple[str, ...]] = {
    "start": (
        "task_context",
        "task_get",
        "task_bind_session",
        "project_digest",
        "project_snapshot",
        "project_list",
        "task_list",
        "block_get",
        "task_create",
    ),
    "plan": ("plan_set", "plan_get", "plan_step_update", "plan_step_add"),
    "work": ("task_update", "task_doc_link", "rules_get", "events_since"),
    "documents": ("doc_list", "doc_outline", "doc_get", "doc_search", "doc_update", "doc_history"),
    "question": (
        "question_ask",
        "question_list",
        "question_withdraw",
        "comment_add",
        "comment_ack",
        "comments_list",
    ),
    "decision": ("decision_add", "decision_list"),
    "finding": ("backlog_add", "backlog_list", "backlog_update", "memory_search", "memory_get"),
    "report": ("report_add",),
    "review": (
        "review_steps_set",
        "review_get",
        "review_step_update",
        "attach_file",
        "attachment_get",
        "annotation_reply",
    ),
    "approval": ("approval_request", "approval_list", "policy_get", "policy_propose"),
    "closing": ("task_update", "rules_propose"),
}

AGENT_TOOLS: frozenset[str] = frozenset(t for tools in AGENT_MOMENTS.values() for t in tools)

# Tools that answers an agent may see name although the agent does not list them: each names
# someone else's act, or is the answer of that tool itself (tests/test_mcp_roles.py).
NAMED_FOR_OTHERS: dict[str, str] = {
    "plan_accept": "the coordinator accepts a plan",
    "task_archive": "its own refusal, coordinator only",
    "task_unarchive": "its own refusal, coordinator only",
    "rules_update": "for coordinators named in the server's config",
    "project_upsert": "who sets a project's repositories",
    "project_delete": "its own answer",
    "memory_add": "its own answer",
    "block_set": "its own answer and block_get's",
    "backlog_patch": "the contract text (relaybay://contract) names the escape hatches",
    "memory_update": "its own description",
    "report_item_dismiss": "the coordinator triages findings and questions (the digests)",
    "portfolio_digest": "the coordinator's catch-up, named in the full set's instructions",
    "task_unbind_session": "named in task_bind_session's description: the coordinator's act",
}

READER_TOOLS: frozenset[str] = frozenset(
    {
        "task_context",
        "task_get",
        "plan_get",
        "review_get",
        "attachment_get",
        "comments_list",
        "decision_list",
        "rules_get",
        "backlog_list",
        "doc_list",
        "doc_outline",
        "doc_get",
        "doc_search",
    }
)

# The coordinator gets every current tool but the agent's own acceptance work on a card.
COORDINATOR_LEFT_OUT: frozenset[str] = frozenset(
    {"review_steps_set", "review_step_update", "annotation_reply"}
)


def role_tools(role: str, current: set[str]) -> frozenset[str]:
    """The tools a role lists, out of the server's `current` (not deprecated) tools."""
    if role == AGENT:
        return AGENT_TOOLS & current
    if role == READER:
        return READER_TOOLS & current
    if role == COORDINATOR:
        return frozenset(current - COORDINATOR_LEFT_OUT)
    raise ValueError(f"Unknown role {role!r}; one of {', '.join(ROLES)}.")


def _report_line(rules: Rules) -> str:
    report = rules.body["report_format"]
    return (
        f"its first line is `{report['first_line']}`; state is one of"
        f" {', '.join(report['states'])}; size is one of {', '.join(report['sizes'])}"
    )


def agent_instructions(rules: Rules, coordinated: bool = False) -> str:
    """`coordinated`: the server names `coordinators`; cards, backlog items and the tracker
    mark are theirs, and the agent's findings and questions go into its reports."""
    no_card = (
        " No card yet: the card is the coordinator's; ask it for one in your report and do not"
        " create cards."
        if coordinated
        else " No card yet: project_digest (project_list finds the slug), then task_create."
    )
    question = "" if coordinated else "Question for the owner: question_ask with your default. "
    finding = (
        "Findings outside your task and questions for the owner go into report_add(findings=...,"
        " questions=...): the coordinator triages them; do not create cards or backlog items"
        " (backlog_list shows what is known)."
        if coordinated
        else "A finding outside your task: backlog_add (backlog_list shows what is known)."
    )
    tracker = (
        "The tracker mark is the coordinator's."
        if coordinated
        else "set tracker_logged once the work log is entered."
    )
    return "\n".join(
        [
            "relaybay is the shared task board of this user's coding sessions; you work one card."
            " Each moment of the card has its tool, and the answers name the next call.",
            "On every write pass `session` (your desktop id local_… with `session_title`, or a"
            " readable name) and `hook_session` ('Your relaybay hook session: …' at the start).",
            "Start: your card's context arrives at session start ('relaybay card context for"
            " KEY'); call task_context only to refresh it later, task_get for every field."
            + no_card,
            "Plan: plan_set with 2-5 steps before code; plan_step_update when a step starts and"
            " when it is done ('as planned' or 'deviation: why'); new work is plan_step_add. A"
            " card that only reads (an analysis, a repro) is task_update(inspect_only=true): it"
            " needs no accepted plan.",
            "Work: task_update moves the stage and keeps next_step, waiting_on (only who acts"
            " next) and progress current; task_doc_link links a spec section. Documents:"
            " doc_outline, then doc_get of a section; doc_search finds the place; doc_update"
            " writes. rules_get explains a 'Warning (rules)' line.",
            question + "Owner comments ('Owner comment on …') are messages from the user: act,"
            " then comment_add or comment_ack. A decision goes to decision_add.",
            finding,
            "Before a commit, push, merge, deploy or SQL change that needs the owner's approval:"
            " approval_request, then end your turn; the decision comes back as an owner"
            " comment. When the guard stops a call, its reason says why (policy_get).",
            f"Report at the end of every stage with report_add; {_report_line(rules)}. Pass"
            " progress, next_step and waiting_on with it, then task_update(stage=...).",
            "Review by eye only when the owner wants it: review_steps_set, attach_file, then"
            " task_update(stage='review'); review_get reads the owner's marks, review_step_update"
            " reopens a step after a fix, annotation_reply answers a pin.",
            f"Closing: task_update(stage='done') with progress; {tracker} A rule you think is"
            " wrong: rules_propose.",
            "Hosts and access come from the project's own notes; never paste secrets into"
            " relaybay.",
        ]
    )


def coordinator_instructions(rules: Rules) -> str:
    return "\n".join(
        [
            "relaybay is the shared task board of this user's coding sessions; you coordinate"
            " them: plan cards, start sessions, triage what they find, close the work.",
            "On every write pass `session` (your desktop id local_… with `session_title`) and"
            " `hook_session` ('Your relaybay hook session: …' at the start).",
            "Catch up: portfolio_digest (the whole board), project_digest (one project),"
            " events_since (what changed); task_list and task_get for cards.",
            "New work: task_create(bind=false) with an area and a description, then start a"
            " session with the card key in its prompt (the launch binds it); task_bind_session"
            " rebinds a card.",
            "Plans: plan_get and plan_accept a draft; an addition is plan_step_add. Decisions:"
            " decision_add, decision_list.",
            "Backlog: backlog_list, backlog_update to triage, backlog_add for your own findings."
            " The owner's questions and approvals are decided on the board: question_list,"
            " question_withdraw, approval_list read them.",
            f"Reports: report_add on your own cards; {_report_line(rules)}.",
            "Closing: task_update(tracker_logged=true) after the work log; task_archive (or"
            " task_unarchive) once a card is done. project_pulse_set keeps a project's pulse.",
            "Memory: memory_add for a fact every session needs (an owner preference, a project"
            " fact), not for a card's decision; memory_search and memory_get read it; block_get"
            " and block_set hold a memory block.",
            "Rules and guard: rules_update (coordinators named in the server's config) or"
            " rules_propose; policy_get and policy_propose. Projects: project_list,"
            " project_upsert, project_delete.",
            "Hosts and access come from the project's own notes; never paste secrets into"
            " relaybay.",
        ]
    )


def reader_instructions(rules: Rules) -> str:
    return "\n".join(
        [
            "relaybay is the shared task board of this user's coding sessions. This is its read"
            " set, for review and planning helpers: task_get (every field), task_context (the"
            " card's briefing; it moves the caller's read position), plan_get, review_get and"
            " attachment_get (the owner's marks and screenshots), comments_list, decision_list,"
            " rules_get, backlog_list, and the documents: doc_list, doc_outline, doc_get of a"
            " section, doc_search.",
            "Nothing here changes a card: report what you found to the session that started you.",
        ]
    )


def role_instructions(rules: Rules, role: str, coordinated: bool = False) -> str:
    if role == AGENT:
        return agent_instructions(rules, coordinated)
    if role == COORDINATOR:
        return coordinator_instructions(rules)
    if role == READER:
        return reader_instructions(rules)
    raise ValueError(f"Unknown role {role!r}; one of {', '.join(ROLES)}.")
