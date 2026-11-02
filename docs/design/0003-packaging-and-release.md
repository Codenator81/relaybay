# 0003 — Packaging and release 0.1.0

- Status: **Accepted**. Next after the documentation restructure, before the 0.2.0 features.
- Size: ~3–4 agent-days (packaging and release workflow ~1, `doctor` ~0.5, `demo` ~0.5, agent
  install page and Getting started ~0.5, community files ~0.5, generated reference and drift checks
  ~0.5–1, GIF ~0.25).

## Goal

A person (or their coding agent) installs relaybay with one command on Windows, macOS or Linux,
connects Claude Code and sees the live board; an agent can diagnose the setup without guessing.

## Distribution

- **Wheel and sdist** from `pyproject.toml` (hatchling); static assets and migrations inside the
  wheel.
- **Install:** `uv tool install relaybay`, `uvx relaybay`, `pipx install relaybay`; Docker image
  `ghcr.io/<owner>/relaybay` (`python:3.12-slim`, non-root, volume `/data`, binds `0.0.0.0` inside
  the container, docs publish only `127.0.0.1:8765:8765`; multi-arch amd64/arm64).
- **PyPI** only by trusted publishing (OIDC from the release workflow, no token in the repository),
  once the name is reserved. Until then a release is a GitHub Release with the wheel and sdist
  attached, installable with `uv tool install <wheel URL>`.
- **Release workflow** on a SemVer tag `v*`: build, test the wheel in a clean environment, publish to
  PyPI (trusted publishing), push the GHCR image, create the GitHub Release with the notes of that
  version from `CHANGELOG.md`, deploy the docs.

## `relaybay doctor [--json]`

The first thing an agent runs when something is off. Prints: relaybay version, Python, OS, data dir
and whether it is writable, token file present and private, server reachable on the configured port
with the same `token_id`, MCP `initialize`, hook entries and their interpreter, MCP registration,
rules hash, minimum supported Claude Code version versus the detected one. `--json` gives
machine-readable items; non-zero exit on a failure. Shares checks with `relaybay setup --check`.

## Agent install page

"Install relaybay with your coding agent": a self-contained, idempotent procedure an agent follows
for a user: detect OS, install, start the server as a background process, run
`relaybay setup --dry-run`, show the plan to the user, apply only after the user agrees, verify with
`relaybay setup --check --json`. Includes a copy-paste prompt ("Install and connect relaybay
following <url>").

## `relaybay demo`

Seeds a neutral demo workspace (`acme-shop`, `demo-api`: tasks in every stage, reports, a tracker
mark, a stale next step, a decision) into a separate data dir. Used for docs screenshots, the README
GIF and manual testing. Never touches an existing database unless asked.

## Community and repository files

Issue forms (`bug_report.yml`, `feature_request.yml`, `config.yml` with blank issues off and links
to Discussions and private vulnerability reporting), PR template (what/why, how tested, docs and
CHANGELOG updated, no secrets or personal data), labels (`bug`, `enhancement`, `good first issue`,
`help wanted`), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md` (supported versions,
private reporting, response expectations), a short GOVERNANCE note (the maintainer decides; the
roadmap is public), Discussions with Ideas, Q&A, Show and tell. README: badges (CI, license, PyPI),
GIF, "Contributing and feedback".

## Docs drift checks in CI

Generated CLI reference (from the argument parser), configuration reference (from the settings) and
MCP tool list (from the live registry) must match the committed pages; every MCP tool is documented;
links are valid (`mkdocs build --strict`); a user-visible change touches `CHANGELOG.md`. CI also
smoke-starts the server and hits `/healthz` and MCP `initialize`.

## Support matrix

Python 3.12+; Windows, macOS, Linux in CI; evergreen browsers including phones. The minimum Claude
Code version and the MCP protocol version are stated in SPEC and checked by `doctor`.

## Risks

| Risk | Mitigation |
|---|---|
| The PyPI name is taken or not yet reserved | GitHub Release with artifacts; PyPI when trusted publishing is configured |
| Install differs per OS (`claude.cmd`, paths, encodings) | Windows in CI, `doctor` reports the detected paths |
| A release leaks a secret | Trusted publishing, no tokens in the repository, secret scanning with push protection |
