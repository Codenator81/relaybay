# 0009 — Annotating screenshots

- Status: **Implemented** (step 11.5b, unreleased), for 0.4.0, on top of Review
  ([0007](0007-review.md)). Shipped behaviour: [SPEC](../SPEC.md#spec-annotations).
- Size: ~2 agent-days (data and APIs ~0.5, viewer drawing tools and SVG layer ~1, flatten-and-send
  and MCP image output ~0.5).

## Goal

In the full-screen viewer the owner draws on a screenshot and drops numbered pins with comments; the
agent gets the pins with coordinates plus a flattened PNG it can look at.

## Model

- `annotation`: id, attachment_id, kind (`rect` / `arrow` / `freehand` / `pin`), colour, stroke
  width, geometry as JSON in **fractions of the image size** (rect `x,y,w,h`; arrow `x1,y1,x2,y2`;
  freehand a list of points, simplified on save; pin `x,y`), pin number (per attachment), body_md
  (pin comment), created_by, created_at, deleted_at.
- `annotation_reply`: id, annotation_id, author (owner or session), body_md, created_at.
- `attachment.annotated_name`: the latest flattened PNG; the original file never changes.
- Events `annotation.added|updated|removed`, `annotation.replied`.

## Flattening in the browser

The viewer has the image and the vector layer; on "Send to agent" it renders both at the original
resolution on a `<canvas>` (`canvas.toBlob('image/png')`) and uploads it (`POST
/files/<id>/annotated`, same CSRF, type and size checks as uploads). No server-side imaging
dependency; the vector data stays the source of truth and the PNG is regenerated on the next send.
Server-side rendering is a fallback only if a headless client ever needs it (not planned).

## Delivery and API

"Send to agent" posts one comment through the comment path ([0005](0005-comments-to-agents.md)):
"Screenshot 3 of step 2 — 2 pins: #1 (x 0.42, y 0.18) 'Button text is cut off'; #2 (x 0.70,
y 0.65) 'Wrong currency'. Annotated image: relaybay://attachments/<id>/annotated". `review_get` and
`attachment_get(id, annotated=true)` return the PNG as MCP image content. The agent answers a pin with
`annotation_reply(annotation, body)`.

## UI

Viewer toolbar: Select, Rectangle, Arrow, Pen, Pin, 4–5 fixed colours, Undo, Delete, "Hide markup",
"Send to agent". Vector layer as SVG over the image (scales with it; ←/→ still page between
screenshots, Esc closes). Pins show their number; clicking a pin opens its thread. Touch works on
phones (pen and pin; rectangles by drag).

## As built

Choices made while implementing, where the design left room:

- Geometry is checked on the server: numbers only, up to 0.01 past an edge held to the edge, at most
  5000 points in and 500 kept on a pen line, at most 200 live annotations per screenshot. A long line
  is first thinned in slices that keep their farthest point, then Ramer-Douglas-Peucker runs once
  with a fixed tolerance in a worker thread: under 100 ms at the cap, and a real shape never collapses
  to its two ends. The stroke width is a fraction of the image's width, and the
  toolbar has no choice of width.
- `attachment.annotated_size` was added beside `annotated_name`: the task's limit needs the size
  without touching the disk. `annotated_sha256` and `annotated_sent_sha256` tell a changed image from
  a repeated send.
- Colours: red, amber, green, blue, near-black. The list lives in `domain/annotations.py` and the
  toolbar is rendered from it.
- Each shape is saved when drawn, so nothing is lost on paging or a closed tab. A pin's comment is
  saved on Save or on leaving its box, and before a send.
- Select only selects; shapes do not move or resize (delete and draw again). Undo covers this
  visit's additions and deletions on the screenshot on screen, through soft delete and restore.
- Pin text and replies are plain text in the viewer, not markdown: the least to escape.
- Only the comment shortens a pin's text (300 characters), and it then says `review_get` has it whole;
  `review_get` and `attachment_get` always give pins and replies in full.
- The comment names each pin's annotation id, because `annotation_reply` needs it, and lists the other
  markup under "Also marked". It also says how to answer, so the server instructions did not change.
- A reply also goes through the task's thread, in both directions: an owner's reply is an owner comment
  (delivered, wakes the session), an agent's is an agent reply (Inbox). Without this a reply would be
  seen only by someone who opens that pin. An agent's reply marks the owner's unread comments on that
  screenshot read, as `comment_add` does.
- Pins and replies pass the secret scan, like notes and documents. Plain comments have no such scan
  today, and the comment path is unchanged.
- Sending the same text with the same image again is a no-op, like a repeated review decision.
- Size guard for MCP answers: 3.5 MB per image (about 5 MB of base64), 7990 px on a side (Claude's
  vision guide allows 8000x8000 px) and 7 MB in one `review_get`.
- While Send to agent runs, paging, closing and the tools wait. Other writes carry the id of the
  screenshot they were made on, so paging away from them is safe.
- On the task page a live event marks the page stale while the viewer is open; it refreshes on close.
- A previous flattened file that cannot be removed is logged and left to the startup sweep.
- `relaybay://attachments/{attachment_id}/annotated` is a real resource, so the address in the comment
  resolves.
- A flattened PNG over `attachments_max_mb` is refused and the viewer says so; there is no automatic
  downscale, as the design asks for the original resolution.

## Risks

| Risk | Mitigation |
|---|---|
| Coordinates drift when the image is scaled | Geometry in fractions of the image size |
| Uploaded "annotated" file is not an image | Same magic-byte, type and size checks as attachments |
| Agent cannot see the markup | Flattened PNG as MCP image content plus pin coordinates in text |
