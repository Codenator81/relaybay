-- Backlog as a queue: a priority (higher first, -9..9) and who the item is for. Additive; existing
-- rows get priority 0 and nobody.
ALTER TABLE backlog_item ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE backlog_item ADD COLUMN assignee TEXT;
CREATE INDEX backlog_item_queue ON backlog_item(project_id, status, priority DESC, id);
