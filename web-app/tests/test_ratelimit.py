"""Limiteur de débit (T6, ADR-0006, EF-S3, CA-7 et CA-32 sur le modèle à deux compteurs)."""

import threading

import pytest

from app import ratelimit
from app.ratelimit import BucketLimiter, Limiters, Limits, LruLimiter, hit_all, normalize_key

W = 15 * 60


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def drive(limiter, clock, key, times):
    """Un accès à chaque instant de `times` ; retourne les instants acceptés."""
    accepted = []
    for t in times:
        clock.t = t
        if limiter.hit(key)[0]:
            accepted.append(t)
    return accepted


@pytest.fixture(params=['bucket', 'lru'])
def make(request):
    def factory(limit=5, window=W, clock=None, **kw):
        clock = clock or Clock()
        if request.param == 'bucket':
            return BucketLimiter(limit, window, clock=clock, secret=b'k' * 32, **kw), clock
        return LruLimiter(limit, window, clock=clock, **kw), clock
    return factory


# --- CA-7 : seuil par fenêtre fixe, 2 × seuil sur fenêtre glissante -----------------------------

def test_sixth_failure_in_a_fixed_window_is_refused(make):
    limiter, clock = make(limit=5)
    start = (clock.t // W + 1) * W          # début exact d'une fenêtre fixe
    results = []
    for i in range(6):
        clock.t = start + i * 10
        results.append(limiter.hit('alice')[0])
    assert results == [True] * 5 + [False]


def test_never_more_than_limit_per_fixed_window_nor_twice_per_sliding_window(make):
    """Balayage du déphasage : accès toutes les 7 s pendant 4 fenêtres, pour 40 déphasages."""
    for phase in range(0, W, 23):
        limiter, clock = make(limit=5)
        base = (clock.t // W) * W + phase
        accepted = drive(limiter, clock, 'alice', [base + i * 7 for i in range(4 * W // 7)])
        per_fixed = {}
        for t in accepted:
            per_fixed[t // W] = per_fixed.get(t // W, 0) + 1
        assert max(per_fixed.values()) <= 5, phase
        for t0 in accepted:   # fenêtre glissante qui démarre sur chaque accès accepté
            assert sum(1 for t in accepted if t0 <= t < t0 + W) <= 10, phase


def test_burst_at_the_window_boundary_stays_within_twice_the_limit(make):
    limiter, clock = make(limit=5)
    boundary = (clock.t // W + 1) * W
    before = drive(limiter, clock, 'a', [boundary - 30 + i for i in range(10)])
    after = drive(limiter, clock, 'a', [boundary + i for i in range(10)])
    assert len(before) == 5 and len(after) <= 5
    assert len(before) + len(after) <= 10


def test_unknown_and_existing_identifiers_behave_the_same(make):
    """Le limiteur ne sait pas si un compte existe : même comportement pour toute clé."""
    for key in ('alice', 'inconnu-12345'):
        limiter, clock = make(limit=5)
        t0 = clock.t
        assert drive(limiter, clock, key, [t0 + i for i in range(8)]) == [t0 + i for i in range(5)]


def test_refused_hits_do_not_extend_the_block(make):
    """Aucun verrouillage définitif : marteler un compte refusé ne repousse pas le déblocage."""
    limiter, clock = make(limit=3)
    start = clock.t
    drive(limiter, clock, 'k', [start, start + 1, start + 2])
    _, retry = limiter.hit('k')
    for i in range(500):
        clock.t = start + 3 + i
        limiter.hit('k')
    clock.t = start + 3 + retry + W
    assert limiter.hit('k')[0] is True


def test_retry_after_is_accurate_and_positive(make):
    limiter, clock = make(limit=3)
    start = clock.t
    drive(limiter, clock, 'k', [start, start + 1, start + 2])
    allowed, retry = limiter.hit('k')
    assert allowed is False and 1 <= retry <= 2 * W
    clock.t += retry - 1
    assert limiter.hit('k')[0] is False       # un peu trop tôt
    clock.t += 1
    assert limiter.hit('k')[0] is True        # à l'échéance annoncée


def test_idle_longer_than_two_windows_resets(make):
    limiter, clock = make(limit=2)
    drive(limiter, clock, 'k', [clock.t, clock.t + 1])
    assert limiter.hit('k')[0] is False
    clock.t += 3 * W
    assert limiter.hit('k')[0] is True


def test_invalid_parameters():
    for bad in ({'limit': 0, 'window_s': 60}, {'limit': 1, 'window_s': 0}):
        with pytest.raises(ValueError):
            LruLimiter(**bad)
    with pytest.raises(ValueError):
        BucketLimiter(1, 60, buckets=0)
    with pytest.raises(ValueError):
        LruLimiter(1, 60, max_entries=0)


def test_thread_safety_never_exceeds_the_limit():
    clock = Clock()
    limiter = LruLimiter(50, W, clock=clock)
    accepted = []

    def work():
        for _ in range(100):
            if limiter.hit('shared')[0]:
                accepted.append(1)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(accepted) == 50


# --- CA-32 : mémoire bornée, sans éviction exploitable ------------------------------------------

def test_identifier_family_memory_is_constant_whatever_the_number_of_identifiers():
    clock = Clock()
    limiter = BucketLimiter(5, W, buckets=64, clock=clock)
    before = (limiter.footprint(), len(limiter._current), len(limiter._previous))
    for i in range(6400):                              # 100 × le nombre de compartiments
        limiter.hit(f'user-{i}')
    assert (limiter.footprint(), len(limiter._current), len(limiter._previous)) == before == (64, 64, 64)


def test_ip_flood_cannot_erase_a_targeted_identifier_counter():
    clock = Clock()
    limits = Limiters(Limits(), clock=clock, buckets=64, ip_entries=100, secret=b's' * 32)
    for _ in range(5):
        assert limits.password_identifier.hit('victime')[0]
    assert limits.password_identifier.hit('victime')[0] is False
    for i in range(10_000):                            # bien plus d'IP distinctes que la LRU
        limits.password_ip.hit(f'10.{i // 256 % 256}.{i % 256}.1')
    assert limits.password_ip.footprint() <= 100
    assert limits.password_identifier.hit('victime')[0] is False


def test_lru_is_bounded_and_evicts_the_least_recent():
    clock = Clock()
    limiter = LruLimiter(1, W, max_entries=3, clock=clock)
    for key in ('a', 'b', 'c'):
        limiter.hit(key)
    limiter.hit('a')                  # 'a' redevient la plus récente
    limiter.hit('d')                  # évince 'b'
    assert limiter.footprint() == 3
    assert limiter.hit('a')[0] is False and limiter.hit('b')[0] is True


def test_colliding_identifiers_share_the_limit_never_more_permissive():
    clock = Clock()
    limiter = BucketLimiter(5, W, buckets=1, clock=clock)   # tout le monde dans le même compartiment
    for i in range(5):
        assert limiter.hit(f'id-{i}')[0]
    assert limiter.hit('autre')[0] is False


def test_default_sizes():
    assert (ratelimit.IDENTIFIER_BUCKETS, ratelimit.IP_LRU_SIZE) == (65_536, 50_000)
    limiters = Limiters()
    assert limiters.password_identifier.footprint() == 65_536
    assert limiters.password_ip._max == 50_000 and limiters.token_ip._max == 50_000


def test_identifier_is_normalized_nfkc_and_casefold():
    clock = Clock()
    limiter = BucketLimiter(2, W, clock=clock)
    assert limiter.hit('Alice')[0] and limiter.hit('ＡＬＩＣＥ')[0] and limiter.hit('  alice ')[0] is False
    assert normalize_key('ﬁ') == 'fi'


def test_bucket_index_depends_on_a_per_instance_secret():
    """Sans la clé, un attaquant ne peut pas viser un compartiment (collisions choisies)."""
    a, b = BucketLimiter(5, W, secret=b'a' * 32), BucketLimiter(5, W, secret=b'b' * 32)
    assert [a._slot(f'u{i}') for i in range(50)] != [b._slot(f'u{i}') for i in range(50)]
    assert BucketLimiter(5, W)._secret != BucketLimiter(5, W)._secret   # tirée au hasard


# --- Assemblage : ordre, bornes ---------------------------------------------------------------

def test_hit_all_stops_at_first_refusal_and_spares_later_counters():
    clock = Clock()
    ip, ident = LruLimiter(1, W, clock=clock), BucketLimiter(5, W, clock=clock)
    assert hit_all((ip, '1.2.3.4'), (ident, 'alice')) == (True, 0)
    allowed, retry = hit_all((ip, '1.2.3.4'), (ident, 'alice'))
    assert allowed is False and retry >= 1
    # Le deuxième accès, refusé par l'IP, n'a pas consommé le compteur de l'identifiant
    assert sum(ident.hit('alice')[0] for _ in range(10)) == 4


def test_limits_defaults_are_inside_their_bounds_and_match_the_spec():
    limits = Limits()
    assert limits.password_per_identifier == 5       # CA-7 : le 6e est refusé
    assert limits.generation_per_user == 30          # RB-S5


@pytest.mark.parametrize('field,bad', [
    ('password_per_identifier', 2), ('password_per_identifier', 21), ('password_per_ip', 9),
    ('password_per_ip', 101), ('mfa_per_account', 2), ('mfa_per_account', 11),
    ('forgot_per_identifier', 0), ('forgot_per_identifier', 11), ('token_per_ip', 4),
    ('token_per_ip', 31), ('generation_per_user', 0), ('generation_per_user', 101),
    ('password_per_identifier', True), ('password_per_identifier', 5.0), ('password_per_identifier', '5'),
])
def test_out_of_bounds_thresholds_are_refused(field, bad):
    with pytest.raises(ValueError):
        Limits(**{field: bad})


def test_limiters_families_use_the_expected_windows_and_thresholds():
    clock = Clock()
    lim = Limiters(Limits(forgot_per_identifier=1, mfa_per_account=3), clock=clock)
    assert lim.forgot_identifier.hit('a@b.c')[0] and not lim.forgot_identifier.hit('A@B.C')[0]
    assert lim.forgot_identifier.window_s == 3600 and lim.password_identifier.window_s == 900
    assert [lim.mfa_account.hit(42)[0] for _ in range(4)] == [True, True, True, False]
    assert lim.mfa_account.hit(43)[0] is True        # par compte
    assert lim.generation_user.limit == 30 and lim.generation_user.window_s == 3600
