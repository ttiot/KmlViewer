# ADR-0007 — Moteur de routage : GraphHopper auto-hébergé

Statut : **Accepté** (auto-hébergement : arbitrage Q-R2 ; GraphHopper : 2e vague ; emprise Hauts-de-France + Belgique : 4e vague ; levée de la réserve du spike : décision D1 de l'utilisateur ; **condition « mesures sur la machine cible » levée** et méthode de build tranchée : décision journalisée par coord, relayée le 2026-11-24, d'après les mesures d'ops sur « dune »). Périmètre belge « pays entier » : recommandation de cet ADR. **Amendé par l'[ADR-0013](0013-zone-pilote-donnees-moteur.md)** (effet à la livraison du lot Z2) : les extraits et l'emprise du graphe suivent le polygone de zone ; la RAM de l'import et du service est doublée pour dune (`-Xmx12g` dans 14 Gio, `-Xmx3g` dans 4 Gio).
Spec : [gpx-route-builder](../specs/gpx-route-builder.md)

## Contexte
Besoins arbitrés :
- **Boucle fermée** passant par tous les points dans l'ordre, **détours** ajoutés pour atteindre une distance cible (Q-R1 v3) ; avec un seul point, boucle à distance cible.
- Profils : course/trail/rando (pied), vélo de route, **gravel** (profil seul), VTT.
- Auto-hébergé dans `docker-compose.yml`, réseau interne, aucun port publié (modèle `opentopodata`).
- Emprise : **Hauts-de-France + Belgique**, en un seul graphe : les boucles peuvent traverser la frontière. Mise à jour **mensuelle**.

Les détours étant construits par l'appli (ADR-0010) au-dessus d'appels `route` classiques, le moteur doit surtout être **rapide sur des appels répétés** (jusqu'à 15 par génération) et offrir de bons profils. La boucle native ne sert qu'au cas « 1 point ».

## Options comparées

| Critère | GraphHopper | BRouter | openrouteservice | Valhalla | OSRM |
|---|---|---|---|---|---|
| Licence | Apache 2.0 | MIT | GPL v3 | MIT | BSD-2 |
| Boucle à distance cible native (cas 1 point) | **oui** (`round_trip`) | à vérifier | **oui** (`round_trip`) | non | non |
| Points de passage multiples | oui | oui | oui | oui | oui |
| Pied / rando | oui | oui | oui | oui | 1 profil par graphe |
| Vélo de route / VTT | oui | **très bons** | oui | oui | 1 profil par graphe |
| Gravel | modèle personnalisé à écrire | profils communautaires | pas de profil dédié | type `Cross` approchant | non |
| Plusieurs profils sur un service | oui | oui | oui | oui | non |
| Empreinte | Java, RAM d'import selon l'emprise | légère | Java, la plus lourde | C++, modérée | modérée |

Les « à vérifier » et toutes les ressources ne sont pas mesurés ici : le spike les établit.

## Décision
**GraphHopper** (open source), auto-hébergé, sous réserve du spike TR0 :
- un profil par activité (pied, rando, vélo de route, VTT, gravel par modèle personnalisé favorisant les surfaces non revêtues et les pistes) ;
- service compose `graphhopper` sous un profil dédié (`docker compose --profile routing up -d`), aucun port publié, conteneur durci ; **image construite par le projet** : jar `graphhopper-web` de la **release GitHub 11.0 épinglée** (SHA-256 vérifié au build) sur **`eclipse-temurin:21-jre-noble` épinglé par empreinte** ; **pas l'image communautaire** du spike (elle embarque un jar `11.0-SNAPSHOT`, pas la release) ; configuration des profils (dont le modèle gravel) versionnée dans `routing/` ;
- destination du moteur (`routing.base_url`) soumise à `OUTBOUND_ALLOWED_HOSTS` (ADR-0011) : l'adresse interne du service est écrite en env par l'opérateur, ce qui l'autorise implicitement ;
- santé sondée par la tâche `routing_health` de l'ordonnanceur de fond (premier passage au démarrage, puis à intervalle), jamais à la demande d'une page ; avant le premier sondage réussi, fonction indisponible (spec RB-1.2, R3, n8) ;
- **un seul graphe** construit à partir d'un `.osm.pbf` **fusionné** Hauts-de-France + Belgique (voir ci-dessous) ;
- mise à jour **mensuelle** par `routing/update.sh` : téléchargement, fusion, import dans un dossier neuf, bascule ; l'ancien graphe sert jusqu'au succès et reste disponible pour un **retour arrière** ;
- altitude : le DEM local de l'appli remplace celle du moteur (`DEM_BBOX` élargie à la même emprise).

## Emprise : construction des données

**Sources** : extraits régionaux Geofabrik, téléchargés le même jour, sommes de contrôle vérifiées.
- France : extrait(s) couvrant les Hauts-de-France. Geofabrik découpe la France par régions ; vérifier au spike si l'extrait est publié sous le nom « Hauts-de-France » ou sous les anciennes régions Nord-Pas-de-Calais et Picardie (deux fichiers à fusionner).
- Belgique : extrait pays.

**Fusion** : `osmium merge` (ou équivalent) dans un conteneur d'outillage dédié, jamais dans l'image de l'appli. Point à vérifier : un même objet présent dans deux extraits (zone frontalière) avec des versions différentes ; les extraits téléchargés le même jour limitent le risque, le spike contrôle la continuité du réseau routable à la frontière.

**Alternative écartée pour la fusion** : découper un extrait plus large (France entière + Belgique, ou Europe de l'Ouest) avec un polygone. Pas de problème de jointure, mais téléchargement et découpage bien plus lourds chaque mois pour le même résultat.

### Périmètre belge : recommandation « pays entier »

| Option | Pour | Contre |
|---|---|---|
| **Belgique entière** (recommandé) | Extrait Geofabrik prêt à l'emploi, aucun polygone à maintenir ; pays petit (~30 000 km², du même ordre que les Hauts-de-France), donc surcoût modéré ; couvre les Ardennes belges, terrain de choix pour trail, VTT et gravel ; aucune boucle coupée par une limite de province arbitraire | Graphe à peu près doublé par rapport aux seuls Hauts-de-France (RAM, disque, durée d'import : à chiffrer au spike) |
| Régions limitrophes seulement (Flandre-Occidentale, Hainaut, Namur, Luxembourg belge…) | Graphe plus petit | Polygone à construire et maintenir ; découpage mensuel en plus ; boucles coupées à une limite qui n'existe pas sur le terrain ; gain limité vu la taille du pays |

Recommandation : **Belgique entière**. Si le spike montre une empreinte inacceptable, repli sur les provinces limitrophes, ce qui ne change que l'étape d'extraction (aucun impact sur le code de l'appli).

### Limites connues de l'emprise
- Une boucle qui sortirait vers la Normandie, l'Île-de-France, le Grand Est, les Pays-Bas ou le Luxembourg n'est pas générée (422) ; les détours sont rabattus dans l'emprise.
- Une marge française autour des Hauts-de-France (ex. 20 km) éviterait les échecs près de ces limites, au prix d'un découpage par polygone. Non retenu en v1 ; à reconsidérer d'après les retours de la bêta.

## Spike TR0 : critères de levée de la réserve
1. **Ressources chiffrées** sur l'emprise Hauts-de-France + Belgique : RAM d'import, RAM de service, disque, durée d'import et de mise à jour mensuelle ; valeurs reportées dans la doc ops et dans `mem_limit`.
2. **Fusion** : réseau routable continu à la frontière, aucun objet en double bloquant l'import ; au moins 2 boucles transfrontalières générées correctement.
3. Latence d'un appel `route` à ~5 points : compatible avec 15 appels dans un budget de 30 s (cible indicative : ≤ 1 s par appel au 95e centile).
4. `round_trip` fonctionnel avec les profils retenus sur la version épinglée.
5. Qualité du modèle gravel et des profils sur quelques parcours connus de l'utilisateur, des deux côtés de la frontière.
6. Résultats de la stratégie de détours (ADR-0010) jugés acceptables.

Réserve non levée ⇒ **plan B : BRouter**. Comme les détours sont construits par l'appli, le changement passe par un adaptateur (`RoutingProvider`). Si BRouter n'a pas de boucle native, l'appli doit construire **toute boucle** que le moteur ne sait pas produire : le cas « 1 point » **et**, si la stratégie retenue est S2 ou l'hybride (ADR-0010), les **pétales** (M15). Avec S1 seule, seul le cas « 1 point » est concerné.

## Résultats du spike TR0 (poste local, 2026-11-24)

Sources : `spike/detours/out/summary.md` et `results.jsonl` (1 128 exécutions, 0 erreur moteur), mesures d'ops. Moteur : GraphHopper 11.0, image `israelhikingmap/graphhopper:11.0`, données Hauts-de-France + Belgique fusionnées, 6 profils (`foot`, `hike`, `bike`, `racingbike`, `mtb`, `gravel`). Poste : 12 cœurs, 30 Go.

| Critère (section précédente) | Mesure | Statut |
|---|---|---|
| 1. Ressources | import **303 s** ; graphe **320 Mo** ; service **~0,6 Go** au repos, **~2,5 Go** après charge (`-Xmx3g`) | atteint en local ; **refait sur la machine cible** (section suivante) |
| 2. Fusion et frontière | 0 erreur moteur sur 1 128 exécutions ; cas transfrontaliers (Tourcoing-Mouscron…) **100 %** dans la tolérance pour S1 et S2 | atteint |
| 3. Latence | `route` à 5 points : p50 **45-85 ms**, p95 **205-750 ms** (cible indicative ≤ 1 s au p95) ; durée médiane d'une génération complète 0,01 à 0,16 s | atteint en local |
| 4. `round_trip` | cas 1 point : **89 %** dans la tolérance, écart médian 9,7 % | atteint |
| 5. Profils, gravel | dans la tolérance : `foot` 91/90 %, `mtb` 87/89 %, `gravel` 86/88 %, `racingbike` 85/84 % (S1/S2) ; `hike` non mesuré dans le jeu de détours | atteint quantitativement ; **appréciation visuelle des traces gravel par l'utilisateur non faite** |
| 6. Stratégie de détours | ≥ 80 % atteint (S1 87 %, S2 88 %, voir ADR-0010) | atteint |

**Décision D1 (utilisateur, 2026-11-24)** : réserve levée, GraphHopper 11.0 retenu, sous condition que les mesures sur la machine cible tiennent dans ses ressources. Le plan B BRouter n'a pas lieu d'être activé. **Condition levée** le même jour (section suivante) ; l'estimation « au moins ~3,5 Go pour `-Xmx3g` » est remplacée par le dimensionnement mesuré ci-dessous (`-Xmx1536m`, limite 2 Go).

**Image** (point annexe du spike, tranché) : l'image communautaire `israelhikingmap/graphhopper:11.0` a servi au spike et aux mesures ; TR7 construit sa propre image depuis le jar de la release (voir Décision).

## Mesures sur la machine cible « dune » (ops, 2026-11-24)

Sources : `spike/graphhopper/measures-dune-20261124.txt` (service `-Xmx1g`), `measures-dune-20261124-xmx3g.txt` (service `-Xmx3g`), `import-hdf-be.log`. Valeurs marquées « ops » : relevées par ops, absentes de ces fichiers.

- **Machine** : Xeon E5-1650 v3, 12 cœurs, 64 Go ; Docker 26.1.5, Compose 2.26.1. Moteur mesuré : image communautaire, jar `graphhopper-web-11.0-SNAPSHOT`, OpenJDK 21.0.1.
- **Données** : extraits Geofabrik Nord-Pas-de-Calais, Picardie et Belgique (MD5 vérifiés, ops) ; fusion `osmium` en **41 s** (ops) ⇒ `.osm.pbf` de **1 076 132 805 octets**.
- **Import** (`-Xmx6g`) : **154 s** (303 s sur le poste local) ; tas utilisé **3,6 Go** en fin d'import ; conteneur au plus **6,2 Go** (pic échantillonné, plafonné par le tas) ; **3 293 490 nœuds, 4 272 021 arêtes** ; graphe **320 Mo** sur disque.
- **Service** : prêt (`/health`) en **3 s** ; RAM du conteneur après charge **3,24 Go** avec `-Xmx3g` (ops) et **1,2 Go** avec `-Xmx1g` ; **aucun OOM** ; `/info` annonce les 6 profils (`foot`, `hike`, `bike`, `racingbike`, `mtb`, `gravel`).
- **Latence `route`** (5 points + fermeture, 40 appels par profil, 0 erreur), p50 / p95 en ms :

| Profil | `-Xmx3g` | `-Xmx1g` |
|---|---|---|
| foot | 56 / 354 | 74 / 363 |
| hike | 46 / 307 | 58 / 307 |
| bike | 48 / 302 | 64 / 331 |
| racingbike | 49 / 341 | 51 / 283 |
| mtb | 34 / 238 | 56 / 272 |
| gravel | 53 / 378 | 69 / 353 |

- **`round_trip`** (10, 30, 60 km ; `foot`, `bike`, `gravel` ; 8/8 réussis par cas) : p50 **8 à 108 ms** ; rapport distance obtenue / cible, médiane par cas **0,83 à 1,28**.
- **Disque** : **~2,4 Go** au total (extraits, fichier fusionné, graphe ; ops).

Lecture : la cible indicative (≤ 1 s au p95 par appel) est tenue avec une large marge, même à `-Xmx1g` : un tas de 1 Go suffit au service (graphe de 320 Mo), la différence de latence entre 1 Go et 3 Go est faible. L'import demande nettement plus que le service.

## Chiffrage d'exploitation (décision relayée par coord, 2026-11-24)

| Poste | Valeur retenue | Base |
|---|---|---|
| Service : tas Java | **`-Xmx1536m`** (`ROUTING_XMX`) | 1 Go suffit à la mesure ; marge de 50 % |
| Service : limite mémoire du conteneur | **2 Go** (`mem_limit`, `ROUTING_MEM_LIMIT`) | 1,2 Go mesurés à `-Xmx1g` après charge |
| Import mensuel : tas Java | **`-Xmx6g`** (conteneur d'import séparé, pendant l'import seulement) | 3,6 Go de tas utilisé, 6,2 Go de pic |
| Durée de la mise à jour mensuelle | **~4 min** de calcul (fusion 41 s + import 154 s), plus le téléchargement (~1,1 Go d'extraits) | mesures dune |
| Disque | **~2,4 Go** en régime ; pendant une mise à jour, l'ancien graphe reste jusqu'au succès et pour le retour arrière : prévoir **un graphe de plus (~0,3 Go)** et de la marge pour les nouveaux extraits | mesures dune ; surcoût de bascule estimé par la spec, non mesuré |
| Démarrage du service | **~3 s** | mesures dune |

Points à vérifier à la recette de TR7 (non mesurés tels quels) :
- la configuration retenue elle-même : **`-Xmx1536m` dans 2 Go**, RAM du conteneur après charge (cible : ≤ ~1,8 Go, aucune mise à mort par le noyau) ;
- l'image du projet (jar de la **release** 11.0 sur Temurin 21) au lieu du jar `SNAPSHOT` mesuré : rejouer le banc court (latence `route` par profil, `round_trip`) et vérifier que `/info` annonce les 6 profils ;
- la mise à jour mensuelle de bout en bout, y compris un **retour arrière** volontaire (`routing/update.sh`).

## Limites connues relevées au spike
- **Profil gravel** (avis d'ops sur l'échantillon) : il **évite surtout le bitume** — part de bitume Durbuy 62 → 29 %, Spa 85 → 60 %, Ypres 72 → 39 % — au prix de **+3 à +21 %** de distance ; la part de surface non revêtue **taguée** dans OSM reste faible (≤ 18 %), le modèle ne peut donc pas viser activement les chemins de gravier ; dans les Ardennes, il se rapproche du profil `mtb`. Limite acceptée pour la bêta ; **le jugement terrain revient à l'utilisateur** (appréciation de l'échantillon `spike/detours/out/echantillon/`).
- **Rectangle de `/info`** (`[-8.32, 48.83, 6.42, 54.60]` au spike) bien plus large que la zone couverte (bords des extraits, ferries) : inutilisable comme couverture. La spec utilise un rectangle configuré (`routing.coverage_bbox`, RB-1.3 ; depuis le lot Z1 du 2026-11-26, polygone `routing.coverage_polygon` dont le défaut est ce rectangle en 4 sommets), par défaut `[1.35, 48.83, 6.42, 51.55]` : son sud et son est reprennent ceux de `/info` sur les données réelles, pour que le diagnostic « rectangle contenu » soit vrai ; `/info` ne sert qu'au diagnostic.

## Conséquences
- Charge **ops** nouvelle : service Java (2 Go), chaîne mensuelle téléchargement → fusion → import (6 Go de tas pendant ~3 min) → bascule, avec retour arrière (`routing/update.sh`) ; image du moteur construite et mise à jour par le projet (jar de release épinglé, JRE épinglé).
- Charge moteur multipliée par les détours : bornée par le budget d'appels et la limite de générations simultanées (spec RB-S5).
- `DEM_BBOX` à élargir pour que les traces belges aient une altitude MNT.
- `routing.api_key` inutile en v1.

## Options écartées
- openrouteservice : boucle native, mais la plus lourde, sans profil gravel.
- Valhalla, OSRM : pas de boucle native ; OSRM impose un graphe par profil.
- Tout SaaS : écarté par l'arbitrage Q-R2.
- Deux graphes séparés (France, Belgique) : boucles transfrontalières impossibles, contraire à l'arbitrage.
