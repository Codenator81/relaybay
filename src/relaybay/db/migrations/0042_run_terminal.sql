-- The run is with the owner in the board's Terminal tab (`claude --resume` in a PTY of the server):
-- `with_owner_in = 'terminal'`; NULL is the Claude desktop app of 0034. A terminal dies with the
-- server, so a start hands back a run still marked 'terminal'. Additive only.
ALTER TABLE run ADD COLUMN with_owner_in TEXT;
