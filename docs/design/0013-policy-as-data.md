# 0013 — Policy as data

- Status: **Implemented** (step 11.4c, unreleased, for 0.2.0). Shipped behaviour:
  [SPEC §7](../SPEC.md#spec-policy), keys and presets: [policy reference](../reference/policy.md).
- Size: ~2 agent-days (model, schema and layering ~0.5; guard and secret checks on the policy
  ~0.5; Settings → Policy, proposals and MCP ~0.5; docs and tests ~0.5).

## Goal

The restrictions of [0006](0006-environments-and-access.md) (levels, guard, secret checks) are not
hard-coded: a policy defines them. A cautious team tightens them; a person alone on a trusted
machine gives agents full access and keeps harmless test passwords next to the test stand.
Without a policy file relaybay behaves exactly as before (the `default` preset).

## Layers

A policy is a JSON document. The effective policy of an environment is merged from:

1. the built-in **preset** named by the global layer (`default`, `strict`, `trusted`);
2. the legacy `guard_mode` and `[permission_defaults]` of `config.toml` (`config`), if set;
3. the **global** file, `<data dir>/policy.json` (setting `policy_file`);
4. the **project** layer, stored in the database;
5. the **environment** layer, stored in the database.

A lower layer overrides the upper one: objects merge key by key, scalars and lists replace. A
list key may instead be written `name+` (add items: strings by value, patterns by `id`, an item
with an existing id replaces it) or `name-` (remove items by value or `id`). The environment's own
level and per-capability overrides (Stands & access) still win over every policy layer.

Every value of the effective policy knows which layer set it; the board and `policy_get` show it.

## Schema

`src/relaybay/policy/schema.json` (JSON Schema 2020-12, published as a reference page):

- `preset` (global layer only);
- `levels.<kind>.<default|capability>`: `none` / `read` / `ask` / `full`;
- `guard.mode`: `off` / `log` / `warn` / `block` (`log`: journal only, no messages to anyone);
- `guard.disable`: built-in mutating rules by stable id (`ssh-remote-change`, `git-push`, …);
- `guard.patterns`: custom mutating patterns `{id, capability?, command?, change, description}`
  (regular expressions, at most 200 characters, no back-references or nested quantifiers, timed on
  hostile input when loaded);
- `secrets.disable`: built-in secret detectors by id; `secrets.allow`: patterns of values that
  pass;
- `secrets.test_secrets.{local,test}`: whether access fields may hold
  `{"value": "...", "test": true}`; the schema has no `live` key, so no layer can allow it;
- `secrets.expose_test_secrets_to_agents`: whether `environments_get` shows those values.

Validation errors name the layer, the path and the message. A broken global file stops the start
like a broken `config.toml`; a broken file found on a later reload, or a broken project or
environment layer, is logged and ignored (the last good global file stays) and shown on the
Policy page.

## Presets

| Preset | Levels | Guard | Secrets |
|---|---|---|---|
| `default` | local and test `full`, live `read` | `warn`, all rules | all detectors, no test secrets |
| `strict` | local `full`, test `ask`, live `none` | `block`, all rules | all detectors, no test secrets |
| `trusted` | `full` everywhere | `log` | all detectors, test secrets on local and test, not shown to agents |

## Who changes it

- A person: edits `policy.json` (the server notices the change on the next read, no restart) or
  uses Settings → Policy (board session and CSRF only, never the Bearer token): preset picker,
  a JSON editor per layer with a validation preview, and a confirmation when the change widens
  agents' access on live (a level above `read` that rises, or a weaker guard mode).
- An agent: `policy_propose(scope, patch, reason)` records a proposal (Inbox, Settings → Policy),
  accepted or rejected by a person like environment proposals. `policy_get(project?,
  environment?)` returns the effective policy with the source layer of every value.

## UI

Settings → Policy: effective policy for the chosen project and environment with the source of each
value, the raw JSON of every layer, errors, the preset picker, editors and pending proposals.
Stands & access shows each environment's effective level and where it comes from.

## Risks

| Risk | Mitigation |
|---|---|
| A custom regex hangs the hook | Static limits (length, no back-references, no nested or alternation quantifiers, at most two unbounded quantifiers), a probe on load, and the `regex` engine with a hard per-match timeout on at most 8 KB at run time (a call that times out fails closed) |
| A layer staged on a test environment skips the live confirmation | Becoming live compares the environment's policy with the project's and asks again |
| `trusted` hides real mistakes | `log` still writes the live journal; the preset is named on the Policy page and in `environments_get` |
| Test secrets leak to agents or live | Only on local and test (schema and service), masked in the UI, not sent to agents unless the policy says so, masked in the journal, never accepted in proposals |
| A broken file locks the owner out | Start refuses with path and message; later breakage keeps the last good policy |
