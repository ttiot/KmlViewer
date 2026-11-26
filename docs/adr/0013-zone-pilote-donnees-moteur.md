# ADR-0013 — Le polygone de zone, source unique de la couverture

Statut : **accepté** (v2, 2026-11-26, décisions de l'utilisateur relayées par coord ; dimensionnement recalé sur les mesures de dune du 2026-11-26). La v1 a été acceptée sous réserve de review ; la relecture (`REVIEW.md`) a relevé un bloquant et quatre majeurs, repris ici et dans la spec v2. **Principe confirmé par l'utilisateur** : le polygone `routing.coverage_polygon` est la **source unique** de tout ce qui dépend de la couverture (extraits OSM, graphe, points acceptés, dalles MNT), avec **une seule tâche de fond, un seul statut, une seule zone active**. **Acquis** : MNT = option (1) (dalles = zone, jamais purgées sous le plafond), précisée au point 5. **Décidé** : la zone par défaut reste le rectangle du lot Z1, avec des plafonds relevés (D2) ; calcul géométrique exact par `shapely` (G1) ; budget de 6 tâches et 12 Gio par jour, délai de 300 s, 10 % hors des données ; RAM doublée d'abord, puis recalée sur les mesures de dune du 2026-11-26 (point 4, section « Mesures ») ; recadrage de chaque extrait avant la fusion (point 6) ; `shapely==2.2.0`, feu vert de review (point 3). Lot Z2 · Spec : [routing-zone-donnees](../specs/routing-zone-donnees.md) · Amende l'[ADR-0007](0007-moteur-routage.md) (chaîne de données, extraits fixes, `DEM_BBOX`) et renverse la règle RB-1.3b « la zone ne réduit pas le graphe du moteur » ([gpx-route-builder](../specs/gpx-route-builder.md)).

## Contexte

- Depuis le lot Z1, la zone couverte est un seul polygone (admin-space EF-6.20). Aujourd'hui elle **filtre seulement les points**. Trois autres choses décrivent la couverture, chacune de son côté :
  - les **extraits OSM**, liste fixe en env de l'hôte (`ROUTING_EXTRACT_URLS` : Nord-Pas-de-Calais, Picardie, Belgique) ;
  - le **graphe** GraphHopper, construit chaque mois par `routing/update.sh` depuis ces extraits ;
  - les **dalles MNT**, emprise en env (`DEM_BBOX`), téléchargées par la tâche `dem-tiles` au démarrage du profil `dem`. Exemple : `DEM_BBOX=48,1,52,7` ne couvre pas la Normandie à l'ouest de 1° E.
- Demande : **modifier le polygone met tout le reste en accord**, en tâche de fond. Pendant ce temps, la zone **demandée** diffère de la zone **active**. **L'acceptation des points ne change qu'une fois la tâche finie.** Le polygone peut déborder des extraits actuels (exemple : la Normandie) : la tâche ajoute alors les extraits Geofabrik nécessaires, sous un **plafond de ressources obligatoire**. La borne actuelle (2 000 km de côté) est trop large pour en tenir lieu.
- Contraintes existantes :
  - l'application n'a **pas** le socket Docker (il donnerait l'hôte) et elle est exposée à Internet ;
  - `update.sh` tourne sur l'hôte : verrou, dossier de graphe neuf, bascule atomique, retour arrière, outils sans réseau sauf le téléchargement ;
  - mesures sur dune (64 Go, 12 cœurs ; ADR-0007) : environ 1,1 Go d'extraits, import `-Xmx6g` avec **6,2 Go de pic** en 154 s, fusion 41 s, service 2 Go, démarrage ≈ 3 s. Ces mesures valent pour 3 régions ; la zone par défaut gardée en tire 17 : mesures du 2026-11-26 dans la section « Mesures » ;
  - dalles Copernicus : environ 45 Mo par degré carré (docs/bibliotheque.md).

## Décision

1. **Une zone demandée, une zone active.**
   - La **demandée** est le réglage, écrit par l'admin.
   - L'**active** est la zone que couvrent à la fois le graphe en service **et** les dalles MNT. L'application la tient en base, et ne l'écrit que sur rapport de l'hôte.
   - Tout ce qui accepte ou refuse un point lit la zone **active** : contrôle 400, `options`, front, rabattement RB-S7.
   - **Un seul statut** : `idle`, `pending`, `building`, `starting` ou `failed`, avec un motif (`zone`, `mensuel` ou `activation`). Après une demande dépassée, le statut est **recalculé** (`idle` ou `pending`) : un échec ancien ne bloque jamais la demande la plus récente.
   - **Activation** : au premier démarrage avec `ROUTING_ZONE_BUILD=1` sur un graphe d'avant Z2, l'application crée une demande pour la zone demandée. Installation neuve et instance existante suivent le même chemin.
2. **Une seule tâche de fond, sur l'hôte, sans socket Docker dans l'application.**
   - Une minuterie systemd lance chaque minute `routing/update.sh`. La mise à jour mensuelle est la **même tâche, avec le même cycle** (prise, `switching`, `starting`, `done`) et le téléchargement forcé, sous le même verrou et le même statut.
   - L'hôte parle à l'application par une commande Flask (`docker compose exec -T kml-viewer flask routing-zone …`). L'hôte tire, l'application ne pousse rien.
   - La base de l'application reste la **seule source de vérité** de la zone et du statut.
   - Les contenus volumineux (catalogue, `zone.json`) passent par l'**entrée standard** de la commande, avec une taille bornée avant analyse. **Aucun montage n'est ajouté** au service de l'application.
3. **Les sources suivent la zone.**
   - Le **catalogue** est l'index publié par Geofabrik, limité à un sous-arbre (`europe` par défaut), avec la taille de chaque extrait. L'hôte le télécharge, le contrôle, puis le transmet à l'application.
   - La **résolution** est faite par l'application, **sur le polygone** et non sur son emprise (revue Z2-B1), par un **calcul exact** : chaque morceau du polygone va à la plus petite région candidate qui le couvre ; une région est retenue si son morceau atteint 0,01 % de la zone (spec ZD-6).
   - Les candidates sont les feuilles du catalogue, **moins les agrégats** (`dach`, `great-britain`… relevés sur l'index réel) et l'outre-mer.
   - Une part du polygone hors de tout contour (la mer) est tolérée jusqu'à un plafond.
   - **Méthode géométrique : `shapely`** (décision de l'utilisateur, G1). L'approximation par échantillonnage est écartée : elle est lente en pur Python (1,4 à 2,8 s pour la zone par défaut) et sensible à l'alignement de la grille (une région à 0,05 % retenue ou non selon la zone).
   - **Dépendance `shapely`**, nouvelle pour l'application, sous ces conditions :
     - **version** : **`shapely==2.2.0`**, feu vert de review (`REVIEW.md`, « Dépendance shapely »). C'est la seule version qui publie des roues cp315 (x86_64 et aarch64) : ne pas descendre en dessous. Elle embarque **GEOS 3.14.1** (C API 1.20.5) dans `shapely.libs/` ;
     - **binaire, par roue seulement** : GEOS est du code natif C/C++. Le verrou contient aussi l'empreinte du sdist et le builder a `gcc` ; l'option `--only-binary shapely` doit donc figurer dans le `pip install` du Dockerfile, sans quoi rien ne garantit l'absence de compilation (revue SHP-1, dans TZ2-a). Aucun paquet `libgeos` ajouté à l'image ;
     - **épinglage avec empreintes** : ajoutée à `requirements.in`, verrouillée dans `requirements.txt` par `uv pip compile --generate-hashes`, installée avec `--require-hashes`, comme les autres dépendances. Seule dépendance transitive : `numpy`, déjà verrouillé ;
     - **surveillance** : `pip-audit` (CI) et OSV couvrent le paquet PyPI `shapely`. **GEOS embarqué leur est invisible**, ainsi qu'à Trivy : une `.so` copiée dans `shapely.libs/` n'est ni un paquet Python ni un paquet Debian (revue SHP-2). Une faille publiée côté GEOS ne ferait rien sortir en CI tant que `shapely` n'a pas sa propre entrée. GEOS est donc **suivi à la main** : à chaque montée de `shapely`, et sur les avis de sécurité de `libgeos/geos`. Un test CI fige la version de GEOS (`shapely.geos_version == (3, 14, 1)`) : une montée qui la change devient visible en review (SHP-1). En option, ops peut ajouter une alerte sur les publications de sécurité de `libgeos/geos` ;
     - **licence** : `shapely` BSD-3-Clause ; GEOS LGPL-2.1, lié dynamiquement, texte de licence présent dans l'image. L'image publiée doit le mentionner (nit SHP-n1, doc) ;
     - **surface d'attaque** : seuls les contours du catalogue (document tiers, contrôlé par l'hôte puis par l'application) et le polygone de la zone (admin, borné par EF-6.20) atteignent GEOS. Contours bornés en sommets et en coordonnées (spec §6).
   - L'application ne renvoie à l'hôte que des **identifiants de régions**. L'hôte prend les URL dans **sa propre copie** du catalogue, jamais dans la sortie de l'application.
   - `ROUTING_EXTRACT_URLS` disparaît. `ROUTING_ALLOWED_HOSTS` reste (`download.geofabrik.de`).
4. **Plafond de ressources obligatoire**, contrôlé à l'écriture (**400** clair, rien n'est écrit), puis recontrôlé par l'hôte.
   - Taille cumulée des extraits retenus (`ROUTING_MAX_SOURCE_BYTES`) : elle borne le réseau et le disque, et elle est recontrôlée sur les octets réellement reçus ;
   - taille du fichier recadré envoyé à l'import (`ROUTING_MAX_IMPORT_BYTES`) : c'est **elle** qui borne la RAM. Elle est contrôlée par l'hôte après le recadrage et la fusion. L'utilisateur a choisi de **doubler la RAM** plutôt que de mesurer d'abord (`-Xmx12g` dans 14 Gio, service `-Xmx3g` dans 4 Gio), puis coord a recalé les valeurs sur les mesures de dune du 2026-11-26, avec ≈ 30 % de marge : défaut **2,4 Go** (fichier mesuré : 1,956 Go), `ROUTING_IMPORT_XMX=8g` et `ROUTING_IMPORT_MEM_LIMIT=10g` (tas mesuré au plus 4,4 Go, pic 5,2 Gio), service `-Xmx3g` dans **5 Gio** (81 % de 4 Gio après charge : cible de 80 % non tenue), cache des extraits 8 Gio. **Ces valeurs sont dimensionnées pour dune** (64 Go, 51 Go disponibles) ; une autre machine applique la règle de dimensionnement de la spec (ZD-4) ;
   - nombre de régions (`ROUTING_MAX_REGIONS`) ;
   - nombre de dalles MNT (`DEM_MAX_TILES`) ;
   - part du polygone hors des données (`ROUTING_MAX_UNCOVERED_PCT`) ;
   - **budget par jour** (`ROUTING_MAX_BUILDS_PER_DAY`, `ROUTING_MAX_DOWNLOAD_BYTES_PER_DAY`) et **délai de stabilité** avant la prise (`ROUTING_ZONE_SETTLE_S`) : chaque tâche est bornée, et leur nombre aussi (revue Z2-M2) ;
   - **chiffres de l'index réel** (2026-11-26) : les 3 extraits actuels font 1,08 Go ; le rectangle par défaut du lot Z1, gardé (D2), tire 17 régions, soit 3,28 Go. Défauts : 20 régions, 4 Gio d'extraits, 60 dalles ; budget de 6 tâches et 12 Gio par jour.
   - L'application contrôle **par anticipation** ; l'hôte **fait foi** avec son propre environnement.
   - Ces plafonds protègent l'**hôte**. Ils sont donc en **env**, pas dans l'admin : un compte admin compromis ne peut pas les relever.
   - La borne de 2 000 km de côté reste un garde-fou géométrique, écrite dans EF-6.20 seulement ; ce sont les plafonds qui limitent réellement.
5. **Les dalles MNT suivent la zone.**
   - `DEM_BBOX` n'est **plus un réglage** : l'emprise MNT est l'emprise d'extraction (zone plus marge), passée par l'hôte à `dem-tiles` à chaque exécution de la tâche.
   - Les dalles ne sont **pas purgées** à la réduction de la zone : elles servent aussi aux activités importées (voir « Conséquences »).
   - **Propriété** (revue Z2-M3) : la tâche ne compte et ne peut purger que les dalles **qu'elle a téléchargées**. Une dalle présente avant elle (ancienne `DEM_BBOX`) n'est jamais supprimée. Au-delà de `DEM_MAX_TILES`, on purge les dalles de la tâche hors de la zone active, les plus anciennes d'abord.
   - Après la bascule, les activités `dem_status = 'no_coverage'` sont remises en file ; la tâche `dem_backfill` du service est réveillée par un **marqueur en base**, puisque la commande tourne dans un autre processus.
6. **Extraction sur l'emprise plus une marge** (`osmium extract --bbox`, stratégie `complete_ways`, défaut 10 km), et non sur le polygone.
   - **Chaque extrait est recadré, puis les recadrés sont fusionnés** (décision de coord d'après les mesures de dune). L'ordre inverse échoue : les extraits Geofabrik voisins se chevauchent, la fusion ne dédoublonne pas ces objets, et `osmium extract` sur le fichier fusionné s'arrête sur « Node ID twice ».
   - Une arête droite sur la carte (Web Mercator) reste dans le rectangle longitude/latitude de ses extrémités. On évite ainsi l'écart entre droite de la carte et droite d'`osmium`, sans bibliothèque de géométrie.
7. **Cache des extraits.**
   - Les `.pbf` sont gardés dans le volume, avec leur date et leur somme MD5. Les liens `*-latest` de Geofabrik redirigent (307) vers un fichier daté : l'hôte suit au plus 2 redirections, en `https`, et recontrôle l'hôte de chaque cible.
   - Une construction n'utilise que des extraits **de la même date Geofabrik** (lue dans le nom du fichier daté), comme le demandait déjà TR7 pour la jointure aux frontières, et de moins de 35 jours. Si les extraits manquants sont publiés à la date des extraits en cache, seuls eux sont téléchargés ; sinon tous les extraits retenus sont retéléchargés.
   - **Purge** : les extraits hors de la sélection courante et de plus de 35 jours, puis les plus anciens au-delà de `ROUTING_CACHE_MAX_BYTES`.
8. **Une construction à la fois, la dernière demande gagne.**
   - Avant la bascule, l'hôte demande si sa demande est toujours la dernière. Sinon, il jette son travail, sans redémarrage ; les extraits et les dalles téléchargés restent en cache.
9. **Tout ou rien.**
   - Les dalles sont téléchargées **avant** la bascule.
   - Un échec à n'importe quelle étape (catalogue, plafond, téléchargement, fusion, extraction, import, MNT, santé) laisse l'ancien graphe et l'ancienne zone active. Le statut passe à `failed` avec un code. **Aucune relance automatique.**
10. **Bascule par redémarrage bref.**
    - L'état passe à `starting` avant la recréation du moteur. Pendant ce temps, `route` répond 503 sans appel au moteur.
    - Le service d'altitude est redémarré seulement s'il ne voit pas les nouvelles dalles (à vérifier).
    - Bleu/vert est écarté en v1 ; à rouvrir si l'interruption mesurée dépasse 60 s.
11. **Le graphe porte sa zone.**
    - L'hôte écrit `zone.json` dans le dossier du graphe : polygone, emprise, régions, date des données.
    - À chaque passage, il **rapporte** le graphe courant, et l'application aligne la zone active sur ce rapport.
    - Ce mécanisme couvre le retour arrière manuel, l'arrêt brutal pendant `starting` et le graphe d'avant Z2 (sans `zone.json`, appelé « sources historiques »).
    - Audit et alertes une fois par **changement**, dédoublonnés, jamais à chaque passage.
12. **Activation explicite** par `ROUTING_ZONE_BUILD=1`, à poser quand l'unité hôte est installée. Sans elle, on garde le comportement du lot Z1, `DEM_BBOX` compris.
13. **Confirmation et audit.**
    - L'enregistrement annonce ce qu'il déclenche : régions, taille à télécharger, dalles, durée, court redémarrage.
    - Chaque étape est auditée, avec l'acteur `system`.

## Mesures sur dune (ops, 2026-11-26)

Premier build réel de la zone par défaut (17 régions, confirmées sur l'index réel), relevé d'ops relayé par coord.

| Grandeur | Mesure | Valeur retenue (≈ 30 % de marge) |
|---|---|---|
| Extraits | 3,279 Go, téléchargés en 1 616 s | `ROUTING_MAX_SOURCE_BYTES` 4 Gio |
| Recadrage extrait par extrait | 1,958 Go en 118 s | recadrer avant de fusionner (point 6) |
| Fusion des recadrés | 1,956 Go en 74 s | `ROUTING_MAX_IMPORT_BYTES` 2,4 Go |
| Import (`-Xmx12g` dans 14 Gio) | 287 s ; tas au plus 4,4 Go ; pic du conteneur 5,2 Gio ; pas d'OOM | `-Xmx8g` dans 10 Gio |
| Graphe | 662 Mo | — |
| Service (`-Xmx3g` dans 4 Gio) | repos 1,42 Gio ; 3,25 Gio après 256 appels dont 8 en parallèle (81 %) ; pic du cgroup 3,28 Gio ; p50 83 ms | `-Xmx3g` dans 5 Gio |
| Dalles MNT (24) | 835 Mo | `DEM_MAX_TILES` 60 |
| Disque | chaîne ≈ 7,9 Go ; ≈ 9,4 Go avec dalles et graphe en service | cache 8 Gio ; volume ≈ 18 Go |

L'exemple Normandie (zone par défaut plus un lobe vers Caen) n'a pas été mesuré : ses chiffres restent des estimations (spec §3).

## Raisons

- **Source unique** : quatre descriptions indépendantes de la couverture divergent forcément. Le cas Normandie le montre : la zone acceptée, le graphe et les dalles n'auraient pas couvert la même chose. Une seule zone active, dérivée d'un seul polygone et atteinte en une seule tâche, rend l'écart impossible en régime établi.
- **Zone demandée ≠ zone active** : c'est la seule manière de n'accepter que ce que le graphe et le MNT en service couvrent, pendant les minutes de la reconstruction.
- **Pas de socket, l'hôte tire** : une application compromise ne doit pas pouvoir faire exécuter quoi que ce soit sur l'hôte. Elle ne fournit que des identifiants de régions et des nombres, contrôlés par l'hôte contre ses propres données.
- **Plafonds en env** : ils protègent la machine et relèvent de l'exploitation, pas de l'admin web.
- **Catalogue Geofabrik** plutôt qu'une liste blanche écrite à la main : c'est ce que demande l'utilisateur (le polygone peut déborder librement). Le risque d'un document tiers est contenu par le contrôle de l'hôte (hôte, chemin, extension, sous-arbre) et par les plafonds.
- **Réutiliser `update.sh`** : verrou, retour arrière et contrôles d'entrée existent déjà et ont passé la recette (TR7).

## Conséquences

- **Le MNT sert aussi les activités importées**, pas seulement les traces planifiées : altitude-mnt, `dem_backfill`, bouton montagne. Le dériver de la zone de routage le lie à une fonction bêta.
  - Une instance sans constructeur de traces, ou une activité hors de la zone (vacances dans les Alpes), n'aurait plus de dalles neuves.
  - L'absence de purge protège l'existant, pas l'avenir. **Accepté par l'utilisateur** (option (1), 2026-11-26) : une installation neuve n'a de dalles que sur sa zone.
- **La zone par défaut devient un engagement de ressources** : le premier passage avec `ROUTING_ZONE_BUILD=1` télécharge les extraits et les dalles de la zone par défaut. Le rectangle du lot Z1 est gardé (décision D2) : 17 régions, 3,28 Go d'extraits, 1,956 Go après recadrage (mesures de dune). Par rapport à la situation actuelle (1,08 Go), l'import passe de 154 à 287 s, le graphe de 320 à 662 Mo, la RAM du service de 2 à 5 Gio, et le volume de routage et des dalles à ≈ 18 Go à prévoir. Ces valeurs sont dimensionnées pour **dune** : une instance sur une machine plus petite doit régler ses variables.
- **Nouvelle dépendance binaire** `shapely` dans l'application (point 3) : une bibliothèque native de plus, GEOS, dont le suivi des failles est **manuel** (invisible pour `pip-audit` et Trivy).
- **Côté hôte** : nouvelle unité systemd, nouvelles variables d'env (plafonds, budget, catalogue et exclusions, cache ; `ROUTING_IMPORT_XMX`, `ROUTING_IMPORT_MEM_LIMIT`, `ROUTING_XMX`, `ROUTING_MEM_LIMIT`, `ROUTING_CACHE_MAX_BYTES` recalées sur les mesures de dune), nouvelle commande Flask, nouvelle table d'état (migration nommée), nouveau réglage `routing.zone_margin_km`. `ROUTING_EXTRACT_URLS` et `DEM_BBOX` sont retirés.
- **Compose** : `opentopodata` dépend aujourd'hui de `dem-tiles` « terminé avec succès ». Or `dem-tiles` sort en erreur (code 2) sans `DEM_BBOX`. Il faut revoir le câblage du profil `dem`.
- **Interruption du routage** de quelques secondes à chaque changement de zone, comme chaque mois aujourd'hui.
- **Dépendance à Geofabrik** au moment du geste : son indisponibilité donne un `failed`, et l'ancienne zone reste.

## Options écartées

- **Socket Docker dans l'application**, ou API Docker exposée : prendre l'application reviendrait à prendre l'hôte.
- **Volume partagé** pour la demande et le statut : un répertoire en écriture commun à un service exposé et à un script privilégié, sans transaction.
- **Liste blanche d'extraits écrite à la main** (variante V-a de la version précédente de cet ADR) : plus fermée, mais contraire au débordement libre demandé. Elle reste possible en restreignant `ROUTING_CATALOG_ROOT` à un sous-arbre étroit.
- **Zone par défaut redessinée** sur Nord-Pas-de-Calais, Picardie et Belgique (D1), pour garder 1,08 Go : écartée par l'utilisateur, qui garde le rectangle du lot Z1. **Seuil de présence par région** (D3) : des points acceptés tomberaient hors des données.
- **Approximation géométrique sans dépendance** (G2) : écartée par l'utilisateur au profit de `shapely` (point 3).
- **Extraction par le polygone** (`osmium extract --polygon`) : il faudrait densifier les arêtes et calculer un tampon, donc ajouter une bibliothèque de géométrie dans l'outillage. Le gain est faible pour la bêta.
- **Dalles MNT après la bascule, en tâche séparée** : deux statuts et une zone active qui couvrirait le graphe mais pas le MNT. C'est contraire au principe.
- **Bleu/vert** : double RAM du service et pilotage de la bascule par l'application. Disproportionné tant que l'interruption n'est pas mesurée.
- **Activer une construction dépassée**, ou **relancer automatiquement après un échec** : redémarrage inutile dans le premier cas, boucle coûteuse dans le second.
