"""
API d'administration (admin-space §8, EF-8.3, P-T14-3…9) : actions sur les comptes, invitations, réglages, secrets, e-mail de
test, fonctions bêta, journal. TOUT est réservé aux admins (`admin_required`, vérifié à chaque requête : un admin `password` sans
second facteur actif n'ouvre rien). Les lectures ne renvoient jamais un secret ; les écritures sont tracées par les services
(audit acteur `user`).
"""

import logging
import os
from datetime import timedelta

from flask import current_app, g, jsonify, request

from app import audit, features, outbound
from app.api import bp
from app.api.payload import json_body
from app.auth import admin_required, factors, invitations, modes, tokens, users
from app.auth.notify import security_notice
from app.db import get_db, write_transaction
from app.errors import ApiError
from app.secrets_store import SECRET_KINDS, SecretsError
from app.settings import REGISTRY, SettingsError, effective

logger = logging.getLogger(__name__)

RESET_LINK_TTL = timedelta(hours=24)                      # EF-1.8
CONVERT_LINK_TTL = timedelta(hours=24)


def _actor():
    return audit.Actor.from_user(g.user)


def _target(user_id):
    row = users.get_by_id(get_db(), user_id)
    if row is None:
        raise ApiError('Utilisateur inconnu', 404)
    return row


def _password_account_required(target):
    if target['auth_method'] != 'password':
        raise ApiError('Seuls les comptes à mot de passe sont concernés.', 409)


def _public_link(token: str) -> str:
    url = current_app.config.get('PUBLIC_URL')
    if not url:
        raise ApiError('PUBLIC_URL absente : impossible de construire un lien.', 409)
    return tokens.link(url, token)


# -- Comptes (EF-8.3) ---------------------------------------------------------------------------------------------------------

@bp.route('/admin/users/<int:user_id>/reset-link', methods=['POST'])
@admin_required
def admin_reset_link(user_id):
    """
    Lien de réinitialisation du mot de passe (24 h, usage unique, EF-1.8) : montré UNE fois à l'admin ET, si l'e-mail est
    configuré, envoyé (file différée) à l'adresse validée du compte. `sent_by_mail` dit si l'envoi a été mis en file.
    """
    target = _target(user_id)
    _password_account_required(target)
    if not target['is_active']:
        raise ApiError('Compte désactivé.', 409)
    tokens.revoke_pending(get_db(), user_id, 'reset', actor=_actor())             # un seul lien de réinitialisation à la fois
    created = tokens.create(get_db(), 'reset', actor=_actor(), created_by=g.user['id'], target_user_id=user_id,
                            email=target['email'], ttl=RESET_LINK_TTL, ip=request.remote_addr)
    link = _public_link(created.token)
    sent = False
    mailer = current_app.extensions['mailer']
    if target['email'] and mailer.has_settings:
        from app.mailer import compose  # import tardif : mailer dépend de app.auth
        subject, body = compose('reset', link=link, username=target['username'],
                                hours=int(RESET_LINK_TTL.total_seconds() // 3600))
        sent = mailer.enqueue(target['email'], subject, body) == 'queued'
        if sent:
            tokens.mark_sent(get_db(), created.id)
    return jsonify({'success': True, 'link': link, 'expires_at': created.expires_at, 'sent_by_mail': sent})


@bp.route('/admin/users/<int:user_id>/mfa-reset', methods=['POST'])
@admin_required
def admin_mfa_reset(user_id):
    """Perte de tous les facteurs (EF-2.7) : facteurs retirés, sessions fermées, rétrogradation si admin. Jamais sur soi-même."""
    if user_id == g.user['id']:
        raise ApiError("Un autre administrateur doit réinitialiser tes facteurs (ou `flask user-mfa-reset`).", 409)
    db = get_db()
    target = _target(user_id)
    _password_account_required(target)
    demoted = bool(target['is_admin'])
    from app.api.users_routes import ensure_admin_remains
    with write_transaction(db):
        ensure_admin_remains(db, user_id, 'réinitialiser ses facteurs (il serait rétrogradé)')
        removed = factors.clear_factors(db, user_id)
        db.execute("UPDATE users SET session_token = ?, is_admin = 0 WHERE id = ?", (users._new_session_token(), user_id))
        audit.record(db, 'user.mfa_reset', actor=_actor(), target_type='user', target_id=user_id, ip=request.remote_addr,
                     details={'demoted': demoted, 'factors_removed': sum(removed.values())})
    current_app.extensions['pending'].destroy_for_user(user_id)
    security_notice(target, 'factor_reset')
    return jsonify({'success': True, 'demoted': demoted})


@bp.route('/admin/users/<int:user_id>/revoke-sessions', methods=['POST'])
@admin_required
def admin_revoke_sessions(user_id):
    """Ferme toutes les sessions du compte (sur soi : la courante comprise, reconnexion)."""
    _target(user_id)
    db = get_db()
    users.revoke_sessions(db, user_id)
    audit.record(db, 'user.sessions_revoked', actor=_actor(), target_type='user', target_id=user_id, ip=request.remote_addr)
    db.commit()
    current_app.extensions['pending'].destroy_for_user(user_id)
    return jsonify({'success': True})


@bp.route('/admin/users/<int:user_id>/resend-verification', methods=['POST'])
@admin_required
def admin_resend_verification(user_id):
    target = _target(user_id)
    _password_account_required(target)
    from app.auth.token_routes import resend_verification
    resend_verification(get_db(), target)                  # même limite (3 / h) que le renvoi de l'utilisateur
    return jsonify({'success': True})


@bp.route('/admin/users/<int:user_id>/convert', methods=['POST'])
@admin_required
def admin_convert(user_id):
    """Conversion SSO -> mot de passe (EF-0.5) : lien « définir un mot de passe » (24 h), mode `password` seulement."""
    if current_app.config['AUTH_MODE'] != 'password':
        raise ApiError("La conversion n'existe qu'en mode password.", 409)
    target = _target(user_id)
    if target['auth_method'] != 'oidc' or not target['is_active']:
        raise ApiError('Seul un compte SSO actif peut être converti.', 409)
    created = tokens.create(get_db(), 'set_password', actor=_actor(), created_by=g.user['id'], target_user_id=user_id,
                            email=target['email'], ttl=CONVERT_LINK_TTL, ip=request.remote_addr)
    return jsonify({'success': True, 'link': _public_link(created.token), 'expires_at': created.expires_at})


# -- Invitations (toutes) -----------------------------------------------------------------------------------------------------

@bp.route('/admin/invitations')
@admin_required
def admin_invitations():
    _require_password_accounts()
    return jsonify({'success': True, 'invitations': invitations.list_for(get_db(), g.user, everyone=True)})


@bp.route('/admin/invitations/<int:token_id>', methods=['DELETE'])
@admin_required
def admin_invitation_revoke(token_id):
    """L'admin révoque n'importe quelle invitation (crédit non rendu, EF-3.7)."""
    _require_password_accounts()
    if not invitations.revoke(get_db(), g.user, token_id, ip=request.remote_addr):
        raise ApiError('Invitation introuvable', 404)
    return jsonify({'success': True})


# -- Réglages (P-T14-6) -------------------------------------------------------------------------------------------------------

def _setting_view(store, setting, conn_overrides) -> dict:
    key = setting.key
    source = store.source(key)
    value = store.get(key)
    allowlist = None
    if setting.destination_service:
        allowed = current_app.config['OUTBOUND'].servers.get(setting.destination_service, ())
        allowlist = sorted(f'{host}:{port}' for host, port in allowed)
    view = {'key': key, 'section': setting.section, 'label': setting.label, 'help': setting.help, 'type': setting.type,
            'min': setting.min, 'max': setting.max, 'max_length': setting.max_length, 'choices': setting.choices,
            'max_items': setting.max_items, 'default': setting.default, 'env': setting.env,
            'secret': setting.secret, 'requires_restart': setting.requires_restart, 'source': source,
            'destination_service': setting.destination_service, 'allowlist': allowlist,
            'value': None if setting.secret else value}
    if setting.env and source == 'base' and conn_overrides.get(setting.env):
        view['env_overridden'] = True                                   # la base surcharge une variable d'environnement
    return view


_DESTINATIONS = (('smtp', 'Serveur SMTP'), ('routing', 'Moteur de routage'), ('dem', "Service d'altitude (MNT)"))


def _destination_refused(cfg, service: str) -> bool:
    """Destination effective (réglage forcé en base compris) hors de OUTBOUND_ALLOWED_HOSTS ; vide = service désactivé."""
    try:
        if service == 'smtp':
            host, port = effective(cfg, 'smtp.host'), effective(cfg, 'smtp.port')
            return bool(host) and not outbound.allowed(cfg, 'smtp', outbound.destination_of_smtp(host, port))
        url = effective(cfg, 'routing.base_url' if service == 'routing' else 'DEM_URL')
        return bool(url) and not outbound.allowed(cfg, service, outbound.destination_of_url(url))
    except outbound.OutboundError:
        return True


def _setting_alerts() -> list:
    """Bandeaux de l'admin (CA-64, T16-n3) : jamais la valeur d'un secret, seulement le service ou le réglage en cause."""
    from app.api.map_routes import allowed_basemaps, default_basemap_alert
    cfg = current_app.config
    alerts = []
    if not current_app.extensions['mailer'].has_settings:
        alerts.append({'code': 'smtp_not_configured', 'message':
                       "E-mail non configuré : les adresses ne peuvent pas être validées, le mot de passe oublié est désactivé."})
    for service, label in _DESTINATIONS:
        if _destination_refused(cfg, service):
            alerts.append({'code': 'destination_not_allowed', 'service': service, 'message':
                           f"{label} : la destination configurée n'est pas dans OUTBOUND_ALLOWED_HOSTS ({service}=) : "
                           "le service ne sera pas appelé."})
    if len(allowed_basemaps(cfg, audit=False)) < len(effective(cfg, 'map.basemaps')):
        alerts.append({'code': 'destination_not_allowed', 'service': 'tiles', 'message':
                       "Fond de carte : un hôte de tuiles est hors OUTBOUND_ALLOWED_HOSTS (tiles=) : ce fond n'est pas servi."})
    alert = default_basemap_alert(cfg)
    if alert:
        alerts.append(alert)
    return alerts


@bp.route('/admin/settings')
@admin_required
def admin_settings():
    store = current_app.config['SETTINGS_STORE']
    store.refresh(get_db(), force=True)
    env = {name: bool(os.environ.get(name)) for name in (s.env for s in REGISTRY.values() if s.env)}
    return jsonify({'success': True, 'version': store.version, 'alerts': _setting_alerts(),
                    'settings': [_setting_view(store, s, env) for s in REGISTRY.values()]})


@bp.route('/admin/settings', methods=['PATCH'])
@admin_required
def admin_settings_patch():
    data = json_body()
    changes = data.get('settings', data)
    if not isinstance(changes, dict) or not changes or len(changes) > 50:
        raise ApiError('Un objet {clé: valeur} (50 au plus) est attendu.')
    store = current_app.config['SETTINGS_STORE']
    try:
        store.set_many(get_db(), changes, actor=_actor(), ip=request.remote_addr)
    except SettingsError as exc:
        refused_policy = exc.field == 'auth.mfa_policy' and 'second facteur' in str(exc)       # CA-13b
        raise ApiError(f"{exc.field} : {exc}", 409 if refused_policy else 400) from None
    return jsonify({'success': True, 'changed': sorted(changes)})


@bp.route('/admin/settings/<path:key>')
@admin_required
def admin_setting_one(key):
    store = current_app.config['SETTINGS_STORE']
    if key not in REGISTRY:
        raise ApiError('Réglage inconnu', 404)
    store.refresh(get_db())
    return jsonify({'success': True, 'setting': _setting_view(store, REGISTRY[key], {})})


@bp.route('/admin/settings/diagnostic')
@admin_required
def admin_settings_diagnostic():
    store = current_app.config['SETTINGS_STORE']
    return jsonify({'success': True, 'diagnostic': store.diagnostic(), 'alerts': _setting_alerts()})


@bp.route('/admin/settings/<path:key>', methods=['DELETE'])
@admin_required
def admin_settings_reset(key):
    store = current_app.config['SETTINGS_STORE']
    if key not in REGISTRY:
        raise ApiError('Réglage inconnu', 404)
    removed = store.reset(get_db(), key, actor=_actor(), ip=request.remote_addr)
    return jsonify({'success': True, 'reset': bool(removed)})


# -- Secrets (P-T14-7) --------------------------------------------------------------------------------------------------------

# Destination liée de chaque secret : le réglage qui la porte (EF-5.5)
SECRET_DESTINATION = {'smtp.password': ('smtp', 'smtp'), 'routing.api_key': ('routing', 'routing.base_url')}


def _bound_destination(name: str):
    """Destination effective normalisée du service du secret, ou None si elle est vide ou hors liste blanche."""
    service, source = SECRET_DESTINATION[name]
    cfg = current_app.config
    try:
        if service == 'smtp':
            host, port = effective(cfg, 'smtp.host'), effective(cfg, 'smtp.port')
            destination = outbound.destination_of_smtp(host, port) if host else None
        else:
            url = effective(cfg, source)
            destination = outbound.destination_of_url(url) if url else None
        if destination is None or not outbound.allowed(cfg, service, destination):
            return None
        return destination
    except outbound.OutboundError:
        return None


def _secret_status(name: str) -> dict:
    store = current_app.extensions['secrets']
    status = store.status(get_db(), name)
    status.setdefault('last4', None)                      # présent pour tous ; renseigné pour les seules clés API (EF-5.3)
    status['bound_is_effective'] = bool(status.get('configured')) and status.get('bound_to') == _bound_destination(name)
    return status


def _secrets_unavailable() -> list:
    """E-10, EF-0.4, CA-61 : mode `oidc` sans SECRETS_KEY ou PUBLIC_URL : saisie de secrets désactivée ; variables manquantes."""
    if current_app.config.get('AUTH_MODE') != 'oidc' or current_app.config.get('PASSWORD_ACCOUNTS'):
        return []
    return list(current_app.config.get('PASSWORD_ACCOUNTS_MISSING') or [])


def _require_password_accounts() -> None:
    """CA-61 : comptes à mot de passe indisponibles (mode oidc sans SECRETS_KEY ou PUBLIC_URL) : invitations en 404."""
    if current_app.config.get('AUTH_MODE') == 'oidc' and not modes.password_accounts_available():
        raise ApiError('Introuvable', 404)


def _require_secrets() -> None:
    if _secrets_unavailable():
        raise ApiError('Introuvable', 404)


@bp.route('/admin/secrets')
@admin_required
def admin_secrets():
    missing = _secrets_unavailable()
    return jsonify({'success': True, 'available': current_app.extensions['secrets'].available,
                    'disabled': bool(missing), 'missing': missing,
                    'secrets': [_secret_status(n) for n in SECRET_KINDS]})


@bp.route('/admin/secrets/<name>')
@admin_required
def admin_secret_one(name):
    if name not in SECRET_KINDS:
        raise ApiError('Secret inconnu', 404)
    return jsonify({'success': True, 'secret': _secret_status(name)})


@bp.route('/admin/secrets/<name>', methods=['PUT'])
@admin_required
def admin_secret_put(name):
    _require_secrets()
    if name not in SECRET_KINDS:
        raise ApiError('Secret inconnu', 404)
    data = json_body()
    value = data.get('value')
    if not isinstance(value, str):
        raise ApiError('Valeur requise.')
    destination = _bound_destination(name)
    if destination is None:
        raise ApiError("Configure d'abord la destination (hôte autorisé) de ce service.", 409)
    try:
        current_app.extensions['secrets'].set(get_db(), name, value, destination, actor=_actor(), ip=request.remote_addr)
    except SecretsError:
        raise ApiError("Les secrets ne sont pas disponibles (SECRETS_KEY).", 409) from None
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return jsonify({'success': True, 'secret': _secret_status(name)})


@bp.route('/admin/secrets/<name>', methods=['DELETE'])
@admin_required
def admin_secret_delete(name):
    if name not in SECRET_KINDS:
        raise ApiError('Secret inconnu', 404)
    cleared = current_app.extensions['secrets'].clear(get_db(), name, actor=_actor(), ip=request.remote_addr)
    return jsonify({'success': True, 'cleared': cleared})


@bp.route('/admin/secrets/<name>/test', methods=['POST'])
@admin_required
def admin_secret_test(name):
    """« Tester » par service (EF-5.6) : ok / ko SEULEMENT, rendu à l'échéance du service, quel que soit le résultat."""
    _require_secrets()
    if name not in SECRET_KINDS:
        raise ApiError('Secret inconnu', 404)
    if name == 'smtp.password':
        address = g.user['email'] or g.user['pending_email']
        if not address:
            raise ApiError("Aucune adresse pour envoyer le test.", 409)
        return jsonify({'success': True, **current_app.extensions['mailer'].send_test(address)})
    tester = current_app.extensions.get('routing_test')
    if tester is None:
        return jsonify({'success': True, 'ok': False})
    return jsonify({'success': True, **tester()})


@bp.route('/admin/mail/test', methods=['POST'])
@admin_required
def admin_mail_test():
    """E-mail de test (EF-4.2) : à l'adresse validée de l'admin, sinon son adresse en attente (P-T14-9)."""
    address = g.user['email'] or g.user['pending_email']
    if not address:
        raise ApiError("Ton compte n'a aucune adresse : impossible d'envoyer un test.", 409)
    return jsonify({'success': True, **current_app.extensions['mailer'].send_test(address)})


# -- Fonctions bêta (P-T14-5) -------------------------------------------------------------------------------------------------

def _feature_view(key: str) -> dict:
    db = get_db()
    state = features.get_state(db, key)
    names = {}
    if state['users']:
        marks = ','.join('?' * len(state['users']))
        names = {r['id']: r['username'] for r in db.execute(f"SELECT id, username FROM users WHERE id IN ({marks})",
                                                            state['users'])}
    diagnostic = None
    hook = current_app.extensions.get('feature_diagnostics', {}).get(key)
    if hook is not None:
        diagnostic = hook()
    return {'key': key, 'label': features.FEATURES[key].label, 'description': features.FEATURES[key].help,
            'state': state['state'], 'users': [{'id': i, 'username': names.get(i)} for i in state['users']],
            'updated_at': state['updated_at'], 'updated_by': state['updated_by'], 'diagnostic': diagnostic}


@bp.route('/admin/features')
@admin_required
def admin_features():
    return jsonify({'success': True, 'timezone': effective(current_app.config, 'APP_TIMEZONE'),
                    'features': [_feature_view(k) for k in features.FEATURES]})


@bp.route('/admin/features/<key>', methods=['PATCH'])
@admin_required
def admin_feature_patch(key):
    if key not in features.FEATURES:
        raise ApiError('Fonction inconnue', 404)
    data = json_body()
    if 'state' not in data and 'users' not in data:
        raise ApiError('Rien à modifier')
    db = get_db()
    state = data.get('state', features.get_state(db, key)['state'])
    ids = data.get('users')
    if ids is not None and not isinstance(ids, list):
        raise ApiError('`users` : liste d’identifiants attendue')
    try:
        features.set_state(db, key, state, actor=_actor(), user_ids=ids, ip=request.remote_addr)
    except features.FeatureError as exc:
        raise ApiError(str(exc)) from None
    return jsonify({'success': True, 'feature': _feature_view(key)})


# -- Journal (P-T14-8) --------------------------------------------------------------------------------------------------------

@bp.route('/admin/audit')
@admin_required
def admin_audit():
    args = request.args

    def integer(name, default=None, low=None, high=None):
        raw = args.get(name)
        if raw in (None, ''):
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ApiError(f'{name} : entier attendu') from None
        if (low is not None and value < low) or (high is not None and value > high):
            raise ApiError(f'{name} hors bornes')
        return value

    limit = integer('limit', 50, 1, 200)
    try:
        entries = audit.list_entries(get_db(), action=args.get('action') or None, actor_type=args.get('actor_type') or None,
                                     since=args.get('since') or None, until=args.get('until') or None,
                                     before_id=integer('before_id', None, 1), limit=limit,
                                     target_type=args.get('target_type') or None, target_id=args.get('target_id') or None)
    except ValueError as exc:
        raise ApiError(str(exc)) from None
    return jsonify({'success': True, 'entries': entries,
                    'next_before_id': entries[-1]['id'] if len(entries) == limit else None})

