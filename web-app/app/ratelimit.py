"""
Limiteur de débit en mémoire (ADR-0006, admin-space EF-S3).

Fenêtre glissante APPROCHÉE à deux compteurs : on retient, par clé, le compte de la fenêtre
fixe courante et celui de la précédente ; l'estimation vaut `précédente × (part de fenêtre
restante) + courante`. Mémoire constante par clé (pas de liste d'horodatages).

Garanties (testées, horloge injectée) :
  - au plus `limit` accès acceptés par fenêtre fixe ;
  - donc au plus 2 × `limit` sur toute fenêtre glissante de même durée.
Seuls les accès ACCEPTÉS sont comptés : un attaquant saturé ne prolonge pas son propre blocage,
et ne peut pas verrouiller durablement un compte visé (aucun verrouillage définitif).

Familles de clés, à mémoire bornée sans éviction exploitable (M8) :
  - BucketLimiter : identifiants SAISIS (mot de passe, mot de passe oublié). Tableau FIXE de
    compartiments indexés par HMAC-SHA-256 de l'identifiant normalisé, clé tirée au démarrage :
    rien n'est évincé, deux identifiants qui partagent un compartiment partagent la limite
    (plus strict, jamais plus permissif). Le choix de l'identifiant par l'attaquant ne fait pas
    croître la mémoire.
  - LruLimiter : IP (50 000 entrées, l'éviction ne profite qu'à qui dispose de nombreuses IP et
    contourne de toute façon la limite par IP) ; comptes et utilisateurs (clés non choisies
    par l'attaquant : identifiants de comptes existants).

Le limiteur se consulte AVANT toute opération coûteuse (hachage, r9). Compteurs remis à zéro au
redémarrage ; un seul worker (ADR-0006), à revoir sinon : `RateLimiter` est l'interface à
remplacer par un stockage partagé.
"""

import hmac
import ipaddress
import math
import os
import threading
import time
import unicodedata
from abc import ABC, abstractmethod
from array import array
from collections import OrderedDict
from dataclasses import dataclass, fields
from typing import Callable, Union

from app.settings.registry import REGISTRY

IDENTIFIER_BUCKETS = 65_536
IP_LRU_SIZE = 50_000
ACCOUNT_LRU_SIZE = 100_000


def normalize_key(value) -> str:
    """Forme de comparaison d'un identifiant ou d'une adresse saisis : NFKC, casefold, sans blancs de bord."""
    return unicodedata.normalize('NFKC', str(value)).strip().casefold()


def ip_key(address) -> str:
    """
    Clé de limitation par IP (T6-1) : IPv4 telle quelle ; IPv6 ramenée à son préfixe /64 (un client en dispose de 2^64) ;
    adresse IPv4 mappée (`::ffff:a.b.c.d`) ramenée en IPv4. Une adresse illisible partage une clé unique : jamais de
    contournement.
    """
    try:
        ip = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return 'invalid'
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class RateLimiter(ABC):
    """Interface : `hit(key) -> (allowed, retry_after_s)`. 429 générique côté appelant si refusé."""

    @abstractmethod
    def hit(self, key) -> tuple:
        ...

    @abstractmethod
    def footprint(self) -> int:
        """Nombre d'emplacements occupés (constant pour les compartiments, borné pour la LRU)."""


def _estimate(window_s, now, index, current, previous):
    """Estimation glissante à l'instant `now` pour un état (index, courante, précédente) déjà à jour."""
    elapsed = (now - index * window_s) / window_s
    return previous * (1.0 - elapsed) + current


def _roll(window_s, now, index, current, previous):
    """Fait avancer l'état à la fenêtre fixe qui contient `now`."""
    here = int(now // window_s)
    if here == index:
        return index, current, previous
    if here == index + 1:
        return here, 0, current
    return here, 0, 0


def _retry_after(limit, window_s, now, index, current, previous) -> int:
    """Secondes (entières, ≥ 1) avant qu'un accès soit de nouveau accepté, état supposé inchangé."""
    start = index * window_s
    if current >= limit:
        # Bloqué jusqu'à la fenêtre suivante, où `current` devient `previous` et décroît
        threshold = 1.0 - limit / current
        ok_at = start + window_s + threshold * window_s
    else:
        threshold = 1.0 - (limit - current) / previous if previous else 0.0
        ok_at = start + max(threshold, 0.0) * window_s
    return max(1, math.floor(ok_at - now) + 1)


class _Counter:
    """Logique commune ; les sous-classes décident où vivent les états."""

    def __init__(self, limit: Union[int, Callable[[], int]], window_s: float, clock):
        """`limit` : entier, ou fonction relue à chaque accès (seuil modifiable à chaud, sans remise à zéro)."""
        self._limit = limit if callable(limit) else (lambda value=int(limit): value)
        if window_s <= 0 or int(self._limit()) < 1:
            raise ValueError("limit >= 1 et window_s > 0 requis")
        self.window_s = float(window_s)
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def limit(self) -> int:
        return max(1, int(self._limit()))

    def _decide(self, index, current, previous):
        """Retourne (allowed, retry_after, nouvel état)."""
        now = self._clock()
        limit = self.limit
        index, current, previous = _roll(self.window_s, now, index, current, previous)
        if _estimate(self.window_s, now, index, current, previous) < limit:
            return True, 0, (index, current + 1, previous)
        return False, _retry_after(limit, self.window_s, now, index, current, previous), (index, current, previous)


class BucketLimiter(_Counter, RateLimiter):
    """Famille « identifiant » : compartiments fixes indexés par HMAC de la clé normalisée."""

    def __init__(self, limit, window_s, buckets=IDENTIFIER_BUCKETS, clock=time.monotonic, secret=None):
        super().__init__(limit, window_s, clock)
        if buckets < 1:
            raise ValueError("buckets >= 1 requis")
        self._secret = secret if secret is not None else os.urandom(32)
        self._index = array('q', [-1]) * buckets   # -1 : jamais utilisé
        self._current = array('l', [0]) * buckets
        self._previous = array('l', [0]) * buckets

    def _slot(self, key) -> int:
        digest = hmac.digest(self._secret, normalize_key(key).encode('utf-8'), 'sha256')
        return int.from_bytes(digest[:8], 'big') % len(self._index)

    def hit(self, key):
        slot = self._slot(key)
        with self._lock:
            allowed, retry, (index, current, previous) = self._decide(
                self._index[slot], self._current[slot], self._previous[slot])
            self._index[slot], self._current[slot], self._previous[slot] = index, current, previous
        return allowed, retry

    def footprint(self) -> int:
        return len(self._index)


class LruLimiter(_Counter, RateLimiter):
    """Famille « IP », « compte », « utilisateur » : au plus `max_entries` clés, la moins récente évincée."""

    def __init__(self, limit, window_s, max_entries=IP_LRU_SIZE, clock=time.monotonic):
        super().__init__(limit, window_s, clock)
        if max_entries < 1:
            raise ValueError("max_entries >= 1 requis")
        self._max = int(max_entries)
        self._states = OrderedDict()

    def hit(self, key):
        with self._lock:
            index, current, previous = self._states.get(key, (-1, 0, 0))
            allowed, retry, state = self._decide(index, current, previous)
            self._states[key] = state
            self._states.move_to_end(key)
            while len(self._states) > self._max:
                self._states.popitem(last=False)
        return allowed, retry

    def footprint(self) -> int:
        with self._lock:
            return len(self._states)


def hit_all(*checks) -> tuple:
    """
    Consulte plusieurs (limiteur, clé) dans l'ordre et s'arrête au premier refus : un accès
    refusé par la limite d'IP ne consomme pas le compteur de l'identifiant visé. Retourne
    (allowed, retry_after).
    """
    for limiter, key in checks:
        allowed, retry = limiter.hit(key)
        if not allowed:
            return False, retry
    return True, 0


# Champ de Limits -> clé du registre de réglages : le registre est la SEULE source des bornes et des
# défauts (T6-2) ; ces deux tables ne peuvent donc pas diverger
SETTING_KEYS = {
    'password_per_identifier': 'ratelimit.password_per_identifier',
    'password_per_ip': 'ratelimit.password_per_ip',
    'mfa_per_account': 'ratelimit.mfa_per_account',
    'forgot_per_identifier': 'ratelimit.forgot_per_identifier',
    'forgot_per_ip': 'ratelimit.forgot_per_ip',
    'token_per_ip': 'ratelimit.token_per_ip',
    'generation_per_user': 'routing.rate_limit_per_user',
}


def _default(name):
    return REGISTRY[SETTING_KEYS[name]].default


@dataclass(frozen=True)
class Limits:
    """
    Jeu de seuils fixes (tests, outils). Bornes et défauts viennent du registre de réglages
    (EF-6.5, M7) ; la spec ne fixe que « 6e échec refusé » (CA-7 : 5 par identifiant) et 30
    générations par heure (RB-S5), les autres défauts ont été retenus et validés par coord.
    Tout seuil hors de ses bornes est refusé : un réglage ne désactive jamais le limiteur.
    """

    password_per_identifier: int = _default('password_per_identifier')   # / 15 min
    password_per_ip: int = _default('password_per_ip')                   # / 15 min
    mfa_per_account: int = _default('mfa_per_account')                   # / 15 min
    forgot_per_identifier: int = _default('forgot_per_identifier')       # / h
    forgot_per_ip: int = _default('forgot_per_ip')                       # / h
    token_per_ip: int = _default('token_per_ip')                         # / 15 min
    generation_per_user: int = _default('generation_per_user')           # / h

    BOUNDS = {name: (int(REGISTRY[key].min), int(REGISTRY[key].max)) for name, key in SETTING_KEYS.items()}

    def __post_init__(self):
        for field in fields(self):
            low, high = self.BOUNDS[field.name]
            value = getattr(self, field.name)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError(f"{field.name} doit être un entier entre {low} et {high}")


class Limiters:
    """
    Les familles de l'application, construites une fois au démarrage (tailles injectables en test).

    Seuils : `limits` (jeu fixe) ou `get` (fonction clé de registre -> entier, typiquement
    `store.get`) relue à CHAQUE accès : modifier un réglage s'applique à la requête suivante, sans
    redémarrage et sans remettre les compteurs à zéro (EF-6.4). Les bornes sont celles du registre,
    déjà appliquées à l'écriture ; le limiteur ne descend de toute façon jamais sous 1.
    """

    WINDOW_15_MIN = 15 * 60
    WINDOW_1_H = 60 * 60

    def __init__(self, limits=None, clock=time.monotonic, buckets=IDENTIFIER_BUCKETS, ip_entries=IP_LRU_SIZE,
                 account_entries=ACCOUNT_LRU_SIZE, secret=None, get=None):
        if get is None:
            limits = limits or Limits()
            self.limits = limits

            def get_limit(name):
                return lambda: getattr(limits, name)
        else:
            def get_limit(name):
                key = SETTING_KEYS[name]
                return lambda: get(key)
        w15, w1h = self.WINDOW_15_MIN, self.WINDOW_1_H
        # Clés saisies : une même clé secrète pour les deux familles, un tableau par famille
        self.password_identifier = BucketLimiter(get_limit('password_per_identifier'), w15, buckets, clock, secret)
        self.password_ip = LruLimiter(get_limit('password_per_ip'), w15, ip_entries, clock)
        self.forgot_identifier = BucketLimiter(get_limit('forgot_per_identifier'), w1h, buckets, clock, secret)
        self.forgot_ip = LruLimiter(get_limit('forgot_per_ip'), w1h, ip_entries, clock)
        self.token_ip = LruLimiter(get_limit('token_per_ip'), w15, ip_entries, clock)
        # Clés non choisies par l'attaquant : second facteur (compte déjà établi), générations (utilisateur)
        self.mfa_account = LruLimiter(get_limit('mfa_per_account'), w15, account_entries, clock)
        self.generation_user = LruLimiter(get_limit('generation_per_user'), w1h, account_entries, clock)
