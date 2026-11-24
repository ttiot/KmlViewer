"""
Cas limites du magasin de secrets (T4, EF-5.1 à EF-5.7, ADR-0003, ADR-0011, CA-20, CA-34, CA-38).

Complète test_secrets_store.py : aucune valeur ni clé dans les journaux, exceptions, repr, audit,
sortie CLI et sauvegarde ; `last4` aux bornes ; lecture liée à la destination ; rotation interrompue
à chaque position et reprise ; ordre des clés ; clés invalides ou perdues ; CLI.
"""

import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import traceback

import pytest

from tests.conftest import ensure_column
from cryptography.fernet import Fernet, InvalidToken

from app import create_app
from app import db as database
from app.audit import Actor
from app.config import TestingConfig
from app.secrets_store import (SecretsKeyError, SecretsStore, SecretsUnavailable, SecretUnavailable, UndecryptableSecrets,
                               parse_keys)

KEY1, KEY2, KEY3 = (Fernet.generate_key().decode() for _ in range(3))
ALICE, CLI = Actor.user(1, 'alice'), Actor.cli()
DEST = 'https://moteur.interne:8989'
SMTP = 'smtp://relais.interne:587'
API_KEY = 'ak_live_0123456789abcdef'
PASSWORD = 'smtp-p@ssw0rd-très-secret'
ROOT = str(pathlib.Path(__file__).resolve().parents[1])


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / 'sec.sqlite3')
    database.init_db(p)
    return p


@pytest.fixture
def conn(path):
    c = database.connect(path)
    yield c
    c.close()


def ciphers(conn):
    return {r['name']: bytes(r['ciphertext']) for r in conn.execute("SELECT name, ciphertext FROM secrets")}


def everything_readable(store, conn):
    return store.verify_all(conn)


def full_trace(exc):
    return ''.join(traceback.format_exception(exc)) + repr(exc) + str(exc) + repr(getattr(exc, 'args', ''))


def forbidden_in(text, *extra):
    needles = (KEY1, KEY2, KEY3, API_KEY, PASSWORD, *extra)
    return [n for n in needles if n and n in text]


# --- Aucune fuite, nulle part ----------------------------------------------------------------

def test_no_value_and_no_key_in_any_exception_log_audit_repr_or_status(conn, caplog):
    cipher_texts = []
    texts = []
    with caplog.at_level(logging.DEBUG):
        store = SecretsStore(f'{KEY2},{KEY1}')
        store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
        store.set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
        cipher_texts += [c.decode('ascii') for c in ciphers(conn).values()]
        texts += [repr(store), repr(vars(store)), json.dumps(store.status(conn, 'routing.api_key')),
                  json.dumps(store.status(conn, 'smtp.password'))]
        for call in (lambda: parse_keys(f'{KEY1},pas-une-cle'), lambda: parse_keys('é' * 44),
                     lambda: SecretsStore('').set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE),
                     lambda: SecretsStore('').get(conn, 'smtp.password', SMTP),
                     lambda: store.get(conn, 'routing.api_key', 'https://evil:1'),
                     lambda: store.get(conn, 'nope', DEST),
                     lambda: store.set(conn, 'smtp.password', 'x\ny', SMTP, actor=ALICE),
                     lambda: SecretsStore(KEY3).get(conn, 'smtp.password', SMTP),
                     lambda: SecretsStore(KEY3).verify_all(conn),
                     lambda: SecretsStore(KEY3).rotate(conn, actor=CLI)):
            with pytest.raises(Exception) as exc:
                call()
            texts.append(full_trace(exc.value))
        store.rotate(conn, actor=CLI)
        cipher_texts += [c.decode('ascii') for c in ciphers(conn).values()]
        store.reset(conn, actor=CLI)
    texts += [caplog.text, json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit_log")])]
    for text in texts:
        assert not forbidden_in(text, API_KEY[-4:] if text.startswith('{"name": "smtp') else ''), text[:200]
        assert not any(c in text for c in cipher_texts if len(c) > 30)


def test_application_startup_failures_leak_neither_keys_nor_values(tmp_path):
    overrides = {'DATA_DIR': str(tmp_path / 'data'), 'SECRETS_KEY': KEY1}
    app = create_app(TestingConfig, overrides=overrides)
    c = database.connect(app.config['DB_PATH'])
    app.extensions['secrets'].set(c, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    c.close()
    for wrong in (KEY2, '', 'pas-une-cle', f'{KEY2},{KEY3}'):
        with pytest.raises(RuntimeError) as exc:
            create_app(TestingConfig, overrides={**overrides, 'SECRETS_KEY': wrong})
        assert not forbidden_in(full_trace(exc.value), wrong or None), wrong
        assert exc.value.__cause__ is None


def test_the_key_never_stays_in_the_application_object(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'SECRETS_KEY': KEY1})
    dump = repr(dict(app.config)) + repr(app.extensions['secrets'].__dict__)
    assert KEY1 not in dump


def test_flask_backup_contains_the_ciphertext_never_the_key_nor_the_plaintext(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'd'), 'SECRETS_KEY': KEY1})
    c = database.connect(app.config['DB_PATH'])
    app.extensions['secrets'].set(c, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    app.extensions['secrets'].set(c, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    c.close()
    target = tmp_path / 'sauvegarde.sqlite3'
    result = app.test_cli_runner().invoke(args=['backup', str(target)])
    assert result.exit_code == 0
    raw = target.read_bytes()
    assert KEY1.encode() not in raw and API_KEY.encode() not in raw and PASSWORD.encode() not in raw
    restored = sqlite3.connect(target)
    assert restored.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 2
    restored.close()


# --- last4 (EF-5.3, n3) -----------------------------------------------------------------------

@pytest.mark.parametrize('length,expected', [(1, None), (8, None), (11, None), (12, True), (13, True), (64, True), (512, True)])
def test_last4_is_kept_for_an_api_key_only_from_twelve_characters(conn, length, expected):
    value = ('K' * (length - 4) + 'wxyz') if length >= 4 else 'k' * length
    SecretsStore(KEY1).set(conn, 'routing.api_key', value, DEST, actor=ALICE)
    status = SecretsStore(KEY1).status(conn, 'routing.api_key')
    assert status['last4'] == ('wxyz' if expected else None)
    assert conn.execute("SELECT last4 FROM secrets").fetchone()[0] == ('wxyz' if expected else None)


def test_last4_counts_characters_not_bytes(conn):
    SecretsStore(KEY1).set(conn, 'routing.api_key', 'é' * 11, DEST, actor=ALICE)
    assert SecretsStore(KEY1).status(conn, 'routing.api_key')['last4'] is None
    SecretsStore(KEY1).set(conn, 'routing.api_key', 'é' * 8 + 'ñoçà', DEST, actor=ALICE)
    assert SecretsStore(KEY1).status(conn, 'routing.api_key')['last4'] == 'ñoçà'


@pytest.mark.parametrize('length', [1, 12, 100, 512])
def test_a_password_never_keeps_any_trailing_character(conn, length):
    SecretsStore(KEY1).set(conn, 'smtp.password', 'p' * (length - 1) + 'Z', SMTP, actor=ALICE)
    assert conn.execute("SELECT last4 FROM secrets").fetchone()[0] is None
    assert 'last4' not in SecretsStore(KEY1).status(conn, 'smtp.password')


def test_status_hides_last4_of_a_password_even_if_the_row_carries_one(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    conn.execute("UPDATE secrets SET last4 = 'rêts' WHERE name = 'smtp.password'")
    conn.commit()
    assert 'last4' not in SecretsStore(KEY1).status(conn, 'smtp.password')
    assert 'rêts' not in json.dumps(SecretsStore(KEY1).status(conn, 'smtp.password'))


def test_status_keys_are_a_closed_set(conn):
    SecretsStore(KEY1).set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    assert set(SecretsStore(KEY1).status(conn, 'routing.api_key')) == {'name', 'configured', 'bound_to', 'updated_at', 'updated_by', 'last4'}
    assert set(SecretsStore(KEY1).status(conn, 'smtp.password')) == {'name', 'configured', 'bound_to', 'updated_at', 'updated_by'}


def test_value_replaced_by_a_short_one_drops_the_old_last4(conn):
    SecretsStore(KEY1).set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    SecretsStore(KEY1).set(conn, 'routing.api_key', 'court', DEST, actor=ALICE)
    assert SecretsStore(KEY1).status(conn, 'routing.api_key')['last4'] is None


# --- Lecture liée à la destination (EF-5.4, EF-5.5, CA-34) -----------------------------------------

@pytest.mark.parametrize('destination', ['', 'None', 'https://moteur.interne:8989/', ' https://moteur.interne:8989',
                                         'https://moteur.interne:8989 ', 'https://moteur.interne:443', 'HTTPS://moteur.interne:8989',
                                         'https://moteur.interne.evil:8989', 'https://evil/https://moteur.interne:8989', DEST[:-1]])
def test_any_destination_that_is_not_exactly_the_bound_one_gets_nothing(conn, destination):
    SecretsStore(KEY1).set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    with pytest.raises(SecretUnavailable) as exc:
        SecretsStore(KEY1).get(conn, 'routing.api_key', destination)
    assert API_KEY not in full_trace(exc.value)


def test_every_refused_use_is_audited_by_the_system_with_no_value(conn, caplog):
    SecretsStore(KEY1).set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    with caplog.at_level(logging.DEBUG):
        for _ in range(3):
            with pytest.raises(SecretUnavailable):
                SecretsStore(KEY1).get(conn, 'routing.api_key', 'https://evil.example:443')
    rows = conn.execute("SELECT actor_type, actor_id, target_id, details FROM audit_log WHERE action = 'secret.refused'").fetchall()
    assert len(rows) == 3 and all(tuple(r)[:3] == ('system', None, 'routing.api_key') for r in rows)
    assert API_KEY not in caplog.text and 'evil.example' not in caplog.text and 'evil.example' not in json.dumps([tuple(r) for r in rows])


def test_get_does_not_exist_as_a_way_to_read_without_naming_a_destination(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    with pytest.raises(TypeError):
        SecretsStore(KEY1).get(conn, 'smtp.password')               # `destination` obligatoire


def test_get_without_any_key_or_with_the_wrong_key_returns_nothing_and_says_nothing(conn, caplog):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SecretsUnavailable) as no_key:
            SecretsStore('').get(conn, 'smtp.password', SMTP)
        with pytest.raises(SecretUnavailable) as wrong:
            SecretsStore(KEY2).get(conn, 'smtp.password', SMTP)
    assert 'smtp.password' in caplog.text
    for text in (caplog.text, full_trace(no_key.value), full_trace(wrong.value)):
        assert not forbidden_in(text)


@pytest.mark.parametrize('junk', [b'', b'\x00\xff', b'gAAAAA', b'gAAAAAB' + b'A' * 80, b'x' * 10_000])
def test_a_corrupted_ciphertext_is_a_clean_refusal_everywhere(conn, junk):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    conn.execute("UPDATE secrets SET ciphertext = ?", (junk,))
    conn.commit()
    with pytest.raises(SecretUnavailable):
        SecretsStore(KEY1).get(conn, 'smtp.password', SMTP)
    with pytest.raises(UndecryptableSecrets) as exc:
        SecretsStore(KEY1).verify_all(conn)
    assert exc.value.count == 1


def test_a_text_typed_ciphertext_counts_as_undecryptable_not_as_a_crash(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    conn.execute("UPDATE secrets SET ciphertext = 'pas-du-fernet'")
    conn.commit()
    with pytest.raises(UndecryptableSecrets):
        SecretsStore(KEY1).verify_all(conn)


def test_a_name_that_looks_like_sql_is_refused_before_touching_the_database(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    for evil in ("x'; DROP TABLE secrets;--", 'smtp.password ', 'SMTP.PASSWORD', '', None):
        with pytest.raises(ValueError):
            SecretsStore(KEY1).status(conn, evil)
        with pytest.raises(ValueError):
            SecretsStore(KEY1).clear(conn, evil, actor=ALICE)
        with pytest.raises(SecretUnavailable):
            SecretsStore(KEY1).get(conn, evil, SMTP)
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 1


# --- Changement de destination et env (EF-5.5, CA-34) ---------------------------------------------------

def test_clear_linked_with_an_unknown_name_rolls_the_earlier_deletions_back(conn):
    store = SecretsStore(KEY1)
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    with pytest.raises(ValueError):
        store.clear_linked(conn, ['routing.api_key', 'inconnu'], actor=ALICE)
    assert store.status(conn, 'routing.api_key')['configured'] and conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == before


def test_a_secret_set_inside_a_callers_transaction_disappears_with_it(conn):
    store = SecretsStore(KEY1)
    with pytest.raises(RuntimeError), database.write_transaction(conn):
        store.set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
        raise RuntimeError
    assert conn.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action LIKE 'secret.%'").fetchone()[0] == 0


def test_after_a_destination_change_the_old_secret_is_gone_and_a_new_one_binds_to_the_new_destination(conn):
    store = SecretsStore(KEY1)
    store.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    with database.write_transaction(conn):
        store.clear_linked(conn, ['routing.api_key'], actor=ALICE)
    with pytest.raises(SecretUnavailable):
        store.get(conn, 'routing.api_key', DEST)
    with pytest.raises(SecretUnavailable):
        store.get(conn, 'routing.api_key', 'https://nouveau:1')
    store.set(conn, 'routing.api_key', 'cle-pour-le-nouveau-1', 'https://nouveau:1', actor=ALICE)
    assert store.get(conn, 'routing.api_key', 'https://nouveau:1') == 'cle-pour-le-nouveau-1'
    with pytest.raises(SecretUnavailable):
        store.get(conn, 'routing.api_key', DEST)


def test_env_secret_rules_in_every_combination(conn):
    store = SecretsStore(KEY1)
    env = {'env_secret': 'secret-d-env-12345', 'env_destination': 'https://env:1'}
    assert store.effective(conn, 'smtp.password', 'https://env:1', **env) == 'secret-d-env-12345'
    assert store.effective(conn, 'smtp.password', 'https://env:2', **env) is None
    assert store.effective(conn, 'smtp.password', 'https://env:1', env_secret='', env_destination='https://env:1') is None
    assert store.effective(conn, 'smtp.password', 'https://env:1', env_secret='x', env_destination=None) is None
    store.set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    assert store.effective(conn, 'smtp.password', SMTP, **env) == PASSWORD                 # la base prime
    with pytest.raises(SecretUnavailable):
        store.effective(conn, 'smtp.password', 'https://env:1', **env)                    # pas de repli sur l'env
    store.clear(conn, 'smtp.password', actor=ALICE)
    assert store.effective(conn, 'smtp.password', 'https://env:1', **env) == 'secret-d-env-12345'   # base vide : l'env redevient utilisable


def test_effective_with_the_database_secret_but_no_key_fails_instead_of_falling_back_to_the_env(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    with pytest.raises(SecretsUnavailable):
        SecretsStore('').effective(conn, 'smtp.password', SMTP, env_secret='env-secret-1', env_destination=SMTP)


# --- Clés : ordre, anciennes clés, valeurs invalides ------------------------------------------------

def test_the_first_key_encrypts_and_the_others_only_decrypt(conn):
    store = SecretsStore(f'{KEY2},{KEY1}')
    store.set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    token = ciphers(conn)['smtp.password']
    assert Fernet(KEY2.encode()).decrypt(token) == PASSWORD.encode('utf-8')
    with pytest.raises(InvalidToken):
        Fernet(KEY1.encode()).decrypt(token)


def test_an_old_key_in_any_position_still_reads_and_a_foreign_key_does_not(conn):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    for keys in (f'{KEY2},{KEY1}', f'{KEY2},{KEY3},{KEY1}', f'{KEY1},{KEY2}', KEY1):
        assert SecretsStore(keys).get(conn, 'smtp.password', SMTP) == PASSWORD
    for keys in (KEY2, f'{KEY2},{KEY3}'):
        with pytest.raises(SecretUnavailable):
            SecretsStore(keys).get(conn, 'smtp.password', SMTP)


@pytest.mark.parametrize('raw', [f'{KEY1}\n{KEY2}', f'{KEY1};{KEY2}', f'{KEY1} {KEY2}', 'AAAA', KEY1[:-1], KEY1.replace('=', ''),
                                 '\x00' * 44, 'é' * 44, KEY1.encode().hex()])
def test_malformed_keys_are_refused_and_never_quoted(raw):
    try:
        parsed = parse_keys(raw)
    except SecretsKeyError as exc:
        assert raw not in str(exc) and not forbidden_in(full_trace(exc))
        assert exc.__cause__ is None
    else:                                           # tolérés : les séparateurs blancs internes sont ignorés par Fernet
        assert all(isinstance(f, Fernet) for f in parsed)


def test_keys_are_trimmed_and_empty_entries_ignored():
    assert len(parse_keys(f' ,{KEY1}\t, ,{KEY2},\n')) == 2


def test_an_invalid_entry_among_valid_keys_refuses_the_whole_list():
    with pytest.raises(SecretsKeyError) as exc:
        SecretsStore(f'{KEY1},{KEY2},xx')
    assert 'n°3' in str(exc.value)


# --- Rotation : interruption à chaque position, reprise (EF-5.7, CA-38) ---------------------------------

def _three_secrets(conn):
    old = SecretsStore(KEY1)
    old.set(conn, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    old.set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    ensure_column(conn, 'users', 'totp_secret', 'BLOB')
    conn.execute("INSERT INTO users (sub, username, totp_secret) VALUES ('a', 'a', ?)", (Fernet(KEY1.encode()).encrypt(b'JBSWY3DPEHPK3PXP'),))
    conn.commit()
    return 3


@pytest.mark.parametrize('stop_at', [1, 2, 3])
@pytest.mark.parametrize('interruption', [RuntimeError("coupure"), KeyboardInterrupt()], ids=['erreur', 'ctrl-c'])
def test_rotation_interrupted_at_any_row_leaves_everything_readable_and_resumes_without_loss(conn, monkeypatch, stop_at, interruption):
    total = _three_secrets(conn)
    new = SecretsStore(f'{KEY2},{KEY1}')
    real, calls = new._fernet.rotate, []

    def flaky(token):
        calls.append(1)
        if len(calls) == stop_at:
            raise interruption
        return real(token)

    monkeypatch.setattr(new._fernet, 'rotate', flaky)
    with pytest.raises(type(interruption)):
        new.rotate(conn, actor=CLI)
    assert not conn.in_transaction
    assert new.verify_all(conn) == total                                         # toutes lisibles avec les deux clés
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'secret.rotate'").fetchone()[0] == 0
    # Reprise : une nouvelle exécution termine le travail, sans perte
    monkeypatch.setattr(new._fernet, 'rotate', real)
    assert new.rotate(conn, actor=CLI) == {'secrets': 2, 'totp': 1}
    only_new = SecretsStore(KEY2)
    assert only_new.verify_all(conn) == total
    assert only_new.get(conn, 'routing.api_key', DEST) == API_KEY and only_new.get(conn, 'smtp.password', SMTP) == PASSWORD
    assert Fernet(KEY2.encode()).decrypt(bytes(conn.execute("SELECT totp_secret FROM users").fetchone()[0])) == b'JBSWY3DPEHPK3PXP'
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'secret.rotate'").fetchone()[0] == 1


def test_a_failing_update_in_the_middle_rolls_that_row_back_and_keeps_the_others(conn):
    _three_secrets(conn)
    new = SecretsStore(f'{KEY2},{KEY1}')
    before = ciphers(conn)

    class Wrapped:
        """Fait échouer la seconde écriture de ciphertext : l'ancienne valeur de cette ligne doit rester."""

        def __init__(self, inner):
            self.inner, self.writes = inner, 0

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def execute(self, sql, *args):
            if sql.startswith('UPDATE secrets SET ciphertext'):
                self.writes += 1
                if self.writes == 2:
                    raise sqlite3.OperationalError("disk I/O error")
            return self.inner.execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError):
        new.rotate(Wrapped(conn), actor=CLI)
    after = ciphers(conn)
    assert sum(1 for name in before if before[name] != after[name]) == 1
    assert new.verify_all(conn) == 3


def test_rotation_is_repeatable_and_keeps_the_count_stable(conn):
    _three_secrets(conn)
    new = SecretsStore(f'{KEY2},{KEY1}')
    for _ in range(3):
        assert new.rotate(conn, actor=CLI) == {'secrets': 2, 'totp': 1}
    assert SecretsStore(KEY2).verify_all(conn) == 3


def test_with_three_keys_everything_ends_on_the_first_one(conn):
    _three_secrets(conn)
    SecretsStore(f'{KEY2},{KEY1}').rotate(conn, actor=CLI)
    SecretsStore(f'{KEY3},{KEY2},{KEY1}').rotate(conn, actor=CLI)
    assert SecretsStore(KEY3).verify_all(conn) == 3


def test_a_secret_saved_during_the_rotation_is_not_overwritten_by_its_old_value(conn, path):
    SecretsStore(KEY1).set(conn, 'routing.api_key', 'ancienne-valeur-123456', DEST, actor=ALICE)
    new = SecretsStore(f'{KEY2},{KEY1}')
    real = new._fernet.rotate
    other = database.connect(path)

    def rotate_with_a_concurrent_save(token):
        renewed = real(token)
        SecretsStore(f'{KEY2},{KEY1}').set(other, 'routing.api_key', 'NOUVELLE-valeur-999999', DEST, actor=ALICE)
        return renewed

    new._fernet.rotate = rotate_with_a_concurrent_save
    new.rotate(conn, actor=CLI)
    other.close()
    assert SecretsStore(KEY2).get(conn, 'routing.api_key', DEST) == 'NOUVELLE-valeur-999999'


def test_a_totp_secret_enrolled_during_the_rotation_is_not_overwritten(conn, path):
    ensure_column(conn, 'users', 'totp_secret', 'BLOB')
    conn.execute("INSERT INTO users (sub, username, totp_secret) VALUES ('a', 'a', ?)", (Fernet(KEY1.encode()).encrypt(b'ANCIENSECRET234'),))
    conn.commit()
    new = SecretsStore(f'{KEY2},{KEY1}')
    real = new._fernet.rotate
    other = database.connect(path)

    def rotate_with_a_concurrent_enrolment(token):
        renewed = real(token)
        other.execute("UPDATE users SET totp_secret = ?", (Fernet(KEY2.encode()).encrypt(b'NOUVEAUSECRET567'),))
        other.commit()
        return renewed

    new._fernet.rotate = rotate_with_a_concurrent_enrolment
    new.rotate(conn, actor=CLI)
    other.close()
    assert Fernet(KEY2.encode()).decrypt(bytes(conn.execute("SELECT totp_secret FROM users").fetchone()[0])) == b'NOUVEAUSECRET567'


def test_rotation_with_nothing_encrypted_is_a_clean_no_op_but_still_traced(conn):
    assert SecretsStore(KEY1).rotate(conn, actor=CLI) == {'secrets': 0, 'totp': 0}
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'secret.rotate'").fetchone()[0] == 1


# --- Écritures concurrentes ----------------------------------------------------------------------

def test_concurrent_saves_stay_consistent_and_each_is_audited(path):
    errors = []

    def work(n):
        try:
            c = database.connect(path)
            try:
                store = SecretsStore(KEY1)
                for i in range(10):
                    store.set(c, ('routing.api_key', 'smtp.password')[n % 2], f'valeur-{n}-{i}-xxxxxxxx', DEST, actor=ALICE)
            finally:
                c.close()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    c = database.connect(path)
    try:
        assert not errors, errors
        assert c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 2
        assert c.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'secret.update'").fetchone()[0] == 60
        assert SecretsStore(KEY1).verify_all(c) == 2
    finally:
        c.close()


# --- CLI (EF-5.7) ---------------------------------------------------------------------------------

@pytest.fixture
def secret_app(tmp_path):
    app = create_app(TestingConfig, overrides={'DATA_DIR': str(tmp_path / 'data'), 'SECRETS_KEY': f'{KEY2},{KEY1}'})
    c = database.connect(app.config['DB_PATH'])
    SecretsStore(KEY1).set(c, 'routing.api_key', API_KEY, DEST, actor=ALICE)
    c.close()
    return app


def secret_count(app):
    c = database.connect(app.config['DB_PATH'])
    try:
        return c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0]
    finally:
        c.close()


@pytest.mark.parametrize('answer', ['', 'n\n', 'N\n', 'no\n', 'non\n', '\n', 'peut-être\n'])
def test_secrets_reset_without_yes_does_nothing_unless_explicitly_confirmed(secret_app, answer):
    result = secret_app.test_cli_runner().invoke(args=['secrets-reset'], input=answer)
    assert result.exit_code != 0 and secret_count(secret_app) == 1


@pytest.mark.parametrize('answer', ['y\n', 'Y\n', 'yes\n'])
def test_secrets_reset_proceeds_on_an_explicit_yes(secret_app, answer):
    result = secret_app.test_cli_runner().invoke(args=['secrets-reset'], input=answer)
    assert result.exit_code == 0 and secret_count(secret_app) == 0 and not forbidden_in(result.output)


def test_secrets_reset_is_traced_with_the_cli_actor_and_prints_no_secret(secret_app):
    result = secret_app.test_cli_runner().invoke(args=['secrets-reset', '--yes'])
    c = database.connect(secret_app.config['DB_PATH'])
    row = c.execute("SELECT actor_type, actor_id, details FROM audit_log WHERE action = 'secret.reset'").fetchone()
    c.close()
    assert tuple(row)[:2] == ('cli', None) and json.loads(row['details']) == {'values': 1, 'accounts': 0}
    assert not forbidden_in(result.output + str(row['details']))


def test_secrets_rotate_prints_counts_only_and_is_traced_by_the_cli(secret_app):
    result = secret_app.test_cli_runner().invoke(args=['secrets-rotate'])
    assert result.exit_code == 0 and '1 secret(s)' in result.output and not forbidden_in(result.output)
    c = database.connect(secret_app.config['DB_PATH'])
    assert SecretsStore(KEY2).verify_all(c) == 1
    assert c.execute("SELECT actor_type FROM audit_log WHERE action = 'secret.rotate'").fetchone()[0] == 'cli'
    c.close()


def _flask_cli(data_dir, keys, *args):
    env = {**os.environ, 'DATA_DIR': str(data_dir), 'FLASK_ENV': 'development', 'PYTHONPATH': ROOT, 'SECRETS_KEY': keys}
    return subprocess.run([sys.executable, '-m', 'flask', '--app', 'kml_viewer', *args], capture_output=True, text=True,
                          cwd=ROOT, env=env, timeout=120)


def _store_with_real_cli_stack(data_dir, key):
    code = ("import kml_viewer\nfrom app import db\nfrom app.audit import Actor\n"
            "c = db.connect(kml_viewer.app.config['DB_PATH'])\n"
            f"kml_viewer.app.extensions['secrets'].set(c, 'smtp.password', {PASSWORD!r}, {SMTP!r}, actor=Actor.cli())\n")
    env = {**os.environ, 'DATA_DIR': str(data_dir), 'FLASK_ENV': 'development', 'PYTHONPATH': ROOT, 'SECRETS_KEY': key}
    subprocess.run([sys.executable, '-c', code], check=True, capture_output=True, cwd=ROOT, env=env, timeout=120)


@pytest.mark.parametrize('lost_key', [KEY2, ''], ids=['mauvaise-cle', 'sans-cle'])
def test_secrets_reset_works_when_every_key_is_lost(tmp_path, lost_key):
    _store_with_real_cli_stack(tmp_path, KEY1)
    result = _flask_cli(tmp_path, lost_key, 'secrets-reset', '--yes')
    assert result.returncode == 0, (result.stdout + result.stderr)[-400:]
    c = sqlite3.connect(tmp_path / 'kmlviewer.sqlite3')
    assert c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    c.close()


def test_with_the_right_keys_the_real_cli_resets_and_the_application_starts_again(tmp_path):
    _store_with_real_cli_stack(tmp_path, KEY1)
    assert _flask_cli(tmp_path, KEY1, 'secrets-reset', '--yes').returncode == 0
    assert _flask_cli(tmp_path, KEY2, 'secrets-rotate').returncode == 0          # base vide : plus rien d'indéchiffrable


def test_the_real_cli_refuses_to_start_with_a_lost_key_without_leaking_anything(tmp_path):
    _store_with_real_cli_stack(tmp_path, KEY1)
    result = _flask_cli(tmp_path, KEY2, 'secrets-rotate')
    assert result.returncode != 0
    assert not forbidden_in(result.stdout + result.stderr, PASSWORD) and 'indéchiffrable' in result.stderr   # mode secours de la CLI : même message, la commande refuse


# --- Corrections T4 : parse_keys strict, rotation CAS, E2E clé perdue -------------------------------------

def test_no_valid_key_is_ever_rejected_by_the_strict_validation():
    """Une fausse alerte empêcherait le démarrage : 500 clés tirées au hasard (dont avec « - » et « _ ») passent."""
    seen_dash = seen_underscore = False
    for _ in range(500):
        key = Fernet.generate_key().decode()
        seen_dash |= '-' in key
        seen_underscore |= '_' in key
        assert len(parse_keys(key)) == 1, key
    assert seen_dash and seen_underscore


@pytest.mark.parametrize('mutate', [
    lambda k: k + 'x', lambda k: k + '=', lambda k: k + '!!', lambda k: 'x' + k, lambda k: k[:-1] + '!', lambda k: k[:20] + '!' + k[21:],
    lambda k: k[:20] + ' ' + k[21:], lambda k: k[:-1], lambda k: k[:-2], lambda k: k.rstrip('=') + '==', lambda k: k * 2,
    lambda k: k[:-1] + 'é', lambda k: k.encode().hex(), lambda k: k[:10] + '\n' + k[10:],
], ids=range(14))
def test_a_key_with_any_trailing_leading_or_inner_junk_is_refused_without_being_quoted(mutate):
    bad = mutate(KEY1)
    with pytest.raises(SecretsKeyError) as exc:
        parse_keys(bad)
    assert bad not in full_trace(exc.value) and KEY1 not in full_trace(exc.value)


def test_surrounding_whitespace_of_a_valid_key_is_still_tolerated():
    assert len(parse_keys(f'  {KEY1}\r\n')) == 1 and len(parse_keys(f'\t{KEY1} , {KEY2}\n')) == 2


def test_rotation_reports_a_row_changed_meanwhile_and_a_second_run_finishes_the_job(conn, path):
    SecretsStore(KEY1).set(conn, 'routing.api_key', 'ancienne-valeur-123456', DEST, actor=ALICE)
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    new = SecretsStore(f'{KEY2},{KEY1}')
    real, other, calls = new._fernet.rotate, database.connect(path), []

    def rotate_with_one_concurrent_save(token):
        renewed = real(token)
        if not calls:
            calls.append(1)
            SecretsStore(f'{KEY2},{KEY1}').set(other, 'routing.api_key', 'NOUVELLE-valeur-999999', DEST, actor=ALICE)
        return renewed

    new._fernet.rotate = rotate_with_one_concurrent_save
    first = new.rotate(conn, actor=CLI)
    assert first['skipped'] == 1 and first['secrets'] == 1                   # une ligne laissée telle quelle, signalée
    assert new.get(conn, 'routing.api_key', DEST) == 'NOUVELLE-valeur-999999'
    audit_row = conn.execute("SELECT details FROM audit_log WHERE action = 'secret.rotate'").fetchone()[0]
    assert json.loads(audit_row)['skipped'] == 1
    new._fernet.rotate = real
    second = new.rotate(conn, actor=CLI)
    other.close()
    assert second == {'secrets': 2, 'totp': 0}                               # plus rien d'ignoré
    assert SecretsStore(KEY2).verify_all(conn) == 2


def test_rotation_never_counts_a_row_it_did_not_write(conn, path):
    SecretsStore(KEY1).set(conn, 'smtp.password', PASSWORD, SMTP, actor=ALICE)
    new = SecretsStore(f'{KEY2},{KEY1}')
    real, other = new._fernet.rotate, database.connect(path)

    def always_raced(token):
        renewed = real(token)
        other.execute("UPDATE secrets SET ciphertext = ?", (Fernet(KEY1.encode()).encrypt(b'autre'),))
        other.commit()
        return renewed

    new._fernet.rotate = always_raced
    result = new.rotate(conn, actor=CLI)
    other.close()
    assert result['secrets'] == 0 and result['skipped'] == 1


def _service_start(data_dir, keys):
    """Démarrage « service » (gunicorn kml_viewer:app) : l'import crée l'application."""
    env = {**os.environ, 'DATA_DIR': str(data_dir), 'FLASK_ENV': 'development', 'PYTHONPATH': ROOT, 'SECRETS_KEY': keys}
    env.pop('FLASK_RUN_FROM_CLI', None)
    return subprocess.run([sys.executable, '-c', 'import kml_viewer'], capture_output=True, text=True, cwd=ROOT, env=env, timeout=120)


@pytest.mark.parametrize('lost_key', [KEY2, '', 'pas-une-cle'], ids=['mauvaise-cle', 'sans-cle', 'cle-malformee'])
def test_end_to_end_lost_key_service_refuses_then_secrets_reset_then_service_starts(tmp_path, lost_key):
    _store_with_real_cli_stack(tmp_path, KEY1)
    refused = _service_start(tmp_path, lost_key)                               # 1. le service refuse de démarrer
    assert refused.returncode != 0 and 'Démarrage refusé' in refused.stderr
    assert not forbidden_in(refused.stdout + refused.stderr, PASSWORD, lost_key or None)
    reset = _flask_cli(tmp_path, lost_key, 'secrets-reset', '--yes')           # 2. la CLI de secours s'exécute malgré la clé perdue
    assert reset.returncode == 0, (reset.stdout + reset.stderr)[-500:]
    assert not forbidden_in(reset.stdout + reset.stderr, PASSWORD, lost_key or None)
    c = sqlite3.connect(tmp_path / 'kmlviewer.sqlite3')
    assert c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 0
    row = c.execute("SELECT actor_type, actor_id FROM audit_log WHERE action = 'secret.reset'").fetchone()
    c.close()
    assert tuple(row) == ('cli', None)
    assert _service_start(tmp_path, KEY3).returncode == 0                       # 3. le service redémarre avec une nouvelle clé


def test_the_rescue_mode_is_never_available_to_the_service_process(tmp_path):
    """Seule la CLI `flask <commande>` (hors `run`) tolère une clé perdue : import direct et `flask run` refusent."""
    _store_with_real_cli_stack(tmp_path, KEY1)
    assert _service_start(tmp_path, KEY2).returncode != 0
    env = {**os.environ, 'DATA_DIR': str(tmp_path), 'FLASK_ENV': 'development', 'PYTHONPATH': ROOT, 'SECRETS_KEY': KEY2}
    try:
        result = subprocess.run([sys.executable, '-m', 'flask', '--app', 'kml_viewer', 'run', '--port', '0'], capture_output=True,
                                text=True, cwd=ROOT, env=env, timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail("`flask run` a démarré avec une clé perdue : le mode secours fuit vers le service")
    assert result.returncode != 0 and 'Démarrage refusé' in result.stderr


def test_in_rescue_mode_other_commands_cannot_read_or_rotate_secrets(tmp_path):
    _store_with_real_cli_stack(tmp_path, KEY1)
    result = _flask_cli(tmp_path, KEY2, 'secrets-rotate')
    assert result.returncode != 0 and 'secrets-reset' in result.stderr and not forbidden_in(result.stdout + result.stderr, PASSWORD)
    c = sqlite3.connect(tmp_path / 'kmlviewer.sqlite3')
    assert c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 1          # rien n'a bougé
    c.close()


def test_rescue_secrets_reset_asks_for_confirmation_too(tmp_path):
    _store_with_real_cli_stack(tmp_path, KEY1)
    env = {**os.environ, 'DATA_DIR': str(tmp_path), 'FLASK_ENV': 'development', 'PYTHONPATH': ROOT, 'SECRETS_KEY': KEY2}
    aborted = subprocess.run([sys.executable, '-m', 'flask', '--app', 'kml_viewer', 'secrets-reset'], input='n\n', capture_output=True,
                             text=True, cwd=ROOT, env=env, timeout=120)
    assert aborted.returncode != 0
    c = sqlite3.connect(tmp_path / 'kmlviewer.sqlite3')
    assert c.execute("SELECT COUNT(*) FROM secrets").fetchone()[0] == 1
    c.close()
