# 0002 — Working rules inside relaybay

- Status: **1.5a Implemented in v0.1.0** (behaviour in [SPEC §6](../SPEC.md#spec-rules) and
  [SPEC §3](../SPEC.md#spec-mcp-instructions)); **1.5b–c Implemented** for 0.4.0
  ([SPEC §6](../SPEC.md#spec-rules-versions), [prompts](../SPEC.md#spec-prompts)).
- Size: 1.5a ~1–1.5 agent-days (done); 1.5b ~1.5; 1.5c ~1.5–2.

## Goal

The rules for how agents (and people) work with tasks live in relaybay itself: any client gets them
from the server, the server checks them, and they can be changed through MCP, instead of living only
in each user's private agent setup.

## Shipped in 1.5a

- The MCP `instructions` are the working contract (snapshot first, pick or create a task, keep next
  step / waiting on / progress current, `state · size · gist` reports, `waiting_on` for decisions,
  `task_patch` for missing fields, fix `Warning (rules)` lines).
- Rules as structured JSON with a fixed set of keys (`report_format`, `stage_requirements` with
  kinds `report_since_stage_change` and `field_present`, `description_template`, `guidance`).
- Layers: defaults ⊕ `[rules]` in `config.toml` ⊕ `project_rules` row (migration `0006`, set by
  hand). Chosen over a separate `rules.json`: one config file.
- A content hash stands in for version numbers; `project_snapshot` starts with the rules line.
- Checks on stage moves only, `warn` (default) and `strict` modes.

## 1.5b — versioning and changing rules

**Model.** `rule_set` (scope `global` or a project) and `rule_version` (number, body JSON, rendered
markdown, author session, reason, created_at, diff to the previous version). `rule_proposal`
(scope, patch, reason, author, status open / accepted / rejected).

**API.**

- `rules_update(scope, patch, reason)`: a new version; allowed directly only for coordinator sessions
  named in config (`coordinators = ["…"]`, matched by desktop id, alias or name). Everyone else gets
  an error pointing to `rules_propose`. With no coordinator configured every change is a proposal.
- `rules_propose(scope, patch, reason)`: stored as a proposal for the owner.
- `project_snapshot` starts with "Rules vN (global vG, project vP)"; when the version an agent last
  saw is older, tool responses add one line: "rules updated to vN — re-read relaybay://rules".
- `report_add` format checks and the `require_tracker_log` → `done` rule.

## 1.5c — Settings → Rules and prompts

**UI.** Settings → Rules: current version, history with author, reason and diff, Accept / Reject for
proposals (they also appear in the Inbox, see [0004](0004-navigation.md)), "Roll back to vK"
(creates a new version equal to vK).

**MCP prompts.** `start_task` and `finish_stage` (arguments: project, task) expand to the current
rules plus a checklist. Claude Code lists MCP prompts as slash commands
(`/mcp__relaybay__start_task`); to verify on the current client version before relying on it.

## As built (1.5b–c)

- `Rules vN` is the sum of the global and project version numbers, so it grows with a change of either;
  a project without its own rules is `project v0`.
- `project_rules` stays the row a project's rules are read from; a new version writes through to it. A
  hand edit applies at once and becomes a version at the next start.
- `config.toml`: the server remembers the hash of the raw `[rules]` table it last turned into a version
  (not merged with the defaults, so a release never replaces accepted rules). An unchanged table leaves
  accepted versions in force; a changed one wins with a new version by `config`. Comparing with the
  current version instead would have undone every accepted proposal on restart.
- A broken version never stops the server: the newest valid one is in force, the error is shown in
  Settings → Rules. Sets are found by project id; `project:global` reaches a project called `global`.
- `coordinators` is a guard against mistakes, not access control (one shared token). A `local_…` entry
  needs the session's exact hook id from an earlier write; a direct update is a notice in the Inbox.
- With no coordinator configured `rules_update` refuses and points to `rules_propose` (it does not store
  a proposal by itself), like for any non-coordinator.
- Patches merge like `policy_propose`, with `null` removing a key.
- `rules_mode` and `require_tracker_log` stay config keys outside the versioned document;
  `require_tracker_log` is also a requirement kind usable per scope.
- Author of an accepted version: `<proposer>, accepted by <owner>`.
- The report structure is checked only for the standard `first_line`.
- Prompts checked with the MCP Python SDK client; Claude Code's listing is **not verified**.

## Risks

| Risk | Mitigation |
|---|---|
| Agents ignore the rules | Warnings in every response, `strict` mode, the contract in the instructions |
| Rule churn confuses long-running sessions | Version line in the snapshot and a one-line "rules updated" hint |
| A non-coordinator rewrites the rules | Direct updates only for configured coordinators; proposals otherwise |
