"""Notifications de sécurité d'un compte (EF-4.7, P-T11-6) : à l'adresse VALIDÉE seulement, jamais bloquantes."""

import logging

from flask import current_app

logger = logging.getLogger(__name__)


def security_notice(user, kind: str, **context) -> str:
    """
    Met en file le message `kind` pour l'adresse validée du compte. Retourne `queued`, `no_address` (rien n'est envoyé, ligne
    de log INFO) ou `not_configured`. N'attend pas l'envoi et ne lève pas.
    """
    from app.mailer import compose                       # import tardif : mailer dépend de app.auth
    address = user['email'] if user is not None else None
    if not address:
        logger.info("Notification « %s » : aucune adresse validée, rien n'est envoyé", kind)
        return 'no_address'
    mailer = current_app.extensions['mailer']
    if not mailer.has_settings:
        return 'not_configured'
    try:
        subject, body = compose(kind, **context)
        return mailer.enqueue(address, subject, body)
    except Exception:  # noqa: BLE001 - une notification ne casse jamais l'action
        logger.warning("Notification « %s » non mise en file", kind)
        return 'not_configured'
