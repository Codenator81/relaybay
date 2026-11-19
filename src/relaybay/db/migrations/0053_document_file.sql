-- Documents that live in a file on disk, the section index and byte-exact import (design 0026).
-- Additive only: existing documents stay database documents with source_path NULL.

-- A file document: `source_path` is the absolute path relaybay reads; the text stays in the file
-- and is never versioned here. The file_* columns are what the last read saw: the content hash
-- (SHA-256 of the bytes), the size and modification time that let a read skip the hash, when the
-- file was last read, and since when it is missing (NULL while it is there).
ALTER TABLE document ADD COLUMN source_path TEXT;
ALTER TABLE document ADD COLUMN file_hash TEXT;
ALTER TABLE document ADD COLUMN file_size INTEGER;
ALTER TABLE document ADD COLUMN file_mtime REAL;
ALTER TABLE document ADD COLUMN file_seen_at TEXT;
ALTER TABLE document ADD COLUMN file_missing_at TEXT;
-- How many lines the index holds masked because a secret detector matched them.
ALTER TABLE document ADD COLUMN masked_lines INTEGER NOT NULL DEFAULT 0;

-- One row per section of a document's current text, in order: the outline and the pointers of
-- links and search hits. Position 0 with level 0 is the text before the first heading, when
-- there is any. Lines are 1-based and inclusive; first_line-last_line covers the subsections,
-- own_last ends the section's own text (the next heading of any level). `hash` is of the own
-- text and tells a changed section from an unchanged one. The searchable text is in the FTS
-- table `doc_section_index`, which the service creates (like `search_index`).
CREATE TABLE document_section (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    position     INTEGER NOT NULL,
    anchor       TEXT NOT NULL,
    alias        TEXT,
    level        INTEGER NOT NULL,
    title        TEXT NOT NULL,
    heading_path TEXT NOT NULL,
    first_line   INTEGER NOT NULL,
    last_line    INTEGER NOT NULL,
    own_last     INTEGER NOT NULL,
    chars        INTEGER NOT NULL,
    bytes        INTEGER NOT NULL,
    hash         TEXT NOT NULL,
    UNIQUE (document_id, position)
);

-- What the import's normalisation removed from a file, so export writes the same bytes back:
-- the line ending ('lf' or 'crlf'), a UTF-8 byte order mark, and the exact whitespace before
-- and after the stored text (as LF text).
ALTER TABLE import_item ADD COLUMN eol TEXT;
ALTER TABLE import_item ADD COLUMN bom INTEGER NOT NULL DEFAULT 0;
ALTER TABLE import_item ADD COLUMN lead_blank TEXT;
ALTER TABLE import_item ADD COLUMN final_newlines TEXT;
