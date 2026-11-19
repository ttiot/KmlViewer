"""
Contrôles de configuration au démarrage : une configuration dangereuse empêche l'application
de démarrer, plutôt que de la laisser tourner ouverte.
"""

import logging

from app.config import DEFAULT_SECRET_KEY

logger = logging.getLogger(__name__)

AUTH_MODES = ('oidc', 'local')
MIN_SECRET_KEY_LENGTH = 32
# Réglages qui n'ont de sens que derrière un reverse proxy ou avec l'OIDC : les trouver sans
# OIDC_ISSUER trahit une mise en production dont l'authentification a sauté.
_PRODUCTION_HINTS = ('TRUSTED_HOSTS', 'TRUST_PROXY', 'SESSION_COOKIE_SECURE', 'OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET')


def _auth_mode(config) -> str:
    """Mode retenu ('oidc' ou 'local'). Lève RuntimeError si la configuration est incohérente."""
    wanted = config.get('AUTH_MODE') or ''
    issuer = config.get('OIDC_ISSUER')
    if wanted not in ('', *AUTH_MODES):
        raise RuntimeError(f"AUTH_MODE invalide : « {wanted} » (attendu : oidc ou local)")
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


def check_startup_config(app) -> None:
    """
    Valide le mode d'authentification et fixe les réglages qui en dépendent. À appeler avant
    tout le reste : rien ne doit être câblé sur une configuration refusée.
    """
    config = app.config
    mode = _auth_mode(config)
    config['AUTH_MODE'] = mode

    secure = config.get('SESSION_COOKIE_SECURE')
    if mode == 'local':
        config['SESSION_COOKIE_SECURE'] = bool(secure)
        logger.warning("Mode mono-utilisateur local SANS authentification : chaque visiteur est administrateur")
        return

    _check_oidc(config)
    # Sûr par défaut : le cookie de session ne part qu'en HTTPS, sauf dérogation explicite
    config['SESSION_COOKIE_SECURE'] = True if secure is None else bool(secure)
    if not config['SESSION_COOKIE_SECURE']:
        logger.warning("OIDC activé avec SESSION_COOKIE_SECURE=0 : le cookie de session peut partir "
                       "en clair. À réserver au développement local.")
