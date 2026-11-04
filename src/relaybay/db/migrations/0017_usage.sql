-- Token usage read from Claude Code transcripts. Additive only: one new column and three new
-- tables; existing rows keep their data.

-- The transcript file the session's hooks reported last.
ALTER TABLE session ADD COLUMN transcript_path TEXT;

-- One row per transcript file: how far it has been read. A partial last line is not counted in
-- `read_offset`; `skipping` says the reader is inside a line too long to parse.
CREATE TABLE usage_file (
    id            INTEGER PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    session_id    INTEGER REFERENCES session(id) ON DELETE SET NULL,
    parent_id     INTEGER REFERENCES usage_file(id) ON DELETE CASCADE, -- a subagent's transcript
    read_offset   INTEGER NOT NULL DEFAULT 0,
    skipping      INTEGER NOT NULL DEFAULT 0,
    unknown_lines INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL,
    read_at       TEXT
);

-- Totals per file, day (UTC) and model. The file in the key lets a file that shrank replace its
-- own rows.
CREATE TABLE usage_day (
    file_id     INTEGER NOT NULL REFERENCES usage_file(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    session_id  INTEGER REFERENCES session(id) ON DELETE SET NULL,
    model       TEXT NOT NULL,
    input       INTEGER NOT NULL DEFAULT 0,
    output      INTEGER NOT NULL DEFAULT 0,
    cache_read  INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    messages    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_id, day, model)
);
CREATE INDEX usage_day_day ON usage_day(day);
CREATE INDEX usage_day_session ON usage_day(session_id, day);

-- Message ids already counted: a resumed transcript repeats the lines of the one before it.
CREATE TABLE usage_message (
    message_id TEXT PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES usage_file(id) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE INDEX usage_message_file ON usage_message(file_id);
