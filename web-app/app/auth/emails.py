"""Forme canonique des adresses e-mail (admin-space EF-4.10, §13 n°32) : module pur, partagé par tout le code."""

import unicodedata

MAX_EMAIL = 254


def canonical_email(email) -> str:
    """
    `NFKC(adresse.strip()).casefold()` (partie locale et domaine ; pas de conversion IDNA). C'est la forme STOCKÉE et COMPARÉE
    partout : le `lower()` de SQL ne replie que l'ASCII. Lève ValueError pour une adresse inutilisable.
    """
    if not isinstance(email, str) or len(email) > MAX_EMAIL * 2 or not email.strip():
        raise ValueError("Adresse e-mail invalide")
    value = unicodedata.normalize('NFKC', email.strip()).casefold()
    if (len(value) > MAX_EMAIL or '@' not in value
            or any(c.isspace() or ord(c) < 0x20 or ord(c) == 0x7f for c in value)):
        raise ValueError("Adresse e-mail invalide")
    return value


def canonical_or_none(email):
    """Forme canonique, ou None si l'adresse est absente ou inutilisable (jamais d'exception)."""
    try:
        return canonical_email(email)
    except ValueError:
        return None


def validate_email(email) -> str:
    """
    VALIDATEUR UNIQUE de syntaxe (mailer, invitations, changement d'adresse) : forme canonique d'une adresse unique, sinon
    ValueError. Une seule arobase, parties non vides, pas de `..`, pas de séparateur d'en-tête (`<>,;"\\`), pas de caractère
    de contrôle ni de format invisible (U+200B, U+202E…).
    """
    value = canonical_email(email)
    local, _, domain = value.partition('@')
    if (not local or not domain or '@' in domain or len(local) > 64 or '..' in value
            or domain.startswith('.') or domain.endswith('.') or any(c in value for c in '<>,;"\\')
            or any(unicodedata.category(c) in ('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp') for c in value)):
        raise ValueError("Adresse e-mail invalide")
    return value
