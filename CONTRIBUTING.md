# Contributing to relaybay

Thanks for your interest. relaybay is young; small, focused pull requests are the easiest to review.
For larger changes, open an issue first so we can agree on the approach.

## Development setup

```bash
uv sync                      # creates .venv with runtime and dev dependencies
uv run relaybay serve --reload --home ./.dev-data
```

`--home` keeps development data out of your real data directory (`.dev-data` is yours to delete).

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
