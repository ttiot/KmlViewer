"""
Résultats générés, gardés EN MÉMOIRE en vue de l'enregistrement (gpx-route-builder RB-5.2, m10, m11, n7, P-TR5-8).

  - 30 min de vie, 20 résultats par utilisateur ET plafond global (200 résultats ou 256 Mo, le premier atteint), éviction LRU ;
  - `result_id` de 192 bits, LIÉ à l'utilisateur : inconnu, expiré, évincé ou d'un autre ⇒ None (le 404 est celui des routes) ;
  - taille comptée = octets du GPX + octets du payload sérialisé en JSON ;
  - dédoublonnage : un résultat enregistré garde une RÉFÉRENCE COMPOSÉE (`activity_id`, `user_id`, `sha256`) ; l'appelant vérifie
    en base qu'elle désigne toujours la trace créée (l'id seul n'est jamais une preuve d'existence, n7).
Module PUR : ni Flask ni base ; l'horloge est injectable.
"""

import json
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Optional

TTL_S = 30 * 60
MAX_PER_USER = 20
MAX_TOTAL = 200
MAX_BYTES = 256 * 1024 * 1024


@dataclass
class Result:
    id: str
    user_id: int
    gpx: bytes
    payload: dict
    meta: dict
    size: int
    expires: float
    saved: Optional[dict] = field(default=None)       # {'activity_id', 'user_id', 'sha256'} après un enregistrement


class ResultStore:
    def __init__(self, *, ttl_s: float = TTL_S, max_per_user: int = MAX_PER_USER, max_total: int = MAX_TOTAL,
                 max_bytes: int = MAX_BYTES, clock: Callable = time.monotonic):
        self.ttl_s, self.max_per_user, self.max_total, self.max_bytes = ttl_s, max_per_user, max_total, max_bytes
        self._clock = clock
        self._items: OrderedDict = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    @staticmethod
    def size_of(gpx: bytes, payload: dict) -> int:
        return len(gpx) + len(json.dumps(payload, separators=(',', ':')).encode('utf-8'))

    def put(self, user_id: int, gpx: bytes, payload: dict, meta: dict) -> str:
        size = self.size_of(gpx, payload)
        with self._lock:
            self._purge_expired()
            result_id = secrets.token_urlsafe(24)
            self._items[result_id] = Result(result_id, int(user_id), gpx, payload, meta, size, self._clock() + self.ttl_s)
            self._bytes += size
            self._enforce(int(user_id), keep=result_id)
        return result_id

    def get(self, user_id: int, result_id) -> Optional[Result]:
        """Le résultat s'il existe, est vivant ET appartient à `user_id` ; sinon None (même réponse dans tous les cas)."""
        if not isinstance(result_id, str) or not 20 <= len(result_id) <= 64:
            return None
        with self._lock:
            item = self._items.get(result_id)
            if item is None:
                return None
            if item.expires <= self._clock():
                self._drop(result_id)
                return None
            if item.user_id != int(user_id):
                return None
            self._items.move_to_end(result_id)
            return item

    def mark_saved(self, user_id: int, result_id: str, reference: dict) -> bool:
        with self._lock:
            item = self._items.get(result_id)
            if item is None or item.user_id != int(user_id):
                return False
            item.saved = dict(reference)
            return True

    def clear_user(self, user_id: int) -> int:
        """Efface les résultats d'un utilisateur (déconnexion, suppression du compte)."""
        with self._lock:
            gone = [k for k, v in self._items.items() if v.user_id == int(user_id)]
            for key in gone:
                self._drop(key)
            return len(gone)

    def stats(self) -> dict:
        with self._lock:
            return {'results': len(self._items), 'bytes': self._bytes}

    # -- interne (verrou tenu) ---------------------------------------------------------------------------------------
    def _drop(self, key) -> None:
        item = self._items.pop(key, None)
        if item is not None:
            self._bytes -= item.size

    def _purge_expired(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._items.items() if v.expires <= now]:
            self._drop(key)

    def _enforce(self, user_id: int, keep: str) -> None:
        mine = [k for k, v in self._items.items() if v.user_id == user_id]
        for key in mine[:max(0, len(mine) - self.max_per_user)]:
            self._drop(key)
        while len(self._items) > 1 and (len(self._items) > self.max_total or self._bytes > self.max_bytes):
            oldest = next(iter(self._items))
            if oldest == keep:
                self._items.move_to_end(oldest)
                oldest = next(iter(self._items))
            self._drop(oldest)
