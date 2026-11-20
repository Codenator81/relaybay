-- When the owner first saw a backlog item on the board (it scrolled into view). An item not
-- seen yet carries the "new" mark there. Items decided before this migration count as seen.
ALTER TABLE backlog_item ADD COLUMN seen_at TEXT;
UPDATE backlog_item SET seen_at = decided_at WHERE decided_at IS NOT NULL;
