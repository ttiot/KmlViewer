"""
Secrets des services externes au repos (admin-space EF-5, ADR-0003) : Fernet, `SECRETS_KEY` dédiée.

  - `SECRETS_KEY` (env seulement, hors registre, jamais en base) : liste de clés Fernet séparées par des
    virgules ; la première chiffre, toutes déchiffrent (MultiFernet) : c'est la rotation (EF-5.7).
  - Écriture seule : on ne relit jamais un secret en clair par une route. `status()` ne renvoie que
    « configuré », date, auteur, destination liée et, pour un secret de type `api_key` uniquement, les
    4 derniers caractères (jamais pour un mot de passe).
  - Liaison secret <-> destination (B3, EF-5.5) : la destination effective normalisée est enregistrée avec
    le secret (`bound_to`) ; à l'usage, une destination différente ⇒ le secret n'est PAS rendu (audit,
    « secret à ressaisir »). Modifier la destination d'un service efface ses secrets dans la transaction
    du changement (`clear_linked`). Un secret défini en env n'est envoyé qu'à la destination définie en env.
  - Déchiffrement au seul moment de l'appel sortant ; ni valeur ni clé n'apparaissent dans un journal, un
    message d'erreur ou l'audit (qui ne reçoit jamais de valeur, voir audit.record_secret_event).
Les secrets TOTP des comptes (colonne `users.totp_secret`, T11) suivent la même clé : rotation et remise à
zéro les couvrent dès que la colonne existe.
"""

import base64
import hmac
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app import audit
from app.db import write_transaction

logger = logging.getLogger(__name__)

# Secrets déclarés (EF-5.1) : nom -> type. `api_key` : les 4 derniers caractères sont conservés ; `password` : jamais.
SECRET_KINDS = {'routing.api_key': 'api_key', 'smtp.password': 'password'}
MAX_SECRET_LENGTH = 512
MAX_DESTINATION_LENGTH = 500
LAST4_MIN_LENGTH = 12          # en dessous, 4 caractères en révéleraient une part trop grande : rien n'est conservé


class SecretsError(Exception):
    """Racine des erreurs du magasin. Les messages ne contiennent ni valeur ni clé."""


class SecretsKeyError(SecretsError):
    """SECRETS_KEY mal formée (le message n'en cite aucune partie)."""


class SecretsUnavailable(SecretsError):
    """Aucune clé fournie : impossible de chiffrer ou de déchiffrer."""


class UndecryptableSecrets(SecretsError):
    """Des valeurs chiffrées en base ne se déchiffrent avec aucune clé fournie : démarrage refusé (EF-5.2)."""

    def __init__(self, count: int):
        super().__init__(f"{count} valeur(s) chiffrée(s) en base indéchiffrable(s) avec les clés de SECRETS_KEY")
        self.count = count


class SecretUnavailable(SecretsError):
    """Secret absent, ou lié à une autre destination : « secret à ressaisir »."""

    message = "Secret à ressaisir"

    def __init__(self):
        super().__init__(self.message)


_FERNET_KEY = re.compile(r'[A-Za-z0-9_-]{43}=')


def _valid_key(text: str) -> bool:
    """Clé Fernet stricte : base64 URL-safe de 32 octets, rien avant ni après (Fernet() tolère des déchets finaux)."""
    if not _FERNET_KEY.fullmatch(text):
        return False
    try:
        return len(base64.urlsafe_b64decode(text)) == 32
    except ValueError:
        return False


def _as_bytes(value) -> bytes:
    """Jeton lu en base. Un TEXT (édition à la main, restauration) ou un type inattendu n'est pas un plantage : il
    donne une valeur qui ne se déchiffre pas, donc comptée « indéchiffrable »."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return value.encode('utf-8', 'replace') if isinstance(value, str) else b''


def parse_keys(raw: Optional[str]) -> list:
    """Clés Fernet de `SECRETS_KEY` (la première chiffre). Vide ⇒ []. Une entrée invalide ⇒ SecretsKeyError."""
    fernets = []
    for position, part in enumerate((raw or '').split(','), start=1):
        part = part.strip()
        if not part:
            continue
        try:
            if not _valid_key(part):
                raise ValueError
            fernets.append(Fernet(part.encode('ascii')))
        except (ValueError, TypeError, UnicodeEncodeError):
            raise SecretsKeyError(f"SECRETS_KEY invalide : l'entrée n°{position} n'est pas une clé Fernet "
                                  "(génère-en une avec `python -c \"from cryptography.fernet import Fernet; "
                                  "print(Fernet.generate_key().decode())\"`)") from None
    return fernets


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _updated_by(actor) -> str:
    return actor.label if actor.type == 'user' else actor.type


def _has_totp_column(conn) -> bool:
    return 'totp_secret' in {r['name'] for r in conn.execute("PRAGMA table_info(users)")}


class SecretsStore:
    def __init__(self, raw_keys: Optional[str] = None):
        fernets = parse_keys(raw_keys)
        self._fernet = MultiFernet(fernets) if fernets else None
        # Nombre de valeurs indéchiffrables constaté au démarrage, en mode « secours » de la CLI uniquement
        # (init_secrets) : seules reset() et status() fonctionnent alors, tout le reste refuse
        self.degraded: Optional[int] = None

    def _healthy(self) -> None:
        if self.degraded is not None:
            raise UndecryptableSecrets(self.degraded)

    @property
    def available(self) -> bool:
        return self._fernet is not None

    def _require(self) -> MultiFernet:
        if self._fernet is None:
            raise SecretsUnavailable("SECRETS_KEY n'est pas renseignée")
        return self._fernet

    # -- écriture ------------------------------------------------------------------------------
    def set(self, conn, name: str, plaintext, bound_to: str, *, actor: audit.Actor, ip: Optional[str] = None) -> None:
        """Enregistre (ou remplace) un secret, lié à sa destination effective normalisée `bound_to`."""
        self._healthy()
        kind = SECRET_KINDS.get(name)
        if kind is None:
            raise ValueError(f"Secret inconnu : {name!r}")
        if (not isinstance(plaintext, str) or not plaintext or len(plaintext) > MAX_SECRET_LENGTH
                or any(c in plaintext for c in '\r\n\x00')):
            raise ValueError(f"Secret : 1 à {MAX_SECRET_LENGTH} caractères, sans saut de ligne")
        if (not isinstance(bound_to, str) or not bound_to or len(bound_to) > MAX_DESTINATION_LENGTH
                or not all(c.isprintable() for c in bound_to)):
            raise ValueError("Destination liée invalide")
        token = self._require().encrypt(plaintext.encode('utf-8'))
        last4 = plaintext[-4:] if kind == 'api_key' and len(plaintext) >= LAST4_MIN_LENGTH else None
        with write_transaction(conn):
            conn.execute(
                "INSERT INTO secrets (name, ciphertext, last4, bound_to, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET ciphertext = excluded.ciphertext, last4 = excluded.last4, "
                "bound_to = excluded.bound_to, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                (name, token, last4, bound_to, _now(), _updated_by(actor)))
            audit.record_secret_event(conn, name, 'update', actor=actor, ip=ip)

    def clear(self, conn, name: str, *, actor: audit.Actor, ip: Optional[str] = None) -> bool:
        """Efface un secret. Retourne False s'il n'y en avait pas (rien n'est tracé)."""
        self._healthy()
        if name not in SECRET_KINDS:
            raise ValueError(f"Secret inconnu : {name!r}")
        with write_transaction(conn):
            if not conn.execute("DELETE FROM secrets WHERE name = ?", (name,)).rowcount:
                return False
            audit.record_secret_event(conn, name, 'clear', actor=actor, ip=ip)
        return True

    def clear_linked(self, conn, names, *, actor: audit.Actor, ip: Optional[str] = None) -> list:
        """
        Efface les secrets liés à un service dont la destination change (EF-5.5). À appeler DANS la
        transaction qui change la destination : les deux sont validés ou annulés ensemble. Retourne les noms
        effacés.
        """
        self._healthy()
        cleared = []
        with write_transaction(conn):
            for name in names:
                if name not in SECRET_KINDS:
                    raise ValueError(f"Secret inconnu : {name!r}")
                if conn.execute("DELETE FROM secrets WHERE name = ?", (name,)).rowcount:
                    audit.record_secret_event(conn, name, 'clear_on_destination_change', actor=actor, ip=ip)
                    cleared.append(name)
        return cleared

    # -- lecture -------------------------------------------------------------------------------
    def get(self, conn, name: str, destination: str, *, ip: Optional[str] = None) -> str:
        """
        Le secret en clair, POUR UN APPEL SORTANT vers `destination` (effective, normalisée). Destination
        différente de celle liée ⇒ refus audité, aucune valeur rendue. Ne jamais journaliser le résultat.
        """
        self._healthy()
        row = conn.execute("SELECT ciphertext, bound_to FROM secrets WHERE name = ?", (name,)).fetchone() \
            if name in SECRET_KINDS else None
        if row is None:
            raise SecretUnavailable()
        if not hmac.compare_digest(row['bound_to'].encode('utf-8'), str(destination).encode('utf-8')):
            audit.record_secret_event(conn, name, 'refused', actor=audit.Actor.system(), reason='destination différente',
                                      ip=ip)
            logger.warning("Secret %s refusé : sa destination liée n'est pas la destination effective", name)
            raise SecretUnavailable()
        try:
            return self._require().decrypt(_as_bytes(row['ciphertext'])).decode('utf-8')
        except InvalidToken:
            logger.error("Secret %s indéchiffrable avec les clés fournies", name)
            raise SecretUnavailable() from None

    def effective(self, conn, name: str, destination: str, env_secret: Optional[str] = None,
                  env_destination: Optional[str] = None) -> Optional[str]:
        """
        Base > env (ADR-0008). Un secret en base lié à une autre destination n'est pas rendu (et on ne
        retombe PAS sur l'env : la base prime). Un secret d'env n'est rendu que si `destination` est celle
        définie en env (`env_destination`), jamais si la base surcharge la destination. None : pas de secret.
        """
        self._healthy()
        exists = conn.execute("SELECT 1 FROM secrets WHERE name = ?", (name,)).fetchone() if name in SECRET_KINDS else None
        if exists:
            return self.get(conn, name, destination)
        if env_secret and env_destination is not None and hmac.compare_digest(
                str(env_destination).encode('utf-8'), str(destination).encode('utf-8')):
            return env_secret
        return None

    def status(self, conn, name: str) -> dict:
        """Ce que l'API peut montrer : jamais la valeur. `last4` seulement pour un secret de type api_key."""
        if name not in SECRET_KINDS:
            raise ValueError(f"Secret inconnu : {name!r}")
        row = conn.execute("SELECT last4, bound_to, updated_at, updated_by FROM secrets WHERE name = ?", (name,)).fetchone()
        if row is None:
            return {'name': name, 'configured': False}
        status = {'name': name, 'configured': True, 'bound_to': row['bound_to'], 'updated_at': row['updated_at'],
                  'updated_by': row['updated_by']}
        if SECRET_KINDS[name] == 'api_key':
            status['last4'] = row['last4']
        return status

    # -- démarrage, rotation, remise à zéro ----------------------------------------------------
    def _encrypted_rows(self, conn):
        """(table, colonne clé, valeur de clé, jeton) de toutes les valeurs chiffrées : secrets, puis TOTP."""
        for row in conn.execute("SELECT name, ciphertext FROM secrets"):
            yield 'secrets', 'name', row['name'], _as_bytes(row['ciphertext'])
        if _has_totp_column(conn):
            for row in conn.execute("SELECT id, totp_secret FROM users WHERE totp_secret IS NOT NULL"):
                yield 'users', 'id', row['id'], _as_bytes(row['totp_secret'])

    def verify_all(self, conn) -> int:
        """
        Contrôle de démarrage (EF-5.2, CA-20) : toute valeur chiffrée doit se déchiffrer. Lève
        UndecryptableSecrets(n) sinon, y compris sans aucune clé fournie alors qu'il y en a. Retourne le nombre
        de valeurs vérifiées.
        """
        total = bad = 0
        for _table, _column, _key, token in self._encrypted_rows(conn):
            total += 1
            try:
                self._require().decrypt(token)
            except (InvalidToken, SecretsUnavailable):
                bad += 1
        if bad:
            raise UndecryptableSecrets(bad)
        return total

    def rotate(self, conn, *, actor: audit.Actor) -> dict:
        """
        Rechiffre toutes les valeurs avec la PREMIÈRE clé (`flask secrets-rotate`), une ligne par transaction :
        une interruption laisse chaque ligne soit à l'ancienne, soit à la nouvelle clé, toutes lisibles. Une
        valeur indéchiffrable arrête l'opération (rien de plus n'est touché). Retourne les comptes ; `skipped` : lignes
        modifiées par ailleurs pendant la rotation (non écrasées : écriture conditionnelle sur la valeur lue).
        """
        self._healthy()
        fernet = self._require()
        counts = {'secrets': 0, 'totp': 0}
        skipped = 0
        for table, column, key, token in list(self._encrypted_rows(conn)):
            try:
                renewed = fernet.rotate(token)
            except InvalidToken:
                raise UndecryptableSecrets(1) from None
            target = 'ciphertext' if table == 'secrets' else 'totp_secret'
            with write_transaction(conn):
                # Compare-and-swap sur la valeur lue : une écriture intercalée (nouveau secret, ré-enrôlement TOTP)
                # n'est jamais écrasée par l'ancienne valeur rechiffrée
                changed = conn.execute(f"UPDATE {table} SET {target} = ? WHERE {column} = ? AND {target} = ?",
                                       (renewed, key, token)).rowcount
            if not changed:
                skipped += 1
            elif table == 'secrets':
                counts['secrets'] += 1
            else:
                counts['totp'] += 1
        with write_transaction(conn):
            # clés neutres : « secrets » / « totp » seraient masquées par le filet de l'audit
            audit.record(conn, 'secret.rotate', actor=actor, details={'values': counts['secrets'], 'accounts': counts['totp'],
                                                                    'skipped': skipped})
        return {**counts, 'skipped': skipped} if skipped else counts

    def reset(self, conn, *, actor: audit.Actor) -> dict:
        """
        Perte de toutes les clés (`flask secrets-reset`) : efface les secrets et les secrets TOTP. Les clés
        WebAuthn, non chiffrées, restent. Le retrait du rôle aux admins `password` sans plus aucun facteur
        (EF-5.7) dépend des facteurs (T11) : à brancher là.
        """
        with write_transaction(conn):
            counts = {'secrets': conn.execute("DELETE FROM secrets").rowcount, 'totp': 0}
            if _has_totp_column(conn):
                counts['totp'] = conn.execute(
                    "UPDATE users SET totp_secret = NULL WHERE totp_secret IS NOT NULL").rowcount
                if _has_columns(conn, 'totp_last_step', 'totp_enabled_at'):
                    conn.execute("UPDATE users SET totp_last_step = NULL, totp_enabled_at = NULL")
            audit.record(conn, 'secret.reset', actor=actor, details={'values': counts['secrets'], 'accounts': counts['totp']})
        return counts


def _has_columns(conn, *names) -> bool:
    existing = {r['name'] for r in conn.execute("PRAGMA table_info(users)")}
    return all(n in existing for n in names)


def _rescue_cli_context() -> bool:
    """
    Vrai quand l'application est chargée pour une commande `flask <commande>` autre que `flask run` : c'est la
    CLI de secours (T4-1). Le service (gunicorn, `python kml_viewer.py`, `flask run`) ne l'est jamais : il refuse
    de démarrer. Détection : Flask pose FLASK_RUN_FROM_CLI pour toute commande ; `flask run` charge l'application
    depuis son propre contexte (commande « run »), les autres depuis celui du groupe.
    """
    import os

    import click
    if os.environ.get('FLASK_RUN_FROM_CLI') != 'true':
        return False
    context = click.get_current_context(silent=True)
    return context is not None and getattr(context.command, 'name', None) != 'run'


def init_secrets(app) -> SecretsStore:
    """
    Démarrage : valide `SECRETS_KEY` et vérifie que tout ce qui est chiffré se déchiffre (CA-20), sinon
    refuse de démarrer (RuntimeError, sans valeur ni clé). Exception : la CLI de secours (`flask secrets-reset`
    doit pouvoir s'exécuter quand la clé est perdue, c'est sa raison d'être) : le store démarre alors en mode
    dégradé (`degraded` = nombre de valeurs indéchiffrables), où seules `reset` et `status` fonctionnent.
    La clé n'est ni dans `Config` ni conservée dans `app.config` : lue dans `app.config` (tests, surcharge) puis
    dans l'environnement.
    """
    import os

    from app.db import connect
    raw = app.config.pop('SECRETS_KEY', None)
    if raw is None:
        raw = os.environ.get('SECRETS_KEY', '')
    rescue = _rescue_cli_context()
    try:
        try:
            store = SecretsStore(raw)
        except SecretsKeyError:
            if not rescue:
                raise
            store = SecretsStore('')                   # clé inutilisable : tout le chiffré est indéchiffrable
            logger.error("SECRETS_KEY invalide : mode secours de la CLI (seul `secrets-reset` fonctionne)")
        conn = connect(app.config['DB_PATH'])
        try:
            checked = store.verify_all(conn)
        finally:
            conn.close()
    except UndecryptableSecrets as exc:
        if not rescue:
            raise RuntimeError(f"Secrets : {exc}. Démarrage refusé. Clé perdue : `flask secrets-reset` "
                               "(efface les secrets, à ressaisir).") from None
        store.degraded = exc.count
        logger.error("%s : mode secours de la CLI (seul `secrets-reset` fonctionne)", exc)
        checked = 0
    except SecretsError as exc:
        raise RuntimeError(f"Secrets : {exc}. Démarrage refusé.") from None
    if checked:
        logger.info("Secrets chiffrés vérifiés : %d valeur(s)", checked)
    app.extensions['secrets'] = store
    return store
