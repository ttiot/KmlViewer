"""
Journal d'audit (admin-space EF-9) : écriture, consultation paginée, purge.

`record` écrit dans la transaction de l'appelant : une action et sa trace sont validées ou
annulées ensemble (changement de réglage + ligne d'audit).

Secrets : le SEUL moyen sûr est de ne jamais recevoir la valeur. Un réglage se trace par
`record_setting_change(…, secret=<type du registre>)` : avec `secret=True`, ni l'ancienne ni la
nouvelle valeur ne sont écrites. Un secret du magasin se trace par `record_secret_event`, qui ne
prend aucune valeur en paramètre. Le masquage par nom de clé dans `record` n'est qu'un filet
(il masque à tort `bypass_mfa`, et ne voit pas un jeton sous une clé anodine) : il ne protège pas
la VALEUR d'un réglage, dont seul le type du registre dit qu'elle est secrète.

Acteur (EF-9.1, CA-24b) : l'ORIGINE d'une action se lit sur `actor_type` (`user` | `cli` | `system`),
jamais sur le libellé. Un compte SSO que l'IdP nomme `cli` reste `actor_type = 'user'` avec son `actor_id`
et ne peut pas se faire passer pour une action de la ligne de commande. La base l'impose
(`CHECK ((actor_type = 'user') = (actor_id IS NOT NULL))`) ; `Actor` l'impose côté code, sans jamais
déduire le type du libellé. `actor_label` est un instantané de l'identifiant (survit à la suppression
du compte), vide pour `cli` et `system`.
"""

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.db import write_transaction

logger = logging.getLogger(__name__)

RETENTION_MIN_DAYS = 30
RETENTION_MAX_DAYS = 3650
RETENTION_DEFAULT_DAYS = 90

MASK = '[masqué]'
MAX_DETAILS_CHARS = 4000
MAX_TEXT = 128
MAX_LIMIT = 200

_ACTION = re.compile(r'[a-z][a-z0-9_.]{0,63}')
MAX_VALUE_CHARS = 200
MAX_LIST_ITEMS = 20
SECRET_EVENTS = frozenset({'update', 'clear', 'clear_on_destination_change', 'refused', 'rotate'})
_TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


ACTOR_TYPES = ('user', 'cli', 'system')


@dataclass(frozen=True)
class Actor:
    """
    Qui agit. Construire par `Actor.user(id, libellé)`, `Actor.from_user(ligne)`, `Actor.cli()` ou
    `Actor.system()`. Un acteur `user` a toujours un `id` et un libellé lisible ; `cli` et `system`
    n'ont ni l'un ni l'autre (cohérent avec la contrainte de la base).
    """

    type: str
    id: Optional[int] = None
    label: str = ''

    def __post_init__(self):
        if self.type not in ACTOR_TYPES:
            raise ValueError(f"Type d'acteur inconnu : {self.type!r}")
        if self.type == 'user':
            if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id < 1:
                raise ValueError("Un acteur « user » a un identifiant de compte")
            label = _text(self.label, 64).strip() if isinstance(self.label, str) else ''
            if not label:
                raise ValueError("Un acteur « user » a un libellé (identifiant du compte)")
            object.__setattr__(self, 'label', label)
        elif self.id is not None or self.label:
            raise ValueError(f"Un acteur « {self.type} » n'a ni identifiant ni libellé de compte")

    @classmethod
    def user(cls, user_id: int, label: str) -> 'Actor':
        return cls('user', user_id, label)

    @classmethod
    def cli(cls) -> 'Actor':
        return cls('cli')

    @classmethod
    def system(cls) -> 'Actor':
        return cls('system')

    @classmethod
    def from_user(cls, user) -> 'Actor':
        """Depuis une ligne `users` : identifiant, à défaut e-mail, à défaut `user#<id>`. Toujours `user`."""
        label = ''
        for field in ('username', 'email'):
            try:
                value = user[field]
            except (KeyError, IndexError, TypeError):
                value = None
            if isinstance(value, str) and _text(value, 64).strip():
                label = value
                break
        return cls('user', user['id'], label or f"user#{user['id']}")


def _text(value, limit=MAX_TEXT) -> str:
    """Chaîne sans caractère de contrôle (pas de fausse ligne de journal), tronquée."""
    return ''.join(c for c in str(value) if c.isprintable())[:limit]


# Noms de clés dont la valeur n'est jamais écrite (filet, EF-9.1 « sans secret, mot de passe, jeton ni code, IP »).
# La clé est découpée en MOTS (snake, kebab, camelCase) : `bypass_mfa` et `passage` ne contiennent pas le mot
# `pass`, `x-api-key` contient `api` + `key`. Une clé `key` seule n'est PAS masquée : c'est le nom du réglage
# dans record_setting_change. Le seul rempart des valeurs de réglages secrets reste le type du registre.
_WORD_SPLIT = re.compile(r'[^a-z0-9]+')
_CAMEL = re.compile(r'(?<=[a-z0-9])(?=[A-Z])')
_SENSITIVE_WORDS = frozenset({
    'password', 'passwd', 'pass', 'passphrase', 'passe', 'pwd', 'mdp', 'token', 'tokens', 'jeton', 'jetons', 'secret',
    'secrets', 'hash', 'credential', 'credentials', 'cookie', 'cookies', 'otp', 'totp', 'recovery', 'bearer', 'jwt',
    'authorization', 'apikey', 'privatekey', 'accesskey', 'signingkey', 'sessionkey', 'ip', 'ips', 'motdepasse'})
_KEY_PREFIXES = frozenset({'api', 'private', 'access', 'signing', 'session', 'encryption', 'master', 'secret', 'ssh', 'auth'})
_IP_PREFIXES = frozenset({'remote', 'client', 'source', 'peer', 'real', 'forwarded'})
_BENIGN_CODE_PREFIXES = frozenset({'status', 'http', 'exit', 'error', 'return', 'response', 'country'})
MAX_DEPTH = 8


def _is_sensitive_key(key) -> bool:
    words = [w for w in _WORD_SPLIT.split(_CAMEL.sub('_', str(key)).lower()) if w]
    for i, word in enumerate(words):
        previous = words[i - 1] if i else ''
        if word in _SENSITIVE_WORDS:
            return True
        if word in ('code', 'codes') and previous not in _BENIGN_CODE_PREFIXES:
            return True
        if word == 'key' and previous in _KEY_PREFIXES:
            return True
        if word in ('addr', 'address') and previous in _IP_PREFIXES:
            return True
    return False


def _sanitize(value, depth=0, path=frozenset()):
    """
    Une seule passe : masque les clés sensibles, tronque les valeurs (chaînes, listes), neutralise NaN et
    l'infini (pas du JSON valide), borne la profondeur et coupe les cycles : un `details` hostile est
    tracé tronqué, il ne fait jamais échouer l'action auditée.
    """
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE_CHARS else value[:MAX_VALUE_CHARS] + '…[tronqué]'
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if isinstance(value, (dict, list, tuple)):
        if depth >= MAX_DEPTH:
            return '…[profondeur]'
        if id(value) in path:
            return '…[cycle]'
        inner = path | {id(value)}
        if isinstance(value, dict):
            return {str(k): (MASK if _is_sensitive_key(k) else _sanitize(v, depth + 1, inner)) for k, v in value.items()}
        items = [_sanitize(v, depth + 1, inner) for v in value[:MAX_LIST_ITEMS]]
        return items + [f'…(+{len(value) - MAX_LIST_ITEMS})'] if len(value) > MAX_LIST_ITEMS else items
    return _sanitize(str(value), depth, path)


def _details_json(details) -> Optional[str]:
    if details is None:
        return None
    text = json.dumps(_sanitize(details), ensure_ascii=False, allow_nan=False)
    if len(text) > MAX_DETAILS_CHARS:       # beaucoup de clés : rien de plus à rogner sans perdre la structure
        return json.dumps({'tronqué': True})
    return text


def _stamp(moment: Optional[datetime]) -> str:
    """Instant en UTC au format stocké. Aware non UTC : converti ; naïf : supposé UTC."""
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def record(conn, action: str, *, actor: Actor, target_type: Optional[str] = None, target_id=None, details=None,
           ip: Optional[str] = None, now: Optional[datetime] = None) -> int:
    """Ajoute une ligne et retourne son id. `action` : minuscules, chiffres, `_` et `.` (ex. `setting.update`)."""
    if not isinstance(actor, Actor):
        raise TypeError("actor doit être un Actor (Actor.user / Actor.cli / Actor.system)")
    if not isinstance(action, str) or not _ACTION.fullmatch(action):
        raise ValueError(f"Action d'audit invalide : {action!r}")
    at = _stamp(now)
    with write_transaction(conn):
        cursor = conn.execute(
            "INSERT INTO audit_log (at, actor_type, actor_id, actor_label, action, target_type, target_id, details, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (at, actor.type, actor.id, actor.label, action,
             _text(target_type, 32) if target_type else None,
             _text(target_id) if target_id is not None else None,
             _details_json(details), _text(ip, 64) if ip else None))
    return cursor.lastrowid


def record_setting_change(conn, key: str, old, new, *, secret: bool, actor: Actor,
                          ip: Optional[str] = None, now: Optional[datetime] = None) -> int:
    """
    Trace la modification d'un réglage (EF-9.2). `secret` vient du type du registre, jamais d'une
    devinette sur le nom : avec `secret=True`, aucune valeur (ni ancienne ni nouvelle) n'est écrite.
    """
    if not isinstance(secret, bool):
        raise TypeError("secret doit être un booléen issu du registre")
    details = {'key': key, 'changed': True} if secret else {'key': key, 'old': old, 'new': new}
    return record(conn, 'setting.update', actor=actor, target_type='setting',
                  target_id=key, details=details, ip=ip, now=now)


def record_secret_event(conn, name: str, event: str, *, actor: Actor,
                        reason: Optional[str] = None, ip: Optional[str] = None, now: Optional[datetime] = None) -> int:
    """
    Trace un événement sur un secret du magasin : `secret.<event>`. Ne prend AUCUNE valeur, pas
    même masquée : `reason` est un texte court choisi par le code (ex. destination en cause), jamais
    une donnée saisie.
    """
    if event not in SECRET_EVENTS:
        raise ValueError(f"Événement de secret inconnu : {event!r}")
    return record(conn, f'secret.{event}', actor=actor, target_type='secret',
                  target_id=name, details={'reason': reason} if reason else None, ip=ip, now=now)


def _utc_stamp(value: Optional[str], ceil: bool = False) -> Optional[str]:
    """Horodatage ISO d'une consultation ramené au format stocké ; naïf = UTC ; illisible = ValueError."""
    if value is None:
        return None
    moment = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if ceil and moment.microsecond:         # borne exclusive : une fraction de seconde compte pour la seconde entière
        moment += timedelta(seconds=1)
    return moment.strftime(_TIME_FORMAT)


def list_entries(conn, *, action: Optional[str] = None, actor_type: Optional[str] = None,
                 actor_id: Optional[int] = None, since: Optional[str] = None, until: Optional[str] = None,
                 before_id: Optional[int] = None, limit: int = 50) -> list:
    """
    Consultation (lecture seule), de la plus récente à la plus ancienne. Pagination par curseur :
    passer le plus petit `id` reçu comme `before_id`. `action` : égalité, ou préfixe si elle finit
    par `.` (ex. `secret.`). `since` / `until` : horodatages ISO (naïf = UTC), ValueError si illisible.
    """
    clauses, params = [], []
    if action:
        if action.endswith('.'):
            clauses.append("action LIKE ? ESCAPE '\\'")
            params.append(action.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        else:
            clauses.append("action = ?")
            params.append(action)
    if actor_type is not None and actor_type not in ACTOR_TYPES:
        raise ValueError(f"Type d'acteur inconnu : {actor_type!r}")
    filters = (('actor_type', '=', actor_type), ('actor_id', '=', actor_id), ('at', '>=', _utc_stamp(since)),
               ('at', '<', _utc_stamp(until, ceil=True)), ('id', '<', before_id))
    for column, op, value in filters:
        if value is not None:
            clauses.append(f"{column} {op} ?")
            params.append(value)
    limit = max(1, min(int(limit), MAX_LIMIT))
    where = ('WHERE ' + ' AND '.join(clauses)) if clauses else ''
    rows = conn.execute(f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
    entries = []
    for row in rows:
        entry = dict(row)
        try:
            entry['details'] = json.loads(row['details']) if row['details'] else None
        except ValueError:      # ligne abîmée (base éditée, restauration partielle) : le journal reste lisible
            entry['details'] = {'illisible': True}
        entries.append(entry)
    return entries


def purge(conn, retention_days: int = RETENTION_DEFAULT_DAYS, now: Optional[datetime] = None) -> int:
    """
    Supprime les lignes plus vieilles que `retention_days` (30 à 3650) et trace la purge. Retourne le
    nombre de lignes supprimées. Une durée hors bornes est refusée : une valeur énorme ou nulle
    désactiverait la rétention (ou viderait le journal) aussi sûrement qu'une erreur.
    """
    if (isinstance(retention_days, bool) or not isinstance(retention_days, int)
            or not RETENTION_MIN_DAYS <= retention_days <= RETENTION_MAX_DAYS):
        raise ValueError(f"Rétention d'audit : entier entre {RETENTION_MIN_DAYS} et {RETENTION_MAX_DAYS} jours")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = _stamp(now - timedelta(days=retention_days))
    with write_transaction(conn):
        deleted = conn.execute("DELETE FROM audit_log WHERE at < ?", (cutoff,)).rowcount
        record(conn, 'audit.purge', actor=Actor.system(), details={'deleted': deleted, 'retention_days': retention_days},
               now=now)
    logger.info("Purge du journal d'audit : %d ligne(s) supprimée(s) (rétention %d j)", deleted, retention_days)
    return deleted
