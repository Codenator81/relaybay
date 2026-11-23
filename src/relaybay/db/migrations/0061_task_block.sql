-- A card blocked by another card (of any project on the board). Whether a blocker still blocks
-- is read from its stage on every read (plan, dev, review, test block; live and done do not), so
-- a stage move writes nothing here. Both ends go with their task. Additive.
CREATE TABLE task_block (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    blocker_id INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, blocker_id)
);
CREATE INDEX task_block_blocker ON task_block(blocker_id);
