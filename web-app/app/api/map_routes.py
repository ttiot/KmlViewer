"""Configuration de la carte pour le navigateur (R1) : fonds de carte AUTORISÉS seulement, fond par défaut, cadrage initial."""

from flask import current_app, jsonify

from app import outbound
from app.api import bp
from app.db import get_db
from app.settings import effective


def allowed_basemaps(cfg, *, audit: bool = True) -> list:
    """
    Les fonds sont filtrés à l'USAGE : un fond dont un hôte est hors de `tiles=` (valeur forcée en base) n'est pas envoyé au
    navigateur, et le refus est audité (dédoublonné) si `audit`. Aucun navigateur ne contacte un hôte non autorisé.
    """
    allowed_hosts = outbound.allowlist_of(cfg).tile_hosts()
    basemaps = []
    for basemap in effective(cfg, 'map.basemaps'):
        try:
            hosts = outbound.tile_hosts_of_template(basemap['url'], basemap['subdomains'])
        except outbound.OutboundError:
            continue
        refused = sorted(hosts - allowed_hosts)
        if refused:
            if audit:
                outbound.refuse(get_db(), 'tiles', refused[0])
            continue
        basemaps.append(basemap)
    return basemaps


def default_basemap_alert(cfg):
    """T16-n3 : le fond par défaut configuré n'est pas (ou plus) servi aux utilisateurs ; `None` si tout va bien."""
    names = {b['name'] for b in allowed_basemaps(cfg, audit=False)}
    default = effective(cfg, 'map.default_basemap')
    if not names or default in names:
        return None
    return {'code': 'default_basemap_unavailable', 'message':
            f"Le fond par défaut « {default} » est indisponible (retiré de la liste ou hôte hors de tiles=) : "
            f"un autre fond est utilisé à la place."}


@bp.route('/map-config')
def map_config():
    """Fonds autorisés seulement, fond par défaut (repli sur le premier fond servi), cadrage initial."""
    cfg = current_app.config
    basemaps = allowed_basemaps(cfg)
    default = effective(cfg, 'map.default_basemap')
    if basemaps and default not in {b['name'] for b in basemaps}:
        default = basemaps[0]['name']
    return jsonify({'basemaps': basemaps, 'default': default if basemaps else None,
                    'center': [effective(cfg, 'map.center_lat'), effective(cfg, 'map.center_lon')],
                    'zoom': effective(cfg, 'map.zoom')})
