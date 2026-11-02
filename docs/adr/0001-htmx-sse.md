# ADR-0001: Server-rendered HTMX + SSE front-end

- Status: accepted
- Date: 2026-11-02

## Context

The board shows projects, tasks, stages, reports and live session status. It must update without page
reloads, recover after sleep or a dropped connection, work on a phone, and install with `pipx`/`uvx`
on Windows, macOS and Linux. Most screens are read-mostly; data flows from server to browser, with a
few edits from the browser. Contributors are expected to be Python developers.

## Options

1. **HTMX + Server-Sent Events + Jinja2** — server-rendered HTML, a vendored `htmx.min.js` and its SSE
   extension, no build step.
2. **Small SPA (Preact or Svelte) + WebSocket** — client-side rendering over a JSON API.

## Decision

Option 1.

- SSE matches the one-way data flow. `EventSource` reconnects automatically and sends `Last-Event-ID`;
  the server replays newer rows of the `event` table (whose id is the SSE id) or sends `resync` when
  the client is too far behind. With WebSocket, reconnect, replay and heartbeats would be hand-written.
- SSE messages carry small JSON, not HTML. On an event the page asks HTMX to refetch the affected
  fragment, so rendering lives only in templates and replay is idempotent.
- No Node toolchain: one language in the repository, the wheel ships vendored static assets, no CDN.
- Phones: plain responsive HTML; a `visibilitychange`/`online` listener forces a reconnect on wake-up.

## Consequences

- Rich client interactions (drag-and-drop between stage columns) need a small library such as
  `Sortable.js` or stay out of v1.
- Browser writes are ordinary HTMX `POST`s through the same service layer as MCP and REST, protected by
  cookie session, Host/Origin checks and a CSRF token.
- If the UI outgrows HTMX, a SPA can be added later on top of the same JSON API without changing the
  event protocol.
