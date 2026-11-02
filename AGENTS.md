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
```

All three checks must pass before you report a change as done.

## Rules

- Writes go only through `src/relaybay/service.py` (state change + `event` row in one transaction,
  then publish on the bus). Do not write SQL from web routes or MCP tools.
- Keep runtime dependencies few; adding one needs a reason in the pull request.
- No CDN or outbound network calls at runtime; front-end assets are vendored.
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
