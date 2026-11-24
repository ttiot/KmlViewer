# ADR-0010 — Stratégie de détours pour atteindre la distance cible

Statut : **Proposé** — choix final au spike TR0 ; seuils (tolérance 15 %, accroche 50 m, recouvrement 25 %, budget 15 appels / 30 s) **acceptés** en 3e vague · Spec : [gpx-route-builder](../specs/gpx-route-builder.md) RB-4.4 à RB-4.7

## Contexte
Arbitrage Q-R1 v3 : la trace est **toujours** une boucle fermée passant par tous les points P1…Pn dans l'ordre ; si la boucle de base L0 (P1 → … → Pn → P1) est plus courte que la cible T, des **détours** l'allongent. Aucun moteur ne fait cela nativement : la logique vit dans l'appli (`app/services/routing/detours.py`), au-dessus d'appels `route` du moteur, ce qui la rend indépendante du moteur (ADR-0007).

Contraintes : ordre et fermeture préservés ; budget de 15 appels et 30 s ; trace praticable (peu d'allers-retours) ; reproductible à graine égale.

## Stratégies candidates

**S1 — Points de passage latéraux.** Pour un ou plusieurs tronçons Pi → Pi+1 (fermeture comprise), on insère un point technique décalé perpendiculairement au milieu du tronçon. Pour un tronçon de longueur à vol d'oiseau c et un allongement visé δ, décalage initial h = √(((c + δ)/2)² − (c/2)²), puis correction par la sécante selon la distance réellement routée (≤ 4 itérations). Le manque Δ est réparti sur les tronçons les plus longs ; le côté (gauche/droite) est tiré par la graine ; point non routable ⇒ autre côté, puis décalage réduit.
- Pour : ouvre la boucle (moins de recouvrement), formes régulières, nombre d'appels prévisible (1 par tronçon modifié et par itération).
- Contre : sensible au réseau local (un point décalé tombe dans un lac ou une impasse) ; peu de marge si les points sont très denses.

**S2 — Pétales.** On greffe sur la boucle de base une ou plusieurs boucles natives du moteur (`round_trip`) de longueur ≈ Δ / k, partant de points de l'utilisateur (choisis par la graine), insérées dans la trace à ce point.
- Pour : chaque pétale est une boucle « propre » produite par le moteur ; peu d'appels.
- Contre : la trace repasse par le point de greffe (forme en fleur), recouvrement possible au départ du pétale ; **dépend de `round_trip`, donc du moteur** : `detours.py` ne connaît que l'interface `RoutingProvider`, mais S2 exige que le provider implémente `round_trip`. Avec un moteur sans boucle native (plan B BRouter, ADR-0007), S2 et l'hybride imposent de **construire les pétales dans l'appli** (petite boucle par S1 autour du point de greffe), pas seulement le cas « 1 point » (M15).

**Hybride (proposé par défaut si S1 et S2 sont tous deux corrects au spike).** S1 d'abord ; si le budget s'épuise sans atteindre la tolérance, un pétale S2 comble le reste.

## Décision proposée
- Interface `DetourStrategy` : entrée = points, L0, Δ, profil, graine, `RoutingProvider`, budget ; sortie = liste ordonnée de points (utilisateur + techniques) ou de pétales, ou échec.
- Implémenter **S1** et **S2** de façon jetable au spike, mesurer, puis livrer en v1 la stratégie (ou l'hybride) retenue, réglable par `routing.detour_strategy`.

## Mesures du spike (jeu de ~10 cas sur l'emprise Hauts-de-France + Belgique : urbain, rural, relief (Ardennes), littoral, transfrontalier ; 1, 2, 5 et 15 points ; T de 1,2× à 3× D0 ; chaque profil)
- taux de générations dans la tolérance (cible proposée : ≥ 80 % des cas) ;
- taux de recouvrement médian et maximal ;
- appels consommés et latence totale ;
- appréciation visuelle (zigzags, éperons, voies absurdes) par l'utilisateur sur un échantillon.

Ces mesures sont des critères **de spike** (CA-R4s, CA-R6s, CA-R21 de la spec) : elles exigent GraphHopper réel et ne tournent pas en CI. Leurs résultats sont consignés ici à l'issue de TR0. La CI vérifie la logique avec un provider factice (m13).

## Conséquences
- Logique de géométrie et d'itération à tester finement avec un provider factice déterministe (spec CA-R4 à R10, R26) ; la variabilité entre graines se teste sur un cas construit qui admet plusieurs détours (« au moins 2 traces distinctes sur 10 graines »), pas sur une égalité stricte (m14).
- Qualité perçue de la fonction dépendante de ce choix : c'est le principal risque produit de la bêta.

## Options écartées
- Raccourcir une boucle trop longue en retirant ou déplaçant des points : contraire à l'arbitrage (tous les points, dans l'ordre).
- Itinéraires alternatifs du moteur (alternative routes) choisis pour leur longueur : allongement non contrôlable, limités en nombre, souvent quasi identiques.
- Optimisation globale (recherche de cycle de longueur cible dans le graphe) : hors de portée d'une bêta, et dépendante des internes du moteur.
