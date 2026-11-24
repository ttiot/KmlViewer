"""Journal d'audit (T2, EF-9, CA-24)."""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import audit, db
from app.audit import Actor

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / 'a.sqlite3')
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY id")]


def test_migration_is_named_and_recorded(conn):
    assert '0006_audit_log_v2' in [n for n, _ in db.NAMED_MIGRATIONS]
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = '0006_audit_log_v2'").fetchone()
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(audit_log)")}
    assert cols == {'id', 'at', 'actor_type', 'actor_id', 'actor_label', 'action', 'target_type', 'target_id', 'details', 'ip'}


def test_record_stores_every_field(conn):
    rid = audit.record(conn, 'setting.update', actor=Actor.user(7, 'alice'), target_type='setting',
                       target_id='USER_QUOTA_MB', details={'old': 1, 'new': 2}, ip='203.0.113.9', now=NOW)
    (row,) = rows(conn)
    assert row['id'] == rid and row['at'] == '2026-11-24T12:00:00Z'
    assert (row['actor_type'], row['actor_id'], row['actor_label'], row['action']) == ('user', 7, 'alice', 'setting.update')
    assert (row['target_type'], row['target_id'], row['ip']) == ('setting', 'USER_QUOTA_MB', '203.0.113.9')
    assert json.loads(row['details']) == {'old': 1, 'new': 2}
    assert not conn.in_transaction       # validé quand l'appelant n'avait rien ouvert


def test_cli_and_system_actions_are_typed_with_no_account_and_an_empty_label(conn):
    """CA-24 : action CLI ⇒ actor_type 'cli', actor_id nul ; tâche (purge) ⇒ 'system'."""
    audit.record(conn, 'setting.reset', actor=Actor.cli())
    audit.record(conn, 'audit.purge', actor=Actor.system())
    cli, system = rows(conn)
    assert (cli['actor_type'], cli['actor_id'], cli['actor_label']) == ('cli', None, '')
    assert (system['actor_type'], system['actor_id'], system['actor_label']) == ('system', None, '')


def test_actor_from_user_prefers_username_then_email_then_id():
    assert Actor.from_user({'id': 3, 'username': 'bob', 'email': 'b@x'}) == Actor('user', 3, 'bob')
    assert Actor.from_user({'id': 3, 'username': None, 'email': 'b@x'}).label == 'b@x'
    assert Actor.from_user({'id': 3, 'username': '', 'email': ''}).label == 'user#3'
    assert Actor.from_user({'id': 4}).label == 'user#4'


@pytest.mark.parametrize('name', ['cli', 'system', 'CLI', 'System'])
def test_an_sso_account_named_cli_stays_a_user_and_is_not_found_by_the_cli_filter(conn, name):
    """CA-24b : l'origine se lit sur actor_type, jamais sur le libellé."""
    actor = Actor.from_user({'id': 9, 'username': name})
    audit.record(conn, 'setting.update', actor=actor)
    audit.record(conn, 'setting.reset', actor=Actor.cli())
    row = rows(conn)[0]
    assert (row['actor_type'], row['actor_id'], row['actor_label']) == ('user', 9, name)
    assert [e['id'] for e in audit.list_entries(conn, actor_type='cli')] == [2]
    assert [e['id'] for e in audit.list_entries(conn, actor_type='user')] == [1]


def test_database_refuses_a_cli_row_with_an_account_and_a_user_row_without(conn):
    """CA-24b : la contrainte est en base, pas seulement dans le code."""
    base = "INSERT INTO audit_log (at, actor_type, actor_id, actor_label, action) VALUES ('2026-01-01T00:00:00Z', ?, ?, ?, 'x.y')"
    for bad in (('cli', 5, ''), ('system', 5, 'x'), ('user', None, 'alice'), ('root', None, ''), (None, None, '')):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, bad)
    conn.execute(base, ('cli', None, ''))
    conn.execute(base, ('user', 5, 'alice'))


def test_actor_rules():
    for bad in (dict(type='user'), dict(type='user', id=0, label='a'), dict(type='user', id=True, label='a'),
                dict(type='user', id=1, label=''), dict(type='user', id=1, label='   '), dict(type='user', id=1, label='\n\x00'),
                dict(type='user', id=1, label=None), dict(type='cli', id=1), dict(type='system', label='x'), dict(type='root')):
        with pytest.raises(ValueError):
            Actor(**bad)
    assert Actor.user(1, ' alice\n ').label == 'alice'


def test_record_requires_an_actor_object_never_a_label(conn):
    for bad in ('cli', None, {'type': 'cli'}):
        with pytest.raises(TypeError):
            audit.record(conn, 'x.y', actor=bad)
    with pytest.raises(TypeError):
        audit.record(conn, 'x.y', actor_label='cli')
    assert rows(conn) == []


def test_unknown_actor_type_filter_is_refused(conn):
    with pytest.raises(ValueError):
        audit.list_entries(conn, actor_type='root')


def test_label_survives_account_deletion(conn):
    audit.record(conn, 'user.delete', actor=Actor.user(7, 'alice'))
    assert rows(conn)[0]['actor_label'] == 'alice'      # pas de clé étrangère : l'instantané reste


def test_secrets_in_details_are_masked_at_any_depth(conn):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), details={
        'password': 'hunter2', 'new_password': 'x', 'token': 't', 'api_key': 'k', 'secret_value': 's',
        'recovery_codes': ['1', '2'], 'code': '123456', 'Authorization_Cookie': 'c', 'otp': '1',
        'nested': {'password_hash': 'h', 'ok': 'visible'}, 'list': [{'token': 't'}], 'key_name': 'visible',
        'status_code': 200})
    text = rows(conn)[0]['details']
    for leaked in ('hunter2', '"t"', '123456', '"c"', '"h"', '"s"'):
        assert leaked not in text, leaked
    details = json.loads(text)
    assert details['nested']['ok'] == 'visible' and details['key_name'] == 'visible'
    assert details['status_code'] == 200 and details['password'] == audit.MASK


def test_long_values_are_shortened_but_the_structure_survives(conn):
    """T2-2 : on garde les clés et un marqueur, on ne perd pas tout."""
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), details={'blob': 'x' * 10_000, 'tiles': [f't{i}' * 20 for i in range(60)],
                                                        'old': 1})
    details = json.loads(rows(conn)[0]['details'])
    assert details['blob'].endswith('…[tronqué]') and len(details['blob']) < 300
    assert len(details['tiles']) == audit.MAX_LIST_ITEMS + 1 and details['tiles'][-1] == '…(+40)'
    assert details['old'] == 1


def test_a_huge_flat_dict_falls_back_to_a_marker(conn):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), details={f'k{i}': 'v' * 150 for i in range(100)})
    assert json.loads(rows(conn)[0]['details']) == {'tronqué': True}


# --- T2-1 : les valeurs d'un réglage secret ne sont jamais écrites -----------------------------

def all_text(conn):
    return json.dumps(rows(conn))


def test_secret_setting_change_never_stores_either_value(conn):
    audit.record_setting_change(conn, 'smtp.password', 'hunter2hunter2', 's3cr3t-nouveau', secret=True, actor=Actor.user(1, 'alice'))
    text = all_text(conn)
    assert 'hunter2hunter2' not in text and 's3cr3t-nouveau' not in text
    row = rows(conn)[0]
    assert (row['action'], row['target_id']) == ('setting.update', 'smtp.password')
    assert json.loads(row['details']) == {'key': 'smtp.password', 'changed': True}     # le NOM du réglage reste lisible


def test_non_secret_setting_change_keeps_both_values(conn):
    audit.record_setting_change(conn, 'USER_QUOTA_MB', 1024, 2048, secret=False, actor=Actor.user(1, 'alice'))
    assert json.loads(rows(conn)[0]['details']) == {'key': 'USER_QUOTA_MB', 'old': 1024, 'new': 2048}


@pytest.mark.parametrize('flag', [None, 'oui', 1, 0])
def test_secret_flag_must_be_an_explicit_boolean(conn, flag):
    """Pas de « truthy » ni de défaut : le type du registre est passé tel quel."""
    with pytest.raises(TypeError):
        audit.record_setting_change(conn, 'k', 'a', 'b', secret=flag, actor=Actor.user(1, 'a'))
    assert rows(conn) == []


def test_secret_events_take_no_value_and_only_known_events(conn):
    audit.record_secret_event(conn, 'routing.api_key', 'update', actor=Actor.user(1, 'alice'), reason='destination liée : routing')
    row = rows(conn)[0]
    assert (row['action'], row['target_type'], row['target_id']) == ('secret.update', 'secret', 'routing.api_key')
    with pytest.raises(ValueError):
        audit.record_secret_event(conn, 'x', 'dump', actor=Actor.user(1, 'a'))
    import inspect
    assert not {'value', 'plaintext', 'secret'} & set(inspect.signature(audit.record_secret_event).parameters)


def test_since_and_until_accept_any_iso_format_and_refuse_garbage(conn):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), now=NOW)                    # 12:00:00Z
    assert len(audit.list_entries(conn, since='2026-11-24T13:00:00+01:00')) == 1      # 12:00Z
    assert len(audit.list_entries(conn, since='2026-11-24T12:00:00')) == 1             # naïf = UTC
    assert len(audit.list_entries(conn, until='2026-11-24T12:00:00.500Z')) == 1
    assert audit.list_entries(conn, since='2026-11-24T12:00:01Z') == []
    with pytest.raises(ValueError):
        audit.list_entries(conn, since='hier')


def test_non_serializable_values_do_not_fail(conn):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), details={'when': NOW, 'obj': object, 'set': {1}})
    assert json.loads(rows(conn)[0]['details'])['when']


def test_free_text_is_cleaned_and_bounded(conn):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a\nFAKE LOG LINE\x1b[31m'), target_type='t' * 100, target_id='id\r\nforged' + 'z' * 500,
                 ip='1.2.3.4\nx')
    row = rows(conn)[0]
    for field in ('actor_label', 'target_type', 'target_id', 'ip'):
        assert '\n' not in row[field] and '\r' not in row[field] and '\x1b' not in row[field]
    assert len(row['actor_label']) <= 64 and len(row['target_type']) <= 32 and len(row['target_id']) <= 128


@pytest.mark.parametrize('action', ['', 'Setting.Update', 'a b', "a'; DROP TABLE audit_log;--", 'x' * 65, '1abc', None, 5])
def test_invalid_actions_are_refused(conn, action):
    with pytest.raises(ValueError):
        audit.record(conn, action, actor=Actor.user(1, 'a'))
    assert rows(conn) == []


def test_record_joins_the_callers_transaction_and_rolls_back_with_it(conn):
    """L'action et sa trace sont validées ou annulées ensemble."""
    with pytest.raises(RuntimeError), db.write_transaction(conn):
        audit.record(conn, 'x.y', actor=Actor.user(1, 'a'))
        assert conn.in_transaction
        raise RuntimeError
    assert rows(conn) == []
    with db.write_transaction(conn):
        audit.record(conn, 'x.y', actor=Actor.user(1, 'a'))
    assert len(rows(conn)) == 1


# --- Consultation ---------------------------------------------------------------------------

def seed(conn, n=10):
    for i in range(n):
        audit.record(conn, 'secret.set' if i % 2 else 'setting.update', actor=Actor.user(i % 3 + 1, 'a'),
                     now=NOW + timedelta(minutes=i))


def test_list_is_newest_first_filterable_and_paginated(conn):
    seed(conn)
    page = audit.list_entries(conn, limit=4)
    assert [e['id'] for e in page] == [10, 9, 8, 7]
    nxt = audit.list_entries(conn, limit=4, before_id=page[-1]['id'])
    assert [e['id'] for e in nxt] == [6, 5, 4, 3]
    assert {e['action'] for e in audit.list_entries(conn, action='secret.set')} == {'secret.set'}
    assert {e['action'] for e in audit.list_entries(conn, action='secret.')} == {'secret.set'}
    assert {e['actor_id'] for e in audit.list_entries(conn, actor_id=2)} == {2}
    since = (NOW + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert len(audit.list_entries(conn, since=since)) == 5
    assert len(audit.list_entries(conn, until=since)) == 5


def test_prefix_filter_does_not_treat_wildcards_as_wildcards(conn):
    seed(conn)
    assert audit.list_entries(conn, action='%.') == [] and audit.list_entries(conn, action='_ecret.') == []


def test_limit_is_clamped(conn):
    seed(conn, 5)
    assert len(audit.list_entries(conn, limit=0)) == 1
    assert len(audit.list_entries(conn, limit=10_000)) == 5


# --- Purge ----------------------------------------------------------------------------------

def test_purge_removes_only_old_rows_and_is_itself_traced(conn, caplog):
    for days in (200, 91, 89, 1, 0):
        audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), now=NOW - timedelta(days=days))
    with caplog.at_level(logging.INFO, logger='app.audit'):
        deleted = audit.purge(conn, 90, now=NOW)
    assert deleted == 2
    remaining = rows(conn)
    assert len(remaining) == 4                              # 3 gardées + la ligne de purge
    last = remaining[-1]
    assert (last['action'], last['actor_type'], last['actor_label'], last['actor_id']) == ('audit.purge', 'system', '', None)
    assert json.loads(last['details']) == {'deleted': 2, 'retention_days': 90}


def test_purge_boundary_is_exclusive_of_the_cutoff_second(conn):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), now=NOW - timedelta(days=30))          # pile à la limite : gardée
    audit.record(conn, 'x.z', actor=Actor.user(1, 'a'), now=NOW - timedelta(days=30, seconds=1))
    assert audit.purge(conn, 30, now=NOW) == 1


@pytest.mark.parametrize('days', [0, 29, 3651, -1, 90.0, '90', None, True])
def test_purge_refuses_out_of_bounds_retention(conn, days):
    seed(conn, 3)
    with pytest.raises(ValueError):
        audit.purge(conn, days, now=NOW + timedelta(days=9999))
    assert len(rows(conn)) == 3


def test_purge_default_is_90_days(conn):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), now=NOW - timedelta(days=100))
    assert audit.purge(conn, now=NOW) == 1 and audit.RETENTION_DEFAULT_DAYS == 90


def test_purge_is_atomic_with_its_trace(conn, monkeypatch):
    audit.record(conn, 'x.y', actor=Actor.user(1, 'a'), now=NOW - timedelta(days=100))

    def boom(*a, **k):
        raise RuntimeError

    monkeypatch.setattr(audit, 'record', boom)
    with pytest.raises(RuntimeError):
        audit.purge(conn, 90, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1       # rien supprimé sans trace


# --- Migration v2 : reconstruction d'une base passée par l'ancienne 0006 -----------------------

def test_old_shape_audit_table_is_rebuilt_without_losing_rows(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    c = db.connect(path)
    c.executescript(db.SCHEMA)
    db.migrate(c, named=[])
    c.execute("""CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor_id INTEGER,
                 actor_label TEXT NOT NULL, action TEXT NOT NULL, target_type TEXT, target_id TEXT, details TEXT, ip TEXT)""")
    c.execute("CREATE INDEX idx_audit_log_at ON audit_log (at)")
    c.execute("INSERT INTO schema_migrations (name) VALUES ('0006_audit_log')")
    c.executemany("INSERT INTO audit_log (at, actor_id, actor_label, action) VALUES (?, ?, ?, 'x.y')",
                  [('2026-01-01T00:00:00Z', 7, 'alice'), ('2026-01-01T00:00:01Z', None, 'cli'),
                   ('2026-01-01T00:00:02Z', None, 'system'), ('2026-01-01T00:00:03Z', None, 'alice')])
    c.commit()
    c.close()

    db.init_db(path)
    db.init_db(path)                                   # idempotent
    c = db.connect(path)
    got = [tuple(r) for r in c.execute("SELECT actor_type, actor_id, actor_label FROM audit_log ORDER BY id")]
    assert got == [('user', 7, 'alice'), ('cli', None, ''), ('system', None, ''), ('system', None, '')]
    assert c.execute("SELECT name FROM sqlite_master WHERE name = 'audit_log_v1'").fetchone() is None
    names = {r['name'] for r in c.execute("SELECT name FROM schema_migrations")}
    assert '0006_audit_log_v2' in names                # l'ancien nom reste, signalé en WARNING (ADR-0012)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO audit_log (at, actor_type, actor_id, action) VALUES ('x', 'cli', 3, 'x.y')")
    c.close()
