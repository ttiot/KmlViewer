"""
Clés de sécurité WebAuthn / FIDO2 comme second facteur (admin-space ADR-0004, EF-2.1, EF-2.4, EF-2.8, EF-S9 ; T12).

  - RP ID = hôte de `PUBLIC_URL` (surcharge `WEBAUTHN_RP_ID`, suffixe de domaine de cet hôte) ; origine attendue = origine de
    `PUBLIC_URL` ; `http://localhost` accepté SEULEMENT en test ou en mode debug (M12) ;
  - attestation `none`, vérification utilisateur `preferred`, au plus 10 clés par compte ;
  - le défi (32 octets) vit CÔTÉ SERVEUR (`pending.py`), lié à son propriétaire (état de connexion, d'enrôlement ou session du
    profil), valable 2 min et CONSOMMÉ atomiquement : un défi réutilisé, y compris via un cookie rejoué, est refusé ;
  - la régression d'un compteur de signatures non nul est REFUSÉE et auditée ;
  - jamais de détail de la bibliothèque dans une réponse : `WebAuthnError` porte un message fixe.
Module sans Flask ; la base et le magasin d'états sont passés en paramètre.
"""

import json
import logging
import os
import secrets
import sqlite3
from typing import Optional
from urllib.parse import urlsplit

from webauthn import (generate_authentication_options, generate_registration_options, options_to_json,
                      verify_authentication_response, verify_registration_response)
from webauthn.helpers import base64url_to_bytes
from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                      UserVerificationRequirement)

from app import audit
from app.db import write_transaction

logger = logging.getLogger(__name__)

RP_NAME = 'KmlViewer'
MAX_KEYS = 10
MAX_NAME = 60
CHALLENGE_BYTES = 32
MAX_CREDENTIAL_JSON = 16 * 1024


class WebAuthnError(Exception):
    """Refus (défi, origine, signature, compteur…) : message FIXE, le détail va au journal sans donnée de la clé."""

    def __init__(self, message: str = "Clé de sécurité refusée"):
        super().__init__(message)


# -- configuration ---------------------------------------------------------------------------------------------------------

def _public(cfg):
    url = cfg.get('PUBLIC_URL') or ''
    parts = urlsplit(url)
    return parts.scheme, (parts.hostname or ''), parts.port


def rp_id(cfg) -> Optional[str]:
    """RP ID : surcharge `WEBAUTHN_RP_ID` (suffixe de domaine de l'hôte) ou l'hôte de PUBLIC_URL ; None si inutilisable."""
    _scheme, host, _port = _public(cfg)
    if not host:
        return None
    override = (cfg.get('WEBAUTHN_RP_ID') or os.environ.get('WEBAUTHN_RP_ID') or '').strip().lower()
    if override:
        if host == override or host.endswith('.' + override):
            return override
        logger.error("WEBAUTHN_RP_ID n'est pas un suffixe de l'hôte de PUBLIC_URL : WebAuthn indisponible")
        return None
    return host


def expected_origin(cfg) -> Optional[str]:
    """Origine de PUBLIC_URL ; `http` n'est accepté que pour `localhost` en test ou en mode debug (EF-2.4, M12)."""
    scheme, host, port = _public(cfg)
    if not host or scheme not in ('http', 'https'):
        return None
    if scheme == 'http' and not (host == 'localhost' and (cfg.get('TESTING') or cfg.get('DEBUG'))):
        return None
    default = 443 if scheme == 'https' else 80
    return f"{scheme}://{host}" + (f":{port}" if port and port != default else '')


def available(cfg) -> bool:
    return rp_id(cfg) is not None and expected_origin(cfg) is not None


# -- état des clés ----------------------------------------------------------------------------------------------------------

def keys_of(conn: sqlite3.Connection, user_id: int) -> list:
    return conn.execute("SELECT id, credential_id, public_key, sign_count, name, created_at, last_used_at "
                        "FROM webauthn_credentials WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()


def clean_name(value, default: str) -> str:
    if value is None or value == '':
        return default
    if not isinstance(value, str) or any(not c.isprintable() for c in value) or not 1 <= len(value.strip()) <= MAX_NAME:
        raise WebAuthnError("Nom de clé invalide")
    return value.strip()


def _credential_text(credential) -> str:
    """La réponse du navigateur, comme texte JSON borné (jamais un objet géant)."""
    text = credential if isinstance(credential, str) else json.dumps(credential)
    if len(text) > MAX_CREDENTIAL_JSON:
        raise WebAuthnError()
    return text


# -- enregistrement ----------------------------------------------------------------------------------------------------------

def registration_options(cfg, pending, owner: str, user, existing) -> str:
    """Options JSON pour `navigator.credentials.create`, défi retenu côté serveur sous `owner`."""
    if not available(cfg):
        raise WebAuthnError("WebAuthn indisponible")
    if len(existing) >= MAX_KEYS:
        raise WebAuthnError("10 clés au plus par compte")
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    options = generate_registration_options(
        rp_id=rp_id(cfg), rp_name=RP_NAME, user_name=user['username'], user_id=str(user['id']).encode('ascii'),
        user_display_name=user['username'], challenge=challenge,
        exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(k['credential_id'])) for k in existing if k['credential_id']],
        authenticator_selection=AuthenticatorSelectionCriteria(user_verification=UserVerificationRequirement.PREFERRED))
    pending.put_challenge(owner, challenge)
    return options_to_json(options)


def verify_registration(cfg, pending, owner: str, credential):
    """Vérifie la réponse contre le défi (consommé) ; retourne (credential_id, public_key, sign_count, transports)."""
    challenge = pending.take_challenge(owner)                  # atomique : un défi ne sert qu'une fois
    if challenge is None or not available(cfg):
        raise WebAuthnError()
    text = _credential_text(credential)
    try:
        verified = verify_registration_response(credential=text, expected_challenge=challenge, expected_rp_id=rp_id(cfg),
                                                expected_origin=expected_origin(cfg))
    except Exception:  # noqa: BLE001 - jamais le détail de la bibliothèque
        logger.warning("WebAuthn : enregistrement refusé")
        raise WebAuthnError() from None
    transports = None
    try:
        raw = json.loads(text).get('response', {}).get('transports')
        if isinstance(raw, list):
            transports = ','.join(str(t)[:20] for t in raw[:8])
    except (ValueError, AttributeError):
        pass
    return verified.credential_id, verified.credential_public_key, int(verified.sign_count), transports


def add_credential(conn: sqlite3.Connection, user_id: int, credential_id: bytes, public_key: bytes, sign_count: int,
                   transports: Optional[str], name: str) -> int:
    """Enregistre la clé (dans la transaction de l'appelant). Une clé déjà connue (même identifiant) est refusée."""
    if conn.execute("SELECT COUNT(*) FROM webauthn_credentials WHERE user_id = ?", (user_id,)).fetchone()[0] >= MAX_KEYS:
        raise WebAuthnError("10 clés au plus par compte")
    try:
        cursor = conn.execute(
            "INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, transports, name) "
            "VALUES (?, ?, ?, ?, ?, ?)", (user_id, credential_id, public_key, sign_count, transports, name))
    except sqlite3.IntegrityError:
        raise WebAuthnError("Clé déjà enregistrée") from None
    return cursor.lastrowid


# -- authentification ----------------------------------------------------------------------------------------------------------

def authentication_options(cfg, pending, owner: str, keys) -> str:
    """Options JSON pour `navigator.credentials.get` limitées aux clés du compte."""
    if not available(cfg) or not keys:
        raise WebAuthnError("WebAuthn indisponible")
    challenge = secrets.token_bytes(CHALLENGE_BYTES)
    options = generate_authentication_options(
        rp_id=rp_id(cfg), challenge=challenge, user_verification=UserVerificationRequirement.PREFERRED,
        allow_credentials=[PublicKeyCredentialDescriptor(id=bytes(k['credential_id'])) for k in keys if k['credential_id']])
    pending.put_challenge(owner, challenge)
    return options_to_json(options)


def verify_authentication(conn: sqlite3.Connection, cfg, pending, owner: str, user, credential) -> sqlite3.Row:
    """
    Vérifie l'assertion contre le défi consommé. Retourne la ligne de la clé utilisée (compteur et date mis à jour de façon
    conditionnelle). Régression d'un compteur non nul : refus ET audit `webauthn.counter_regression`.
    """
    challenge = pending.take_challenge(owner)
    if challenge is None or not available(cfg):
        raise WebAuthnError()
    text = _credential_text(credential)
    try:
        raw_id = base64url_to_bytes(json.loads(text)['rawId'])
    except Exception:  # noqa: BLE001
        raise WebAuthnError() from None
    row = conn.execute("SELECT * FROM webauthn_credentials WHERE user_id = ? AND credential_id = ?",
                       (user['id'], raw_id)).fetchone()
    if row is None:
        raise WebAuthnError()
    try:
        verified = verify_authentication_response(
            credential=text, expected_challenge=challenge, expected_rp_id=rp_id(cfg), expected_origin=expected_origin(cfg),
            credential_public_key=bytes(row['public_key']), credential_current_sign_count=int(row['sign_count']))
    except Exception as exc:  # noqa: BLE001 - jamais le détail de la bibliothèque
        if 'sign count' in str(exc).lower():
            with write_transaction(conn):
                audit.record(conn, 'webauthn.counter_regression', actor=audit.Actor.from_user(user), target_type='user',
                             target_id=user['id'], details={'key': row['id']})
            logger.warning("WebAuthn : compteur de signatures en régression (clé #%s)", row['id'])
        else:
            logger.warning("WebAuthn : authentification refusée")
        raise WebAuthnError() from None
    new_count = int(verified.new_sign_count)
    with write_transaction(conn):
        # Écriture conditionnelle : deux assertions concurrentes avec le même compteur ne passent pas toutes les deux
        changed = conn.execute(
            "UPDATE webauthn_credentials SET sign_count = ?, last_used_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND sign_count = ?", (new_count, row['id'], row['sign_count'])).rowcount
    if not changed:
        raise WebAuthnError()
    return row


def remove_credential(conn: sqlite3.Connection, user_id: int, key_id: int) -> bool:
    return conn.execute("DELETE FROM webauthn_credentials WHERE id = ? AND user_id = ?", (key_id, user_id)).rowcount == 1
