"""
Fonctions bêta (admin-space §5.7, EF-7) : un état par fonction, `off` (défaut) | `users` | `all`.

  - `users` : la fonction n'est ouverte qu'aux comptes de la liste. La liste est CONSERVÉE entre états : passer
    à `all` puis revenir à `users` la retrouve.
  - Une fonction indisponible pour l'utilisateur répond 404 (jamais 403 : on ne révèle pas son existence) et son
    entrée est absente du HTML (`feature_enabled` dans les gabarits).
  - Le contrôle est SERVEUR (`feature_required`) ; le front ne fait que masquer.
  - L'état se lit en base à chaque requête (deux petites lectures ; mémorisées pour la requête dans les gabarits
    seulement, `feature_required` relit à chaque appel) : un changement
    d'un autre processus s'applique aussitôt. L'écriture (`set_state`, `set_users`) est réservée à l'API admin
    (T14) ; elle valide, écrit et journalise dans une transaction.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from typing import Iterable, Optional

from flask import abort, g

from app import audit
from app.db import get_db, write_transaction

logger = logging.getLogger(__name__)

STATES = ('off', 'users', 'all')
MAX_USERS_PER_FEATURE = 1000


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    help: str = ''


FEATURES = {
    'route_builder': Feature('route_builder', "Créer une trace", "Générer des traces planifiées (boucles) à partir de points."),
}


class FeatureError(ValueError):
    """Écriture refusée (fonction ou état inconnu, utilisateur inexistant, liste trop longue)."""


def _known(key) -> Feature:
    feature = FEATURES.get(key) if isinstance(key, str) else None
    if feature is None:
        raise FeatureError("Fonction inconnue")
    return feature


def get_state(conn, key: str) -> dict:
    """
    État d'une fonction : `state` (off si jamais réglée), `users` (identifiants, conservés entre états), date, auteur.
    UNE seule requête : un lecteur concurrent à un `set_state` voit l'état ET la liste d'une même écriture, jamais un mélange.
    """
    _known(key)
    row = conn.execute(
        "SELECT f.state, f.updated_at, f.updated_by, "
        "(SELECT group_concat(user_id) FROM feature_users WHERE feature_key = k.key) AS users "
        "FROM (SELECT ? AS key) k LEFT JOIN feature_flags f ON f.key = k.key", (key,)).fetchone()
    users = sorted(int(i) for i in row['users'].split(',')) if row['users'] else []
    return {'key': key, 'state': row['state'] or 'off', 'users': users,
            'updated_at': row['updated_at'], 'updated_by': row['updated_by']}


def is_enabled(conn, key: str, user_id: Optional[int]) -> bool:
    """Vrai si la fonction est ouverte à cet utilisateur (une seule requête, donc une lecture cohérente)."""
    if key not in FEATURES or user_id is None:
        return False
    row = conn.execute(
        "SELECT state, EXISTS(SELECT 1 FROM feature_users WHERE feature_key = f.key AND user_id = ?) AS listed "
        "FROM feature_flags f WHERE f.key = ?", (user_id, key)).fetchone()
    if row is None:
        return False
    return row['state'] == 'all' or (row['state'] == 'users' and bool(row['listed']))


def _updated_by(actor) -> str:
    return actor.label if actor.type == 'user' else actor.type


def set_state(conn, key: str, state: str, *, actor: audit.Actor, user_ids: Optional[Iterable[int]] = None,
              ip: Optional[str] = None) -> dict:
    """
    Change l'état d'une fonction. `user_ids` (facultatif) remplace la liste ; sans lui la liste est conservée.
    Tout ou rien, tracé (`feature.update`). Retourne le nouvel état.
    """
    _known(key)
    if state not in STATES:
        raise FeatureError("État inconnu (off, users ou all)")
    ids = None
    if user_ids is not None:
        raw = list(user_ids)
        ids = sorted({i for i in raw if not isinstance(i, bool) and isinstance(i, int)})
        if len(ids) != len(raw) or len(ids) > MAX_USERS_PER_FEATURE:      # doublon, non-entier ou trop long
            raise FeatureError(f"Liste d'identifiants invalide (entiers, {MAX_USERS_PER_FEATURE} au plus)")
    with write_transaction(conn):
        if ids is not None:
            found = {r[0] for r in conn.execute(
                f"SELECT id FROM users WHERE id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
            if found != set(ids):
                raise FeatureError("Utilisateur inexistant dans la liste")
        before = get_state(conn, key)
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        conn.execute("INSERT INTO feature_flags (key, state, updated_at, updated_by) VALUES (?, ?, ?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET state = excluded.state, updated_at = excluded.updated_at, "
                     "updated_by = excluded.updated_by", (key, state, now, _updated_by(actor)))
        if ids is not None:
            conn.execute("DELETE FROM feature_users WHERE feature_key = ?", (key,))
            conn.executemany("INSERT INTO feature_users (feature_key, user_id) VALUES (?, ?)", [(key, i) for i in ids])
        after = get_state(conn, key)
        audit.record(conn, 'feature.update', actor=actor, target_type='feature', target_id=key, ip=ip,
                     details={'old': before['state'], 'new': after['state'], 'listed_users': len(after['users']),
                              'list_changed': ids is not None,
                              # Qui a gagné ou perdu l'accès (numéros de compte, pas des secrets)
                              'added': sorted(set(after['users']) - set(before['users'])),
                              'removed': sorted(set(before['users']) - set(after['users']))})
    return after


def feature_required(key: str):
    """Décorateur de route : 404 si la fonction n'est pas ouverte à l'utilisateur courant (jamais 403)."""
    _known(key)

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            user = g.get('user')
            if user is None or not is_enabled(get_db(), key, user['id']):
                abort(404)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def init_features(app) -> None:
    """Expose `feature_enabled(clé)` aux gabarits : une entrée d'une fonction indisponible n'est pas dans le HTML."""
    def feature_enabled(key):
        user = g.get('user')
        cache = g.setdefault('_features', {})
        if key not in cache:
            cache[key] = user is not None and is_enabled(get_db(), key, user['id'])
        return cache[key]

    app.context_processor(lambda: {'feature_enabled': feature_enabled})
