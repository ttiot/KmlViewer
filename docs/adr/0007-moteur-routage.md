# ADR-0007 — Moteur de routage : GraphHopper auto-hébergé

Statut : **Accepté sous réserve du spike TR0** (auto-hébergement : arbitrage Q-R2 ; GraphHopper : 2e vague ; emprise Hauts-de-France + Belgique : 4e vague, relayées par coord le 2026-11-24). Périmètre belge « pays entier » : recommandation de cet ADR.
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
- service compose `graphhopper` sous un profil dédié (`docker compose --profile routing up -d`), construit depuis une **release épinglée** (ops tranche la méthode de build), aucun port publié, conteneur durci ;
- destination du moteur (`routing.base_url`) soumise à `OUTBOUND_ALLOWED_HOSTS` (ADR-0011) : l'adresse interne du service est écrite en env par l'opérateur, ce qui l'autorise implicitement ;
- santé sondée par la tâche `routing_health` de l'ordonnanceur de fond (premier passage au démarrage, puis à intervalle), jamais à la demande d'une page ; avant le premier sondage réussi, fonction indisponible (spec RB-1.2, R3, n8) ;
- **un seul graphe** construit à partir d'un `.osm.pbf` **fusionné** Hauts-de-France + Belgique (voir ci-dessous) ;
- mise à jour **mensuelle** : téléchargement, fusion, import dans un dossier neuf, bascule ; l'ancien graphe sert jusqu'au succès ;
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

## Conséquences
- Charge **ops** nouvelle : service Java, chaîne mensuelle téléchargement → fusion → import → bascule, ressources à dimensionner.
- Charge moteur multipliée par les détours : bornée par le budget d'appels et la limite de générations simultanées (spec RB-S5).
- `DEM_BBOX` à élargir pour que les traces belges aient une altitude MNT.
- `routing.api_key` inutile en v1.

## Options écartées
- openrouteservice : boucle native, mais la plus lourde, sans profil gravel.
- Valhalla, OSRM : pas de boucle native ; OSRM impose un graphe par profil.
- Tout SaaS : écarté par l'arbitrage Q-R2.
- Deux graphes séparés (France, Belgique) : boucles transfrontalières impossibles, contraire à l'arbitrage.
