"""
Étape 2 de la connexion par mot de passe : vérification du second facteur (`/auth/password/mfa`) et enrôlement du premier facteur
(`/auth/password/enroll`) (admin-space EF-1.6, EF-0.3, EF-2.3, EF-2.4, P-T11-1…5 ; T11).

Tout part d'un état `mfa_pending` SERVEUR valide (compte actif, jeton de session et mot de passe inchangés) désigné par
le cookie : sans lui, redirection vers la connexion. Chaque échec compte pour l'état (5 essais, détruit au 5e) ET pour le
limiteur par compte.
Réponse d'échec UNIQUE (« code invalide ou déjà utilisé »). L'enrôlement garde le secret TOTP en mémoire jusqu'à la confirmation
par un code valide ; alors, UNE transaction : secret chiffré, activation, pas accepté, codes de secours (premier facteur),
octroi d'admin porté par l'état (B5, même chemin `users.set_admin`) ; puis l'état est détruit et le client part vers la
connexion (aucune session ouverte ici, r8).
"""

import logging
import time

from flask import current_app, jsonify, redirect, render_template, request, session, url_for

from app import audit
from app.auth import bodies, bp, csrf, factors, mfa_limit, modes, second_factor, users, webauthn_factor
from app.auth.notify import security_notice
from app.auth.password_login import PENDING_COOKIE_KEY, _full_session, current_pending, pending_store
from app.db import get_db, write_transaction

logger = logging.getLogger(__name__)

INVALID = {'success': False, 'error': 'Code invalide ou déjà utilisé.'}
RESTART = {'success': False, 'error': 'Connexion expirée : reconnecte-toi.', 'restart': True}
LIMITED = {'success': False, 'error': 'Trop de tentatives, réessaie plus tard.'}


def _guard():
    if not modes.password_accounts_available():
        return jsonify({'success': False, 'error': 'Introuvable'}), 404
    return None


def _code_request():
    """(réponse d'erreur, code) d'un POST : mode, corps borné, CSRF."""
    refused = _guard()
    if refused:
        return refused, None
    data = bodies.small_json()
    if data is None or not csrf.verify(request.headers.get('X-CSRF-Token')):
        return (jsonify({'success': False, 'error': 'Requête invalide ou formulaire expiré : recharge la page.'}), 400), None
    code = data.get('code')
    return None, code if isinstance(code, str) and 0 < len(code) <= 64 else ''


def _account_limited(user_id: int):
    """Limiteur par compte : essai RÉSERVÉ avant toute vérification (atomique, MFA-1) ; 429 générique et identique pour tous."""
    retry = mfa_limit.reserve(user_id)
    if retry is None:
        return None
    response = jsonify(LIMITED)
    response.status_code = 429
    response.headers['Retry-After'] = str(retry)
    return response


def _failure(pid):
    """Un échec : l'essai du compte est déjà réservé ; compté sur l'état ; au dernier essai l'état est détruit."""
    remaining = pending_store().hit(pid)
    if remaining is None or remaining == 0:
        session.pop(PENDING_COOKIE_KEY, None)
        return jsonify(RESTART), 401
    return jsonify(INVALID), 401


def _open_session(db, pid, *, recovery: bool = False):
    """Fin de la seconde étape : l'état est consommé atomiquement, sa liaison revérifiée, puis la session COMPLÈTE s'ouvre."""
    done = pending_store().consume(pid, step='mfa')            # atomique : un cookie rejoué ne passe pas deux fois
    if done is None:
        return jsonify(RESTART), 401
    user = db.execute("SELECT * FROM users WHERE id = ? AND is_active = 1", (done.user_id,)).fetchone()
    bound = tuple(done.binding) == (user['session_token'], user['password_changed_at']) if user is not None else False
    if user is None or user['auth_method'] != 'password' or not bound:
        return jsonify(RESTART), 401                  # liaison revérifiée au moment d'ouvrir la session (T11-n2)
    if recovery:
        audit.record(db, 'user.recovery_used', actor=audit.Actor.from_user(user), target_type='user', target_id=user['id'],
                     ip=request.remote_addr, details={'remaining': second_factor.remaining_recovery(db, user['id'])})
        db.commit()
        security_notice(user, 'recovery_used')
    return _full_session(user, done.next_url)


# -- Vérification à la connexion ----------------------------------------------------------------------------------------------

@bp.route('/auth/password/mfa', methods=['GET', 'POST'])
def password_mfa():
    refused = _guard()
    if refused:
        return refused
    state = current_pending('mfa')
    if state is None:
        return redirect(url_for('auth.password_login'))
    if request.method == 'GET':
        has_keys = webauthn_factor.available(current_app.config) and bool(webauthn_factor.keys_of(get_db(), state.user_id))
        return render_template('password_mfa.html', csrf_token=csrf.issue(), has_keys=has_keys)
    refused, code = _code_request()
    if refused:
        return refused
    pid = session.get(PENDING_COOKIE_KEY)
    limited = _account_limited(state.user_id)
    if limited:
        return limited
    db = get_db()
    kind = second_factor.kind_of(code)
    ok = False
    if kind == 'totp':
        ok = second_factor.verify_totp(db, current_app.extensions['secrets'], state.user_id, code)
    elif kind == 'recovery':
        ok = second_factor.consume_recovery(db, state.user_id, code)
    if not ok:
        return _failure(pid)
    mfa_limit.release(state.user_id)
    return _open_session(db, pid, recovery=(kind == 'recovery'))


# -- Enrôlement ---------------------------------------------------------------------------------------------------------------

def _enrollment_secret(state) -> str:
    """Secret en cours d'enrôlement : créé à la première visite, gardé EN MÉMOIRE dans l'état (P-T11-1), jamais en base."""
    secret = state.extra.get('totp_secret')
    if secret is None:
        secret = state.extra['totp_secret'] = second_factor.new_secret()
    return secret


@bp.route('/auth/password/enroll', methods=['GET', 'POST'])
def password_enroll():
    refused = _guard()
    if refused:
        return refused
    state = current_pending('enroll')
    if state is None:
        return redirect(url_for('auth.password_login'))
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (state.user_id,)).fetchone()
    if request.method == 'GET':
        secret = _enrollment_secret(state)
        uri = second_factor.provisioning_uri(secret, user['username'])
        return render_template('password_enroll.html', csrf_token=csrf.issue(), qr_svg=second_factor.qr_svg(uri),
                               secret=second_factor.grouped(secret), grant=state.admin_grant,
                               webauthn=webauthn_factor.available(current_app.config))
    refused, code = _code_request()
    if refused:
        return refused
    pid = session.get(PENDING_COOKIE_KEY)
    limited = _account_limited(state.user_id)
    if limited:
        return limited
    secret = state.extra.get('totp_secret')
    normalized = second_factor.normalize_totp(code)
    step = second_factor.matching_step(secret, normalized) if secret and normalized else None
    if step is None:
        return _failure(pid)
    mfa_limit.release(state.user_id)
    store = current_app.extensions['secrets']

    def write(db, user):
        # Écriture CONDITIONNELLE à la liaison de l'état (session, mot de passe, compte actif) et à l'absence de TOTP (T11-n2/n3)
        written = db.execute(
            "UPDATE users SET totp_secret = ?, totp_enabled_at = ?, totp_last_step = ? WHERE id = ? AND is_active = 1 "
            "AND auth_method = 'password' AND session_token IS ? AND password_changed_at IS ? AND totp_enabled_at IS NULL",
            (store.encrypt_value(secret.encode('ascii')), time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), step,
             user['id'], *state.binding)).rowcount
        return bool(written)

    return _finish_enrollment(db, state, user, pid, 'totp', write)


def _finish_enrollment(db, state, user, pid, factor_type: str, write_factor):
    """
    Fin d'un enrôlement (TOTP ou clé de sécurité) : l'état est consommé atomiquement, puis UNE transaction écrit le facteur
    (`write_factor(db, user)` -> écrit-il ?), les codes de secours (premier facteur) et l'octroi d'admin porté par l'état (B5).
    """
    if pending_store().consume(pid, step='enroll') is None:     # atomique : une seule confirmation aboutit
        return jsonify(RESTART), 401
    actor = audit.Actor.from_user(user)
    with write_transaction(db):
        first = not factors.has_second_factor(db, user['id'])
        if not write_factor(db, user):
            db.rollback()
            return jsonify(RESTART), 401
        codes = second_factor.replace_recovery_codes(db, user['id']) if first else []
        audit.record(db, 'user.factor_added', actor=actor, target_type='user', target_id=user['id'], ip=request.remote_addr,
                     details={'type': factor_type})
        if state.admin_grant:
            users.set_admin(db, user['id'], True, actor=actor, ip=request.remote_addr)   # B5 : après le facteur
    session.pop(PENDING_COOKIE_KEY, None)
    security_notice(user, 'factor_added')
    return jsonify({'success': True, 'recovery_codes': codes, 'next': url_for('auth.password_login')})


# -- Clés de sécurité WebAuthn (T12) -------------------------------------------------------------------------------------------

def _webauthn_request():
    """(réponse d'erreur, corps) d'un POST WebAuthn : mode, corps borné, CSRF."""
    refused = _guard()
    if refused:
        return refused, None
    data = bodies.small_json(limit=bodies.MAX_BODY * 4)
    if data is None or not csrf.verify(request.headers.get('X-CSRF-Token')):
        return (jsonify({'success': False, 'error': 'Requête invalide ou formulaire expiré : recharge la page.'}), 400), None
    return None, data


def _webauthn_unavailable():
    return jsonify({'success': False, 'error': "Les clés de sécurité ne sont pas disponibles."}), 400


@bp.route('/auth/password/mfa/webauthn/options', methods=['POST'])
def password_mfa_webauthn_options():
    refused, _data = _webauthn_request()
    if refused:
        return refused
    state = current_pending('mfa')
    if state is None:
        return jsonify(RESTART), 401
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (state.user_id,)).fetchone()
    try:
        options = webauthn_factor.authentication_options(current_app.config, pending_store(),
                                                          f"login:{session.get(PENDING_COOKIE_KEY)}",
                                                          webauthn_factor.keys_of(db, user['id']))
    except webauthn_factor.WebAuthnError:
        return _webauthn_unavailable()
    return current_app.response_class(options, mimetype='application/json')


@bp.route('/auth/password/mfa/webauthn/verify', methods=['POST'])
def password_mfa_webauthn_verify():
    refused, data = _webauthn_request()
    if refused:
        return refused
    state = current_pending('mfa')
    if state is None:
        return jsonify(RESTART), 401
    pid = session.get(PENDING_COOKIE_KEY)
    limited = _account_limited(state.user_id)
    if limited:
        return limited
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (state.user_id,)).fetchone()
    try:
        webauthn_factor.verify_authentication(db, current_app.config, pending_store(), f"login:{pid}", user,
                                              data.get('credential'))
    except webauthn_factor.WebAuthnError:
        return _failure(pid)
    mfa_limit.release(state.user_id)
    return _open_session(db, pid)


@bp.route('/auth/password/enroll/webauthn/options', methods=['POST'])
def password_enroll_webauthn_options():
    refused, _data = _webauthn_request()
    if refused:
        return refused
    state = current_pending('enroll')
    if state is None:
        return jsonify(RESTART), 401
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (state.user_id,)).fetchone()
    try:
        options = webauthn_factor.registration_options(current_app.config, pending_store(),
                                                        f"enroll:{session.get(PENDING_COOKIE_KEY)}", user,
                                                        webauthn_factor.keys_of(db, user['id']))
    except webauthn_factor.WebAuthnError:
        return _webauthn_unavailable()
    return current_app.response_class(options, mimetype='application/json')


@bp.route('/auth/password/enroll/webauthn/verify', methods=['POST'])
def password_enroll_webauthn_verify():
    refused, data = _webauthn_request()
    if refused:
        return refused
    state = current_pending('enroll')
    if state is None:
        return jsonify(RESTART), 401
    pid = session.get(PENDING_COOKIE_KEY)
    limited = _account_limited(state.user_id)
    if limited:
        return limited
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE id = ?", (state.user_id,)).fetchone()
    try:
        credential_id, public_key, sign_count, transports = webauthn_factor.verify_registration(
            current_app.config, pending_store(), f"enroll:{pid}", data.get('credential'))
        name = webauthn_factor.clean_name(data.get('name'), 'Clé de sécurité')
    except webauthn_factor.WebAuthnError:
        return _failure(pid)
    mfa_limit.release(state.user_id)

    def write(conn, account):
        bound = conn.execute(
            "SELECT 1 FROM users WHERE id = ? AND is_active = 1 AND auth_method = 'password' AND session_token IS ? "
            "AND password_changed_at IS ?", (account['id'], *state.binding)).fetchone()
        if not bound:
            return False
        try:
            webauthn_factor.add_credential(conn, account['id'], credential_id, public_key, sign_count, transports, name)
        except webauthn_factor.WebAuthnError:
            return False
        return True

    return _finish_enrollment(db, state, user, pid, 'webauthn', write)
