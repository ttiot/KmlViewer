"""
Cas limites de l'envoi d'e-mail (T9 : EF-4.1, EF-4.2, EF-4.7 à EF-4.9, EF-1.9, EF-5.5 ; CA-34, CA-35, CA-37, CA-49).

STARTTLS RÉUSSI avec un certificat auto-signé de test (puis tous les cas où il doit échouer sans jamais rien envoyer en
clair), injection d'en-têtes et de commandes, effacement du mot de passe au changement d'hôte OU de port, mot de passe
jamais envoyé ailleurs, aucune fuite du lien / jeton / mot de passe, file bornée et son comportement à saturation, durée
constante du test d'envoi. Complète test_mailer.py.
"""

import base64
import datetime
import email as email_lib
import email.policy
import logging
import socketserver
import ssl
import threading
import time
import traceback

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from app import create_app, outbound
from app import db as database
from app import mailer as mailer_module
from app.audit import Actor
from app.config import TestingConfig
from app.mailer import MailError, MailQueue, build_message, compose, validate_address
from app.settings import SettingsError
from tests.fake_smtp import FakeSMTP, closed_port

KEY = Fernet.generate_key().decode()
HOST = 'traces.example.com'
CLI = Actor.cli()
PASSWORD = 'mot-de-passe-smtp-S3CRET-42'


class Prod(TestingConfig):
    TESTING = False
    INBOX_ENABLED = False
    SECRET_KEY = 'k' * 40
    SESSION_COOKIE_SECURE = None


def make_app(tmp_path, allowed, name='data'):
    return create_app(Prod, overrides={
        'DATA_DIR': str(tmp_path / name), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY,
        'PUBLIC_URL': f'https://{HOST}', 'OUTBOUND_ALLOWED_HOSTS': ','.join(f'smtp=127.0.0.1:{p}' for p in allowed)})


def configure(app, port, security='none', timeout=2, **more):
    conn = database.connect(app.config['DB_PATH'])
    app.config['SETTINGS_STORE'].set_many(conn, {'smtp.host': '127.0.0.1', 'smtp.port': port, 'smtp.security': security,
                                                 'smtp.sender': 'noreply@example.org', 'smtp.timeout_s': timeout, **more}, actor=CLI)
    conn.close()


def store_password(app, port, value=PASSWORD):
    conn = database.connect(app.config['DB_PATH'])
    app.extensions['secrets'].set(conn, 'smtp.password', value, outbound.destination_of_smtp('127.0.0.1', port), actor=CLI)
    conn.close()


def db_of(app):
    return database.connect(app.config['DB_PATH'])


# ====================================================== serveur SMTP avec TLS réel ===========================================

def make_certificate(tmp_path, *, name='srv', ips=('127.0.0.1',), dns=('localhost',), expired=False):
    """Certificat AUTO-SIGNÉ de test (il est sa propre ancre de confiance), avec les extensions qu'exige la vérification stricte."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'smtp-test')])
    now = datetime.datetime.now(datetime.timezone.utc)
    start, end = (now - datetime.timedelta(days=30), now - datetime.timedelta(days=1)) if expired else (now - datetime.timedelta(days=1), now + datetime.timedelta(days=2))
    import ipaddress
    names = [x509.DNSName(d) for d in dns] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number()).not_valid_before(start).not_valid_after(end)
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, content_commitment=False, key_encipherment=False,
                                            data_encipherment=False, key_agreement=False, crl_sign=False, encipher_only=False,
                                            decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
               .add_extension(x509.SubjectAlternativeName(names), critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), critical=False))
    cert = builder.sign(key, hashes.SHA256())
    cert_path, key_path = tmp_path / f'{name}.crt', tmp_path / f'{name}.key'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cert_path), str(key_path)


def _readline(sock):
    data = b''
    while not data.endswith(b'\n'):
        chunk = sock.recv(1)
        if not chunk:
            return None
        data += chunk
    return data.decode('utf-8', 'replace').rstrip('\r\n')


class _TlsHandler(socketserver.BaseRequestHandler):
    def handle(self):
        server, sock, phase = self.server, self.request, 'plain'
        sock.settimeout(10)
        try:
            if server.mode == 'implicit':
                sock = server.context.wrap_socket(sock, server_side=True)
                phase = 'tls'
            sock.sendall(b"220 tls-fake ESMTP\r\n")
            data_mode, lines = False, []
            while True:
                line = _readline(sock)
                if line is None:
                    return
                if data_mode:
                    if line == '.':
                        server.messages.append((phase, '\n'.join(lines)))
                        data_mode, lines = False, []
                        sock.sendall(b"250 queued\r\n")
                    else:
                        lines.append(line[1:] if line.startswith('..') else line)
                    continue
                server.log.append((phase, line))
                verb = line.split(' ', 1)[0].upper()
                if verb in ('EHLO', 'HELO'):
                    advertise = server.mode in ('starttls', 'fake_upgrade') and phase == 'plain'
                    sock.sendall(b"250-tls-fake\r\n" + (b"250-STARTTLS\r\n" if advertise else b"") + b"250 AUTH PLAIN\r\n")
                elif verb == 'STARTTLS':
                    if server.mode == 'fake_upgrade':
                        sock.sendall(b"220 Ready to start TLS\r\n")          # promet, ne fait rien : le client devra échouer
                        continue
                    sock.sendall(b"220 Ready to start TLS\r\n")
                    sock = server.context.wrap_socket(sock, server_side=True)
                    phase = 'tls'
                elif verb == 'AUTH':
                    sock.sendall(b"235 ok\r\n")
                elif verb in ('MAIL', 'RCPT', 'NOOP', 'RSET'):
                    sock.sendall(b"250 ok\r\n")
                elif verb == 'DATA':
                    data_mode = True
                    sock.sendall(b"354 go\r\n")
                elif verb == 'QUIT':
                    sock.sendall(b"221 bye\r\n")
                    return
                else:
                    sock.sendall(b"250 ok\r\n")
        except (ssl.SSLError, OSError):
            server.failures += 1


class TlsSMTP(socketserver.ThreadingTCPServer):
    """Modes : `starttls` (annonce et exécute), `strip` (n'annonce pas STARTTLS), `fake_upgrade` (promet et ne fait rien), `implicit`."""
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cert, key, mode='starttls'):
        super().__init__(('127.0.0.1', 0), _TlsHandler)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.mode, self.log, self.messages, self.failures = mode, [], [], 0
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def verbs(self, phase):
        return [line.split(' ', 1)[0].upper() for p, line in self.log if p == phase]

    def clear_sensitive(self):
        return [line for p, line in self.log if p == 'plain' and line.split(' ', 1)[0].upper() in ('AUTH', 'MAIL', 'RCPT', 'DATA')]

    def stop(self):
        self.shutdown()
        self.server_close()


@pytest.fixture
def trust(monkeypatch):
    """Fait faire confiance au certificat de test (et à lui seul) : la vérification reste complète (chaîne, nom, validité)."""
    real = ssl.create_default_context

    def apply(cert_path):
        monkeypatch.setattr(mailer_module.ssl, 'create_default_context', lambda *a, **k: real(cafile=cert_path))
    return apply


def tls_app(tmp_path, server, security='starttls', username='smtpuser', password=PASSWORD, name='data'):
    app = make_app(tmp_path, [server.port], name)
    configure(app, server.port, security=security, **({'smtp.username': username} if username else {}))
    if username:
        store_password(app, server.port, password)
    return app


@pytest.fixture
def certs(tmp_path):
    return make_certificate(tmp_path)


@pytest.fixture
def starttls(certs):
    server = TlsSMTP(*certs, mode='starttls')
    yield server
    server.stop()


# --- STARTTLS réussi, certificat auto-signé de test --------------------------------------------------------------------

def test_a_successful_starttls_with_a_self_signed_certificate_delivers_and_nothing_sensitive_is_ever_clear(tmp_path, certs, starttls, trust):
    trust(certs[0])
    app = tls_app(tmp_path, starttls)
    app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps du message avec un lien https://traces.example.com/auth/token#t=ABC')
    assert starttls.verbs('plain') == ['EHLO', 'STARTTLS']                              # en clair : seulement l'annonce et la demande de TLS
    assert starttls.clear_sensitive() == [] and starttls.failures == 0
    tls = starttls.verbs('tls')
    assert tls[0] == 'EHLO' and tls[1:] == ['AUTH', 'MAIL', 'RCPT', 'DATA', 'QUIT'] or tls[1:4] == ['AUTH', 'MAIL', 'RCPT']
    assert [phase for phase, _ in starttls.messages] == ['tls']                          # le message n'a voyagé que chiffré
    raw = starttls.messages[0][1]
    assert 'To: dest@example.org' in raw and '[KmlViewer] Sujet' in raw


def test_the_smtp_password_travels_only_inside_tls_and_is_the_stored_one(tmp_path, certs, starttls, trust):
    trust(certs[0])
    app = tls_app(tmp_path, starttls)
    app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
    auth = [line for phase, line in starttls.log if line.upper().startswith('AUTH')]
    assert len(auth) == 1 and ('tls', auth[0]) in starttls.log
    token = auth[0].split()[-1]
    assert PASSWORD in base64.b64decode(token).decode() and 'smtpuser' in base64.b64decode(token).decode()
    assert not any(PASSWORD in line for phase, line in starttls.log if phase == 'plain')


def test_without_trust_in_the_certificate_the_send_fails_before_any_authentication_or_message(tmp_path, certs, starttls):
    app = tls_app(tmp_path, starttls)                                                   # contexte par défaut : le certificat auto-signé est refusé
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
    assert starttls.messages == [] and starttls.verbs('tls') == [] and starttls.clear_sensitive() == []
    assert set(starttls.verbs('plain')) <= {'EHLO', 'STARTTLS'}


@pytest.mark.parametrize('kind', ['wrong-name', 'expired'])
def test_a_certificate_for_another_name_or_expired_is_refused_even_if_trusted(tmp_path, trust, kind):
    cert, key = make_certificate(tmp_path, name=kind, ips=('203.0.113.7',) if kind == 'wrong-name' else ('127.0.0.1',),
                                 dns=('autre.example',) if kind == 'wrong-name' else ('localhost',), expired=kind == 'expired')
    server = TlsSMTP(cert, key)
    try:
        trust(cert)
        app = tls_app(tmp_path, server)
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert server.messages == [] and server.clear_sensitive() == [] and not [line for _p, line in server.log if line.upper().startswith('AUTH')]
    finally:
        server.stop()


def test_a_server_that_strips_starttls_gets_nothing_sensitive_and_the_send_fails(tmp_path, certs, trust):
    server = TlsSMTP(*certs, mode='strip')
    try:
        trust(certs[0])
        app = tls_app(tmp_path, server)
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert server.clear_sensitive() == [] and server.messages == [] and 'STARTTLS' not in server.verbs('plain')
    finally:
        server.stop()


def test_a_server_that_promises_tls_and_stays_in_clear_gets_nothing_sensitive(tmp_path, certs, trust):
    server = TlsSMTP(*certs, mode='fake_upgrade')
    try:
        trust(certs[0])
        app = tls_app(tmp_path, server)
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert server.clear_sensitive() == [] and server.messages == []
    finally:
        server.stop()


def test_a_downgrade_to_clear_never_leaks_the_password_even_with_a_username_configured(tmp_path, certs, trust):
    for mode in ('strip', 'fake_upgrade'):
        server = TlsSMTP(*certs, mode=mode)
        try:
            trust(certs[0])
            app = tls_app(tmp_path, server, name=f'd-{mode}')
            with pytest.raises(MailError):
                app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
            assert not any(PASSWORD in line or 'AUTH' in line.upper() for phase, line in server.log if phase == 'plain'), mode
        finally:
            server.stop()


def test_implicit_tls_succeeds_when_trusted_and_fails_without_trust(tmp_path, certs, trust):
    server = TlsSMTP(*certs, mode='implicit')
    try:
        app = tls_app(tmp_path, server, security='tls')
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert server.messages == []
        trust(certs[0])
        app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert [phase for phase, _ in server.messages] == ['tls'] and server.clear_sensitive() == []
    finally:
        server.stop()


def test_old_tls_versions_are_refused(tmp_path, certs, trust):
    server = TlsSMTP(*certs)
    try:
        try:
            server.context.maximum_version = ssl.TLSVersion.TLSv1_1
            server.context.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, ssl.SSLError):
            pytest.skip("OpenSSL local sans TLS 1.0/1.1 : le serveur ne peut pas les proposer")
        trust(certs[0])
        app = tls_app(tmp_path, server)
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert server.messages == []
    finally:
        server.stop()


def test_a_successful_send_after_a_failed_one_does_not_reuse_a_clear_connection(tmp_path, certs, starttls, trust):
    app = tls_app(tmp_path, starttls)
    with pytest.raises(MailError):
        app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')            # non fiable : échec
    trust(certs[0])
    app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
    assert starttls.clear_sensitive() == [] and len(starttls.messages) == 1


# --- Injection d'en-têtes et de commandes (EF-4.9) ---------------------------------------------------------------------

BAD_ADDRESSES = ['a@b.c\r\nBcc: evil@x.y', 'a@b.c\nBcc: evil@x.y', 'a@b.c\rBcc: e@x.y', 'a@b.c Bcc: e@x.y', 'a@b.c\u0085Bcc: e@x.y',
                 'a@b.c, d@e.f', 'a@b.c;d@e.f', '"a b"@c.d', '<a@b.c>', 'a@b.c>\r\nRCPT TO:<e@x.y', 'a b@c.d', 'a@b.c\x00',
                 'Nom <a@b.c>', 'a@b.c (commentaire)', '＜a＠b.c＞', 'a，b@c.d', 'a＼b@c.d', '', ' ', None, 5, b'a@b.c', ['a@b.c'], 'a' * 300 + '@b.c']


@pytest.mark.parametrize('address', BAD_ADDRESSES, ids=lambda a: repr(a)[:30])
def test_hostile_recipients_are_refused_before_any_connection(tmp_path, address):
    server = FakeSMTP()
    try:
        app = make_app(tmp_path, [server.port])
        configure(app, server.port)
        with pytest.raises(MailError):
            app.extensions['mailer'].send(address, 'Sujet', 'Corps')
        assert app.extensions['mailer'].enqueue(address, 'Sujet', 'Corps') == 'invalid'
        assert server.connections == 0
    finally:
        server.stop()


@pytest.mark.parametrize('field', ['smtp.sender', 'smtp.host', 'smtp.username'])
@pytest.mark.parametrize('value', ['a@b.c\r\nBcc: evil@x.y', 'x\ny', 'x\ry', 'x\x00y', 'x y'])
def test_hostile_smtp_settings_are_refused_at_write(tmp_path, field, value):
    server = FakeSMTP()
    try:
        app = make_app(tmp_path, [server.port])
        configure(app, server.port)
        conn = db_of(app)
        with pytest.raises(SettingsError):
            app.config['SETTINGS_STORE'].set_many(conn, {field: value}, actor=CLI)
        conn.close()
    finally:
        server.stop()


def _headers(raw):
    return email_lib.message_from_string(raw, policy=email.policy.default)


@pytest.mark.parametrize('payload', ['x\r\nBcc: evil@x.y', 'x\nBcc: evil@x.y', 'x\r\n\r\nCorps injecté', 'x Bcc: e@x.y', '=?utf-8?q?x?=\r\nBcc: e@x.y'])
@pytest.mark.parametrize('kind,field', [('invitation', 'inviter'), ('invitation', 'note'), ('verification', 'username'), ('notification', 'detail')])
def test_user_data_with_line_breaks_never_becomes_a_header(payload, kind, field):
    kwargs = {'link': 'https://traces.example.com/auth/token#t=ABC', 'inviter': 'alice', 'username': 'bob', 'note': None, 'detail': ''}
    kwargs[field] = payload
    subject, body = compose('password_changed' if kind == 'notification' else kind, **kwargs)
    message = build_message('noreply@example.org', 'dest@example.org', subject, body)
    parsed = _headers(message.as_string())
    assert sorted(k.lower() for k in parsed.keys()) == ['content-transfer-encoding', 'content-type', 'from', 'mime-version', 'subject', 'to']
    assert 'bcc' not in {k.lower() for k in parsed.keys()} and parsed['To'] == 'dest@example.org'
    assert '\n' not in parsed['Subject'] and parsed['Subject'].startswith('[KmlViewer] ')
    assert subject == compose('password_changed' if kind == 'notification' else kind, **{**kwargs, field: 'autre'})[0]      # sujet fixe


def test_the_wire_form_of_a_hostile_message_has_exactly_the_expected_headers():
    server = FakeSMTP()
    try:
        message = build_message('noreply@example.org', 'dest@example.org', 'Sujet', "Message de x\r\nBcc: evil@x.y\r\n\r\nFin")
        import smtplib
        with smtplib.SMTP('127.0.0.1', server.port, timeout=5) as client:
            client.send_message(message)
        header_block = server.messages[0].split('\n\n', 1)[0]
        assert 'Bcc' not in header_block and [ln.split(':')[0] for ln in header_block.splitlines() if ':' in ln and not ln.startswith(' ')]
        assert [c.upper() for c in server.commands if c.upper().startswith('RCPT')] == ['RCPT TO:<DEST@EXAMPLE.ORG>']
    finally:
        server.stop()


def test_a_message_body_line_starting_with_a_dot_cannot_end_the_data_phase_early():
    server = FakeSMTP()
    try:
        import smtplib
        message = build_message('noreply@example.org', 'dest@example.org', 'Sujet', "ligne\r\n.\r\nQUIT\r\n.RCPT TO:<evil@x.y>\r\nfin")
        with smtplib.SMTP('127.0.0.1', server.port, timeout=5) as client:
            client.send_message(message)
        assert 'RCPT TO:<evil@x.y>' not in server.commands and len(server.messages) == 1 and 'fin' in server.messages[0]
    finally:
        server.stop()


def test_an_address_with_two_at_signs_is_refused():
    for address in ('a@b＠c.d', 'a@b@c.d', 'a＠b＠c.d'):
        with pytest.raises(ValueError):
            validate_address(address)


def test_invisible_format_characters_are_not_accepted_in_an_address():
    for address in ('a​@b.c', 'a@b.c﻿', 'a‮@b.c', 'a⁠@b.c', 'a@b­.c'):
        with pytest.raises(ValueError):
            validate_address(address)


# --- Changement d'hôte OU de port : le mot de passe part avec (EF-5.5, CA-34) ---------------------------------------------

def secret_present(app):
    conn = db_of(app)
    try:
        return bool(conn.execute("SELECT 1 FROM secrets WHERE name = 'smtp.password'").fetchone())
    finally:
        conn.close()


def two_servers(tmp_path):
    a, b = FakeSMTP(), FakeSMTP()
    app = make_app(tmp_path, [a.port, b.port])
    configure(app, a.port, **{'smtp.username': 'smtpuser'}, security='starttls')
    store_password(app, a.port)
    return app, a, b


@pytest.mark.parametrize('change,clears', [
    ({'smtp.port': 'B'}, True),
    ({'smtp.security': 'tls'}, False), ({'smtp.username': 'autre'}, False), ({'smtp.sender': 'autre@example.org'}, False),
    ({'smtp.timeout_s': 5}, False), ({'smtp.host': '127.0.0.1'}, False),
], ids=lambda v: str(v)[:34])
def test_the_password_is_cleared_exactly_when_the_effective_destination_changes(tmp_path, change, clears):
    app, a, b = two_servers(tmp_path)
    try:
        conn = db_of(app)
        change = {k: (b.port if v == 'B' else v) for k, v in change.items()}
        app.config['SETTINGS_STORE'].set_many(conn, change, actor=CLI)
        conn.close()
        assert secret_present(app) is (not clears), change
    finally:
        a.stop()
        b.stop()


def test_a_host_only_change_to_another_allowed_name_clears_the_password(tmp_path):
    server = FakeSMTP()
    try:
        app = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 'd'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST], 'SECRETS_KEY': KEY,
                                          'PUBLIC_URL': f'https://{HOST}',
                                          'OUTBOUND_ALLOWED_HOSTS': f'smtp=127.0.0.1:{server.port},smtp=localhost:{server.port}'})
        configure(app, server.port, security='starttls', **{'smtp.username': 'smtpuser'})
        store_password(app, server.port)
        conn = db_of(app)
        app.config['SETTINGS_STORE'].set_many(conn, {'smtp.host': 'localhost'}, actor=CLI)
        conn.close()
        assert secret_present(app) is False
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert server.connections == 0
    finally:
        server.stop()


def test_a_port_only_change_clears_the_password_and_the_next_send_cannot_authenticate_anywhere(tmp_path):
    app, a, b = two_servers(tmp_path)
    try:
        conn = db_of(app)
        app.config['SETTINGS_STORE'].set_many(conn, {'smtp.port': b.port}, actor=CLI)
        conn.close()
        assert secret_present(app) is False
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps')
        assert a.connections == 0 and b.connections == 0 and not any('AUTH' in v for v in a.verbs() + b.verbs())
    finally:
        a.stop()
        b.stop()


def test_a_destination_forced_by_raw_sql_never_receives_the_password_bound_to_the_old_one(tmp_path):
    """Contournement de l'API : l'hôte bascule sur un AUTRE serveur autorisé sans passer par le store ; le secret reste lié à l'ancien."""
    app, a, b = two_servers(tmp_path)
    try:
        conn = db_of(app)
        conn.execute("UPDATE settings SET value = ? WHERE key = 'smtp.port'", (str(b.port),))
        conn.execute("UPDATE settings_meta SET version = version + 1")
        conn.commit()
        app.config['SETTINGS_STORE'].refresh(conn, force=True)
        assert secret_present(app) is True                                              # pas effacé (pas de passage par le store)
        with pytest.raises(MailError):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', 'Corps', conn=conn)
        assert b.connections == 0 and a.connections == 0                                # rien n'est envoyé à B, ni à A
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'secret.refused'").fetchone()[0] >= 1
        conn.close()
    finally:
        a.stop()
        b.stop()


def test_the_password_never_appears_in_the_other_servers_traffic_when_the_host_is_swapped_back_and_forth(tmp_path):
    app, a, b = two_servers(tmp_path)
    try:
        conn = db_of(app)
        store = app.config['SETTINGS_STORE']
        for port in (b.port, a.port, b.port):
            store.set_many(conn, {'smtp.port': port}, actor=CLI)
        conn.close()
        assert not secret_present(app)
        for server in (a, b):
            assert all(PASSWORD not in c for c in server.commands) and not [m for m in server.messages if PASSWORD in m]
    finally:
        a.stop()
        b.stop()


# --- Aucune fuite : lien, jeton, mot de passe (EF-4.7, EF-S5) -------------------------------------------------------------------

LINK_TOKEN = 'T0KEN-' + 'x' * 37


def test_no_link_token_or_password_in_logs_audit_exceptions_or_reprs_on_any_failure_path(tmp_path, caplog):
    dead = closed_port()
    app = make_app(tmp_path, [dead])
    configure(app, dead, security='starttls', **{'smtp.username': 'smtpuser'})
    store_password(app, dead)
    body = f"Lien : https://{HOST}/auth/token#t={LINK_TOKEN}\n"
    mailer = app.extensions['mailer']
    outputs = []
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MailError) as exc:
            mailer.send('dest@example.org', 'Sujet', body)
        outputs += [repr(exc.value), str(exc.value), ''.join(traceback.format_exception(exc.value))]
        assert exc.value.__cause__ is None
        mailer.enqueue('dest@example.org', 'Sujet', body)
        mailer.queue.drain()
        outputs += [repr(mailer), repr(mailer.queue), repr(vars(mailer.queue).keys())]
    conn = db_of(app)
    audit_dump = repr([dict(r) for r in conn.execute("SELECT * FROM audit_log")])
    conn.close()
    for text in (caplog.text, audit_dump, *outputs):
        assert LINK_TOKEN not in text and PASSWORD not in text
    for text in (caplog.text, *outputs):                                               # l'identifiant SMTP n'est pas secret (audité comme réglage) mais n'a rien à faire dans un journal d'envoi
        assert 'smtpuser' not in text


def test_the_composed_message_object_never_prints_its_body_in_a_log_line(tmp_path, caplog):
    server = FakeSMTP()
    try:
        app = make_app(tmp_path, [server.port])
        configure(app, server.port)
        body = f"Lien : https://{HOST}/auth/token#t={LINK_TOKEN}\n"
        with caplog.at_level(logging.DEBUG):
            app.extensions['mailer'].send('dest@example.org', 'Sujet', body)
            app.extensions['mailer'].enqueue('dest@example.org', 'Sujet', body)
            app.extensions['mailer'].queue.drain()
        assert LINK_TOKEN not in caplog.text and 'dest@example.org' not in caplog.text
        assert len(server.messages) == 2
    finally:
        server.stop()


def test_a_refused_destination_logs_the_service_only_and_never_the_host_or_the_link(tmp_path, caplog):
    server = FakeSMTP()
    try:
        app = make_app(tmp_path, [server.port + 1])                                    # le serveur réel n'est PAS dans la liste
        configure_conn = db_of(app)
        configure_conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('smtp.host', '\"127.0.0.1\"', 'x', 'sql'), "
                               "('smtp.port', ?, 'x', 'sql'), ('smtp.sender', '\"noreply@example.org\"', 'x', 'sql'), ('smtp.security', '\"none\"', 'x', 'sql')",
                               (str(server.port),))
        configure_conn.execute("UPDATE settings_meta SET version = version + 1")
        configure_conn.commit()
        app.config['SETTINGS_STORE'].refresh(configure_conn, force=True)
        configure_conn.close()
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(MailError):
                app.extensions['mailer'].send('dest@example.org', 'Sujet', f"https://{HOST}/auth/token#t={LINK_TOKEN}")
        assert server.connections == 0 and LINK_TOKEN not in caplog.text
        conn = db_of(app)
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'outbound.refused'").fetchone()[0] == 1
        conn.close()
    finally:
        server.stop()


# --- File d'envoi : bornée à 1000, comportement à saturation (EF-4.8) ---------------------------------------------------------

def _message(i=0):
    return build_message('noreply@example.org', 'dest@example.org', f'S{i}', f"Lien https://{HOST}/auth/token#t={LINK_TOKEN}")


def test_at_saturation_the_1001st_message_is_dropped_logged_and_never_blocks(caplog):
    queue = MailQueue(lambda m: None)
    started = time.monotonic()
    with caplog.at_level(logging.DEBUG):
        results = [queue.put(_message(i)) for i in range(1100)]
    assert results.count(True) == 1000 and results.count(False) == 100 and queue.dropped == 100 and queue.pending() == 1000
    assert time.monotonic() - started < 5
    assert LINK_TOKEN not in caplog.text and 'dest@example.org' not in caplog.text and caplog.text.count('File d\'envoi pleine') == 100
    assert queue.drain() == 1000 and queue.pending() == 0 and queue.put(_message()) is True       # vidée : elle accepte de nouveau


def test_concurrent_producers_never_exceed_the_bound_and_never_raise():
    queue = MailQueue(lambda m: None)
    results, errors = [], []

    def produce(n):
        try:
            for i in range(200):
                results.append(queue.put(_message(n * 1000 + i)))
        except Exception as exc:                                                        # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=produce, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors and results.count(True) == 1000 and results.count(False) == 600 and queue.pending() == 1000


def test_the_enqueue_status_reports_a_drop_to_the_caller(tmp_path):
    server = FakeSMTP()
    try:
        app = make_app(tmp_path, [server.port])
        configure(app, server.port)
        mailer = app.extensions['mailer']
        statuses = [mailer.enqueue('dest@example.org', 'Sujet', 'Corps') for _ in range(1005)]
        assert statuses.count('queued') == 1000 and statuses.count('dropped') == 5
    finally:
        server.stop()


def test_a_slow_or_dead_smtp_never_slows_down_enqueue(tmp_path):
    """EF-1.9 : la réponse du mot de passe oublié est rendue sans attendre l'envoi, que le serveur réponde, refuse ou se taise."""
    timings = {}
    for mode in ('ok', 'hang', 'dead'):
        server = FakeSMTP(mode='hang' if mode == 'hang' else 'ok')
        port = closed_port() if mode == 'dead' else server.port
        try:
            app = make_app(tmp_path, [port], name=f'q-{mode}')
            configure(app, port, timeout=5)
            mailer = app.extensions['mailer']
            started = time.perf_counter()
            for _ in range(200):
                assert mailer.enqueue('dest@example.org', 'Sujet', 'Corps') == 'queued'
            timings[mode] = time.perf_counter() - started
            assert server.connections == 0 or mode == 'ok'
        finally:
            server.stop()
    assert max(timings.values()) < 1.0 and max(timings.values()) < 5 * min(timings.values()) + 0.1, timings


def test_a_failing_message_does_not_stop_the_following_ones():
    seen = []

    def send(message):
        seen.append(message['Subject'])
        if 'S1' in message['Subject']:
            raise RuntimeError("boum avec un lien https://secret")

    queue = MailQueue(send)
    for i in range(4):
        queue.put(_message(i))
    assert queue.drain() == 4 and len(seen) == 4


def test_the_thread_start_is_idempotent_and_stop_ends_it():
    queue = MailQueue(lambda m: None)
    queue.start()
    first = queue._thread
    queue.start()
    assert queue._thread is first
    queue.put(_message())
    queue.stop()
    assert not first.is_alive() and queue._thread is None


def test_stop_returns_even_when_the_queue_is_full_and_the_worker_is_stuck():
    release = threading.Event()
    queue = MailQueue(lambda m: release.wait(30))
    queue.start()
    for i in range(1002):
        queue.put(_message(i))
    done = threading.Event()
    threading.Thread(target=lambda: (queue.stop(), done.set()), daemon=True).start()
    try:
        assert done.wait(1)
    finally:
        release.set()


# --- send_test : durée constante, réponse neutre (EF-4.2, CA-37) ----------------------------------------------------------------

def test_the_test_mail_always_answers_after_the_timeout_whatever_happens(tmp_path, certs):
    ok_server, hang, tls = FakeSMTP(), FakeSMTP(mode='hang'), TlsSMTP(*certs)
    dead = closed_port()
    try:
        cases = {}
        for name, port, security in (('ok', ok_server.port, 'none'), ('hang', hang.port, 'none'), ('refused', dead, 'none'),
                                     ('tls-untrusted', tls.port, 'starttls')):
            app = make_app(tmp_path, [port], name=f't-{name}')
            configure(app, port, security=security, timeout=1)
            cases[name] = app
        cases['not-configured'] = create_app(Prod, overrides={'DATA_DIR': str(tmp_path / 't-nc'), 'AUTH_MODE': 'password', 'TRUSTED_HOSTS': [HOST],
                                                             'SECRETS_KEY': KEY, 'PUBLIC_URL': f'https://{HOST}'})
        cases['refused-destination'] = make_app(tmp_path, [ok_server.port + 1], name='t-rd')
        conn = db_of(cases['refused-destination'])
        conn.execute("INSERT INTO settings (key, value, updated_at, updated_by) VALUES ('smtp.host', '\"127.0.0.1\"', 'x', 'sql'), "
                     "('smtp.port', ?, 'x', 'sql'), ('smtp.sender', '\"noreply@example.org\"', 'x', 'sql'), ('smtp.security', '\"none\"', 'x', 'sql'), "
                     "('smtp.timeout_s', '1', 'x', 'sql')", (str(ok_server.port),))
        conn.execute("UPDATE settings_meta SET version = version + 1")
        conn.commit()
        cases['refused-destination'].config['SETTINGS_STORE'].refresh(conn, force=True)
        conn.close()
        durations, answers = {}, {}
        for name, app in cases.items():
            started = time.monotonic()
            answers[name] = app.extensions['mailer'].send_test('admin@example.org', timeout_s=1.0)
            durations[name] = time.monotonic() - started
        assert all(set(a) == {'ok'} and isinstance(a['ok'], bool) for a in answers.values()), answers
        assert answers['ok'] == {'ok': True} and all(answers[n] == {'ok': False} for n in answers if n != 'ok'), answers
        assert all(0.95 <= d <= 1.15 for d in durations.values()), durations
    finally:
        for server in (ok_server, hang, tls):
            server.stop()


def test_the_test_mail_answer_carries_no_code_message_or_address(tmp_path):
    dead = closed_port()
    app = make_app(tmp_path, [dead])
    configure(app, dead, timeout=1)
    answer = app.extensions['mailer'].send_test('admin@example.org', timeout_s=1.0)
    assert answer == {'ok': False} and 'refus' not in repr(answer).lower() and 'example.org' not in repr(answer)
