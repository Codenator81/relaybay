# 0009 — Annotating screenshots

- Status: **Accepted** (step 11.5b), planned for 0.4.0, on top of Review ([0007](0007-review.md)).
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

## Risks

| Risk | Mitigation |
|---|---|
| Coordinates drift when the image is scaled | Geometry in fractions of the image size |
| Uploaded "annotated" file is not an image | Same magic-byte, type and size checks as attachments |
| Agent cannot see the markup | Flattened PNG as MCP image content plus pin coordinates in text |
