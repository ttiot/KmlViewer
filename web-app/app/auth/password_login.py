"""
Connexion par mot de passe en deux étapes et mot de passe oublié (admin-space EF-1.6, EF-1.9, EF-S3, EF-S9 ; M1, M2, M3, r9).

Étape 1, `POST /auth/password/login` :
  - le LIMITEUR est consulté AVANT tout hachage (r9) : une requête refusée n'acquiert pas le sémaphore de hachage ;
    clés = identifiant SAISI normalisé (qu'il existe ou non, M1) et IP (/64 pour IPv6) ; 429 générique identique pour tous ;
  - échec : un message UNIQUE et générique (compte inconnu, désactivé, SSO, mauvais mot de passe), hash factice si le compte
    est absent : même code, même corps, durée homogène ;
  - succès : `session.clear()` (pas de fixation), puis soit la session complète (compte sans second facteur que la politique
    n'impose pas), soit un état `mfa_pending` côté SERVEUR (étape `mfa` ou `enroll`) dont le cookie ne porte que l'identifiant.
Étape 2 (`/auth/password/mfa*`, `/enroll/*`) : avec un état `mfa_pending` valide seulement ; les facteurs arrivent avec T11, T12.
Mot de passe oublié, `POST /auth/password/forgot` : TOUJOURS la même réponse, rendue sans attendre ; tout le travail (recherche,
jeton, message) part dans la file d'envoi, jamais dans le thread de la requête (CA-15).
"""

import logging
import time
from datetime import timedelta

from flask import current_app, jsonify, redirect, render_template, request, session, url_for

from app import audit
from app.auth import bp, csrf, emails, factors, modes, passwords, tokens, users
from app.auth.guard import safe_next
from app.db import get_db

from app.outbound import RefusalLog
from app.ratelimit import ip_key, normalize_key
from app.settings import effective

logger = logging.getLogger(__name__)

PENDING_COOKIE_KEY = 'pending_id'
GENERIC_FAILURE = {'success': False, 'error': 'Identifiant ou mot de passe incorrect.'}
LIMITED = {'success': False, 'error': 'Trop de tentatives, réessaie plus tard.'}
FORGOT_RESPONSE = {'success': True, 'message': "Si un compte correspond, un e-mail vient de lui être envoyé."}
RESET_TTL_HOURS = 1
MAX_FIELD = 1024


def _guard():
    if not modes.password_accounts_available():
        return jsonify({'success': False, 'error': 'Introuvable'}), 404
    return None


def _text(data, name):
    value = data.get(name) if isinstance(data, dict) else None
    return value if isinstance(value, str) and 0 < len(value) <= MAX_FIELD else None


def _json():
    if (request.content_length or 0) > 8192:
        return None
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _csrf_ok() -> bool:
    return csrf.verify(request.headers.get('X-CSRF-Token'))


def pending_store():
    return current_app.extensions['pending']


def _binding(user) -> tuple:
    return (user['session_token'], user['password_changed_at'])


def current_pending(step=None):
    """
    L'état `mfa_pending` désigné par le cookie, ou None. Le cookie ne contient que son identifiant. L'état n'est valide que si le
    compte est TOUJOURS actif, en mode password, et que son jeton de session et la date de son mot de passe n'ont pas bougé depuis
    la création (réinitialisation, désactivation, remise à zéro des facteurs : T10-1). Sinon il est détruit.
    """
    store, pid = pending_store(), session.get(PENDING_COOKIE_KEY)
    state = store.get(pid)
    if state is None:
        return None
    user = get_db().execute("SELECT * FROM users WHERE id = ?", (state.user_id,)).fetchone()
    if user is None or not user['is_active'] or user['auth_method'] != 'password' or _binding(user) != state.binding:
        store.destroy(pid)
        return None
    return state if step is None or state.step == step else None


def _hit(ip_limiter, identifier_limiter, ip: str, identifier: str) -> tuple:
    """IP d'abord, puis identifiant (un refus par IP ne consomme pas l'identifiant). Retourne (autorisé, attente, par IP)."""
    allowed, retry = ip_limiter.hit(ip_key(ip))
    if not allowed:
        return False, retry, True
    allowed, retry = identifier_limiter.hit(identifier)
    return allowed, retry, False


def _log_throttle(identifier: str, ip: str, scope: str, by_ip: bool = False) -> None:
    """
    EF-9.2 : une ligne d'audit par couple (identifiant normalisé, IP) quand un seuil est atteint ; pas une par échec. Si c'est la
    limite d'IP qui refuse, UNE ligne par IP (T10-2) : 200 identifiants inventés depuis une IP n'écrivent pas 200 lignes.
    """
    key = ip_key(ip) if by_ip else f"{identifier[:64]}|{ip_key(ip)}"
    scope = f"{scope}:ip" if by_ip else scope
    throttle = current_app.extensions.setdefault('login_throttle_audit', RefusalLog(window_s=900, max_entries=2048))
    if throttle.due(scope, key):
        audit.record(get_db(), 'auth.rate_limited', actor=audit.Actor.system(), target_type='login', ip=ip,
                     details={'scope': scope, 'identifier': identifier[:64]})
        throttle.mark(scope, key)


def _full_session(user, next_url: str):
    """Session complète : `session.clear()` d'abord, puis le strict nécessaire (jamais une session héritée)."""
    db = get_db()
    session.clear()
    session['user_id'] = user['id']
    session['token'] = users.session_token(db, user['id'])
    session['login_at'] = int(time.time())
    session.permanent = True
    db.execute("UPDATE users SET last_login = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?", (user['id'],))
    db.commit()
    return jsonify({'success': True, 'next': safe_next(next_url)})


def _requires_factor(user, has_factor: bool) -> bool:
    """Un second facteur est exigé : pour un admin (B5), quand la politique est `all`, ou dès que le compte en a un."""
    return has_factor or bool(user['is_admin']) or effective(current_app.config, 'auth.mfa_policy') == 'all'


# -- Étape 1 : mot de passe ---------------------------------------------------------------------------------------------

@bp.route('/auth/password/login', methods=['GET', 'POST'])
def password_login():
    if request.method == 'GET':
        if not modes.password_accounts_available():
            return render_template('auth_message.html', title='Introuvable', message='Cette page n’existe pas.'), 404
        return render_template('password_login.html', csrf_token=csrf.issue(),
                               forgot_enabled=_forgot_available(), next_url=safe_next(request.args.get('next', '')))
    refused = _guard()
    if refused:
        return refused
    data = _json()
    if data is None or not _csrf_ok():
        return jsonify({'success': False, 'error': 'Requête invalide ou formulaire expiré : recharge la page.'}), 400
    username, password = _text(data, 'username'), _text(data, 'password')
    ip = request.remote_addr
    if username is None or password is None:
        return jsonify(GENERIC_FAILURE), 401

    # r9 : le limiteur AVANT tout hachage ; une requête refusée ne touche pas au sémaphore (CA-46)
    limiters = current_app.extensions['limiters']
    identifier = normalize_key(username)
    allowed, retry, by_ip = _hit(limiters.password_ip, limiters.password_identifier, ip, identifier)
    if not allowed:
        _log_throttle(identifier, ip, 'password', by_ip)
        response = jsonify(LIMITED)
        response.status_code = 429
        response.headers['Retry-After'] = str(retry)
        return response

    db = get_db()
    row = db.execute("SELECT * FROM users WHERE lower(username) = ? AND auth_method = 'password'", (identifier,)).fetchone()
    stored = row['password_hash'] if row is not None and row['is_active'] else None   # inconnu, désactivé, SSO : hash factice
    try:
        ok, new_hash = passwords.verify_password(stored, password)
    except passwords.HashBusy:
        response = jsonify(LIMITED)                                                       # 429 générique, identique pour tous
        response.status_code = 429
        response.headers['Retry-After'] = '2'       # fixe : ne dépend pas du compte (T10-n1)
        return response
    if not ok or row is None:
        return jsonify(GENERIC_FAILURE), 401
    if new_hash:                                                                           # rehachage (paramètres relevés)
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, row['id']))
        db.commit()

    next_url = safe_next(_text(data, 'next') or '')
    has_factor = factors.has_second_factor(db, row['id'])
    if not _requires_factor(row, has_factor):
        return _full_session(row, next_url)
    step = 'mfa' if has_factor else 'enroll'
    session.clear()
    session[PENDING_COOKIE_KEY] = pending_store().create(row['id'], step, next_url=next_url, binding=_binding(row))
    target = url_for('auth.password_mfa') if step == 'mfa' else url_for('auth.password_enroll')
    return jsonify({'success': True, 'next': target, 'step': step})


# -- Étape 2 : seconds facteurs (T11, T12) ------------------------------------------------------------------------------------

@bp.route('/auth/password/mfa', methods=['GET', 'POST'])
def password_mfa():
    refused = _guard()
    if refused:
        return refused
    state = current_pending('mfa')
    if state is None:
        return redirect(url_for('auth.password_login'))
    if request.method == 'GET':
        return render_template('auth_message.html', title='Second facteur',
                               message="La vérification du second facteur n'est pas encore disponible."), 501
    if not _csrf_ok():
        return jsonify({'success': False, 'error': 'Formulaire expiré'}), 400
    remaining = pending_store().hit(session.get(PENDING_COOKIE_KEY))     # essai compté côté SERVEUR
    if remaining is None or remaining == 0:
        session.pop(PENDING_COOKIE_KEY, None)
        return jsonify({'success': False, 'error': 'Connexion expirée : reconnecte-toi.', 'restart': True}), 401
    return jsonify({'success': False, 'error': "La vérification du second facteur n'est pas encore disponible."}), 501


@bp.route('/auth/password/enroll', methods=['GET', 'POST'])
def password_enroll():
    refused = _guard()
    if refused:
        return refused
    if current_pending('enroll') is None:
        return redirect(url_for('auth.password_login'))
    return render_template('auth_message.html', title='Second facteur requis',
                           message="L'enrôlement d'un second facteur n'est pas encore disponible."), 501


# -- Mot de passe oublié -------------------------------------------------------------------------------------------------------

def _forgot_available() -> bool:
    """Actif dès que l'e-mail est configuré ; désactivable (`auth.forgot_enabled`)."""
    return bool(effective(current_app.config, 'auth.forgot_enabled')) and current_app.extensions['mailer'].has_settings


def _forgot_job(app, identifier: str, address_form: bool):
    """Travail différé (thread de la file) : recherche, jeton de réinitialisation, message. Jamais dans la requête."""
    def run():
        from app.db import connect
        conn = connect(app.config['DB_PATH'])
        try:
            if address_form:
                wanted = emails.canonical_or_none(identifier)
                matches = [] if wanted is None else conn.execute(
                    "SELECT * FROM users WHERE email = ? AND auth_method = 'password'", (wanted,)).fetchall()
            else:
                matches = conn.execute("SELECT * FROM users WHERE lower(username) = ? AND auth_method = 'password'",
                                       (identifier,)).fetchall()
            # Éligible : compte password ACTIF, adresse VALIDÉE, désigné sans ambiguïté (EF-1.9, EF-4.4)
            if len(matches) != 1 or not matches[0]['is_active'] or not matches[0]['email']:
                return
            user = matches[0]
            created = tokens.create(conn, 'reset', actor=audit.Actor.system(), target_user_id=user['id'], email=user['email'],
                                    ttl=timedelta(hours=RESET_TTL_HOURS))
            from app.mailer import compose  # import tardif : mailer dépend de app.auth
            subject, body = compose('reset', link=tokens.link(app.config['PUBLIC_URL'], created.token))
            app.extensions['mailer'].send(user['email'], subject, body, conn=conn)
        except Exception:  # noqa: BLE001 - jamais de détail (lien) dans un journal
            logger.warning("Mot de passe oublié : traitement différé en échec")
        finally:
            conn.close()
    return run


@bp.route('/auth/password/forgot', methods=['GET', 'POST'])
def password_forgot():
    if not modes.password_accounts_available() or not _forgot_available():
        if request.method == 'GET':
            return render_template('auth_message.html', title='Introuvable', message='Cette page n’existe pas.'), 404
        return jsonify({'success': False, 'error': 'Introuvable'}), 404
    if request.method == 'GET':
        return render_template('password_forgot.html', csrf_token=csrf.issue())
    data = _json()
    if data is None or not _csrf_ok():
        return jsonify({'success': False, 'error': 'Requête invalide ou formulaire expiré : recharge la page.'}), 400
    raw = _text(data, 'identifier')
    if raw is None:
        return jsonify(FORGOT_RESPONSE)                       # même réponse : rien ne distingue une saisie vide d'une autre
    ip = request.remote_addr
    limiters = current_app.extensions['limiters']
    identifier = normalize_key(raw)
    allowed, retry, by_ip = _hit(limiters.forgot_ip, limiters.forgot_identifier, ip, identifier)
    if not allowed:
        _log_throttle(identifier, ip, 'forgot', by_ip)
        response = jsonify(LIMITED)
        response.status_code = 429
        response.headers['Retry-After'] = str(retry)
        return response
    # Dans TOUS les cas : un seul geste dans la requête (mise en file) ; la différence éventuelle (compte éligible ou non)
    # n'existe que dans le thread de la file, donc aucun oracle temporel (CA-15)
    current_app.extensions['mailer'].submit(_forgot_job(current_app._get_current_object(), identifier, '@' in identifier))
    return jsonify(FORGOT_RESPONSE)

