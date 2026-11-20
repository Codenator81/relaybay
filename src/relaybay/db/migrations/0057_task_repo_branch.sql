-- The branch a task works on in each linked repository (backlog #99): a task in two
-- repositories has a branch in each. Set by the hooks from the folder a bound session works in;
-- NULL until seen, then the card falls back to the task's own branch.
ALTER TABLE task_repo ADD COLUMN branch TEXT;
