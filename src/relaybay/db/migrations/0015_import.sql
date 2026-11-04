-- What `relaybay import` wrote, one row per item, so a re-run writes nothing and a target changed on
-- the board since its import is found. Additive only.

-- kind + item name the imported thing (`document` + `acme-shop/spec`), independent of the file it
-- came from; target_id is the row it became in the table of that kind, so there is no foreign key.
-- content_hash is the file's value at the import, target_hash the target's value right after it.
CREATE TABLE import_item (
    kind         TEXT NOT NULL CHECK (kind IN ('document', 'task', 'task_note', 'decision', 'note')),
    item         TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    target_id    INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    target_hash  TEXT NOT NULL,
    imported_at  TEXT NOT NULL,
    PRIMARY KEY (kind, item)
);
