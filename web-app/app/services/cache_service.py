"""Cache mémoire des fichiers parsés, pour ne pas reparser un fichier renvoyé à l'identique."""

import hashlib
import threading
from collections import OrderedDict
from typing import Any, Dict, Tuple

from .gpx_parser import GPXParser
from .kml_parser import KMLParser

# Cache LRU borné en nombre d'entrées ET en volume. Un résultat parsé pèse de l'ordre de
# quinze fois son fichier : borné seulement en nombre, seize gros fichiers distincts
# retiendraient plusieurs Go (déni de service par épuisement mémoire). Le volume est compté
# en taille de fichier source ; un fichier trop gros n'est simplement pas mis en cache.
MAX_ENTRIES = 16
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
_parse_cache: "OrderedDict[Tuple[str, str, str], Dict[str, Any]]" = OrderedDict()
_weights: Dict[Tuple[str, str, str], int] = {}


_lock = threading.Lock()


def _get_or_compute(key, weight: int, compute):
    with _lock:
        if key in _parse_cache:
            _parse_cache.move_to_end(key)
            return _parse_cache[key]
    value = compute()  # hors verrou : le parsing peut être long
    if weight > MAX_ENTRY_BYTES:
        return value
    with _lock:
        if key in _parse_cache:
            return _parse_cache[key]
        _parse_cache[key] = value
        _weights[key] = weight
        while len(_parse_cache) > MAX_ENTRIES or sum(_weights.values()) > MAX_TOTAL_BYTES:
            evicted, _ = _parse_cache.popitem(last=False)
            _weights.pop(evicted, None)
    return value


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def parse_kml_cached(content: str, display_mode: str = "double") -> Dict[str, Any]:
    """Parse KML content using a cache to avoid duplicate work."""
    key = ("kml", _hash_content(content), "")  # le résultat ne dépend plus du mode d'affichage
    return _get_or_compute(key, len(content), lambda: KMLParser.parse_kml_coordinates(content, display_mode))


def parse_gpx_cached(content: str) -> Dict[str, Any]:
    """Parse GPX content using a cache to avoid duplicate work."""
    key = ("gpx", _hash_content(content), "")
    return _get_or_compute(key, len(content), lambda: GPXParser.parse_gpx_coordinates(content))


def clear_cache() -> None:
    """Clear the parsing cache (mainly for tests)."""
    with _lock:
        _parse_cache.clear()
        _weights.clear()
