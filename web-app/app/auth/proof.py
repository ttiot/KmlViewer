"""
Preuve d'un second facteur pour une action du PROFIL (retrait d'un facteur, changement de mot de passe...). Limiteur par compte
atomique (MFA-1) partagé avec la connexion. Un code de secours consommé ici est tracé et notifié comme à la connexion (E-3a). Une
ASSERTION WebAuthn tient lieu de code pour retirer une clé et régénérer les codes de secours (E-3b), pas davantage.
"""

from flask import current_app, request

from app import audit
from app.auth import mfa_limit, second_factor, webauthn_factor
from app.auth.notify import security_notice
from app.db import get_db
from app.errors import ApiError

INVALID = 'Code invalide ou déjà utilisé.'


def _reserve(user) -> None:
    if mfa_limit.reserve(user['id']) is not None:
        raise ApiError(mfa_limit.MESSAGE, 429)


def code_or_recovery(user, data, check=None) -> None:
    """
    Code TOTP ou de secours (le compte peut n'avoir que des clés). `check` : contrôles qui peuvent refuser l'opération
    (409 dernier facteur d'un admin...) ; ils passent APRÈS la validité du code mais AVANT la consommation d'un code de
    secours, qui n'est donc ni consommé ni notifié si l'opération est refusée (E-3-n1). Un code de secours consommé est
    audité et notifié.
    """
    _reserve(user)
    code = data.get('code')
    kind = second_factor.kind_of(code)
    db = get_db()
    if kind == 'totp':
        ok = second_factor.verify_totp(db, current_app.extensions['secrets'], user['id'], code)
    elif kind == 'recovery':
        ok = second_factor.recovery_is_valid(db, user['id'], code)
    else:
        ok = False
    if not ok:
        raise ApiError(INVALID, 403)
    mfa_limit.release(user['id'])
    if check is not None:
        check()
    if kind == 'recovery':
        if not second_factor.consume_recovery(db, user['id'], code):
            raise ApiError(INVALID, 403)
        audit.record(db, 'user.recovery_used', actor=audit.Actor.from_user(user), target_type='user', target_id=user['id'],
                     ip=request.remote_addr, details={'remaining': second_factor.remaining_recovery(db, user['id']),
                                                      'where': 'profile'})
        security_notice(user, 'recovery_used')


def assertion_owner(user) -> str:
    return f"proof:{user['id']}:{user['session_token']}"


def assertion(user, data, check=None) -> None:
    """Assertion WebAuthn d'une clé du compte (défi lié à la session) : preuve pour retirer une clé / régénérer les codes."""
    _reserve(user)
    try:
        webauthn_factor.verify_authentication(get_db(), current_app.config, current_app.extensions['pending'],
                                              assertion_owner(user), user, data.get('assertion'))
    except webauthn_factor.WebAuthnError:
        raise ApiError(INVALID, 403) from None
    mfa_limit.release(user['id'])
    if check is not None:
        check()
