-- FinTrace storage schema (W1)
-- 设计原则：
--   1. filed_at 贯穿 filings / facts，是 point-in-time 过滤的物理基础
--   2. chunk 的 char offset 落在父 section 内，W3 换切块策略时不改表结构
--   3. 数字只进 facts 表，正文只进 sections/chunks，两者不混

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS companies (
    cik             TEXT PRIMARY KEY,
    ticker          TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    group_name      TEXT,
    fiscal_year_end TEXT,
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS filings (
    accession   TEXT PRIMARY KEY,
    cik         TEXT NOT NULL REFERENCES companies (cik),
    form        TEXT NOT NULL,
    period_end  DATE,
    filed_at    DATE NOT NULL,
    primary_doc TEXT,
    raw_path    TEXT,
    raw_sha256  TEXT,
    source      TEXT NOT NULL DEFAULT 'edgar',
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS filings_cik_filed_idx ON filings (cik, filed_at DESC);
CREATE INDEX IF NOT EXISTS filings_form_idx ON filings (form);

CREATE TABLE IF NOT EXISTS sections (
    id         BIGSERIAL PRIMARY KEY,
    accession  TEXT NOT NULL REFERENCES filings (accession) ON DELETE CASCADE,
    item_code  TEXT NOT NULL,
    heading    TEXT,
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL,
    text       TEXT NOT NULL,
    UNIQUE (accession, item_code)
);

CREATE TABLE IF NOT EXISTS chunks (
    id         BIGSERIAL PRIMARY KEY,
    accession  TEXT NOT NULL REFERENCES filings (accession) ON DELETE CASCADE,
    filed_at   DATE NOT NULL,
    item_code  TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL,
    text       TEXT NOT NULL,
    n_tokens   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS chunks_fts_idx
    ON chunks USING GIN (to_tsvector('english', text));
CREATE INDEX IF NOT EXISTS chunks_accession_idx ON chunks (accession);

-- 结构化财务事实：所有数字的唯一来源
CREATE TABLE IF NOT EXISTS facts (
    id           BIGSERIAL PRIMARY KEY,
    cik          TEXT NOT NULL REFERENCES companies (cik),
    tag          TEXT NOT NULL,
    unit         TEXT NOT NULL,
    period_start DATE NOT NULL DEFAULT '1900-01-01',
    period_end   DATE NOT NULL,
    fiscal_year  INTEGER,
    fiscal_period TEXT,
    value        NUMERIC NOT NULL,
    filed_at     DATE NOT NULL,
    form         TEXT,
    accn         TEXT,
    frame        TEXT,
    UNIQUE (cik, tag, unit, period_start, period_end, accn)
);

CREATE INDEX IF NOT EXISTS facts_lookup_idx ON facts (cik, tag, period_end DESC);
CREATE INDEX IF NOT EXISTS facts_filed_idx ON facts (cik, filed_at DESC);

-- W3: embeddings in their own table. Keeping the model name in the key means a
-- provider switch never silently mixes two vector spaces in one query.
-- No HNSW at 20k chunks: a filtered exact scan is milliseconds and cannot lose
-- recall to an approximate index.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id BIGINT NOT NULL REFERENCES chunks (id) ON DELETE CASCADE,
    model    TEXT NOT NULL,
    dim      INTEGER NOT NULL,
    vec      vector NOT NULL,
    PRIMARY KEY (chunk_id, model)
);

CREATE INDEX IF NOT EXISTS chunk_vectors_model_idx ON chunk_vectors (model);
