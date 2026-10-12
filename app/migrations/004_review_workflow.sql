-- Staging/review workflow: every run works on its own git branch (tix/run-N) in a
-- separate worktree; runs that need approval wait in status 'review'.
ALTER TABLE runs ADD COLUMN branch TEXT;
ALTER TABLE runs ADD COLUMN review_reason TEXT;
