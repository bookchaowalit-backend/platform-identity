"""Staged shared admission uses only explicitly supplied disposable PostgreSQL."""
import os
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from service.test_app import (
    ISSUER,
    PLATFORM_ISSUER,
    FakeDatabase,
    SigningKey,
    StaticKey,
    TokenVerifier,
    key_pair,
)
from starlette.requests import Request

from platform_identity.app import (
    ID_TOKEN_TYPE,
    TOKEN_EXCHANGE,
    client_address,
    create_app,
)
from platform_identity.config import Config, ConfigError


def config_env(**overrides):
    return {'IDENTITY_DATABASE_URL':'postgresql://primary.test/db', 'IDENTITY_SIGNING_KEY_PEM':'configured',
            'IDENTITY_ISSUER':ISSUER, 'IDENTITY_PLATFORM_ISSUER':PLATFORM_ISSUER,
            'IDENTITY_PLATFORM_PUBLIC_KEY_PEM':'configured', **overrides}


def test_configuration_preserves_local_default_and_shadow_gate():
    config = Config.from_env(config_env())
    assert config.rate_limit_mode == 'local'
    assert config.decide_mode == 'shadow'
    assert config.trusted_proxies == ()
    assert config.rate_limit_database_url is None
    with pytest.raises(ConfigError):
        Config.from_env(config_env(IDENTITY_ISSUER=PLATFORM_ISSUER))
    with pytest.raises(ConfigError):
        Config.from_env(config_env(IDENTITY_RATE_LIMIT_MODE='postgres'))
    shared = Config.from_env(config_env(IDENTITY_RATE_LIMIT_MODE='postgres',IDENTITY_RATE_LIMIT_DATABASE_URL='postgresql://staged.test/db',IDENTITY_TRUSTED_PROXIES='10.0.0.0/8,192.0.2.3/32'))
    assert shared.rate_limit_mode == 'postgres'
    assert shared.trusted_proxies == ('10.0.0.0/8','192.0.2.3/32')


@pytest.mark.parametrize('overrides', [
    {'IDENTITY_RATE_LIMIT_MODE':'other'}, {'IDENTITY_DB_POOL_MAX':'0'},
    {'IDENTITY_DB_POOL_MAX':'129'}, {'IDENTITY_DB_TIMEOUT_SECONDS':'nan'},
    {'IDENTITY_RATE_LIMIT_POOL_MAX':'0'}, {'IDENTITY_TRUSTED_PROXIES':'*'},
    {'IDENTITY_RATE_LIMIT_PER_KEY':'0'}, {'IDENTITY_RATE_LIMIT_GLOBAL':'0'},
])
def test_configuration_rejects_unbounded_or_unsafe_values(overrides):
    with pytest.raises(ConfigError):
        Config.from_env(config_env(**overrides))


def request_from(peer, forwarded):
    return Request({'type':'http','client':(peer,1234),'headers':[(b'x-forwarded-for',forwarded.encode())]})


def test_explicit_proxy_trust_ignores_direct_spoofed_forwarding():
    assert client_address(request_from('198.51.100.4','203.0.113.9'), trusted_proxies=()) == '198.51.100.4'
    assert client_address(request_from('198.51.100.4','203.0.113.9'), trusted_proxies=('10.0.0.0/8',)) == '198.51.100.4'
    assert client_address(request_from('10.0.0.2','1.1.1.1, 198.51.100.4, 10.0.0.1'), trusted_proxies=('10.0.0.0/8',)) == '198.51.100.4'
    assert client_address(request_from('10.0.0.2','malformed'), trusted_proxies=('10.0.0.0/8',)) == '10.0.0.2'
    # Legacy callers retain their existing local behavior unless trust is explicit.
    assert client_address(request_from('198.51.100.4','203.0.113.9')) == '203.0.113.9'


@pytest.fixture
def temporary():
    dsn = os.environ.get('PLATFORM_TEST_DSN')
    if not dsn:
        pytest.skip('explicit disposable PostgreSQL DSN not supplied')
    from platform_backend_core.testing import temporary_database
    with temporary_database(dsn,prefix='identity_test_') as fixture:
        yield fixture


def migrated(temporary):
    from platform_identity.rate_limit import apply_migration
    apply_migration(temporary.admin_dsn, runtime_role=temporary.runtime_role)


def process_admit(dsn, keys, per_key, global_limit, start):
    from platform_identity.rate_limit import PostgresRateLimiter
    limiter = PostgresRateLimiter(dsn, per_key=per_key, global_limit=global_limit, pool_max=2, timeout=2)
    try:
        while time.time() < start:
            time.sleep(.001)
        return sum(limiter.allow(key) for key in keys)
    finally:
        limiter.close()


def avoid_window_boundary(dsn):
    # Do not accidentally measure two legitimate database-clock windows.
    with psycopg.connect(dsn) as conn:
        remaining = conn.execute("SELECT 60 - mod(extract(epoch FROM clock_timestamp())::numeric,60)").fetchone()[0]
    if remaining < 8:
        time.sleep(float(remaining)+.1)


def test_real_two_process_global_and_per_key_caps(temporary):
    migrated(temporary)
    avoid_window_boundary(temporary.admin_dsn)
    start = time.time()+.5
    with ProcessPoolExecutor(max_workers=2,mp_context=get_context('spawn')) as executor:
        futures = [executor.submit(process_admit,temporary.runtime_dsn,['203.0.113.1']*12,7,15,start) for _ in range(2)]
        assert sum(f.result(timeout=15) for f in futures) == 7
    from platform_identity.rate_limit import PostgresRateLimiter
    limiter = PostgresRateLimiter(temporary.runtime_dsn, per_key=7, global_limit=15)
    try:
        # Per-key denials consumed the remaining global slots; new keys cannot bypass it.
        assert not limiter.allow('198.51.100.2')
        assert limiter.ready()
    finally:
        limiter.close()
    with psycopg.connect(temporary.admin_dsn) as conn:
        rows = conn.execute('SELECT key_hash,hits FROM identity_rate_limit.windows').fetchall()
        assert all('203.0.113.1' not in key and '198.51.100.2' not in key for key,_ in rows)
        assert sorted(count for _,count in rows) == [7,15]


def test_global_cap_shared_across_distinct_process_keys(temporary):
    migrated(temporary)
    avoid_window_boundary(temporary.admin_dsn)
    start = time.time()+.5
    with ProcessPoolExecutor(max_workers=2,mp_context=get_context('spawn')) as executor:
        futures = [executor.submit(process_admit,temporary.runtime_dsn,[f'client-{n}-{i}' for i in range(12)],10,9,start) for n in range(2)]
        assert sum(f.result(timeout=15) for f in futures) == 9


def test_schema_grants_outage_and_pool_shutdown_fail_closed(temporary):
    from platform_identity.rate_limit import PostgresRateLimiter, RateLimitUnavailable
    limiter = PostgresRateLimiter(temporary.runtime_dsn,timeout=.2)
    try:
        assert not limiter.ready()
        with pytest.raises(RateLimitUnavailable):
            limiter.allow('address')
        migrated(temporary)
        assert limiter.ready()
        with psycopg.connect(temporary.admin_dsn) as conn:
            conn.execute(sql.SQL('REVOKE UPDATE ON identity_rate_limit.windows FROM {}').format(sql.Identifier(temporary.runtime_role)))
        assert not limiter.ready()
        with pytest.raises(RateLimitUnavailable):
            limiter.allow('address')
    finally:
        limiter.close()
    assert not limiter.ready()
    with pytest.raises(RateLimitUnavailable):
        limiter.allow('address')
    outage = PostgresRateLimiter('dbname=postgres host=127.0.0.1 port=1',timeout=.2)
    try:
        assert not outage.ready()
        with pytest.raises(RateLimitUnavailable):
            outage.allow('address')
    finally:
        outage.close()


@pytest.fixture(scope='module')
def app_keys():
    private, public = key_pair()
    return SigningKey(private),TokenVerifier(StaticKey(public),PLATFORM_ISSUER)


def make_app(limiter, app_keys):
    signing, verifier = app_keys
    return create_app(connect=FakeDatabase().connect, platform_verifier=verifier,signing_key=signing,
                      issuer=ISSUER,google=None,token_audiences=frozenset(),rate_limiter=limiter,
                      trusted_proxies=())


def token_request(client, forwarded):
    return client.post('/v1/token',data={'grant_type':TOKEN_EXCHANGE,'subject_token':'not-used',
                       'subject_token_type':ID_TOKEN_TYPE,'audience':'unavailable','workspace':str(uuid4())},
                       headers={'X-Forwarded-For':forwarded})


def test_two_app_replicas_share_limit_and_ignore_spoofing(temporary,app_keys):
    migrated(temporary)
    avoid_window_boundary(temporary.admin_dsn)
    from platform_identity.rate_limit import PostgresRateLimiter
    limiters = [PostgresRateLimiter(temporary.runtime_dsn,per_key=5,global_limit=30) for _ in range(2)]
    with TestClient(make_app(limiters[0],app_keys)) as first, TestClient(make_app(limiters[1],app_keys)) as second:
        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(lambda i:token_request([first,second][i%2],f'203.0.113.{i}').status_code,range(12)))
        assert statuses.count(400) == 5
        assert statuses.count(429) == 7
        assert first.get('/health').json()['decide_mode'] == 'shadow'


def test_api_dependency_failure_is_safe_503_and_not_ready(temporary,app_keys):
    from platform_identity.rate_limit import PostgresRateLimiter
    limiter = PostgresRateLimiter(temporary.runtime_dsn,timeout=.2)
    with TestClient(make_app(limiter,app_keys)) as client:
        response = token_request(client,'203.0.113.1')
        assert response.status_code == 503
        assert response.json()['error'] == 'temporarily_unavailable'
        assert 'identity_rate_limit' not in response.text
        assert client.get('/readyz').status_code == 503
        assert client.get('/health').status_code == 200


def test_database_clock_rollover_and_bounded_expired_cleanup(temporary):
    migrated(temporary)
    from platform_identity.rate_limit import PostgresRateLimiter
    limiter = PostgresRateLimiter(temporary.runtime_dsn,per_key=1,global_limit=10)
    try:
        assert limiter.allow('203.0.113.3')
        assert not limiter.allow('203.0.113.3')
        with psycopg.connect(temporary.admin_dsn) as conn:
            conn.execute('UPDATE identity_rate_limit.windows SET window_start=window_start-1')
            conn.execute("INSERT INTO identity_rate_limit.windows SELECT md5(i::text)||md5(i::text), 0, 1, now()-interval '1 day' FROM generate_series(1,250) i")
        assert limiter.allow('203.0.113.3')
        with psycopg.connect(temporary.admin_dsn) as conn:
            assert conn.execute("SELECT count(*) FROM identity_rate_limit.windows WHERE expires_at < clock_timestamp()").fetchone() == (150,)
    finally:
        limiter.close()


def test_privileged_limiter_role_is_rejected(temporary):
    migrated(temporary)
    from platform_identity.rate_limit import PostgresRateLimiter, RateLimitUnavailable
    limiter = PostgresRateLimiter(temporary.admin_dsn,timeout=.2)
    try:
        assert not limiter.ready()
        with pytest.raises(RateLimitUnavailable):
            limiter.allow('address')
    finally:
        limiter.close()


def test_bounded_primary_pool_readiness_exhaustion_and_lifespan(temporary,app_keys):
    from platform_identity.rate_limit import bounded_pool
    pool = bounded_pool(temporary.runtime_dsn,max_size=1,timeout=.1)
    signing, verifier = app_keys
    app = create_app(connection_pool=pool,platform_verifier=verifier,signing_key=signing,
                     issuer=ISSUER,google=None,token_audiences=frozenset())
    with TestClient(app) as client:
        assert client.get('/readyz').status_code == 200
        with pool.connection():
            assert client.get('/readyz').status_code == 503
            assert client.get('/health').status_code == 200
        assert client.get('/readyz').status_code == 200
    assert pool.closed


def test_migration_checksum_and_exact_runtime_grants(temporary):
    migrated(temporary)
    from platform_identity.rate_limit import (
        PostgresRateLimiter,
        RateLimitUnavailable,
        apply_migration,
    )
    # Reapplication is idempotent and uses only the identity admission schema.
    migrated(temporary)
    with psycopg.connect(temporary.runtime_dsn) as conn:
        assert conn.execute("SELECT to_regnamespace('platform')").fetchone() == (None,)
        assert conn.execute("SELECT has_schema_privilege(current_user,'identity_rate_limit','CREATE'),has_table_privilege(current_user,'identity_rate_limit.schema_migrations','UPDATE')").fetchone() == (False,False)
    limiter = PostgresRateLimiter(temporary.runtime_dsn)
    try:
        assert limiter.ready()
        with psycopg.connect(temporary.admin_dsn) as conn:
            conn.execute("UPDATE identity_rate_limit.schema_migrations SET checksum='drift'")
        assert not limiter.ready()
        with pytest.raises(RateLimitUnavailable):
            limiter.allow('address')
        with pytest.raises(ValueError,match='checksum drift'):
            apply_migration(temporary.admin_dsn,runtime_role=temporary.runtime_role)
    finally:
        limiter.close()


def test_documented_cli_applies_migration_and_reports_failures_safely(temporary):
    import subprocess
    import sys
    from pathlib import Path

    from platform_identity.rate_limit import PostgresRateLimiter
    command = [sys.executable,'-m','platform_identity.rate_limit','--runtime-role',temporary.runtime_role]
    environment = {'IDENTITY_RATE_LIMIT_MIGRATION_DATABASE_URL':temporary.admin_dsn,
                   'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    result = subprocess.run(command,env=environment,capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'identity rate-limit migration current'
    limiter = PostgresRateLimiter(temporary.runtime_dsn)
    try:
        assert limiter.ready()
    finally:
        limiter.close()
    result = subprocess.run(command[:-1]+['missing_runtime_role'],env=environment,capture_output=True,text=True,timeout=10)
    assert result.returncode == 1
    assert 'migration failed' in result.stderr
    assert temporary.admin_dsn not in result.stderr


def test_privileged_primary_pool_is_not_ready(temporary,app_keys):
    from platform_identity.rate_limit import bounded_pool
    pool = bounded_pool(temporary.admin_dsn,max_size=1,timeout=.2)
    signing, verifier = app_keys
    app = create_app(connection_pool=pool,platform_verifier=verifier,signing_key=signing,
                     issuer=ISSUER,google=None,token_audiences=frozenset())
    with TestClient(app) as client:
        assert client.get('/readyz').status_code == 503
        assert client.get('/health').status_code == 200
    assert pool.closed


def test_shared_scope_mapping_defaults_empty_and_validates_configuration():
    assert dict(Config.from_env(config_env()).shared_scope_permissions) == {}
    config = Config.from_env(config_env(IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON='{"jobs:write":"tasks.task:write"}'))
    assert dict(config.shared_scope_permissions) == {'jobs:write':'tasks.task:write'}
    for value in ['[]','{"jobs:*":"tasks.task:write"}','{"jobs:write":"unregistered-format"}',
                  '{"jobs:write":"tasks.task:read","jobs:write":"tasks.task:write"}',
                  '{"tasks.task:read":"tasks.task:write"}']:
        with pytest.raises(ConfigError):
            Config.from_env(config_env(IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON=value))


def shared_scope_app(app_keys, *, decision='allow', member=True):
    signing, verifier = app_keys
    db = FakeDatabase()
    db.decision = decision
    workspace, actor = uuid4(),uuid4()
    if member:
        db.members[(str(workspace),'google:synthetic-user')] = actor
    class GoogleFixture:
        def external_subject(self, token):
            return 'google:synthetic-user'
    app = create_app(connect=db.connect,platform_verifier=verifier,signing_key=signing,
                     issuer=ISSUER,google=GoogleFixture(),token_audiences=frozenset({'platform-api'}),
                     shared_scope_permissions={'jobs:write':'tasks.task:write'})
    data = {'grant_type':TOKEN_EXCHANGE,'subject_token':'synthetic-fixture',
            'subject_token_type':ID_TOKEN_TYPE,'audience':'platform-api','workspace':str(workspace),'scope':'jobs:write'}
    return app,db,data,workspace,actor


def test_shared_scope_requires_rbac_and_core_accepts_issued_token(app_keys):
    from fastapi import Depends, FastAPI
    from platform_backend_core import TokenVerifier as CoreVerifier
    from platform_backend_core import require_scope
    app,db,data,workspace,actor = shared_scope_app(app_keys)
    with TestClient(app) as client:
        response = client.post('/v1/token',data=data)
    assert response.status_code == 200, response.text
    assert db.authorize_args == [(actor,workspace,'tasks.task:write','shared-infra','jobs:write',workspace,None,None,None,'api',False)]
    assert db.commits == 0
    # Use the server's trusted public JWK as a cryptography verification key.
    import jwt
    verifier = CoreVerifier(issuer=ISSUER,audience='platform-api',key_source=lambda header:jwt.PyJWK.from_dict(app_keys[0].jwk).key)
    authorization = 'Bearer '+response.json()['access_token']
    principal = verifier.verify(authorization)
    assert principal.workspace == workspace
    assert principal.scopes == frozenset({'jobs:write'})
    consumer = FastAPI()
    consumer.state.verifier = verifier
    @consumer.get('/jobs')
    def jobs(principal=Depends(require_scope('jobs:write'))):
        return {'workspace':str(principal.workspace)}
    with TestClient(consumer) as client:
        assert client.get('/jobs',headers={'Authorization':authorization}).json() == {'workspace':str(workspace)}


@pytest.mark.parametrize('decision', ['deny','require_approval'])
def test_shared_scope_denied_without_token_on_rbac_refusal(app_keys,decision):
    app,db,data,_,_ = shared_scope_app(app_keys,decision=decision)
    with TestClient(app) as client:
        response = client.post('/v1/token',data=data)
    assert response.status_code == 403
    assert response.json()['error'] == 'invalid_scope'
    assert 'access_token' not in response.json()
    assert db.commits == 0


def test_shared_scope_unknown_or_wrong_workspace_cannot_mint(app_keys):
    app,db,data,_,_ = shared_scope_app(app_keys)
    with TestClient(app) as client:
        response = client.post('/v1/token',data={**data,'scope':'unknown:write'})
        assert response.status_code == 400
        assert response.json()['error'] == 'invalid_scope'
        assert not db.authorize_args
        response = client.post('/v1/token',data={**data,'workspace':str(uuid4())})
        assert response.status_code == 400
        assert response.json()['error'] == 'invalid_grant'
        assert not db.authorize_args


@pytest.mark.parametrize('length',[128,129])
def test_shared_scope_total_length_boundary_in_environment_mapping(length):
    import json
    scope = 'a'*64 + ':' + 'b'*(length-65)
    mapping = {scope:'tasks.task:write'}
    environment = config_env(IDENTITY_SHARED_SCOPE_PERMISSIONS_JSON=json.dumps(mapping))
    if length == 128:
        assert dict(Config.from_env(environment).shared_scope_permissions) == mapping
    else:
        with pytest.raises(ConfigError):
            Config.from_env(environment)


@pytest.mark.parametrize('length',[128,129])
def test_shared_scope_total_length_boundary_in_programmatic_app_mapping(length,app_keys):
    signing, verifier = app_keys
    scope = 'a'*64 + ':' + 'b'*(length-65)
    kwargs = dict(connect=FakeDatabase().connect,platform_verifier=verifier,signing_key=signing,
                  issuer=ISSUER,google=None,token_audiences=frozenset(),
                  shared_scope_permissions={scope:'tasks.task:write'})
    if length == 128:
        assert create_app(**kwargs)
        from platform_backend_core import Principal, require_scope
        assert Principal(uuid4(),uuid4(),frozenset({scope})).scopes == frozenset({scope})
        assert callable(require_scope(scope))
    else:
        with pytest.raises(ValueError):
            create_app(**kwargs)
