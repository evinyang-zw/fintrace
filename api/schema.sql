-- W5: the service's own table. The graph checkpointer has its own tables; this
-- one is the user-facing record of what was asked, what it cost, and whether it
-- still waits for a human.
CREATE TABLE IF NOT EXISTS runs (
    id          UUID PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    question    JSONB NOT NULL,
    status      TEXT NOT NULL,           -- queued|running|awaiting_review|done|failed|cancelled
    answer      JSONB,
    review_payload JSONB,
    usage       JSONB,
    error       TEXT,
    review      BOOLEAN NOT NULL DEFAULT FALSE,
    client_ip   TEXT,
    token_kind  TEXT,
    queued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS runs_status_idx ON runs (status, queued_at DESC);
CREATE INDEX IF NOT EXISTS runs_queued_idx ON runs (queued_at DESC);
