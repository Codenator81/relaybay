-- Sections of a project document linked to a card (design 0027), and an area on a decision.
-- Additive only. `anchor` is the name the link was made with (a heading anchor or an explicit
-- anchor), checked against the outline when linked; a section that disappears later keeps its
-- row and shows as broken.
CREATE TABLE task_doc_section (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    anchor      TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (task_id, document_id, anchor)
);
CREATE INDEX task_doc_section_document ON task_doc_section(document_id);

-- What part of the project a decision is about, by area name (case does not matter); NULL for
-- a decision about no area in particular.
ALTER TABLE decision ADD COLUMN area TEXT;
