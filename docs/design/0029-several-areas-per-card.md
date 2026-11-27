# 0029 — Several areas on one card

- Status: **Implemented** (unreleased, for 0.19.0). Changes the "one area per card" rule of
  [0024](0024-card-modes-thread-and-areas.md#areas) (its D1 option "several labels").
- One project often has parts that share one backend: a web cabinet, an admin and a mobile app.
  A card about the API that all three use belongs to "backend" and "api" at once. With one area
  per card the board either loses that or the project is split in two.
- Size: about 1.5 agent-days in one pull request, then the release PR. One migration (0065),
  additive. MCP contract stays **version 3**: parameters widen, no tool is removed or renamed.

## The rule

- A card has **a list of areas**, in order, none to 8, each from the project's list (which still
  grows from use, at most 24 a project, 32 characters a name). The same area twice is one; order
  is the order given, the first is the card's **first area**.
- A name may not contain a comma (a new one with a comma is refused); existing names are kept as
  they are. The comma separates names where a list is one text: the form field, markdown cells.
- **Filter: any of.** The board's area filter, saved filters, `task_list(area=…)` and
  `backlog_list(area=…)` take one name or several; a card matches when it carries any of them.
  One name behaves exactly as today.
- **Rename** keeps the area's id, so every card keeps it under the new name. **Merge** (rename into
  another area's name) moves its cards to the target; a card that had both keeps the target once,
  at the place of the first of the two. **Delete** removes the area from every card's list; the
  others stay. Each card touched gets one `task.updated` with its version raised, as today.
- Saved filters store a list of names: they follow a rename and a merge (two names that merge
  become one) and drop a deleted name; a filter whose only area is deleted loses the area filter.

## Data and migration

- `task_area (task_id, area_id, position)`, primary key (task_id, area_id), both `ON DELETE
  CASCADE`, index on `area_id`. Migration 0065 copies every `task.area_id` into it at position 0.
  Migration 0048 made an index named `task_area`, and tables and indexes share one namespace: 0065
  first drops it and creates the same partial index as `task_first_area`.
- The list is read in one correlated subquery per card (`json_group_array`); its order comes from
  `position`, sorted in Python, since `ORDER BY` inside an aggregate needs SQLite 3.44.
- `task.area_id` stays and is kept equal to the first area (null with none). It is not read for
  the list; it keeps a rollback to 0.18 showing the first area, and avoids rebuilding `task`
  (SQLite cannot drop a column that is a foreign key).
- `saved_filter.areas` (JSON array of names), filled from `saved_filter.area`; `area` is kept equal
  to the first name for the same reason.
- No name, colour or id of an existing area changes: the migration only adds rows.

## Writes

- `task_create(area=…)` and `task_update(area=…)` take a string or a list of strings. A string is
  one area (as today) unless it has a comma, then it is split: "web, docs" is two. A list replaces
  the card's areas; `-`, `""`, `[]` or null clears them. `task_update(fields={"area": …})` and
  `fields={"areas": …}` take the same. New names are added to the project as today, each with its
  `Area '<name>' was new …` line.
- Events: `task.updated` carries `changes.areas` (the new list of names) and, for older readers,
  `changes.area` (the first name, or null). `project.updated` is unchanged.
- The task form has one text field "Areas" (comma-separated) with a checkable chip per existing
  area of the project under it; the chips are the suggestions (no datalist, which offers one name
  for the whole field).

## Reads

- Card chips: one chip per area, in order (the board card, the dialog head, inbox rows).
- `task_get`, `task_context`, the digests, `task_list` rows, markdown export: `Area: web` for one,
  `Areas: web, docs` for several. Export writes the names comma-separated in the `area` column;
  import splits on commas, except a cell that is a whole existing area name.
- `relaybay://schema/task` lists `area` as "string or list of strings".
- `project_snapshot` counts a card once in each area it carries.
- `task_context` lists a project decision on a card when its `area` is any of the card's areas; the
  subject words take all the card's area names.

## Backlog

- A backlog item keeps one free-text area (unchanged). `backlog_list(area=…)` takes one or several
  (any of). "To plan" gives the new card the item's area as its only area.

## Board filter

- The select "Any area" becomes a dropdown of checkboxes (the project's areas) with "Any area" as
  the empty state; the URL carries `area` once per chosen name (`?area=web&area=api`), and a
  comma-free single `area` URL from before opens as before.

## Options considered

| # | Decision | Chosen | Alternatives |
|---|---|---|---|
| A1 | Storage | join table `task_area`, `task.area_id` mirrors the first | JSON column on `task`; drop `area_id` (table rebuild) |
| A2 | Filter semantics | any of | all of; a switch between both |
| A3 | MCP shape | `area` takes string or list | a new `areas` parameter only (breaks nothing, but two names for one thing) |
| A4 | Limit per card | 8 | none; 3 |
| A5 | Backlog | stays one area | a list too (no use asked for) |

## Tests

Migration on a database with areas, cards and a saved filter (lists of one, ids and names equal);
create and update with a string, a comma string, a list, clear; any-of filter on the board, saved
filters, `task_list`, `backlog_list`; rename, merge with a card carrying both, delete leaving the
other area; events carry both shapes; markdown round trip with two areas.
