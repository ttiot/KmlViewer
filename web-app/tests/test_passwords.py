"""Mots de passe : politique, argon2id, rehachage, sémaphore de hachage (T6, CA-8, EF-1.4/1.5, ADR-0002)."""

import logging
import threading
import time

import pytest
from argon2 import PasswordHasher

from app.auth import passwords

GOOD = 'cheval-agrafe-lune-42'


# --- Politique (CA-8, m5) --------------------------------------------------------------------

@pytest.mark.parametrize('password', ['a' * 11, '', 'court', '１２３４５６７８９０１'])  # dernier : 11 pleine chasse
def test_too_short_is_refused(password):
    with pytest.raises(passwords.PolicyError) as exc:
        passwords.check_policy(password)
    assert exc.value.code == 'too_short'


def test_normalized_length_decides_both_ways():
    passwords.check_policy('ﬃ' * 4)                                # 4 caractères bruts, 12 après NFKC : accepté
    with pytest.raises(passwords.PolicyError) as exc:
        passwords.check_policy('ﬃ' * 3 + 'ab')                    # 5 + ... = 11 après NFKC
    assert exc.value.code == 'too_short'


def test_max_length_is_128_after_nfkc():
    passwords.check_policy('x' * 127 + 'y')                   # 128 : accepté
    with pytest.raises(passwords.PolicyError) as exc:
        passwords.check_policy('x' * 129)
    assert exc.value.code == 'too_long'
    with pytest.raises(passwords.PolicyError):
        passwords.check_policy('ﬃ' * 50)                           # 50 brutes, 150 après NFKC
    with pytest.raises(passwords.PolicyError):
        passwords.check_policy('x' * 5_000_000)                    # refusé avant même NFKC


def test_no_composition_rule():
    passwords.check_policy('aaaaaaaaaaaaaaaaaaaaaaaa')  # ni majuscule, ni chiffre, ni symbole : accepté


def test_equal_to_identifier_or_email_is_refused_case_insensitively():
    with pytest.raises(passwords.PolicyError) as exc:
        passwords.check_policy('Julien.Michaux', identities=['julien.michaux', 'j@example.org'])
    assert exc.value.code == 'same_as_identity'
    with pytest.raises(passwords.PolicyError):
        passwords.check_policy('JULIEN@EXAMPLE.ORG', identities=['julien@example.org'])
    passwords.check_policy('julien.michaux-2026', identities=['julien.michaux'])   # seulement l'égalité
    passwords.check_policy(GOOD, identities=[None, ''])                            # identités vides ignorées


def test_common_passwords_are_refused_after_nfkc_and_casefold():
    for candidate in ('password1234', 'PassWord1234', 'ｐａｓｓｗｏｒｄ１２３４', 'Azerty123456', 'qwertyuiop123'):
        with pytest.raises(passwords.PolicyError) as exc:
            passwords.check_policy(candidate)
        assert exc.value.code == 'too_common', candidate


def test_non_string_is_refused():
    for value in (None, 12345678901234, b'x' * 20, ['x'] * 20):
        with pytest.raises(passwords.PolicyError):
            passwords.check_policy(value)


# --- Hachage et vérification ------------------------------------------------------------------

def test_hash_is_argon2id_with_pinned_parameters():
    hashed = passwords.hash_password(GOOD)
    assert hashed.startswith('$argon2id$v=19$m=65536,t=3,p=4$')
    assert GOOD not in hashed
    assert hashed != passwords.hash_password(GOOD)   # sel aléatoire


def test_verify_accepts_and_rejects():
    hashed = passwords.hash_password(GOOD)
    assert passwords.verify_password(hashed, GOOD) == (True, None)
    assert passwords.verify_password(hashed, GOOD + 'x') == (False, None)
    assert passwords.verify_password(hashed, GOOD.upper()) == (False, None)


def test_password_is_normalized_before_hashing_and_verifying():
    hashed = passwords.hash_password('ｃｈｅｖａｌ-agrafe-42')    # pleine chasse
    assert passwords.verify_password(hashed, 'cheval-agrafe-42')[0] is True
    assert passwords.verify_password(hashed, 'ｃｈｅｖａｌ-agrafe-42')[0] is True


def test_hash_refuses_out_of_bounds():
    for bad in ('x' * 11, 'x' * 129, 'x' * 5000, None, 12):
        with pytest.raises(ValueError):
            passwords.hash_password(bad)


def test_verify_out_of_bounds_is_a_plain_refusal_without_hashing(monkeypatch):
    hashed = passwords.hash_password(GOOD)
    calls = []
    monkeypatch.setattr(passwords, '_HASHER', _Spy(calls))
    for bad in ('', 'x' * 129, 'x' * 5_000_000, None, 12, b'bytes'):
        assert passwords.verify_password(hashed, bad) == (False, None)
    assert calls == []


class _Spy:
    """Remplace le PasswordHasher : trace les appels et laisse tout échouer en mismatch."""

    def __init__(self, calls, delay=0.0, tracker=None):
        self.calls, self.delay, self.tracker = calls, delay, tracker

    def _enter(self, name):
        self.calls.append(name)
        if self.tracker is not None:
            self.tracker.enter()
        time.sleep(self.delay)
        if self.tracker is not None:
            self.tracker.leave()

    def hash(self, password):
        self._enter('hash')
        return '$argon2id$stub'

    def verify(self, hashed, password):
        from argon2.exceptions import VerifyMismatchError
        self._enter('verify')
        raise VerifyMismatchError()

    def check_needs_rehash(self, hashed):
        return False


def test_unknown_account_verifies_a_dummy_hash(monkeypatch):
    """Compte absent (None ou vide) : une vérification a lieu quand même, et le résultat est un refus."""
    calls = []
    monkeypatch.setattr(passwords, '_HASHER', _Spy(calls))
    assert passwords.verify_password(None, GOOD) == (False, None)
    assert passwords.verify_password('', GOOD) == (False, None)
    assert calls == ['verify', 'verify']


def test_dummy_hash_is_a_real_argon2id_hash_unrelated_to_any_password():
    assert passwords._DUMMY_HASH.startswith('$argon2id$')
    assert passwords.verify_password(passwords._DUMMY_HASH, GOOD)[0] is False


def test_unreadable_stored_hash_is_never_a_success_and_leaks_nothing(caplog):
    with caplog.at_level(logging.DEBUG):
        for corrupt in ('not-a-hash', '$argon2id$v=19$m=1', '$2b$12$' + 'a' * 53, 'x' * 500):
            assert passwords.verify_password(corrupt, GOOD) == (False, None)
    assert GOOD not in caplog.text and 'not-a-hash' not in caplog.text


def test_rehash_when_parameters_changed():
    old = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(passwords.normalize(GOOD))
    ok, new_hash = passwords.verify_password(old, GOOD)
    assert ok is True and new_hash and new_hash != old
    assert 'm=65536,t=3,p=4' in new_hash
    assert passwords.verify_password(new_hash, GOOD) == (True, None)      # plus de rehachage ensuite
    assert passwords.verify_password(old, 'faux-mot-de-passe-42') == (False, None)   # jamais sur un échec


# --- Sémaphore de hachage (m4) -----------------------------------------------------------------

class _Tracker:
    def __init__(self):
        self.lock, self.now, self.peak = threading.Lock(), 0, 0

    def enter(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def leave(self):
        with self.lock:
            self.now -= 1


def test_at_most_two_concurrent_hashes_even_under_load(monkeypatch):
    tracker = _Tracker()
    monkeypatch.setattr(passwords, '_HASHER', _Spy([], delay=0.05, tracker=tracker))
    monkeypatch.setattr(passwords, '_gate', passwords.HashGate(slots=2, wait_s=10))
    threads = [threading.Thread(target=passwords.verify_password, args=('$argon2id$x', GOOD)) for _ in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert tracker.peak == 2


def test_gate_default_is_two_slots_and_five_seconds():
    assert (passwords.HASH_SLOTS, passwords.HASH_WAIT_S) == (2, 5.0)
    assert passwords.HashGate()._wait_s == 5.0


def test_saturated_gate_raises_hashbusy_after_the_wait_and_hashes_nothing(monkeypatch, caplog):
    calls = []
    monkeypatch.setattr(passwords, '_HASHER', _Spy(calls))
    gate = passwords.HashGate(slots=2, wait_s=0.1)
    monkeypatch.setattr(passwords, '_gate', gate)
    held = [gate.slot(), gate.slot()]
    [h.__enter__() for h in held]
    try:
        started = time.monotonic()
        with pytest.raises(passwords.HashBusy):
            passwords.verify_password('$argon2id$x', GOOD)
        assert 0.09 <= time.monotonic() - started < 2
        with pytest.raises(passwords.HashBusy):
            passwords.verify_password(None, GOOD)               # hash factice compris
        with pytest.raises(passwords.HashBusy):
            passwords.hash_password(GOOD)
    finally:
        [h.__exit__(None, None, None) for h in held]
    assert calls == []
    # Les places sont rendues : le hachage repart
    assert passwords.verify_password('$argon2id$x', GOOD) == (False, None)


def test_slot_is_released_when_the_work_fails(monkeypatch):
    gate = passwords.HashGate(slots=1, wait_s=0.1)
    monkeypatch.setattr(passwords, '_gate', gate)
    with pytest.raises(RuntimeError), gate.slot():
        raise RuntimeError("boom")
    with gate.slot():
        pass


def test_lone_surrogate_is_refused_everywhere_without_error():
    """Surrogat isolé (JSON "\\ud800") : politique ⇒ refus propre ; hachage ⇒ ValueError ; login ⇒ refus ordinaire."""
    bad = 'a' * 11 + '\ud800'
    with pytest.raises(passwords.PolicyError) as exc:
        passwords.check_policy(bad)
    assert exc.value.code == 'invalid_characters'
    with pytest.raises(ValueError):
        passwords.hash_password(bad)
    assert passwords.verify_password(passwords.hash_password(GOOD), bad) == (False, None)
    assert passwords.verify_password(None, bad) == (False, None)
