"""Durcissement HTTP : reverse proxy, en-têtes de sécurité, protection CSRF."""

import logging
from urllib.parse import urlsplit

from flask import jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
LOCAL_HOSTS = ('localhost', '127.0.0.1', '[::1]')

# Scripts : uniquement ceux servis par l'application (bibliothèques embarquées dans
# static/vendor, aucun gestionnaire inline). Un contenu injecté ne peut donc rien exécuter.
# Styles : 'unsafe-inline' reste nécessaire pour les attributs style des templates.
# Images : https: pour les tuiles des fonds de carte.
CONTENT_SECURITY_POLICY = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob: https:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
])


def _check_same_origin():
    """
    Anti-CSRF pour une API à cookie de session : toute requête modifiante émise par un
    navigateur doit provenir de notre propre origine (en complément de SameSite=Lax).
    """
    if request.method not in UNSAFE_METHODS:
        return None
    origin = request.headers.get('Origin')
    if origin:
        if urlsplit(origin).netloc != request.host:
            # %r : le chemin est décodé (%0a -> saut de ligne), il ne doit pas forger de ligne de log
            logger.warning("Requête %s %r refusée : origine %r", request.method, request.path, origin)
            return jsonify({'success': False, 'error': 'Origine de la requête refusée'}), 403
    elif request.headers.get('Sec-Fetch-Site') in {'cross-site', 'same-site'}:
        logger.warning("Requête %s %r refusée : Sec-Fetch-Site=%r", request.method, request.path,
                       request.headers.get('Sec-Fetch-Site'))
        return jsonify({'success': False, 'error': 'Origine de la requête refusée'}), 403
    return None


def _set_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    # L'origine seule part vers les tiers : les serveurs de tuiles OSM refusent (403) les
    # requêtes sans Referer, et aucun chemin ni paramètre de l'appli ne fuit.
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    response.headers.setdefault('Content-Security-Policy', CONTENT_SECURITY_POLICY)
    if request.is_secure:
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
    if request.path.startswith(('/api/', '/auth/')):
        response.headers.setdefault('Cache-Control', 'no-store')
    return response


def init_security(app) -> None:
    hops = app.config.get('TRUST_PROXY', 0)
    if hops:
        x_host = hops if app.config.get('TRUST_PROXY_HOST') else 0
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=x_host)

    # Sans authentification, l'en-tête Host est la seule barrière contre le DNS rebinding :
    # un site tiers qui se résout vers 127.0.0.1 deviendrait « même origine » que l'appli.
    hosts = list(app.config.get('TRUSTED_HOSTS') or [])
    if not hosts and not app.config.get('OIDC_ISSUER'):
        hosts = list(LOCAL_HOSTS)
    app.config['TRUSTED_HOSTS'] = hosts or None
    if hosts:
        logger.info("Noms d'hôte acceptés : %s", ', '.join(hosts))
    app.before_request(_check_same_origin)
    app.after_request(_set_security_headers)
