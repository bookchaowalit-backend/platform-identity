-- Standalone identity admission state; no platform identity/membership writers.
CREATE SCHEMA IF NOT EXISTS identity_rate_limit;
REVOKE ALL ON SCHEMA identity_rate_limit FROM PUBLIC;
CREATE TABLE identity_rate_limit.windows (
    key_hash text PRIMARY KEY CHECK (key_hash ~ '^[a-f0-9]{64}$'),
    window_start bigint NOT NULL CHECK (window_start >= 0),
    hits integer NOT NULL CHECK (hits >= 0),
    expires_at timestamptz NOT NULL
);
CREATE INDEX windows_expiry ON identity_rate_limit.windows (expires_at, key_hash);
REVOKE ALL ON identity_rate_limit.windows FROM PUBLIC;
