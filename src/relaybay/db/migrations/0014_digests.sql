-- Digests: the project pulse and what each reader has read. Additive only.

-- A short summary of where the project stands, written by agents with project_pulse_set.
ALTER TABLE project ADD COLUMN pulse TEXT;
ALTER TABLE project ADD COLUMN pulse_at TEXT;
ALTER TABLE project ADD COLUMN pulse_by TEXT;

-- How far a reader (a session's desktop id or name) has read the events of one digest. ref_id is
-- the project or task id, 0 for the portfolio: SQLite treats NULLs in a key as all different.
-- read_at is when the cursor last moved; a read that found only noise leaves it where it is.
CREATE TABLE reader_cursor (
    reader        TEXT NOT NULL,
    level         TEXT NOT NULL CHECK (level IN ('portfolio', 'project', 'task')),
    ref_id        INTEGER NOT NULL DEFAULT 0,
    last_event_id INTEGER NOT NULL,
    read_at       TEXT NOT NULL,
    PRIMARY KEY (reader, level, ref_id)
);
