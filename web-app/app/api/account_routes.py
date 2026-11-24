"""
Compte personnel (`/api/me/password`, `/api/me/email` : admin-space EF-1.7, EF-4.6, EF-4.10 ; T13) : compte `password`, session
complète. Chaque changement exige le MOT DE PASSE ACTUEL (limiteur puis sémaphore) ET un second facteur s'il en existe un.
"""

from flask import current_app, g, jsonify, request, session

from app import audit
from app.api import bp
from app.api.mfa_routes import _check_password, _password_user
from app.api.payload import json_body
from app.auth import emails, factors, passwords, second_factor, tokens, users
from app.auth.notify import security_notice
from app.db import get_db, write_transaction
from app.errors import ApiError


def _check_second_factor(user, data) -> None:
    """Code TOTP ou de secours, exigé seulement si le compte a un second facteur actif ; limité par compte, usage unique."""
    db = get_db()
    if not factors.has_second_factor(db, user['id']):
        return
    allowed, _retry = current_app.extensions['limiters'].mfa_account.hit(int(user['id']))
    if not allowed:
        raise ApiError('Trop de tentatives, réessaie plus tard.', 429)
    code = data.get('code')
    kind = second_factor.kind_of(code)
    ok = (second_factor.verify_totp(db, current_app.extensions['secrets'], user['id'], code) if kind == 'totp'
          else second_factor.consume_recovery(db, user['id'], code) if kind == 'recovery' else False)
    if not ok:
        raise ApiError('Code invalide ou déjà utilisé.', 403)


@bp.route('/me/password', methods=['POST'])
def change_password():
    """EF-1.7 : autres sessions révoquées, courante réémise, notification, états intermédiaires détruits."""
    user = _password_user()
    data = json_body()
    _check_password(user, {'password': data.get('current_password')})
    new = data.get('new_password')
    if not isinstance(new, str):
        raise ApiError('Nouveau mot de passe requis.')
    try:                                   # politique AVANT le second facteur : un refus ne brûle pas un code (T13-n1)
        passwords.check_policy(new, identities=[user['username'], user['email'], user['pending_email']])
    except passwords.PolicyError as exc:
        raise ApiError(str(exc)) from None
    _check_second_factor(user, data)
    try:
        hashed = passwords.hash_password(new)
    except passwords.PolicyError as exc:
        raise ApiError(str(exc)) from None
    except passwords.HashBusy:
        raise ApiError('Service occupé, réessaie dans un instant.', 429) from None
    db = get_db()
    with write_transaction(db):
        token = users._new_session_token()
        db.execute("UPDATE users SET password_hash = ?, password_changed_at = ?, session_token = ? WHERE id = ?",
                   (hashed, tokens._stamp(None), token, user['id']))
        audit.record(db, 'user.password_changed', actor=audit.Actor.from_user(user), target_type='user', target_id=user['id'],
                     ip=request.remote_addr)
    session['token'] = token                                              # la session courante survit, les autres tombent
    current_app.extensions['pending'].destroy_for_user(user['id'])
    security_notice(user, 'password_changed')
    return jsonify({'success': True})


@bp.route('/me/email', methods=['POST'])
def change_email():
    """
    EF-4.6 : nouvelle adresse EN ATTENTE ; l'ancienne reste active et reçoit une notification ; lien de validation envoyé à la
    nouvelle. Réponse identique que l'adresse soit libre ou prise : l'unicité n'est imposée qu'à la validation (message neutre).
    """
    user = _password_user()
    data = json_body()
    _check_password(user, {'password': data.get('password')})
    _check_second_factor(user, data)
    try:
        wanted = emails.validate_email(data.get('email'))
    except ValueError:
        raise ApiError('Adresse e-mail invalide.') from None
    db = get_db()
    if wanted == user['email']:
        raise ApiError('C’est déjà ton adresse.', 409)
    with write_transaction(db):
        db.execute("UPDATE users SET pending_email = ? WHERE id = ?", (wanted, user['id']))
        audit.record(db, 'user.email_change_requested', actor=audit.Actor.from_user(user), target_type='user',
                     target_id=user['id'], ip=request.remote_addr)
    if user['email']:
        security_notice(user, 'email_changed')                                    # à l'ANCIENNE adresse, toujours active
    from app.auth.token_routes import _queue_verification
    _queue_verification(db, g.user)
    return jsonify({'success': True, 'pending_email': wanted})
