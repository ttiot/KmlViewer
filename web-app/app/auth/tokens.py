"""
Jetons à usage unique (admin-space §5.3, EF-3.3, EF-3.6, EF-S8, B4) : invitation, amorçage, réinitialisation,
validation d'adresse, définition de mot de passe.

  - Jeton : 256 bits aléatoires (`secrets.token_urlsafe(32)`). En base : SEUL son SHA-256 (hexadécimal). Le jeton
    en clair n'existe que dans le lien rendu à l'appelant, une fois : ni la base, ni l'audit, ni les journaux.
  - Lien : `<PUBLIC_URL>/auth/token#t=<jeton>`. Le jeton est dans le FRAGMENT, que ni le navigateur ni le proxy
    n'envoient : il ne figure jamais dans un chemin, une query ou un journal d'accès (B4).
  - `inspect` ne consomme rien et ne dit que le TYPE de formulaire à afficher ; jeton inconnu, malformé, expiré,
    utilisé ou révoqué ⇒ la même réponse (None), sans distinction (EF-3.6, r5).
  - `consume` est atomique : un seul UPDATE conditionnel, une seule consommation même sous deux requêtes
    concurrentes. Il participe à la transaction de l'appelant (création du compte comprise).
  - Seul le jeton d'AMORÇAGE peut porter un octroi d'admin (EF-0.3) ; la base le garantit (CHECK).
  - Aucun accès GET ne consomme jamais un jeton (c'est l'affaire des routes).
"""

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

from app import audit
from app.auth.emails import MAX_EMAIL, canonical_email  # noqa: F401 - ré-exportés
from app.db import write_transaction
from app.security import SQLITE_MAX_INT

logger = logging.getLogger(__name__)

KINDS = ('invite', 'bootstrap', 'reset', 'verify_email', 'set_password')
DEFAULT_TTL = {
    'invite': timedelta(days=7), 'bootstrap': timedelta(hours=24), 'reset': timedelta(hours=24),
    'set_password': timedelta(hours=24), 'verify_email': timedelta(hours=48),
}
MIN_TTL, MAX_TTL = timedelta(hours=1), timedelta(days=30)          # EF-6.5 : validité des invitations 1 h – 30 j
# Type de formulaire révélé par `inspect` (jamais l'adresse, l'émetteur, la note ni l'échéance)
FORM_FOR_KIND = {'invite': 'choose_credentials', 'bootstrap': 'choose_credentials', 'reset': 'set_password',
                 'set_password': 'set_password', 'verify_email': 'verify_email'}
MAX_NOTE = 200
MAX_CREDITS = 1000
_TOKEN_SHAPE = re.compile(r'[A-Za-z0-9_-]{43}')                  # token_urlsafe(32) : 43 caractères
_TIME_FORMAT = '%Y-%m-%dT%H:%M:%SZ'


@dataclass(frozen=True)
class NewToken:
    """
    Jeton fraîchement créé. Le jeton en clair n'est accessible que par l'attribut explicite `.token` ; ni `repr`, ni
    `str`, ni un format ne le contiennent (un log ou une trace de test ne doit jamais le recopier).
    """
    id: int
    token: str = field(repr=False)        # en clair : à ne montrer qu'une fois, ne jamais journaliser
    expires_at: str

    def __str__(self) -> str:
        return repr(self)


class TokenInfo(NamedTuple):
    """Ce qu'une consommation réussie livre à l'appelant (jamais le jeton ni son hash)."""
    id: int
    kind: str
    created_by: Optional[int]
    target_user_id: Optional[int]
    email: Optional[str]
    is_admin_grant: bool
    initial_credits: int
    note: Optional[str]


def hash_token(token) -> Optional[str]:
    """SHA-256 hexadécimal d'un jeton bien formé ; None pour tout ce qui ne peut pas en être un."""
    if not isinstance(token, str) or not _TOKEN_SHAPE.fullmatch(token):
        return None
    return hashlib.sha256(token.encode('ascii')).hexdigest()


def link(public_url: str, token: str) -> str:
    """Lien à communiquer : le jeton est dans le fragment (EF-S8). `public_url` vient de PUBLIC_URL, jamais de Host."""
    if not public_url:
        raise ValueError("PUBLIC_URL absente : impossible de construire un lien")
    return f"{public_url.rstrip('/')}/auth/token#t={token}"


def _stamp(moment: Optional[datetime]) -> str:
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def _clean_email(email) -> Optional[str]:
    return None if email is None else canonical_email(email)


def pending_for_email(conn, email, now: Optional[datetime] = None) -> bool:
    """Vrai si une invitation ou un amorçage encore valable vise déjà cette adresse (forme canonique comparée)."""
    wanted = canonical_email(email)
    rows = conn.execute("SELECT email FROM auth_tokens WHERE kind IN ('invite', 'bootstrap') AND email IS NOT NULL "
                        "AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?", (_stamp(now),)).fetchall()
    return any(r['email'] == wanted for r in rows)


def create(conn, kind: str, *, actor: audit.Actor, created_by: Optional[int] = None, target_user_id: Optional[int] = None,
           email: Optional[str] = None, is_admin_grant: bool = False, initial_credits: int = 0, note: Optional[str] = None,
           sent_by_mail: bool = False, ttl: Optional[timedelta] = None, now: Optional[datetime] = None,
           ip: Optional[str] = None) -> NewToken:
    """
    Crée un jeton et le trace (`token.create`, sans le jeton). `created_by` : compte émetteur (None pour la CLI).
    Seul `bootstrap` peut porter `is_admin_grant`. Retourne le jeton EN CLAIR, une seule fois.
    """
    if kind not in KINDS:
        raise ValueError("Type de jeton inconnu")
    if is_admin_grant and kind != 'bootstrap':
        raise ValueError("Seul un jeton d'amorçage peut donner l'admin")
    if (isinstance(initial_credits, bool) or not isinstance(initial_credits, int)
            or not 0 <= initial_credits <= MAX_CREDITS):
        raise ValueError(f"Crédits initiaux : 0 à {MAX_CREDITS}")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE or not all(c.isprintable() for c in note)):
        raise ValueError(f"Note : {MAX_NOTE} caractères au plus, sans caractère de contrôle")
    ttl = DEFAULT_TTL[kind] if ttl is None else ttl
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise ValueError("Durée de validité : 1 h à 30 j")
    clean_email = _clean_email(email)
    moment = now or datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    with write_transaction(conn):
        cursor = conn.execute(
            "INSERT INTO auth_tokens (token_hash, kind, created_by, target_user_id, email, is_admin_grant, initial_credits, "
            "note, sent_by_mail, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (hash_token(token), kind, created_by, target_user_id, clean_email, int(is_admin_grant), initial_credits, note,
             int(sent_by_mail), _stamp(moment), _stamp(moment + ttl)))
        audit.record(conn, 'token.create', actor=actor, target_type='token', target_id=cursor.lastrowid, ip=ip,
                     details={'kind': kind, 'target_user': target_user_id, 'admin_grant': bool(is_admin_grant)},
                     now=moment)
    return NewToken(cursor.lastrowid, token, _stamp(moment + ttl))


def inspect(conn, token, now: Optional[datetime] = None) -> Optional[str]:
    """
    Type de formulaire à afficher (`choose_credentials`, `set_password`, `verify_email`), ou None. None est LA
    réponse pour tout jeton inconnu, mal formé, expiré, utilisé ou révoqué : rien ne les distingue. Ne consomme rien
    et ne révèle ni adresse, ni émetteur, ni note, ni échéance.
    """
    digest = hash_token(token)
    row = None if digest is None else conn.execute(
        "SELECT kind FROM auth_tokens WHERE token_hash = ? AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?",
        (digest, _stamp(now))).fetchone()
    return FORM_FOR_KIND[row['kind']] if row else None


def peek(conn, token, now: Optional[datetime] = None):
    """
    (type, compte visé) INTERNES d'un jeton VALABLE, pour router la consommation, ou None. Jamais renvoyés au client :
    `inspect` ne révèle que le type de formulaire. Ne consomme rien.
    """
    digest = hash_token(token)
    row = None if digest is None else conn.execute(
        "SELECT kind, target_user_id FROM auth_tokens WHERE token_hash = ? AND used_at IS NULL AND revoked_at IS NULL "
        "AND expires_at > ?", (digest, _stamp(now))).fetchone()
    return (row['kind'], row['target_user_id']) if row else None


def kind_of(conn, token, now: Optional[datetime] = None) -> Optional[str]:
    found = peek(conn, token, now)
    return found[0] if found else None


def consume(conn, token, *, kind: str, used_by: Optional[int] = None,
            target_user_id: Optional[int] = None, now: Optional[datetime] = None) -> Optional[TokenInfo]:
    """
    Consomme le jeton ATOMIQUEMENT (UPDATE conditionnel) et rend ce qu'il porte, ou None (même réponse que
    `inspect` pour tout jeton non valable). À appeler dans la transaction qui crée le compte : si elle échoue, le
    jeton n'est pas consommé. `used_by` : compte créé ou concerné, s'il est déjà connu.

    `kind` est OBLIGATOIRE (un oubli ne peut plus consommer un jeton d'un autre type). Dans le MÊME UPDATE conditionnel
    (R2, CA-17b) : `kind` exige ce type de jeton, `target_user_id` (facultatif) exige que le jeton vise ce compte
    (ex. `verify_email` consommé seulement avec la session du compte visé). Une contrainte non satisfaite ne
    consomme RIEN : le jeton reste valable, et la réponse est la même que pour un jeton inconnu.
    """
    digest = hash_token(token)
    if digest is None or kind not in KINDS:
        return None
    if target_user_id is not None and (isinstance(target_user_id, bool) or not isinstance(target_user_id, int)
                                       or not 0 < target_user_id <= SQLITE_MAX_INT):
        return None
    constraints, params = " AND kind = ?", [kind]
    if target_user_id is not None:
        constraints += " AND target_user_id = ?"
        params.append(target_user_id)
    with write_transaction(conn):
        moment = _stamp(now)
        changed = conn.execute(
            "UPDATE auth_tokens SET used_at = ?, used_by = ? WHERE token_hash = ? AND used_at IS NULL "
            f"AND revoked_at IS NULL AND expires_at > ?{constraints}", (moment, used_by, digest, moment, *params)).rowcount
        if not changed:
            return None
        row = conn.execute(
            "SELECT id, kind, created_by, target_user_id, email, is_admin_grant, initial_credits, note "
            "FROM auth_tokens WHERE token_hash = ?", (digest,)).fetchone()
    return TokenInfo(row['id'], row['kind'], row['created_by'], row['target_user_id'], row['email'],
                     bool(row['is_admin_grant']), row['initial_credits'], row['note'])


def set_used_by(conn, token_id: int, user_id: int) -> None:
    """Complète `used_by` une fois le compte créé (la consommation précède la création dans la transaction)."""
    with write_transaction(conn):
        conn.execute("UPDATE auth_tokens SET used_by = ? WHERE id = ? AND used_at IS NOT NULL", (user_id, token_id))


def revoke(conn, token_id: int, *, actor: audit.Actor, now: Optional[datetime] = None, ip: Optional[str] = None) -> bool:
    """Révoque un jeton non utilisé (le crédit éventuel n'est PAS rendu). False s'il n'y a rien à révoquer."""
    with write_transaction(conn):
        changed = conn.execute("UPDATE auth_tokens SET revoked_at = ? WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL",
                               (_stamp(now), token_id)).rowcount
        if changed:
            audit.record(conn, 'token.revoke', actor=actor, target_type='token', target_id=token_id, ip=ip, now=now)
    return bool(changed)


def revoke_pending_for_creator(conn, user_id: int, *, actor: audit.Actor, now: Optional[datetime] = None) -> int:
    """Émetteur désactivé ou supprimé : ses invitations en attente sont révoquées (EF-3.7)."""
    with write_transaction(conn):
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM auth_tokens WHERE created_by = ? AND used_at IS NULL AND revoked_at IS NULL", (user_id,))]
        for token_id in ids:
            revoke(conn, token_id, actor=actor, now=now)
    return len(ids)


def matches(stored_hash: str, token) -> bool:
    """Comparaison en temps constant d'un jeton avec un hash déjà lu (EF-S4)."""
    digest = hash_token(token)
    return digest is not None and hmac.compare_digest(stored_hash, digest)


def mark_sent(conn, token_id: int) -> None:
    """Note que le lien est aussi parti par e-mail (information pour l'émetteur ; ne valide jamais l'adresse, EF-3.4)."""
    with write_transaction(conn):
        conn.execute("UPDATE auth_tokens SET sent_by_mail = 1 WHERE id = ?", (token_id,))
