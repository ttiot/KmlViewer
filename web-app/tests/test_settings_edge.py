"""
Cas limites des réglages administrables (T3, EF-6.1 à 6.4, CA-21 à CA-21d, M7, M10, ADR-0008).

Complète test_settings.py : bornes basse et haute de TOUTES les entrées du registre (test
paramétré sur REGISTRY, donc valable pour les entrées ajoutées plus tard), env invalide, lot tout
ou rien, versions et concurrence entre processus, USER_QUOTA_MB = 0, seuils du limiteur à chaud,
aucune valeur secrète dans l'audit, le diagnostic, les journaux ni la CLI.
"""

import json
import logging
import math
import os
import pathlib
import subprocess
import sys
import threading

import pytest

from app import audit, create_app
from app import db as database
from app.config import TestingConfig
from app.ratelimit import SETTING_KEYS, Limiters
from app.services import ingest_service
from app.settings import REGISTRY, STORE_KEY, SettingsError, SettingsStore, effective
from app.settings.registry import Setting
from tests.conftest import line, make_gpx

NUMERIC = [k for k, s in REGISTRY.items() if s.type in ('int', 'float')]
WITH_ENV = [k for k, s in REGISTRY.items() if s.env]
HUGE = 10 ** 400
CLI = audit.Actor.cli()


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / 's.sqlite3')
    database.init_db(p)
    return p


@pytest.fixture
def conn(path):
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


def count(conn, table='settings'):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def version(conn):
    return conn.execute("SELECT version FROM settings_meta").fetchone()[0]


# --- EF-6.1 / CA-21c : bornes de TOUTES les entrées ---------------------------------------------

def test_the_registry_is_not_empty_and_covers_the_spec_perimeter():
    assert len(REGISTRY) >= 9 and 'USER_QUOTA_MB' in REGISTRY and 'audit.retention_days' in REGISTRY


@pytest.mark.parametrize('key', list(REGISTRY))
def test_every_entry_is_well_formed(key):
    s = REGISTRY[key]
    assert s.key == key and s.label and s.section
    assert s.validate(s.default) == s.default or (s.type == 'float' and s.validate(s.default) == float(s.default))
    assert s.requires_restart is False, "EF-6.4 : aucun réglage v1 n'exige de redémarrage"
    if s.env:
        assert sum(1 for o in REGISTRY.values() if o.env == s.env) == 1, "variable d'env partagée entre deux réglages"


@pytest.mark.parametrize('key', NUMERIC)
def test_numeric_bounds_are_inclusive_and_exact(key):
    s = REGISTRY[key]
    for ok in (s.min, s.max, s.default):
        assert s.validate(ok) == ok
    step = 1 if s.type == 'int' else 1e-6
    for bad in (s.min - step, s.max + step, s.min - 10 ** 6, s.max + 10 ** 6, -s.max - 1):
        with pytest.raises(ValueError):
            s.validate(bad)


@pytest.mark.parametrize('key', NUMERIC)
@pytest.mark.parametrize('bad', [True, False, '1', None, [1], {'a': 1}, math.nan, math.inf, -math.inf, 1.5, b'1'],
                         ids=repr)
def test_numeric_entries_reject_wrong_types_and_non_finite(key, bad):
    s = REGISTRY[key]
    if s.type == 'float' and isinstance(bad, float) and math.isfinite(bad):
        pytest.skip("un float fini est un nombre valide pour un réglage float")
    with pytest.raises(ValueError):
        s.validate(bad)


@pytest.mark.parametrize('key', NUMERIC)
def test_numeric_entries_reject_oversized_integers(key):
    """Un entier JSON démesuré est refusé par une ValueError (400), jamais une exception brute (500)."""
    s = REGISTRY[key]
    for bad in (HUGE, -HUGE):
        with pytest.raises(ValueError):
            s.validate(bad)


@pytest.mark.parametrize('key', NUMERIC)
def test_writing_the_bounds_is_accepted_and_one_beyond_is_a_400_naming_the_field(store, conn, key):
    s = REGISTRY[key]
    for good in (s.min, s.max):
        store.set_many(conn, {key: good}, actor=audit.Actor.user(1, 'a'))
        assert store.get(key) == good and store.source(key) == 'base'
    v, rows = version(conn), count(conn, 'audit_log')
    step = 1 if s.type == 'int' else 1e-6
    for bad in (s.min - step, s.max + step, HUGE if s.type == 'int' else math.inf):
        with pytest.raises(SettingsError) as exc:
            store.set_many(conn, {key: bad}, actor=audit.Actor.user(1, 'a'))
        assert exc.value.field == key
    assert store.get(key) == s.max and version(conn) == v and count(conn, 'audit_log') == rows


@pytest.mark.parametrize('key', [k for k, s in REGISTRY.items() if s.type == 'str'])
def test_string_entries_have_a_length_bound(key):
    s = REGISTRY[key]
    s.validate('x' * s.max_length)
    with pytest.raises(ValueError):
        s.validate('x' * (s.max_length + 1))


@pytest.mark.parametrize('key', [k for k, s in REGISTRY.items() if s.type == 'enum'])
def test_enum_entries_refuse_anything_outside_their_choices(key):
    s = REGISTRY[key]
    for bad in ('', 'ADMINS', None, 0, ['admins'], s.choices[0] + ' '):
        with pytest.raises(ValueError):
            s.validate(bad)


@pytest.mark.parametrize('key', [k for k, s in REGISTRY.items() if s.type == 'list'])
def test_list_entries_bound_their_size_and_each_element(key):
    s = REGISTRY[key]
    s.validate(['x'] * s.max_items)
    for bad in (['x'] * (s.max_items + 1), ['x' * (s.max_length + 1)], 'a,b', [1], None):
        with pytest.raises(ValueError):
            s.validate(bad)


@pytest.mark.parametrize('key', NUMERIC)
def test_a_value_in_base_outside_the_bounds_is_ignored_and_reported_by_name_only(store, conn, caplog, key):
    s = REGISTRY[key]
    for raw in (json.dumps(s.max + 1), json.dumps(s.min - 1), '"texte"', 'null', 'true', '[1]', '{', ''):
        conn.execute("DELETE FROM settings")
        conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 'x', 'y')", (key, raw))
        conn.execute("UPDATE settings_meta SET version = version + 1")
        conn.commit()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger='app.settings.store'):
            store._warned.clear()
            store.refresh(conn, force=True)
        assert store.get(key) == s.default and store.source(key) == 'défaut', raw
        assert key in caplog.text and (len(raw) < 6 or raw not in caplog.text)


@pytest.mark.parametrize('key', WITH_ENV)
def test_invalid_env_is_ignored_signalled_by_name_and_never_fatal(store, env, caplog, key):
    s = REGISTRY[key]
    bads = ['abc', '1.5', '-1', str(s.max + 1), str(s.min - 1), '9' * 40, '1e3', '0x10', 'NaN', ' ', 'oui']
    for raw in bads:
        env[s.env] = raw
        store._warned.clear()
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger='app.settings.store'):
            value = store.get(key)
        if raw.strip() == '':
            assert value == s.default
            continue
        assert value == s.default and store.source(key) == 'défaut', raw
        assert s.env in caplog.text and raw not in caplog.text.replace(s.env, ''), raw
        diag = {d['key']: d for d in store.diagnostic()}[key]
        assert diag['env_invalid'] is True


@pytest.mark.parametrize('key', WITH_ENV)
def test_valid_env_at_both_bounds_is_taken(store, env, key):
    s = REGISTRY[key]
    for good in (s.min, s.max):
        env[s.env] = f' {int(good)} '
        assert store.get(key) == good and store.source(key) == 'env'


def test_app_starts_with_an_invalid_env_value_for_every_registry_entry(tmp_path, caplog):
    """CA-21b : démarrage OK, défaut appliqué, signalé."""
    overrides = {'DATA_DIR': str(tmp_path / 'data'), **{s.env: 'valeur-absurde' for s in REGISTRY.values() if s.env}}
    with caplog.at_level(logging.WARNING):
        app = create_app(TestingConfig, overrides=overrides)
    with app.app_context():
        for key in WITH_ENV:
            assert effective(app.config, key) == REGISTRY[key].default
    assert 'valeur-absurde' not in caplog.text


def test_the_config_class_no_longer_crashes_at_import_on_a_bad_quota():
    """Avant T3, `int(os.environ['USER_QUOTA_MB'])` faisait échouer l'import de la configuration."""
    root = str(pathlib.Path(__file__).resolve().parents[1])
    for raw, expected in (('abc', 'abc'), ('0', '0'), ('', 'None'), ('9999999999', '9999999999')):
        out = subprocess.run([sys.executable, '-c', "from app.config import Config; print(repr(Config.USER_QUOTA_MB))"],
                             capture_output=True, text=True, cwd=root,
                             env={**os.environ, 'PYTHONPATH': root, 'USER_QUOTA_MB': raw})
        assert out.returncode == 0, out.stderr[-300:]
        assert out.stdout.strip() == (expected if expected == 'None' else repr(expected))


# --- Lot tout ou rien (EF-6.3) ---------------------------------------------------------------

@pytest.mark.parametrize('position', [0, 1, 2])
def test_a_bad_value_anywhere_in_a_batch_writes_nothing(store, conn, position):
    keys = ['USER_QUOTA_MB', 'audit.retention_days', 'ratelimit.token_per_ip']
    good = {'USER_QUOTA_MB': 10, 'audit.retention_days': 60, 'ratelimit.token_per_ip': 7}
    batch = {k: (10 ** 9 if i == position else good[k]) for i, k in enumerate(keys)}
    v = version(conn)
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, batch, actor=audit.Actor.user(1, 'a'))
    assert exc.value.field == keys[position]
    assert count(conn) == 0 and version(conn) == v and count(conn, 'audit_log') == 0
    assert all(store.source(k) == 'défaut' for k in keys)


def test_an_unknown_key_in_a_batch_writes_nothing(store, conn):
    with pytest.raises(SettingsError):
        store.set_many(conn, {'USER_QUOTA_MB': 10, 'inconnu.cle': 1}, actor=audit.Actor.user(1, 'a'))
    assert count(conn) == 0


def test_a_failure_halfway_through_the_writes_rolls_back_the_earlier_ones(store, conn, monkeypatch):
    real, calls = audit.record_setting_change, []

    def second_fails(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disque plein")
        return real(*a, **k)

    monkeypatch.setattr(audit, 'record_setting_change', second_fails)
    v = version(conn)
    with pytest.raises(RuntimeError):
        store.set_many(conn, {'USER_QUOTA_MB': 10, 'audit.retention_days': 60}, actor=audit.Actor.user(1, 'a'))
    assert count(conn) == 0 and count(conn, 'audit_log') == 0 and version(conn) == v and not conn.in_transaction
    assert store.get('USER_QUOTA_MB') == 2048


def test_an_empty_batch_is_a_no_op_without_version_or_audit(store, conn):
    v = version(conn)
    store.set_many(conn, {}, actor=audit.Actor.user(1, 'a'))
    assert version(conn) == v and count(conn, 'audit_log') == 0


def test_a_refused_actor_rolls_the_whole_batch_back(store, conn):
    with pytest.raises((TypeError, AttributeError)):
        store.set_many(conn, {'USER_QUOTA_MB': 10}, actor='alice')          # pas un Actor : erreur de programmation, tout est annulé
    assert count(conn) == 0 and not conn.in_transaction


def test_the_error_names_the_field_and_never_echoes_the_value(store, conn):
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, {'USER_QUOTA_MB': 987654321987}, actor=audit.Actor.user(1, 'a'))
    assert exc.value.field == 'USER_QUOTA_MB' and '987654321987' not in str(exc.value)


def test_unknown_key_name_in_the_error_is_bounded(store, conn):
    with pytest.raises(SettingsError) as exc:
        store.set_many(conn, {'x' * 5000: 1}, actor=audit.Actor.user(1, 'a'))
    assert len(exc.value.field) <= 64


def test_writing_a_value_equal_to_the_current_one_still_bumps_the_version_and_is_traced(store, conn):
    v = version(conn)
    store.set_many(conn, {'USER_QUOTA_MB': 2048}, actor=audit.Actor.user(1, 'a'))
    assert version(conn) == v + 1 and count(conn, 'audit_log') == 1


def test_every_registry_entry_can_be_set_and_reset_through_the_store(store, conn):
    for key, s in REGISTRY.items():
        if s.type not in ('int', 'float'):
            continue
        store.set_many(conn, {key: s.max}, actor=audit.Actor.user(1, 'a'))
        assert store.get(key) == s.max
        assert store.reset(conn, key, actor=CLI) == 1
        assert store.get(key) == s.default


# --- Versions et deux processus (EF-6.4, M10, CA-21d) -------------------------------------------

def test_a_reset_by_another_process_is_seen_by_the_next_request(app, client, db):
    """CA-21d : `flask settings-reset` tourne dans un autre processus (autre store, autre connexion)."""
    store = app.extensions['settings']
    store.set_many(db, {'USER_QUOTA_MB': 7}, actor=audit.Actor.user(1, 'alice'))
    assert effective(app.config, 'USER_QUOTA_MB') == 7
    other = database.connect(app.config['DB_PATH'])
    try:
        assert SettingsStore().reset(other, 'USER_QUOTA_MB', actor=CLI) == 1
    finally:
        other.close()
    assert effective(app.config, 'USER_QUOTA_MB') == 7           # le serveur n'a pas encore relu
    client.get('/api/health')
    assert effective(app.config, 'USER_QUOTA_MB') == 2048


def test_the_cli_command_itself_resets_and_the_server_sees_it(app, client, db):
    app.extensions['settings'].set_many(db, {'USER_QUOTA_MB': 7}, actor=audit.Actor.user(1, 'alice'))
    result = app.test_cli_runner().invoke(args=['settings-reset', 'USER_QUOTA_MB'])
    assert result.exit_code == 0 and '1 valeur' in result.output
    client.get('/api/health')
    assert effective(app.config, 'USER_QUOTA_MB') == 2048
    last = db.execute("SELECT actor_type, actor_id, actor_label FROM audit_log WHERE action = 'setting.reset'").fetchone()
    assert tuple(last) == ('cli', None, '')


def test_a_reset_all_by_another_process_is_seen_by_the_background_reader_without_any_request(app, db):
    """L'ordonnanceur lit `effective(cfg, clé, db)` à chaque tour : il voit le changement sans requête."""
    store = app.extensions['settings']
    store.set_many(db, {'USER_QUOTA_MB': 7, 'ratelimit.token_per_ip': 9}, actor=audit.Actor.user(1, 'alice'))
    other = database.connect(app.config['DB_PATH'])
    try:
        SettingsStore().reset(other, None, actor=CLI)
    finally:
        other.close()
    assert effective(app.config, 'USER_QUOTA_MB') == 7
    assert effective(app.config, 'USER_QUOTA_MB', db) == 2048 and effective(app.config, 'ratelimit.token_per_ip', db) == 10


def test_a_write_by_another_process_then_a_reset_is_seen_even_if_the_values_are_equal(app, client, db):
    """ABA : deux écritures entre deux requêtes ⇒ la version a bougé, le cache est rechargé."""
    store = app.extensions['settings']
    other = database.connect(app.config['DB_PATH'])
    try:
        v = version(other)
        SettingsStore().set_many(other, {'USER_QUOTA_MB': 3}, actor=CLI)
        SettingsStore().reset(other, 'USER_QUOTA_MB', actor=CLI)
        assert version(other) == v + 2
    finally:
        other.close()
    assert client.get('/api/health').status_code == 200
    assert store._state.version == version(db) and effective(app.config, 'USER_QUOTA_MB') == 2048


def test_refresh_is_one_cheap_row_read_when_nothing_changed(store, conn):
    store.refresh(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    assert store.refresh(conn) is False
    conn.set_trace_callback(None)
    assert len(statements) == 1 and 'settings_meta' in statements[0] and 'FROM settings ' not in statements[0]


def test_concurrent_writers_never_lose_a_version_or_an_audit_row(path):
    errors = []

    def work(n):
        try:
            c = database.connect(path)
            try:
                s = SettingsStore()
                for i in range(15):
                    s.set_many(c, {'USER_QUOTA_MB': (n * 15 + i) % 1000}, actor=audit.Actor.user(n + 1, f'w{n}'))
            finally:
                c.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    c = database.connect(path)
    v0 = version(c)
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    try:
        assert not errors, errors
        assert version(c) == v0 + 90 and count(c, 'audit_log') == 90 and count(c) == 1
    finally:
        c.close()


def test_readers_during_writes_only_ever_see_a_complete_old_or_new_value(path):
    stop, seen, errors = threading.Event(), set(), []
    store = SettingsStore()
    writer_conn = database.connect(path)
    store.refresh(writer_conn, force=True)

    def reader():
        c = database.connect(path)
        try:
            while not stop.is_set():
                store.refresh(c)
                seen.add(store.get('USER_QUOTA_MB'))
        except Exception as exc:
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    [t.start() for t in threads]
    for i in range(60):
        store.set_many(writer_conn, {'USER_QUOTA_MB': 100 + i % 3}, actor=audit.Actor.user(1, 'w'))
        if i % 7 == 0:
            store.reset(writer_conn, None, actor=CLI)
    stop.set()
    [t.join(30) for t in threads]
    writer_conn.close()
    assert not errors, errors
    assert seen <= {2048, 100, 101, 102}


def test_audit_old_value_is_the_value_actually_replaced(conn, path):
    mine = SettingsStore()
    mine.refresh(conn, force=True)
    other = database.connect(path)
    SettingsStore().set_many(other, {'USER_QUOTA_MB': 5}, actor=CLI)
    other.close()
    mine.set_many(conn, {'USER_QUOTA_MB': 10}, actor=audit.Actor.user(1, 'alice'))
    details = json.loads(conn.execute("SELECT details FROM audit_log WHERE actor_label = 'alice'").fetchone()[0])
    assert details['old'] == 5


def test_a_deeply_nested_json_value_in_base_is_ignored_not_fatal(conn, env):
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('USER_QUOTA_MB', ?, 'x', 'y')", ('[' * 100_000,))
    conn.commit()
    s = SettingsStore(env=env.get)
    s.refresh(conn, force=True)
    assert s.get('USER_QUOTA_MB') == 2048


def test_the_application_boots_with_a_poisoned_settings_row(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    overrides = {'DATA_DIR': str(data)}
    app = create_app(TestingConfig, overrides=overrides)
    c = database.connect(app.config['DB_PATH'])
    c.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('USER_QUOTA_MB', ?, 'x', 'y')", ('[' * 100_000,))
    c.commit()
    c.close()
    create_app(TestingConfig, overrides=overrides)


def test_float_setting_with_an_oversized_integer_is_a_value_error():
    f = Setting('a.f', 'float', 1.0, 's', 'l', min=0, max=2)
    with pytest.raises(ValueError):
        f.validate(HUGE)


# --- USER_QUOTA_MB : 0 = illimité, Config = None -----------------------------------------------

def test_zero_is_a_real_value_in_every_layer_not_a_missing_one(store, conn, env):
    assert store.get('USER_QUOTA_MB') == 2048
    for raw in (0, '0', ' 0 '):
        env['USER_QUOTA_MB'] = raw
        assert store.get('USER_QUOTA_MB') == 0 and store.source('USER_QUOTA_MB') == 'env', raw
    env.clear()
    store.set_many(conn, {'USER_QUOTA_MB': 0}, actor=audit.Actor.user(1, 'a'))
    assert store.get('USER_QUOTA_MB') == 0 and store.source('USER_QUOTA_MB') == 'base'
    d = {x['key']: x for x in store.diagnostic()}['USER_QUOTA_MB']
    assert d['value'] == 0 and d['source'] == 'base'


def test_effective_treats_zero_as_zero_and_none_as_absent():
    for cfg, expected in (({'USER_QUOTA_MB': 0}, 0), ({'USER_QUOTA_MB': '0'}, 0), ({'USER_QUOTA_MB': None}, 2048),
                          ({'USER_QUOTA_MB': ''}, 2048), ({}, 2048)):
        assert effective(cfg, 'USER_QUOTA_MB') == expected, cfg


def test_config_none_falls_back_to_base_then_default(app, db):
    app.config['USER_QUOTA_MB'] = None
    assert effective(app.config, 'USER_QUOTA_MB') == 2048
    app.extensions['settings'].set_many(db, {'USER_QUOTA_MB': 5}, actor=audit.Actor.user(1, 'a'))
    assert effective(app.config, 'USER_QUOTA_MB') == 5


def _ingest_with_filler(app, db, quota_mb_setting, filler_bytes):
    db.execute("INSERT OR IGNORE INTO users (id, sub, username) VALUES (1, 'u', 'u')")
    db.commit()
    app.extensions['settings'].set_many(db, {'USER_QUOTA_MB': quota_mb_setting}, actor=audit.Actor.user(1, 'a'))
    folder = os.path.join(app.config['DATA_DIR'], 'files', '1')
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, 'filler.bin'), 'wb') as f:
        f.write(b'0' * filler_bytes)
    return ingest_service.ingest_bytes(db, app.config, 1, make_gpx(line(20)).encode(), 'a.gpx', 'upload')


def test_quota_zero_means_unlimited_for_ingestion(app, db):
    result = _ingest_with_filler(app, db, 0, 3 * 1024 * 1024)
    assert result.status != 'error' or 'Quota' not in result.message


def test_quota_one_mb_still_refuses_when_exceeded(app, db):
    result = _ingest_with_filler(app, db, 1, 2 * 1024 * 1024)
    assert result.status == 'error' and 'Quota' in result.message


def test_quota_at_the_upper_bound_is_accepted_without_overflow(app, db):
    result = _ingest_with_filler(app, db, 102_400, 1024)
    assert result.status != 'error' or 'Quota' not in result.message


def test_a_corrupt_quota_in_base_falls_back_to_the_default_not_to_unlimited(app, db):
    """Une valeur illisible ne doit jamais devenir « illimité » (0) : repli sur le défaut 2048."""
    db.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('USER_QUOTA_MB', 'zéro', 'x', 'y')")
    db.execute("UPDATE settings_meta SET version = version + 1")
    db.commit()
    app.extensions['settings'].refresh(db)
    assert effective(app.config, 'USER_QUOTA_MB') == 2048


# --- Seuils du limiteur à chaud (EF-6.4, M7) ----------------------------------------------------

FAMILIES = {'password_per_identifier': 'password_identifier', 'password_per_ip': 'password_ip', 'mfa_per_account': 'mfa_account',
            'forgot_per_identifier': 'forgot_identifier', 'forgot_per_ip': 'forgot_ip', 'token_per_ip': 'token_ip',
            'generation_per_user': 'generation_user'}


def test_every_limit_field_maps_to_a_family_and_a_registry_key():
    assert set(FAMILIES) == set(SETTING_KEYS) and all(k in REGISTRY for k in SETTING_KEYS.values())


@pytest.mark.parametrize('field', list(FAMILIES))
def test_each_threshold_follows_the_store_at_both_bounds(store, conn, field):
    key, s = SETTING_KEYS[field], REGISTRY[SETTING_KEYS[field]]
    limiters = Limiters(get=store.get, buckets=64, secret=b's' * 32)
    family = getattr(limiters, FAMILIES[field])
    assert family.limit == s.default
    for value in (s.min, s.max, s.default):
        store.set_many(conn, {key: value}, actor=audit.Actor.user(1, 'a'))
        assert family.limit == value
    store.reset(conn, key, actor=CLI)
    assert family.limit == s.default


@pytest.mark.parametrize('field', list(FAMILIES))
def test_lowering_below_the_current_count_blocks_at_once_and_raising_reopens(store, conn, field):
    class Clock:
        t = 10_000.0

        def __call__(self):
            return self.t

    key, s = SETTING_KEYS[field], REGISTRY[SETTING_KEYS[field]]
    clock = Clock()
    family = getattr(Limiters(get=store.get, clock=clock, buckets=64, secret=b's' * 32), FAMILIES[field])
    store.set_many(conn, {key: s.max}, actor=audit.Actor.user(1, 'a'))
    used = s.min + 1
    assert all(family.hit('k')[0] for _ in range(used))
    store.set_many(conn, {key: s.min}, actor=audit.Actor.user(1, 'a'))
    assert family.hit('k')[0] is False                          # durci : s'applique aussitôt, compteur conservé
    store.set_many(conn, {key: s.max}, actor=audit.Actor.user(1, 'a'))
    assert family.hit('k')[0] is True                           # assoupli : rouvre


def test_the_limiter_never_goes_below_one_even_if_the_source_misbehaves_later():
    current = {'value': 5}
    limiters = Limiters(get=lambda _key: current['value'], buckets=8, secret=b's' * 32)
    for bad in (0, -5):
        current['value'] = bad
        assert limiters.password_identifier.limit == 1


def test_a_source_that_is_already_wrong_at_construction_is_refused():
    with pytest.raises(ValueError):
        Limiters(get=lambda _key: 0, buckets=8, secret=b's' * 32)


def test_a_threshold_changed_by_another_process_applies_after_the_next_refresh(app, db):
    store = app.extensions['settings']
    limiters = Limiters(get=store.get, buckets=64, secret=b's' * 32)
    other = database.connect(app.config['DB_PATH'])
    SettingsStore().set_many(other, {'ratelimit.password_per_identifier': 20}, actor=CLI)
    other.close()
    assert limiters.password_identifier.limit == 5
    store.refresh(db)
    assert limiters.password_identifier.limit == 20


# --- Aucune valeur secrète nulle part (EF-9.2, EF-6.3c, EF-S5) ---------------------------------------

SECRET = Setting('svc.token', 'str', '', 'Test', 'Jeton', max_length=40, secret=True, env='SVC_TOKEN')
S3CRET = ('valeur-base-S3CRET', 'valeur-env-S3CRET', 'defaut-S3CRET', 'autre-S3CRET')


@pytest.fixture
def secret_store(conn, env):
    registry = {'svc.token': Setting('svc.token', 'str', S3CRET[2], 'Test', 'Jeton', max_length=40, secret=True, env='SVC_TOKEN')}
    s = SettingsStore(registry=registry, env=env.get)
    env['SVC_TOKEN'] = S3CRET[1]
    s.refresh(conn, force=True)
    return s


def test_no_registry_entry_is_secret_until_secret_storage_is_decided():
    """Garde-fou : `settings.value` est en clair (JSON). Un réglage `secret=True` y serait stocké sans chiffrement ;
    les secrets passent par le magasin chiffré (ADR-0003), pas par ce registre. À revoir avant d'en ajouter un."""
    assert [k for k, s in REGISTRY.items() if s.secret] == []


def test_a_secret_setting_leaks_nowhere_through_every_operation(secret_store, conn, env, caplog):
    with caplog.at_level(logging.DEBUG):
        secret_store.set_many(conn, {'svc.token': S3CRET[0]}, actor=audit.Actor.user(1, 'alice'))
        secret_store.set_many(conn, {'svc.token': S3CRET[3]}, actor=audit.Actor.user(1, 'alice'))
        secret_store.diagnostic()
        secret_store.log_overrides()
        secret_store.refresh(conn, force=True)
        with pytest.raises(SettingsError) as exc:
            secret_store.set_many(conn, {'svc.token': S3CRET[0] * 5}, actor=audit.Actor.user(1, 'alice'))
        env['SVC_TOKEN'] = 'x' * 500
        secret_store._warned.clear()
        secret_store.get('svc.token')
        secret_store.reset(conn, 'svc.token', actor=CLI)
    dump = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")]) + json.dumps(secret_store.diagnostic())
    for text in (dump, caplog.text, str(exc.value)):
        assert 'S3CRET' not in text and 'x' * 50 not in text


def test_a_secret_with_an_invalid_env_reports_the_variable_name_only(secret_store, env, caplog):
    env['SVC_TOKEN'] = 'y' * 200
    with caplog.at_level(logging.DEBUG):
        d = secret_store.diagnostic()[0]
    assert d['env_invalid'] is True and 'SVC_TOKEN' in caplog.text and 'y' * 20 not in caplog.text
    assert 'y' * 20 not in json.dumps(d)


def test_settings_list_cli_masks_secret_values(app, db):
    registry = {'svc.token': Setting('svc.token', 'str', S3CRET[2], 'Test', 'Jeton', max_length=40, secret=True, env='SVC_TOKEN')}
    store = SettingsStore(registry=registry, env={'SVC_TOKEN': S3CRET[1]}.get)
    app.extensions['settings'] = store
    store.refresh(db, force=True)
    store.set_many(db, {'svc.token': S3CRET[0]}, actor=audit.Actor.user(1, 'alice'))
    out = app.test_cli_runner().invoke(args=['settings-list']).output
    assert 'S3CRET' not in out and 'svc.token' in out and '[masqué]' in out


def test_the_settings_table_never_holds_more_than_the_json_value_and_the_actor_label(conn, store):
    store.set_many(conn, {'USER_QUOTA_MB': 12}, actor=audit.Actor.user(9, 'alice'), ip='203.0.113.9')
    row = dict(conn.execute("SELECT * FROM settings").fetchone())
    assert set(row) == {'key', 'value', 'updated_at', 'updated_by'} and '203.0.113.9' not in json.dumps(row)


def test_app_config_publishes_the_store_under_one_key(app):
    assert app.config[STORE_KEY] is app.extensions['settings']
