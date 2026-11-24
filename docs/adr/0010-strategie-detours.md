# ADR-0010 — Stratégie de détours pour atteindre la distance cible

Statut : **Accepté** — stratégie **hybride S1 puis pétale S2**, jusqu'à 2 essais de graine supplémentaires (décision D2), seuil de recouvrement 25 % à partir de 3 points et 40 % à 2 points (D3), avertissement et suggestion d'ajout de point pour 2 points hors tolérance (D4) ; décisions de l'utilisateur relayées par coord le 2026-11-24, après le spike TR0 · Spec : [gpx-route-builder](../specs/gpx-route-builder.md) RB-4.4 à RB-4.7, RB-4.11 à RB-4.14

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

## Décision proposée (avant spike, conservée pour l'historique)
- Interface `DetourStrategy` : entrée = points, L0, Δ, profil, graine, `RoutingProvider`, budget ; sortie = liste ordonnée de points (utilisateur + techniques) ou de pétales, ou échec.
- Implémenter **S1** et **S2** de façon jetable au spike, mesurer, puis livrer en v1 la stratégie (ou l'hybride) retenue, réglable par `routing.detour_strategy`.

## Mesures du spike (jeu de ~10 cas sur l'emprise Hauts-de-France + Belgique : urbain, rural, relief (Ardennes), littoral, transfrontalier ; 1, 2, 5 et 15 points ; T de 1,2× à 3× D0 ; chaque profil)
- taux de générations dans la tolérance (cible proposée : ≥ 80 % des cas) ;
- taux de recouvrement médian et maximal ;
- appels consommés et latence totale ;
- appréciation visuelle (zigzags, éperons, voies absurdes) par l'utilisateur sur un échantillon.

Ces mesures sont des critères **de spike** (CA-R4s, CA-R6s, CA-R21 de la spec) : elles exigent GraphHopper réel et ne tournent pas en CI. Leurs résultats sont consignés ici à l'issue de TR0. La CI vérifie la logique avec un provider factice (m13).

## Résultats du spike TR0 (poste local, 2026-11-24)

Jeu : 15 cas (Lille, Bruxelles, Artois, littoral d'Opale, Ardennes, transfrontalier), 1, 2, 5 et 15 points, cible de 1,2× à 3× D0, profils `foot`, `mtb`, `gravel`, `racingbike`, 3 graines par cas ; 1 128 exécutions, 0 erreur moteur. Détail : `spike/detours/out/summary.md`.

**Vue d'ensemble (≥ 2 points, 528 exécutions par stratégie)**

| | S1 points latéraux | S2 pétales |
|---|---|---|
| Dans la tolérance ±15 % | **87 %** | **88 %** |
| Écart médian à la cible | 6,2 % | 4,8 % |
| Recouvrement médian | 0,18 | 0,21 |
| Appels au moteur, médiane / max | 2 / 5 | 3 / 13 |
| Ordre et fermeture | 0 violation | 0 violation |

1 point (`round_trip` natif, identique pour les deux) : 89 %, écart médian 9,7 %.

**Ventilations (dans la tolérance, S1 / S2)**
- Zone : Lille 100/100, Bruxelles 100/100, transfrontalier 100/100, Artois 87/96, **littoral 88/50**, **Ardennes 57/64**.
- Nombre de points : 15 pts 100/100, 5 pts 97/95, **2 pts 74/76**.
- Facteur de cible : 1,2× 98/89, 1,5× 82/89, 2× 83/87, 3× 84/85.
- Profil : foot 91/90, mtb 87/89, gravel 86/88, racingbike 85/84.

**Recouvrement** : 36 % des traces à ≥ 2 points dépassent le seuil de 25 % ; **60 % des traces à 2 points**, contre 16 % à 5 points et plus. À 2 points, la boucle de base est un aller-retour (recouvrement 0,47), que les détours ramènent à 0,32 en médiane. Part des traces au-dessus du seuil, 2 points / 5 points et plus : 25 % ⇒ 60 / 16 % ; 35 % ⇒ 45 / 4 % ; 40 % ⇒ 38 / 2 % ; 50 % ⇒ 6 / 0 %.

**Analyse complémentaire (spec, à partir de `results.jsonl`, non mesurée directement)** :
- **Borne haute de l'hybride** « S1, puis S2 si S1 est hors tolérance » : **95 %** dans la tolérance à ≥ 2 points, appels médians 2, **max 14** (sous le budget de 15, mais sans marge). Calculée en combinant des exécutions indépendantes de S1 et S2 sur le même cas : l'hybride réel, où S2 part du résultat de S1 avec le budget restant, doit être **vérifié** (CI avec le provider factice, puis quelques cas réels).
- À 2 points : Ardennes S1 25 % / S2 46 % ⇒ hybride ≤ **54 %** ; littoral 88 / 50 ⇒ hybride ≤ 94 % ; Artois 56 / 85 ⇒ ≤ 92 %.
- **Plusieurs graines** : avec 3 graines par cas, au moins une trace dans la tolérance pour 88 % des cas avec S1 seule, **99 %** avec l'hybride (borne haute).

## Décision (utilisateur, 2026-11-24, sur recommandation de spec)

1. **Stratégie : hybride S1 puis pétale S2 en repli** (D2 ; lecture de dev, confirmée par l'analyse ci-dessus). S1 d'abord : moins d'appels, meilleur recouvrement, et S2 échoue sur le littoral (50 %) ; S2 en repli comble les cas où S1 échoue (Ardennes, Artois). Le budget de 15 appels est conservé ; l'hybride réel doit tenir sous ce budget.
   - *Alternative S1 seule* : plus simple, 87 %, au plus 5 appels, sans dépendance à `round_trip` (plan B BRouter plus simple) ; échoue sur les 2 points en relief (25 %).
   - *Alternative S2 seule* : 88 %, mais 50 % sur le littoral et jusqu'à 13 appels.
2. **Essais de graine supplémentaires** (D2) dans la limite du budget restant : quand la trace est hors tolérance et qu'il reste assez d'appels et de temps, relancer la stratégie avec la graine suivante (au plus 2 essais de plus, 3 graines au total comme au spike) et garder la meilleure trace.
3. **Seuil de recouvrement différencié par nombre de points** (D3) : 25 % à partir de 3 points, **40 %** à 2 points ; le seuil unique de 25 % avertirait sur 60 % des traces à 2 points, où l'aller-retour est structurel.
4. **2 points en relief ou sur le littoral** (D4) : sous 80 % en relief même avec l'hybride (≤ 54 % dans les Ardennes). En v1 : avertir clairement et **suggérer d'ajouter un point de passage** (à 5 points, 95 à 97 %), plutôt que d'investir dans une stratégie supplémentaire pendant la bêta.

## Conséquences
- Logique de géométrie et d'itération à tester finement avec un provider factice déterministe (spec CA-R4 à R10, R26) ; la variabilité entre graines se teste sur un cas construit qui admet plusieurs détours (« au moins 2 traces distinctes sur 10 graines »), pas sur une égalité stricte (m14).
- Qualité perçue de la fonction dépendante de ce choix : c'est le principal risque produit de la bêta.

## Options écartées
- Raccourcir une boucle trop longue en retirant ou déplaçant des points : contraire à l'arbitrage (tous les points, dans l'ordre).
- Itinéraires alternatifs du moteur (alternative routes) choisis pour leur longueur : allongement non contrôlable, limités en nombre, souvent quasi identiques.
- Optimisation globale (recherche de cycle de longueur cible dans le graphe) : hors de portée d'une bêta, et dépendante des internes du moteur.
