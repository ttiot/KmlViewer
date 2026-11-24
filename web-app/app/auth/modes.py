"""
Modes d'authentification (admin-space §5.0, EF-0.0, ADR-0001) : SEULE source du mode pour le garde, les
routes d'authentification et les gabarits. Le mode est celui validé au démarrage par `checks.py` : la
présence d'`OIDC_ISSUER` ne le décide jamais (B1 : le mode `password` n'a pas d'OIDC_ISSUER, un garde qui le
déduirait de là donnerait l'admin à tout visiteur).
"""

from flask import current_app, has_app_context

MODES = ('oidc', 'local', 'password')

# Méthodes d'authentification (colonne users.auth_method) permises par mode (EF-0.6, M4)
PERMITTED_METHODS = {
    'local': frozenset({'none'}),
    'oidc': frozenset({'oidc', 'password'}),
    'password': frozenset({'password'}),
}


def auth_mode(config=None) -> str:
    """Mode validé au démarrage. Lève RuntimeError s'il n'a pas été validé : jamais de repli silencieux."""
    mode = (config if config is not None else current_app.config).get('AUTH_MODE')
    if mode not in MODES:
        raise RuntimeError("Mode d'authentification non validé (checks.check_startup_config n'a pas tourné)")
    return mode


def method_permitted(mode: str, auth_method, config=None) -> bool:
    """
    Vrai si un compte de cette méthode peut avoir une session dans ce mode. En mode `oidc`, les comptes `password` n'en ont une
    que si les comptes à mot de passe sont disponibles (EF-0.4, Doc-6) : désactivés, leurs sessions existantes sont refusées.
    """
    if mode == 'oidc' and auth_method == 'password' and (config is not None or has_app_context()):
        return password_accounts_available(config)
    return auth_method in PERMITTED_METHODS.get(mode, frozenset())


def password_accounts_available(config=None) -> bool:
    """
    Comptes à mot de passe actifs (routes, invitations, saisie de secrets) : toujours en mode `password` ;
    en mode `oidc` seulement avec `SECRETS_KEY` ET `PUBLIC_URL` (EF-0.4) ; jamais en mode `local`.
    Calculé au démarrage par checks.py.
    """
    return bool((config if config is not None else current_app.config).get('PASSWORD_ACCOUNTS'))
