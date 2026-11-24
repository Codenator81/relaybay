-- The findings and questions an agent sends with report_add: data the coordinator triages, never
-- a backlog item by itself. An item is open while it has neither a backlog item (backlog_add with
-- from_report) nor a dismissal (report_item_dismiss). `n` counts per report and kind from 1;
-- `title` is a finding's title or a question's text. Additive.
CREATE TABLE report_item (
    id               INTEGER PRIMARY KEY,
    report_id        INTEGER NOT NULL REFERENCES report(id) ON DELETE CASCADE,
    task_id          INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL CHECK (kind IN ('finding', 'question')),
    n                INTEGER NOT NULL,
    title            TEXT NOT NULL,
    body             TEXT,
    location         TEXT,
    item_kind        TEXT,
    area             TEXT,
    question_default TEXT,
    backlog_id       INTEGER REFERENCES backlog_item(id) ON DELETE SET NULL,
    dismissed_at     TEXT,
    dismissed_by     TEXT,
    dismiss_reason   TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE (report_id, kind, n)
);
CREATE INDEX report_item_task ON report_item(task_id);
CREATE INDEX report_item_open ON report_item(id) WHERE backlog_id IS NULL AND dismissed_at IS NULL;
