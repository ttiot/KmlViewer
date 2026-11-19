"""Contrôle d'accès : tout est fermé par défaut, sauf une liste blanche explicite."""

import time
from functools import wraps
from urllib.parse import urlsplit

from flask import abort, current_app, g, jsonify, redirect, render_template, request, session, url_for

from app.auth import users
from app.db import get_db

# Seuls ces endpoints sont accessibles sans session. Toute nouvelle route est protégée d'office.
PUBLIC_ENDPOINTS = frozenset({
    'static', 'auth.login', 'auth.callback', 'auth.logout', 'auth.logged_out', 'api.health_check',
})


def oidc_enabled() -> bool:
    return bool(current_app.config.get('OIDC_ISSUER'))


def wants_json() -> bool:
    return request.path.startswith('/api/')


def safe_next(target: str) -> str:
    """N'accepte qu'un chemin local, pour éviter les redirections ouvertes."""
    fallback = url_for('main.index')
    if not target or not target.startswith('/') or target.startswith('//') or '\\' in target:
        return fallback
    # Tabulation, saut de ligne, espace : les navigateurs (et Werkzeug) les retirent de
    # l'URL, « /\t/evil.com » deviendrait « //evil.com »
    if any(ord(char) <= 0x20 or ord(char) == 0x7f for char in target):
        return fallback
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return fallback
    return target


def session_expired() -> bool:
    """
    Durée de vie absolue, comptée depuis la connexion : le cookie est rafraîchi à chaque
    requête, sans cette borne une session active (ou volée) ne finirait jamais, et un rôle
    retiré côté IdP ne serait jamais relu.
    """
    login_at = session.get('login_at')
    if not isinstance(login_at, (int, float)):
        return True
    lifetime = current_app.permanent_session_lifetime.total_seconds()
    return not 0 <= time.time() - login_at <= lifetime


def _load_user_and_guard():
    g.user = None
    if request.endpoint == 'static':
        return None

    if not oidc_enabled():
        g.user = users.get_or_create_local_user(get_db())
        return None

    user_id = session.get('user_id')
    if user_id is not None:
        if session_expired():
            session.clear()
        else:
            g.user = users.get_by_id(get_db(), user_id)
            if g.user is None:
                session.clear()

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    if g.user is None:
        if wants_json():
            return jsonify({'success': False, 'error': 'Authentification requise'}), 401
        return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))

    if not g.user['is_active']:
        if wants_json():
            return jsonify({'success': False, 'error': 'Compte désactivé'}), 403
        return render_template(
            'auth_message.html', title='Compte en attente',
            message="Ton compte n'est pas (ou plus) activé. Contacte un administrateur."), 403
    return None


def init_guard(app) -> None:
    app.before_request(_load_user_and_guard)
    app.context_processor(lambda: {'current_user': g.get('user'), 'oidc_enabled': oidc_enabled()})


def current_user_id() -> int:
    """Identifiant de l'utilisateur courant ; toutes les requêtes SQL métier filtrent dessus."""
    if g.get('user') is None:
        abort(401)
    return g.user['id']


def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get('user') is None or not g.user['is_admin']:
            if wants_json():
                return jsonify({'success': False, 'error': 'Droits administrateur requis'}), 403
            abort(403)
        return view(*args, **kwargs)
    return wrapper
