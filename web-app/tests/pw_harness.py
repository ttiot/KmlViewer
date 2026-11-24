"""Outillage commun des tests du mode `password` (rôle test) : application, comptes, TOTP réel, sessions injectées, appels d'API."""

import re
import threading
import time

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


def make_app(tmp_path, **extra):
    return create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'data'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                       'SECRETS_KEY': KEY, 'PUBLIC_URL': ORIGIN, **extra})


def connect(app):
    return database.connect(app.config['DB_PATH'])


def new_secret():
    return pyotp.random_base32(32)


def code_at(secret, moment=None, offset=0):
    """Code TOTP valide à `moment` décalé de `offset` pas de 30 s."""
    moment = time.time() if moment is None else moment
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
        html = app.test_client().get('/auth/password/login', **KW).get_data(as_text=True)
        _csrf['v'] = re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)
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
        self.now = time.time() if now is None else now

    def time(self):
        return self.now
