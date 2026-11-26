"""
Configuration des tests pytest.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app
from app.config import TestingConfig
from app.db import connect

OIDC_OVERRIDES = {
    'OIDC_ISSUER': 'https://idp.test', 'OIDC_CLIENT_ID': 'kmlviewer', 'OIDC_CLIENT_SECRET': 'secret',
}


def make_gpx(points, start=datetime(2025, 6, 1, 8, 0, tzinfo=timezone.utc), step_s=10,
             name='Sortie test', activity_type='running', with_time=True):
    """GPX minimal à partir de (lat, lon, ele)."""
    rows = []
    for i, (lat, lon, ele) in enumerate(points):
        stamp = (start + timedelta(seconds=i * step_s)).strftime('%Y-%m-%dT%H:%M:%SZ')
        time_tag = f"<time>{stamp}</time>" if with_time else ''
        rows.append(f"<trkpt lat='{lat:.6f}' lon='{lon:.6f}'><ele>{ele}</ele>{time_tag}</trkpt>")
    return ("<?xml version='1.0' encoding='UTF-8'?><gpx xmlns='http://www.topografix.com/GPX/1/1'>"
            f"<trk><name>{name}</name><type>{activity_type}</type><trkseg>{''.join(rows)}</trkseg></trk></gpx>")


def line(n, lat0=45.0, lon0=6.0, dlat=0.0003, dlon=0.0, ele=lambda i: 100.0):
    """n points alignés ; dlat=0.0003 ≈ 33 m entre deux points."""
    return [(lat0 + i * dlat, lon0 + i * dlon, ele(i)) for i in range(n)]


@pytest.fixture
def app(tmp_path):
    """Application de test en mode mono-utilisateur local, données isolées."""
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data')})
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    """Fixture pour créer un client de test."""
    return app.test_client()


@pytest.fixture
def runner(app):
    """Fixture pour créer un runner CLI de test."""
    return app.test_cli_runner()


@pytest.fixture
def db(app):
    conn = connect(app.config['DB_PATH'])
    yield conn
    conn.close()


@pytest.fixture
def local_user_id(client, db):
    client.get('/api/me')  # provisionne le compte local
    return db.execute("SELECT id FROM users WHERE sub = 'local'").fetchone()[0]


@pytest.fixture
def oidc_app(tmp_path):
    """Application avec OIDC activé (aucun appel réseau tant qu'on ne lance pas le flux)."""
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), **OIDC_OVERRIDES})
    with app.app_context():
        yield app


def login_as(app, sub, admin=False, **claims):
    """
    Crée (ou retrouve) un utilisateur et renvoie un client porteur de sa session.
    admin=True : lui donne le rôle admin, comme le ferait `flask user-admin`.
    """
    from app.auth import users
    conn = connect(app.config['DB_PATH'])
    try:
        user = users.upsert_from_claims(conn, {'sub': sub, 'preferred_username': sub, **claims})
        if admin:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user['id'],))
            conn.commit()
        token = users.session_token(conn, user['id'])
    finally:
        conn.close()
    client = app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = user['id']
        session['token'] = token
        session['login_at'] = int(time.time())
    return client, user['id']


@pytest.fixture(autouse=True)
def _frozen_totp_clock(monkeypatch):
    """Serveur et tests lisent le même instant TOTP, milieu d'un pas de 30 s : plus de frontière de pas entre un code émis et sa
    vérification ou son rejeu. Les tests qui pilotent leur propre horloge (FakeTime) la posent par-dessus."""
    from app.auth import second_factor
    from tests.pw_harness import TOTP_CLOCK
    TOTP_CLOCK.now = TOTP_CLOCK.mid_step()
    monkeypatch.setattr(second_factor, 'time', TOTP_CLOCK)
    yield


@pytest.fixture(autouse=True)
def _fresh_outbound_refusal_log():
    """`outbound._refusals` (dédoublonnage des refus audités) est global au processus : sans remise à zéro, un test dépend de l'ordre."""
    from app import outbound
    outbound._refusals = outbound.RefusalLog()
    yield


def ensure_column(conn, table, column, definition):
    """ALTER TABLE ADD COLUMN tolérant : la colonne peut déjà exister (migration 0015 pour le second facteur)."""
    if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def force_setting(conn, store, key, value):
    """Écrit un réglage DIRECTEMENT en base (contourne les contrôles d'écriture), pour tester l'état qu'ils interdisent."""
    import json
    conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES (?, ?, 'x', 'sql') "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, json.dumps(value)))
    conn.execute("UPDATE settings_meta SET version = version + 1")
    conn.commit()
    store.refresh(conn, force=True)
