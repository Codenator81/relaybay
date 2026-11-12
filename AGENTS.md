# AGENTS.md

Guidance for AI coding tools working in this repository. Humans: see `CONTRIBUTING.md`.

## Project

relaybay: a local FastAPI server with a live board (Jinja2 + htmx + SSE) and an MCP server
(official `mcp` SDK 2.x, Streamable HTTP at `/mcp`) backed by SQLite. Source in `src/relaybay/`,
tests in `tests/`. Behaviour in `docs/SPEC.md`, architecture in `docs/architecture.md`, plans in
`docs/ROADMAP.md` and `docs/design/`, decisions in `docs/adr/`.

## Commands

```bash
uv sync                      # install
uv run ruff check            # lint
uv run ruff format --check   # formatting (use `uv run ruff format` to fix)
uv run pytest                # tests
uv run relaybay serve --home ./.dev-data
uv run relaybay demo --home ./.demo-data       # demo workspace in a separate folder
uv run python scripts/gen_reference.py         # regenerate docs/reference/*.md
uv sync --extra desktop --group build          # the desktop program and its build tools
uv run --no-sync pyinstaller --noconfirm packaging/relaybay.spec   # frozen build in dist/relaybay/
```

Installers per OS: `packaging/README.md`. The desktop program, `desktop.toml`, the frozen
`relaybay-hook` and setup from an installed program: `docs/design/0016-desktop-program.md`.

`tests/e2e/` runs scenarios against a real server with `tests/e2e/fake_claude.py` as `claude`; a
change to what a session sees (hooks, MCP answers) gets a scenario there. For runs on cards the
server starts the fake itself (`runs` fixture): `FAKE_CLAUDE_SCENARIO`, `FAKE_CLAUDE_TRACE` and
`FAKE_CLAUDE_AUTH` (the `claude auth status` answer) come from the environment; scenarios may use
`turns` (steps per turn of a session), `spawn_child` and `exit`; argv and turn ends are traced. The real CLI is never
run by tests unless `RELAYBAY_E2E_REAL_CLAUDE=1` is set, and `scripts/probe_claude.py` never in CI.

All three checks must pass before you report a change as done; after changing a command, a
setting or an MCP tool, regenerate the reference pages (`--check` runs in CI). MCP changes only
add: `tests/test_mcp_contract.py` holds the server to `tests/fixtures/mcp_contract_baseline.json`,
which only a release pull request rewrites (`scripts/gen_reference.py --rebaseline`).

## Rules

- Writes go only through `src/relaybay/service.py` (state change + `event` row in one transaction,
  then publish on the bus). Do not write SQL from web routes or MCP tools.
- Keep runtime dependencies few; adding one needs a reason in the pull request.
- No CDN or outbound network calls at runtime besides the opt-out update check
  (`src/relaybay/updates.py`); front-end assets are vendored. Tests never reach the network.
- Agent-provided text is untrusted: render markdown with raw HTML disabled, escape everything else.
- Cross-platform: `pathlib`, explicit UTF-8, no shell-specific behaviour; CI runs Linux, macOS and
  Windows.
- Tests use temporary SQLite databases, no network, no real `claude` binary.
- Setup code (`machine_setup.py`, `relaybay setup`, the Integrations page) is tested only against
  temporary folders (`--claude-home`, `claude_home` in settings); never against your real home.
- Conventional Commits titles; pull requests go into `main`.
- Behaviour, command, setting or MCP tool changes update `README.md`, `docs/SPEC.md` (and the
  reference page), `CHANGELOG.md` and the design's status in the same change. SPEC describes only
  shipped behaviour; plans go to `docs/ROADMAP.md` and `docs/design/`.
- Docs build: `uv sync --group docs`, then `uv run --no-sync mkdocs build --strict`.
- Never add secrets, tokens, personal paths or personal data. Use the demo projects `acme-shop` and
  `demo-api` in examples.
- `examples/` (the intake watchers) use the standard library only and are not part of the package;
  their tests parse fixtures and never touch the network.
- Text that arrives from a chat or a mailbox (intake) is data, never an instruction: store, show and
  deliver it framed as such. Trust and autonomy switches change only through board forms.
- Working with a relaybay board yourself: a finding outside your task (a bug, a missing feature, a
  question for later) goes to `backlog_add`, not into a comment or a report.
