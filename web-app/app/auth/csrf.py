"""
Jeton CSRF des formulaires PUBLICS (EF-S2) : sans session, donc sans état. Un jeton signé (SECRET_KEY, sel dédié) et daté
est placé dans la page et renvoyé en en-tête `X-CSRF-Token` ; un site tiers ne peut pas le lire (même origine) donc ne peut pas
forger la requête. S'ajoute au contrôle d'origine global (security._check_same_origin).
"""

import secrets

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer

MAX_AGE_S = 3600


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config['SECRET_KEY'], salt='public-form-csrf')


def issue() -> str:
    return _serializer().dumps(secrets.token_hex(16))


def verify(token) -> bool:
    if not isinstance(token, str) or not token or len(token) > 500:
        return False
    try:
        _serializer().loads(token, max_age=MAX_AGE_S)
        return True
    except BadSignature:
        return False
