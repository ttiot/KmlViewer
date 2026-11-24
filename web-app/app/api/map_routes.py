"""Configuration de la carte pour le navigateur (R1) : fonds de carte AUTORISÉS seulement, fond par défaut, cadrage initial."""

from flask import current_app, jsonify

from app import outbound
from app.api import bp
from app.db import get_db
from app.settings import effective


@bp.route('/map-config')
def map_config():
    """
    Les fonds sont filtrés ici, à l'USAGE : un fond dont un hôte est hors de `tiles=` (valeur forcée en base) n'est pas
    envoyé au navigateur, et le refus est audité (dédoublonné). Aucun navigateur ne contacte un hôte non autorisé.
    """
    cfg = current_app.config
    allowed_hosts = outbound.allowlist_of(cfg).tile_hosts()
    basemaps = []
    for basemap in effective(cfg, 'map.basemaps'):
        try:
            hosts = outbound.tile_hosts_of_template(basemap['url'], basemap['subdomains'])
        except outbound.OutboundError:
            continue
        refused = sorted(hosts - allowed_hosts)
        if refused:
            outbound.refuse(get_db(), 'tiles', refused[0])
            continue
        basemaps.append(basemap)
    default = effective(cfg, 'map.default_basemap')
    if basemaps and default not in {b['name'] for b in basemaps}:
        default = basemaps[0]['name']
    return jsonify({'basemaps': basemaps, 'default': default if basemaps else None,
                    'center': [effective(cfg, 'map.center_lat'), effective(cfg, 'map.center_lon')],
                    'zoom': effective(cfg, 'map.zoom')})
