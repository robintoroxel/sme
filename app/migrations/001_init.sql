CREATE TABLE tickets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- queued | running | done | failed | cancelled | reverted
    status      TEXT NOT NULL DEFAULT 'queued',
    model       TEXT NOT NULL DEFAULT 'deepseek/deepseek-v4-pro',
    session_id  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One agent run per ticket submission or follow-up comment.
CREATE TABLE runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id        INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    prompt           TEXT NOT NULL,
    -- queued | running | done | failed | cancelled
    status           TEXT NOT NULL DEFAULT 'queued',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    summary          TEXT,
    commit_sha       TEXT,
    error            TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    started_at       TEXT,
    finished_at      TEXT
);

CREATE INDEX idx_runs_ticket ON runs(ticket_id);
CREATE INDEX idx_runs_status ON runs(status);
