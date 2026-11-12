-- A run handed to the owner in the Claude desktop app (`claude --desktop --resume`): while
-- `with_owner_at` is set relaybay starts no turn of it, texts sent wait. `desktop_opened_at` is
-- the last time the session was opened there. Columns, not a status: the status CHECK of 0031
-- stays as it is and a run with the owner keeps its idle, stopped or lost status.
ALTER TABLE run ADD COLUMN with_owner_at TEXT;
ALTER TABLE run ADD COLUMN with_owner_by TEXT;
ALTER TABLE run ADD COLUMN desktop_opened_at TEXT;
