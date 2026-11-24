# ADR-0011 — Destinations sortantes : liste blanche d'env par service et secrets liés à leur destination

Statut : **Accepté** (arbitrage utilisateur post-review B3 ; extension aux fonds de carte arbitrée en re-review R1 ; précisions r3, r4 ; relayés par coord le 2026-11-24) · Specs : [admin-space](../specs/admin-space.md) EF-4.1, EF-4.2, EF-5.5, EF-5.6, EF-6.5, EF-6.6, EF-6.8 ; [gpx-route-builder](../specs/gpx-route-builder.md) RB-S3

## Contexte
Avec la priorité base > env (ADR-0008), un admin — ou une session admin volée — peut modifier `smtp.host`, `routing.base_url`, `DEM_URL` et les URL des fonds de carte. La review (B3) et la re-review (R1) ont montré qu'il pouvait alors :
1. recevoir le mot de passe SMTP en clair (hôte à lui, sécurité `none`, e-mail de test), y compris un secret défini par l'env ;
2. recevoir la clé de routage et **les points de tous les utilisateurs** ;
3. recevoir les coordonnées de **toutes** les traces via le rattrapage d'altitude ;
4. sonder le réseau interne avec « Tester » et l'e-mail de test ;
5. via un fond de carte `https://attaquant/{z}/{x}/{y}.png`, recevoir du **navigateur de chaque utilisateur** son IP et les tuiles consultées, donc les zones de ses traces (R1). La CSP `img-src https:` ne l'empêche pas.

## Décision
1. **Secret lié à sa destination** : la destination effective normalisée (schéma, hôte, port) est enregistrée avec le secret ; destination différente à l'usage ⇒ secret non envoyé ; changer la destination efface les secrets liés ; un secret d'env n'est envoyé qu'à la destination définie en env.
2. **SMTP `none` sans authentification** ; `starttls` sans repli en clair.
3. **Liste blanche `OUTBOUND_ALLOWED_HOSTS`** (env, hors registre), **une seule variable à entrées préfixées par service** :
   `routing=hôte:port`, `dem=hôte:port`, `smtp=hôte:port`, `tiles=hôte`.
   - une entrée n'autorise que **son** service (r3) ;
   - **port obligatoire** pour `routing`, `dem`, `smtp`, comparé exactement au port effectif (défaut du schéma compris) ; entrée sans port ⇒ démarrage refusé (r3) ;
   - `tiles` : hôte seul, `https` et 443 implicites ;
   - appliquée à l'écriture (400) **et** à chaque usage (refus + audit ; un fond de carte hors liste n'est pas envoyé au navigateur) ;
   - destinations définies en env implicitement autorisées **pour leur seul service** (r3) ; liste vide ⇒ seules celles-là ;
   - **tuiles par défaut** : sans entrée `tiles=`, les hôtes des fonds embarqués aujourd'hui ; la première entrée `tiles=` remplace cette liste ;
   - redirections jamais suivies ; comparaison sur le nom, sans résolution DNS.
4. **CSP `img-src` restreinte** (R1) aux hôtes de tuiles autorisés (`'self' data: blob:` + `https://<hôte>` pour chacun), calculée au démarrage depuis l'env ; `https:` disparaît.
5. **« Tester » et e-mail de test neutres** : ok/ko seulement ; **durée = délai du service** (`routing.timeout_s`, `DEM_TIMEOUT`, `smtp.timeout_s`), sonde interrompue et réponse rendue à cette échéance quel que soit le résultat (r4).

Principe inscrit dans la spec : toute URL ou tout hôte vers lequel partent des données d'utilisateurs ou un secret est une frontière de sécurité, **qu'il soit appelé par le serveur ou par le navigateur**.

## Conséquences
- Ajouter un service, un relais SMTP ou un fournisseur de tuiles exige une modification de l'env par l'opérateur (et un redémarrage pour les tuiles, la CSP étant calculée au démarrage) : l'admin applicatif ne peut plus, seul, diriger des données vers l'extérieur.
- Changer d'hôte SMTP ou de moteur oblige à ressaisir le secret.
- Mise à jour sans changement visible : sans entrée `tiles=`, les fonds actuels restent autorisés.
- Comparaison sur le nom : un nom autorisé dont l'attaquant contrôlerait la résolution DNS reste hors de portée (hors périmètre).

## Options écartées
- **Liste dédiée aux tuiles** (`TILE_ALLOWED_HOSTS`), permise par l'arbitrage R1 : écartée au profit d'une seule variable préfixée par service, plus simple à auditer, le préfixe donnant la même séparation entre services.
- Exempter les fonds de carte de la règle (risque accepté) : écarté par l'arbitrage R1.
- Sortir `DEM_URL` et `routing.base_url` du registre (env seul) : contraire à la gestion depuis l'admin ; la liste blanche donne la même garantie.
- Liste noire des adresses privées : inadaptée, les services légitimes sont internes.
- Proxy de tuiles côté serveur : masquerait l'IP des utilisateurs, mais hors périmètre v1 (charge, cache, conditions des fournisseurs).
