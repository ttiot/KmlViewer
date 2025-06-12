import hashlib
from typing import Dict, Any, Tuple
from .kml_parser import KMLParser
from .gpx_parser import GPXParser

# Simple in-memory cache keyed by a tuple (type, hash, display_mode)
_parse_cache: Dict[Tuple[str, str, str], Dict[str, Any]] = {}


def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def parse_kml_cached(content: str, display_mode: str = "double") -> Dict[str, Any]:
    """Parse KML content using a cache to avoid duplicate work."""
    key = ("kml", _hash_content(content), display_mode)
    if key not in _parse_cache:
        _parse_cache[key] = KMLParser.parse_kml_coordinates(content, display_mode)
    return _parse_cache[key]


def parse_gpx_cached(content: str) -> Dict[str, Any]:
    """Parse GPX content using a cache to avoid duplicate work."""
    key = ("gpx", _hash_content(content), "")
    if key not in _parse_cache:
        _parse_cache[key] = GPXParser.parse_gpx_coordinates(content)
    return _parse_cache[key]


def clear_cache() -> None:
    """Clear the parsing cache (mainly for tests)."""
    _parse_cache.clear()
