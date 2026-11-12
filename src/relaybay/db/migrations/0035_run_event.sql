-- The stream-json lines of a run's turns, one row per line in the order read (`seq` from 1 per
-- turn). Every string of a line is masked with the journal's secret masking
-- (`domain.environments.redact`) before the line is compressed (zlib, one stream per line) into
-- `data`; nothing unmasked is stored. `parent_tool_use_id` nests a subagent's lines under the tool
-- call that started it. A line over the reader's limit keeps only its masked head (`truncated` =
-- 1; `size` is the length read). `run_turn.report_id` is the report the turn's result became.
CREATE TABLE run_event (
    id                 INTEGER PRIMARY KEY,
    turn_id            INTEGER NOT NULL REFERENCES run_turn(id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    type               TEXT NOT NULL,
    subtype            TEXT,
    parent_tool_use_id TEXT,
    ts                 TEXT NOT NULL,
    size               INTEGER NOT NULL,
    truncated          INTEGER NOT NULL DEFAULT 0 CHECK (truncated IN (0, 1)),
    data               BLOB NOT NULL,
    UNIQUE (turn_id, seq)
);
ALTER TABLE run_turn ADD COLUMN report_id INTEGER REFERENCES report(id) ON DELETE SET NULL;
