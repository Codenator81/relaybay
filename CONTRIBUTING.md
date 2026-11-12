# Contributing to relaybay

Thanks for your interest. relaybay is young; small, focused pull requests are the easiest to review.
For larger changes, open an issue first so we can agree on the approach.

## Development setup

```bash
uv sync                      # creates .venv with runtime and dev dependencies
uv run relaybay serve --reload --home ./.dev-data
```

`--home` keeps development data out of your real data directory (`.dev-data` is yours to delete).
`uv run relaybay demo --home ./.demo-data` fills a separate folder with the demo workspace.

## Checks

Run all three before opening a pull request; CI runs them on Linux, macOS and Windows with Python
3.12 and 3.13:

```bash
uv run ruff check
uv run ruff format --check
uv run pytest
```

## Code style

- Python 3.12+, type hints everywhere, `pathlib` for paths, explicit `encoding="utf-8"` for file I/O.
- Formatting and lint rules are `ruff`'s, configured in `pyproject.toml` (line length 100).
- All writes go through `service.py`: one transaction for the state change and its `event` row,
  then the event is published on the in-process bus. Web routes and MCP tools never write SQL.
- The board is server-rendered (Jinja2 + htmx, see [ADR-0001](docs/adr/0001-htmx-sse.md)); no build
  step, no CDN. Front-end assets live in `src/relaybay/web/static/`.
- Comments explain *why*, not *what*.

## Tests

`pytest` with temporary SQLite databases; no network access and no real `claude` binary. MCP tests
run the official SDK client against a uvicorn server started on a free local port. New behaviour
comes with a test.

`tests/e2e/` runs whole scenarios against a real server: `tests/e2e/fake_claude.py` stands in for
`claude -p` (a JSON scenario instead of a model), found on PATH through a shim in a temporary folder.
It runs the hook commands from a temporary Claude home as real processes and calls MCP through the
SDK client. Assert by polling, never by a fixed wait; keep the folder under 60 s. A feature that
changes what a session sees gets a scenario there. Runs on cards (`tests/e2e/test_runs.py`) let the
server start the fake itself: the `runs` fixture puts `FAKE_CLAUDE_SCENARIO`, `FAKE_CLAUDE_TRACE`
and `FAKE_CLAUDE_AUTH` (the JSON answer and exit code of `claude auth status`; without it, a
subscription sign-in) into the environment the server passes on. The fake takes the prompt from
stdin when it is missing (a `claude.cmd` shim), `--model`, `--effort`, `--chrome` and the `manual`
mode; a scenario may give `turns` (one list of steps per turn of a session) and the steps
`spawn_child` (a sleeping grandchild that writes its pid) and `exit` (no result); each run traces
its argv, prompt and `RELAYBAY_RUN_ID` (`kind: argv`) and its end (`kind: turn_end`). Hooks come
from the user layer (`$CLAUDE_CONFIG_DIR/settings.json`, when `user` is in `--setting-sources`) and
each `--settings`; an identical command handler runs once; each hook run is traced (`kind: hook`).
The memory it loads is traced (`kind: memory`): the user `CLAUDE.md` of the config folder, the cwd's
`CLAUDE.md` and `CLAUDE.local.md`, and those of each `--add-dir` with
`CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1`. `make_git_repo` in `tests/e2e/conftest.py` makes a
repository with untracked files for worktree tests. Against the real CLI, by hand and never in CI:
`RELAYBAY_E2E_REAL_CLAUDE=1 uv run pytest tests/e2e/test_real_claude.py` and
`uv run python scripts/probe_claude.py` (both use your login and spend a little quota).

## Commits and pull requests

- [Conventional Commits](https://www.conventionalcommits.org/) titles: `feat: …`, `fix: …`,
  `docs: …`, `refactor: …`, `test: …`, `chore: …`.
- Open pull requests against `main`; keep one topic per pull request.
- A change in behaviour, a command, a setting or an MCP tool updates `README.md`, `docs/SPEC.md` and
  the relevant reference page in `docs/`, and `CHANGELOG.md` (`[Unreleased]`) in the same pull
  request.
- Never commit secrets, tokens or personal data. Examples use the demo projects `acme-shop` and
  `demo-api`.

## Documentation

- [docs/SPEC.md](docs/SPEC.md) — how relaybay behaves now; a behaviour change updates it in the same
  pull request.
- [docs/architecture.md](docs/architecture.md) — processes, modules, data model, transports.
- [docs/ROADMAP.md](docs/ROADMAP.md) and [docs/design/](docs/design/index.md) — what comes next; a
  design's status changes when its step ships.
- [docs/adr/](docs/adr/) — single decisions.

The docs site is built with MkDocs Material:

```bash
uv sync --group docs
uv run --no-sync mkdocs build --strict   # or: mkdocs serve
```

The CLI, configuration, MCP and policy reference pages in `docs/reference/`, and
`docs/reference/policy.schema.json`, are generated from the code. After changing a command, a
setting, an MCP tool or the policy schema and presets, regenerate them; CI fails when they drift:

```bash
uv run python scripts/gen_reference.py           # rewrite docs/reference/*.md
uv run python scripts/gen_reference.py --check   # what CI runs
```

`tests/test_mcp_contract.py` fails when a change breaks the MCP contract (a removed tool or
parameter, a narrowed type, a new required parameter) against `tests/fixtures/mcp_contract_baseline.json`
without an expired deprecation ([SPEC](docs/SPEC.md#spec-mcp-compat)); only a release pull request
rewrites the baseline, with `uv run python scripts/gen_reference.py --rebaseline`.

## Desktop builds

The installers are built from a PyInstaller onedir folder; [packaging/README.md](packaging/README.md)
has the steps per OS. On any OS:

```bash
uv sync --extra desktop --group build
uv run --no-sync pyinstaller --noconfirm packaging/relaybay.spec   # dist/relaybay/ (+ relaybay.app)
uv run --no-sync python scripts/hook_timing.py -- dist/relaybay/relaybay-hook
```

CI builds and smoke-tests them on Windows, macOS and Linux (`.github/workflows/desktop.yml`). A new
runtime dependency adds a line to `THIRD_PARTY_LICENSES.txt` (a test checks it against `uv.lock`).

To try setup from a frozen build, run its `relaybay` against a scratch Claude Code folder, never
your real one: `dist/relaybay/relaybay setup --claude-home <scratch folder>`, then `doctor` with the
same option. Setup refuses to write hooks for a build that runs from the temp or Downloads folder,
so build into the repository's `dist/` (the default). Tests simulate a frozen build with
`sys.frozen` and `sys.executable` (`tests/test_installed_setup.py`).

## Releases

Maintainers only. Set the version in `pyproject.toml` (the only place), run `uv lock`, move
`[Unreleased]` in `CHANGELOG.md` to `## [x.y.z] - YYYY-MM-DD`, merge, then tag `vx.y.z` on `main`
and push the tag. `.github/workflows/release.yml` checks that the tag matches the version, builds
and checks the wheel and sdist (`scripts/check_dist.py`), publishes to PyPI by trusted publishing,
pushes `ghcr.io/codenator81/relaybay:<version>` and `:latest`, builds the installers
(`desktop.yml`), creates the GitHub Release with the notes from `CHANGELOG.md`
(`scripts/changelog_notes.py`), the wheel, the sdist, the installers and `SHA256SUMS.txt`, and
deploys the docs.
