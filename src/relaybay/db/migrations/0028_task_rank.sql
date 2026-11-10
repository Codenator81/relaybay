-- The owner's order of the cards within a stage column: lower rank first. Additive; existing tasks
-- are ranked by the order the board showed until now, so no card moves on the upgrade.
ALTER TABLE task ADD COLUMN rank INTEGER NOT NULL DEFAULT 0;
UPDATE task SET rank = (
    SELECT o.n FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY project_id, stage ORDER BY priority DESC, updated_at DESC, id DESC
        ) AS n
        FROM task
    ) AS o
    WHERE o.id = task.id
);
CREATE INDEX task_board_order ON task(project_id, stage, rank);
