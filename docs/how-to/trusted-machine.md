# Full access on a trusted local machine

relaybay's default policy keeps agents read-only on live and warns when a command looks like a
change there. On a machine where you are the only user and you trust your agents, you can give
them full access instead, keep the guard as a journal only, and store harmless test passwords next
to your test stands. This page sets that up with the `trusted` preset.

!!! warning "Only for a machine you trust"
    `trusted` lets agents change live without asking. The guard still writes every tool call that
    touches live to the journal (the Audit page, Activity → Actions on live), but nobody is
    warned. Real secrets still never go into relaybay: test secrets are for throw-away test values only.

## 1. Pick the preset

Open **Settings → Policy**, choose `trusted` under **Preset** and press **Use preset**. Because
this widens what agents may do on live, the page asks you to confirm. Or write the file yourself:
`policy.json` in the data folder (next to `config.toml`):

```json
{
  "$schema": "https://codenator81.github.io/relaybay/reference/policy.schema.json",
  "preset": "trusted"
}
```

The server picks up the file on its next check, without a restart. A file that does not validate
stops `relaybay serve` on start with the path and the problem; a file that breaks while the server
runs is logged and the last valid version stays in effect.

## 2. Check what agents get

**Settings → Policy** shows the effective policy for each project and environment, with the layer
that set every value: `levels.live.default = "full"` from `preset:trusted`, `guard.mode = "log"`.
Agents see the same with `policy_get`.

## 3. Test passwords

Until 0.15 a test stand's access entry on the board could hold test secrets. The board's Stands &
access page was removed in 0.15: keep test accounts where the project keeps its other access
notes, outside relaybay. Test secrets stored before stay masked in the journal and are never shown
to agents.

## Variations

Narrow it down for one project with a project layer (Settings → Policy, scope `acme-shop`):

```json
{
  "levels": {"live": {"default": "read", "ssh": "ask"}},
  "guard": {"mode": "warn"}
}
```

Or the opposite of `trusted`, for a shared machine:

```json
{"preset": "strict"}
```

`strict` makes test stands `ask` and live `none`, and the guard blocks. The three presets in full
are in the [policy reference](../reference/policy.md#presets).
