"""
Gestion du second facteur depuis le profil (`/api/me/mfa`, admin-space P-T11-7, EF-2.2, EF-2.3, EF-4.7) : session complète, compte
`password`. Toute modification exige le MOT DE PASSE ACTUEL (vérifié par le limiteur puis le sémaphore, EF-1.5) ; retirer un
facteur ou régénérer les codes exige en plus un code TOTP valide : une session volée ne suffit ni à ajouter ni à retirer un
facteur. Le secret en cours d'enrôlement reste en mémoire, lié au compte ET au jeton de session (10 min), jamais en base avant
confirmation.
"""

import time

from flask import current_app, g, jsonify, request, session

from app import audit
from app.api import bp
from app.api.payload import json_body
from app.auth import factors, mfa_limit, passwords, second_factor, users, webauthn_factor
from app.auth.notify import security_notice
from app.db import get_db, write_transaction
from app.errors import ApiError
from app.ratelimit import ip_key, normalize_key


def _password_user():
    user = g.user
    if user['auth_method'] != 'password':
        raise ApiError('Les facteurs se gèrent dans ton fournisseur d’identité.', 404)
    return user


def _rotate_sessions(db, user) -> str:
    """CA-14 : tout changement de facteur ferme les AUTRES sessions du compte (jeton renouvelé dans la transaction) ; la session
    courante est réémise par l'appelant (`_keep_current`)."""
    token = users._new_session_token()
    db.execute("UPDATE users SET session_token = ? WHERE id = ?", (token, user['id']))
    return token


def _owner(user) -> str:
    return f"profile:{user['id']}:{user['session_token']}"


def _check_password(user, data) -> None:
    """Mot de passe actuel : limiteur AVANT le hachage, 429 générique ; refus 403 sans détail."""
    password = data.get('password')
    if not isinstance(password, str) or not 0 < len(password) <= 1024:
        raise ApiError('Mot de passe requis.', 400)
    limiters = current_app.extensions['limiters']
    for limiter, key in ((limiters.password_ip, ip_key(request.remote_addr)),
                         (limiters.password_identifier, normalize_key(user['username']))):
        allowed, _retry = limiter.hit(key)
        if not allowed:
            raise ApiError('Trop de tentatives, réessaie plus tard.', 429)
    try:
        ok, _new = passwords.verify_password(user['password_hash'], password)
    except passwords.HashBusy:
        raise ApiError('Service occupé, réessaie dans un instant.', 429) from None
    if not ok:
        raise ApiError('Mot de passe incorrect.', 403)
    for limiter, key in ((limiters.password_ip, ip_key(request.remote_addr)),
                         (limiters.password_identifier, normalize_key(user['username']))):
        limiter.refund(key)                                      # un succès ne consomme pas le budget


def _check_totp(user, data) -> None:
    if mfa_limit.blocked(user['id']) is not None:
        raise ApiError(mfa_limit.MESSAGE, 429)
    if not second_factor.verify_totp(get_db(), current_app.extensions['secrets'], user['id'], data.get('code')):
        mfa_limit.failed(user['id'])
        raise ApiError('Code invalide ou déjà utilisé.', 403)


def _state(user) -> dict:
    db = get_db()
    row = db.execute("SELECT totp_enabled_at FROM users WHERE id = ?", (user['id'],)).fetchone()
    keys = webauthn_factor.keys_of(db, user['id'])
    return {'totp': bool(row and row['totp_enabled_at']), 'keys': len(keys),
            'key_list': [{'id': k['id'], 'name': k['name'], 'created_at': k['created_at'], 'last_used_at': k['last_used_at']}
                         for k in keys],
            'webauthn_available': webauthn_factor.available(current_app.config),
            'recovery_codes_remaining': second_factor.remaining_recovery(db, user['id'])}


@bp.route('/me/mfa')
def mfa_state():
    return jsonify({'success': True, **_state(_password_user())})


@bp.route('/me/mfa/totp/setup', methods=['POST'])
def mfa_totp_setup():
    user = _password_user()
    data = json_body()
    _check_password(user, data)
    if _state(user)['totp']:
        raise ApiError('Un code TOTP est déjà actif : retire-le d’abord.', 409)
    secret = second_factor.new_secret()
    current_app.extensions['pending'].put_enrollment(_owner(user), secret)
    uri = second_factor.provisioning_uri(secret, user['username'])
    return jsonify({'success': True, 'secret': second_factor.grouped(secret), 'uri': uri, 'qr_svg': second_factor.qr_svg(uri)})


@bp.route('/me/mfa/totp/confirm', methods=['POST'])
def mfa_totp_confirm():
    user = _password_user()
    data = json_body()
    pending = current_app.extensions['pending']
    secret = pending.get_enrollment(_owner(user))
    if mfa_limit.blocked(user['id']) is not None:
        raise ApiError(mfa_limit.MESSAGE, 429)
    code = second_factor.normalize_totp(data.get('code'))
    step = second_factor.matching_step(secret, code) if secret and code else None
    if step is None:
        mfa_limit.failed(user['id'])
        raise ApiError('Code invalide ou déjà utilisé.', 403 if secret else 409)
    db = get_db()
    store = current_app.extensions['secrets']
    with write_transaction(db):
        if _state(user)['totp']:
            raise ApiError('Un code TOTP est déjà actif.', 409)
        first = not factors.has_second_factor(db, user['id'])
        db.execute("UPDATE users SET totp_secret = ?, totp_enabled_at = ?, totp_last_step = ? WHERE id = ?",
                   (store.encrypt_value(secret.encode('ascii')), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), step,
                    user['id']))
        codes = second_factor.replace_recovery_codes(db, user['id']) if first else []
        audit.record(db, 'user.factor_added', actor=audit.Actor.from_user(user), target_type='user', target_id=user['id'],
                     ip=request.remote_addr, details={'type': 'totp'})
        token = _rotate_sessions(db, user)
    pending.drop_enrollment(_owner(user))
    session['token'] = token
    security_notice(user, 'factor_added')
    return jsonify({'success': True, 'recovery_codes': codes})


@bp.route('/me/mfa/totp', methods=['DELETE'])
def mfa_totp_remove():
    user = _password_user()
    data = json_body()
    _check_password(user, data)
    _check_totp(user, data)
    db = get_db()
    with write_transaction(db):
        state = _state(user)
        if not state['totp']:
            raise ApiError('Aucun code TOTP actif.', 409)
        if user['is_admin'] and state['keys'] == 0:
            raise ApiError('Un administrateur garde au moins un second facteur.', 409)         # EF-2.2, B5
        db.execute("UPDATE users SET totp_secret = NULL, totp_enabled_at = NULL, totp_last_step = NULL WHERE id = ?",
                   (user['id'],))
        if state['keys'] == 0:
            db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user['id'],))     # plus de facteur : codes invalidés (r6)
        audit.record(db, 'user.factor_removed', actor=audit.Actor.from_user(user), target_type='user', target_id=user['id'],
                     ip=request.remote_addr, details={'type': 'totp'})
        token = _rotate_sessions(db, user)
    session['token'] = token
    current_app.extensions['pending'].destroy_for_user(user['id'])
    security_notice(user, 'factor_removed')
    return jsonify({'success': True})


@bp.route('/me/mfa/recovery-codes', methods=['POST'])
def mfa_recovery_codes():
    user = _password_user()
    data = json_body()
    _check_password(user, data)
    _check_totp(user, data)
    db = get_db()
    with write_transaction(db):
        if not factors.has_second_factor(db, user['id']):
            raise ApiError('Aucun second facteur actif.', 409)
        codes = second_factor.replace_recovery_codes(db, user['id'])
        audit.record(db, 'user.recovery_regenerated', actor=audit.Actor.from_user(user), target_type='user',
                     target_id=user['id'], ip=request.remote_addr)
    return jsonify({'success': True, 'recovery_codes': codes})


# -- Clés de sécurité WebAuthn (T12) ---------------------------------------------------------------------------------------------

@bp.route('/me/mfa/webauthn/options', methods=['POST'])
def mfa_webauthn_options():
    """Ajoute une clé depuis le profil : mot de passe actuel exigé (une session volée ne suffit pas), défi lié à la session."""
    user = _password_user()
    data = json_body()
    _check_password(user, data)
    try:
        options = webauthn_factor.registration_options(current_app.config, current_app.extensions['pending'],
                                                        f"profile:{user['id']}:{user['session_token']}", user,
                                                        webauthn_factor.keys_of(get_db(), user['id']))
    except webauthn_factor.WebAuthnError as exc:
        raise ApiError(str(exc), 409) from None
    return current_app.response_class(options, mimetype='application/json')


@bp.route('/me/mfa/webauthn/verify', methods=['POST'])
def mfa_webauthn_verify():
    user = _password_user()
    data = json_body(large=False)
    if mfa_limit.blocked(user['id']) is not None:
        raise ApiError(mfa_limit.MESSAGE, 429)
    pending = current_app.extensions['pending']
    try:
        credential_id, public_key, sign_count, transports = webauthn_factor.verify_registration(
            current_app.config, pending, f"profile:{user['id']}:{user['session_token']}", data.get('credential'))
        name = webauthn_factor.clean_name(data.get('name'), 'Clé de sécurité')
    except webauthn_factor.WebAuthnError as exc:
        mfa_limit.failed(user['id'])
        raise ApiError(str(exc), 403) from None
    db = get_db()
    with write_transaction(db):
        first = not factors.has_second_factor(db, user['id'])
        try:
            key_id = webauthn_factor.add_credential(db, user['id'], credential_id, public_key, sign_count, transports,
                                                    name)
        except webauthn_factor.WebAuthnError as exc:
            raise ApiError(str(exc), 409) from None
        codes = second_factor.replace_recovery_codes(db, user['id']) if first else []
        audit.record(db, 'user.factor_added', actor=audit.Actor.from_user(user), target_type='user', target_id=user['id'],
                     ip=request.remote_addr, details={'type': 'webauthn'})
        token = _rotate_sessions(db, user)
    session['token'] = token
    security_notice(user, 'factor_added')
    return jsonify({'success': True, 'id': key_id, 'recovery_codes': codes})


@bp.route('/me/mfa/webauthn/<int:key_id>', methods=['DELETE'])
def mfa_webauthn_remove(key_id):
    """Retire une clé : mot de passe + code ; jamais le dernier facteur d'un admin (EF-2.2, B5)."""
    user = _password_user()
    data = json_body()
    _check_password(user, data)
    _check_totp_or_recovery(user, data)
    db = get_db()
    with write_transaction(db):
        state = _state(user)
        if not any(k['id'] == key_id for k in state['key_list']):
            raise ApiError('Clé introuvable', 404)
        if user['is_admin'] and not state['totp'] and state['keys'] <= 1:
            raise ApiError('Un administrateur garde au moins un second facteur.', 409)
        webauthn_factor.remove_credential(db, user['id'], key_id)
        if not state['totp'] and state['keys'] <= 1:
            db.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user['id'],))     # plus de facteur : codes invalidés
        audit.record(db, 'user.factor_removed', actor=audit.Actor.from_user(user), target_type='user', target_id=user['id'],
                     ip=request.remote_addr, details={'type': 'webauthn'})
        token = _rotate_sessions(db, user)
    session['token'] = token
    current_app.extensions['pending'].destroy_for_user(user['id'])
    security_notice(user, 'factor_removed')
    return jsonify({'success': True})


def _check_totp_or_recovery(user, data) -> None:
    """Second facteur exigé pour une suppression : code TOTP ou de secours (le compte peut n'avoir que des clés)."""
    if mfa_limit.blocked(user['id']) is not None:
        raise ApiError(mfa_limit.MESSAGE, 429)
    code = data.get('code')
    kind = second_factor.kind_of(code)
    db = get_db()
    ok = (second_factor.verify_totp(db, current_app.extensions['secrets'], user['id'], code) if kind == 'totp'
          else second_factor.consume_recovery(db, user['id'], code) if kind == 'recovery' else False)
    if not ok:
        mfa_limit.failed(user['id'])
        raise ApiError('Code invalide ou déjà utilisé.', 403)
