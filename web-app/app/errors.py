"""Gestion d'erreurs centralisée : réponses JSON homogènes pour l'API, sans détail interne."""

import logging

from flask import jsonify, request
from werkzeug.exceptions import HTTPException, InternalServerError

logger = logging.getLogger(__name__)

HTTP_MESSAGES = {
    400: 'Requête invalide',
    401: 'Authentification requise',
    403: 'Accès refusé',
    404: 'Ressource introuvable',
    405: 'Méthode non autorisée',
    413: 'Requête trop volumineuse',
    415: 'Corps JSON attendu',
}


class ApiError(Exception):
    """
    Erreur métier destinée au client : le message est affichable tel quel. C'est la seule
    façon pour une route API de répondre une erreur (400 par défaut, 404, 409, 413, 503…).
    """

    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra          # champs JSON ajoutés à la réponse (ex. `activity_id` d'un 409), jamais de donnée interne
        self.retry_after = None     # secondes : en-tête Retry-After d'un 429


def is_api_request() -> bool:
    """Les routes API répondent en JSON, les pages en HTML."""
    return request.path.startswith('/api/')


def json_error(message: str, status: int):
    """Réponse d'erreur de l'API : le format unique lu par le front (`success`, `error`)."""
    return jsonify({'success': False, 'error': message}), status


def init_errors(app) -> None:
    @app.errorhandler(ApiError)
    def api_error(exc):
        body, status = json_error(exc.message, exc.status)
        if getattr(exc, 'extra', None):
            payload = body.get_json()
            payload.update(exc.extra)
            body = jsonify(payload)
        if getattr(exc, 'retry_after', None):
            body.headers['Retry-After'] = str(int(exc.retry_after))
        return body, status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        if not is_api_request():
            return exc
        return json_error(HTTP_MESSAGES.get(exc.code, exc.name), exc.code)

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        # Le détail part dans les logs, jamais dans la réponse
        logger.exception("Erreur non gérée sur %s %r", request.method, request.path)
        if is_api_request():
            return json_error('Erreur interne du serveur', 500)
        return InternalServerError()
