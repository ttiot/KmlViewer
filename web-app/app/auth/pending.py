"""
État SERVEUR des connexions en cours et des défis WebAuthn (admin-space EF-1.6, EF-S9, M3).

Une connexion à mot de passe réussie ne crée PAS de session : elle crée ici un état `mfa_pending` (étape `mfa` ou `enroll`) dont
le cookie ne porte que l'identifiant opaque (`pending_id`, ≥ 128 bits aléatoires), JAMAIS `user_id`. L'échéance (5 min), le
nombre d'essais (5) et la consommation (atomique, une seule fois) sont contrôlés ICI : rejouer un ancien cookie ne remet rien à
zéro, et un cookie dont l'état a été consommé, évincé ou expiré ne désigne rien.

Mémoire bornée : 10 000 états ; au-delà, les plus anciens sont évincés (l'utilisateur se reconnecte, rien de plus). Un redémarrage
perd les connexions en cours : acceptable. Les défis WebAuthn (T12) suivent la même mécanique : 2 min, consommés atomiquement.
"""

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

STEPS = ('mfa', 'enroll')
COOKIE_KEY = 'pending_id'      # seule clé de session de l'état intermédiaire : un identifiant opaque, jamais le compte
TTL_S = 300
ENROLL_TTL_S = 900          # P-T11-2 : installer une application et scanner prend plus que l'étape `mfa`
MAX_TRIES = 5
MAX_ENTRIES = 10_000
CHALLENGE_TTL_S = 120
PROFILE_ENROLL_TTL_S = 600      # P-T11-1 : enrôlement depuis le profil


@dataclass(frozen=True)
class Pending:
    id: str
    user_id: int
    step: str
    expires: float
    tries: int = 0
    next_url: str = '/'
    binding: tuple = ()             # (session_token, password_changed_at) à la création : tout changement invalide
    admin_grant: bool = False       # octroi d'admin porté par un amorçage (EF-0.3) : appliqué par l'enrôlement (T11)
    extra: dict = field(default_factory=dict, compare=False)     # données en MÉMOIRE (secret TOTP en cours d'enrôlement, P-T11-1)


class PendingStore:
    def __init__(self, *, ttl_s: float = TTL_S, max_tries: int = MAX_TRIES, max_entries: int = MAX_ENTRIES,
                 clock: Callable = time.monotonic):
        self.ttl_s, self.max_tries, self.max_entries, self._clock = ttl_s, max_tries, max_entries, clock
        self._states: OrderedDict = OrderedDict()
        self._challenges: OrderedDict = OrderedDict()
        self._enrollments: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    # -- connexions en cours -----------------------------------------------------------------------------------
    def create(self, user_id: int, step: str, *, next_url: str = '/', admin_grant: bool = False, binding: tuple = (),
               ttl_s: Optional[float] = None) -> str:
        """Nouvel état ; retourne son identifiant opaque (à poser seul dans le cookie)."""
        if step not in STEPS:
            raise ValueError("Étape inconnue")
        pid = secrets.token_urlsafe(32)
        with self._lock:
            lifetime = ttl_s if ttl_s is not None else (ENROLL_TTL_S if step == 'enroll' else self.ttl_s)
            self._states[pid] = Pending(pid, int(user_id), step, self._clock() + lifetime, 0, next_url, tuple(binding),
                                         bool(admin_grant))
            self._evict(self._states)
        return pid

    def get(self, pid) -> Optional[Pending]:
        """L'état courant ou None (inconnu, expiré, consommé). Ne modifie rien (hors purge de l'expiré)."""
        if not isinstance(pid, str) or not pid or len(pid) > 100:
            return None
        with self._lock:
            state = self._states.get(pid)
            if state is None:
                return None
            if state.expires <= self._clock():
                del self._states[pid]
                return None
            return state

    def hit(self, pid) -> Optional[int]:
        """
        Compte un essai SERVEUR. Retourne les essais restants, 0 si le plafond est atteint (l'état est alors DÉTRUIT : il faut
        se reconnecter), None si l'état n'existe plus. Un cookie rejoué retrouve le même compteur, jamais remis à zéro.
        """
        with self._lock:
            state = self._states.get(pid) if isinstance(pid, str) else None
            if state is None or state.expires <= self._clock():
                self._states.pop(pid, None) if isinstance(pid, str) else None
                return None
            state = replace(state, tries=state.tries + 1)
            if state.tries >= self.max_tries:
                del self._states[pid]
                return 0
            self._states[pid] = state
            return self.max_tries - state.tries

    def consume(self, pid, *, step: Optional[str] = None) -> Optional[Pending]:
        """Retire l'état ATOMIQUEMENT et le rend (une seule fois, même sous requêtes concurrentes), ou None."""
        with self._lock:
            state = self._states.get(pid) if isinstance(pid, str) else None
            if state is None or state.expires <= self._clock() or (step is not None and state.step != step):
                return None
            del self._states[pid]
            return state

    def destroy(self, pid) -> None:
        with self._lock:
            if isinstance(pid, str):
                self._states.pop(pid, None)

    def destroy_for_user(self, user_id: int) -> int:
        """Détruit les connexions en cours d'un compte (mot de passe changé, facteurs réinitialisés...)."""
        with self._lock:
            gone = [k for k, v in self._states.items() if v.user_id == user_id]
            for key in gone:
                del self._states[key]
            return len(gone)

    # -- défis WebAuthn (T12) ----------------------------------------------------------------------------------
    def put_challenge(self, owner: str, challenge: bytes) -> None:
        """Retient le défi lié à `owner` (état de connexion ou session) pour 2 min ; un nouveau remplace l'ancien."""
        with self._lock:
            self._challenges[owner] = (challenge, self._clock() + CHALLENGE_TTL_S)
            self._challenges.move_to_end(owner)
            self._evict(self._challenges)

    def take_challenge(self, owner: str) -> Optional[bytes]:
        """Retire le défi ATOMIQUEMENT : réutilisé (y compris par un cookie rejoué), expiré ou inconnu => None."""
        with self._lock:
            entry = self._challenges.pop(owner, None)
            if entry is None or entry[1] <= self._clock():
                return None
            return entry[0]

    # -- enrôlement depuis le profil (P-T11-1) ------------------------------------------------------------------------
    def put_enrollment(self, owner: str, secret: str) -> None:
        """Secret TOTP en cours d'enrôlement depuis le profil, lié à `owner` (compte + jeton de session), 10 min, en mémoire."""
        with self._lock:
            self._enrollments[owner] = (secret, self._clock() + PROFILE_ENROLL_TTL_S)
            self._enrollments.move_to_end(owner)
            self._evict(self._enrollments)

    def get_enrollment(self, owner: str) -> Optional[str]:
        with self._lock:
            entry = self._enrollments.get(owner)
            if entry is None or entry[1] <= self._clock():
                self._enrollments.pop(owner, None)
                return None
            return entry[0]

    def drop_enrollment(self, owner: str) -> None:
        with self._lock:
            self._enrollments.pop(owner, None)

    def _evict(self, table: OrderedDict) -> None:
        while len(table) > self.max_entries:
            table.popitem(last=False)

    def size(self) -> int:
        with self._lock:
            return len(self._states)
