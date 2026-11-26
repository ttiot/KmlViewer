"""Outillage commun des tests du mode `password` (rôle test) : application, comptes, TOTP réel, sessions injectées, appels d'API."""

import re
import tempfile
import threading
import time
from pathlib import Path

import pyotp
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.auth import passwords
from app.config import TestingConfig

HOST = 'traces.example.com'
ORIGIN = f'https://{HOST}'
KEY = Fernet.generate_key().decode()
GOOD = 'cheval-agrafe-lune-42'
OTHER = 'un-autre-mot-de-passe-9'
HASH = passwords.hash_password(GOOD)
KW = {'base_url': ORIGIN}


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


# Horloge FIGÉE des limiteurs : à l'intérieur d'une fenêtre de 15 min (indice 4001,5) ET d'une heure (indice 1000,375), loin de tout bord
# de fenêtre. Avec l'horloge réelle, une séquence de requêtes qui chevauche un bord de fenêtre (fenêtre à deux compteurs) donne des verdicts
# différents d'un identifiant à l'autre : un échec sur ~5 000 passages (T-flaky). Les tests de comptage doivent dépendre des seuls
# comptes, jamais de l'heure ; la glissade de fenêtre a ses propres tests, à horloge injectée (tests/test_ratelimit*.py).
FROZEN_NOW = 3_601_350.0
FIXED_BUCKET_SECRET = b'test-secret-fixed-for-bucket-slots'


def freeze_limiters(app, now=FROZEN_NOW):
    """Horloge constante et clé de compartiments fixe sur chaque limiteur de l'application (état par application, jamais partagé)."""
    limiters = app.extensions['limiters']
    for limiter in vars(limiters).values():
        if hasattr(limiter, '_clock') and hasattr(limiter, 'hit'):
            limiter._clock = lambda value=now: value
            if hasattr(limiter, '_secret'):
                limiter._secret = FIXED_BUCKET_SECRET
    return app


def make_app(tmp_path, **extra):
    app = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                      'SECRETS_KEY': KEY, 'PUBLIC_URL': ORIGIN, **extra})
    return freeze_limiters(app)


def connect(app):
    return database.connect(app.config['DB_PATH'])


def new_secret():
    return pyotp.random_base32(32)


class TotpClock:
    """Horloge TOTP des tests, figée au milieu d'un pas de 30 s (posée par la fixture autouse `_frozen_totp_clock` de conftest).

    Le serveur (`second_factor.time`) et les tests lisent le même instant : une frontière de pas ne peut plus passer entre
    l'émission d'un code et sa vérification (ou son rejeu), ce qui rendait ces tests aléatoires (~1 fois sur 15 s de décalage).
    """

    def __init__(self):
        self.now = self.mid_step()

    @staticmethod
    def mid_step():
        return time.time() // 30 * 30 + 15

    def time(self):
        return self.now


TOTP_CLOCK = TotpClock()


def code_at(secret, moment=None, offset=0):
    """Code TOTP valide à `moment` (par défaut l'horloge TOTP figée des tests) décalé de `offset` pas de 30 s."""
    moment = TOTP_CLOCK.now if moment is None else moment
    return pyotp.TOTP(secret).at(int(moment // 30 + offset) * 30)


def add(app, conn, name, *, admin=False, active=True, email=None, pending_email=None, method='password', password=HASH, secret=None,
        token=None, credits=0):
    """Compte de test. `secret` : secret TOTP en clair, chiffré comme l'application (facteur actif)."""
    token = token or f'tok-{name}'
    conn.execute("INSERT INTO users (sub, username, email, pending_email, is_admin, is_active, auth_method, password_hash, session_token, "
                 "invite_credits) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (f'pwd:{name}' if method == 'password' else f'sub:{name}', name, email, pending_email, int(admin), int(active), method,
                  password if method == 'password' else None, token, credits))
    if secret:
        store = app.extensions['secrets']
        conn.execute("UPDATE users SET totp_secret = ?, totp_enabled_at = '2026-01-01T00:00:00Z', totp_last_step = NULL WHERE username = ?",
                     (store.encrypt_value(secret.encode('ascii')), name))
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


def refresh(conn, name):
    return conn.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()


_ips = iter(f'203.0.{a}.{b}' for a in range(1, 250) for b in range(1, 255))


def new_ip():
    return next(_ips)


def session_client(app, row):
    """Client avec une session COMPLÈTE valide pour `row` (cookie signé par l'application)."""
    client = app.test_client()
    serializer = app.session_interface.get_signing_serializer(app)
    client.set_cookie('session', serializer.dumps({'user_id': row['id'], 'token': row['session_token'], 'login_at': int(time.time()),
                                                     '_permanent': True}), domain=HOST)
    return client


def cookie_payload(app, client):
    cookie = client.get_cookie('session', domain=HOST) or client.get_cookie('session')
    if cookie is None:
        return {}
    return dict(app.session_interface.get_signing_serializer(app).loads(cookie.value))


def raw_cookie(client):
    cookie = client.get_cookie('session', domain=HOST) or client.get_cookie('session')
    return cookie.value if cookie else None


def call(client, method, path, payload=None, *, origin=ORIGIN, ip=None, headers=None, raw=None, content_type='application/json'):
    hdrs = dict(headers or {})
    if origin:
        hdrs['Origin'] = origin
    kwargs = {'data': raw, 'content_type': content_type} if raw is not None else ({'json': payload} if payload is not None else {})
    return getattr(client, method)(path, headers=hdrs, environ_overrides={'REMOTE_ADDR': ip or new_ip()}, **KW, **kwargs)


_csrf = {}


def csrf_token(app):
    if 'v' not in _csrf:
        pattern = r'name="csrf-token" content="([^"]+)"'
        found = re.search(pattern, app.test_client().get('/auth/password/login', **KW).get_data(as_text=True))
        if found is None:                      # appli sans comptes à mot de passe (page 404) : jeton émis par une appli qui en a
            with tempfile.TemporaryDirectory() as tmp:
                found = re.search(pattern, make_app(Path(tmp)).test_client().get('/auth/password/login', **KW).get_data(as_text=True))
        _csrf['v'] = found.group(1)           # le jeton est signé avec la clé fixe du harnais : valable pour toute appli du harnais
    return _csrf['v']


def form_post(app, path, payload, *, client=None, ip=None, origin=ORIGIN, token=True, raw=None, content_type='application/json'):
    """POST d'un formulaire PUBLIC (jeton CSRF + origine) ; `client` conserve le cookie entre les étapes."""
    client = client or app.test_client()
    headers = {}
    if token:
        headers['X-CSRF-Token'] = token if isinstance(token, str) else csrf_token(app)
    if origin:
        headers['Origin'] = origin
    kwargs = {'data': raw, 'content_type': content_type} if raw is not None else {'json': payload}
    return client.post(path, headers=headers, environ_overrides={'REMOTE_ADDR': ip or new_ip()}, **KW, **kwargs)


def login(app, username, password=GOOD, *, client=None, ip=None, **extra):
    return form_post(app, '/auth/password/login', {'username': username, 'password': password, **extra}, client=client, ip=ip)


def mfa(app, client, code, *, ip=None, **kw):
    return form_post(app, '/auth/password/mfa', {'code': code}, client=client, ip=ip, **kw)


def concurrent(n, job):
    results, barrier = [None] * n, threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            results[i] = job(i)
        except Exception as exc:  # noqa: BLE001
            results[i] = exc
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    return results


class FakeTime:
    """Remplace le module `time` d'un module de l'application (seulement `time()`) : horloge murale pilotée."""

    def __init__(self, now=None):
        self.now = TOTP_CLOCK.now if now is None else now

    def time(self):
        return self.now
