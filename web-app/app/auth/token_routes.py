"""
Routes publiques des jetons (EF-S8, B4, r5, r8) : `GET /auth/token` (page statique), `POST /auth/token/inspect`,
`POST /auth/token/consume`.

  - Le jeton n'est JAMAIS dans le chemin ni la query : il est dans le fragment (`#t=…`), lu par le JS de la page, effacé
    de l'URL, puis envoyé dans le CORPS d'un POST. Les journaux d'accès (gunicorn, proxy) n'en voient donc aucune trace.
  - La page n'embarque aucune donnée du jeton ni ressource tierce ; aucun GET ne consomme quoi que ce soit.
  - `inspect` : même réponse (404, même corps) pour tout jeton inconnu, expiré, utilisé, révoqué ou mal formé ; pour un
    jeton valable, SEULEMENT le type de formulaire (jamais adresse, émetteur, note ni échéance).
  - La consommation n'ouvre jamais de session (r8) : elle se termine par une redirection vers la page de connexion.
    `verify_email` exige au contraire la session complète du compte cible, sans quoi le jeton n'est pas consommé (R2).
  - Limitation par IP (clé /64 pour IPv6) AVANT tout traitement ; formulaire public protégé par un jeton CSRF sans état.
"""

import logging

from flask import current_app, g, jsonify, render_template, request, url_for

from app.audit import Actor
from app.auth import bp, csrf, invitations, modes, passwords, tokens
from app.db import get_db
from app.ratelimit import ip_key

logger = logging.getLogger(__name__)

INVALID = {'success': False, 'error': 'Lien invalide ou expiré.'}
MAX_FIELD = 1024


def _guard():
    """None si la requête peut être traitée ; sinon la réponse (404 si les comptes à mot de passe sont indisponibles)."""
    if not modes.password_accounts_available():
        return jsonify({'success': False, 'error': 'Introuvable'}), 404
    return None


def _limited():
    allowed, retry = current_app.extensions['limiters'].token_ip.hit(ip_key(request.remote_addr))
    if allowed:
        return None
    response = jsonify({'success': False, 'error': 'Trop de tentatives, réessaie plus tard.'})
    response.status_code = 429
    response.headers['Retry-After'] = str(retry)
    return response


def _body():
    """Corps JSON borné : un objet dont les champs sont des chaînes courtes, sinon None."""
    if (request.content_length or 0) > 8192:
        return None
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None
    return {k: v for k, v in data.items() if isinstance(k, str)}


def _checked():
    """Contrôles communs aux deux POST : mode, limite, CSRF, corps. Retourne (réponse d'erreur, corps)."""
    refused = _guard() or _limited()
    if refused:
        return refused, None
    if not csrf.verify(request.headers.get('X-CSRF-Token')):
        return (jsonify({'success': False, 'error': 'Formulaire expiré : recharge la page.'}), 403), None
    data = _body()
    if data is None:
        return (jsonify({'success': False, 'error': 'Requête invalide'}), 400), None
    return None, data


def _text(data, name):
    value = data.get(name)
    return value if isinstance(value, str) and len(value) <= MAX_FIELD else None


def _queue_verification(db, account) -> None:
    """
    Mail de validation d'adresse (EF-4.3), en FILE D'ENVOI : la réponse ne dépend jamais de l'envoi. Sans SMTP, l'adresse
    reste en attente.
    """
    from app.mailer import compose as mail_compose          # import tardif : mailer dépend de app.auth
    mailer = current_app.extensions['mailer']
    row = db.execute("SELECT pending_email FROM users WHERE id = ?", (account['id'],)).fetchone()
    if not mailer.configured or row is None or not row['pending_email']:
        return
    try:
        created = tokens.create(db, 'verify_email', actor=Actor.user(account['id'], account['username']),
                                target_user_id=account['id'], email=row['pending_email'])
        subject, body = mail_compose('verification', link=tokens.link(current_app.config['PUBLIC_URL'], created.token),
                                     username=account['username'])
        mailer.enqueue(row['pending_email'], subject, body)
    except Exception:  # noqa: BLE001 - le compte est créé : un échec de mise en file n'est pas une erreur pour l'invité
        logger.exception("Mail de validation non mis en file")


@bp.route('/auth/token')
def token_page():
    """Page STATIQUE : aucune donnée de jeton côté serveur. Son JS lit le fragment de l'URL."""
    if not modes.password_accounts_available():
        return render_template('auth_message.html', title='Introuvable', message='Cette page n’existe pas.'), 404
    return render_template('token.html', csrf_token=csrf.issue())


@bp.route('/auth/token/inspect', methods=['POST'])
def token_inspect():
    refused, data = _checked()
    if refused:
        return refused
    form = tokens.inspect(get_db(), _text(data, 'token'))
    if form is None:
        return jsonify(INVALID), 404
    return jsonify({'success': True, 'form': form})


@bp.route('/auth/token/consume', methods=['POST'])
def token_consume():
    refused, data = _checked()
    if refused:
        return refused
    db, token = get_db(), _text(data, 'token')
    found = tokens.peek(db, token)
    if found is None:
        return jsonify(INVALID), 404
    kind = found[0]
    login = url_for('auth.password_login')       # jetons de comptes password : jamais chez l'IdP (T10-3)
    ip = request.remote_addr
    try:
        if kind in ('invite', 'bootstrap'):
            username, password = _text(data, 'username'), _text(data, 'password')
            if username is None or password is None:
                return jsonify({'success': False, 'error': 'Identifiant et mot de passe requis.'}), 400
            result = invitations.accept(db, token, username, password, ip=ip)
        elif kind == 'reset':
            password = _text(data, 'password')
            if password is None:
                return jsonify({'success': False, 'error': 'Mot de passe requis.'}), 400
            result = invitations.reset_password(db, token, password, ip=ip)
        elif kind == 'verify_email':
            user = g.get('user')
            if user is None:
                # Pas de session du compte : rien n'est consommé (R2). Message neutre, aucun identifiant affiché.
                return jsonify({'success': False, 'login_required': True,
                                'error': 'Connecte-toi au compte concerné, puis rouvre ce lien.'}), 403
            outcome = invitations.verify_email(db, user, token, ip=ip)
            if outcome == 'invalid':
                return jsonify(INVALID), 404
            if outcome == 'refused':
                return jsonify({'success': False, 'error': 'Impossible de valider cette adresse.'}), 409
            return jsonify({'success': True, 'next': url_for('main.index')})
        else:
            return jsonify(INVALID), 404            # set_password (conversion SSO) : arrive avec T14
    except passwords.PolicyError as exc:
        return jsonify({'success': False, 'code': exc.code, 'error': str(exc)}), 400
    except passwords.HashBusy:
        return jsonify({'success': False, 'error': 'Service occupé, réessaie dans un instant.'}), 429
    except invitations.AccountError as exc:
        return jsonify({'success': False, 'code': exc.code, 'error': exc.message}), exc.status
    if result is None:
        return jsonify(INVALID), 404
    if kind in ('invite', 'bootstrap'):
        _queue_verification(db, result)
    if kind == 'reset':
        current_app.extensions['pending'].destroy_for_user(result)    # un état mfa_pending ne survit pas au mot de passe (T10-1)
    return jsonify({'success': True, 'next': login})     # jamais de session ouverte ici (r8)
