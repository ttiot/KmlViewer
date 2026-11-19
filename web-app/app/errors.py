"""Gestion d'erreurs centralisée : réponses JSON homogènes pour l'API, sans détail interne."""

import logging

from flask import jsonify, request
from werkzeug.exceptions import HTTPException, InternalServerError

logger = logging.getLogger(__name__)

HTTP_MESSAGES = {
    400: 'Requête invalide',
    404: 'Ressource introuvable',
    405: 'Méthode non autorisée',
    413: 'Fichier trop volumineux',
    415: 'Corps JSON attendu',
}


class ApiError(Exception):
    """Erreur métier destinée au client : le message est affichable tel quel."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _is_api() -> bool:
    return request.path.startswith('/api/')


def init_errors(app) -> None:
    @app.errorhandler(ApiError)
    def api_error(exc):
        return jsonify({'success': False, 'error': exc.message}), exc.status

    @app.errorhandler(HTTPException)
    def http_error(exc):
        if not _is_api():
            return exc
        message = HTTP_MESSAGES.get(exc.code, exc.name)
        return jsonify({'success': False, 'error': message}), exc.code

    @app.errorhandler(Exception)
    def unexpected_error(exc):
        # Le détail part dans les logs, jamais dans la réponse
        logger.exception("Erreur non gérée sur %s %r", request.method, request.path)
        if _is_api():
            return jsonify({'success': False, 'error': 'Erreur interne du serveur'}), 500
        return InternalServerError()
