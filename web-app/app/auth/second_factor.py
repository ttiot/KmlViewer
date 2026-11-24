"""
TOTP et codes de secours (admin-space ADR-0004, EF-2.3, EF-2.4 ; T11). Module sans Flask : la connexion, l'enrôlement et le profil
l'appellent, la base et le magasin de secrets lui sont passés.

TOTP : RFC 6238, SHA-1, 6 chiffres, pas de 30 s, fenêtre ±1 ; secret de 160 bits chiffré au repos (Fernet, ADR-0003) ;
anti-rejeu ATOMIQUE : un code n'est accepté que si son pas est STRICTEMENT supérieur au dernier pas accepté, par une mise à
jour conditionnelle (deux requêtes simultanées avec le même code : une seule passe).
Codes de secours : 10 codes de 16 caractères base32 (80 bits), stockés par empreinte SHA-256 de la forme normalisée, usage unique.
"""

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from typing import List, Optional
from urllib.parse import quote

import pyotp
import segno

ISSUER = 'KmlViewer'
STEP_S = 30
DIGITS = 6
WINDOW = 1
RECOVERY_COUNT = 10
RECOVERY_LENGTH = 16
_TOTP_CODE = re.compile(r'[0-9]{6}')
_RECOVERY_CHARS = re.compile(r'[A-Z2-7]{16}')
_BASE32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'


# -- TOTP ---------------------------------------------------------------------------------------------------------------------

def new_secret() -> str:
    """Secret de 160 bits en base32 (32 caractères)."""
    return pyotp.random_base32(32)


def grouped(secret: str) -> str:
    """Secret par groupes de 4 pour la saisie manuelle."""
    return ' '.join(secret[i:i + 4] for i in range(0, len(secret), 4))


def provisioning_uri(secret: str, account: str) -> str:
    """URI d'approvisionnement (P-T11-3) : émetteur constant, paramètres explicites."""
    return (f"otpauth://totp/{ISSUER}:{quote(account, safe='')}?secret={secret}&issuer={ISSUER}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}")


def qr_svg(uri: str) -> str:
    """QR code SVG produit côté serveur (aucun service externe), sans script ni style en ligne."""
    return segno.make(uri, error='m', micro=False).svg_inline(scale=5, border=2, dark='#000000', light='#ffffff')


def normalize_totp(code) -> Optional[str]:
    """Six chiffres ASCII (espaces ignorés), sinon None : jamais les chiffres d'autres écritures."""
    if not isinstance(code, str):
        return None
    text = code.replace(' ', '')
    return text if _TOTP_CODE.fullmatch(text) else None


def matching_step(secret: str, code: str, now: Optional[float] = None) -> Optional[int]:
    """Pas (entier) dont le code vaut `code` dans la fenêtre ±1, ou None. Les trois comparaisons sont toujours faites."""
    moment = time.time() if now is None else now
    current = int(moment // STEP_S)
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=STEP_S)
    found = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(totp.at(step * STEP_S), code) and found is None:
            found = step
    return found


def verify_totp(conn: sqlite3.Connection, store, user_id: int, code, *, now: Optional[float] = None) -> bool:
    """
    Vrai si `code` est le code TOTP valide du compte ET n'a pas déjà servi (pas strictement supérieur au dernier accepté). Le
    pas est enregistré par une mise à jour conditionnelle : le premier de deux envois simultanés gagne.
    """
    normalized = normalize_totp(code)
    row = conn.execute("SELECT totp_secret, totp_enabled_at, totp_last_step FROM users WHERE id = ?", (user_id,)).fetchone()
    if normalized is None or row is None or row['totp_secret'] is None or row['totp_enabled_at'] is None:
        return False
    try:
        secret = store.decrypt_value(row['totp_secret']).decode('ascii')
    except Exception:  # noqa: BLE001 - secret illisible (clé perdue, base éditée) : jamais un succès, jamais une erreur 500
        return False
    step = matching_step(secret, normalized, now)
    if step is None:
        return False
    cursor = conn.execute("UPDATE users SET totp_last_step = ? WHERE id = ? AND (totp_last_step IS NULL OR totp_last_step < ?)",
                          (step, user_id, step))
    conn.commit()
    return cursor.rowcount == 1


# -- Codes de secours ---------------------------------------------------------------------------------------------------------

def normalize_recovery(code) -> Optional[str]:
    """Espaces et tirets ignorés, casse ignorée ; 16 caractères base32 ou None."""
    if not isinstance(code, str):
        return None
    text = re.sub(r'[\s-]', '', code).upper()
    return text if _RECOVERY_CHARS.fullmatch(text) else None


def _hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode('ascii')).hexdigest()


def _new_recovery_code() -> str:
    return ''.join(secrets.choice(_BASE32) for _ in range(RECOVERY_LENGTH))


def format_recovery(code: str) -> str:
    return '-'.join(code[i:i + 4] for i in range(0, len(code), 4))


def replace_recovery_codes(conn: sqlite3.Connection, user_id: int) -> List[str]:
    """
    Remplace TOUS les codes du compte par `RECOVERY_COUNT` neufs (dans la transaction de l'appelant) et retourne leur forme
    affichée `XXXX-XXXX-XXXX-XXXX`, à montrer UNE fois : seules les empreintes sont conservées.
    """
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    codes = set()
    while len(codes) < RECOVERY_COUNT:
        codes.add(_new_recovery_code())
    for code in codes:
        conn.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)", (user_id, _hash(code)))
    return [format_recovery(c) for c in sorted(codes)]


def consume_recovery(conn: sqlite3.Connection, user_id: int, code) -> bool:
    """Marque le code utilisé de façon atomique (usage unique) ; faux s'il est inconnu, déjà utilisé ou mal formé."""
    normalized = normalize_recovery(code)
    if normalized is None:
        return False
    cursor = conn.execute("UPDATE recovery_codes SET used_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                          "WHERE user_id = ? AND code_hash = ? AND used_at IS NULL", (user_id, _hash(normalized)))
    conn.commit()
    return cursor.rowcount == 1


def remaining_recovery(conn: sqlite3.Connection, user_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL", (user_id,)).fetchone()[0]


def kind_of(code) -> Optional[str]:
    """`totp` (6 chiffres) ou `recovery` (16 caractères base32), distingués par la forme (P-T11-5), sinon None."""
    if normalize_totp(code) is not None:
        return 'totp'
    if normalize_recovery(code) is not None:
        return 'recovery'
    return None
