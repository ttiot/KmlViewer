"""Corps JSON des routes PUBLIQUES (avant toute authentification) : bornés, profondeur comprise, jamais une erreur 500."""

from flask import request
from werkzeug.exceptions import RequestEntityTooLarge

MAX_BODY = 8192
MAX_FIELD = 1024
MAX_DEPTH = 4                      # un formulaire n'a pas de structure : on ne tolère qu'un peu d'imbrication


def small_json(limit: int = MAX_BODY):
    """
    Un objet JSON PEU imbriqué de `limit` octets au plus, sinon None. Borne aussi un corps SANS Content-Length (transfert
    fragmenté : la lecture s'arrête à la limite, T10-C) ; un JSON profondément imbriqué (RecursionError) ou illisible est
    refusé (T10-A).
    """
    if (request.content_length or 0) > limit:
        return None
    request.max_content_length = limit
    try:
        data = request.get_json(silent=True)
    except (RequestEntityTooLarge, RecursionError, ValueError):
        return None
    if not isinstance(data, dict) or _too_deep(data):
        return None
    return data


def _too_deep(value, limit: int = MAX_DEPTH) -> bool:
    """Imbrication au-delà de `limit` niveaux (itératif : aucun risque de récursion)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, (dict, list)):
            if depth > limit:
                return True
            stack.extend((child, depth + 1) for child in (item.values() if isinstance(item, dict) else item))
    return False


def clean_text(value, max_length: int = MAX_FIELD):
    """Chaîne non vide d'au plus `max_length` caractères ENCODABLE en UTF-8 (un surrogat isolé n'est pas du texte, T10-B)."""
    if not isinstance(value, str) or not 0 < len(value) <= max_length:
        return None
    try:
        value.encode('utf-8')
    except UnicodeEncodeError:
        return None
    return value
