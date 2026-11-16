-- Schedules (design 0023): a clock in the program. `kind`: a coordinator turn (a line in its
-- next wake), a read-only check (a one-shot `check` run) or a reminder (a tray notice and an
-- Inbox line, no model). `repeat`: 'once' at `at`; 'daily' at `time_of_day` (HH:MM, the
-- machine's local time, so it follows DST), optionally weekdays only; 'every' `every_minutes`
-- at most `max_times` times (a check also stops when its answer starts with "done").
-- Times are stored in UTC; `timezone` is the local zone's name when the row was saved.
-- `next_at` NULL with `done_at` set: finished (a once that ran, an every that reached its end).
-- `deleted_at`: deleted on the board or by schedule_cancel; the row and its runs stay, so a
-- card's budget keeps counting the tokens its checks spent.
CREATE TABLE schedule (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('coordinator', 'check', 'reminder')),
    repeat          TEXT NOT NULL CHECK (repeat IN ('once', 'daily', 'every')),
    at              TEXT,
    time_of_day     TEXT,
    weekdays_only   INTEGER NOT NULL DEFAULT 0 CHECK (weekdays_only IN (0, 1)),
    every_minutes   INTEGER CHECK (every_minutes IS NULL OR every_minutes >= 5),
    max_times       INTEGER NOT NULL DEFAULT 24 CHECK (max_times BETWEEN 1 AND 24),
    timezone        TEXT NOT NULL,
    project_id      INTEGER REFERENCES project(id) ON DELETE CASCADE,
    task_id         INTEGER REFERENCES task(id) ON DELETE CASCADE,
    profile         TEXT,
    prompt          TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_by_kind TEXT NOT NULL CHECK (created_by_kind IN ('owner', 'coordinator')),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    next_at         TEXT,
    times_run       INTEGER NOT NULL DEFAULT 0,
    done_at         TEXT,
    deleted_at      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    updated_by      TEXT NOT NULL,
    CHECK ((repeat = 'once') = (at IS NOT NULL)),
    CHECK ((repeat = 'daily') = (time_of_day IS NOT NULL)),
    CHECK ((repeat = 'every') = (every_minutes IS NOT NULL)),
    CHECK (kind != 'check' OR project_id IS NOT NULL),
    CHECK (task_id IS NULL OR project_id IS NOT NULL)
);
CREATE INDEX schedule_due ON schedule(next_at)
    WHERE enabled = 1 AND next_at IS NOT NULL AND deleted_at IS NULL;

-- One row per due time handled: `ran`, `missed` (K6: late by an hour or more), `skipped` (with
-- its reason), `failed` or `stopped`; NULL while a check runs. `notice`: its Inbox line (a
-- reminder, the answer of a check without a card), until the owner marks it seen.
CREATE TABLE schedule_run (
    id          INTEGER PRIMARY KEY,
    schedule_id INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
    due_at      TEXT NOT NULL,
    started_at  TEXT,
    ended_at    TEXT,
    outcome     TEXT CHECK (outcome IN ('ran', 'missed', 'skipped', 'failed', 'stopped')),
    reason      TEXT,
    tokens      INTEGER NOT NULL DEFAULT 0,
    run_id      INTEGER REFERENCES run(id) ON DELETE SET NULL,
    turn_id     INTEGER REFERENCES run_turn(id) ON DELETE SET NULL,
    report_id   INTEGER REFERENCES report(id) ON DELETE SET NULL,
    notice      TEXT,
    seen_at     TEXT,
    CHECK ((outcome IS NULL) = (ended_at IS NULL))
);
CREATE INDEX schedule_run_schedule ON schedule_run(schedule_id, id);
-- At most one run of a schedule at a time.
CREATE UNIQUE INDEX schedule_run_open ON schedule_run(schedule_id) WHERE ended_at IS NULL;
CREATE INDEX schedule_run_run ON schedule_run(run_id) WHERE run_id IS NOT NULL;
CREATE INDEX schedule_run_notice ON schedule_run(id) WHERE notice IS NOT NULL AND seen_at IS NULL;
