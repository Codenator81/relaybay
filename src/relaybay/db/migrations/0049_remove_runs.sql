-- 0.12 removed runs inside the program, schedules, the brake, token budgets, the Usage page,
-- intake and messages between sessions (design 0025). Data only: every row stays and no table
-- is dropped, so a return to 0.11 from the backup taken before this start loses nothing. What a
-- removed part left open is closed with one reason; the tables of the removed parts stay unread.
--
-- The worktrees runs created and did not remove are listed once, for one Inbox line (paths
-- only; nothing on disk is touched). Written before the runs are ended, which changes nothing
-- of run_worktree.
INSERT OR REPLACE INTO app_setting (key, value, updated_at, updated_by)
SELECT 'removed_runs_worktrees',
       json_group_array(path),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       'relaybay'
FROM (SELECT path FROM run_worktree
      WHERE created_by_run = 1 AND removed_at IS NULL ORDER BY id)
HAVING count(*) > 0;

-- A turn that waited is cancelled; one that ran when the program stopped lost its process.
UPDATE run_turn
SET outcome = 'cancelled', ended_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
WHERE ended_at IS NULL AND started_at IS NULL;
UPDATE run_turn
SET outcome = 'lost', ended_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
WHERE ended_at IS NULL;

UPDATE run
SET status = 'ended',
    status_reason = 'runs inside the program were removed in 0.12',
    ended_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
    ended_by = 'relaybay',
    updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
WHERE ended_at IS NULL;

-- Questions and approvals a run asked (their asker is gone), and relaybay's own questions about
-- a worktree a card's close kept, are withdrawn with the same reason.
UPDATE question
SET status = 'withdrawn',
    withdrawn_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
    withdrawn_by = 'relaybay: runs inside the program were removed in 0.12'
WHERE status = 'open'
  AND (run_id IS NOT NULL
       OR id IN (SELECT question_id FROM run_worktree WHERE question_id IS NOT NULL));
UPDATE approval
SET status = 'expired',
    expired_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
    expired_reason = 'runs inside the program were removed in 0.12'
WHERE status IN ('open', 'approved') AND run_id IS NOT NULL;

-- Every schedule that could still fire is cancelled, of every kind; a check under way ends.
UPDATE schedule_run
SET outcome = 'stopped',
    ended_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
    reason = 'schedules were removed in 0.12'
WHERE ended_at IS NULL;
UPDATE schedule
SET enabled = 0,
    next_at = NULL,
    done_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
    updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
    updated_by = 'relaybay'
WHERE deleted_at IS NULL AND done_at IS NULL;

-- Intake's "answer received" mark has nothing left to clear it.
UPDATE task SET answer_received_at = NULL WHERE answer_received_at IS NOT NULL;
