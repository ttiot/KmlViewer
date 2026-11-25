"""
Contrôles de configuration au démarrage : une configuration dangereuse empêche l'application
de démarrer, plutôt que de la laisser tourner ouverte.
"""

import logging
import os
from urllib.parse import urlsplit

from werkzeug.sansio.utils import host_is_trusted

from app.config import DEFAULT_SECRET_KEY
from app.outbound import Allowlist, OutboundError
from app.secrets_store import SecretsKeyError, parse_keys

logger = logging.getLogger(__name__)

AUTH_MODES = ('oidc', 'local', 'password')
MIN_SECRET_KEY_LENGTH = 32
# Réglages qui n'ont de sens que derrière un reverse proxy ou avec l'OIDC : les trouver sans
# OIDC_ISSUER trahit une mise en production dont l'authentification a sauté.
_PRODUCTION_HINTS = ('TRUSTED_HOSTS', 'TRUST_PROXY', 'SESSION_COOKIE_SECURE', 'OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET')


def _auth_mode(config) -> str:
    """
    Mode retenu ('oidc', 'local' ou 'password'). Lève RuntimeError si la configuration est incohérente.
    Le mode `password` s'écrit toujours explicitement : le mode déduit (variable vide) ne le choisit jamais.
    """
    wanted = config.get('AUTH_MODE') or ''
    issuer = config.get('OIDC_ISSUER')
    if wanted not in ('', *AUTH_MODES):
        raise RuntimeError(f"AUTH_MODE invalide : « {wanted} » (attendu : oidc, local ou password)")
    if wanted == 'password':
        if issuer:
            raise RuntimeError("AUTH_MODE=password alors qu'OIDC_ISSUER est défini : retire l'un des deux "
                               "(le mode password n'a pas de SSO).")
        return 'password'
    if wanted == 'oidc' and not issuer:
        raise RuntimeError("AUTH_MODE=oidc mais OIDC_ISSUER est vide : démarrage refusé, "
                           "l'application tournerait sans authentification.")
    if wanted == 'local' and issuer:
        raise RuntimeError("AUTH_MODE=local alors qu'OIDC_ISSUER est défini : retire l'un des deux.")
    if issuer:
        return 'oidc'
    if not wanted and not config.get('TESTING'):
        hints = [name for name in _PRODUCTION_HINTS if config.get(name)]
        if hints:
            raise RuntimeError(
                f"Pas d'OIDC_ISSUER alors que {', '.join(hints)} est renseigné : cette configuration "
                "ressemble à une mise en production, et sans OIDC chaque visiteur serait "
                "administrateur. Renseigne OIDC_ISSUER, ou AUTH_MODE=local si le mode "
                "mono-utilisateur sans authentification est voulu.")
    return 'local'


def _check_oidc(config) -> None:
    missing = [k for k in ('OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET') if not config.get(k)]
    if missing:
        raise RuntimeError(f"OIDC activé mais configuration incomplète : {', '.join(missing)}")
    if config.get('TESTING'):
        return
    if config['SECRET_KEY'] == DEFAULT_SECRET_KEY:
        raise RuntimeError(
            "OIDC activé avec la SECRET_KEY par défaut : les cookies de session seraient "
            "falsifiables. Définis SECRET_KEY (ex. `openssl rand -hex 32`).")
    if len(config['SECRET_KEY']) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            f"SECRET_KEY trop courte (moins de {MIN_SECRET_KEY_LENGTH} caractères) : "
            "génère-la avec `openssl rand -hex 32`.")
    if not config.get('TRUSTED_HOSTS'):
        raise RuntimeError(
            "OIDC activé sans TRUSTED_HOSTS : l'en-tête Host, fourni par le client, servirait à "
            "construire l'URL de retour OIDC. Renseigne le ou les noms publics de l'application "
            "(ex. TRUSTED_HOSTS=traces.example.com).")


def normalize_public_url(value) -> str:
    """
    PUBLIC_URL (EF-S7) : origine publique de l'application, seule source des liens envoyés et du RP ID.
    `https://hôte[:port]` (ou `http://` pour localhost/127.0.0.1/::1), sans identifiants, chemin, query ni
    fragment. Retourne l'origine sans « / » final, ou lève RuntimeError.
    """
    text = (value or '').strip()
    try:
        parts = urlsplit(text)
        host, _port = parts.hostname, parts.port
    except ValueError:
        raise RuntimeError("PUBLIC_URL invalide") from None
    local = host in ('localhost', '127.0.0.1', '::1')
    if (parts.scheme not in ('https', 'http') or not host or (parts.scheme == 'http' and not local)
            or parts.username is not None or parts.password is not None or parts.path not in ('', '/')
            or parts.query or parts.fragment):
        raise RuntimeError("PUBLIC_URL invalide : attendu https://hôte[:port] (http:// seulement pour localhost), "
                           "sans identifiants, chemin ni paramètres.")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _secrets_key_state(config) -> bool:
    """SECRETS_KEY présente (config de test ou environnement) et bien formée ; RuntimeError si mal formée."""
    raw = config.get('SECRETS_KEY') or os.environ.get('SECRETS_KEY') or ''
    try:
        return bool(parse_keys(raw))
    except SecretsKeyError as exc:
        raise RuntimeError(str(exc)) from None


def _check_password_mode(config) -> None:
    """Démarrage du mode `password` refusé si une des garanties manque (EF-0.2)."""
    # Jamais allégé par TESTING ni FLASK_ENV : le mode password est un mode d'authentification réel (les tests
    # passent des valeurs explicites par `overrides`)
    if config['SECRET_KEY'] == DEFAULT_SECRET_KEY:
        raise RuntimeError("Mode password avec la SECRET_KEY par défaut : les cookies de session seraient "
                           "falsifiables. Définis SECRET_KEY (ex. `openssl rand -hex 32`).")
    if len(config['SECRET_KEY']) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(f"SECRET_KEY trop courte (moins de {MIN_SECRET_KEY_LENGTH} caractères) : "
                           "génère-la avec `openssl rand -hex 32`.")
    if not config.get('TRUSTED_HOSTS'):
        raise RuntimeError("Mode password sans TRUSTED_HOSTS : renseigne le ou les noms publics de l'application "
                           "(ex. TRUSTED_HOSTS=traces.example.com).")
    if not _secrets_key_state(config):
        raise RuntimeError("Mode password sans SECRETS_KEY : les secrets TOTP ne pourraient pas être chiffrés. "
                           "Génère une clé : python -c \"from cryptography.fernet import Fernet; "
                           "print(Fernet.generate_key().decode())\"")
    if not config.get('PUBLIC_URL'):
        raise RuntimeError("Mode password sans PUBLIC_URL : les liens d'invitation et l'identifiant WebAuthn en dépendent "
                           "(ex. PUBLIC_URL=https://traces.example.com).")
    host = urlsplit(config['PUBLIC_URL']).hostname
    if config.get('TRUSTED_HOSTS') and not host_is_trusted(host, list(config['TRUSTED_HOSTS'])):
        raise RuntimeError("PUBLIC_URL n'est pas couverte par TRUSTED_HOSTS : l'application refuserait ses propres liens.")


def check_startup_config(app) -> None:
    """
    Valide le mode d'authentification et fixe les réglages qui en dépendent. À appeler avant
    tout le reste : rien ne doit être câblé sur une configuration refusée.
    """
    config = app.config
    mode = _auth_mode(config)
    config['AUTH_MODE'] = mode

    # Liste blanche des destinations sortantes : une entrée invalide arrête le démarrage (CA-42, CA-52)
    try:
        config['OUTBOUND'] = Allowlist.parse(config.get('OUTBOUND_ALLOWED_HOSTS') or os.environ.get('OUTBOUND_ALLOWED_HOSTS'))
    except OutboundError as exc:
        raise RuntimeError(str(exc)) from None

    # PUBLIC_URL : validée dès qu'elle est renseignée, quel que soit le mode, et normalisée
    if config.get('PUBLIC_URL'):
        config['PUBLIC_URL'] = normalize_public_url(config['PUBLIC_URL'])
    else:
        config['PUBLIC_URL'] = ''
    # Comptes à mot de passe : toujours en mode password, avec SECRETS_KEY et PUBLIC_URL en mode oidc (EF-0.4)
    config['PASSWORD_ACCOUNTS'] = False

    secure = config.get('SESSION_COOKIE_SECURE')
    if mode == 'local':
        config['SESSION_COOKIE_SECURE'] = bool(secure)
        logger.warning("Mode mono-utilisateur local SANS authentification : chaque visiteur est administrateur")
        return

    if mode == 'oidc':
        _check_oidc(config)
        config['PASSWORD_ACCOUNTS'] = bool(config['PUBLIC_URL']) and _secrets_key_state(config)
        if not config['PASSWORD_ACCOUNTS']:
            logger.info("Comptes à mot de passe désactivés : SECRETS_KEY et PUBLIC_URL sont requises (mode oidc)")
    else:
        _check_password_mode(config)
        config['PASSWORD_ACCOUNTS'] = True
    # Sûr par défaut : le cookie de session ne part qu'en HTTPS, sauf dérogation explicite
    config['SESSION_COOKIE_SECURE'] = True if secure is None else bool(secure)
    if not config['SESSION_COOKIE_SECURE'] and mode == 'password' and config['PUBLIC_URL'].startswith('https://'):
        raise RuntimeError("SESSION_COOKIE_SECURE=0 est refusé : PUBLIC_URL est en https, le cookie de session doit être Secure. "
                           "Retire SESSION_COOKIE_SECURE (Secure par défaut) ou utilise http://localhost en développement.")
    if not config['SESSION_COOKIE_SECURE']:
        logger.warning("Authentification activée avec SESSION_COOKIE_SECURE=0 : le cookie de session peut partir "
                       "en clair. À réserver au développement local.")
