-- A document that lives in a repository: the board links to it there and does not edit it
-- (doc_update refuses without force). Additive only; existing documents stay unlinked.
ALTER TABLE document ADD COLUMN source_url TEXT;
