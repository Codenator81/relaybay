## What and why

<!-- What this pull request changes and why. Link the issue: "Closes #123". -->

## How it was tested

<!-- Commands you ran and what you checked by hand (screens, OS). -->

- [ ] `uv run ruff check` and `uv run ruff format --check`
- [ ] `uv run pytest`
- [ ] `uv run python scripts/gen_reference.py --check` (when the CLI, settings or MCP tools changed)

## Checklist

- [ ] Docs updated for a change in behaviour, a command, a setting or an MCP tool (`README.md`,
      `docs/SPEC.md`, the reference pages)
- [ ] `CHANGELOG.md` updated under `[Unreleased]` for a user-visible change
- [ ] No secrets, tokens, personal paths or personal data (examples use `acme-shop` / `demo-api`)
