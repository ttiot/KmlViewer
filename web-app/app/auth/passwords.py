"""
Mots de passe : politique, hachage argon2id, vérification, rehachage (ADR-0002, EF-1.4, EF-1.5).

Module pur : ni Flask, ni base, ni limiteur. Les routes (app/auth/password_login.py) consultent
le limiteur AVANT d'appeler ce module (r9) : une requête refusée ne hache rien.

Tout hachage ou vérification, hash factice compris, passe par un sémaphore de 2 places (m4) :
un hash argon2id coûte ~64 Mio, et la route de connexion est publique. Attente d'au plus 5 s,
puis HashBusy, que l'appelant traduit en un 429 générique identique pour tous les identifiants.
Aucun mot de passe ni hash n'est jamais journalisé (EF-S5).
"""

import contextlib
import logging
import secrets
import threading
import unicodedata
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


logger = logging.getLogger(__name__)

MIN_LENGTH = 12
MAX_LENGTH = 128   # mesuré après NFKC ; borne le coût CPU d'une requête publique
# Borne sur la saisie brute, avant même NFKC (qui coûte en proportion de l'entrée) : au-delà,
# aucune forme normalisée ne tiendrait en MAX_LENGTH de façon plausible
_RAW_LIMIT = 1024
HASH_SLOTS = 2
HASH_WAIT_S = 5.0

# Paramètres figés ici (RFC 9106, profil « mémoire réduite » : valeurs par défaut d'argon2-cffi
# 25.x). Les relever plus tard fait rehacher chaque mot de passe à la connexion suivante.
_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16)


# Liste de mots de passe courants (EF-1.4b) : fichiers embarqués, générés par
# scripts/build_common_passwords.py ; l'application ne télécharge jamais rien
WORDLIST = Path(__file__).resolve().parent / 'wordlists' / 'common_passwords.txt'
_common = None
_common_lock = threading.Lock()


class CommonPasswordsError(RuntimeError):
    """Liste absente, illisible ou vide : jamais de politique silencieusement affaiblie."""


class HashBusy(Exception):
    """Aucune place de hachage libre dans le délai : l'appelant répond un 429 générique."""


class PolicyError(ValueError):
    """Mot de passe refusé par la politique. `code` est stable ; le message est affichable."""

    MESSAGES = {
        'too_short': f"Le mot de passe doit faire au moins {MIN_LENGTH} caractères.",
        'too_long': f"Le mot de passe doit faire au plus {MAX_LENGTH} caractères.",
        'same_as_identity': "Le mot de passe ne doit pas être ton identifiant ou ton adresse e-mail.",
        'too_common': "Ce mot de passe est trop courant.",
        'invalid_characters': "Le mot de passe contient un caractère non valide.",
    }

    def __init__(self, code: str):
        super().__init__(self.MESSAGES[code])
        self.code = code


class HashGate:
    """Sémaphore de hachage : `slots` places, attente `wait_s` au plus. Injectable en test."""

    def __init__(self, slots: int = HASH_SLOTS, wait_s: float = HASH_WAIT_S):
        self._semaphore = threading.BoundedSemaphore(slots)
        self._wait_s = wait_s

    @contextlib.contextmanager
    def slot(self):
        if not self._semaphore.acquire(timeout=self._wait_s):
            logger.warning("Hachage refusé : aucune place libre après %.1f s", self._wait_s)
            raise HashBusy()
        try:
            yield
        finally:
            self._semaphore.release()


_gate = HashGate()
# Hash d'un secret aléatoire jeté, calculé une fois à l'import : sert à vérifier « quelque chose »
# quand le compte n'existe pas ou que son hash est illisible
_DUMMY_HASH = _HASHER.hash(secrets.token_urlsafe(24))


def normalize(password: str) -> str:
    """NFKC : la longueur, la politique et le hachage portent tous sur cette forme."""
    return unicodedata.normalize('NFKC', password)


def _encodable(text: str) -> bool:
    """Faux pour un surrogat isolé (ex. JSON "\\ud800") : valide pour Python, non encodable en UTF-8."""
    try:
        text.encode('utf-8')
    except UnicodeEncodeError:
        return False
    return True


def load_common_passwords(path=None) -> frozenset:
    """
    Lit la liste (UTF-8, une entrée par ligne, déjà NFKC + casefold) dans un frozenset. Lève
    CommonPasswordsError si le fichier est absent, illisible ou vide : à appeler au démarrage
    quand les comptes à mot de passe sont disponibles, pour refuser de démarrer plutôt que
    d'accepter n'importe quel mot de passe. On coupe sur \\n seulement (pas splitlines : une
    entrée peut contenir U+2028).
    """
    target = Path(path) if path else WORDLIST
    try:
        text = target.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise CommonPasswordsError(f"Liste de mots de passe courants illisible : {target.name}") from exc
    entries = frozenset(line for line in text.split('\n') if line)
    if not entries:
        raise CommonPasswordsError(f"Liste de mots de passe courants vide : {target.name}")
    return entries


def common_passwords() -> frozenset:
    """La liste, chargée une fois par processus (au premier usage si le démarrage ne l'a pas déjà fait)."""
    global _common
    if _common is None:
        with _common_lock:
            if _common is None:
                _common = load_common_passwords()
                logger.info("Liste de mots de passe courants chargée : %d entrées", len(_common))
    return _common


def init_common_passwords() -> int:
    """Charge la liste au démarrage (mode avec comptes à mot de passe) ; CommonPasswordsError si absente ou vide."""
    return len(common_passwords())


def __getattr__(name):
    if name == 'COMMON_PASSWORDS':          # compatibilité : accès paresseux, la liste n'est pas lue à l'import
        return common_passwords()
    raise AttributeError(name)


def is_common(password: str) -> bool:
    return normalize(password).casefold() in common_passwords()


def check_policy(password, identities=()) -> None:
    """
    Lève PolicyError si le mot de passe est refusé : longueur (après NFKC) hors de 12–128,
    égal (casse ignorée) à l'un des `identities` (identifiant, e-mail), ou courant. Aucune
    règle de composition. Une valeur qui n'est pas une chaîne est refusée comme trop courte.
    """
    if not isinstance(password, str):
        raise PolicyError('too_short')
    if len(password) > _RAW_LIMIT:
        raise PolicyError('too_long')
    normalized = normalize(password)
    if len(normalized) < MIN_LENGTH:
        raise PolicyError('too_short')
    if len(normalized) > MAX_LENGTH:
        raise PolicyError('too_long')
    if not _encodable(normalized):
        raise PolicyError('invalid_characters')
    folded = normalized.casefold()
    for identity in identities:
        if identity and folded == normalize(str(identity)).strip().casefold():
            raise PolicyError('same_as_identity')
    if folded in common_passwords():
        raise PolicyError('too_common')


def hash_password(password: str) -> str:
    """Hash argon2id (sel aléatoire, paramètres encodés). La politique est vérifiée par l'appelant."""
    if not isinstance(password, str) or len(password) > _RAW_LIMIT:
        raise ValueError("Longueur de mot de passe hors bornes")
    normalized = normalize(password)
    if not MIN_LENGTH <= len(normalized) <= MAX_LENGTH:
        raise ValueError("Longueur de mot de passe hors bornes")
    if not _encodable(normalized):
        raise ValueError("Caractère non valide dans le mot de passe")
    with _gate.slot():
        return _HASHER.hash(normalized)


def verify_password(stored_hash, password):
    """
    Vérifie `password` contre `stored_hash` et retourne `(ok, new_hash)`. `new_hash` n'est
    renseigné que si la vérification réussit ET que les paramètres ont changé : l'appelant
    l'enregistre alors à la place de l'ancien (rehachage à la connexion).

    `stored_hash` None (compte inconnu, désactivé, SSO…) ou illisible : un hash factice est
    vérifié à la place, pour que le temps de réponse ne révèle rien. Un mot de passe hors
    bornes est refusé sans hacher : le coût est ainsi borné, et la durée ne dépend pas du compte.
    Lève HashBusy si le sémaphore reste saturé.
    """
    if not isinstance(password, str) or len(password) > _RAW_LIMIT:
        return False, None
    normalized = normalize(password)
    if not 1 <= len(normalized) <= MAX_LENGTH or not _encodable(normalized):
        return False, None      # même refus qu'un mot de passe faux, sans hacher

    with _gate.slot():
        if not isinstance(stored_hash, str) or not stored_hash:
            _verify_discard(_DUMMY_HASH, normalized)
            return False, None
        try:
            _HASHER.verify(stored_hash, normalized)
        except VerifyMismatchError:
            return False, None
        except (InvalidHashError, VerificationError):
            # Hash corrompu ou d'un format inconnu : jamais un succès. Le contenu n'est pas journalisé.
            logger.error("Hash de mot de passe illisible en base, connexion refusée")
            _verify_discard(_DUMMY_HASH, normalized)
            return False, None
        new_hash = _HASHER.hash(normalized) if _HASHER.check_needs_rehash(stored_hash) else None
    return True, new_hash


def _verify_discard(hashed: str, password: str) -> None:
    with contextlib.suppress(VerificationError, InvalidHashError):
        _HASHER.verify(hashed, password)
