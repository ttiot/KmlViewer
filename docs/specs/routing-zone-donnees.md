# Spec — Le polygone de zone, source unique de la couverture (lot Z2)

Statut : **v2 validée par l'utilisateur** (2026-11-26, décisions relayées par coord). Dimensionnement mémoire **provisoire, pour dune**, vérifié au premier build réel (§9, V-Z2).
- La v1 a été validée sous réserve de la relecture de review. La relecture (`REVIEW.md`, « Specs Z1 (zone unique) et proposition Z2 ») a relevé **1 bloquant, 4 majeurs, 5 mineurs**. Cette v2 les reprend (§10).
- **Acquis** :
  - le **principe** : le polygone `routing.coverage_polygon` est la **source unique** de tout ce qui dépend de la couverture (extraits OSM et graphe, points acceptés, dalles MNT), avec **une seule tâche de fond, un seul statut, une seule zone active** ;
  - **Q-A = option (1)** : les dalles suivent la zone et ne sont jamais purgées sous le plafond. La v2 précise ce qui peut être purgé (ZD-8) ;
  - **D = D2** : la zone par défaut reste le rectangle du lot Z1 ; les plafonds sont relevés (ZD-4) ;
  - **m1 = G1** : calcul géométrique exact par `shapely` (ZD-6, ADR-0013) ;
  - budget de 6 tâches et 12 Gio par jour, délai de stabilité de 300 s, 10 % de la zone au plus hors des données ;
  - **RAM doublée** plutôt que mesurée d'abord : import `-Xmx12g` dans 14 Gio, service `-Xmx3g` dans 4 Gio, `ROUTING_MAX_IMPORT_BYTES` 2,4 Go (ZD-4). Valeurs **dimensionnées pour dune** ; la mesure devient une vérification au premier build réel, pas un préalable à TZ2-c.
- Jusqu'à la livraison de Z2, [gpx-route-builder](gpx-route-builder.md) RB-1.3b décrit le code en service.

Décision d'architecture : [ADR-0013](../adr/0013-zone-pilote-donnees-moteur.md) (**accepté**). Dépend du lot Z1 (admin-space EF-6.20).

## 1. Objet

Enregistrer un nouveau polygone lance **une** tâche de fond qui met la couverture en accord avec la zone : choix des extraits Geofabrik, téléchargement (avec cache), fusion, extraction recadrée, import du graphe GraphHopper, dalles MNT, bascule.

Tant que la tâche n'est pas finie, la **zone demandée** (le réglage) diffère de la **zone active** (celle que couvrent le graphe et les dalles en service). **L'acceptation des points suit la zone active.**

## 2. Vocabulaire

- **Zone demandée** : valeur effective de `routing.coverage_polygon`.
- **Zone active** : zone que couvrent le graphe en service et les dalles MNT. Elle est tenue en base et écrite seulement sur rapport de l'hôte. Un graphe d'avant Z2, construit sans `zone.json`, a une zone active dite « sources historiques ».
- **Emprise d'extraction** : emprise de la zone élargie de la marge (ZD-5). Elle sert à l'extraction OSM et aux dalles MNT. Elle ne sert **pas** au choix des régions (ZD-6).
- **Catalogue** : sous-arbre de l'index Geofabrik. Pour chaque région : identifiant, parent, URL `.pbf`, contour, taille en octets.
- **Hôte** : `routing/update.sh` et l'unité systemd qui le lance. Seul acteur qui touche à Docker et au réseau de téléchargement.

## 3. Constats sur l'index réel (2026-11-26)

Index `https://download.geofabrik.de/index-v1.json` (3,5 Mo), relevé par spec. Calcul par un prototype jetable, hors dépôt : pur Python, points d'échantillonnage tous les 5 km, chaque point attribué à la plus petite région qui le contient.

- **222 feuilles** sous `europe`. Parmi elles, des **extraits agrégés** qui recouvrent d'autres feuilles : `alps`, `britain-and-ireland`, `dach`, `great-britain`. Quatre feuilles d'outre-mer sont aussi rangées sous `france`. Sans exclusion explicite, les agrégats gagnent les points en mer : `great-britain` est retenu pour la Manche.
- **`*-latest.osm.pbf` répond par une redirection 307** vers un fichier daté, sur le même hôte (exemple : `…/belgium-261124.osm.pbf`). La taille ne se lit qu'en suivant la redirection.
- **Situation actuelle** : `belgium` 699,9 Mo, `nord-pas-de-calais` 242,0 Mo, `picardie` 133,7 Mo. Total **1,08 Go**, 3 régions.
- **Zone par défaut du lot Z1** (le rectangle `[1.35, 48.83, 6.42, 51.55]`), résolue sur le **polygone** : **17 régions, 3,28 Go**.

  | Régions | Part des points de la zone |
  |---|---|
  | belgium | 28,5 % |
  | picardie | 17,7 % |
  | nord-pas-de-calais | 13,2 % |
  | champagne-ardenne | 9,8 % |
  | lorraine | 7,4 % |
  | ile-de-france | 4,2 % |
  | haute-normandie | 3,0 % |
  | luxembourg | 2,3 % |
  | noord-brabant, limburg, zeeland | ≈ 2 % chacune |
  | koeln-regbez, rheinland-pfalz, duesseldorf-regbez, kent | ≤ 1,7 % |
  | centre, saarland | ≤ 0,1 % |

  4,0 % de la zone est hors de tout contour (mer). Le rectangle n'a jamais été « Hauts-de-France + Belgique » : c'est le rectangle annoncé par le moteur (RB-1.3). Avec la résolution par emprise de la v1, le résultat serait encore plus large : c'est le défaut Z2-B1.
- **Fichier recadré estimé** pour la zone par défaut : **≈ 1,9 Go** (prorata de la surface de chaque région dans l'emprise d'extraction `[1.205, 48.74, 6.565, 51.64]` : Belgique, Picardie, Nord-Pas-de-Calais et Luxembourg entiers, Île-de-France et Champagne-Ardenne à ≈ 45 %, régions allemandes et néerlandaises en partie). C'est une **estimation** : la densité de données n'est pas uniforme (Paris, Aix-la-Chapelle). La vérification V-Z2 (§9) fait foi.
- **Exemple Normandie** : la zone par défaut plus un lobe vers Caen (polygone de CA-ZD10 b) donne 17 à 18 régions (`basse-normandie` entre ; `saarland`, à 0,05 %, sort ou reste selon la méthode), ≈ 3,4 Go d'extraits, ≈ 2,05 Go recadrés (estimation), emprise `[-0.645, 48.74, 6.565, 51.64]`, 32 dalles (8 de plus).
- **Temps de calcul** du prototype pur Python : 4 436 points × 214 feuilles en **1,4 à 2,8 s**, non optimisé. Ce temps a conduit au choix d'un calcul exact par bibliothèque (décision m1 = G1).
- **Sensibilité de l'échantillonnage** : les deux zones ci-dessus ne diffèrent qu'à l'ouest, mais `saarland` (0,05 %) est retenu dans l'une et pas dans l'autre, selon l'alignement de la grille. Le calcul exact (ZD-6) n'a pas ce défaut.

## 4. Inventaire de ce qui dépend de la couverture

| # | Élément | Aujourd'hui | Après Z2 | Qui |
|---|---|---|---|---|
| 1 | Points acceptés : contrôle 400 avant le moteur | zone demandée (= active, Z1) | **zone active** (ZD-3) | dev |
| 2 | `GET …/options` : `coverage_polygon`, `coverage_bbox` | zone en vigueur | zone active ; `state: "starting"` pendant la bascule | dev |
| 3 | Front du constructeur : tracé, refus local, RB-2.7 | `options` | inchangé (lit `options`) | — |
| 4 | Rabattement des points techniques (RB-S7, RB-1.3b) | zone en vigueur | zone active | dev |
| 5 | Points en cours dans le navigateur (`localStorage`, RB-2.6) | — | un point hors de la nouvelle zone active est refusé en 400 au calcul ; le front le signale déjà | test (à vérifier) |
| 6 | Résultats en mémoire, « Autre proposition », régénération RB-2.7 | — | une régénération après une bascule repasse le contrôle ; un `save` d'un résultat déjà calculé reste permis | — |
| 7 | Traces planifiées enregistrées | — | non concernées (GPX stockés) | — |
| 8 | **Extraits OSM** | `ROUTING_EXTRACT_URLS` fixe | **résolus depuis le polygone** dans le catalogue (ZD-6) ; variable retirée | ops, dev |
| 9 | Fichier fusionné `hdf-be-*.osm.pbf` ; défaut `GH_PBF` du compose | nom figé sur la région | `sources-AAAAMMJJ.osm.pbf` (jour des extraits, utilisé par la règle « même jour », ZD-9) | ops |
| 10 | **Graphe** GraphHopper | extraits complets | emprise d'extraction ; `zone.json` dans son dossier | ops |
| 11 | **Mise à jour mensuelle** | tâche distincte | **même tâche, même cycle** (`claim --monthly`, `switching`, `starting`, `done`), motif `mensuel` (ZD-3) | ops, dev |
| 12 | **Dalles MNT** (`DEM_BBOX`, `dem-tiles`) | env fixe, lue au démarrage du profil `dem` | dérivées de l'emprise d'extraction ; `DEM_BBOX` retiré ; seules les dalles **téléchargées par la tâche** sont purgeables (ZD-8) | ops, dev |
| 13 | Câblage compose du profil `dem` | `opentopodata` attend la fin sans erreur de `dem-tiles`, qui sort en code 2 sans `DEM_BBOX` | `dem-tiles` dans un profil d'outillage lancé par l'hôte ; `opentopodata` ne dépend plus de lui | ops |
| 14 | Prise en compte des nouvelles dalles par `opentopodata` | — | **à vérifier** : sans redémarrage, ou redémarrage à la bascule | ops |
| 15 | Activités `dem_status = 'no_coverage'` | jamais retentées | remises en file après une bascule qui ajoute des dalles (ZD-10, réveil par marqueur en base) | dev |
| 16 | **MNT des activités importées** (altitude-mnt) | `DEM_BBOX` choisie par l'exploitant | zone plus dalles déjà présentes, jamais purgées (Q-A, option 1) | — |
| 17 | Diagnostic `coverage_contained` (P-TR5-9, `admin.js`) | « zone dans `/info` ? » | **contrôle de cohérence** : `/info` doit contenir l'emprise de la zone active, sinon alerte « graphe et zone active divergent » | dev |
| 18 | Sondage `routing_health` | toutes les 60 s | inchangé ; en `starting`, le statut de zone prime (503 annoncé) | — |
| 19 | Fonds de carte, centre et zoom (`map.*`) | réglages de carte | non dépendants | — |
| 20 | `OUTBOUND_ALLOWED_HOSTS` de l'application | — | inchangé : l'application ne contacte ni Geofabrik ni le bucket MNT | — |
| 21 | `ROUTING_ALLOWED_HOSTS`, sortie réseau de l'hôte | `download.geofabrik.de` | idem, redirections recontrôlées (ZD-9) ; bucket Copernicus pour `dem-tiles` (existant) | ops |
| 22 | Caches et disque | ~2,4 Go routage, dalles selon `DEM_BBOX` | cache des extraits borné (ZD-9), graphes inchangés, dalles de la tâche bornées (ZD-8) | ops |
| 23 | Borne de taille de la zone | 2 000 km de côté, EF-6.20 | inchangée (règle dans EF-6.20 seulement) ; **plafonds de ressources** en plus (ZD-4) | dev |
| 24 | Page et diagnostic de l'admin | réglage seul | panneau « Couverture en service » (ZD-12) | dev |
| 25 | Documentation (README, `docs/exploitation.md`, `docs/bibliotheque.md`, `.env.example`, compose) | variables fixes | à réécrire ; règle de dimensionnement de la RAM d'import (ZD-4) | doc, ops |
| 26 | Tests du diagnostic (`test_route_builder_api*.py`, `test_zone_edge.py`, `test_lot2_g3b_edge.py`), banc | — | à adapter | test |
| 27 | Recette (`recette.md`, étape 0 et profil `dem`) | extraits fixes | à réécrire avec la tâche unique | spec, après validation |
| 28 | **Zone par défaut** (Z1 : rectangle) et installation neuve | 3 régions, 1,08 Go | rectangle gardé (**D2**) : **17 régions, 3,28 Go**, ≈ 1,9 Go recadrés (estimation) ; plafonds relevés (ZD-4) | — |
| 29 | Commande `flask routing-zone` lancée chaque minute | — | ni ordonnanceur ni fils de fond (déjà garanti par `_start_scheduler` hors service, à tester) | dev, test |
| 30 | **RAM d'import** (`ROUTING_IMPORT_XMX`, `ROUTING_IMPORT_MEM_LIMIT`) | `-Xmx6g`, 8 Go, pour 1,08 Go | **`-Xmx12g` dans 14 Gio** (provisoire, dune), vérifié par V-Z2 (§9) | ops |
| 31 | **RAM du service** (`ROUTING_XMX`, `ROUTING_MEM_LIMIT`) | `-Xmx1536m` dans 2 Go, graphe de 320 Mo | graphe ≈ 1,8 fois plus gros (estimation) ; **`-Xmx3g` dans 4 Gio** (provisoire, dune), vérifié par V-Z2 | ops |
| 32 | **Disque** du routage (extraits, fusionné, recadré, graphes, dalles) | ~2,4 Go en régime, pic ~4 Go | ≈ 3 fois plus d'extraits ; pic relevé par V-Z2, `ROUTING_CACHE_MAX_BYTES` à revoir alors | ops |
| 33 | **Dépendance** `shapely` de l'application (G1) | absente | `shapely==2.2.0` (GEOS 3.14.1 embarqué), épinglée avec empreintes, roue seulement ; feu vert de review (ADR-0013, point 3) | dev |

## 5. Exigences

### Activation

- **ZD-1** Variable d'env **`ROUTING_ZONE_BUILD`** : `0` par défaut, `1` pour activer. Elle est lue par l'application et par l'hôte.
  - À `0` : comportement du lot Z1. `DEM_BBOX` et `ROUTING_EXTRACT_URLS` sont lus comme aujourd'hui ; table d'état non utilisée ; aucun panneau ; `flask routing-zone` sort avec le code 4.
  - À `1` : ces deux variables sont **ignorées**, avec un `WARNING` si elles restent définies. **Au premier démarrage avec `1`**, si la zone active est « sources historiques » ou absente, l'application crée une demande pour la zone demandée, motif `activation`, statut `pending`. Une installation neuve et une instance existante suivent ainsi le même chemin (revue Z2-M4 a). Si cette demande dépasse les plafonds : statut `failed`, code `cap`, alerte « La zone dépasse les plafonds : réduis-la ou relève les plafonds (exploitation) ».

### Zone active et statut unique

- **ZD-2** Table d'état à une ligne (migration nommée `0018_routing_coverage_state`, ADR-0012). Elle contient :
  - la zone active et son emprise d'extraction ;
  - les régions actives et le graphe en service ;
  - la date des données OSM ;
  - les dalles téléchargées par la tâche ;
  - le statut, son motif (`zone`, `mensuel` ou `activation`) et sa date ;
  - la demande en cours et sa date de prise ;
  - le code du dernier échec ;
  - la date du catalogue ;
  - le marqueur de réveil du MNT ;
  - les compteurs du budget du jour, rapportés par l'hôte.
- **ZD-3** **Un seul statut** pour toute la couverture :

  | Statut | Sens | Entrée |
  |---|---|---|
  | `idle` | zone demandée = zone active (listes de sommets égales) | `done` ; enregistrement d'une zone égale à l'active ; **recalcul** (ci-dessous) |
  | `pending` | zone demandée ≠ zone active, aucune tâche en cours | enregistrement d'une autre zone ; « Réessayer » ; prise expirée ; **recalcul** ; activation (ZD-1) |
  | `building` | une demande est prise (`claim`, y compris mensuelle) | `claim` |
  | `starting` | graphe basculé, moteur (et service d'altitude si besoin) en redémarrage | `switching` accepté |
  | `failed` | la **dernière** demande a échoué ; tout l'actif est inchangé | `failed` d'une demande **non dépassée** |

  - **Recalcul** (revue Z2-M4 c) : après `switching` en code 3, après `failed` d'une demande **dépassée** et après `done`, le statut devient `idle` si la zone demandée égale l'active, sinon `pending`. Un échec d'une demande dépassée ne bloque jamais la plus récente.
  - L'acceptation des points (inventaire n° 1 à 4) lit la **zone active**. Avec « sources historiques » (avant la première bascule), elle lit la zone demandée, comme au lot Z1.
  - En `starting`, quel que soit le motif (mensuel compris) : `options` renvoie `available: false` et `state: "starting"` ; `route` répond **503** « Service en cours de redémarrage : réessaie dans une minute. », sans appel au moteur.
  - Un enregistrement pendant `building` ou `starting` ne change pas le statut. La demande en cours devient **dépassée**.

### Écriture d'une zone : couverture et plafonds

- **ZD-4** Avec `ROUTING_ZONE_BUILD=1`, contrôles en plus d'EF-6.20 (la borne de 2 000 km de côté y reste écrite, nulle part ailleurs), dans cet ordre :
  1. **catalogue connu**, sinon 400 « Zone : catalogue des données pas encore chargé, réessaie dans quelques minutes. » ;
  2. **couverture du polygone** (ZD-6) : la part des points du polygone hors de toute région candidate est au plus `ROUTING_MAX_UNCOVERED_PCT` (env, 0 à 50, défaut **10**). Sinon 400 « Zone : 23 % de la zone hors des données OSM (mer ou hors d'Europe). ». La zone par défaut est à 4,0 % (§3) ;
  3. **plafonds**, tous en env :
     - `ROUTING_MAX_REGIONS` (1 à 50, défaut **20**) : nombre de régions retenues ;
     - `ROUTING_MAX_SOURCE_BYTES` (défaut **4 Gio**, soit 4 294 967 296 octets) : somme des tailles du catalogue. Ce plafond porte sur le **réseau et le disque** ;
     - `DEM_MAX_TILES` (1 à 1 000, défaut **60**) : nombre de dalles de l'emprise d'extraction.
     
     Messages chiffrés, par exemple « Zone trop grande : 4,6 Go d'extraits (23 régions : belgium, picardie, …) pour 4 Gio au plus. ».
     
     Avec ces défauts (décision D2), la zone par défaut (17 régions, 3,28 Go, 24 dalles) et l'exemple Normandie (§3 : 17 à 18 régions, ≈ 3,4 Go, 32 dalles) passent. Marge : ≈ 30 % d'octets, pour la croissance d'OSM et de petites extensions.
     
     Un quatrième plafond, `ROUTING_MAX_IMPORT_BYTES`, porte sur la taille du fichier **recadré** envoyé à l'import, seule grandeur liée à la **RAM** (revue Z2-m3). Ce plafond ne se connaît qu'après l'extraction : il est contrôlé par l'**hôte** (ZD-9, étape 6). **Valeurs par défaut, provisoires et dimensionnées pour dune** (64 Go, 51 Go disponibles ; décision de l'utilisateur : doubler la RAM plutôt que mesurer d'abord) :
     - `ROUTING_IMPORT_XMX` = **12g** et `ROUTING_IMPORT_MEM_LIMIT` = **14g** (au lieu de 6g et 8g). Le pic de 6 244 Mio relevé à l'ADR-0007 était plafonné par `-Xmx6g` : il ne mesure pas le besoin réel ;
     - `ROUTING_MAX_IMPORT_BYTES` = **2,4 Go** (2 400 000 000 octets), recalé sur ce tas par la règle ci-dessous : tas utilisé ≤ 12 Go / 1,5 = 8 Go ; repère de l'ADR-0007, 3,6 Go de tas utilisé pour 1,08 Go ; d'où 8 × 1,08 / 3,6 ≈ 2,4 Go. La zone par défaut (≈ 1,9 Go recadrés, estimation) et l'exemple Normandie (≈ 2,05 Go) passent ;
     - pendant une tâche, import (14 Gio) et service (4 Gio, inventaire n° 31) tournent ensemble avec l'application : ≈ 20 Gio, dans les 51 Go disponibles de dune.
     
     **Règle de dimensionnement**, pour une autre machine ou après la vérification V-Z2 (§9) :
     - `ROUTING_IMPORT_XMX` ≥ tas utilisé pour un fichier de `ROUTING_MAX_IMPORT_BYTES` × 1,5 ;
     - `ROUTING_IMPORT_MEM_LIMIT` ≥ `ROUTING_IMPORT_XMX` + 2 Go, et le pic du conteneur y tient sans mise à mort par le noyau ;
     - relever `ROUTING_MAX_IMPORT_BYTES` suppose de relever `ROUTING_IMPORT_XMX` et `ROUTING_IMPORT_MEM_LIMIT` dans la même proportion, à vérifier sur la machine.
     
     Ces valeurs **ne valent que pour dune** : une instance sur une machine plus petite les règle dans son `.env` (doc, `.env.example`).
  4. **confirmation** (EF-6.13) enrichie : régions ajoutées et retirées, octets à télécharger hors cache, dalles nouvelles, et « La zone actuelle reste en service pendant la reconstruction (quelques minutes, au plus tôt dans `ROUTING_ZONE_SETTLE_S`), suivie d'un court redémarrage. ».

  En cas d'échec : rien n'est écrit, version inchangée. Les contrôles de l'application sont **anticipés** ; **l'hôte fait foi** et recontrôle avec son propre environnement. Si les deux environnements divergent, le refus de l'hôte donne `failed --code cap`.

### Calculs de l'application

- **ZD-5** **Emprise d'extraction** : emprise de la zone `[lon_min, lat_min, lon_max, lat_max]`, marge `m` km (`routing.zone_margin_km`, entier de 0 à 50, défaut 10), R = 6 371 km.
  - Δlat = m / 111,1949 ; lat_min − Δlat et lat_max + Δlat, bornées à ±85,0511.
  - φ = la plus grande valeur absolue des deux latitudes ; Δlon = m / (111,1949 · cos φ) ; lon_min − Δlon et lon_max + Δlon, bornées à ±180.
  - Arrondi vers l'extérieur à 4 décimales.
  - Exemple : zone par défaut Z1, marge 10 km ⇒ `[1.205, 48.74, 6.565, 51.64]`, 24 dalles.
- **ZD-6** **Résolution des régions, sur le polygone** (revue Z2-B1) :
  - **candidates** : les feuilles du sous-arbre `ROUTING_CATALOG_ROOT` (défaut `europe`), **moins** les agrégats et l'outre-mer listés dans `ROUTING_CATALOG_EXCLUDE`. Défaut, relevé sur l'index du 2026-11-26 : `alps,britain-and-ireland,dach,great-britain,guadeloupe,martinique,mayotte,reunion`. L'hôte signale en `WARNING` toute nouvelle feuille sans fichier `shp` (indice d'agrégat), à ajouter à la liste ;
  - **calcul exact** (décision m1 = G1), avec `shapely` (dépendance : ADR-0013, point 3). Polygone de la zone et contours des candidates dans la projection de la carte (Web Mercator, règle d'EF-6.20) ; contour invalide dans le catalogue ⇒ corrigé par `make_valid` à l'import du catalogue, ou région écartée avec un `WARNING` ;
  - **attribution à la plus petite région** : les candidates dont l'emprise coupe celle de la zone sont prises par aire croissante (égalité : par identifiant). On part de *reste* = la zone. Pour chaque candidate : *part* = *reste* ∩ contour. Si l'aire de *part* atteint **0,01 %** de l'aire de la zone, la région est **retenue** et *reste* devient *reste* − contour. Sinon elle n'est pas retenue et *reste* est inchangé (une région plus grande peut couvrir ce morceau) ;
  - **part hors des données** = aire de *reste* final / aire de la zone (contrôle ZD-4.2) ;
  - le seuil de 0,01 % écarte les liserés dus au recouvrement des contours voisins ; ce qu'il écarte compte « hors des données » s'il n'est pas couvert par une région retenue ;
  - **la marge n'intervient pas** : les contours Geofabrik débordent déjà des frontières. L'extraction se fait ensuite sur l'emprise d'extraction dans les seules régions retenues. Des coins de l'emprise sans données, hors du polygone, ne gênent pas ;
  - résultat **déterministe**, trié par identifiant ;
  - **temps** : contours préparés une fois à l'import du catalogue et gardés en mémoire du processus ; calcul ≤ 1 s mesuré en CI sur la zone de 2 000 km de côté ;
  - **critère de non-régression** : la zone par défaut se résout en régions **listées dans un test**, contre une copie figée du catalogue dans les données de test. La liste est établie par TZ2-a avec ce calcul exact. L'échantillonnage du §3 donne 17 régions ; si le calcul exact dépasse `ROUTING_MAX_REGIONS` ou `ROUTING_MAX_SOURCE_BYTES` par défaut, dev le signale avant de poursuivre (la spec est à revoir).
- **ZD-7** **Dalles** : les dalles 1° × 1° Copernicus dont le carré coupe l'emprise d'extraction.
- **ZD-8** **Propriété des dalles** (revue Z2-M3 ; précision de Q-A, option 1) :
  - la tâche tient la liste des dalles **qu'elle a téléchargées elle-même** ;
  - une dalle présente avant elle (ancienne `DEM_BBOX`, ajout manuel) n'est **jamais** supprimée ni comptée ;
  - `DEM_MAX_TILES` borne les dalles de la zone (ZD-4.3) et les dalles de la tâche au total ;
  - au-delà, seules les dalles **de la tâche** et **hors de la zone active** sont purgées, les plus anciennes d'abord ;
  - sous le plafond, rien n'est purgé.

### Tâche unique côté hôte

- **ZD-9** `routing/update.sh` est lancé par une minuterie systemd toutes les minutes, sous le verrou existant (verrou pris ⇒ sortie 0). À chaque passage :
  1. **rapport** (`report`) du graphe en service et de son `zone.json`, plus les compteurs du budget du jour ;
  2. **catalogue**, si sa date dépasse 7 jours ou s'il est absent :
     - téléchargement de l'index (`ROUTING_CATALOG_URL`) et des tailles des candidates, avec des requêtes `HEAD` qui suivent les redirections ;
     - contrôle, puis copie locale, puis transmission (`catalog -`) ;
  3. **prise** : `claim`, ou `claim --monthly` si l'échéance mensuelle est passée (une demande `pending` est alors prise, motif `mensuel` ; sinon on reconstruit la zone active) ;
     - prise seulement si la demande a au moins `ROUTING_ZONE_SETTLE_S` (défaut **300 s**) : une saisie vite remplacée ne lance pas d'import (revue Z2-M2) ;
     - **budget** (revue Z2-M2), tenu par l'hôte dans le volume de routage : au plus `ROUTING_MAX_BUILDS_PER_DAY` tâches (défaut **6**) et `ROUTING_MAX_DOWNLOAD_BYTES_PER_DAY` octets téléchargés (défaut **12 Gio**, soit 3 × `ROUTING_MAX_SOURCE_BYTES`) par jour UTC, mensuelle comprise. Au-delà : `failed --code budget`, sans relance ; une mensuelle refusée est retentée le lendemain ;
  4. **contrôle de la sortie de `claim`**, traitée comme non fiable :
     - JSON lu par un outil ;
     - identifiants de régions présents dans la copie locale ;
     - emprise : 4 nombres au format strict, ordonnés, dans les bornes ;
     - plafonds recalculés avec la copie locale et l'environnement de l'hôte.
     
     Échec : `failed --code other` ou `cap` ;
  5. **extraits** :
     - **règle du même jour** : tous les extraits d'une construction portent la **même date Geofabrik** (celle du nom de fichier daté, lue dans la cible de la redirection : `…-261124.osm.pbf`), âgée de moins de 35 jours ;
     - extraits en cache tous à la date *D* et extraits manquants publiés à la date *D* (cible de la redirection) ⇒ seuls les **manquants** sont téléchargés ; sinon tous sont retéléchargés (mensuelle : toujours) ;
     - **redirections** (revue Z2-m2) : au plus 2, `https` seulement, et l'hôte de **chaque** cible recontrôlé contre `ROUTING_ALLOWED_HOSTS` avant de télécharger (URL effective lue, pas seulement `--proto-redir`) ;
     - `.md5` vérifié ;
     - **taille réelle** : plafond par fichier existant, plus total cumulé recontrôlé sur les octets reçus contre `ROUTING_MAX_SOURCE_BYTES`. Dépassement : arrêt, `failed --code cap` ;
  6. **fusion** (`sources-AAAAMMJJ.osm.pbf`), puis **extraction** `osmium extract --bbox` (stratégie `complete_ways`) dans un conteneur sans réseau. Fichier recadré au-delà de `ROUTING_MAX_IMPORT_BYTES` : `failed --code cap`, sans import ;
  7. **graphe** : dossier neuf, `zone.json` (polygone, emprise, marge, régions, date, demande), import ;
  8. **dalles** : `dem-tiles` sur l'emprise d'extraction (au format `lat_min,lon_min,lat_max,lon_max` qu'il attend), sans retélécharger les dalles présentes ; liste des dalles nouvelles retenue pour ZD-8 ; purge de ZD-8. Étape sautée sans profil `dem` ;
  9. **bascule** :
     - `switching` : code 3 ⇒ suppression du graphe neuf et du fichier recadré, extraits et dalles gardés en cache ;
     - puis bascule, redémarrage (et `opentopodata` si nécessaire) ;
     - santé non atteinte ⇒ retour arrière, `failed --code health` ;
  10. **`done`** avec la liste des dalles nouvelles ; nettoyage (`ROUTING_KEEP_GRAPHS`, cache ZD-11, fichiers fusionnés sauf le dernier).
  - Une erreur aux étapes 2 à 8 donne `failed` avec son code (`catalog`, `cap`, `budget`, `download`, `merge`, `extract`, `import`, `incomplete`, `dem`, `disk`, `timeout`, `other`). Graphe et dalles en service intacts.

### Dialogue avec l'application (`flask routing-zone`)

- **ZD-10** Sous-commandes, appelées par l'hôte seulement (`docker compose exec -T kml-viewer …`).
  - **Sortie** : une ligne JSON sur la sortie standard ; journaux sur la sortie d'erreur, sans coordonnée. Codes : 0 succès, 2 argument ou contenu invalide, 3 demande dépassée ou inconnue, 4 désactivé.
  - **Contenus volumineux** (catalogue, `zone.json`) : passés par l'**entrée standard** (`catalog -`, `report --zone -`) (revue Z2-M1). Taille lue et bornée **avant** toute analyse : catalogue ≤ **16 Mio** (index complet : 3,5 Mo), `zone.json` ≤ **64 Kio**. Au-delà : code 2, rien n'est lu. **Aucun montage n'est ajouté** au service `kml-viewer`, qui reste en `read_only` sans le volume de routage.
  - **Processus** : la commande ne démarre ni l'ordonnanceur ni aucun fil de fond (déjà le cas pour `flask <commande>`, `_start_scheduler`), et ne rejoue aucune tâche. Elle ne fait qu'écrire en base, en une transaction.

  | Sous-commande | Effet |
  |---|---|
  | `catalog -` | catalogue contrôlé (sous-arbre, exclusions, URL `https` sur un hôte autorisé, chemin simple, extension, tailles entières, contours valides et simplifiés, ZD-6), puis remplacé en une transaction ; invalide ⇒ 2, l'ancien est gardé |
  | `claim [--monthly]` | demande prise selon ZD-9.3 ⇒ `building`, sortie `{"request_id", "reason", "polygon", "extract_bbox", "regions", "dem_tiles", "margin_km"}` ; sinon `{}`. Jamais une demande `failed` |
  | `switching ID` | demande encore la dernière ⇒ `starting` ; sinon recalcul (ZD-3), sortie 3 |
  | `done ID --graph NOM --data-date AAAA-MM-JJ --new-tiles -` | zone, emprise, régions, dalles de la tâche = celles de la demande ; recalcul ; si des dalles ont été ajoutées, `no_coverage` remis en file et **marqueur de réveil** posé en base, lu par la tâche `dem_backfill` au tick suivant de l'ordonnanceur du service (revue Z2-m4) |
  | `failed ID --code CODE` | demande non dépassée ⇒ `failed` ; dépassée ⇒ recalcul. Actif inchangé |
  | `report --graph NOM (--zone - \| --historic) --budget BUILDS,BYTES` | zone rapportée contrôlée (EF-6.20). Différente de l'active ⇒ réalignement et audit, **une seule fois par changement**. Invalide ⇒ ignorée, `WARNING` et alerte **dédoublonnés** (une par contenu, tant qu'il ne change pas ; revue Z2-m5) |

  Tous les arguments sont contrôlés : nom `graph-` suivi de 14 chiffres, date ISO, nombres finis, code dans la liste. Une valeur invalide donne 2, et rien n'est écrit.

- **ZD-11** **Cache des extraits** : volume de routage, avec la date et la somme MD5 de chaque fichier. Purge : les extraits absents de la sélection active et âgés de plus de 35 jours, puis les plus anciens tant que le total dépasse `ROUTING_CACHE_MAX_BYTES` (défaut 6 Gio, à revoir par V-Z2 : la sélection active fait déjà 3,28 Go). Les extraits de la sélection active ne sont jamais purgés.

### Administration

- **ZD-12** Avec `=1`, l'onglet « Traces planifiées » montre un panneau **« Couverture en service »** :
  - statut unique en clair, avec son motif et sa date. Exemples : « À jour », « En attente (prise dans 4 min) », « Reconstruction en cours depuis 3 min », « Mise à jour mensuelle en cours », « Redémarrage », « Échec : budget du jour atteint (code `budget`) — la couverture actuelle reste en service » ;
  - zone active en gris sous la zone demandée ;
  - régions actives, date des données OSM, nombre de dalles ;
  - consommation des plafonds (« 1,1 Go / 2 Go · 3 régions / 8 · 24 dalles / 60 ») et du budget du jour ;
  - boutons :
    - `failed` : « Réessayer » et « Revenir à la zone active » ;
    - `building` depuis plus de 2 h : « Tâche bloquée ? » ;
  - diagnostic de l'inventaire n° 17 ;
  - alertes **dédoublonnées** comme les refus de sortie (`outbound._refusals`) : une alerte par cause et par contenu, effacée quand la cause disparaît ;
  - API `GET /api/admin/routing/coverage` et `POST …/coverage/retry` : admin seulement, 403 sinon, 404 si `=0`.
- **ZD-13** **Audit**, acteur `system`, une entrée par **changement**, jamais par passage :
  - catalogue chargé (s'il a changé) ;
  - prise, bascule annoncée, succès (régions, dalles ajoutées), échec (code) ;
  - demande dépassée jetée ;
  - réalignement ;
  - purges (dalles, extraits) ;
  - budget atteint.
  
  Acteur admin : « Réessayer ».

## 6. Sécurité

- Aucun accès Docker depuis l'application ; l'hôte tire. Aucun montage nouveau dans l'application (ZD-10).
- L'application ne fournit que des identifiants de régions et des nombres. Les URL viennent de la copie locale du catalogue, et chaque redirection est recontrôlée (ZD-9.5). La sortie de `claim` est non fiable.
- Catalogue (document tiers) : taille bornée avant analyse, hôte, chemin, extension, sous-arbre et exclusions contrôlés. Ses contours vont à GEOS (code natif, via `shapely`) : au plus **10 000 sommets par région** et **500 000 au total** (relevé du 2026-11-26 : 1 543 au plus), coordonnées finies et dans les bornes, sinon catalogue refusé et ancien catalogue gardé.
- Charge bornée : par tâche (plafonds), par jour (budget), par saisie (délai de stabilité), une tâche à la fois, aucune relance automatique. Le volume tiré de Geofabrik reste borné par jour, ce qui respecte sa politique d'usage.
- Destruction de fichiers de l'hôte : seules les dalles et extraits **créés par la tâche** sont purgeables (ZD-8, ZD-11).

## 7. Cas limites

- **Deux enregistrements rapprochés** : délai de stabilité, puis une seule prise. Pendant une tâche : la tâche est jetée à `switching`, puis recalcul.
- **Retour à la zone active** pendant `building` : demande dépassée, puis `idle` au `switching`.
- **Mensuelle avec une demande en attente** : elle prend la demande, même cycle, même 503 en `starting`.
- **Budget atteint** : `failed --code budget`. Une nouvelle saisie est refusée à la prise jusqu'au lendemain (le panneau le dit).
- **Geofabrik indisponible, ou redirection vers un autre hôte** : `failed --code download`, couverture actuelle gardée.
- **Agrégat nouveau dans l'index** (feuille sans `shp`) : `WARNING` de l'hôte ; tant qu'il n'est pas exclu, il peut gagner des points en mer.
- **Arrêt de l'hôte** : prise expirée au bout de 2 h, puis reprise ; en `starting`, `report` réaligne.
- **Retour arrière manuel par ops** : `report` réaligne (une entrée d'audit).
- **Point dans la zone active mais en mer ou dans un trou des données** : 422, comme aujourd'hui.
- **`ROUTING_ZONE_BUILD` repassé de 1 à 0** : retour à `ROUTING_EXTRACT_URLS` et `DEM_BBOX` ; le graphe reste recadré jusqu'à la mise à jour suivante (à documenter, ops).

## 8. Critères d'acceptation

| # | Critère |
|---|---|
| CA-ZD1 [CI] (ZD-1) | `=0` : enregistrer une zone ⇒ `options` la renvoie aussitôt ; table non écrite ; `claim` ⇒ sortie 4 ; API ⇒ 404 ; CA-R49 et CA-85 inchangés. `=1` avec `DEM_BBOX` défini ⇒ `WARNING`. Premier démarrage avec `=1` et graphe « sources historiques » ⇒ demande motif `activation`, `pending` ; zone au-delà des plafonds ⇒ `failed` `cap` et alerte. |
| CA-ZD2 [CI] (ZD-3, ZD-4) | Catalogue de test figé (contours et tailles), zone active = défaut. `PATCH` d'une zone Z ⇒ 200, `pending` ; `options` ⇒ zone active ; un point de l'active hors de Z ⇒ accepté. Cas inverse ⇒ 400, 0 appel. Zone à 23 % hors des contours ⇒ 400 avec le pourcentage. Plafonds dépassés (régions, octets, dalles) ⇒ 400 chiffré, version inchangée. Catalogue absent ⇒ 400. `PATCH` de la zone active ⇒ `idle`. |
| CA-ZD3 [CI] (ZD-5, ZD-6, ZD-7) | Emprise d'extraction de la zone par défaut Z1 = `[1.205, 48.74, 6.565, 51.64]`, 24 dalles ; bornes respectées ; emprise contenant tous les points des arêtes tracées. Résolution sur le polygone : une zone en U dont l'échancrure couvre une région C ne retient pas C. Agrégat exclu jamais retenu ; un morceau couvert par deux régions va à la plus petite ; un liseré sous 0,01 % de la zone ne retient pas sa région ; une petite région de 10 km² entièrement dans une zone de 50 km × 50 km est retenue (pas d'effet de grille). Même zone ⇒ mêmes régions, quel que soit l'ordre des sommets. Zone par défaut ⇒ la liste de régions du test, dans les plafonds par défaut. Zone de 2 000 km de côté ⇒ calcul ≤ 1 s. `shapely` présent dans `requirements.txt` avec empreintes ; le `pip install` du Dockerfile porte `--only-binary shapely` (revue SHP-1) ; un test fige `shapely.__version__ == "2.2.0"` et `shapely.geos_version == (3, 14, 1)` ; l'image amd64 et arm64 se construit en CI. |
| CA-ZD4 [CI] (ZD-3, ZD-10) | Horloge injectée. `claim` avant `ROUTING_ZONE_SETTLE_S` ⇒ `{}` ; après ⇒ JSON, `building` ; second `claim` ⇒ `{}`. Nouvelle zone puis `switching` de l'ancienne ⇒ 3 et `pending` ; retour à la zone active puis `switching` ⇒ 3 et `idle`. `failed` d'une demande dépassée ⇒ recalcul, la plus récente reste prenable. `failed` de la dernière ⇒ `failed`, `claim` ⇒ `{}` jusqu'à « Réessayer ». `claim --monthly` ⇒ motif `mensuel`, puis `switching` ⇒ `starting`, `done` ⇒ `idle`, aucune alerte de réalignement. `done` avec dalles nouvelles ⇒ `no_coverage` remis en file, marqueur lu au tick suivant (`tick(now)`). Prise de plus de 2 h ⇒ reprise. Arguments invalides ⇒ 2. `catalog -` de 16 Mio + 1 octet ⇒ 2 sans analyse ; URL `http://`, autre hôte, `..`, taille négative, contour de 10 001 sommets, coordonnée `NaN` ⇒ 2, ancien catalogue gardé. La commande ne démarre aucun fil (`threading.enumerate()` inchangé). |
| CA-ZD5 [CI] (ZD-3) | `starting` (motifs `zone` et `mensuel`) ⇒ `options` `available: false`, `state: "starting"` ; `route` ⇒ 503, 0 appel. |
| CA-ZD6 [CI] (ZD-10 `report`, ZD-12, ZD-13) | Active A ; `report` de B ⇒ active B, une entrée d'audit ; même `report` 10 fois ⇒ aucune entrée de plus. Zone rapportée invalide 10 fois ⇒ une alerte, un `WARNING` par contenu. `--historic` ⇒ acceptation sur la zone demandée. `/info` hors de l'emprise active ⇒ alerte « graphe et zone active divergent ». |
| CA-ZD7 [CI] (ZD-8) | Dalles présentes avant la tâche (100, hors zone) + `DEM_MAX_TILES` = 60 ⇒ aucune supprimée ni comptée. Dalles de la tâche au-delà du plafond ⇒ seules celles hors de la zone active, plus anciennes d'abord. |
| CA-ZD8 [CI] (ZD-12) | Libellés de chaque statut et motif, budget et plafonds affichés, boutons selon le statut ; non admin ⇒ 403 ; « Revenir à la zone active » ⇒ réglage = zone active, `idle`, audit admin. |
| CA-ZD9 [hôte] (ZD-9) | Avec une commande `flask` simulée : sortie de `claim` contenant `$(touch /tmp/x)`, `;`, un retour à la ligne, une région inconnue de la copie locale ou des plafonds dépassés ⇒ aucun fichier créé, aucune construction, `failed` appelé. Redirection vers un hôte hors liste ⇒ aucun téléchargement, `failed --code download`. Total reçu au-delà de `ROUTING_MAX_SOURCE_BYTES` (tailles du catalogue sous-estimées) ⇒ arrêt, `cap`. Fichier recadré au-delà de `ROUTING_MAX_IMPORT_BYTES` ⇒ pas d'import, `cap`. 7e tâche du jour ⇒ `budget`. Cache à la date *D* ⇒ aucun téléchargement ; un extrait nouveau publié à *D* ⇒ lui seul téléchargé ; publié à une autre date ⇒ tous retéléchargés. Recadré de 2,4 Go + 1 octet (défaut) ⇒ `cap`, pas d'import. Verrou tenu ⇒ sortie 0 sans `claim`. |
| CA-ZD10 [recette dune] | (a) Zone réduite à la métropole lilloise ⇒ graphe reconstruit, `/info` dans l'emprise, Lille routé, Liège 400 ; durée, pic de RAM, **interruption** relevés. (b) Zone par défaut plus Normandie, polygone `[[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55], [1.35, 49.75], [-0.5, 49.4], [-0.5, 49.05], [1.35, 49.05]]` ⇒ `basse-normandie` ajoutée (≈ 3,4 Go, §3), seul extrait téléchargé si la date Geofabrik n'a pas changé, Caen routé, 8 dalles ajoutées, activité `no_coverage` à Caen corrigée ensuite ; taille recadrée, pic de RAM et disque relevés (V-Z2). (c) Import tué ⇒ couverture précédente, `failed`. (d) Retour à la zone par défaut ⇒ cache réutilisé. (e) Mensuelle ⇒ même cycle, 503 annoncé, zone active conservée. (f) Instance existante passée à `=1` ⇒ demande `activation` construite au premier passage. |

## 9. Décisions

**Décisions de l'utilisateur** (2026-11-26, relayées par coord) :
- **D = D2** : la zone par défaut reste le rectangle du lot Z1 (17 régions, 3,28 Go) ; plafonds relevés à 20 régions et 4 Gio (ZD-4). Le polygone redessiné (D1) et le seuil par région (D3) sont abandonnés ;
- **m1 = G1** : calcul exact par `shapely` (ZD-6) ; dépendance consignée dans l'ADR-0013 (point 3). L'approximation sans dépendance (G2) est abandonnée ;
- budget de **6 tâches par jour**, délai de stabilité de **300 s**, `ROUTING_MAX_UNCOVERED_PCT` à **10 %** ;
- reconduits de la v1 : marge 10 km, extraction sur l'emprise, redémarrage bref, demande dépassée jetée, prise expirée à 2 h, catalogue 7 jours, cache 35 jours, sous-arbre `europe`, tout ou rien avec le MNT avant la bascule ; Q-A option (1), précisée par ZD-8.

- **budget en octets** : `ROUTING_MAX_DOWNLOAD_BYTES_PER_DAY` = **12 Gio** (3 × `ROUTING_MAX_SOURCE_BYTES`), avec l'affinage du cache de ZD-9.5 : à date Geofabrik égale, seuls les extraits manquants sont téléchargés. Une journée type compte au plus un téléchargement complet ; les 6 tâches restent possibles. Écartés : 8 Gio (2 téléchargements complets seulement), 24 Gio sans affinage (le budget en octets ne limiterait jamais) ;
- **RAM doublée plutôt que mesurée d'abord** : import `-Xmx12g` dans 14 Gio, service `-Xmx3g` dans 4 Gio, `ROUTING_MAX_IMPORT_BYTES` recalé à 2,4 Go (ZD-4, inventaire n° 30 et 31). **Ces valeurs sont dimensionnées pour dune** (64 Go, 51 Go disponibles) ; une autre machine applique la règle de ZD-4 ;
- **dépendance** `shapely==2.2.0` : feu vert de review (`REVIEW.md`, « Dépendance shapely ») ; SHP-1 inscrit dans TZ2-a, SHP-2 reporté dans l'ADR-0013 (point 3).

**V-Z2 — Vérification au premier build réel sur dune** (ops, avec CA-ZD10 ; **pas un préalable** à TZ2-c). Relever :
  1. tailles des extraits, du fusionné et du recadré ; durées de fusion, d'extraction et d'import ;
  2. import : tas utilisé en fin d'import (attendu ≤ 8 Go), pic du conteneur (attendu < 14 Gio), `OOMKilled=false`, taille du graphe ;
  3. service, protocole de charge de l'ADR-0007 (256 appels dont 8 en parallèle) : RAM au repos et après charge (attendu < 80 % de 4 Gio), `OOMKilled=false` ;
  4. disque : pic pendant la tâche (extraits, fusionné, recadré, graphe neuf et graphe en service, dalles) ; `ROUTING_CACHE_MAX_BYTES` revu alors ;
  5. report des valeurs relevées dans `docs/exploitation.md` (doc) et, si une valeur attendue n'est pas tenue, dans ZD-4 et l'ADR-0013 (spec), selon la règle de ZD-4.
  
  Estimations de départ (extrapolation linéaire depuis l'ADR-0007) : recadré ≈ 1,9 Go ⇒ tas utilisé ≈ 6,3 Go ; graphe ≈ 560 Mo ; import ≈ 5 min. Si la vérification montre que la RAM ne tient pas sur dune, le choix de la zone par défaut revient à l'utilisateur.

## 10. Reprise de la relecture de review (v2)

| Point | Reprise |
|---|---|
| Z2-B1 | Résolution sur le polygone, pas sur l'emprise (ZD-6) ; tolérance mer (ZD-4.2) ; agrégats exclus ; chiffres de l'index réel (§3) ; défaut : D2, plafonds relevés (ZD-4, §9) |
| Z2-M1 | Entrée standard bornée, aucun montage (ZD-10) |
| Z2-M2 | Budget par jour, délai de stabilité, code `budget` (ZD-9.3) |
| Z2-M3 | Propriété des dalles, rien d'antérieur purgé (ZD-8) |
| Z2-M4 | Activation (ZD-1) ; mensuelle par le même cycle (ZD-3, ZD-9.3) ; recalcul après dépassement (ZD-3) |
| Z2-m1 | G1 retenue : calcul exact par `shapely` (ZD-6, ADR-0013 point 3) |
| Z2-m2 | Redirections recontrôlées, total sur les octets reçus (ZD-9.5) |
| Z2-m3 | `ROUTING_MAX_IMPORT_BYTES` lié à la RAM ; l'hôte fait foi ; 2,4 Go avec `-Xmx12g`, vérifié par V-Z2 (ZD-4, §9) |
| Z2-m4 | CLI sans fil, réveil par marqueur en base (ZD-10) |
| Z2-m5 | Audit et alertes par changement, dédoublonnés (ZD-10, ZD-12, ZD-13) |
| Nits | 2 000 km : renvoi à EF-6.20 seulement (inventaire n° 23) ; `sources-AAAAMMJJ.osm.pbf` (n° 9) |

## 11. Découpage

1. **TZ2-a (dev)** Table d'état et migration ; ZD-1 à ZD-8 ; ZD-10 ; API et audit ; diagnostic ; réveil du MNT. Dépendance `shapely==2.2.0` dans `requirements.in` et le verrou à empreintes ; **SHP-1** : `--only-binary shapely` dans le `pip install` du Dockerfile, test qui fige la version de `shapely` et de GEOS. Critères CA-ZD1 à CA-ZD8.
2. **TZ2-b (dev, front)** Panneau « Couverture en service » ; banc local.
3. **TZ2-c (ops)** Tâche unique dans `update.sh` (catalogue, budget, cache, redirections, extraction, MNT, bascule), unité systemd, recâblage du profil `dem`, retrait de `ROUTING_EXTRACT_URLS` et `DEM_BBOX`, valeurs de RAM provisoires de ZD-4. Critères CA-ZD9, CA-ZD10 ; vérification V-Z2 au premier build réel.
4. **TZ2-d (doc)** README, exploitation (RAM d'import et du service dimensionnées pour dune, règle de ZD-4 pour une autre machine, valeurs relevées par V-Z2, disque, retour arrière), bibliothèque, `.env.example`. Nit de review SHP-n1 : composant tiers GEOS 3.14.1 sous LGPL-2.1, lien vers son source.
5. **TZ2-e (test)** Tests du diagnostic et banc (inventaire n° 26).

Dépend de TZ1 (lot Z1). TZ2-a établit la liste exacte des régions de la zone par défaut (ZD-6) ; review a donné son feu vert à `shapely==2.2.0` (ADR-0013, point 3).
