"""Durcissement HTTP : reverse proxy, en-têtes de sécurité, protection CSRF, bornes d'URL."""

import logging
from urllib.parse import urlsplit

from flask import current_app, request
from werkzeug.exceptions import SecurityError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.routing import IntegerConverter

from app.errors import json_error

logger = logging.getLogger(__name__)

UNSAFE_METHODS = frozenset({'POST', 'PUT', 'PATCH', 'DELETE'})
LOCAL_HOSTS = ('localhost', '127.0.0.1', '[::1]')
# Sonde de santé : servie quel que soit l'en-tête Host (voir _reject_untrusted_host)
HEALTH_PATH, HEALTH_ENDPOINT = '/api/health', 'api.health_check'
# Plus grand entier qu'une colonne SQLite sait stocker
SQLITE_MAX_INT = 2 ** 63 - 1

# Scripts : uniquement ceux servis par l'application (bibliothèques embarquées dans
# static/vendor, aucun gestionnaire inline). Un contenu injecté ne peut donc rien exécuter.
# Styles : 'unsafe-inline' reste nécessaire pour les attributs style des templates.
# Images : https: pour les tuiles des fonds de carte.
# form-action n'hérite pas de default-src : sans lui, un formulaire injecté pourrait poster
# vers un site tiers.
CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "frame-ancestors 'none'",
)


class SqliteIntConverter(IntegerConverter):
    """
    Remplace le convertisseur `int` des routes : un identifiant hors des bornes d'un entier
    SQLite ne correspond à aucune route (404) au lieu de faire échouer la requête SQL (500).
    """

    def __init__(self, url_map, *args, **kwargs):
        super().__init__(url_map, *args, **kwargs)
        if self.max is None:
            self.max = SQLITE_MAX_INT


def content_security_policy(oidc_issuer: str = '', image_hosts=()) -> str:
    """
    Politique de l'application. Avec l'OIDC, le formulaire de déconnexion est redirigé vers
    l'IdP (end_session) : Chrome applique form-action aux redirections, son origine est donc
    autorisée en plus de la nôtre.
    """
    form_targets = ["'self'"]
    issuer = urlsplit(oidc_issuer or '')
    if issuer.scheme and issuer.netloc:
        form_targets.append(f"{issuer.scheme}://{issuer.netloc}")
    # Images (R1) : 'self', data:, blob: et EXACTEMENT les hôtes de tuiles autorisés (calculés au démarrage depuis l'env) ;
    # plus de `https:` : un fond de carte hors liste ne peut pas faire fuiter l'IP ni les zones consultées
    img_src = "img-src " + " ".join(["'self'", 'data:', 'blob:', *sorted(image_hosts)])
    return "; ".join([*CSP_DIRECTIVES, img_src, "form-action " + " ".join(form_targets)])


def _reject_untrusted_host():
    """
    Un en-tête Host hors de TRUSTED_HOSTS est refusé (400) avant tout autre traitement. Flask
    ne lève ce refus qu'au moment du routage, après les before_request : le contrôle d'accès
    tournerait d'abord, sans pouvoir construire la moindre URL (redirection vers le login).

    Seule exception : la sonde de santé. Elle est interrogée sans le nom public de l'appli
    (HEALTHCHECK de l'image sur localhost, sonde d'un répartiteur de charge sur l'adresse du
    conteneur) ; la refuser rendait l'image « unhealthy » dès que TRUSTED_HOSTS était posé,
    donc toujours en OIDC. Sa réponse est fixe (état et version), ne dépend pas du Host et
    ne construit aucune URL : la servir sous un nom quelconque n'ouvre rien.
    """
    if not isinstance(request.routing_exception, SecurityError):
        return None
    if request.method in ('GET', 'HEAD') and request.path == HEALTH_PATH:
        return current_app.view_functions[HEALTH_ENDPOINT]()
    logger.warning("Requête refusée : en-tête Host %r hors de TRUSTED_HOSTS", request.headers.get('Host'))
    raise request.routing_exception


def _origin_tuple(url: str):
    """(schéma, hôte, port effectif) d'une origine, ou None si elle est inutilisable."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https') or not host or parts.username is not None or parts.password is not None:
        return None                    # « https://evil@hôte » n'est pas l'origine de « hôte »
    return parts.scheme, host.lower(), port if port is not None else (443 if parts.scheme == 'https' else 80)


def _origin_matches(origin: str) -> bool:
    """
    L'Origin d'une requête modifiante doit être la NÔTRE. Avec PUBLIC_URL (obligatoire en mode password) : schéma, hôte et port
    égaux à ceux de PUBLIC_URL, quel que soit ce que le proxy transmet (derrière un proxy TLS sans TRUST_PROXY l'application
    voit « http » alors que le navigateur envoie « https »). Sans PUBLIC_URL : hôte et port comparés à l'en-tête Host, sans le
    schéma (comportement antérieur, que seul un proxy de confiance permet de durcir).
    """
    public = current_app.config.get('PUBLIC_URL')
    if public:
        return _origin_tuple(origin) == _origin_tuple(public)
    return urlsplit(origin).netloc == request.host


def _check_same_origin():
    """
    Anti-CSRF pour une API à cookie de session : toute requête modifiante émise par un
    navigateur doit provenir de notre propre origine (en complément de SameSite=Lax).
    """
    if request.method not in UNSAFE_METHODS:
        return None
    origin = request.headers.get('Origin')
    if origin:
        if not _origin_matches(origin):
            # %r : le chemin est décodé (%0a -> saut de ligne), il ne doit pas forger de ligne de log
            logger.warning("Requête %s %r refusée : origine %r", request.method, request.path, origin)
            return json_error('Origine de la requête refusée', 403)
    elif request.headers.get('Sec-Fetch-Site') in {'cross-site', 'same-site'}:
        logger.warning("Requête %s %r refusée : Sec-Fetch-Site=%r", request.method, request.path,
                       request.headers.get('Sec-Fetch-Site'))
        return json_error('Origine de la requête refusée', 403)
    return None


def init_security(app) -> None:
    hops = app.config.get('TRUST_PROXY', 0)
    if hops:
        x_host = hops if app.config.get('TRUST_PROXY_HOST') else 0
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=hops, x_proto=hops, x_host=x_host)

    # À poser avant l'enregistrement des blueprints : toutes les routes <int:…> en héritent
    app.url_map.converters['int'] = SqliteIntConverter

    # Sans authentification, l'en-tête Host est la seule barrière contre le DNS rebinding :
    # un site tiers qui se résout vers 127.0.0.1 deviendrait « même origine » que l'appli.
    # Avec l'OIDC, la liste est exigée au démarrage (app/auth/checks.py).
    hosts = list(app.config.get('TRUSTED_HOSTS') or [])
    if not hosts and app.config.get('AUTH_MODE', 'local') == 'local':
        hosts = list(LOCAL_HOSTS)
    app.config['TRUSTED_HOSTS'] = hosts or None
    if hosts:
        logger.info("Noms d'hôte acceptés : %s", ', '.join(hosts))

    from app.outbound import Allowlist, img_src_hosts
    allowlist = app.config.get('OUTBOUND')
    policy = content_security_policy(app.config.get('OIDC_ISSUER') or '',
                                     img_src_hosts(allowlist if isinstance(allowlist, Allowlist) else Allowlist()))

    def set_security_headers(response):
        response.headers.setdefault('X-Content-Type-Options', 'nosniff')
        response.headers.setdefault('X-Frame-Options', 'DENY')
        # L'origine seule part vers les tiers : les serveurs de tuiles OSM refusent (403) les
        # requêtes sans Referer, et aucun chemin ni paramètre de l'appli ne fuit.
        response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        response.headers.setdefault('Content-Security-Policy', policy)
        if request.is_secure:
            response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000')
        if request.path.startswith(('/api/', '/auth/', '/admin', '/profile')):
            response.headers.setdefault('Cache-Control', 'no-store')
        return response

    app.before_request(_reject_untrusted_host)
    app.before_request(_check_same_origin)
    app.after_request(set_security_headers)
