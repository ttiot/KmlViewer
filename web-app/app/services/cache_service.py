import hashlib
import threading
from collections import OrderedDict
from typing import Dict, Any, Tuple
from .kml_parser import KMLParser
from .gpx_parser import GPXParser

# Cache mémoire LRU borné, indexé par (type, hash). Sans borne, chaque
# upload distinct resterait en RAM indéfiniment (déni de service par épuisement mémoire).
MAX_ENTRIES = 16
_parse_cache: "OrderedDict[Tuple[str, str, str], Dict[str, Any]]" = OrderedDict()


_lock = threading.Lock()


def _get_or_compute(key, compute):
    with _lock:
        if key in _parse_cache:
            _parse_cache.move_to_end(key)
            return _parse_cache[key]
    value = compute()  # hors verrou : le parsing peut être long
    with _lock:
        value = _parse_cache.setdefault(key, value)
        while len(_parse_cache) > MAX_ENTRIES:
            _parse_cache.popitem(last=False)
    return value


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def parse_kml_cached(content: str, display_mode: str = "double") -> Dict[str, Any]:
    """Parse KML content using a cache to avoid duplicate work."""
    key = ("kml", _hash_content(content), "")  # le résultat ne dépend plus du mode d'affichage
    return _get_or_compute(key, lambda: KMLParser.parse_kml_coordinates(content, display_mode))


def parse_gpx_cached(content: str) -> Dict[str, Any]:
    """Parse GPX content using a cache to avoid duplicate work."""
    key = ("gpx", _hash_content(content), "")
    return _get_or_compute(key, lambda: GPXParser.parse_gpx_coordinates(content))


def clear_cache() -> None:
    """Clear the parsing cache (mainly for tests)."""
    _parse_cache.clear()
