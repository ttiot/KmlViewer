"""Une session ne vaut que pour le compte qui l'a ouverte, et seulement tant qu'il ne l'a pas fermée."""

import io
import time

from app import db as database
from app.auth import oidc, users
from tests.conftest import line, login_as, make_gpx

# Table users telle qu'elle existait avant la migration 3 (sans AUTOINCREMENT ni jeton de session)
LEGACY_USERS = """
CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY,
    sub          TEXT NOT NULL UNIQUE,
    email        TEXT,
    username     TEXT,
    display_name TEXT,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    is_active    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    last_login   TEXT
);
"""


def _replay(app, cookie):
    """Client qui présente un cookie copié plus tôt, comme le ferait son voleur."""
    client = app.test_client()
    client.set_cookie('session', cookie)
    return client


def _import(client, name='Sortie privée'):
    data = {'files': [(io.BytesIO(make_gpx(line(30), name=name).encode()), 'a.gpx')]}
    return client.post('/api/activities', data=data, content_type='multipart/form-data')


def test_deleted_account_cookie_never_reaches_the_next_account(oidc_app):
    admin, _ = login_as(oidc_app, 'admin', admin=True)
    mallory, mallory_id = login_as(oidc_app, 'mallory')
    stolen = mallory.get_cookie('session').value
    assert admin.delete(f'/api/users/{mallory_id}').status_code == 200

    victim, victim_id = login_as(oidc_app, 'victim')
    assert victim_id != mallory_id  # l'identifiant du compte supprimé n'est pas réattribué
    assert _import(victim).get_json()['results'][0]['status'] == 'created'
    assert _replay(oidc_app, stolen).get('/api/me').status_code == 401
    assert _replay(oidc_app, stolen).get('/api/activities').status_code == 401


def test_session_is_refused_for_an_account_it_was_not_issued_for(oidc_app, db):
    """Même si un identifiant revenait à un autre compte, le jeton de l'ancien n'ouvre rien."""
    _, alice_id = login_as(oidc_app, 'alice')
    alice_token = users.session_token(db, alice_id)
    _, bob_id = login_as(oidc_app, 'bob')
    client = oidc_app.test_client()
    for token in (alice_token, '', None, 42):
        with client.session_transaction() as session:
            session.update(user_id=bob_id, token=token, login_at=int(time.time()))
        assert client.get('/api/me').status_code == 401
    with client.session_transaction() as session:
        session.update(user_id=str(bob_id), token=users.session_token(db, bob_id), login_at=int(time.time()))
    assert client.get('/api/me').status_code == 401  # identifiant qui n'est pas un entier


def test_logout_invalidates_copies_of_the_cookie(oidc_app, monkeypatch):
    monkeypatch.setattr(oidc.oauth.oidc, 'load_server_metadata', lambda: {})
    alice, _ = login_as(oidc_app, 'alice')
    copied = alice.get_cookie('session').value
    assert _replay(oidc_app, copied).get('/api/me').status_code == 200
    assert alice.post('/auth/logout').status_code == 302
    assert alice.get('/api/me').status_code == 401
    assert _replay(oidc_app, copied).get('/api/me').status_code == 401
    # Une nouvelle connexion repart sur le nouveau jeton
    assert login_as(oidc_app, 'alice')[0].get('/api/me').status_code == 200


def test_login_on_a_second_device_keeps_the_first_session(oidc_app):
    first, _ = login_as(oidc_app, 'alice')
    second, _ = login_as(oidc_app, 'alice')
    assert first.get('/api/me').status_code == 200 and second.get('/api/me').status_code == 200


def test_deactivation_closes_sessions_and_reactivation_does_not_reopen_them(oidc_app):
    admin, _ = login_as(oidc_app, 'admin', admin=True)
    bob, bob_id = login_as(oidc_app, 'bob')
    copied = bob.get_cookie('session').value
    assert admin.patch(f'/api/users/{bob_id}', json={'is_active': False}).status_code == 200
    assert _replay(oidc_app, copied).get('/api/me').status_code == 401
    assert admin.patch(f'/api/users/{bob_id}', json={'is_active': True}).status_code == 200
    assert _replay(oidc_app, copied).get('/api/me').status_code == 401
    assert login_as(oidc_app, 'bob')[0].get('/api/me').status_code == 200


def test_callback_binds_the_session_to_the_account_token(oidc_app, db, monkeypatch):
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token', lambda: {'userinfo': {'sub': 'u1'}})
    client = oidc_app.test_client()
    client.get('/auth/callback?code=x&state=y')
    with client.session_transaction() as session:
        token = session['token']
    row = db.execute("SELECT session_token FROM users WHERE sub = 'u1'").fetchone()
    assert token and token == row['session_token']
    assert 'session_token' not in client.get('/api/me').get_json()['user']


# --- Migration ---------------------------------------------------------------------------

def _legacy_database(path):
    conn = database.connect(path)
    other_tables = database.SCHEMA[database.SCHEMA.index('CREATE TABLE IF NOT EXISTS activities'):]
    conn.executescript(LEGACY_USERS + other_tables)
    conn.execute("PRAGMA user_version = 2")
    conn.executemany("INSERT INTO users (sub, username, is_admin, is_active, last_login) VALUES (?, ?, ?, ?, ?)",
                     [('u1', 'alice', 1, 1, '2026-01-01T00:00:00Z'), ('u2', 'bob', 0, 0, None), ('u3', 'carol', 0, 1, None)])
    conn.execute("DELETE FROM users WHERE sub = 'u2'")  # trou dans les identifiants : 1 et 3 restent
    conn.execute("INSERT INTO activities (user_id, name, filename, sha256, source, start_time, points) "
                 "VALUES (3, 'Sortie', 'a.gpx', 'abc', 'upload', '2026-01-01T00:00:00Z', x'00')")
    conn.execute("INSERT INTO segments (user_id, name, distance_m, points) VALUES (3, 'Col', 1200, x'00')")
    conn.execute("INSERT INTO efforts (segment_id, activity_id, start_idx, end_idx, start_time, elapsed_s) "
                 "VALUES (1, 1, 0, 5, '2026-01-01T00:00:00Z', 60)")
    conn.execute("INSERT INTO activity_analyses (activity_id, version, computed_at, result) VALUES (1, 1, 'x', x'00')")
    conn.commit()
    assert 'AUTOINCREMENT' not in conn.execute("SELECT sql FROM sqlite_master WHERE name = 'users'").fetchone()[0]
    conn.close()


def test_users_table_is_rebuilt_without_losing_rows_or_foreign_keys(tmp_path):
    path = str(tmp_path / 'old.sqlite3')
    _legacy_database(path)

    database.init_db(path)
    database.init_db(path)  # idempotent
    conn = database.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(database.MIGRATIONS)
    rows = {r['id']: dict(r) for r in conn.execute("SELECT * FROM users")}
    assert sorted(rows) == [1, 3]
    assert (rows[1]['username'], rows[1]['is_admin'], rows[1]['last_login']) == ('alice', 1, '2026-01-01T00:00:00Z')
    assert rows[3]['username'] == 'carol' and rows[3]['session_token'] == ''
    # Les données restent rattachées à leur propriétaire
    counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ('activities', 'segments', 'efforts', 'activity_analyses')]
    assert counts == [1, 1, 1, 1]
    assert conn.execute("SELECT user_id FROM activities").fetchone()[0] == 3
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    # Les clés étrangères pointent bien sur la nouvelle table : la cascade fonctionne toujours
    conn.execute("DELETE FROM users WHERE id = 3")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM efforts").fetchone()[0] == 0
    # … et l'identifiant libéré n'est pas redonné
    new_id = conn.execute("INSERT INTO users (sub) VALUES ('u4')").lastrowid
    assert new_id == 4
    conn.close()


def test_fresh_database_never_reuses_an_account_id(tmp_path):
    path = str(tmp_path / 'new.sqlite3')
    database.init_db(path)
    conn = database.connect(path)
    first = conn.execute("INSERT INTO users (sub) VALUES ('a')").lastrowid
    conn.execute("DELETE FROM users WHERE id = ?", (first,))
    assert conn.execute("INSERT INTO users (sub) VALUES ('b')").lastrowid == first + 1
    conn.close()
