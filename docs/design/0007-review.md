# 0007 — Review: acceptance checklist with screenshots

- Status: **Accepted** (step 11.5), planned for 0.3.0, after comments ([0005](0005-comments-to-agents.md))
  and environments ([0006](0006-environments-and-access.md)).
- Size: ~4–4.5 agent-days (data, storage and safety tests ~1, MCP ~0.5, Review tab and viewer
  ~1–1.5, review stage and paged board ~0.5, Review queue and notification ~0.5–1, screenshot recipes
  ~0.25).

## Goal

The owner accepts a task step by step inside relaybay; the agent prepares the steps and the evidence
and is told as each step is accepted or rejected.

## Model

- `review_step`: id, task_id, position, title, details_md (what, how and why to check), status
  (`open` / `accepted` / `rejected`), decided_at, decided_by, created_by (session), created_at,
  updated_at.
- `attachment`: id, task_id, step_id (nullable), kind (`image`), stored_name (random id +
  extension), original_name, mime, size, sha256, caption_md, created_by, created_at.
- `comment` ([0005](0005-comments-to-agents.md)) gains optional `step_id` / `attachment_id`.
- Events `review.step_added|updated|accepted|rejected`, `attachment.added|removed`.

## Review stage and paged board

- New stage `review` between dev and test: plan → dev → review → test → live → done. Existing tasks
  keep their stage; a migration widens the stage check. MCP tools and rules accept `review`.
- Six columns do not fit one screen, so the board is paged: page 1 = plan, dev, review, test; page 2 =
  live, done. On-screen arrows and keyboard ←/→ switch pages (not inside inputs, text areas or an
  open dialog or viewer); the page is in the URL (`?page=2`) and survives live updates. Phones keep
  the stage tab bar, now with six tabs.

## Owner check lives in the checklist

What the owner should check by eye is written by the agent into the checklist through
`review_steps_set`, not only into the report. Moving a task to `review` or `test` with an empty
checklist returns a hint to fill it in.

## API

`review_steps_set(task, steps[])` (replace: title, details), `review_step_update(task, step, …)`,
`attach_file(task, path | content_base64, step?, caption)`, `attachment_caption(id, caption)`,
`review_get(task)` (steps, statuses, attachments with captions, owner comments). The owner's
decisions reach the bound session through the comment path: "Step 2 'Login with 2FA' accepted —
continue with step 3" / "rejected: <owner comment>".

## UI

- **Review tab in the task dialog** (tabs Overview | Review | History): ordered steps; each row has
  an Accept checkbox, title, an expandable "What, how and why" block, thumbnails with captions and a
  comment box.
- **Viewer:** full screen over the dialog, image fit to screen, caption below, ←/→ and arrows, Esc,
  counter "3 / 7", swipe on phones.
- **Card:** a "Review 2/5" counter when a checklist exists.
- **Review queue in the Inbox** ([0004](0004-navigation.md)): tasks in `review` or `test` with a
  checklist across all projects, oldest first; an item opens the dialog on its Review tab.
- **Browser notification** "Waiting for your acceptance: <task>" when a task moves to `review` or
  `test`; permission is requested only from a button in Settings (Notification API, while a board
  tab is open).
- **Clipboard paste** as the fallback: the owner pastes a screenshot into a step with Ctrl+V.

## File storage and safety

- Files under `<data dir>/attachments/<project>/<task key>/<random id>.<ext>`; the database keeps
  only the stored name. Served by `GET /files/<attachment id>` behind the board cookie with the
  stored `Content-Type`, `X-Content-Type-Options: nosniff`, `Content-Disposition: inline`; the CSP
  stays `img-src 'self'`.
- Allowed: PNG, JPEG, WebP, GIF, checked by magic bytes; SVG and everything else refused. Limits
  10 MB per file and 200 MB per task (`attachments_max_mb`, `attachments_task_max_mb`).
- `attach_file` reads only paths inside `attachment_roots` (default: the project's `repo_path`, the
  user's temp directory, a configured screenshots folder), resolves symlinks and `..` before the
  check, refuses device files, copies (never moves or links) and hashes to avoid duplicates.
  `content_base64` for agents elsewhere, same rules.
- Deleting a task (or archiving with purge) deletes its folder; removing an attachment deletes the
  file; a startup sweep removes orphans.

## How agents produce screenshot files

The desktop app's built-in browser returns a screenshot to the model only, not as a file. Recipes for
agents (published as a how-to with `attach_file` examples):

- **Sign in once, then screenshot by URL** (local and test environments only, never live, and only
  where the environment allows it, see [0006](0006-environments-and-access.md)): sign in once with a
  test account, save the login state (for example a Playwright storage state), then capture pages by
  URL with that state. The saved state and test credentials live outside the project repository and
  outside relaybay's database (a user directory whose path is in the environment's settings);
  relaybay stores only where the state lives. Public examples use neutral demo accounts.
- **Headless Chrome / Edge** without login: `--headless --screenshot=<file> --window-size=… <url>`.
- **Android emulator:** `adb exec-out screencap -p > shot.png`; actions via `adb shell input …` and
  `uiautomator dump`, or a mobile MCP server.
- **iOS simulator (macOS):** `xcrun simctl io booted screenshot shot.png`.
- Then `attach_file(task, path, step, caption)`.

## Risks

| Risk | Mitigation |
|---|---|
| A malicious file served as a page | Magic-byte check, no SVG, `nosniff`, CSP, served behind the cookie |
| Path traversal through `attach_file` | Allowed roots after resolving, copy only, no device files |
| Login state for a real account leaks | Never on live; state outside the repo and the DB; docs use test accounts |
| Disk fills up | Per-file and per-task limits, purge with the task, orphan sweep |
