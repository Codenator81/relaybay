-- An inspect-only card (an analysis, a repro, a question answered by reading): it changes no
-- code, so the rule "accepted plan before dev" does not apply to it. Additive.
ALTER TABLE task ADD COLUMN inspect_only INTEGER NOT NULL DEFAULT 0;
