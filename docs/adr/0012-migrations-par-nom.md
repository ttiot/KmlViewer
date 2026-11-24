# ADR-0012 — Migrations de schéma repérées par nom

Statut : **Accepté** — choix délégué à la spec (re-review r2), entériné par l'utilisateur (relayé par coord le 2026-11-24) · Spec : [admin-space](../specs/admin-space.md) §7

## Contexte
`web-app/app/db.py` applique ses migrations par **position** : `PRAGMA user_version` retient le numéro de la dernière appliquée. Les deux specs ajoutent une dizaine de migrations, développées sur des branches parallèles. La review v3 (D1) a écarté les numéros réservés (ils cassent si les branches fusionnent dans un autre ordre) ; la spec v4 a donc attribué le numéro à la fusion. La re-review (r2) a montré la faille restante : la CI publie une image `unstable-<branche>` pour **chaque** branche (`.github/workflows/docker.yml`). Une base qui a tourné sur une image de branche (migration X en n° N) puis passe sur `main` (où N est une autre migration Y, X renumérotée N+1) **saute Y sans aucune erreur**.

La re-review laissait le choix entre : (a) migrations repérées par nom ; (b) une règle écrite « une image de branche ne tourne jamais sur une base persistante » plus des migrations idempotentes qui vérifient le schéma.

## Décision
**(a) Migrations repérées par nom**, en gardant (b) en défense en profondeur :
- les 5 migrations existantes restent positionnelles (`user_version` ≤ 5), inchangées ;
- toute nouvelle migration a un nom unique et stable ; table `schema_migrations(name PRIMARY KEY, applied_at)` ;
- au démarrage : positionnelles jusqu'à 5, puis chaque migration nommée absente de la table, dans l'ordre de la liste du code, chacune dans sa transaction avec son enregistrement ;
- nom présent en base mais inconnu du code ⇒ `WARNING`, aucune annulation automatique ;
- **« publiée » = fusionnée sur la branche principale** ; une migration publiée n'est jamais modifiée ; une migration modifiée **avant** fusion (donc peut-être déjà dans une image `unstable-<branche>`) **change de nom** (suffixe), pour être appliquée dans sa nouvelle version partout (v5-n3) ; chaque migration est idempotente et vérifie le schéma ;
- deux migrations dépendantes vivent dans la même tâche ;
- règle d'exploitation conservée : pas d'image de branche sur une base persistante de production.

## Raisons
- Avec (b) seule, la sûreté repose sur une discipline et ne détecte rien : la migration sautée passe inaperçue jusqu'à l'erreur SQL en production. Avec (a), l'ordre de fusion devient indifférent et une base « en avance » est signalée.
- Coût faible : une table, une boucle, quelques lignes ; les migrations existantes ne bougent pas.
- L'idempotence reste exigée parce qu'elle couvre aussi les reprises après une migration interrompue.

## Conséquences
- Tâche T0 (admin-space) avant toute tâche qui ajoute une migration, dans les deux specs.
- Les noms de migration deviennent une interface stable (ne jamais renommer une migration publiée).
- Le test commun couvre l'application dans un ordre différent de la liste (CA-47).

## Options écartées
- Numéros réservés à l'avance (D1) : cassent si les branches fusionnent dans un autre ordre.
- Numéro attribué à la fusion (spec v4) : saute silencieusement une migration sur une base passée par une image de branche.
- (b) seule : voir « Raisons ».
- Outil de migration externe (Alembic…) : dépendance et changement de modèle disproportionnés pour SQLite sans ORM.
- Empreinte du code de chaque migration stockée en base, avec `WARNING` si elle diffère (v5-n3) : un reformatage sans effet déclencherait des alertes ; le renommage obligatoire suffit.
