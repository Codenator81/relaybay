-- What the task is about, and a running summary of what is done (both markdown).
ALTER TABLE task ADD COLUMN description TEXT;
ALTER TABLE task ADD COLUMN progress TEXT;
