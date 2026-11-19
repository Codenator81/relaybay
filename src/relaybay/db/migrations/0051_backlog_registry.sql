-- The backlog as a bug registry (design 0026): what kind of finding it is, where in the code, the
-- source and the decision in words, the item it duplicates and the day it was found. Additive and
-- all nullable: existing items keep every value and have no kind.
ALTER TABLE backlog_item ADD COLUMN kind TEXT CHECK (kind IN ('bug', 'idea', 'question', 'debt'));
ALTER TABLE backlog_item ADD COLUMN location TEXT;
ALTER TABLE backlog_item ADD COLUMN origin_text TEXT;
ALTER TABLE backlog_item ADD COLUMN triage_note TEXT;
ALTER TABLE backlog_item ADD COLUMN duplicate_of INTEGER
    REFERENCES backlog_item(id) ON DELETE SET NULL;
ALTER TABLE backlog_item ADD COLUMN found_on TEXT;
CREATE INDEX backlog_item_duplicate ON backlog_item(duplicate_of);
