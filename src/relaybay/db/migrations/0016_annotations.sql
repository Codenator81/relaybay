-- Screenshot annotations. Additive only: two new tables and two new columns; existing rows keep
-- their data.

-- Markup on a screenshot. Geometry is JSON in fractions of the image size. A removed annotation
-- keeps its row (deleted_at), so the pin number it took is never given out again.
CREATE TABLE annotation (
    id            INTEGER PRIMARY KEY,
    attachment_id INTEGER NOT NULL REFERENCES attachment(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('rect', 'arrow', 'freehand', 'pin')),
    colour        TEXT NOT NULL,
    stroke_width  REAL NOT NULL,
    geometry      TEXT NOT NULL,
    pin_number    INTEGER,
    body_md       TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    deleted_at    TEXT,
    UNIQUE (attachment_id, pin_number)
);
CREATE INDEX annotation_attachment ON annotation(attachment_id, id);

-- The thread of a pin: the owner and the agents.
CREATE TABLE annotation_reply (
    id            INTEGER PRIMARY KEY,
    annotation_id INTEGER NOT NULL REFERENCES annotation(id) ON DELETE CASCADE,
    author_kind   TEXT NOT NULL CHECK (author_kind IN ('owner', 'agent')),
    author        TEXT NOT NULL,
    session_id    INTEGER REFERENCES session(id) ON DELETE SET NULL,
    body_md       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX annotation_reply_annotation ON annotation_reply(annotation_id, id);

-- The latest flattened PNG of a screenshot, in the task's folder; the original never changes. Its
-- size counts in the task's limit; its hash and the hash last sent tell a changed image from a
-- repeated send.
ALTER TABLE attachment ADD COLUMN annotated_name TEXT;
ALTER TABLE attachment ADD COLUMN annotated_size INTEGER;
ALTER TABLE attachment ADD COLUMN annotated_sha256 TEXT;
ALTER TABLE attachment ADD COLUMN annotated_sent_sha256 TEXT;
