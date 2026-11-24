"""
Identifiants de compte à mot de passe (admin-space EF-1.2, EF-1.2b).

Seule la validation du FORMAT et des identifiants RÉSERVÉS vit ici ; l'unicité (insensible à la casse,
index unique partiel) relève de la base. Appelé partout où un identifiant `password` est CHOISI :
acceptation d'invitation, amorçage, conversion SSO -> mot de passe. Pas pour les identifiants SSO donnés
par l'IdP (non refusables à la connexion) : c'est pourquoi l'audit s'appuie sur `actor_type`, pas sur
le libellé (EF-9.1, CA-24b). Défense en profondeur seulement.
"""

import re
import unicodedata

# Constante dans le code, pas dans le registre de réglages (EF-1.2b)
RESERVED_IDENTIFIERS = frozenset({'cli', 'system', 'local', 'admin', 'root'})
UNAVAILABLE_MESSAGE = "Identifiant indisponible"      # identique pour « réservé » et « déjà pris » : pas d'oracle
FORMAT_MESSAGE = "Identifiant : 3 à 64 caractères parmi a-z, 0-9, « . », « _ » et « - »."
_FORMAT = re.compile(r'[a-z0-9._-]{3,64}')
_RAW_LIMIT = 256


class IdentifierError(ValueError):
    """Identifiant refusé ; `message` est affichable tel quel. `unavailable` : même message qu'un identifiant pris."""

    def __init__(self, message: str, unavailable: bool = False):
        super().__init__(message)
        self.message, self.unavailable = message, unavailable


def normalize_identifier(value) -> str:
    """NFKC puis casefold, sans blancs de bord : la forme comparée et stockée."""
    if not isinstance(value, str) or len(value) > _RAW_LIMIT:
        return ''
    return unicodedata.normalize('NFKC', value).strip().casefold()


def is_reserved(value) -> bool:
    return normalize_identifier(value) in RESERVED_IDENTIFIERS


def check_identifier(value) -> str:
    """Retourne l'identifiant normalisé, ou lève IdentifierError (format, ou réservé => « indisponible »)."""
    identifier = normalize_identifier(value)
    if not _FORMAT.fullmatch(identifier):
        raise IdentifierError(FORMAT_MESSAGE)
    if identifier in RESERVED_IDENTIFIERS:
        raise IdentifierError(UNAVAILABLE_MESSAGE, unavailable=True)
    return identifier
