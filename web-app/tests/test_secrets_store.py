"""Secrets au repos : Fernet multi-clés, liaison à la destination, rotation, remise à zéro (T4, EF-5, CA-19/20/34)."""

import json
import logging

import pytest
from cryptography.fernet import Fernet

from app import create_app, db as database
from app.audit import Actor
from app.config import TestingConfig
from app.secrets_store import (SecretsKeyError, SecretsStore, SecretsUnavailable, SecretUnavailable, UndecryptableSecrets,
                               parse_keys)

KEY1, KEY2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
ALICE = Actor.user(1, 'alice')
DEST = 'https://moteur.interne:8989'
API_KEY = 'ak_live_0123456789abcdef'
PASSWORD = 'smtp-p@ssw0rd-très-secret'


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / 'sec.sqlite3')
    database.init_db(path)
    c = database.connect(path)
    yield c
    c.close()


@pytest.fixture
def store():
    return SecretsStore(KEY1)


def audit_text(conn):
    return json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])


def events(conn):
    return [r['action'] for r in conn.execute("SELECT action FROM audit_log WHERE action LIKE 'secret.%' ORDER BY id")]


# --- Clés ------------------------------------------------------------------------------------

def test_parse_keys():
    assert parse_keys('') == [] and parse_keys(None) == [] and parse_keys(' , ,') == []
    assert len(parse_keys(f'{KEY1}, {KEY2} ,')) == 2


@pytest.mark.parametrize('raw', ['pas-une-cle', KEY1 + 'x', KEY1[:-4], f'{KEY1},oups', 'é' * 44])
def test_invalid_key_is_refused_without_quoting_it(raw):
    with pytest.raises(SecretsKeyError) as exc:
        parse_keys(raw)
    assert raw not in str(exc.value) and 'oups' not in str(exc.value)
    if raw == f'{KEY1},oups':
        assert 'n°2' in str(exc.value)


def test_without_a_key_the_store_is_unavailable(conn):
    s = SecretsStore('')
    assert not s.available
    with pytest.raises(SecretsUnavailable):
        s.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0


# --- Écriture seule, jamais en clair (CA-19) -----------------------------------------------------

def test_secret_is_encrypted_at_rest_and_never_in_the_audit(store, conn):
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    store.set(conn, 'smtp.password', PASSWORD, 'smtp://relais:587', actor=ALICE)
    raw = b''.join(bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM secrets"))
    assert API_KEY.encode() not in raw and PASSWORD.encode() not in raw
    assert API_KEY not in audit_text(conn) and PASSWORD not in audit_text(conn) and API_KEY[-4:] not in audit_text(conn)
    assert events(conn) == ['secret.update', 'secret.update']


def test_status_shows_no_value_and_last4_only_for_api_keys(store, conn):
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    store.set(conn, 'smtp.password', PASSWORD, 'smtp://relais:587', actor=ALICE)
    api, pwd = store.status(conn, 'routing.api_key'), store.status(conn, 'smtp.password')
    assert api['configured'] and api['last4'] == API_KEY[-4:] and api['bound_to'] == DEST and api['updated_by'] == 'alice'
    assert pwd['configured'] and 'last4' not in pwd                     # jamais pour un mot de passe (n3)
    for status in (api, pwd):
        assert API_KEY not in json.dumps(status) and PASSWORD not in json.dumps(status)
    assert store.status(conn, 'routing.api_key') != {'name': 'routing.api_key', 'configured': False}


def test_a_short_api_key_keeps_no_trailing_characters(store, conn):
    store.set(conn, 'routing.api_key', 'abcdefgh', DEST, actor=ALICE)
    assert store.status(conn, 'routing.api_key')['last4'] is None


def test_status_of_an_unset_secret_and_unknown_names(store, conn):
    assert store.status(conn, 'smtp.password') == {'name': 'smtp.password', 'configured': False}
    for call in (lambda: store.status(conn, 'nope'), lambda: store.clear(conn, 'nope', actor=ALICE),
                 lambda: store.set(conn, 'nope', 'x', DEST, actor=ALICE)):
        with pytest.raises(ValueError):
            call()
    with pytest.raises(SecretUnavailable):
        store.get(conn, 'nope', DEST)


@pytest.mark.parametrize('value', ['', None, 5, 'a\nb', 'a\r', 'a\x00b', 'x' * 513, b'bytes'])
def test_invalid_secret_values_are_refused(store, conn, value):
    with pytest.raises(ValueError):
        store.set(conn, 'smtp.password', value, 'smtp://r:587', actor=ALICE)
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0


@pytest.mark.parametrize('dest', ['', None, 'x' * 501, 'a\nb'])
def test_invalid_bound_destination_is_refused(store, conn, dest):
    with pytest.raises(ValueError):
        store.set(conn, 'smtp.password', PASSWORD, dest, actor=ALICE)


def test_replacing_a_secret_replaces_value_destination_and_last4(store, conn):
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    store.set(conn, 'routing.api_key', 'nouvelle-cle-000111', 'https://autre:1', actor=Actor.cli())
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 1
    assert store.get(conn, 'routing.api_key', 'https://autre:1') == 'nouvelle-cle-000111'
    status = store.status(conn, 'routing.api_key')
    assert status['last4'] == '0111' and status['updated_by'] == 'cli'


def test_clear_is_traced_only_when_something_was_cleared(store, conn):
    assert store.clear(conn, 'routing.api_key', actor=ALICE) is False and events(conn) == []
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    assert store.clear(conn, 'routing.api_key', actor=ALICE) is True
    assert events(conn) == ['secret.update', 'secret.clear'] and not store.status(conn, 'routing.api_key')['configured']


# --- Liaison à la destination (B3, CA-34) -------------------------------------------------------------

def test_secret_is_given_back_only_for_its_bound_destination(store, conn):
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    assert store.get(conn, 'routing.api_key', DEST) == API_KEY
    for other in ('https://moteur.interne:8990', 'http://moteur.interne:8989', 'https://MOTEUR.interne:8989', '', 'evil'):
        with pytest.raises(SecretUnavailable):
            store.get(conn, 'routing.api_key', other)


def test_a_refused_use_is_audited_without_any_value(store, conn, caplog):
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    with caplog.at_level(logging.DEBUG), pytest.raises(SecretUnavailable):
        store.get(conn, 'routing.api_key', 'https://evil.example:443')
    assert events(conn)[-1] == 'secret.refused'
    last = conn.execute("SELECT actor_type, actor_id, details FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert last['actor_type'] == 'system' and last['actor_id'] is None
    assert API_KEY not in audit_text(conn) and API_KEY not in caplog.text and 'evil.example' not in audit_text(conn)


def test_changing_a_destination_clears_the_linked_secrets_in_the_same_transaction(store, conn):
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    store.set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    with pytest.raises(RuntimeError), database.write_transaction(conn):
        assert store.clear_linked(conn, ['routing.api_key'], actor=ALICE) == ['routing.api_key']
        raise RuntimeError                                          # le changement de destination échoue : tout est annulé
    assert store.status(conn, 'routing.api_key')['configured']
    with database.write_transaction(conn):
        assert store.clear_linked(conn, ['routing.api_key', 'smtp.password'], actor=ALICE) == ['routing.api_key', 'smtp.password']
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    assert events(conn)[-2:] == ['secret.clear_on_destination_change'] * 2
    assert store.clear_linked(conn, ['routing.api_key'], actor=ALICE) == []


def test_env_secret_goes_only_to_the_env_destination_and_the_database_wins(store, conn):
    """EF-5.5 : base > env ; un secret d'env n'est pas envoyé si la base surcharge la destination."""
    env = dict(env_secret='secret-d-env-12345', env_destination='https://env:1')
    assert store.effective(conn, 'routing.api_key', 'https://env:1', **env) == 'secret-d-env-12345'
    assert store.effective(conn, 'routing.api_key', 'https://base-surcharge:2', **env) is None
    assert store.effective(conn, 'routing.api_key', 'https://env:1', env_secret='x') is None      # destination d'env inconnue
    assert store.effective(conn, 'routing.api_key', 'https://env:1') is None
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    assert store.effective(conn, 'routing.api_key', DEST, **env) == API_KEY                        # la base prime
    with pytest.raises(SecretUnavailable):                                                         # et pas de repli sur l'env
        store.effective(conn, 'routing.api_key', 'https://env:1', **env)


# --- Démarrage (CA-20) -----------------------------------------------------------------------

def test_verify_all_counts_and_refuses_undecryptable_values(store, conn):
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    store.set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    assert store.verify_all(conn) == 2
    with pytest.raises(UndecryptableSecrets) as exc:
        SecretsStore(KEY2).verify_all(conn)
    assert exc.value.count == 2 and API_KEY not in str(exc.value) and KEY1 not in str(exc.value)
    with pytest.raises(UndecryptableSecrets):                                  # aucune clé fournie alors qu'il y a du chiffré
        SecretsStore('').verify_all(conn)
    assert SecretsStore(f'{KEY2},{KEY1}').verify_all(conn) == 2                # l'ancienne clé en queue suffit à lire


def test_verify_all_on_an_empty_database_needs_no_key(conn):
    assert SecretsStore('').verify_all(conn) == 0


def _app(tmp_path, **overrides):
    return create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), **overrides})


def test_application_refuses_to_start_when_a_value_cannot_be_decrypted(tmp_path):
    app = _app(tmp_path, SECRETS_KEY=KEY1)
    with app.app_context():
        c = database.connect(app.config['DB_PATH'])
        app.extensions['secrets'].set(c, 'routing.api_key', API_KEY, DEST, actor=ALICE)
        c.close()
    _app(tmp_path, SECRETS_KEY=f'{KEY2},{KEY1}')                              # clés tournées : démarre
    for wrong in (KEY2, ''):
        with pytest.raises(RuntimeError) as exc:
            _app(tmp_path, SECRETS_KEY=wrong)
        text = str(exc.value)
        assert '1 valeur' in text and 'Démarrage refusé' in text and API_KEY not in text and KEY1 not in text and KEY2 not in text


def test_application_refuses_an_invalid_key_and_never_echoes_it(tmp_path):
    with pytest.raises(RuntimeError) as exc:
        _app(tmp_path, SECRETS_KEY='pas-une-cle-fernet')
    assert 'pas-une-cle-fernet' not in str(exc.value)


def test_the_key_is_not_kept_in_the_application_config(tmp_path):
    app = _app(tmp_path, SECRETS_KEY=KEY1)
    assert 'SECRETS_KEY' not in app.config and app.extensions['secrets'].available
    assert not _app(tmp_path / 'b').extensions['secrets'].available


def test_secrets_key_is_not_a_registered_setting():
    from app.settings import REGISTRY
    assert not any('SECRETS' in k.upper() or 'SECRETS' in (s.env or '') for k, s in REGISTRY.items())


# --- Rotation (EF-5.7) ---------------------------------------------------------------------------

def test_rotation_reencrypts_with_the_first_key_so_the_old_one_can_be_removed(conn):
    old = SecretsStore(KEY1)
    old.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    old.set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    before = [bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM secrets ORDER BY name")]
    new = SecretsStore(f'{KEY2},{KEY1}')
    assert new.rotate(conn, actor=Actor.cli()) == {'secrets': 2, 'totp': 0}
    assert [bytes(r[0]) for r in conn.execute("SELECT ciphertext FROM secrets ORDER BY name")] != before
    only_new = SecretsStore(KEY2)
    assert only_new.verify_all(conn) == 2 and only_new.get(conn, 'routing.api_key', DEST) == API_KEY
    with pytest.raises(UndecryptableSecrets):
        SecretsStore(KEY1).verify_all(conn)
    last = conn.execute("SELECT actor_type, actor_id, action, details FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (last['actor_type'], last['actor_id'], last['action']) == ('cli', None, 'secret.rotate')
    assert json.loads(last['details']) == {'values': 2, 'accounts': 0, 'skipped': 0}      # comptes lisibles (non masqués)


def test_rotation_is_idempotent_and_needs_a_key(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    s = SecretsStore(KEY1)
    assert s.rotate(conn, actor=Actor.cli())['secrets'] == 1 and s.rotate(conn, actor=Actor.cli())['secrets'] == 1
    assert s.get(conn, 'smtp.password', 'smtp://r:587') == PASSWORD
    with pytest.raises(SecretsUnavailable):
        SecretsStore('').rotate(conn, actor=Actor.cli())


def test_interrupted_rotation_leaves_every_row_readable(conn, monkeypatch):
    SecretsStore(KEY1).set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    new = SecretsStore(f'{KEY2},{KEY1}')
    real = new._fernet.rotate
    calls = []

    def flaky(token):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("coupure")
        return real(token)

    monkeypatch.setattr(new._fernet, 'rotate', flaky)
    with pytest.raises(RuntimeError):
        new.rotate(conn, actor=Actor.cli())
    assert not conn.in_transaction
    assert new.verify_all(conn) == 2                      # une ligne à la nouvelle clé, l'autre à l'ancienne, toutes lisibles
    assert events(conn) == ['secret.update', 'secret.update']          # pas de trace de rotation réussie


def test_rotation_stops_on_an_undecryptable_value_and_touches_nothing_more(conn):
    SecretsStore(KEY1).set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    before = bytes(conn.execute("SELECT ciphertext FROM secrets").fetchone()[0])
    with pytest.raises(UndecryptableSecrets):
        SecretsStore(KEY2).rotate(conn, actor=Actor.cli())
    assert bytes(conn.execute("SELECT ciphertext FROM secrets").fetchone()[0]) == before


def _add_totp_columns(conn, key):
    conn.execute("ALTER TABLE users ADD COLUMN totp_secret BLOB")
    conn.execute("ALTER TABLE users ADD COLUMN totp_last_step INTEGER")
    conn.execute("ALTER TABLE users ADD COLUMN totp_enabled_at TEXT")
    conn.execute("INSERT INTO users (sub, username, totp_secret, totp_last_step, totp_enabled_at) VALUES "
                 "('a', 'a', ?, 5, 'x'), ('b', 'b', NULL, NULL, NULL)", (Fernet(key.encode()).encrypt(b'JBSWY3DPEHPK3PXP'),))
    conn.commit()


def test_rotation_and_verification_cover_totp_secrets_once_the_column_exists(conn):
    _add_totp_columns(conn, KEY1)
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    assert SecretsStore(KEY1).verify_all(conn) == 2
    with pytest.raises(UndecryptableSecrets) as exc:
        SecretsStore(KEY2).verify_all(conn)
    assert exc.value.count == 2
    assert SecretsStore(f'{KEY2},{KEY1}').rotate(conn, actor=Actor.cli()) == {'secrets': 1, 'totp': 1}
    assert SecretsStore(KEY2).verify_all(conn) == 2
    assert Fernet(KEY2.encode()).decrypt(bytes(conn.execute("SELECT totp_secret FROM users WHERE sub = 'a'").fetchone()[0])) \
        == b'JBSWY3DPEHPK3PXP'


# --- Remise à zéro (perte de toutes les clés) --------------------------------------------------------

def test_reset_erases_secrets_and_totp_and_is_traced(conn):
    _add_totp_columns(conn, KEY1)
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    assert SecretsStore(KEY2).reset(conn, actor=Actor.cli()) == {'secrets': 1, 'totp': 1}
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    row = conn.execute("SELECT totp_secret, totp_last_step, totp_enabled_at FROM users WHERE sub = 'a'").fetchone()
    assert tuple(row) == (None, None, None)
    assert SecretsStore(KEY2).verify_all(conn) == 0                           # l'instance peut de nouveau démarrer
    last = conn.execute("SELECT actor_type, action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    assert (last['actor_type'], last['action']) == ('cli', 'secret.reset')


def test_reset_without_totp_columns(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    assert SecretsStore('').reset(conn, actor=Actor.cli()) == {'secrets': 1, 'totp': 0}


# --- Commandes CLI ---------------------------------------------------------------------------

@pytest.fixture
def secret_app(tmp_path):
    app = _app(tmp_path, SECRETS_KEY=f'{KEY2},{KEY1}')
    c = database.connect(app.config['DB_PATH'])
    SecretsStore(KEY1).set(c, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    c.close()
    return app


def test_secrets_rotate_command(secret_app):
    result = secret_app.test_cli_runner().invoke(args=['secrets-rotate'])
    assert result.exit_code == 0 and '1 secret(s)' in result.output and API_KEY not in result.output
    c = database.connect(secret_app.config['DB_PATH'])
    assert SecretsStore(KEY2).verify_all(c) == 1
    c.close()


def test_secrets_reset_command_asks_for_confirmation(secret_app):
    runner = secret_app.test_cli_runner()
    assert runner.invoke(args=['secrets-reset'], input='n\n').exit_code != 0
    c = database.connect(secret_app.config['DB_PATH'])
    assert c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 1
    assert runner.invoke(args=['secrets-reset', '--yes']).exit_code == 0
    assert c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    c.close()


def test_secrets_rotate_command_without_a_key_fails_cleanly(tmp_path):
    result = _app(tmp_path).test_cli_runner().invoke(args=['secrets-rotate'])
    assert result.exit_code != 0 and 'SECRETS_KEY' in result.output


# --- Schéma ----------------------------------------------------------------------------------

def test_secrets_table_is_created_by_its_named_migration(conn):
    assert conn.execute("SELECT 1 FROM schema_migrations WHERE name = '0008_secrets'").fetchone()
    cols = {r['name'] for r in conn.execute("PRAGMA table_info(secrets)")}
    assert cols == {'name', 'ciphertext', 'last4', 'bound_to', 'updated_at', 'updated_by'}


# --- Relecture T4 : CLI de secours, rotation concurrente, clés strictes, jeton TEXT -------------------

@pytest.mark.parametrize('raw', [KEY1 + 'x', KEY1 + '=', KEY1[:-1], KEY1[:-1] + '!', 'A' * 44, 'A' * 43 + '=' + 'B'])
def test_keys_are_validated_strictly(raw):
    """T4 (4) : Fernet() tolère des déchets après le « = » ; pas ici (base64 URL-safe, 32 octets, rien d'autre)."""
    with pytest.raises(SecretsKeyError):
        parse_keys(raw)


def test_a_text_typed_ciphertext_is_undecryptable_not_a_crash(store, conn):
    store.set(conn, 'smtp.password', PASSWORD, 'smtp://r:587', actor=ALICE)
    conn.execute("UPDATE secrets SET ciphertext = 'pas-du-fernet'")
    conn.commit()
    with pytest.raises(UndecryptableSecrets):
        store.verify_all(conn)
    with pytest.raises(SecretUnavailable):
        store.get(conn, 'smtp.password', 'smtp://r:587')
    with pytest.raises(UndecryptableSecrets):
        store.rotate(conn, actor=Actor.cli())


def test_rotation_never_overwrites_a_value_changed_meanwhile_and_reports_it(conn, tmp_path, monkeypatch):
    SecretsStore(KEY1).set(conn, 'routing.api_key', 'ancienne-valeur-123456', DEST, actor=ALICE)
    new = SecretsStore(f'{KEY2},{KEY1}')
    real = new._fernet.rotate
    other = database.connect(str(tmp_path / 'sec.sqlite3'))

    def rotate_while_another_process_saves(token):
        renewed = real(token)
        SecretsStore(KEY2).set(other, 'routing.api_key', 'NOUVELLE-valeur-999999', DEST, actor=ALICE)
        return renewed

    monkeypatch.setattr(new._fernet, 'rotate', rotate_while_another_process_saves)
    counts = new.rotate(conn, actor=Actor.cli())
    other.close()
    assert counts == {'secrets': 0, 'totp': 0, 'skipped': 1}
    assert SecretsStore(KEY2).get(conn, 'routing.api_key', DEST) == 'NOUVELLE-valeur-999999'


def _in_cli_context(command_name):
    import click
    return click.Context(click.Command(command_name))


def test_rescue_context_is_only_a_flask_cli_command_other_than_run(monkeypatch):
    from app import secrets_store
    monkeypatch.delenv('FLASK_RUN_FROM_CLI', raising=False)
    assert secrets_store._rescue_cli_context() is False                    # gunicorn, python kml_viewer.py
    monkeypatch.setenv('FLASK_RUN_FROM_CLI', 'true')
    assert secrets_store._rescue_cli_context() is False                    # pas de contexte click (fil du serveur)
    with _in_cli_context('flask'):
        assert secrets_store._rescue_cli_context() is True
    with _in_cli_context('run'):
        assert secrets_store._rescue_cli_context() is False                # `flask run` est le service, pas la CLI de secours


def test_cli_rescue_mode_degrades_instead_of_refusing_and_only_reset_works(tmp_path, monkeypatch):
    app = _app(tmp_path, SECRETS_KEY=KEY1)
    c = database.connect(app.config['DB_PATH'])
    SecretsStore(KEY1).set(c, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    monkeypatch.setenv('FLASK_RUN_FROM_CLI', 'true')
    with _in_cli_context('flask'):
        rescue = _app(tmp_path, SECRETS_KEY=KEY2).extensions['secrets']
        invalid = _app(tmp_path, SECRETS_KEY='pas-une-cle').extensions['secrets']
    for store_ in (rescue, invalid):
        assert store_.degraded == 1
        for call in (lambda s=store_: s.get(c, 'routing.api_key', DEST), lambda s=store_: s.rotate(c, actor=Actor.cli()),
                     lambda s=store_: s.set(c, 'routing.api_key', API_KEY, DEST, actor=ALICE),
                     lambda s=store_: s.effective(c, 'routing.api_key', DEST), lambda s=store_: s.verify_all(c)):
            with pytest.raises(UndecryptableSecrets):
                call()
        assert store_.status(c, 'routing.api_key')['configured']
    assert rescue.reset(c, actor=Actor.cli()) == {'secrets': 1, 'totp': 0}
    c.close()
    _app(tmp_path)                                                          # base vide : le service démarre de nouveau, sans clé


def test_the_service_still_refuses_to_start_even_with_flask_cli_variables(tmp_path, monkeypatch):
    app = _app(tmp_path, SECRETS_KEY=KEY1)
    c = database.connect(app.config['DB_PATH'])
    SecretsStore(KEY1).set(c, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    c.close()
    monkeypatch.setenv('FLASK_RUN_FROM_CLI', 'true')
    with _in_cli_context('run'), pytest.raises(RuntimeError, match='secrets-reset'):
        _app(tmp_path, SECRETS_KEY=KEY2)


def test_secrets_key_is_neither_a_config_class_attribute_nor_kept_after_startup(tmp_path, monkeypatch):
    from app.config import Config
    assert not hasattr(Config, 'SECRETS_KEY') and not hasattr(TestingConfig, 'SECRETS_KEY')
    monkeypatch.setenv('SECRETS_KEY', KEY1)
    app = _app(tmp_path)                                                    # lue dans l'environnement
    assert app.extensions['secrets'].available and 'SECRETS_KEY' not in app.config
