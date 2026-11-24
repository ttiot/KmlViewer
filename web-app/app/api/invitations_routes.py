"""API des invitations (EF-3) : les siennes, création, révocation. Toujours derrière une session complète."""

import logging
from datetime import timedelta

from flask import current_app, g, jsonify, request

from app.api import bp
from app.api.payload import json_body
from app.auth import invitations, modes, tokens
from app.db import get_db
from app.mailer import MailError
from app.settings import effective
from app.mailer import compose as mail_compose
from app.errors import ApiError

logger = logging.getLogger(__name__)


def _available():
    if not modes.password_accounts_available():
        raise ApiError('Introuvable', 404)


def _issuer():
    """Ligne du compte courant, relue (les crédits changent)."""
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (g.user['id'],)).fetchone()
    if row is None:
        raise ApiError('Authentification requise', 401)
    return row


def _send_invitation(created, link, data, issuer) -> bool:
    """Envoi SYNCHRONE (EF-4.8) : l'émetteur doit savoir si le message est parti ; sinon il transmet le lien lui-même."""
    mailer = current_app.extensions['mailer']
    if not mailer.configured:
        return False
    subject, body = mail_compose('invitation', link=link, inviter=issuer['username'], note=data.get('note') or None)
    try:
        mailer.send(data['email'], subject, body)
    except MailError:
        return False
    tokens.mark_sent(get_db(), created.id)
    return True


@bp.route('/invitations')
def list_invitations():
    _available()
    issuer = _issuer()
    return jsonify({'invitations': invitations.list_for(get_db(), issuer), 'invite_credits': issuer['invite_credits'],
                    'can_invite': invitations.can_invite(issuer), 'is_admin': bool(issuer['is_admin'])})


@bp.route('/invitations', methods=['POST'])
def create_invitation():
    _available()
    data = json_body()
    email, note, credits = data.get('email'), data.get('note'), data.get('initial_credits', 0)
    if not isinstance(email, str) or (note is not None and not isinstance(note, str)) \
            or isinstance(credits, bool) or not isinstance(credits, int):
        raise ApiError('Requête invalide', 400)
    issuer = _issuer()
    try:
        created = invitations.create(get_db(), issuer, email, note=note, initial_credits=credits, ip=request.remote_addr,
                                     ttl=timedelta(hours=effective(current_app.config, 'auth.invite_ttl_hours')))
    except invitations.InvitationError as exc:
        raise ApiError(exc.message, exc.status) from exc
    # Le lien n'est montré QU'ICI, une fois ; il se construit sur PUBLIC_URL, jamais sur l'en-tête Host (EF-S7)
    link = tokens.link(current_app.config['PUBLIC_URL'], created.token)
    sent = _send_invitation(created, link, data, issuer)
    return jsonify({'success': True, 'id': created.id, 'expires_at': created.expires_at, 'link': link,
                    'sent_by_mail': sent}), 201


@bp.route('/invitations/<int:token_id>', methods=['DELETE'])
def revoke_invitation(token_id):
    _available()
    if not invitations.revoke(get_db(), _issuer(), token_id, ip=request.remote_addr):
        raise ApiError('Invitation introuvable', 404)
    return jsonify({'success': True})
