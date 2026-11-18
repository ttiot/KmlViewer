"""Lecture validée du corps JSON des requêtes API."""

from typing import Any, Dict

from flask import request

from app.errors import ApiError


def json_body() -> Dict[str, Any]:
    data = request.get_json(silent=True)
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
