"""Doc-2 (annonce du dernier admin avant d'agir), Doc-4 (avertissement SMTP absent), Doc-6 (sessions password en mode oidc sans comptes)."""

import logging
import time

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.auth import modes
from app.config import TestingConfig
from tests.conftest import OIDC_OVERRIDES

HOST = 'traces.example.com'
KEY = Fernet.generate_key().decode()


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def password_app(tmp_path, **extra):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}', **extra})


def add(app, name, *, admin=False, method='password', active=True):
    conn = database.connect(app.config['DB_PATH'])
    conn.execute("INSERT INTO users (sub, username, is_admin, is_active, auth_method, session_token) VALUES (?, ?, ?, ?, ?, 'tok')",
                 (f'pwd:{name}', name, int(admin), int(active), method))
    conn.commit()
    uid = conn.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()['id']
    conn.close()
    return uid


# --- Doc-2 -----------------------------------------------------------------------------------------------------------------

def test_mfa_reset_announces_the_last_admin_before_acting(tmp_path):
    app = password_app(tmp_path)
    add(app, 'seul', admin=True)
    out = app.test_cli_runner().invoke(args=['user-mfa-reset', 'seul']).output
    assert 'DERNIER administrateur' in out and 'user-invite --admin' in out
    assert out.index('DERNIER') < out.index('retirés')                       # l'annonce précède le compte rendu de l'action


def test_mfa_reset_does_not_announce_when_another_admin_remains_or_the_target_is_not_admin(tmp_path):
    app = password_app(tmp_path)
    add(app, 'a', admin=True)
    add(app, 'b', admin=True)
    add(app, 'simple')
    runner = app.test_cli_runner()
    assert 'DERNIER' not in runner.invoke(args=['user-mfa-reset', 'a']).output
    assert 'DERNIER' not in runner.invoke(args=['user-mfa-reset', 'simple']).output


# --- Doc-4 -----------------------------------------------------------------------------------------------------------------

def test_password_mode_without_smtp_warns_at_startup_and_with_smtp_does_not(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        app = password_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='smtp=smtp.example.org:587')
    assert any('SMTP non configuré' in r.getMessage() for r in caplog.records)
    conn = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(conn, {'smtp.host': 'smtp.example.org', 'smtp.sender': 'noreply@example.org'},
                                          actor=Actor.cli())
    conn.close()
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        password_app(tmp_path, OUTBOUND_ALLOWED_HOSTS='smtp=smtp.example.org:587')
    assert not any('SMTP non configuré' in r.getMessage() for r in caplog.records)


def test_oidc_mode_does_not_warn_about_smtp(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'o'), **OIDC_OVERRIDES})
    assert not any('SMTP non configuré' in r.getMessage() for r in caplog.records)


# --- Doc-6 -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('accounts,expected', [(True, True), (False, False)])
def test_in_oidc_mode_password_methods_follow_the_availability_of_password_accounts(accounts, expected):
    config = {'PASSWORD_ACCOUNTS': accounts}
    assert modes.method_permitted('oidc', 'password', config) is expected
    assert modes.method_permitted('oidc', 'oidc', config) is True
    assert modes.method_permitted('password', 'password', config) is True
    assert modes.method_permitted('password', 'oidc', config) is False
    assert modes.method_permitted('local', 'password', config) is False


def test_a_password_account_session_is_refused_when_password_accounts_are_disabled(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'o'), **OIDC_OVERRIDES})      # sans SECRETS_KEY ni PUBLIC_URL
    assert app.config['PASSWORD_ACCOUNTS'] is False
    uid = add(app, 'pwd')
    client = app.test_client()
    with client.session_transaction() as s:
        s['user_id'], s['token'], s['login_at'] = uid, 'tok', int(time.time())
    assert client.get('/api/activities').status_code in (401, 302)
