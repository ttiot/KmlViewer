"""
Cas limites de la politique, du hachage et du sémaphore (T6, EF-1.4, EF-1.5, ADR-0002, EF-S5).

Complète test_passwords.py : bornes NFKC dans les deux sens, identité normalisée, mots de passe
non rognés, attente du sémaphore, fuite de place, journaux.
"""

import logging
import threading
import time

import pytest
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerifyMismatchError

from app.auth import passwords

GOOD = 'cheval-agrafe-lune-42'


def _code(password, identities=()):
    with pytest.raises(passwords.PolicyError) as exc:
        passwords.check_policy(password, identities)
    return exc.value.code


class _Spy:
    def __init__(self, error=None):
        self.calls, self.error = [], error

    def hash(self, password):
        self.calls.append(('hash', password))
        return '$argon2id$stub'

    def verify(self, hashed, password):
        self.calls.append(('verify', password))
        if self.error:
            raise self.error
        raise VerifyMismatchError()

    def check_needs_rehash(self, hashed):
        return False


# --- NFKC et bornes 12 / 128 -----------------------------------------------------------------

@pytest.mark.parametrize('password,verdict', [
    ('x' * 11, 'too_short'), ('x' * 11 + 'y', None),   # 'x' * 12 est dans la liste publique (EF-1.4b)
    ('x' * 128, None), ('x' * 129, 'too_long'),
    ('ﬃ' * 4, None), ('ﬃ' * 3 + 'abc', None), ('ﬃ' * 3 + 'ab', 'too_short'),           # 12 / 11 après NFKC
    ('ﬃ' * 42 + 'ab', None), ('ﬃ' * 42 + 'abc', 'too_long'),                           # 128 / 129 après NFKC
    ('e\u0301' * 6, 'too_short'),                                                        # 12 brutes, 6 après NFKC
    ('e\u0301' * 12, None),                                                              # 24 brutes, 12 après
    ('e\u0301' * 64, None), ('e\u0301' * 128, None), ('e\u0301' * 129, 'too_long'),   # 64 / 128 / 129 après NFKC
    ('ｘ' * 11 + 'ｙ', None), ('ｘ' * 11, 'too_short'), ('ｘ' * 128, None), ('ｘ' * 129, 'too_long'),
    ('①②③④⑤⑥⑦⑧⑨⑩⑪z', None), ('㈱' * 4, None),                                                  # ① → 1 ; ㈱ → (株) = 3
])
def test_length_is_measured_after_nfkc(password, verdict):
    if verdict is None:
        passwords.check_policy(password)
    else:
        assert _code(password) == verdict


def test_too_long_is_reported_before_the_costly_checks():
    """Aucune règle de composition, mais le refus de longueur précède la liste de mots courants."""
    assert _code('password' * 40) == 'too_long'


def test_raw_limit_is_before_nfkc_and_independent_of_content():
    assert _code('ｘ' * 1025) == 'too_long'
    start = time.monotonic()
    assert _code('ﬃ' * 5_000_000) == 'too_long'
    assert time.monotonic() - start < 1            # n'a pas normalisé 15 millions de caractères


def test_surrogates_and_control_characters_do_not_crash_the_policy():
    for value in ('\ud800' * 12, '\x00' * 12, 'a\x00' * 12, '‮' * 12, '👩‍👩‍👧‍👦' * 3):
        try:
            passwords.check_policy(value)
        except passwords.PolicyError:
            pass


_SURROGATE = 'a' * 11 + '\ud800'    # str valide en Python (ex. issue d'un JSON "\ud800"), non encodable en UTF-8


def test_verify_with_lone_surrogate_is_a_plain_refusal():
    hashed = passwords.hash_password(GOOD)
    assert passwords.verify_password(hashed, _SURROGATE) == (False, None)


def test_policy_and_hashing_agree_on_a_lone_surrogate():
    try:
        passwords.check_policy(_SURROGATE)
    except passwords.PolicyError:
        return                                       # refusé par la politique : cohérent
    passwords.hash_password(_SURROGATE)              # accepté par la politique ⇒ doit pouvoir être haché


# --- Identité (égalité identifiant / e-mail) ---------------------------------------------------

@pytest.mark.parametrize('password,identity', [
    ('Julien.Michaux@Example.org', 'julien.michaux@example.org'),
    ('ＪＵＬＩＥＮ.ＭＩＣＨＡＵＸ', 'julien.michaux'),                  # pleine chasse vs ASCII
    ('julien.michaux', '  Julien.Michaux  '),                          # blancs de bord de l'identité
    ('Maßstab-Straße-12', 'MASSSTAB-STRASSE-12'),                       # casefold ß → ss
    ('école-primaire-42', 'e\u0301cole-primaire-42'),                  # composé vs décomposé
])
def test_identity_equality_is_nfkc_and_casefold(password, identity):
    if len(passwords.normalize(password)) < passwords.MIN_LENGTH:
        pytest.skip("trop court : refusé plus tôt pour une autre raison")
    assert _code(password, [identity]) == 'same_as_identity'


def test_identity_with_surrounding_spaces_matches_only_if_the_password_has_none():
    """L'identité est rognée, pas le mot de passe : ' alice.longname ' n'est pas 'alice.longname'."""
    passwords.check_policy(' alice.longname ', identities=['alice.longname'])


def test_identity_check_ignores_non_matching_and_substring():
    passwords.check_policy('julien.michaux.2026!', identities=['julien.michaux'])
    passwords.check_policy('xjulien.michaux', identities=['julien.michaux'])


def test_identity_accepts_any_iterable_and_non_string_values():
    assert _code('12345678-12345678', identities=iter(['12345678-12345678'])) == 'same_as_identity'
    assert _code('1234567890123', identities=[1234567890123]) == 'same_as_identity'
    passwords.check_policy(GOOD, identities=(None, '', 0))


def test_short_identity_cannot_leak_through_the_length_rule():
    """Identifiant de 5 caractères : le mot de passe identique est refusé pour sa longueur."""
    assert _code('alice', ['alice']) == 'too_short'


# --- Mots de passe courants --------------------------------------------------------------------

def test_common_list_is_matched_whatever_the_unicode_form():
    entry = next(e for e in sorted(passwords.COMMON_PASSWORDS))
    assert _code(entry.upper()) == 'too_common'
    assert _code(''.join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalnum() else c for c in entry)) in ('too_common', 'too_short')


def test_is_common_agrees_with_check_policy():
    assert passwords.is_common('PASSWORD1234') and not passwords.is_common(GOOD)


# --- Mots de passe non rognés -------------------------------------------------------------------

def test_surrounding_whitespace_is_significant():
    spaced = ' ' + GOOD + ' '
    hashed = passwords.hash_password(spaced)
    assert passwords.verify_password(hashed, spaced)[0] is True
    assert passwords.verify_password(hashed, GOOD)[0] is False
    assert passwords.verify_password(passwords.hash_password(GOOD), spaced)[0] is False


# --- Hachage aux bornes ------------------------------------------------------------------------

@pytest.mark.parametrize('password', ['x' * 12, 'x' * 128, 'ﬃ' * 42 + 'ab', 'e\u0301' * 12])
def test_roundtrip_at_the_limits(password):
    hashed = passwords.hash_password(password)
    assert passwords.verify_password(hashed, password) == (True, None)


def test_verify_hashes_up_to_128_and_never_above(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(passwords, '_HASHER', spy)
    assert passwords.verify_password('$argon2id$x', 'y' * 128) == (False, None)
    assert passwords.verify_password('$argon2id$x', 'y' * 129) == (False, None)
    assert [c[0] for c in spy.calls] == ['verify']


def test_verify_receives_the_normalized_form(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(passwords, '_HASHER', spy)
    passwords.verify_password('$argon2id$x', 'ＡＢＣ')
    assert spy.calls == [('verify', 'ABC')]


def test_unknown_account_and_wrong_password_cost_the_same_calls(monkeypatch):
    """Même nombre de vérifications argon2 : le temps de réponse ne distingue pas les deux cas."""
    spy_unknown, spy_wrong = _Spy(), _Spy()
    monkeypatch.setattr(passwords, '_HASHER', spy_unknown)
    passwords.verify_password(None, GOOD)
    monkeypatch.setattr(passwords, '_HASHER', spy_wrong)
    passwords.verify_password('$argon2id$x', GOOD)
    assert [c[0] for c in spy_unknown.calls] == [c[0] for c in spy_wrong.calls] == ['verify']


def test_non_string_stored_hash_is_treated_as_unknown(monkeypatch):
    spy = _Spy()
    monkeypatch.setattr(passwords, '_HASHER', spy)
    for stored in (b'$argon2id$x', 12, [], {}):
        assert passwords.verify_password(stored, GOOD) == (False, None)
    assert len(spy.calls) == 4


# --- Rehachage ---------------------------------------------------------------------------------

@pytest.mark.parametrize('kind', [Type.I, Type.D])
def test_hashes_of_another_argon2_variant_are_accepted_and_upgraded_to_argon2id(kind):
    old = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, type=kind).hash(passwords.normalize(GOOD))
    ok, new_hash = passwords.verify_password(old, GOOD)
    assert ok is True and new_hash and new_hash.startswith('$argon2id$')


def test_rehash_needs_the_gate_and_does_not_leak_a_slot(monkeypatch):
    gate = passwords.HashGate(slots=1, wait_s=0.2)
    monkeypatch.setattr(passwords, '_gate', gate)
    old = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(passwords.normalize(GOOD))
    ok, new_hash = passwords.verify_password(old, GOOD)
    assert ok and new_hash
    with gate.slot():          # la seule place est libre : vérification + rehachage l'ont bien rendue
        pass


# --- Sémaphore ---------------------------------------------------------------------------------

def test_a_waiter_gets_the_slot_if_one_frees_up_within_the_wait(monkeypatch):
    """Attente ≤ wait_s : une place libérée à temps sert le demandeur au lieu de le refuser."""
    monkeypatch.setattr(passwords, '_HASHER', _Spy())
    gate = passwords.HashGate(slots=1, wait_s=3)
    monkeypatch.setattr(passwords, '_gate', gate)
    holder = gate.slot()
    holder.__enter__()
    threading.Timer(0.2, lambda: holder.__exit__(None, None, None)).start()
    start = time.monotonic()
    assert passwords.verify_password('$argon2id$x', GOOD) == (False, None)
    assert 0.15 <= time.monotonic() - start < 2


def test_hashbusy_does_not_leak_or_over_release_places(monkeypatch):
    gate = passwords.HashGate(slots=2, wait_s=0.05)
    monkeypatch.setattr(passwords, '_gate', gate)
    held = [gate.slot(), gate.slot()]
    [h.__enter__() for h in held]
    for _ in range(20):
        with pytest.raises(passwords.HashBusy), gate.slot():
            pass
    [h.__exit__(None, None, None) for h in held]
    # Toujours exactement 2 places : un surplus de release lèverait ValueError, une fuite ferait refuser
    a, b = gate.slot(), gate.slot()
    a.__enter__()
    b.__enter__()
    with pytest.raises(passwords.HashBusy), gate.slot():
        pass
    a.__exit__(None, None, None)
    b.__exit__(None, None, None)


def test_unexpected_error_inside_the_hasher_releases_the_slot(monkeypatch):
    gate = passwords.HashGate(slots=1, wait_s=0.1)
    monkeypatch.setattr(passwords, '_gate', gate)
    monkeypatch.setattr(passwords, '_HASHER', _Spy(error=MemoryError()))
    with pytest.raises(MemoryError):
        passwords.verify_password('$argon2id$x', GOOD)
    with gate.slot():
        pass


def test_hash_and_verify_share_the_same_two_places(monkeypatch):
    """Mélange de hachages, de vérifications et de hash factices : jamais plus de 2 en parallèle."""
    state = {'now': 0, 'peak': 0}
    lock = threading.Lock()

    class Tracked(_Spy):
        def _work(self):
            with lock:
                state['now'] += 1
                state['peak'] = max(state['peak'], state['now'])
            time.sleep(0.03)
            with lock:
                state['now'] -= 1

        def hash(self, password):
            self._work()
            return '$argon2id$stub'

        def verify(self, hashed, password):
            self._work()
            raise VerifyMismatchError()

    monkeypatch.setattr(passwords, '_HASHER', Tracked())
    monkeypatch.setattr(passwords, '_gate', passwords.HashGate(slots=2, wait_s=10))
    jobs = [lambda: passwords.hash_password(GOOD), lambda: passwords.verify_password(None, GOOD),
            lambda: passwords.verify_password('$argon2id$x', GOOD)] * 6
    threads = [threading.Thread(target=job) for job in jobs]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert state['peak'] == 2


def test_busy_responses_are_identical_whatever_the_account(monkeypatch):
    """EF-1.5 : le refus de saturation ne distingue ni compte inconnu, ni compte existant."""
    gate = passwords.HashGate(slots=1, wait_s=0.05)
    monkeypatch.setattr(passwords, '_gate', gate)
    holder = gate.slot()
    holder.__enter__()
    try:
        outcomes = []
        for stored in (None, '', passwords._DUMMY_HASH, 'garbage'):
            with pytest.raises(passwords.HashBusy) as exc:
                passwords.verify_password(stored, GOOD)
            outcomes.append((type(exc.value), str(exc.value)))
        assert len(set(outcomes)) == 1
    finally:
        holder.__exit__(None, None, None)


# --- Journaux (EF-S5) --------------------------------------------------------------------------

def test_nothing_sensitive_is_ever_logged(monkeypatch, caplog):
    secret = 'secret-ultra-confidentiel-777'
    with caplog.at_level(logging.DEBUG):
        hashed = passwords.hash_password(secret)
        passwords.verify_password(hashed, secret)
        passwords.verify_password(hashed, secret + 'x')
        passwords.verify_password('corrompu', secret)
        passwords.verify_password(None, secret)
        monkeypatch.setattr(passwords, '_gate', passwords.HashGate(slots=1, wait_s=0.01))
        held = passwords._gate.slot()
        held.__enter__()
        with pytest.raises(passwords.HashBusy):
            passwords.verify_password(hashed, secret)
        held.__exit__(None, None, None)
        with pytest.raises(passwords.PolicyError):
            passwords.check_policy(secret[:5])
    assert secret not in caplog.text and hashed not in caplog.text and 'corrompu' not in caplog.text
