"""
Authentification OIDC et contrôle d'accès.

- oidc.py   : client OIDC (Authlib) et routes login / callback / logout
- guard.py  : chargement de l'utilisateur courant et contrôle d'accès global
- users.py  : persistance des comptes (provisioning à la volée depuis les claims)

Sans OIDC_ISSUER configuré, l'application tourne en mode mono-utilisateur :
un compte "local" administrateur est utilisé pour toutes les requêtes.
"""

from flask import Blueprint

bp = Blueprint('auth', __name__)

from . import oidc  # noqa: E402,F401 - enregistre les routes
from . import token_routes  # noqa: E402,F401 - jetons à usage unique
from . import password_login  # noqa: E402,F401 - connexion par mot de passe
from .guard import admin_required, current_user_id, init_guard  # noqa: E402,F401
from .oidc import init_oidc  # noqa: E402


def init_auth(app) -> None:
    init_oidc(app)
    init_guard(app)
    app.register_blueprint(bp)
