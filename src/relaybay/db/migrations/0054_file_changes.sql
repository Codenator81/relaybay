-- File documents written through relaybay (design 0027, part A). Additive only.

-- Who made the last change of a file document's file that relaybay saw: 'relaybay' (a doc_update)
-- or 'outside' (an editor, a script, a session's own file tools, noticed by a read); when, and the
-- anchors of the sections it changed (comma-separated). NULL until the first change after this.
ALTER TABLE document ADD COLUMN file_changed_by TEXT;
ALTER TABLE document ADD COLUMN file_changed_at TEXT;
ALTER TABLE document ADD COLUMN file_changed_sections TEXT;
-- Writes through relaybay into the file are opt-in per document, turned on by a coordinator;
-- existing file documents stay off.
ALTER TABLE document ADD COLUMN file_writable INTEGER NOT NULL DEFAULT 0;
-- A file document's version counts the hashes its file has had. 0.14 kept every file document
-- at 0, which base_version reads as "create": its first hash is version 1.
UPDATE document SET version = 1 WHERE source_path IS NOT NULL AND version = 0;
