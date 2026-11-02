# 0006 — Project environments, access map and agent permissions

- Status: **Implemented** (step 11.4b, unreleased, for 0.2.0). Shipped behaviour: [SPEC §7](../SPEC.md#spec-environments).
  Review ([0007](0007-review.md)) uses the sign-in flag for screenshots.
- As built: the journal event is `live.action` (not `environment.action`); agents have no write
  tool, only `environment_propose` (no `project_environments_set` or `project_upsert(environments=…)`);
  `full` and allowed calls get no `permissionDecision` at all, because `allow` would skip Claude
  Code's own permission prompt; `ask` stays `ask` in `warn` mode (the owner chose to be asked).
  Levels, guard mode, rules and secret detectors now come from the policy
  ([0013](0013-policy-as-data.md)); `permission_defaults` and `guard_mode` remain as a legacy layer.
- Size: ~1.5 agent-days for environments and the access map; +~1 for permission levels, the guard
  and the journal.

## Goal

Every project says which environments it has (local, test, live), how an agent reaches them and what
an agent may do there unasked, without relaybay ever storing a secret. Agents read this instead of
guessing hosts from old notes, and a guard catches mistakes such as a command aimed at live.

## Model

`environment`: id, project_id, name, kind (`local` / `test` / `live`), base URL, notes, position.

Per environment, an **access map without secrets**, each entry optional:

- ssh: config alias or host, port, user, key path as a reference;
- kubectl: context, namespace;
- database: type, host or port-forward, database name, user;
- GitOps repository and path;
- logs and monitoring links;
- sign-in for screenshots: "agent may sign in and remember the login" flag (default on for local and
  test; off for live and cannot be enabled) and the path to the saved login state;
- free notes.

Secrets are never stored, only **where they live** (a file, an environment variable name, a vault
path, an ssh config entry). Writes are checked for key and token patterns and refused with a clear
message.

**Permission level** per environment, optionally per capability (sign in and remember, ssh, kubectl,
database, deploy): `none` / `read` / `ask` / `full`. Public defaults: local and test `full`, live
`read`. Only a person changes levels, in the UI; agents may propose a change via MCP (it lands in the
Inbox). The level of `ask` means the agent requests permission and waits.

**Journal:** agent actions on a live environment (seen by hooks: the tool call that mentions a live
identifier) are recorded as task events (`environment.action`) and shown in Activity.

## API

- `project_snapshot`: a short environments block (name, kind, base URL, level).
- Resource `relaybay://project/{slug}/environments`: the full map.
- Write: `project_upsert(…, environments=…)` or a dedicated `project_environments_set(project,
  environments)`; agents' level changes go through `environment_access_propose`.
- The working rules ([0002](0002-working-rules.md)) state the contract: the environment kind and
  level set what an agent may do unasked; live is read-only and acts only on the owner's command.

## Guard

A `PreToolUse` hook check (mode `off` / `warn` / `block`, default `warn`): when a Bash or MCP tool
call mentions an identifier of an environment whose level forbids it (host, ssh alias, kube context,
database host), relaybay answers with a warning or a block and a clear reason ("acme-shop live is
read-only for agents; ask the owner"). Identifiers are matched as whole tokens.

**The guard protects against mistakes, it is not a sandbox.** An agent can reach a host by a name
relaybay does not know, through a script, or with the guard off. The documentation says so.

## UI

Project → Stands & access ([0004](0004-navigation.md)): a table of environments with their kind,
base URL, level and access entries; edit form; a "proposed by agent" banner with Accept / Reject.
Environment chips (local / test / live colours) on the board's project header.

## Risks

| Risk | Mitigation |
|---|---|
| Secrets pasted into notes | Pattern check on write, refusal names the field; docs show references only |
| False sense of safety | Docs and UI call the guard a safety net; live default `read` |
| Guard false positives slow work | `warn` by default, whole-token matching, per-project mode |
| Stale access data | Each entry shows when and by whom it was last changed |
