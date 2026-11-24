"""Fonctions bêta : off / users / all, 404 serveur, liste conservée (T5, EF-7, CA-22)."""

import json
import sqlite3

import pytest
from flask import jsonify, render_template_string

from app import create_app, db as database, features
from app.audit import Actor
from app.config import TestingConfig
from app.features import FEATURES, FeatureError, feature_required
from tests.conftest import OIDC_OVERRIDES, login_as

ADMIN = Actor.user(1, 'admin')
KEY = 'route_builder'


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / 'f.sqlite3')
    database.init_db(path)
    c = database.connect(path)
    c.executemany("INSERT INTO users (sub, username) VALUES (?, ?)", [(f's{i}', f'u{i}') for i in range(1, 6)])
    c.commit()
    yield c
    c.close()


def audit_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM audit_log WHERE action = 'feature.update' ORDER BY id")]


# --- Registre et migration -----------------------------------------------------------------------

def test_registry_and_migration(conn):
    assert 'route_builder' in FEATURES
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = '0010_feature_flags'").fetchone()
    assert {r['name'] for r in conn.execute("PRAGMA table_info(feature_users)")} == {'feature_key', 'user_id'}


def test_default_state_is_off_for_everyone(conn):
    assert features.get_state(conn, KEY) == {'key': KEY, 'state': 'off', 'users': [], 'updated_at': None, 'updated_by': None}
    assert not any(features.is_enabled(conn, KEY, i) for i in range(1, 6))


def test_database_refuses_an_unknown_state(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO feature_flags (key, state, updated_at, updated_by) VALUES ('x', 'beta', 'a', 'b')")


# --- États (EF-7.2) --------------------------------------------------------------------------------

def test_users_state_opens_only_for_the_listed_accounts_all_opens_for_everyone_off_closes(conn):
    features.set_state(conn, KEY, 'users', actor=ADMIN, user_ids=[2, 4])
    assert [features.is_enabled(conn, KEY, i) for i in range(1, 6)] == [False, True, False, True, False]
    features.set_state(conn, KEY, 'all', actor=ADMIN)
    assert all(features.is_enabled(conn, KEY, i) for i in range(1, 6))
    features.set_state(conn, KEY, 'off', actor=ADMIN)
    assert not any(features.is_enabled(conn, KEY, i) for i in range(1, 6))


def test_the_list_is_kept_between_states(conn):
    features.set_state(conn, KEY, 'users', actor=ADMIN, user_ids=[2, 3])
    features.set_state(conn, KEY, 'all', actor=ADMIN)
    features.set_state(conn, KEY, 'off', actor=ADMIN)
    assert features.get_state(conn, KEY)['users'] == [2, 3]
    features.set_state(conn, KEY, 'users', actor=ADMIN)                       # sans liste : la liste revient
    assert features.is_enabled(conn, KEY, 3) and not features.is_enabled(conn, KEY, 1)
    features.set_state(conn, KEY, 'users', actor=ADMIN, user_ids=[])          # liste vide explicite : personne
    assert not any(features.is_enabled(conn, KEY, i) for i in range(1, 6))


def test_deleting_a_user_removes_it_from_every_list(conn):
    features.set_state(conn, KEY, 'users', actor=ADMIN, user_ids=[2, 3])
    conn.execute("DELETE FROM users WHERE id = 2")
    conn.commit()
    assert features.get_state(conn, KEY)['users'] == [3]


def test_anonymous_and_unknown_features_are_never_enabled(conn):
    features.set_state(conn, KEY, 'all', actor=ADMIN)
    assert features.is_enabled(conn, KEY, None) is False
    assert features.is_enabled(conn, 'inconnue', 1) is False
    with pytest.raises(FeatureError):
        features.get_state(conn, 'inconnue')


# --- Écriture validée, tout ou rien, tracée ---------------------------------------------------------------

@pytest.mark.parametrize('args', [
    dict(key='inconnue', state='all'), dict(key=KEY, state='beta'), dict(key=KEY, state=None), dict(key=None, state='all'),
    dict(key=KEY, state='users', user_ids=[99]), dict(key=KEY, state='users', user_ids=[1, 1]),
    dict(key=KEY, state='users', user_ids=['1']), dict(key=KEY, state='users', user_ids=[True]),
    dict(key=KEY, state='users', user_ids=list(range(1, 1002))),
])
def test_invalid_writes_change_nothing(conn, args):
    features.set_state(conn, KEY, 'users', actor=ADMIN, user_ids=[3])
    before = features.get_state(conn, KEY)
    with pytest.raises(FeatureError):
        features.set_state(conn, args.pop('key'), args.pop('state'), actor=ADMIN, **args)
    assert features.get_state(conn, KEY) == before and len(audit_rows(conn)) == 1
    assert not conn.in_transaction


def test_every_change_is_audited_with_the_actor_and_no_user_list_values(conn):
    features.set_state(conn, KEY, 'users', actor=Actor.user(1, 'admin'), user_ids=[2, 3], ip='203.0.113.5')
    features.set_state(conn, KEY, 'all', actor=Actor.cli())
    first, second = audit_rows(conn)
    assert (first['actor_type'], first['actor_id'], first['target_id'], first['ip']) == ('user', 1, KEY, '203.0.113.5')
    assert json.loads(first['details']) == {'old': 'off', 'new': 'users', 'listed_users': 2, 'list_changed': True,
                                            'added': [2, 3], 'removed': []}
    assert (second['actor_type'], json.loads(second['details'])['old']) == ('cli', 'users')
    assert json.loads(second['details'])['list_changed'] is False and json.loads(second['details'])['added'] == []
    assert features.get_state(conn, KEY)['updated_by'] == 'cli'
    features.set_state(conn, KEY, 'users', actor=ADMIN, user_ids=[3, 4])
    third = audit_rows(conn)[-1]
    assert (json.loads(third['details'])['added'], json.loads(third['details'])['removed']) == ([4], [2])


def test_write_and_audit_are_one_transaction(conn, monkeypatch):
    monkeypatch.setattr(features.audit, 'record', lambda *a, **k: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        features.set_state(conn, KEY, 'all', actor=ADMIN)
    assert features.get_state(conn, KEY)['state'] == 'off'


# --- Contrôle serveur et HTML (EF-7.3, EF-7.4, CA-22) ---------------------------------------------------

@pytest.fixture
def web(tmp_path):
    app = create_app(TestingConfig, overrides={**OIDC_OVERRIDES, 'DATA_DIR': str(tmp_path / 'w')})

    @app.route('/api/beta-probe')
    @feature_required(KEY)
    def probe():
        return jsonify({'ok': True})

    @app.route('/beta-page')
    @feature_required(KEY)
    def page():
        return 'page'

    @app.route('/beta-menu')
    def menu():
        return render_template_string("{% if feature_enabled('route_builder') %}<button>Créer une trace</button>{% endif %}x")

    return app


def conn_of(app):
    return database.connect(app.config['DB_PATH'])


def test_unavailable_function_answers_404_not_403_and_hides_its_entry(web):
    """CA-22 : off, ou users sans l'utilisateur => 404 et absente du HTML."""
    client, uid = login_as(web, 'alice')
    other_client, other = login_as(web, 'bob')
    c = conn_of(web)
    for setup in (lambda: None, lambda: features.set_state(c, KEY, 'users', actor=ADMIN, user_ids=[other])):
        setup()
        assert client.get('/api/beta-probe').status_code == 404
        assert client.get('/beta-page').status_code == 404
        assert 'Créer une trace' not in client.get('/beta-menu').get_data(as_text=True)
    assert other_client.get('/api/beta-probe').status_code == 200
    assert 'Créer une trace' in other_client.get('/beta-menu').get_data(as_text=True)
    c.close()


def test_users_with_the_user_or_all_get_access_and_the_change_is_seen_at_once(web):
    client, uid = login_as(web, 'carol')
    c = conn_of(web)
    features.set_state(c, KEY, 'users', actor=ADMIN, user_ids=[uid])
    assert client.get('/api/beta-probe').status_code == 200
    features.set_state(c, KEY, 'off', actor=ADMIN)                           # autre processus : vu à la requête suivante
    assert client.get('/api/beta-probe').status_code == 404
    features.set_state(c, KEY, 'all', actor=ADMIN)
    assert client.get('/api/beta-probe').status_code == 200 and client.get('/beta-page').status_code == 200
    c.close()


def test_anonymous_requests_never_reach_a_feature(web):
    assert web.test_client().get('/api/beta-probe').status_code in (401, 302)


def test_unknown_feature_in_the_decorator_is_a_code_error():
    with pytest.raises(FeatureError):
        feature_required('inexistante')
