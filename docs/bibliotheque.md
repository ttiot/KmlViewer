# Bibliothèque d'activités, tronçons et authentification

## Ce que ça fait

| Fonction | Où | Détail |
|---|---|---|
| Dossier surveillé | `INBOX_DIR` | Chaque `.gpx`/`.kml`/`.kmz` déposé est parsé, résumé puis stocké en SQLite. Les fichiers ne sont jamais déplacés ni modifiés. |
| Import depuis l'UI | Tableau de bord → « Importer », ou glisser-déposer du visualiseur (case « Enregistrer dans ma bibliothèque ») | Même pipeline que le dossier surveillé. |
| Tableau de bord | `/dashboard` | km, D+ et temps par semaine / mois / année, records, liste des activités. |
| Tronçons | `/segments` | Un tronçon se définit en deux clics sur une trace ; il est recherché dans toutes les traces, existantes et futures. |
| Correction d'altitude | `DEM_URL` | D+ recalculé sur un MNT (API OpenTopoData). Le D+ GPS reste conservé à côté. |
| Utilisateurs | OIDC + `/admin/users` | Chaque activité et chaque tronçon appartient à un utilisateur et n'est visible que de lui. |

## Organisation du code

```
web-app/app/
├── __init__.py            factory : câblage uniquement
├── config.py              toute la configuration (variables d'environnement)
├── db.py                  schéma SQLite et connexions
├── security.py            en-têtes HTTP, contrôle d'origine (CSRF), reverse proxy
├── cli.py                 commandes d'exploitation
├── auth/
│   ├── oidc.py            client OIDC, login / callback / logout
│   ├── guard.py           contrôle d'accès global (fermé par défaut)
│   └── users.py           persistance des comptes
├── api/                   une famille de routes par fichier, aucune logique métier
│   ├── activities_routes.py · dashboard_routes.py · segments_routes.py · users_routes.py
└── services/              logique métier, sans dépendance à Flask
    ├── geo.py             distances, projection, D+ (numpy)
    ├── activity_stats.py  trace -> résumé (fonctions pures)
    ├── activity_store.py  SQL des activités, filtré par utilisateur
    ├── ingest_service.py  pipeline d'ingestion
    ├── inbox_watcher.py   dossier surveillé
    ├── segment_service.py recherche des tronçons
    ├── dem_service.py     client MNT
    └── dashboard_service.py agrégats et records
```

Règle de dépendance : `api` → `services` → `db`. Les services reçoivent une connexion et un
mapping de configuration, ce qui les rend testables et réutilisables hors requête HTTP
(le watcher et la CLI s'en servent tels quels).

## Schéma de base et interface

- **Migrations** : `app/db.py` porte une liste `MIGRATIONS` appliquée dans l'ordre aux bases
  existantes (`PRAGMA user_version`). `SCHEMA` décrit toujours l'état final. Pour faire évoluer
  le schéma : modifier `SCHEMA` et ajouter une migration en fin de liste, sans jamais retoucher
  une migration déjà publiée.
- **Thème** : toutes les couleurs sont des jetons définis dans `static/css/theme.css`, en clair
  et en sombre ; les autres feuilles ne référencent que des rôles (`--surface`, `--accent`…).
  Le mode suit le système, avec une bascule mémorisée dans le navigateur (`static/js/theme.js`).

## Dossier surveillé

```
inbox/
├── jmichaux/            -> bibliothèque du compte dont le username (ou l'email) est « jmichaux »
│   └── 2026/sortie.gpx     (sous-dossiers libres)
└── sortie.gpx           -> compte INBOX_DEFAULT_USER (ignoré si non défini)
```

- Scrutation toutes les `INBOX_SCAN_INTERVAL` secondes (polling : fiable sur bind mount et NFS).
- Un fichier modifié depuis moins de 5 s est laissé pour le passage suivant (copie en cours).
- Dédoublonnage par SHA-256 du contenu, par utilisateur.
- Un dossier sans compte actif correspondant reste en attente et est repris dès que le compte existe.
- Liens symboliques et fichiers cachés ignorés ; le volume peut être monté en lecture seule.
- Diagnostic : `GET /api/inbox` (admin) ou `flask --app kml_viewer inbox-scan`.

## Calculs

- **D+ / D-** : lissage sur 5 points puis hystérésis (4 m en GPS, 2 m sur MNT). Sommer les
  variations brutes point à point additionne le bruit du capteur.
- **Temps en mouvement** : intervalles où la vitesse dépasse 0,5 m/s.
- **Records** : meilleur temps sur 1, 5, 10, 21,1 et 42,2 km par fenêtre glissante.
- **Tronçons** : tolérance latérale de 30 m, sens de parcours respecté, plusieurs passages par
  trace possibles ; abandon après 150 m parcourus hors du tracé. Constantes en tête de
  `segment_service.py`.

## Authentification OIDC (FusionAuth)

Flux authorization code + PKCE, client confidentiel. Dans FusionAuth, sur l'application :

| Réglage | Valeur |
|---|---|
| Authorized redirect URLs | `https://<hôte>/auth/callback` et `https://<hôte>/auth/logged-out` |
| Enabled grants | Authorization Code |
| Client authentication | Required |
| Rôle applicatif (optionnel) | `admin`, à reporter dans `OIDC_ADMIN_ROLE` |
| JWT → Enabled, clés « Access token » et « Id token » | une clé **asymétrique** (RS256 ou ES256) |

Deux pièges constatés sur FusionAuth 1.69 :

- Par défaut l'ID token est signé en HS256 : le JWKS est alors vide et la vérification de
  signature échoue (« L'authentification a échoué », avec le motif dans les logs). Il faut
  générer une clé RS256/ES256 (Settings → Key Master) et l'associer à l'application.
- `OIDC_ISSUER` est l'URL de base de FusionAuth (celle qui sert
  `/.well-known/openid-configuration`), pas le champ « Issuer » du tenant.

Les rôles applicatifs ne figurent pas dans l'ID token : ils sont lus via `/userinfo`, puis à
défaut dans l'access token après vérification de sa signature, de son audience et de son sujet.

Puis renseigner `.env` (modèle : `.env.example`) : `SECRET_KEY`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`,
`OIDC_CLIENT_SECRET`, `SESSION_COOKIE_SECURE=1`, `TRUST_PROXY=1`.

Gestion des comptes :

- Création à la première connexion à partir des claims `sub`, `email`, `preferred_username`, `name`.
- Sans `OIDC_ADMIN_ROLE` : le premier compte connecté est admin et gère les autres dans `/admin/users`.
- Avec `OIDC_ADMIN_ROLE` : le droit admin suit le claim `roles` à chaque connexion.
- `OIDC_AUTO_ACTIVATE=0` : un nouveau compte attend l'activation par un admin.
- Dépannage : `flask --app kml_viewer user-admin <login>` donne le rôle admin en ligne de commande.

Sans `OIDC_ISSUER`, l'application tourne en mono-utilisateur local **sans authentification**.

## Sécurité

- Accès fermé par défaut : toute route exige une session, hors liste blanche explicite
  (`auth/guard.py`). Un test parcourt toutes les routes pour le garantir.
- Cloisonnement : chaque requête SQL métier filtre sur l'utilisateur courant ; l'accès à la
  ressource d'un autre répond 404.
- Session : cookie signé `HttpOnly`, `SameSite=Lax`, `Secure` en HTTPS, 12 h ; régénérée au login.
  Démarrage refusé si l'OIDC est actif avec la `SECRET_KEY` par défaut.
- CSRF : les requêtes modifiantes dont l'origine n'est pas celle de l'appli sont refusées.
- XML : parsing via `defusedxml` (XXE, bombes d'entités).
- Fichiers : noms de stockage dérivés du SHA-256, jamais du nom fourni ; droits `0600`.
- En-têtes : CSP, `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`.
- Scripts : la CSP n'autorise que ceux servis par l'application (`script-src 'self'`). Les
  bibliothèques front sont embarquées dans `static/vendor` (aucun CDN) et aucun template ne
  contient de script ou de gestionnaire inline ; un test le vérifie. Les données issues des
  fichiers (noms, descriptions) sont toujours insérées comme texte, jamais comme HTML.
- KMZ : décompression bornée (64 Mo), seul le document KML est lu.
- Erreurs : une erreur inattendue de l'API est journalisée ; le client ne reçoit qu'un message générique.
- Conteneur : gunicorn, utilisateur non-root, racine en lecture seule, capacités supprimées,
  port publié sur `127.0.0.1` (à placer derrière un reverse proxy TLS).

Limite connue : `style-src` garde `'unsafe-inline'`, les templates utilisant des attributs `style`.

## Correction d'altitude

Le profil compose `dem` fournit tout le nécessaire, en local (les traces ne sortent pas de la machine) :

```bash
# .env
DEM_URL=http://opentopodata:5000/v1/copernicus30
DEM_BBOX=41,-6,52,10        # lat_min,lon_min,lat_max,lon_max : France métropolitaine, ~9 Go

docker compose --profile dem up -d
```

- `dem-tiles` : tâche ponctuelle qui télécharge les dalles Copernicus GLO-30 de l'emprise
  (bucket public AWS, ~45 Mo par degré carré) dans le volume `dem-data`. Relançable sans
  risque : les dalles présentes ne sont pas retéléchargées, et aucun accès Internet n'est
  requis une fois qu'elles sont là. Pour étendre la zone, élargir `DEM_BBOX` et relancer.
- `opentopodata` : API d'altitude, construite depuis le dépôt du projet (pas d'image publiée),
  épinglée sur un commit. Aucun port publié : seul `kml-viewer` peut l'appeler.

Un point tous les 20 m est interrogé, le reste est interpolé. Un import depuis l'interface
n'attend pas le MNT : l'activité est enregistrée avec l'altitude GPS, puis corrigée par la
tâche de fond, réveillée aussitôt (`DEM_BACKFILL_BATCH` activités par cycle, 25 par défaut).
Il en va de même si le service est indisponible au moment de l'ingestion (démarrage, panne). Une trace hors des dalles disponibles garde
son altitude GPS ; le bouton montagne du tableau de bord relance la correction à la demande.

## Sauvegarde

Deux choses sont à conserver : la base SQLite et les fichiers d'origine (`/data/files`).

```bash
# Copie cohérente de la base, application en marche ; --keep limite le nombre de copies gardées
docker compose exec kml-viewer flask backup --keep 7      # -> /data/backups/kmlviewer-<date>.sqlite3

# Archive du volume complet (base sauvegardée + fichiers d'origine)
docker run --rm -v kmlviewer_kml-viewer-data:/data:ro -v "$PWD":/out alpine \
    tar czf /out/kmlviewer-data.tgz -C /data backups files
```

Le nom du volume dépend du dossier du projet (`docker volume ls`). Ne pas copier
`kmlviewer.sqlite3` à chaud avec `cp` : en mode WAL, la copie peut être incohérente.
Restauration : arrêter l'application, remettre la copie à la place de
`/data/kmlviewer.sqlite3` (supprimer les fichiers `-wal` et `-shm`), restaurer `files/`, relancer.

## Dépendances et image

- `requirements.in` liste les dépendances directes ; `requirements.txt` est le verrou complet,
  transitives et empreintes comprises, installé avec `--require-hashes`. Régénération :
  `uv pip compile requirements.in --generate-hashes --python-version 3.15 --upgrade -o requirements.txt`
  (idem pour `requirements-dev.in`).
- L'image applique les correctifs de sécurité Debian au build et ne contient ni `pip` ni les tests.
- Dependabot propose chaque semaine les montées de version (Python, image de base, actions).

## Intégration continue

`.github/workflows/docker.yml` enchaîne lint (ruff), tests avec seuil de couverture, audit des dépendances, build, essai de l'image
en conditions de production (racine en lecture seule, sans capacités), scan Trivy, puis
publication multi-architecture sur GHCR et Docker Hub. Rien n'est publié si une étape échoue.

| Déclencheur | Tags publiés |
|---|---|
| Tag git `vX.Y.Z` | `X.Y.Z`, `X.Y`, `X`, `latest` |
| Branche `main` | `unstable` |
| Autre branche | `unstable-<branche>` |
| Chaque lundi | la dernière release est reconstruite sans cache, pour embarquer les correctifs de l'image de base |

`/api/health` renvoie la version embarquée dans l'image.

## Tests

```bash
cd web-app
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m ruff check .
python -m pytest --cov=app
```
