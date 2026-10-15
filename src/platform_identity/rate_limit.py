"""Opt-in database-clock token admission, without platform-table dependencies."""
from __future__ import annotations

import argparse
import hashlib
from importlib.resources import files
import math
import os

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

_GLOBAL = hashlib.sha256(b'identity-token-rate-limit-global-v1').hexdigest()
_VERSION = '0001_token_rate_limit'


class RateLimitUnavailable(RuntimeError):
    """Admission could not be established; callers must fail closed."""


def _migration():
    source = files('platform_identity').joinpath('migrations', _VERSION+'.sql').read_text()
    return source, hashlib.sha256(source.encode()).hexdigest()


def bounded_pool(dsn: str, *, max_size: int = 8, timeout: float = 5,
                 statement_timeout_ms: int = 5000) -> ConnectionPool:
    if type(max_size) is not int or not 1 <= max_size <= 32:
        raise ValueError('pool maximum must be 1..32')
    if type(timeout) not in (int,float) or not math.isfinite(timeout) or not .1 <= timeout <= 30:
        raise ValueError('database timeout must be .1..30 seconds')
    if type(statement_timeout_ms) is not int or not 100 <= statement_timeout_ms <= 30000:
        raise ValueError('statement timeout must be 100..30000 milliseconds')

    def configure(conn):
        if conn.execute('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone() != (False,):
            raise ValueError('serving role must be NOSUPERUSER NOBYPASSRLS')
        for name in ('statement_timeout','lock_timeout','idle_in_transaction_session_timeout'):
            conn.execute('SELECT set_config(%s,%s,false)',(name,str(statement_timeout_ms)))
        conn.commit()

    def reset(conn):
        conn.rollback()
        conn.execute("SELECT set_config('app.workspace_id','',false)")
        conn.commit()

    return ConnectionPool(dsn,min_size=0,max_size=max_size,max_waiting=max_size*4,
                          timeout=timeout,max_lifetime=1800,max_idle=300,
                          kwargs={'connect_timeout':max(1,math.ceil(timeout))},
                          configure=configure,reset=reset,open=True)


class PostgresRateLimiter:
    """Global requests include per-key denials. One-minute windows use only DB time."""
    def __init__(self, dsn: str, *, per_key: int = 30, global_limit: int = 300,
                 pool_max: int = 4, timeout: float = 5, statement_timeout_ms: int = 5000):
        if any(type(n) is not int or not 1 <= n <= 1_000_000 for n in (per_key,global_limit)):
            raise ValueError('rate limits must be 1..1000000')
        self.per_key, self.global_limit = per_key, global_limit
        self._checksum = _migration()[1]
        self._pool = bounded_pool(dsn,max_size=pool_max,timeout=timeout,statement_timeout_ms=statement_timeout_ms)

    def _check(self, conn):
        row = conn.execute('SELECT checksum FROM identity_rate_limit.schema_migrations WHERE version=%s',(_VERSION,)).fetchone()
        if row != (self._checksum,):
            raise RateLimitUnavailable('admission unavailable')
        permissions = conn.execute("SELECT NOT (rolsuper OR rolbypassrls) AND has_schema_privilege(current_user,'identity_rate_limit','USAGE') AND "
                                   "has_table_privilege(current_user,'identity_rate_limit.windows','SELECT') AND "
                                   "has_table_privilege(current_user,'identity_rate_limit.windows','INSERT') AND "
                                   "has_table_privilege(current_user,'identity_rate_limit.windows','UPDATE') AND "
                                   "has_table_privilege(current_user,'identity_rate_limit.windows','DELETE') "
                                   'FROM pg_roles WHERE rolname=current_user').fetchone()
        if permissions != (True,):
            raise RateLimitUnavailable('admission unavailable')
        conn.execute('SELECT key_hash,window_start,hits,expires_at FROM identity_rate_limit.windows WHERE false')

    def ready(self) -> bool:
        try:
            with self._pool.connection() as conn:
                self._check(conn)
                return conn.execute('SELECT 1 FROM identity_rate_limit.windows WHERE key_hash=%s',(_GLOBAL,)).fetchone() == (1,)
        except Exception:
            return False

    def allow(self, key: str) -> bool:
        try:
            if not isinstance(key,str) or not 1 <= len(key) <= 256:
                raise ValueError('invalid admission key')
            fingerprint = hashlib.sha256(('identity-token-client-v1:'+key).encode()).hexdigest()
            with self._pool.connection() as conn:
                with conn.transaction():
                    self._check(conn)
                    # The migration seeds this singleton. Lock global first, then
                    # obtain DB time after waiting, then lock/update the client.
                    global_row = conn.execute('SELECT window_start,hits FROM identity_rate_limit.windows WHERE key_hash=%s FOR UPDATE',(_GLOBAL,)).fetchone()
                    if global_row is None:
                        raise RateLimitUnavailable('admission unavailable')
                    window = conn.execute('SELECT floor(extract(epoch FROM clock_timestamp())/60)::bigint').fetchone()[0]
                    conn.execute('DELETE FROM identity_rate_limit.windows WHERE key_hash IN ('
                                 'SELECT key_hash FROM identity_rate_limit.windows WHERE key_hash<>%s AND expires_at<clock_timestamp() '
                                 'ORDER BY expires_at,key_hash LIMIT 100)',(_GLOBAL,))
                    total = global_row[1] if global_row[0] == window else 0
                    if total >= self.global_limit:
                        return False
                    # Per-key denials consume a global slot too; rotating or
                    # repeatedly exhausted keys cannot escape the global cap.
                    conn.execute('UPDATE identity_rate_limit.windows SET window_start=%s,hits=%s,expires_at=to_timestamp(%s) WHERE key_hash=%s',
                                 (window,total+1,(window+2)*60,_GLOBAL))
                    row = conn.execute('SELECT window_start,hits FROM identity_rate_limit.windows WHERE key_hash=%s FOR UPDATE',(fingerprint,)).fetchone()
                    count = row[1] if row and row[0] == window else 0
                    if count >= self.per_key:
                        return False
                    conn.execute('INSERT INTO identity_rate_limit.windows (key_hash,window_start,hits,expires_at) VALUES (%s,%s,%s,to_timestamp(%s)) '
                                 'ON CONFLICT (key_hash) DO UPDATE SET window_start=excluded.window_start,hits=excluded.hits,expires_at=excluded.expires_at',
                                 (fingerprint,window,count+1,(window+2)*60))
                    return True
        except Exception:
            raise RateLimitUnavailable('admission unavailable') from None

    def close(self):
        self._pool.close()


def apply_migration(admin_dsn: str, *, runtime_role: str):
    """Explicit additive migration and exact grants; never called by app startup."""
    source, checksum = _migration()
    with psycopg.connect(admin_dsn,connect_timeout=5) as conn:
        conn.execute('SET LOCAL statement_timeout=30000')
        conn.execute('SET LOCAL lock_timeout=10000')
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('identity-rate-limit-migrations-v1',0))")
        role = conn.execute('SELECT rolsuper,rolbypassrls,rolname=current_user FROM pg_roles WHERE rolname=%s',(runtime_role,)).fetchone()
        if role != (False,False,False):
            raise ValueError('a separate NOSUPERUSER NOBYPASSRLS runtime role is required')
        conn.execute('CREATE SCHEMA IF NOT EXISTS identity_rate_limit')
        conn.execute('CREATE TABLE IF NOT EXISTS identity_rate_limit.schema_migrations (version text PRIMARY KEY, checksum text NOT NULL)')
        row = conn.execute('SELECT checksum FROM identity_rate_limit.schema_migrations WHERE version=%s',(_VERSION,)).fetchone()
        if row and row != (checksum,):
            raise ValueError('admission migration checksum drift')
        if not row:
            conn.execute(source)
            conn.execute('INSERT INTO identity_rate_limit.windows VALUES (%s,0,0,to_timestamp(0))',(_GLOBAL,))
            conn.execute('INSERT INTO identity_rate_limit.schema_migrations VALUES (%s,%s)',(_VERSION,checksum))
        conn.execute('REVOKE ALL ON identity_rate_limit.schema_migrations FROM PUBLIC')
        role_id = sql.Identifier(runtime_role)
        for template in ('GRANT USAGE ON SCHEMA identity_rate_limit TO {}',
                         'GRANT SELECT,INSERT,UPDATE,DELETE ON identity_rate_limit.windows TO {}',
                         'GRANT SELECT ON identity_rate_limit.schema_migrations TO {}'):
            conn.execute(sql.SQL(template).format(role_id))


def main():
    parser = argparse.ArgumentParser(description='Explicit identity rate-limit migration; no platform tables')
    parser.add_argument('--runtime-role',required=True)
    args = parser.parse_args()
    dsn = os.environ.get('IDENTITY_RATE_LIMIT_MIGRATION_DATABASE_URL')
    if not dsn:
        parser.error('IDENTITY_RATE_LIMIT_MIGRATION_DATABASE_URL is required')
    try:
        apply_migration(dsn,runtime_role=args.runtime_role)
    except Exception:
        parser.exit(1,'rate-limit migration failed; inspect schema history and role grants\n')
    print('identity rate-limit migration current')


if __name__ == '__main__':
    main()
