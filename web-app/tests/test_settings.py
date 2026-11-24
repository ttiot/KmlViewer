"""Réglages administrables : registre borné, store base > env > défaut, version, diagnostic, CLI (T3, EF-6, CA-21*)."""

import json
import logging
import math
import os

import pytest

from app import audit, db as database
from app.audit import Actor
from app.ratelimit import Limiters, Limits
from app.services import ingest_service
from app.settings import REGISTRY, STORE_KEY, SettingsError, SettingsStore, effective
from app.settings.registry import Setting
from tests.conftest import line, make_gpx


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / 's.sqlite3')
    database.init_db(path)
    c = database.connect(path)
    yield c
    c.close()


@pytest.fixture
def env():
    return {}


@pytest.fixture
def store(conn, env):
    s = SettingsStore(env=env.get)
    s.refresh(conn, force=True)
    return s


def audit_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action LIKE 'setting.%' ORDER BY id")]


# --- Registre : bornes obligatoires (CA-21c, M7) -------------------------------------------------

def test_every_registry_entry_has_explicit_bounds():
    for key, s in REGISTRY.items():
        if s.type in ('int', 'float'):
            assert s.min is not None and s.max is not None and s.min <= s.default <= s.max, key
        elif s.type == 'str':
            assert s.max_length, key
        elif s.type == 'enum':
            assert s.choices, key
        elif s.type == 'list':
            assert s.max_items and s.max_length, key


@pytest.mark.parametrize('kwargs', [
    dict(type='int', default=1), dict(type='int', default=1, min=0), dict(type='float', default=1.0, max=2),
    dict(type='str', default='a'), dict(type='enum', default='a'), dict(type='list', default=[]),
    dict(type='list', default=[], max_items=3), dict(type='int', default=5, min=10, max=1),
    dict(type='int', default=500, min=0, max=10), dict(type='nope', default=1), dict(type='bool', default='x'),
])
def test_an_entry_without_bounds_or_with_a_bad_default_cannot_be_built(kwargs):
    with pytest.raises(ValueError):
        Setting('some.key', section='s', label='l', **kwargs)


def test_registry_keeps_security_settings_out_of_the_registry():
    """EF-6.6 : ni clés, ni mode d'authentification, ni destinations ne sont des réglages stockables."""
    forbidden = ('SECRET_KEY', 'SECRETS_KEY', 'AUTH_MODE', 'OIDC', 'PUBLIC_URL', 'WEBAUTHN', 'OUTBOUND', 'TRUSTED_HOSTS',
                 'TRUST_PROXY', 'SESSION', 'DATA_DIR', 'DB_PATH', 'LOG_LEVEL')
    for key in REGISTRY:
        if key == 'OIDC_AUTO_ACTIVATE':         # seule exception, listée par EF-6.5 (comptes et sécurité)
            continue
        assert not any(f.lower() in key.lower() for f in forbidden), key
        env_name = REGISTRY[key].env or ''
        assert key == 'OIDC_AUTO_ACTIVATE' or not any(f in env_name for f in forbidden), key


def test_limiter_thresholds_in_the_registry_match_the_spec_bounds():
    bounds = {k: (REGISTRY[k].min, REGISTRY[k].max) for k in REGISTRY if k.startswith('ratelimit.')}
    assert bounds == {
        'ratelimit.password_per_identifier': (3, 20), 'ratelimit.password_per_ip': (10, 100),
        'ratelimit.mfa_per_account': (3, 10), 'ratelimit.forgot_per_identifier': (1, 10),
        'ratelimit.forgot_per_ip': (10, 100), 'ratelimit.token_per_ip': (5, 30)}
    assert (REGISTRY['routing.rate_limit_per_user'].min, REGISTRY['routing.rate_limit_per_user'].max) == (1, 100)
    assert (REGISTRY['USER_QUOTA_MB'].min, REGISTRY['USER_QUOTA_MB'].max) == (0, 102_400)
    assert (REGISTRY['audit.retention_days'].min, REGISTRY['audit.retention_days'].max) == (30, 3650)


def test_limits_bounds_are_the_registry_bounds():
    """T6-2 : une seule source de bornes."""
    for name, (low, high) in Limits.BOUNDS.items():
        from app.ratelimit import SETTING_KEYS
        s = REGISTRY[SETTING_KEYS[name]]
        assert (low, high) == (s.min, s.max)
        assert getattr(Limits(), name) == s.default


# --- Validation stricte ----------------------------------------------------------------------

@pytest.mark.parametrize('value', [True, 1.5, '10', None, [1], -1, 102_401, 10 ** 400, math.nan])
def test_int_setting_refuses_wrong_types_and_out_of_bounds(value):
    with pytest.raises(ValueError):
        REGISTRY['USER_QUOTA_MB'].validate(value)


def test_int_setting_accepts_the_bounds():
    for v in (0, 102_400):
        assert REGISTRY['USER_QUOTA_MB'].validate(v) == v


def test_other_types():
    f = Setting('a.f', 'float', 1.0, 's', 'l', min=0, max=2)
    assert f.validate(1) == 1.0 and f.validate(2.0) == 2.0
    for bad in (True, math.inf, math.nan, '1', 2.1):
        with pytest.raises(ValueError):
            f.validate(bad)
    e = Setting('a.e', 'enum', 'x', 's', 'l', choices=('x', 'y'))
    assert e.validate('y') == 'y'
    with pytest.raises(ValueError):
        e.validate('z')
    t = Setting('a.t', 'str', '', 's', 'l', max_length=5)
    assert t.validate('abcde') == 'abcde'
    for bad in ('abcdef', 'a\nb', 5, None):
        with pytest.raises(ValueError):
            t.validate(bad)
    li = Setting('a.l', 'list', [], 's', 'l', max_items=2, max_length=3)
    assert li.validate(['a', 'b']) == ['a', 'b']
    for bad in (['a', 'b', 'c'], ['abcd'], 'a,b', [1]):
        with pytest.raises(ValueError):
            li.validate(bad)
    b = Setting('a.b', 'bool', False, 's', 'l')
    assert b.validate(True) is True
    for bad in (1, 'true', None):
        with pytest.raises(ValueError):
            b.validate(bad)


@pytest.mark.parametrize('raw,ok', [('512', 512), (' 7 ', 7), ('0', 0), (512, 512), ('+5', 5)])
def test_env_parsing_ok(raw, ok):
    assert REGISTRY['USER_QUOTA_MB'].parse_env(raw) == ok


@pytest.mark.parametrize('raw', ['abc', '1.5', '', '1e3', '-1', '999999', '9' * 50, True, 2.5])
def test_env_parsing_refuses(raw):
    with pytest.raises(ValueError):
        REGISTRY['USER_QUOTA_MB'].parse_env(raw)


# --- Valeur effective : base > env > défaut (EF-6.2) ------------------------------------------

def test_default_then_env_then_base(store, conn, env):
    assert store.get('USER_QUOTA_MB') == 2048 and store.source('USER_QUOTA_MB') == 'défaut'
    env['USER_QUOTA_MB'] = '512'
    assert store.get('USER_QUOTA_MB') == 512 and store.source('USER_QUOTA_MB') == 'env'
    store.set_many(conn, {'USER_QUOTA_MB': 100}, actor=Actor.user(1, 'alice'))
    assert store.get('USER_QUOTA_MB') == 100 and store.source('USER_QUOTA_MB') == 'base'
    assert store.reset(conn, 'USER_QUOTA_MB', actor=Actor.cli()) == 1
    assert store.get('USER_QUOTA_MB') == 512 and store.source('USER_QUOTA_MB') == 'env'
    env.clear()
    assert store.get('USER_QUOTA_MB') == 2048


def test_invalid_env_is_ignored_with_a_name_only_warning_and_never_blocks(store, env, caplog):
    """CA-21b : valeur d'env invalide ⇒ défaut, diagnostic, et la valeur n'est jamais journalisée."""
    env['USER_QUOTA_MB'] = 'beaucoup-trop-secret'
    with caplog.at_level(logging.WARNING, logger='app.settings.store'):
        assert store.get('USER_QUOTA_MB') == 2048
        store.get('USER_QUOTA_MB')
    assert 'beaucoup-trop-secret' not in caplog.text
    assert caplog.text.count('USER_QUOTA_MB') == 1                      # une fois, pas à chaque lecture
    diag = {d['key']: d for d in store.diagnostic()}['USER_QUOTA_MB']
    assert diag['env_invalid'] is True and diag['source'] == 'défaut'


def test_unknown_key_is_a_keyerror(store):
    with pytest.raises(KeyError):
        store.get('nope')


# --- Écriture : validée, tout ou rien, tracée (EF-6.3, CA-21) ----------------------------------

def test_write_is_validated_and_the_batch_is_all_or_nothing(store, conn):
    version = conn.execute("SELECT version FROM settings_meta").fetchone()[0]
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, {'USER_QUOTA_MB': 10, 'ratelimit.password_per_identifier': 21}, actor=Actor.user(1, 'alice'))
    assert exc.value.field == 'ratelimit.password_per_identifier'
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert conn.execute("SELECT version FROM settings_meta").fetchone()[0] == version
    assert audit_rows(conn) == []
    assert store.get('USER_QUOTA_MB') == 2048


@pytest.mark.parametrize('changes', [{'SECRET_KEY': 'x'}, {'AUTH_MODE': 'local'}, {'OUTBOUND_ALLOWED_HOSTS': 'a'},
                                     {'smtp.bogus': 'evil'}, {'': 1}])
def test_keys_outside_the_registry_are_refused(store, conn, changes):
    """CA-21 : aucun réglage de EF-6.6 n'est stockable."""
    with pytest.raises(SettingsError):
        store.set_many(conn, changes, actor=Actor.user(1, 'alice'))
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_every_write_bumps_the_version_once_and_is_audited(store, conn):
    v0 = conn.execute("SELECT version FROM settings_meta").fetchone()[0]
    store.set_many(conn, {'USER_QUOTA_MB': 10, 'audit.retention_days': 60}, actor=Actor.user(3, 'alice'), ip='1.2.3.4')
    assert conn.execute("SELECT version FROM settings_meta").fetchone()[0] == v0 + 1
    rows = audit_rows(conn)
    assert [r['target_id'] for r in rows] == ['USER_QUOTA_MB', 'audit.retention_days']
    assert rows[0]['actor_id'] == 3 and rows[0]['ip'] == '1.2.3.4'
    assert json.loads(rows[0]['details']) == {'key': 'USER_QUOTA_MB', 'old': 2048, 'new': 10}
    saved = dict(conn.execute("SELECT key, updated_by FROM settings").fetchall())
    assert saved == {'USER_QUOTA_MB': 'alice', 'audit.retention_days': 'alice'}


def test_write_and_audit_commit_or_roll_back_together(store, conn, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError

    monkeypatch.setattr(audit, 'record_setting_change', boom)
    with pytest.raises(RuntimeError):
        store.set_many(conn, {'USER_QUOTA_MB': 10}, actor=Actor.user(1, 'alice'))
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert conn.execute("SELECT version FROM settings_meta").fetchone()[0] == 1


def test_secret_typed_setting_never_leaks_its_value_into_audit_or_diagnostic(conn, env):
    """CA-24 / T2-1 : le type du registre, pas le nom, décide du masquage."""
    secret = Setting('svc.token', 'str', '', 'Test', 'Jeton', max_length=50, secret=True, env='SVC_TOKEN')
    s = SettingsStore(registry={'svc.token': secret}, env=env.get)
    env['SVC_TOKEN'] = 'valeur-env-secrete'
    s.refresh(conn, force=True)
    s.set_many(conn, {'svc.token': 'valeur-base-secrete'}, actor=Actor.user(1, 'alice'))
    s.set_many(conn, {'svc.token': 'nouvelle-valeur-secrete'}, actor=Actor.user(1, 'alice'))
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    for leaked in ('valeur-env-secrete', 'valeur-base-secrete', 'nouvelle-valeur-secrete'):
        assert leaked not in dump
    assert len(audit_rows(conn)) == 2
    diag = s.diagnostic()[0]
    assert diag['value'] == '[masqué]' and diag['env_ignored_value'] == '[masqué]' and diag['env_overridden'] == 'SVC_TOKEN'
    assert 'secrete' not in json.dumps(diag)


def test_reset_one_all_and_nothing_to_reset(store, conn):
    store.set_many(conn, {'USER_QUOTA_MB': 10, 'audit.retention_days': 60}, actor=Actor.user(1, 'alice'))
    v = conn.execute("SELECT version FROM settings_meta").fetchone()[0]
    assert store.reset(conn, 'USER_QUOTA_MB', actor=Actor.cli()) == 1
    assert store.reset(conn, 'USER_QUOTA_MB', actor=Actor.cli()) == 0       # déjà absent : pas de version ni d'audit en plus
    assert conn.execute("SELECT version FROM settings_meta").fetchone()[0] == v + 1
    assert store.reset(conn, None, actor=Actor.cli()) == 1
    assert store.get('audit.retention_days') == 90
    last = audit_rows(conn)[-1]
    assert (last['action'], last['actor_type'], last['actor_id'], last['target_id']) == ('setting.reset', 'cli', None, '*')
    with pytest.raises(KeyError):
        store.reset(conn, 'nope', actor=Actor.cli())


# --- Version et rechargement par requête (EF-6.4, CA-21d) -------------------------------------

def test_refresh_reloads_only_when_the_version_changed(store, conn, tmp_path):
    assert store.refresh(conn) is False
    other = database.connect(str(tmp_path / 's.sqlite3'))
    SettingsStore().set_many(other, {'USER_QUOTA_MB': 5}, actor=Actor.cli())     # autre « processus »
    other.close()
    assert store.get('USER_QUOTA_MB') == 2048           # pas encore relu
    assert store.refresh(conn) is True and store.get('USER_QUOTA_MB') == 5
    assert store.refresh(conn) is False


def test_a_value_made_invalid_by_tighter_bounds_or_corruption_is_ignored_not_fatal(store, conn, caplog):
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES "
                 "('USER_QUOTA_MB', '999999999', 'x', 'y'), ('audit.retention_days', 'pas du json', 'x', 'y'), "
                 "('vieux.reglage', '1', 'x', 'y')")
    conn.commit()
    with caplog.at_level(logging.WARNING, logger='app.settings.store'):
        store.refresh(conn, force=True)
    assert store.get('USER_QUOTA_MB') == 2048 and store.get('audit.retention_days') == 90
    assert '999999999' not in caplog.text and 'pas du json' not in caplog.text


def test_missing_meta_row_is_tolerated(conn, env):
    conn.execute("DELETE FROM settings_meta")
    conn.commit()
    s = SettingsStore(env=env.get)
    s.refresh(conn)
    s.set_many(conn, {'USER_QUOTA_MB': 3}, actor=Actor.user(1, 'a'))
    assert s.get('USER_QUOTA_MB') == 3 and conn.execute("SELECT version FROM settings_meta").fetchone()[0] == 1


def test_inside_a_callers_transaction_nothing_is_cached_until_commit(store, conn):
    with pytest.raises(RuntimeError), database.write_transaction(conn):
        store.set_many(conn, {'USER_QUOTA_MB': 7}, actor=Actor.user(1, 'a'))
        raise RuntimeError
    assert store.get('USER_QUOTA_MB') == 2048 and conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


# --- Diagnostic (EF-6.3c) ---------------------------------------------------------------------

def test_diagnostic_reports_source_and_the_overridden_env_variable(store, conn, env, caplog):
    env['USER_QUOTA_MB'] = '512'
    store.set_many(conn, {'USER_QUOTA_MB': 100}, actor=Actor.user(1, 'alice'))
    d = {x['key']: x for x in store.diagnostic()}['USER_QUOTA_MB']
    assert (d['source'], d['value'], d['env_overridden'], d['env_ignored_value']) == ('base', 100, 'USER_QUOTA_MB', 512)
    assert (d['updated_by'], d['min'], d['max']) == ('alice', 0, 102_400)
    with caplog.at_level(logging.WARNING, logger='app.settings.store'):
        assert store.log_overrides() == ['USER_QUOTA_MB']
    assert caplog.text.count('USER_QUOTA_MB') == 1 and '512' not in caplog.text and '100' not in caplog.text


def test_diagnostic_lists_every_setting_with_its_source(store):
    keys = [d['key'] for d in store.diagnostic()]
    assert keys == list(REGISTRY) and {d['source'] for d in store.diagnostic()} == {'défaut'}


# --- Application : pilote USER_QUOTA_MB, requête, CLI -----------------------------------------

def test_app_publishes_the_store_and_the_pilot_quota_follows_the_database(app, db):
    store = app.config[STORE_KEY]
    assert store is app.extensions['settings'] and effective(app.config, 'USER_QUOTA_MB') == 2048
    store.set_many(db, {'USER_QUOTA_MB': 1}, actor=Actor.user(1, 'alice'))
    assert effective(app.config, 'USER_QUOTA_MB') == 1


def test_effective_falls_back_to_the_cfg_value_then_the_default_without_a_store():
    assert effective({}, 'USER_QUOTA_MB') == 2048
    assert effective({'USER_QUOTA_MB': '7'}, 'USER_QUOTA_MB') == 7
    assert effective({'USER_QUOTA_MB': 'zzz'}, 'USER_QUOTA_MB') == 2048
    assert effective({'USER_QUOTA_MB': None}, 'USER_QUOTA_MB') == 2048


def test_config_override_acts_as_the_env_layer(app):
    app.config['USER_QUOTA_MB'] = 1
    assert effective(app.config, 'USER_QUOTA_MB') == 1
    assert app.extensions['settings'].source('USER_QUOTA_MB') == 'env'


def test_next_request_sees_a_change_made_by_another_process(app, client):
    other = database.connect(app.config['DB_PATH'])
    SettingsStore().set_many(other, {'USER_QUOTA_MB': 4}, actor=Actor.cli())
    other.close()
    assert effective(app.config, 'USER_QUOTA_MB') == 2048
    client.get('/api/health')
    assert effective(app.config, 'USER_QUOTA_MB') == 4


def test_a_broken_settings_table_does_not_fail_requests(app, client, db):
    db.execute("DROP TABLE settings_meta")
    db.commit()
    assert client.get('/api/health').status_code == 200


def test_settings_list_and_reset_commands(app, runner, db):
    app.extensions['settings'].set_many(db, {'USER_QUOTA_MB': 9}, actor=Actor.user(1, 'alice'))
    out = runner.invoke(args=['settings-list']).output
    assert "USER_QUOTA_MB = 9  (base)" in out and "audit.retention_days = 90  (défaut)" in out
    assert runner.invoke(args=['settings-reset']).exit_code != 0
    assert runner.invoke(args=['settings-reset', 'nope']).exit_code != 0
    assert runner.invoke(args=['settings-reset', 'USER_QUOTA_MB', '--all']).exit_code != 0
    result = runner.invoke(args=['settings-reset', 'USER_QUOTA_MB'])
    assert result.exit_code == 0 and '1 valeur' in result.output
    last = audit_rows(db)[-1]
    assert (last['action'], last['actor_type'], last['actor_id']) == ('setting.reset', 'cli', None)
    assert runner.invoke(args=['settings-reset', '--all']).exit_code == 0


def test_quota_setting_is_enforced_by_ingestion(app, db):
    """Pilote : la valeur du registre pilote le refus d'import (même code qu'avant, source différente)."""
    db.execute("INSERT INTO users (sub, username) VALUES ('u', 'u')")
    db.commit()
    app.extensions['settings'].set_many(db, {'USER_QUOTA_MB': 1}, actor=Actor.user(1, 'a'))
    quota_dir = os.path.join(app.config['DATA_DIR'], 'files', '1')
    os.makedirs(quota_dir, exist_ok=True)
    with open(os.path.join(quota_dir, 'filler.bin'), 'wb') as f:
        f.write(b'0' * (1024 * 1024 + 1))
    result = ingest_service.ingest_bytes(db, app.config, 1, make_gpx(line(20)).encode(), 'a.gpx', 'upload')
    assert result.status == 'error' and 'Quota' in result.message


# --- Limiteur à chaud (T6-2) -----------------------------------------------------------------

class Clock:
    t = 10_000.0

    def __call__(self):
        return self.t


def test_limiter_thresholds_follow_the_store_without_resetting_counters(store, conn):
    clock = Clock()
    limiters = Limiters(get=store.get, clock=clock, buckets=64, secret=b's' * 32)
    allowed = [limiters.password_identifier.hit('alice')[0] for _ in range(7)]
    assert allowed == [True] * 5 + [False] * 2                       # défaut : 5
    store.set_many(conn, {'ratelimit.password_per_identifier': 7}, actor=Actor.user(1, 'admin'))
    assert limiters.password_identifier.limit == 7
    assert [limiters.password_identifier.hit('alice')[0] for _ in range(3)] == [True, True, False]   # 2 de plus, compteur conservé
    store.set_many(conn, {'ratelimit.password_per_identifier': 3}, actor=Actor.user(1, 'admin'))
    assert limiters.password_identifier.hit('alice')[0] is False                                      # durci : s'applique aussitôt
    store.reset(conn, None, actor=Actor.cli())
    assert limiters.password_identifier.limit == 5


def test_limiter_threshold_cannot_be_pushed_out_of_bounds_through_the_store(store, conn):
    for bad in (0, 2, 21, 10 ** 6):
        with pytest.raises(SettingsError):
            store.set_many(conn, {'ratelimit.password_per_identifier': bad}, actor=Actor.user(1, 'admin'))
    assert Limiters(get=store.get).password_identifier.limit == 5


def test_fixed_limits_still_work_and_are_validated():
    assert Limiters(Limits(token_per_ip=7)).token_ip.limit == 7
    with pytest.raises(ValueError):
        Limits(token_per_ip=31)


# --- Relecture T3 : audit juste, reset symétrique, ordre des hooks -----------------------------

def test_audited_old_value_is_the_one_really_replaced_even_if_another_process_wrote_after_our_refresh(store, conn, tmp_path):
    """T3-1 : le cache dit 2048, un autre processus a écrit 10 entre-temps : l'audit doit dire 10."""
    other = database.connect(str(tmp_path / 's.sqlite3'))
    SettingsStore().set_many(other, {'USER_QUOTA_MB': 10}, actor=Actor.cli())
    other.close()
    assert store.get('USER_QUOTA_MB') == 2048              # cache périmé : pas de refresh depuis
    store.set_many(conn, {'USER_QUOTA_MB': 20}, actor=Actor.user(1, 'alice'))
    last = audit_rows(conn)[-1]
    assert json.loads(last['details']) == {'key': 'USER_QUOTA_MB', 'old': 10, 'new': 20}


def test_old_value_falls_back_to_env_then_default_when_nothing_is_in_the_database(store, conn, env):
    env['USER_QUOTA_MB'] = '512'
    store.set_many(conn, {'USER_QUOTA_MB': 20}, actor=Actor.user(1, 'alice'))
    assert json.loads(audit_rows(conn)[-1]['details'])['old'] == 512
    store.reset(conn, None, actor=Actor.cli())
    env.clear()
    store.set_many(conn, {'audit.retention_days': 60}, actor=Actor.user(1, 'alice'))
    assert json.loads(audit_rows(conn)[-1]['details'])['old'] == 90


def test_reset_traces_the_replaced_values_masking_secrets(conn, env):
    secret = Setting('svc.token', 'str', '', 'Test', 'Jeton', max_length=50, secret=True)
    s = SettingsStore(registry={**REGISTRY, 'svc.token': secret}, env=env.get)
    s.refresh(conn, force=True)
    s.set_many(conn, {'USER_QUOTA_MB': 10, 'svc.token': 'valeur-secrete'}, actor=Actor.user(1, 'alice'))
    s.reset(conn, None, actor=Actor.cli())
    last = audit_rows(conn)[-1]
    details = json.loads(last['details'])
    assert details['keys'] == ['USER_QUOTA_MB', 'svc.token'] and details['old']['USER_QUOTA_MB'] == 10
    assert details['old']['svc.token'] == '[masqué]' and 'valeur-secrete' not in last['details']


def test_settings_are_refreshed_after_the_host_and_origin_checks(app):
    """T3-n1 : une requête rejetée (Host non approuvé) ne lit pas la base."""
    names = [f.__name__ for f in app.before_request_funcs[None]]
    assert names.index('_reject_untrusted_host') < names.index('_check_same_origin') < names.index('refresh_per_request')
