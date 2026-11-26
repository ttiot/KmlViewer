# Copie figée de l'index Geofabrik (tests du lot Z2)

`geofabrik-index-frozen.json` : « catalogue de test figé (contours et tailles) » de routing-zone-donnees (ZD-6, CA-ZD2, CA-ZD3).

- **Source** : `https://download.geofabrik.de/index-v1.json`, relevé le 2026-11-26 (3,5 Mo, 554 régions).
- **Réduction** : les 74 feuilles de `europe` dont le contour coupe la fenêtre lon −2…8,5 / lat 47…53 (France du Nord, Benelux,
  Allemagne de l'Ouest, Sud de l'Angleterre), plus leurs ancêtres (identifiant et parent seulement, sans contour). Les agrégats
  `alps`, `britain-and-ireland`, `dach`, `great-britain` y sont conservés : ils servent à vérifier l'exclusion (ZD-6).
- **Hors sous-arbre (E1, relevé par ops sur l'index réel)** : 64 entrées hors de `europe` y sont ajoutées, sans toucher aux 74 feuilles
  ci-dessus : 7 continents et `russia` (racines), `us` et ses **51 entrées `us/<état>`** (identifiants **avec barre oblique**, que
  la règle d'identifiant des régions d'Europe refuserait), `canada`, `canada/ontario`, `japan`, `russia/central-fed-district`. Contours : un
  carré de 1° ou plus, sans rapport avec la géographie réelle (seule la forme de l'index compte). Elles ne sont ni candidates ni
  contrôlées (hors du sous-arbre `europe`) : le catalogue doit être accepté avec elles, et rester à 70 régions.
- **Contours** : simplifiés à 0,01° (≈ 1 km), coordonnées à 4 décimales. Seuls `id`, `parent`, `urls.pbf` et `geometry` sont gardés.
- **`size`** : champ ajouté par l'hôte en production (ZD-10). Ici, le `Content-Length` des fichiers `*-latest.osm.pbf` relevé par
  des requêtes `HEAD` à la même date : ce sont les tailles réelles (zone par défaut : 17 régions, 3,28 Go).
- **Valeurs attendues** (tests) : la zone par défaut du lot Z1 se résout en 17 régions (voir `DEFAULT_REGIONS` dans
  `tests/routing_zone_support.py`), 4,06 % hors des données, emprise d'extraction `[1.2053, 48.74, 6.5647, 51.64]`, 24 dalles.

Ne pas la régénérer à la légère : un changement de contour ou de taille change les valeurs attendues. Si OSM ou Geofabrik évoluent,
la copie reste la référence de non-régression ; la mettre à jour est une décision de spec (liste des régions de ZD-6).
