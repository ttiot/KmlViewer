"""
Lecture validée des entrées des requêtes API. Une entrée refusée lève ApiError (400, 413) :
aucune route ne lit le corps JSON autrement.
"""

import logging
from typing import Any, Dict, Optional

from flask import current_app, request
from werkzeug.exceptions import RequestEntityTooLarge

from app.errors import ApiError
from app.security import SQLITE_MAX_INT
from app.services import activity_types

logger = logging.getLogger(__name__)

# Taille maximale d'un corps JSON. Désérialisé, il occupe plusieurs dizaines de fois sa
# taille : seules les routes qui reçoivent une trace entière demandent large=True.
JSON_BODY_LIMIT = 2 * 1024 * 1024


def json_body(large: bool = False) -> Dict[str, Any]:
    """
    Corps JSON de la requête, obligatoirement un objet.
    large : accepte jusqu'à MAX_CONTENT_LENGTH (analyse et export d'une trace complète).
    """
    limit = current_app.config['MAX_CONTENT_LENGTH'] if large else JSON_BODY_LIMIT
    if (request.content_length or 0) > limit:
        raise ApiError('Requête trop volumineuse', 413)
    request.max_content_length = limit  # corps sans Content-Length : la lecture s'arrête à la limite
    try:
        data = request.get_json(silent=True)
    except RequestEntityTooLarge as exc:
        raise ApiError('Requête trop volumineuse', 413) from exc
    except RecursionError as exc:
        # Des milliers de niveaux d'imbrication : rien de légitime n'y ressemble
        logger.warning("Corps JSON trop profondément imbriqué sur %r", request.path)
        raise ApiError('Corps JSON invalide') from exc
    if not isinstance(data, dict):
        raise ApiError('Corps JSON attendu')
    return data


def json_list(data: Dict[str, Any], key: str, label: str, min_items: int = 0) -> list:
    """Champ liste obligatoire, avec un nombre minimal d'éléments."""
    value = data.get(key)
    if not isinstance(value, list):
        raise ApiError(f'{label} manquants')
    if len(value) < min_items:
        raise ApiError(f'Au moins {min_items} {label.lower()} sont nécessaires')
    return value


def json_int(data: Dict[str, Any], key: str) -> int:
    """Champ entier obligatoire, positif et stockable par SQLite (un booléen n'est pas un entier)."""
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= SQLITE_MAX_INT:
        raise ApiError(f'{key} doit être un entier positif')
    return value


def json_name(data: Dict[str, Any], max_length: int, error: str) -> str:
    """Champ 'name' : texte non vide une fois les espaces retirés, de longueur bornée."""
    value = data.get('name')
    name = value.strip() if isinstance(value, str) else ''
    if not 1 <= len(name) <= max_length:
        raise ApiError(error)
    return name


def int_arg(name: str, default: int, maximum: int = SQLITE_MAX_INT) -> int:
    """Paramètre d'URL entier, ramené entre 0 et `maximum` ; `default` s'il est absent ou illisible."""
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return max(0, min(value, maximum))


def type_filter() -> Optional[str]:
    """Paramètre d'URL `type` : une clé du catalogue des types, « unclassified », ou rien."""
    value = request.args.get('type') or None
    if value is not None and not activity_types.is_filter(value):
        raise ApiError("Type d'activité inconnu")
    return value
