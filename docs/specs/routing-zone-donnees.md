# Spec — Le polygone de zone, source unique de la couverture (lot Z2)

Statut : **v2 validée par l'utilisateur** (2026-11-26, décisions relayées par coord). Dimensionnement mémoire et disque **recalé sur les mesures de dune** du 2026-11-26 (§9, V-Z2 **vérifiée**).
- La v1 a été validée sous réserve de la relecture de review. La relecture (`REVIEW.md`, « Specs Z1 (zone unique) et proposition Z2 ») a relevé **1 bloquant, 4 majeurs, 5 mineurs**. Cette v2 les reprend (§10).
- **Acquis** :
  - le **principe** : le polygone `routing.coverage_polygon` est la **source unique** de tout ce qui dépend de la couverture (extraits OSM et graphe, points acceptés, dalles MNT), avec **une seule tâche de fond, un seul statut, une seule zone active** ;
  - **Q-A = option (1)** : les dalles suivent la zone et ne sont jamais purgées sous le plafond. La v2 précise ce qui peut être purgé (ZD-8) ;
  - **D = D2** : la zone par défaut reste le rectangle du lot Z1 ; les plafonds sont relevés (ZD-4) ;
  - **m1 = G1** : calcul géométrique exact par `shapely` (ZD-6, ADR-0013) ;
  - budget de 6 tâches et 12 Gio par jour, délai de stabilité de 300 s, 10 % de la zone au plus hors des données ;
  - **RAM** : doublée d'abord, puis recalée sur la vérification V-Z2 avec ≈ 30 % de marge : import `-Xmx8g` dans 10 Gio, service `-Xmx3g` dans 5 Gio, `ROUTING_MAX_IMPORT_BYTES` 2,4 Go, `ROUTING_CACHE_MAX_BYTES` 8 Gio (ZD-4, ZD-11). Valeurs **dimensionnées pour dune**.
- Jusqu'à la livraison de Z2, [gpx-route-builder](gpx-route-builder.md) RB-1.3b décrit le code en service.

Décision d'architecture : [ADR-0013](../adr/0013-zone-pilote-donnees-moteur.md) (**accepté**). Dépend du lot Z1 (admin-space EF-6.20).

## 1. Objet

Enregistrer un nouveau polygone lance **une** tâche de fond qui met la couverture en accord avec la zone : choix des extraits Geofabrik, téléchargement (avec cache), recadrage de chaque extrait, fusion, import du graphe GraphHopper, dalles MNT, bascule.

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
- **Fichier recadré** pour la zone par défaut : **1,956 Go mesurés** sur dune (V-Z2, §9), pour ≈ 1,9 Go estimés au prorata de la surface de chaque région dans l'emprise d'extraction `[1.2053, 48.74, 6.5647, 51.64]` : Belgique, Picardie, Nord-Pas-de-Calais et Luxembourg entiers, Île-de-France et Champagne-Ardenne à ≈ 45 %, régions allemandes et néerlandaises en partie). L'estimation (densité supposée uniforme) était proche de la mesure.
- **Exemple Normandie** : la zone par défaut plus un lobe vers Caen (polygone de CA-ZD10 b) donne 17 à 18 régions (`basse-normandie` entre ; `saarland`, à 0,05 %, sort ou reste selon la méthode), ≈ 3,4 Go d'extraits, ≈ 2,05 Go recadrés (estimation), emprise `[-0.6447, 48.74, 6.5647, 51.64]`, 32 dalles (8 de plus).
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
| 9 | Fichier fusionné `hdf-be-*.osm.pbf` ; défaut `GH_PBF` du compose | nom figé sur la région | `sources-AAAAMMJJ.osm.pbf` (jour des extraits, utilisé par la règle « même jour ») : fusion des extraits **déjà recadrés** (ZD-9.6) | ops |
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
| 28 | **Zone par défaut** (Z1 : rectangle) et installation neuve | 3 régions, 1,08 Go | rectangle gardé (**D2**) : **17 régions, 3,28 Go**, 1,956 Go recadrés (mesure V-Z2) ; plafonds relevés (ZD-4) | — |
| 29 | Commande `flask routing-zone` lancée chaque minute | — | ni ordonnanceur ni fils de fond (déjà garanti par `_start_scheduler` hors service, à tester) | dev, test |
| 30 | **RAM d'import** (`ROUTING_IMPORT_XMX`, `ROUTING_IMPORT_MEM_LIMIT`) | `-Xmx6g`, 8 Go, pour 1,08 Go | **`-Xmx8g` dans 10 Gio** (dune ; mesuré avec `-Xmx12g` : tas 4,4 Go, pic 5,2 Gio, 287 s ; V-Z2, §9) | ops |
| 31 | **RAM du service** (`ROUTING_XMX`, `ROUTING_MEM_LIMIT`) | `-Xmx1536m` dans 2 Go, graphe de 320 Mo | graphe de **662 Mo** ; **`-Xmx3g` dans 5 Gio** (dune ; 3,28 Gio de pic après charge, 81 % de 4 Gio : limite relevée ; V-Z2) | ops |
| 32 | **Disque** du routage (extraits, fusionné, recadré, graphes, dalles) | ~2,4 Go en régime, pic ~4 Go | chaîne **≈ 7,9 Go**, **≈ 9,4 Go** avec les dalles (835 Mo) et le graphe en service (V-Z2) ; `ROUTING_CACHE_MAX_BYTES` 8 Gio (ZD-11) ; volume à prévoir : ZD-11 | ops |
| 33 | **Dépendance** `shapely` de l'application (G1) | absente | `shapely==2.2.0` (GEOS 3.14.1 embarqué), épinglée avec empreintes, roue seulement ; feu vert de review (ADR-0013, point 3) | dev |
| 34 | **Catalogue Geofabrik** (copie de l'application) | — | table `routing_catalog`, une ligne : contenu contrôlé, SHA-256, date ; créée par la migration `0018_routing_coverage_state` avec la table d'état (ZD-2). L'hôte garde sa propre copie dans le volume de routage (ZD-9.2) | dev |

## 5. Exigences

### Activation

- **ZD-1** Variable d'env **`ROUTING_ZONE_BUILD`** : `0` par défaut, `1` pour activer. Elle est lue par l'application et par l'hôte.
  - À `0` : comportement du lot Z1. `DEM_BBOX` et `ROUTING_EXTRACT_URLS` sont lus comme aujourd'hui ; table d'état non utilisée ; aucun panneau ; `flask routing-zone` sort avec le code 4.
  - À `1` : ces deux variables sont **ignorées**, avec un `WARNING` si elles restent définies. **Au premier démarrage avec `1`**, si la zone active est « sources historiques » ou absente, l'application crée une demande pour la zone demandée, motif `activation`, statut `pending`. Une installation neuve et une instance existante suivent ainsi le même chemin (revue Z2-M4 a). Au démarrage, l'application **ne contrôle pas les plafonds** : le catalogue n'est pas encore chargé (il arrive par l'hôte, ZD-9.2). Le contrôle a lieu **à la prise** (`claim`), après le délai de stabilité, comme pour toute demande :
    - catalogue toujours absent ⇒ rien n'est pris, la demande reste `pending`, sans échec ;
    - plafonds dépassés ⇒ statut `failed`, code `cap`, entrée d'audit, alerte « La zone dépasse les plafonds : réduis-la ou relève les plafonds (exploitation) » ; rien n'est construit.

### Zone active et statut unique

- **ZD-2** Deux tables, créées par **la même migration nommée** `0018_routing_coverage_state` (ADR-0012), toutes deux à **une seule ligne** (`id = 1`). Aucune donnée existante n'est touchée.

  **`routing_coverage_state`**, l'état de la couverture. La ligne naît « sources historiques » (zone active vide), statut `idle`. Elle contient :
  - la zone active et son emprise d'extraction ;
  - les régions actives et le graphe en service ;
  - la date des données OSM ;
  - les dalles téléchargées par la tâche ;
  - le statut, son motif (`zone`, `mensuel` ou `activation`) et sa date ;
  - la demande en cours (numéro, zone demandée, motif, date) ;
  - la **demande prise** : son identifiant, sa date de prise et son **contenu** tel que `claim` l'a rendu (zone, emprise d'extraction, régions, dalles, marge de cette demande). `done` reprend ce contenu, sans le recalculer : un catalogue ou une marge changés entre-temps ne modifient pas ce qui est déclaré actif ;
  - le code du dernier échec ;
  - la date du catalogue ;
  - le marqueur de réveil du MNT ;
  - les compteurs du budget du jour, rapportés par l'hôte ;
  - les **empreintes de dédoublonnage** : celle du dernier rapport accepté et celle du dernier rapport invalide, pour n'auditer et n'alerter qu'une fois par changement (ZD-10 `report`, ZD-13).

  **`routing_catalog`**, le **catalogue** :
  - son contenu **après contrôle** (`catalog -`, ZD-10) : les seules candidates du sous-arbre, exclusions retirées, en JSON ;
  - l'empreinte SHA-256 de ce contenu ;
  - sa date de chargement.

  Il est remplacé en une transaction, jamais modifié en place ; un catalogue invalide laisse l'ancien. Un catalogue de même empreinte que l'actuel n'est pas réécrit. Chaque processus de l'application garde les contours préparés (ZD-6) en mémoire, **associés à l'empreinte** : il les recharge quand l'empreinte en base change. Les processus web voient ainsi le catalogue chargé par la commande de l'hôte, sans redémarrage.
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
     
     Un quatrième plafond, `ROUTING_MAX_IMPORT_BYTES`, porte sur la taille du fichier **recadré** envoyé à l'import, seule grandeur liée à la **RAM** (revue Z2-m3). Ce plafond ne se connaît qu'après le recadrage et la fusion : il est contrôlé par l'**hôte** (ZD-9, étape 6). **Valeurs par défaut, dimensionnées pour dune** (64 Go, 51 Go disponibles), recalées sur la vérification V-Z2 du 2026-11-26 avec ≈ 30 % de marge (décision de coord) :
     - `ROUTING_IMPORT_XMX` = **8g** et `ROUTING_IMPORT_MEM_LIMIT` = **10g**. Mesuré avec `-Xmx12g` sur la zone par défaut (1,956 Go) : tas au plus 4,4 Go, pic du conteneur 5,2 Gio, sans mise à mort ;
     - `ROUTING_MAX_IMPORT_BYTES` = **2,4 Go** (2 400 000 000 octets), conservé : 23 % au-dessus du fichier mesuré. Par la règle ci-dessous : tas ≈ 4,4 × 2,4 / 1,956 ≈ 5,4 Go pour un fichier de 2,4 Go, × 1,5 ≈ 8,1 Go, soit `-Xmx8g` à 1 % près. L'exemple Normandie (≈ 2,05 Go, estimation non mesurée) passe ;
     - service : `ROUTING_XMX` = **3g**, `ROUTING_MEM_LIMIT` = **5g** (inventaire n° 31) ;
     - pendant une tâche, import (10 Gio) et service (5 Gio) tournent ensemble avec l'application : ≈ 17 Gio, dans les 51 Go disponibles de dune.
     
     **Règle de dimensionnement**, pour une autre machine ou une autre zone par défaut :
     - `ROUTING_IMPORT_XMX` ≥ tas utilisé pour un fichier de `ROUTING_MAX_IMPORT_BYTES` × 1,5 ;
     - `ROUTING_IMPORT_MEM_LIMIT` ≥ `ROUTING_IMPORT_XMX` + 2 Go, et le pic du conteneur y tient sans mise à mort par le noyau ;
     - service : RAM du conteneur après la charge de l'ADR-0007 < 80 % de `ROUTING_MEM_LIMIT` ;
     - relever `ROUTING_MAX_IMPORT_BYTES` suppose de relever `ROUTING_IMPORT_XMX` et `ROUTING_IMPORT_MEM_LIMIT` dans la même proportion, à vérifier sur la machine.
     
     Ces valeurs **ne valent que pour dune** : une instance sur une machine plus petite les règle dans son `.env` (doc, `.env.example`).
  4. **confirmation** (EF-6.13) enrichie : régions ajoutées et retirées, octets à télécharger hors cache, dalles nouvelles, et « La zone actuelle reste en service pendant la reconstruction (quelques minutes, au plus tôt dans `ROUTING_ZONE_SETTLE_S`), suivie d'un court redémarrage. ».

  En cas d'échec : rien n'est écrit, version inchangée. Les contrôles de l'application sont **anticipés** ; **l'hôte fait foi** et recontrôle avec son propre environnement. Si les deux environnements divergent, le refus de l'hôte donne `failed --code cap`.

### Calculs de l'application

- **ZD-5** **Emprise d'extraction** : emprise de la zone `[lon_min, lat_min, lon_max, lat_max]`, marge `m` km (`routing.zone_margin_km`, entier de 0 à 50, défaut 10), R = 6 371 km.
  - Δlat = m / 111,1949 ; lat_min − Δlat et lat_max + Δlat, bornées à ±85,0511.
  - φ = la plus grande valeur absolue des deux latitudes **de l'emprise de la zone, avant la marge** (51,55 pour la zone par défaut) ; Δlon = m / (111,1949 · cos φ) ; lon_min − Δlon et lon_max + Δlon, bornées à ±180.
  - Arrondi vers l'extérieur à 4 décimales.
  - Exemple : zone par défaut Z1, marge 10 km ⇒ Δlat ≈ 0,08993, Δlon ≈ 0,14463 ⇒ `[1.2053, 48.74, 6.5647, 51.64]`, 24 dalles. La règle fait foi (décision de coord, 2026-11-26) : une valeur arrondie à 3 décimales serait fausse.
  - **Marge seule changée** (règle, décision de coord du 2026-11-26) : le statut ne dépend que des sommets (ZD-3), donc changer `routing.zone_margin_km` ne crée **pas** de demande. La nouvelle marge s'applique à la prochaine tâche (changement de zone ou mensuelle), lue au moment de la prise (`claim`). L'admin l'annonce sous le champ (admin-space EF-6.22, CA-88).
- **ZD-6** **Résolution des régions, sur le polygone** (revue Z2-B1) :
  - **candidates** : les feuilles du sous-arbre `ROUTING_CATALOG_ROOT` (défaut `europe`), **moins** les agrégats et l'outre-mer listés dans `ROUTING_CATALOG_EXCLUDE`. Défaut, relevé sur l'index du 2026-11-26 : `alps,britain-and-ireland,dach,great-britain,guadeloupe,martinique,mayotte,reunion`. L'hôte signale en `WARNING` toute nouvelle feuille sans fichier `shp` (indice d'agrégat), à ajouter à la liste ;
  - **calcul exact** (décision m1 = G1), avec `shapely` (dépendance : ADR-0013, point 3). Polygone de la zone et contours des candidates dans la projection de la carte (Web Mercator, règle d'EF-6.20) ; contour invalide dans le catalogue ⇒ corrigé par `make_valid` à l'import du catalogue, ou région écartée avec un `WARNING` ;
  - **attribution à la plus petite région** : les candidates dont l'emprise coupe celle de la zone sont prises par aire croissante (égalité : par identifiant). On part de *reste* = la zone. Pour chaque candidate : *part* = *reste* ∩ contour. Si l'aire de *part* atteint **0,01 %** de l'aire de la zone, la région est **retenue** et *reste* devient *reste* − contour. Sinon elle n'est pas retenue et *reste* est inchangé (une région plus grande peut couvrir ce morceau) ;
  - **part hors des données** = aire de *reste* final / aire de la zone (contrôle ZD-4.2) ;
  - le seuil de 0,01 % écarte les liserés dus au recouvrement des contours voisins ; ce qu'il écarte compte « hors des données » s'il n'est pas couvert par une région retenue ;
  - **la marge n'intervient pas** : les contours Geofabrik débordent déjà des frontières. L'extraction se fait ensuite sur l'emprise d'extraction dans les seules régions retenues. Des coins de l'emprise sans données, hors du polygone, ne gênent pas ;
  - résultat **déterministe**, trié par identifiant ;
  - **temps** : contours préparés une fois par catalogue et gardés en mémoire du processus, associés à l'empreinte du catalogue (ZD-2) ; calcul ≤ 1 s mesuré en CI sur la zone de 2 000 km de côté ;
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
  0. **étape en suspens** (revue TZ2a-n9) : si un passage précédent a laissé un `switching` ou un `done` sans réponse définitive, l'hôte le rejoue d'abord, **avant `report`** et avant toute prise, selon la conduite de ZD-10 :
     - l'étape en suspens est notée dans le volume de routage (`pending-step.json` : numéro de demande, étape, graphe, date des données, dalles nouvelles), écrite avant l'appel et effacée sur une réponse définitive ;
     - **avant `report`** : un `report` du graphe neuf pendant `starting` réaligne la zone et recalcule le statut ; un `done` rejoué après lui répondrait 3, et les activités `no_coverage` ne seraient pas remises en file ;
     - le rejeu ne compte pas comme une tâche du budget ;
  1. **rapport** (`report`) du graphe en service et de son `zone.json`, plus les compteurs du budget du jour ;
  2. **catalogue**, si sa date dépasse 7 jours ou s'il est absent :
     - téléchargement de l'index (`ROUTING_CATALOG_URL`) et des tailles des candidates, avec des requêtes `HEAD` qui suivent les redirections ; la taille est ajoutée à chaque candidate (champ `size`, format de ZD-10) ;
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
  6. **recadrage de chaque extrait**, puis **fusion** des fichiers recadrés, dans un conteneur sans réseau (décision de coord, d'après V-Z2) :
     - chaque extrait retenu passe par `osmium extract --bbox` sur l'emprise d'extraction (stratégie `complete_ways`) ;
     - les fichiers recadrés sont fusionnés en `sources-AAAAMMJJ.osm.pbf`, le fichier envoyé à l'import ;
     - **l'ordre inverse est interdit** : les extraits Geofabrik voisins se chevauchent, la fusion ne dédoublonne pas ces objets, et `osmium extract` sur le fichier fusionné échoue (« Node ID twice ») ;
     - mesure sur dune, zone par défaut : recadrage 118 s, fusion 74 s, fichier fusionné 1,956 Go ;
     - fichier fusionné au-delà de `ROUTING_MAX_IMPORT_BYTES` : `failed --code cap`, sans import ;
  7. **graphe** : dossier neuf, `zone.json` (polygone, emprise, marge, régions, date, demande), import ;
  8. **dalles** : `dem-tiles` sur l'emprise d'extraction (au format `lat_min,lon_min,lat_max,lon_max` qu'il attend), sans retélécharger les dalles présentes ; liste des dalles nouvelles retenue pour ZD-8 ; purge de ZD-8. Étape sautée sans profil `dem` ;
  9. **bascule** :
     - **ordre obligatoire** (revue TZ2a-n10) : `switching ID` réussi (code 0) **une seule fois**, puis bascule, puis `done ID`. L'hôte n'appelle jamais `done` sans un `switching` à 0 pour la même demande. L'application accepte encore un `done` sur une demande `building` : l'hôte ne s'appuie pas sur cette tolérance ;
     - rejouer `switching` après un code 2 (ZD-10) ne fait pas un second `switching` : sur une demande déjà `starting`, l'application répond 0 sans rien changer (revue TZ2a-n6) ;
     - `switching` : code 3 ⇒ suppression du graphe neuf, des fichiers recadrés et du fusionné, extraits et dalles gardés en cache ;
     - puis bascule, redémarrage (et `opentopodata` si nécessaire) ;
     - santé non atteinte ⇒ retour arrière, `failed --code health` ;
  10. **`done`** avec la liste des dalles nouvelles ; nettoyage (`ROUTING_KEEP_GRAPHS`, cache ZD-11, fichiers fusionnés sauf le dernier).
  - Une erreur aux étapes 2 à 8 donne `failed` avec son code (`catalog`, `cap`, `budget`, `download`, `merge`, `extract`, `import`, `incomplete`, `dem`, `disk`, `timeout`, `other`). Graphe et dalles en service intacts.
  - Un code de sortie de `flask routing-zone` est traité selon la conduite de ZD-10. En particulier, un code 2 sur `switching` ou `done` ne donne **jamais** `failed`, ni suppression du graphe, ni retour arrière.

### Dialogue avec l'application (`flask routing-zone`)

- **ZD-10** Sous-commandes, appelées par l'hôte seulement (`docker compose exec -T kml-viewer …`).
  - **Sortie** : une ligne JSON sur la sortie standard ; journaux sur la sortie d'erreur, sans coordonnée. Codes : 0 succès, 2 argument ou contenu invalide, 3 demande dépassée ou inconnue, 4 désactivé.
  - **Conduite de l'hôte selon le code** (revues TZ2a-n9 et TZ2a-n10). Une erreur passagère de l'application (base verrouillée au-delà de 30 s, exception hors contrat) sort aussi en code 2 (revue TZ2a-n4). Sur `switching` et `done`, qui ne portent aucun contenu à corriger, un 2 est donc **transitoire** :

    | Sous-commande | 0 | 2 | 3 | 4 | autre code (1, 125 à 137 de `docker compose exec`…) |
    |---|---|---|---|---|---|
    | `switching ID` | `starting` ⇒ bascule | **transitoire** : graphe neuf, recadrés et fusionné gardés ; aucun `failed` ; fin du passage ; `switching` rejoué au passage suivant (ZD-9 étape 0), puis bascule | demande dépassée ⇒ suppressions de ZD-9.9 | désactivé ⇒ graphe neuf supprimé, fin du passage | comme 2 (décision de coord, voir plus bas) |
    | `done ID` | tâche close | **transitoire** : graphe en service gardé ; ni `failed` ni retour arrière ; fin du passage ; `done` rejoué au passage suivant, avant `report` (ZD-9 étape 0) | dépassée : rien à défaire, `report` réaligne | fin du passage, rien à défaire | comme 2 (décision de coord) |
    | `catalog -`, `claim`, `report`, `failed` | succès | sens actuel (ZD-9) | sens actuel | fin du passage | **`failed ID --code other`** si l'hôte tient une demande prise dont le `switching` n'a pas encore répondu 0 ; sinon (aucun numéro tenu, ou `failed` lui-même) fin du passage, journalisée |

    - Rejeu borné par la **prise** : une étape en suspens dont la prise date de plus de 2 h est abandonnée et `pending-step.json` effacé. Un `switching` abandonné laisse l'application reprendre la demande (prise expirée, ZD-10 `claim`), et le graphe neuf non basculé est supprimé. Un `done` abandonné laisse le graphe en service, et `report` réaligne la zone active.
    - Dans la séquence de ZD-9, `catalog -`, `claim` et `report` ne sont jamais appelés pendant qu'une demande est tenue sans `switching` à 0 : un 2 sur `switching` termine le passage, et la prise ne se fait qu'après `report`. La branche `failed --code other` de la dernière ligne est donc une **garde** contre un changement d'ordre, sans cas atteignable aujourd'hui.
    - `failed` n'est jamais appelé en réponse à son propre échec : un `failed` sans réponse 0 ou 3 est journalisé, et la prise expire au bout de 2 h.
    - **Décision de coord (2026-11-26)** : un code hors 0/2/3/4 sur `switching` ou `done` est traité comme un 2 (transitoire, rejeu), car une panne du conteneur de l'application ne doit pas faire perdre le graphe, neuf ou en service. En dernier recours, la prise expire au bout de 2 h.
    - La garde `failed --code other` reste écrite (décision de coord).
  - **Contenus volumineux** (catalogue, `zone.json`) : passés par l'**entrée standard** (`catalog -`, `report --zone -`) (revue Z2-M1). Taille lue et bornée **avant** toute analyse : catalogue ≤ **16 Mio** (index complet : 3,5 Mo), `zone.json` ≤ **64 Kio**. Au-delà : code 2, rien n'est lu. **Aucun montage n'est ajouté** au service `kml-viewer`, qui reste en `read_only` sans le volume de routage.
  - **Format d'entrée de `catalog -`** (choix de dev, TZ2-a) : le JSON de l'**index Geofabrik** (`index-v1.json`), tel quel, enrichi par l'hôte :

    ```json
    {"features": [{"properties": {"id": "…", "parent": "…", "urls": {"pbf": "https://…"}, "size": 699945414},
                   "geometry": {"type": "MultiPolygon", "coordinates": […]}}]}
    ```

    - `id`, `parent`, `urls.pbf` et `geometry` (`Polygon` ou `MultiPolygon`) viennent de l'index publié ; les autres champs de l'index sont ignorés ;
    - **`size`** (octets, entier ≥ 0) n'existe pas dans l'index publié : l'**hôte l'ajoute** à chaque candidate d'après ses requêtes `HEAD` (ZD-9.2). Candidate (ZD-6) sans `size` valide ⇒ catalogue invalide, code 2 ;
    - l'application en tire les candidates (sous-arbre, exclusions), les contrôle et n'enregistre que ce résultat (ZD-2, `routing_catalog`).
  - **Processus** : la commande ne démarre ni l'ordonnanceur ni aucun fil de fond (déjà le cas pour `flask <commande>`, `_start_scheduler`), et ne rejoue aucune tâche. Elle ne fait qu'écrire en base, en une transaction.

  | Sous-commande | Effet |
  |---|---|
  | `catalog -` | catalogue contrôlé (sous-arbre, exclusions, URL `https` sur un hôte autorisé, chemin simple, extension, tailles entières, contours valides et bornés, ZD-6 et §6), puis écrit dans `routing_catalog` (ZD-2) en une transaction ; invalide ⇒ 2, l'ancien est gardé |
  | `claim [--monthly]` | demande prise selon ZD-9.3 ⇒ `building`, sortie `{"request_id", "reason", "polygon", "extract_bbox", "regions", "dem_tiles", "margin_km"}` ; sinon `{}`. Jamais une demande `failed` |
  | `switching ID` | demande encore la dernière ⇒ `starting` ; sinon recalcul (ZD-3), sortie 3 |
  | `done ID --graph NOM --data-date AAAA-MM-JJ --new-tiles -` | zone, emprise, régions, dalles de la tâche = celles de la demande prise (contenu enregistré à la prise, ZD-2) ; recalcul ; si des dalles ont été ajoutées, `no_coverage` remis en file et **marqueur de réveil** posé en base, lu par la tâche `dem_backfill` au tick suivant de l'ordonnanceur du service (revue Z2-m4) |
  | `failed ID --code CODE` | demande non dépassée ⇒ `failed` ; dépassée ⇒ recalcul. Actif inchangé |
  | `report --graph NOM (--zone - \| --historic) --budget BUILDS,BYTES` | zone rapportée contrôlée (EF-6.20). Différente de l'active ⇒ réalignement et audit, **une seule fois par changement**. Invalide ⇒ ignorée, `WARNING` et alerte **dédoublonnés** (une par contenu, tant qu'il ne change pas ; revue Z2-m5) |

  Tous les arguments sont contrôlés : nom `graph-` suivi de 14 chiffres, date ISO, nombres finis, code dans la liste. Une valeur invalide donne 2, et rien n'est écrit.

- **ZD-11** **Cache des extraits** : volume de routage, avec la date et la somme MD5 de chaque fichier. Purge : les extraits absents de la sélection active et âgés de plus de 35 jours, puis les plus anciens tant que le total dépasse `ROUTING_CACHE_MAX_BYTES` (défaut **8 Gio**, soit la sélection active de la zone par défaut, 3,28 Go, plus une sélection de même taille pour revenir à une zone précédente sans tout retélécharger, avec ≈ 30 % de marge). Les extraits de la sélection active ne sont jamais purgés.
  - **Volume à prévoir** pour le routage et les dalles sur dune : ≈ 9,4 Go mesurés avec le seul cache de la sélection active (V-Z2), plus ≈ 4,7 Go si le cache est plein, soit ≈ 14 Go ; avec ≈ 30 % de marge, **≈ 18 Go** (validé par coord le 2026-11-26, comme le cache de 8 Gio ; dune a 334 Go libres ; à reporter par doc).

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
| CA-ZD1 [CI] (ZD-1) | `=0` : enregistrer une zone ⇒ `options` la renvoie aussitôt ; table non écrite ; `claim` ⇒ sortie 4 ; API ⇒ 404 ; CA-R49 et CA-85 inchangés. `=1` avec `DEM_BBOX` défini ⇒ `WARNING`. Premier démarrage avec `=1` et graphe « sources historiques » ⇒ demande motif `activation`, `pending`, sans contrôle des plafonds. Puis, horloge injectée après `ROUTING_ZONE_SETTLE_S` : sans catalogue, `claim` ⇒ `{}` et toujours `pending` ; catalogue chargé et zone au-delà des plafonds, `claim` ⇒ `{}`, `failed` `cap`, une entrée d'audit et l'alerte. |
| CA-ZD2 [CI] (ZD-3, ZD-4) | Catalogue de test figé (contours et tailles), zone active = défaut. `PATCH` d'une zone Z ⇒ 200, `pending` ; `options` ⇒ zone active ; un point de l'active hors de Z ⇒ accepté. Cas inverse ⇒ 400, 0 appel. Zone à 23 % hors des contours ⇒ 400 avec le pourcentage. Plafonds dépassés (régions, octets, dalles) ⇒ 400 chiffré, version inchangée. Catalogue absent ⇒ 400. `PATCH` de la zone active ⇒ `idle`. |
| CA-ZD3 [CI] (ZD-5, ZD-6, ZD-7) | Emprise d'extraction de la zone par défaut Z1 = `[1.2053, 48.74, 6.5647, 51.64]` exactement, 24 dalles ; bornes respectées ; emprise contenant tous les points des arêtes tracées. Résolution sur le polygone : une zone en U dont l'échancrure couvre une région C ne retient pas C. Agrégat exclu jamais retenu ; un morceau couvert par deux régions va à la plus petite ; un liseré sous 0,01 % de la zone ne retient pas sa région ; une petite région de 10 km² entièrement dans une zone de 50 km × 50 km est retenue (pas d'effet de grille). Même zone ⇒ mêmes régions, quel que soit l'ordre des sommets. Zone par défaut ⇒ la liste de régions du test, dans les plafonds par défaut. Zone de 2 000 km de côté ⇒ calcul ≤ 1 s. `shapely` présent dans `requirements.txt` avec empreintes ; le `pip install` du Dockerfile porte `--only-binary shapely` (revue SHP-1) ; un test fige `shapely.__version__ == "2.2.0"` et `shapely.geos_version == (3, 14, 1)` ; l'image amd64 et arm64 se construit en CI. |
| CA-ZD4 [CI] (ZD-3, ZD-10) | Horloge injectée. `claim` avant `ROUTING_ZONE_SETTLE_S` ⇒ `{}` ; après ⇒ JSON, `building` ; second `claim` ⇒ `{}`. Nouvelle zone puis `switching` de l'ancienne ⇒ 3 et `pending` ; retour à la zone active puis `switching` ⇒ 3 et `idle`. `failed` d'une demande dépassée ⇒ recalcul, la plus récente reste prenable. `failed` de la dernière ⇒ `failed`, `claim` ⇒ `{}` jusqu'à « Réessayer ». `claim --monthly` ⇒ motif `mensuel`, puis `switching` ⇒ `starting`, `done` ⇒ `idle`, aucune alerte de réalignement. `done` avec dalles nouvelles ⇒ `no_coverage` remis en file, marqueur lu au tick suivant (`tick(now)`). Prise de plus de 2 h ⇒ reprise. Arguments invalides ⇒ 2. `catalog -` de 16 Mio + 1 octet ⇒ 2 sans analyse ; URL `http://`, autre hôte, `..`, taille négative, contour de 10 001 sommets, coordonnée `NaN` ⇒ 2, ancien catalogue gardé. La commande ne démarre aucun fil (`threading.enumerate()` inchangé). |
| CA-ZD5 [CI] (ZD-3) | `starting` (motifs `zone` et `mensuel`) ⇒ `options` `available: false`, `state: "starting"` ; `route` ⇒ 503, 0 appel. |
| CA-ZD6 [CI] (ZD-10 `report`, ZD-12, ZD-13) | Active A ; `report` de B ⇒ active B, une entrée d'audit ; même `report` 10 fois ⇒ aucune entrée de plus. Zone rapportée invalide 10 fois ⇒ une alerte, un `WARNING` par contenu. `--historic` ⇒ acceptation sur la zone demandée. `/info` hors de l'emprise active ⇒ alerte « graphe et zone active divergent ». |
| CA-ZD7 [CI] (ZD-8) | Dalles présentes avant la tâche (100, hors zone) + `DEM_MAX_TILES` = 60 ⇒ aucune supprimée ni comptée. Dalles de la tâche au-delà du plafond ⇒ seules celles hors de la zone active, plus anciennes d'abord. |
| CA-ZD8 [CI] (ZD-12) | Libellés de chaque statut et motif, budget et plafonds affichés, boutons selon le statut ; non admin ⇒ 403 ; « Revenir à la zone active » ⇒ réglage = zone active, `idle`, audit admin. |
| CA-ZD9 [hôte] (ZD-9, ZD-10 conduite de l'hôte) | Avec une commande `flask` simulée : sortie de `claim` contenant `$(touch /tmp/x)`, `;`, un retour à la ligne, une région inconnue de la copie locale ou des plafonds dépassés ⇒ aucun fichier créé, aucune construction, `failed` appelé. Redirection vers un hôte hors liste ⇒ aucun téléchargement, `failed --code download`. Total reçu au-delà de `ROUTING_MAX_SOURCE_BYTES` (tailles du catalogue sous-estimées) ⇒ arrêt, `cap`. Fichier fusionné au-delà de `ROUTING_MAX_IMPORT_BYTES` ⇒ pas d'import, `cap`. 7e tâche du jour ⇒ `budget`. Cache à la date *D* ⇒ aucun téléchargement ; un extrait nouveau publié à *D* ⇒ lui seul téléchargé ; publié à une autre date ⇒ tous retéléchargés. Fusionné de 2,4 Go + 1 octet (défaut) ⇒ `cap`, pas d'import. Recadrage fait extrait par extrait, avant la fusion (aucun `osmium extract` sur le fichier fusionné). Verrou tenu ⇒ sortie 0 sans `claim`. `switching` simulé en code 2 ⇒ graphe neuf gardé, aucun `failed`, `pending-step.json` présent ; passage suivant ⇒ `switching` rejoué avant `report`, puis bascule et `done`. `done` simulé en code 2 ⇒ graphe en service gardé, ni `failed` ni retour arrière ; passage suivant ⇒ `done` rejoué **avant** `report`. Journal des appels : jamais de `done` sans `switching` à 0 pour le même numéro, jamais deux `switching` à 0 pour une même demande hors rejeu. Étape en suspens dont la prise a plus de 2 h ⇒ abandonnée. Code 1 ou 137 sur `catalog -`, `report` ou `claim` ⇒ fin du passage, aucune construction, aucun `failed` (pas de numéro tenu). |
| CA-ZD10 [recette dune] | (a) Zone réduite à la métropole lilloise ⇒ graphe reconstruit, `/info` dans l'emprise, Lille routé, Liège 400 ; durée, pic de RAM, **interruption** relevés. (b) Zone par défaut plus Normandie, polygone `[[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55], [1.35, 49.75], [-0.5, 49.4], [-0.5, 49.05], [1.35, 49.05]]` ⇒ `basse-normandie` ajoutée (≈ 3,4 Go, §3), seul extrait téléchargé si la date Geofabrik n'a pas changé, Caen routé, 8 dalles ajoutées, activité `no_coverage` à Caen corrigée ensuite ; taille recadrée, pic de RAM et disque relevés (V-Z2). (c) Import tué ⇒ couverture précédente, `failed`. (d) Retour à la zone par défaut ⇒ cache réutilisé. (e) Mensuelle ⇒ même cycle, 503 annoncé, zone active conservée. (f) Instance existante passée à `=1` ⇒ demande `activation` construite au premier passage. |

## 9. Décisions

**Décisions de l'utilisateur** (2026-11-26, relayées par coord) :
- **D = D2** : la zone par défaut reste le rectangle du lot Z1 (17 régions, 3,28 Go) ; plafonds relevés à 20 régions et 4 Gio (ZD-4). Le polygone redessiné (D1) et le seuil par région (D3) sont abandonnés ;
- **m1 = G1** : calcul exact par `shapely` (ZD-6) ; dépendance consignée dans l'ADR-0013 (point 3). L'approximation sans dépendance (G2) est abandonnée ;
- budget de **6 tâches par jour**, délai de stabilité de **300 s**, `ROUTING_MAX_UNCOVERED_PCT` à **10 %** ;
- reconduits de la v1 : marge 10 km, extraction sur l'emprise, redémarrage bref, demande dépassée jetée, prise expirée à 2 h, catalogue 7 jours, cache 35 jours, sous-arbre `europe`, tout ou rien avec le MNT avant la bascule ; Q-A option (1), précisée par ZD-8.

- **budget en octets** : `ROUTING_MAX_DOWNLOAD_BYTES_PER_DAY` = **12 Gio** (3 × `ROUTING_MAX_SOURCE_BYTES`), avec l'affinage du cache de ZD-9.5 : à date Geofabrik égale, seuls les extraits manquants sont téléchargés. Une journée type compte au plus un téléchargement complet ; les 6 tâches restent possibles. Écartés : 8 Gio (2 téléchargements complets seulement), 24 Gio sans affinage (le budget en octets ne limiterait jamais) ;
- **RAM doublée plutôt que mesurée d'abord** (import `-Xmx12g` dans 14 Gio, service `-Xmx3g` dans 4 Gio), puis **recalée sur V-Z2** (décision de coord) : import `-Xmx8g` dans 10 Gio, service `-Xmx3g` dans 5 Gio, `ROUTING_MAX_IMPORT_BYTES` 2,4 Go conservé, `ROUTING_CACHE_MAX_BYTES` 8 Gio (ZD-4, ZD-11). **Ces valeurs sont dimensionnées pour dune** (64 Go, 51 Go disponibles) ; une autre machine applique la règle de ZD-4 ;
- **ordre de la chaîne** : recadrer chaque extrait, puis fusionner (ZD-9.6) ;
- **dépendance** `shapely==2.2.0` : feu vert de review (`REVIEW.md`, « Dépendance shapely ») ; SHP-1 inscrit dans TZ2-a, SHP-2 reporté dans l'ADR-0013 (point 3).

**V-Z2 — Vérification au premier build réel sur dune : vérifiée** (ops, 2026-11-26, zone par défaut, 17 régions confirmées sur l'index réel ; relevé relayé par coord).

| Grandeur | Mesure | Attendu / effet |
|---|---|---|
| Extraits (17) | 3,279 Go, téléchargés en 1 616 s | dans `ROUTING_MAX_SOURCE_BYTES` (4 Gio) |
| Recadrage extrait par extrait | 1,958 Go en 118 s | fusionner puis recadrer échoue (« Node ID twice ») ⇒ ZD-9.6 |
| Fusion des recadrés | 1,956 Go en 74 s | sous `ROUTING_MAX_IMPORT_BYTES` (2,4 Go), gardé |
| Import, `-Xmx12g` dans 14 Gio | 287 s ; tas au plus 4,4 Go ; pic du conteneur 5,2 Gio ; pas d'OOM | ramené à `-Xmx8g` dans 10 Gio |
| Graphe | 662 Mo (560 Mo estimés) | — |
| Service, `-Xmx3g` dans 4 Gio | repos 1,42 Gio ; après 256 appels dont 8 en parallèle 3,25 Gio, soit **81 %** (cible < 80 % non tenue) ; pic du cgroup 3,28 Gio ; p50 83 ms | limite relevée à 5 Gio (66 %) |
| Dalles MNT (24) | 835 Mo | `DEM_MAX_TILES` 60 gardé |
| Disque | chaîne ≈ 7,9 Go ; ≈ 9,4 Go avec dalles et graphe en service | cache relevé à 8 Gio ; volume ≈ 18 Go (ZD-11) |

Les valeurs de ZD-4 et de ZD-11 en découlent, avec ≈ 30 % de marge. L'exemple Normandie (CA-ZD10 b) n'a pas été mesuré : ses chiffres du §3 restent des estimations. À reporter dans `docs/exploitation.md` (doc).

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
| Z2-m3 | `ROUTING_MAX_IMPORT_BYTES` lié à la RAM ; l'hôte fait foi ; 2,4 Go avec `-Xmx8g`, recalé sur V-Z2 (ZD-4, §9) |
| Z2-m4 | CLI sans fil, réveil par marqueur en base (ZD-10) |
| Z2-m5 | Audit et alertes par changement, dédoublonnés (ZD-10, ZD-12, ZD-13) |
| TZ2a-n9 (review TZ2-a ; contrat de l'hôte = TZ2-c, et non TZ2-b comme écrit dans REVIEW.md) | Code 2 transitoire sur `switching` et `done` : rejeu, jamais `failed` ni suppression du graphe ; étape en suspens rejouée avant `report` ; code inattendu ailleurs ⇒ `failed --code other` (ZD-9 étape 0, ZD-10 « Conduite de l'hôte », CA-ZD9) |
| TZ2a-n10 (review TZ2-a ; TZ2-c) | Ordre obligatoire : `switching` à 0 une fois, puis `done` ; la tolérance de l'application n'est pas utilisée (ZD-9.9) |
| Nits | 2 000 km : renvoi à EF-6.20 seulement (inventaire n° 23) ; `sources-AAAAMMJJ.osm.pbf` (n° 9) |

## 11. Découpage

1. **TZ2-a (dev)** Tables d'état et du catalogue, migration `0018_routing_coverage_state` (ZD-2) ; ZD-1 à ZD-8 ; ZD-10 ; API et audit ; diagnostic ; réveil du MNT. Dépendance `shapely==2.2.0` dans `requirements.in` et le verrou à empreintes ; **SHP-1** : `--only-binary shapely` dans le `pip install` du Dockerfile, test qui fige la version de `shapely` et de GEOS. Critères CA-ZD1 à CA-ZD8.
2. **TZ2-b (dev, front)** Panneau « Couverture en service » ; banc local.
3. **TZ2-c (ops)** Tâche unique dans `update.sh` (catalogue, budget, cache, redirections, extraction, MNT, bascule), unité systemd, recâblage du profil `dem`, retrait de `ROUTING_EXTRACT_URLS` et `DEM_BBOX`, valeurs de RAM et de cache de ZD-4 et ZD-11, recadrage avant fusion (ZD-9.6). Critères CA-ZD9, CA-ZD10 ; V-Z2 vérifiée.
4. **TZ2-d (doc)** README, exploitation (RAM d'import et du service dimensionnées pour dune, règle de ZD-4 pour une autre machine, mesures V-Z2, volume de ≈ 18 Go, retour arrière), bibliothèque, `.env.example`. Nit de review SHP-n1 : composant tiers GEOS 3.14.1 sous LGPL-2.1, lien vers son source.
5. **TZ2-e (test)** Tests du diagnostic et banc (inventaire n° 26).

Dépend de TZ1 (lot Z1). TZ2-a établit la liste exacte des régions de la zone par défaut (ZD-6) ; review a donné son feu vert à `shapely==2.2.0` (ADR-0013, point 3).
