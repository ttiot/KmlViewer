"""Configuration refusée au démarrage, défauts sûrs des comptes et de la session."""

import logging

import pytest

from app import _start_scheduler, create_app
from app.auth import oidc, users
from app.config import Config, TestingConfig
from app import background
from tests.conftest import OIDC_OVERRIDES, login_as


class Prod(TestingConfig):
    """Comme en production : les contrôles de démarrage ne sont pas allégés."""
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def _app(tmp_path, config=Prod, **overrides):
    return create_app(config, overrides={'DATA_DIR': str(tmp_path / 'data'), **overrides})


PROD_OIDC = {**OIDC_OVERRIDES, 'TRUSTED_HOSTS': ['traces.example.com']}


# --- Mode d'authentification -------------------------------------------------------------

def test_bare_configuration_starts_in_local_mode(tmp_path):
    """Sans aucune variable (poste de travail, essai de l'image) : mode local, limité à localhost."""
    app = _app(tmp_path)
    assert app.config['AUTH_MODE'] == 'local' and app.config['SESSION_COOKIE_SECURE'] is False
    assert app.test_client().get('/api/activities').status_code == 200
    assert app.test_client().get('/api/activities', headers={'Host': 'traces.example.com'}).status_code == 400


@pytest.mark.parametrize('hint', [
    {'TRUSTED_HOSTS': ['traces.example.com']}, {'TRUST_PROXY': 1}, {'SESSION_COOKIE_SECURE': True},
    {'OIDC_CLIENT_ID': 'kmlviewer'}, {'OIDC_CLIENT_SECRET': 'secret'},
])
def test_production_looking_config_without_oidc_refuses_to_start(tmp_path, hint):
    """OIDC_ISSUER perdu (faute de frappe, .env non chargé à moitié) : pas d'instance ouverte à tous."""
    with pytest.raises(RuntimeError, match='OIDC_ISSUER'):
        _app(tmp_path, **hint)
    # Le mode local reste possible sous un autre nom d'hôte, mais il faut le demander
    app = _app(tmp_path, AUTH_MODE='local', **hint)
    assert app.config['AUTH_MODE'] == 'local'


def test_auth_mode_must_agree_with_the_oidc_settings(tmp_path):
    with pytest.raises(RuntimeError, match='AUTH_MODE=oidc'):
        _app(tmp_path, AUTH_MODE='oidc')
    with pytest.raises(RuntimeError, match='AUTH_MODE=local'):
        _app(tmp_path, AUTH_MODE='local', **PROD_OIDC)
    with pytest.raises(RuntimeError, match='AUTH_MODE invalide'):
        _app(tmp_path, AUTH_MODE='none')
    assert _app(tmp_path, AUTH_MODE='oidc', **PROD_OIDC).config['AUTH_MODE'] == 'oidc'


def test_oidc_requires_trusted_hosts(tmp_path):
    with pytest.raises(RuntimeError, match='TRUSTED_HOSTS'):
        _app(tmp_path, **OIDC_OVERRIDES)
    client = _app(tmp_path, **PROD_OIDC).test_client()
    assert client.get('/dashboard', headers={'Host': 'evil.io'}).status_code == 400
    assert client.get('/dashboard', headers={'Host': 'traces.example.com'}).status_code == 302


# --- Cookie de session ---------------------------------------------------------------------

def test_session_cookie_is_secure_by_default_with_oidc(tmp_path, monkeypatch, caplog):
    app = _app(tmp_path, **PROD_OIDC)
    assert app.config['SESSION_COOKIE_SECURE'] is True
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token', lambda: {'userinfo': {'sub': 'u1'}})
    response = app.test_client().get('/auth/callback?code=x&state=y', base_url='https://traces.example.com')
    assert response.status_code == 302 and 'Secure' in response.headers['Set-Cookie']

    # Dérogation : elle doit être demandée, et elle se voit dans les logs
    with caplog.at_level(logging.WARNING, logger='app.auth.checks'):
        assert _app(tmp_path, SESSION_COOKIE_SECURE=False, **PROD_OIDC).config['SESSION_COOKIE_SECURE'] is False
    assert any('SESSION_COOKIE_SECURE=0' in record.getMessage() for record in caplog.records)


# --- Comptes : rien n'est accordé par défaut ---------------------------------------------------

def test_new_accounts_wait_for_activation_by_default(tmp_path, monkeypatch):
    assert Config.OIDC_AUTO_ACTIVATE is False  # défaut de l'application, sans variable d'environnement
    app = _app(tmp_path, OIDC_AUTO_ACTIVATE=Config.OIDC_AUTO_ACTIVATE, **PROD_OIDC)
    monkeypatch.setattr(oidc.oauth.oidc, 'authorize_access_token',
                        lambda: {'userinfo': {'sub': 'first', 'preferred_username': 'first'}})
    client = app.test_client()
    client.get('/auth/callback?code=x&state=y', base_url='https://traces.example.com')
    # Premier compte de l'instance : ni admin ni actif tant que le propriétaire ne l'a pas désigné
    assert client.get('/api/me', base_url='https://traces.example.com').status_code == 403
    assert client.get('/api/users', base_url='https://traces.example.com').status_code == 403

    result = app.test_cli_runner().invoke(args=['user-admin', 'first'])
    assert result.exit_code == 0
    me = client.get('/api/me', base_url='https://traces.example.com').get_json()['user']
    assert me['is_admin'] and me['is_active']


def test_revoking_admin_does_not_reactivate_an_account(oidc_app, db):
    admin, _ = login_as(oidc_app, 'admin', admin=True)
    _, zoe_id = login_as(oidc_app, 'zoe', admin=True)
    assert admin.patch(f'/api/users/{zoe_id}', json={'is_active': False}).status_code == 200
    assert oidc_app.test_cli_runner().invoke(args=['user-admin', 'zoe', '--revoke']).exit_code == 0
    row = db.execute("SELECT is_admin, is_active FROM users WHERE id = ?", (zoe_id,)).fetchone()
    assert (row['is_admin'], row['is_active']) == (0, 0)
    assert login_as(oidc_app, 'zoe')[0].get('/api/me').status_code == 403


def test_email_is_kept_only_when_explicitly_verified(db):
    for sub, claims in (('a', {}), ('b', {'email_verified': False}), ('c', {'email_verified': 'true'})):
        user = users.upsert_from_claims(db, {'sub': sub, 'email': f'{sub}@corp.io', **claims})
        assert user['email'] is None and user['username'] == sub
    assert users.find_by_login(db, 'a@corp.io') is None
    assert users.upsert_from_claims(db, {'sub': 'd', 'email': 'd@corp.io', 'email_verified': True})['email'] == 'd@corp.io'


# --- Connexions entamées ---------------------------------------------------------------------

def test_abandoned_logins_do_not_pile_up_in_the_session_cookie(oidc_app, monkeypatch):
    metadata = {'issuer': 'https://idp.test', 'authorization_endpoint': 'https://idp.test/oauth2/authorize',
                'token_endpoint': 'https://idp.test/oauth2/token', 'jwks_uri': 'https://idp.test/jwks'}
    monkeypatch.setattr(oidc.oauth.oidc, 'load_server_metadata', lambda: metadata)
    client = oidc_app.test_client()
    sizes = []
    for _ in range(25):
        response = client.get('/auth/login?next=/dashboard')
        assert response.status_code == 302
        sizes.append(len(response.headers['Set-Cookie']))
        with client.session_transaction() as session:
            pending = [key for key in session if key.startswith(oidc.STATE_PREFIX)]
        assert len(pending) <= oidc.MAX_PENDING_LOGINS
    assert max(sizes) < 2000  # loin des 4 ko au-delà desquels le navigateur refuse le cookie

    # La dernière demande reste utilisable, une demande périmée disparaît
    assert response.headers['Location'].split('state=')[1].split('&')[0] in ''.join(pending)
    with client.session_transaction() as session:
        for key in pending:
            session[key] = {**session[key], 'exp': 1}
    client.get('/auth/login')
    with client.session_transaction() as session:
        assert len([key for key in session if key.startswith(oidc.STATE_PREFIX)]) == 1


# --- En-têtes ----------------------------------------------------------------------------------

def test_csp_restricts_form_targets(client, oidc_app):
    assert "form-action 'self'" in client.get('/').headers['Content-Security-Policy'].split('; ')
    # Avec l'OIDC, la déconnexion (formulaire) est redirigée vers l'IdP : son origine seule s'ajoute
    csp = oidc_app.test_client().get('/auth/logged-out').headers['Content-Security-Policy']
    assert "form-action 'self' https://idp.test" in csp.split('; ')


# --- Tâche de fond ---------------------------------------------------------------------------

def test_watcher_starts_under_gunicorn_even_in_development_config(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(background.Scheduler, 'start', lambda self: started.append(self))
    monkeypatch.delenv('WERKZEUG_RUN_MAIN', raising=False)
    monkeypatch.delenv('FLASK_RUN_FROM_CLI', raising=False)
    app = _app(tmp_path)
    started.clear()                       # create_app a lui-même démarré l'ordonnanceur (mode servi : toujours, EF-10.1)
    app.config.update(TESTING=False, INBOX_ENABLED=True)
    app.debug = True

    monkeypatch.delenv('SERVER_SOFTWARE', raising=False)
    _start_scheduler(app)
    assert started == []  # parent du reloader du serveur de développement : rien

    monkeypatch.setenv('SERVER_SOFTWARE', 'gunicorn/23.0.0')
    _start_scheduler(app)
    assert len(started) == 1


@pytest.mark.parametrize('host', ['localhost:5000', '127.0.0.1:5000', '172.17.0.2:5000', 'evil.example'])
def test_health_probe_answers_whatever_the_host(tmp_path, host):
    """
    Le HEALTHCHECK de l'image interroge localhost:5000, un répartiteur de charge l'adresse
    du conteneur : avec TRUSTED_HOSTS (obligatoire en OIDC), la sonde était refusée (400)
    et le conteneur restait « unhealthy ». Elle répond sous n'importe quel nom ; tout le
    reste reste refusé.
    """
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), **PROD_OIDC})
    client = app.test_client()
    health = client.get('/api/health', headers={'Host': host})
    assert health.status_code == 200 and health.get_json()['status'] == 'healthy'
    assert client.head('/api/health', headers={'Host': host}).status_code == 200
    # Rien d'autre ne passe sous ce nom, pas même une autre route publique ou une écriture sur la sonde
    for method, path in (('GET', '/'), ('GET', '/api/activities'), ('GET', '/auth/login'),
                         ('GET', '/api/health/'), ('GET', '/api/healthz'), ('POST', '/api/health')):
        assert client.open(path, method=method, headers={'Host': host}).status_code == 400, (method, path)


def test_health_probe_still_answers_under_the_public_name(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), **PROD_OIDC})
    response = app.test_client().get('/api/health', headers={'Host': 'traces.example.com'})
    assert response.status_code == 200 and response.headers['Cache-Control'] == 'no-store'

