"""
Cas limites du limiteur de débit (T6, ADR-0006, EF-S3, CA-7, CA-32, CA-46) écrits depuis la spec.

Complète test_ratelimit.py : retry_after exact sur toutes les branches, adversaire qui calibre ses
accès, horloge proche de zéro, concurrence sur les compartiments et sur hit_all, saturation de la LRU.
"""

import random
import threading

import pytest

from app.ratelimit import (ACCOUNT_LRU_SIZE, BucketLimiter, Limiters, Limits, LruLimiter, hit_all,
                           normalize_key)

W = 900


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _both(limit=5, window=W, clock=None):
    clock = clock or Clock()
    return [BucketLimiter(limit, window, clock=clock, secret=b'k' * 32), LruLimiter(limit, window, clock=clock)], clock


@pytest.fixture(params=['bucket', 'lru'])
def make(request):
    def factory(limit=5, window=W, start=10_000.0, **kw):
        clock = Clock(start)
        if request.param == 'bucket':
            return BucketLimiter(limit, window, clock=clock, secret=b'k' * 32, **kw), clock
        return LruLimiter(limit, window, clock=clock, **kw), clock
    return factory


# --- CA-7 : garantie « seuil par fenêtre fixe, 2 × seuil sur fenêtre glissante » ---------------

@pytest.mark.parametrize('seed', range(20))
def test_random_traffic_never_breaks_the_two_guarantees(make, seed):
    """Trafic aléatoire (rafales et silences) : ≤ seuil par fenêtre fixe, ≤ 2 × seuil par fenêtre glissante."""
    rng = random.Random(seed)
    limit = rng.choice([1, 3, 5, 20])
    limiter, clock = make(limit=limit)
    t, accepted = clock.t, []
    for _ in range(3000):
        t += rng.choice([0, 0.1, 1, 5, 30, 200, 1500]) * rng.random()
        clock.t = t
        if limiter.hit('k')[0]:
            accepted.append(t)
    per_fixed = {}
    for a in accepted:
        per_fixed[int(a // W)] = per_fixed.get(int(a // W), 0) + 1
    assert max(per_fixed.values(), default=0) <= limit
    for start in accepted:
        assert sum(1 for a in accepted if start <= a < start + W) <= 2 * limit


@pytest.mark.parametrize('cadence', [1, 13, 60])
def test_attacker_hitting_at_every_instant_is_held_to_two_times_the_limit(make, cadence):
    limiter, clock = make(limit=5)
    accepted = []
    for step in range(0, 6 * W, cadence):
        clock.t = 10_000.0 + step
        if limiter.hit('k')[0]:
            accepted.append(clock.t)
    for start in accepted:
        assert sum(1 for a in accepted if start <= a < start + W) <= 10
    assert len(accepted) <= 5 * 7        # 6 fenêtres : jamais plus de 5 par fenêtre fixe touchée


def test_attacker_that_waits_exactly_retry_after_stays_within_bounds(make):
    """Adversaire optimal : réessaie à la seconde annoncée par retry_after, indéfiniment."""
    limiter, clock = make(limit=5)
    accepted = []
    while clock.t < 10_000 + 8 * W:
        allowed, retry = limiter.hit('k')
        if allowed:
            accepted.append(clock.t)
        else:
            clock.t += retry
            continue
        clock.t += 0.01
    for start in accepted:
        assert sum(1 for a in accepted if start <= a < start + W) <= 10


# --- retry_after : exact, sur toutes les branches -----------------------------------------------

@pytest.mark.parametrize('seed', range(30))
def test_retry_after_is_the_first_second_where_the_hit_is_accepted(make, seed):
    """Après un refus, `retry_after` est refusé une seconde plus tôt et accepté à l'échéance (états aléatoires)."""
    rng = random.Random(seed)
    limit = rng.choice([1, 2, 5, 9])
    limiter, clock = make(limit=limit)
    t = 10_000.0 + rng.random() * W
    for _ in range(rng.randint(limit, 4 * limit)):
        t += rng.random() * W / (3 * limit) * rng.choice([0.1, 1, 2])
        clock.t = t
        limiter.hit('k')
    clock.t = t
    for _ in range(100):                           # on s'assure d'être bloqué
        allowed, retry = limiter.hit('k')
        if not allowed:
            break
        clock.t += 1
    else:
        pytest.skip("jamais bloqué")
    assert retry >= 1
    now = clock.t
    clock.t = now + retry - 1
    assert limiter.hit('k')[0] is False, f"accepté {retry - 1} s après le refus, avant l'échéance annoncée ({retry})"
    clock.t = now + retry
    assert limiter.hit('k')[0] is True, f"toujours refusé à l'échéance annoncée ({retry} s)"


def test_retry_after_when_blocked_by_previous_window_with_room_left_in_the_current_one(make):
    """Branche « courante < seuil » : 5 en fin de fenêtre N, 1 en début de N+1, puis refus pondéré par N."""
    limiter, clock = make(limit=5)
    start = (clock.t // W + 1) * W
    for i in range(5):
        clock.t = start + W - 50 + i
        assert limiter.hit('k')[0]
    clock.t = start + W + 1
    assert limiter.hit('k')[0] is True               # 5 × (1 - 1/900) + 0 < 5
    clock.t = start + W + 2
    allowed, retry = limiter.hit('k')                # 5 × (1 - 2/900) + 1 > 5
    assert allowed is False and 1 <= retry <= W
    clock.t += retry - 1
    assert limiter.hit('k')[0] is False
    clock.t += 1
    assert limiter.hit('k')[0] is True


# --- Horloge ---------------------------------------------------------------------------------

@pytest.mark.parametrize('start', [0.0, 0.5, 900.0, 1e9, 1.7e9, 1e12])
def test_limit_holds_whatever_the_clock_origin(make, start):
    """time.monotonic() peut être proche de 0 juste après le démarrage de la machine."""
    limiter, clock = make(limit=5, start=start)
    results = []
    for i in range(8):
        clock.t = start + i
        results.append(limiter.hit('k')[0])
    assert results == [True] * 5 + [False] * 3


def test_fractional_and_integer_clocks_agree(make):
    limiter, _ = make(limit=2, start=0.0)
    assert [limiter.hit('k')[0] for _ in range(3)] == [True, True, False]


def test_keys_are_independent(make):
    limiter, _ = make(limit=1)
    assert limiter.hit('a')[0] and limiter.hit('a')[0] is False
    assert limiter.hit('b')[0] is True


# --- CA-46 (préconditions au niveau module) : 20 requêtes simultanées ---------------------------

@pytest.mark.parametrize('kind', ['bucket', 'lru'])
def test_twenty_simultaneous_requests_get_exactly_the_limit(kind):
    """Les 20 requêtes simultanées d'un même identifiant : `limit` passent, toutes les autres sont refusées."""
    clock = Clock()
    limiter = (BucketLimiter(5, W, clock=clock, secret=b'k' * 32) if kind == 'bucket' else LruLimiter(5, W, clock=clock))
    barrier, outcomes = threading.Barrier(20), []

    def request():
        barrier.wait(10)
        outcomes.append(limiter.hit('victime')[0])

    threads = [threading.Thread(target=request) for _ in range(20)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert sorted(outcomes) == [False] * 15 + [True] * 5


def test_bucket_limiter_is_thread_safe():
    limiter = BucketLimiter(50, W, clock=Clock(), secret=b'k' * 32)
    accepted, lock = [], threading.Lock()

    def work():
        for _ in range(100):
            if limiter.hit('shared')[0]:
                with lock:
                    accepted.append(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(accepted) == 50


def test_concurrent_hit_all_never_lets_more_than_the_identifier_limit_through():
    """IP large, identifiant à 5 : 40 requêtes concurrentes, 5 passent."""
    clock = Clock()
    ip, ident = LruLimiter(1000, W, clock=clock), BucketLimiter(5, W, clock=clock, secret=b'k' * 32)
    barrier, passed, lock = threading.Barrier(40), [], threading.Lock()

    def request(i):
        barrier.wait(10)
        if hit_all((ip, f'10.0.0.{i}'), (ident, 'Victime'))[0]:
            with lock:
                passed.append(i)

    threads = [threading.Thread(target=request, args=(i,)) for i in range(40)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(passed) == 5


def test_hit_all_order_is_the_order_given_and_empty_is_allowed():
    assert hit_all() == (True, 0)
    clock = Clock()
    first, second = LruLimiter(1, W, clock=clock), LruLimiter(1, W, clock=clock)
    hit_all((first, 'x'), (second, 'y'))
    assert hit_all((second, 'y'), (first, 'z')) [0] is False
    assert first.hit('z')[0] is True       # 'z' n'a pas été consommé : le refus de `second` a arrêté la boucle


# --- CA-32 : LRU saturée -----------------------------------------------------------------------

def test_lru_under_saturation_stays_bounded_and_keeps_active_keys_counted():
    """Une clé qui frappe sans cesse reste la plus récente : les autres clés ne l'évincent pas."""
    clock = Clock()
    limiter = LruLimiter(3, W, max_entries=50, clock=clock)
    for _ in range(3):
        assert limiter.hit('attaquant')[0]
    for i in range(5000):
        limiter.hit(f'bruit-{i}')
        if i % 10 == 0:
            limiter.hit('attaquant')               # rafraîchit sa place
    assert limiter.footprint() <= 50
    assert limiter.hit('attaquant')[0] is False


def test_lru_evicted_key_restarts_at_zero_documented_tradeoff():
    """Contrepartie documentée (ADR-0006) : une IP évincée repart de zéro ; seul gain pour qui a de nombreuses IP."""
    clock = Clock()
    limiter = LruLimiter(1, W, max_entries=2, clock=clock)
    limiter.hit('a')
    limiter.hit('b')
    limiter.hit('c')
    assert limiter.hit('a')[0] is True


def test_lru_and_bucket_footprint_under_concurrent_flood():
    clock = Clock()
    lru, bucket = LruLimiter(5, W, max_entries=100, clock=clock), BucketLimiter(5, W, buckets=64, clock=clock)

    def flood(n):
        for i in range(2000):
            lru.hit(f'{n}-{i}')
            bucket.hit(f'{n}-{i}')

    threads = [threading.Thread(target=flood, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert lru.footprint() == 100 and bucket.footprint() == 64


def test_default_account_family_size_and_independence_of_families():
    assert ACCOUNT_LRU_SIZE == 100_000
    clock = Clock()
    lim = Limiters(Limits(), clock=clock, buckets=64, ip_entries=10, account_entries=10, secret=b's' * 32)
    for _ in range(5):
        lim.password_identifier.hit('alice')
    assert lim.password_identifier.hit('alice')[0] is False
    # Mêmes clés dans les autres familles : compteurs séparés
    assert lim.forgot_identifier.hit('alice')[0] is True
    assert lim.password_ip.hit('alice')[0] is True
    assert lim.mfa_account.hit('alice')[0] is True


def test_the_two_bucket_families_share_a_secret_but_not_their_counters():
    clock = Clock()
    lim = Limiters(Limits(forgot_per_identifier=1), clock=clock, secret=b's' * 32)
    assert lim.forgot_identifier.hit('x')[0] and lim.forgot_identifier.hit('x')[0] is False
    assert lim.password_identifier.hit('x')[0] is True


# --- Normalisation de la clé saisie (EF-S3) ----------------------------------------------------

@pytest.mark.parametrize('variants', [
    ['Alice', 'ALICE', 'alice', '  alice  ', 'ＡＬＩＣＥ', 'Alice\t'],
    ['Straße', 'STRASSE', 'strasse'],
    ['julien@Example.ORG', 'ＪＵＬＩＥＮ@example.org'],
    ['ﬁnal', 'FINAL', 'final'],
    ['école', 'école', 'ÉCOLE'],                  # é décomposé / composé
])
def test_every_spelling_of_an_identifier_shares_one_counter(variants):
    clock = Clock()
    limiter = BucketLimiter(len(variants) - 1, W, clock=clock, secret=b'k' * 32)
    assert [limiter.hit(v)[0] for v in variants] == [True] * (len(variants) - 1) + [False]


def test_normalize_key_is_idempotent_and_total():
    samples = ['Alice', 'ＡＬＩＣＥ', 'Straße', 'İstanbul', 'ǅ', 'Ω', 'ﬁ', 'é', '', '   ', '\x00', 'a' * 10_000]
    for s in samples:
        assert normalize_key(normalize_key(s)) == normalize_key(s), repr(s)
    assert normalize_key(None) == 'none' and normalize_key(42) == '42'       # str() d'abord : jamais d'exception


def test_empty_identifier_is_still_limited(make):
    """Un identifiant vide ne doit pas échapper au limiteur."""
    limiter, _ = make(limit=2)
    assert [limiter.hit('')[0] for _ in range(3)] == [True, True, False]


def test_limits_replace_revalidates():
    import dataclasses
    assert dataclasses.replace(Limits(), password_per_identifier=3).password_per_identifier == 3
    with pytest.raises(ValueError):
        dataclasses.replace(Limits(), password_per_identifier=2)


def test_both_implementations_agree_on_the_same_traffic():
    """Les deux familles implémentent le même modèle : mêmes décisions et mêmes retry_after."""
    (bucket, lru), clock = _both(limit=4)
    rng = random.Random(7)
    t = clock.t
    for _ in range(500):
        t += rng.random() * 120
        clock.t = t
        assert bucket.hit('k') == lru.hit('k')


# --- Verrou : course forcée ---------------------------------------------------------------------

class _YieldingClock(Clock):
    """Cède le GIL à chaque lecture : sans verrou autour de lecture-modification-écriture, les accès se mélangent."""

    def __call__(self):
        import time
        time.sleep(0.0002)
        return self.t


@pytest.mark.parametrize('kind', ['bucket', 'lru'])
def test_the_lock_really_serializes_the_read_modify_write(kind):
    clock = _YieldingClock()
    limiter = (BucketLimiter(10, W, clock=clock, secret=b'k' * 32) if kind == 'bucket' else LruLimiter(10, W, clock=clock))
    accepted, lock = [], threading.Lock()

    def work():
        for _ in range(10):
            if limiter.hit('k')[0]:
                with lock:
                    accepted.append(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert len(accepted) == 10
