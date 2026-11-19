"""Client OIDC (authorization code + PKCE) et routes de connexion."""

import logging
import sqlite3
import time
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from joserfc import jwt
from joserfc.jwk import KeySet
from flask import current_app, g, redirect, render_template, request, session, url_for

from app.auth import bp, users
from app.auth.guard import oidc_enabled, safe_next
from app.db import get_db

logger = logging.getLogger(__name__)

oauth = OAuth()
# Authlib range chaque demande de connexion en cours dans la session, sous cette clé + state
STATE_PREFIX = '_state_oidc_'
# Connexions entamées conservées en parallèle (plusieurs onglets) ; au-delà, les plus anciennes sautent
MAX_PENDING_LOGINS = 3


def init_oidc(app) -> None:
    """Enregistre le client OIDC. La configuration a déjà été validée (auth/checks.py)."""
    if not app.config.get('OIDC_ISSUER'):
        return

    oauth.init_app(app)
    oauth.register(
        name='oidc',
        client_id=app.config['OIDC_CLIENT_ID'],
        client_secret=app.config['OIDC_CLIENT_SECRET'],
        server_metadata_url=f"{app.config['OIDC_ISSUER']}/.well-known/openid-configuration",
        client_kwargs={'scope': app.config['OIDC_SCOPES'], 'code_challenge_method': 'S256'},
        overwrite=True,
    )
    logger.info("Authentification OIDC activée (issuer=%s)", app.config['OIDC_ISSUER'])


def _verified_access_token_claims(token: dict, sub: str) -> dict:
    """
    Claims de l'access token s'il s'agit d'un JWT signé par l'IdP, émis pour nous et pour
    ce même utilisateur. Retourne {} dans tous les autres cas (jeton opaque, signature
    invalide, audience ou sujet différents, expiré).
    """
    try:
        key_set = KeySet.import_key_set(oauth.oidc.fetch_jwk_set())
        claims = dict(jwt.decode(token['access_token'], key_set).claims)
    except Exception as exc:  # noqa: BLE001 - jeton opaque ou invalide : on l'ignore
        logger.debug("Access token non exploitable comme JWT : %s", exc)
        return {}
    audience = claims.get('aud')
    audiences = audience if isinstance(audience, list) else [audience]
    if (claims.get('sub') != sub or current_app.config['OIDC_CLIENT_ID'] not in audiences
            or float(claims.get('exp') or 0) < time.time()):
        logger.warning("Access token ignoré : sujet, audience ou expiration incohérents")
        return {}
    return claims


def _fetch_claims(token: dict) -> dict:
    """
    Claims de l'ID token (signature, iss, aud, nonce vérifiés par Authlib), complétés si
    besoin par /userinfo puis par l'access token : selon l'IdP et sa politique de scopes,
    le profil et les rôles ne sont pas toujours dans l'ID token (cas de FusionAuth, qui
    place les rôles applicatifs dans l'access token).
    """
    claims = dict(token.get('userinfo') or {})
    sub = claims.get('sub')
    need_profile = not claims.get('email') and not claims.get('preferred_username')
    need_roles = bool(current_app.config['OIDC_ADMIN_ROLE']) and 'roles' not in claims

    if need_profile or need_roles:
        try:
            extra = dict(oauth.oidc.userinfo(token=token))
            # Le sub de /userinfo doit correspondre à celui de l'ID token (OIDC Core 5.3.2)
            if extra.get('sub') == sub:
                claims = {**extra, **claims}
        except Exception as exc:  # noqa: BLE001 - best effort, le sub suffit
            logger.warning("Appel userinfo en échec : %s", exc)

    if need_roles and 'roles' not in claims and sub:
        access_claims = _verified_access_token_claims(token, sub)
        if 'roles' in access_claims:
            claims['roles'] = access_claims['roles']

    logger.debug("Claims OIDC disponibles : %s", sorted(claims))
    return claims


def _jwks_has_keys() -> bool:
    try:
        return bool((oauth.oidc.fetch_jwk_set(force=True) or {}).get('keys'))
    except Exception:  # noqa: BLE001 - simple aide au diagnostic
        return True


def _purge_pending_logins() -> None:
    """
    Retire de la session les connexions entamées périmées ou trop nombreuses. Authlib ne les
    nettoie qu'après une connexion réussie : chaque tentative abandonnée restait dans le
    cookie, qui finissait par dépasser la taille acceptée par le navigateur (4 ko) et
    bloquait toute nouvelle connexion.
    """
    now = time.time()
    pending = []
    for key in [k for k in session if k.startswith(STATE_PREFIX)]:
        entry = session[key]
        expires = entry.get('exp') if isinstance(entry, dict) else None
        if not isinstance(expires, (int, float)) or expires < now:
            session.pop(key)
        else:
            pending.append((expires, key))
    # La demande en cours va s'ajouter : on garde les plus récentes, moins une
    for _expires, key in sorted(pending)[:max(0, len(pending) - (MAX_PENDING_LOGINS - 1))]:
        session.pop(key)


@bp.route('/auth/login')
def login():
    if not oidc_enabled():
        return redirect(url_for('main.index'))
    _purge_pending_logins()
    session['next'] = safe_next(request.args.get('next', ''))
    return oauth.oidc.authorize_redirect(url_for('auth.callback', _external=True))


@bp.route('/auth/callback')
def callback():
    if not oidc_enabled():
        return redirect(url_for('main.index'))
    try:
        token = oauth.oidc.authorize_access_token()
        user = users.upsert_from_claims(
            get_db(), _fetch_claims(token),
            admin_role=current_app.config['OIDC_ADMIN_ROLE'],
            auto_activate=current_app.config['OIDC_AUTO_ACTIVATE'])
        token_value = users.session_token(get_db(), user['id'])
    except Exception:  # noqa: BLE001 - toute erreur OIDC => échec de login propre
        # Détail dans les logs uniquement : rien d'exploitable renvoyé au navigateur
        logger.exception("Échec de l'authentification OIDC")
        if not _jwks_has_keys():
            logger.error("Le JWKS de l'IdP est vide : il signe sans doute les ID tokens en HS256. "
                         "Associer une clé asymétrique (RS256/ES256) à l'application côté IdP.")
        return render_template('auth_message.html', title='Connexion impossible',
                               message="L'authentification a échoué.", show_login=True), 401

    next_url = session.pop('next', None)
    session.clear()  # nouvelle session après login (anti-fixation)
    session['user_id'] = user['id']
    session['token'] = token_value
    session['login_at'] = int(time.time())
    session.permanent = True
    return redirect(safe_next(next_url or ''))


@bp.route('/auth/logout', methods=['POST'])
def logout():
    user = g.get('user')
    session.clear()
    if user is not None:
        # Le cookie est signé mais vit chez le client : le vider ne suffit pas, une copie faite
        # avant resterait valable. Changer le jeton du compte ferme toutes ses sessions.
        try:
            users.revoke_sessions(get_db(), user['id'])
        except sqlite3.Error:
            logger.exception("Déconnexion : jeton de session du compte %s non renouvelé", user['id'])
    if not oidc_enabled():
        return redirect(url_for('main.index'))
    try:
        end_session = oauth.oidc.load_server_metadata().get('end_session_endpoint')
    except Exception as exc:  # noqa: BLE001
        logger.warning("Métadonnées OIDC indisponibles au logout : %s", exc)
        end_session = None
    if end_session:
        params = urlencode({
            'client_id': current_app.config['OIDC_CLIENT_ID'],
            'post_logout_redirect_uri': url_for('auth.logged_out', _external=True),
        })
        return redirect(f"{end_session}{'&' if '?' in end_session else '?'}{params}")
    return redirect(url_for('auth.logged_out'))


@bp.route('/auth/logged-out')
def logged_out():
    return render_template('auth_message.html', title='Déconnecté',
                           message='Tu es déconnecté.', show_login=True)
