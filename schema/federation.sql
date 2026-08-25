-- FRAN federation — schema additions.  Apply AFTER schema/fran_schema.sql:
--     psql "$FRAN_DB_URL" -f schema/federation.sql
--
-- Follows FEDERATION_DESIGN.md. Node identity and the peer registry are deliberately NOT here:
-- node_id lives in config and the registry is a git repo (design §1, §2), so the only thing the
-- database needs to hold is per-search shareability — which is per-row data, not configuration.

-- §4.3 DEFAULT-DENY WITH AN EMBARGO FLAG.
-- New ingests are 'hidden'. An admin promotes a search when it is publishable. The default is the
-- entire point: a lab that leaks an unpublished dataset once will not federate again, so the failure
-- mode of forgetting to set this must be "shares nothing", never "shares everything".
ALTER TABLE delimp_searches
    ADD COLUMN IF NOT EXISTS federation_visibility text NOT NULL DEFAULT 'hidden';

DO $$ BEGIN
    ALTER TABLE delimp_searches ADD CONSTRAINT delimp_searches_federation_visibility_check
        CHECK (federation_visibility IN ('hidden','counts_only','public'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Optional self-expiring embargo: a date after which 'hidden' may be treated as 'public' by the
-- promotion job. Never applied automatically at query time -- an embargo that lifts itself inside a
-- request is a leak waiting on a clock.
ALTER TABLE delimp_searches
    ADD COLUMN IF NOT EXISTS federation_release_date date;

-- The federation router filters on this on every request, so it must not be a seq scan over the
-- searches table. Partial: the overwhelming majority of rows are 'hidden' and never selected.
CREATE INDEX IF NOT EXISTS idx_searches_federation_visible
    ON delimp_searches (federation_visibility)
    WHERE federation_visibility <> 'hidden';

COMMENT ON COLUMN delimp_searches.federation_visibility IS
    'hidden (default, never leaves this node) | counts_only (contributes to presence/aggregates, no '
    'per-observation detail) | public (full public-tier detail). Set by an admin, never by ingest.';
COMMENT ON COLUMN delimp_searches.federation_release_date IS
    'Optional date an embargo may be lifted BY A PROMOTION JOB. Not evaluated at query time.';

-- ── ANTI-EXFILTRATION ────────────────────────────────────────────────────────────────────────────
-- Rate limiting alone does not stop corpus theft. app/ratelimit.py is per-worker, in-process and
-- windowed in seconds: it blocks a tight loop, but a peer asking politely for 1 peptide/second
-- retrieves ~2.6M observations a month and nothing ever trips. The control that actually bounds
-- exfiltration is a CUMULATIVE, DURABLE budget, which means it has to live in the database rather
-- than in a worker's memory.
--
-- The log IS the budget. Deriving spend by summing this table (instead of keeping a counter) means
-- the number can never drift from the audit trail, and "what did they take?" is answerable.
CREATE TABLE IF NOT EXISTS delimp_federation_access_log (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    peer        text NOT NULL,              -- node_id, or 'anonymous'
    endpoint    text NOT NULL,
    query_hash  text NOT NULL,              -- hash of the normalized query: repeat vs novel
    query       jsonb,                      -- what was asked (no results, no payload)
    n_rows      integer NOT NULL DEFAULT 0, -- rows actually returned = budget consumed
    decision    text NOT NULL DEFAULT 'allow'  -- allow | deny_budget | deny_shape | deny_rate
);
CREATE INDEX IF NOT EXISTS idx_fedlog_peer_at ON delimp_federation_access_log (peer, at DESC);
CREATE INDEX IF NOT EXISTS idx_fedlog_at      ON delimp_federation_access_log (at DESC);
-- Novelty ratio per peer is the enumeration signal; this index makes the window scan cheap.
CREATE INDEX IF NOT EXISTS idx_fedlog_peer_hash ON delimp_federation_access_log (peer, query_hash, at DESC);

-- Per-peer overrides. A row here raises or lowers the defaults for one collaborator without
-- loosening them for everyone -- the usual reason a global limit gets raised and never lowered.
CREATE TABLE IF NOT EXISTS delimp_federation_quota (
    peer            text PRIMARY KEY,
    rows_per_day    integer,                -- NULL = use the configured default
    rows_per_month  integer,
    max_rows_per_response integer,
    note            text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE delimp_federation_access_log IS
    'Every federation request: who, what shape, how many rows, allowed or denied. Doubles as the '
    'cumulative budget ledger (spend = SUM(n_rows) over the window) so the budget cannot drift from '
    'the audit trail.';
