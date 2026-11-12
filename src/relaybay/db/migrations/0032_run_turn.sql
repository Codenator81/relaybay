-- One `claude -p` turn of a run: the text it was given, who sent it, when it ran and how it ended,
-- with the tokens of its `result.usage`. `denied_tools` is a JSON list of tool names only, never a
-- tool's input. A turn that waits for the running one has no `started_at`.
CREATE TABLE run_turn (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    prompt             TEXT NOT NULL,
    sent_by            TEXT NOT NULL,
    sent_by_kind       TEXT NOT NULL CHECK (sent_by_kind IN ('owner', 'coordinator', 'relaybay')),
    queued_at          TEXT NOT NULL,
    started_at         TEXT,
    ended_at           TEXT,
    pid                INTEGER,
    outcome            TEXT CHECK (outcome IN
                         ('success', 'error', 'stopped', 'lost', 'cancelled', 'failed_to_start')),
    result_subtype     TEXT,
    exit_code          INTEGER,
    num_turns          INTEGER,
    duration_ms        INTEGER,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    denials            INTEGER NOT NULL DEFAULT 0,
    denied_tools       TEXT,
    UNIQUE (run_id, seq)
);
-- Never two turns of one run at once, held by the database.
CREATE UNIQUE INDEX run_turn_active ON run_turn(run_id)
    WHERE started_at IS NOT NULL AND ended_at IS NULL;
CREATE INDEX run_turn_queued ON run_turn(run_id, seq)
    WHERE started_at IS NULL AND ended_at IS NULL;
