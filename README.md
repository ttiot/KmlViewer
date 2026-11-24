[![Build and Push Docker Image](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml/badge.svg)](https://github.com/ttiot/KmlViewer/actions/workflows/docker.yml)

# 🗺️ KmlViewer

Application web auto-hébergée pour visualiser, analyser et archiver des traces GPS (KML, KMZ, GPX) :
un visualiseur cartographique, et une bibliothèque d'activités avec tableau de bord et tronçons chronométrés.

## 🚀 Démarrage rapide

### Avec Docker (Recommandé)

1. **Cloner et lancer l'application** :

   ```bash
   git clone https://github.com/ttiot/KmlViewer
   cd kmlViewer
   ./start.sh
   ```

2. **Accéder à l'application** :
   Ouvrez votre navigateur à l'adresse : **<http://localhost:8080>**

3. **Arrêter l'application** :

   ```bash
   ./stop.sh
   ```

### Tests

```bash
cd web-app
pip install --require-hashes -r requirements.txt -r requirements-dev.txt
python -m ruff check .
python -m pytest --cov=app
```

## 🏃 Bibliothèque, tableau de bord, tronçons et comptes

Dossier surveillé, import depuis l'interface, statistiques par semaine / mois / année, tronçons
personnels chronométrés, correction d'altitude par MNT, authentification OIDC (FusionAuth),
sécurité et sauvegarde : voir [docs/bibliotheque.md](docs/bibliotheque.md).
Configuration : copier `.env.example` en `.env` et lire [la section Configuration](#-configuration).
Procédures d'exploitation (perte de clé, perte d'un second facteur, bascule de mode, mise à jour,
horloge, mémoire) : [docs/exploitation.md](docs/exploitation.md).

## ✨ Visualiseur

### 📁 Fichiers

- **Formats** : KML, KMZ et GPX, jusqu'à 16 Mo, plusieurs fichiers à la fois (glisser-déposer ou sélection)
- **Fichiers d'exemple** : les traces placées dans `web-app/sample_files/` sont proposées dans la barre
  latérale, à tous les comptes
- **Multi-fichiers** : chaque fichier a sa case d'affichage et son onglet ; l'onglet actif alimente
  la liste des points, l'analyse, l'édition et l'export
- **Modes d'affichage** : double unité (km/h + kts, ft + m) ou unité simple

### 🗺️ Carte

- **5 fonds de carte** : OpenStreetMap, Satellite (Esri), Terrain (OpenTopoMap), CartoDB Positron, CartoDB Dark Matter
- Traces, polygones et points aux couleurs des styles KML, popups de détail
- **Navigation entre les points** : flèches ← → du clavier, boutons Précédent/Suivant, clic dans la liste ou sur un marqueur

### 📊 Analyse

- **Stats, Élévation, Vitesse** : distance, durée et temps en mouvement mesurés, vitesses, dénivelés, profils en graphique (rien n'est estimé : une trace non horodatée n'a ni durée ni vitesse)
- **Avancé** (à la demande) : arrêts détectés, segments homogènes, zones de vitesse, pentes du terrain

### ✏️ Édition

Les modifications portent sur le fichier affiché, dans le navigateur ; l'export les enregistre.

- **Ajouter un point** : bouton, puis clic sur la carte, puis nom et description
- **Modifier / supprimer un point** : bouton, puis clic sur le point (carte ou liste) ; Échap annule
- **Simplifier les traces** : algorithme de Douglas-Peucker, tolérance réglable en mètres

### 📤 Export

GPX, CSV, GeoJSON ou KML, avec nom de fichier au choix. Les points de trace peuvent être
exclus du CSV.

## 🛠️ Scripts utilitaires

| Script | Description |
|--------|-------------|
| `./start.sh` | Démarre l'application avec Docker |
| `./stop.sh` | Arrête l'application |
| `./restart.sh` | Redémarre le conteneur **sans relire `.env`** |
| `./logs.sh` | Affiche les logs en temps réel |
| `./cleanup.sh` | Nettoyage complet du projet, **données comprises** (demande confirmation) |

Ces scripts sont des **outils de développement local**, pas un mode de déploiement. En production, la stack est
gérée par Ansible : [Déploiement géré par Ansible](#déploiement-géré-par-ansible).

Commandes d'exploitation (`docker compose exec kml-viewer flask <commande>`) :

| Commande | Description |
|----------|-------------|
| `backup [--keep N]` | Copie cohérente de la base dans `/data/backups` |
| `inbox-scan` | Passe une fois sur le dossier surveillé. Refuse de tourner (« Un autre processus… ») tant que l'application servie détient le verrou du dossier, c'est-à-dire en temps normal : le dossier est déjà scruté toutes les `INBOX_SCAN_INTERVAL` secondes |
| `recompute-stats` | Recalcule distance, durées et records des activités déjà stockées (à lancer après une mise à jour qui corrige ces calculs) |
| `user-admin <login>` | Donne le rôle admin et active le compte (c'est ainsi que se désigne le premier admin) ; `--revoke` retire le rôle. Sur un compte à mot de passe sans second facteur : refusé |
| `user-invite --admin <e-mail>` | Amorçage d'une instance à comptes à mot de passe : émet un jeton de 24 h à usage unique et affiche son lien (**consommation non livrée**, voir [Non livré](#non-livré)). `--account <login>` reprend l'amorçage d'un compte existant sans facteur |
| `user-mfa-reset <login>` | Perte de tous les seconds facteurs d'un compte à mot de passe : retire ses facteurs, ferme ses sessions, le rétrograde s'il est admin |
| `secrets-rotate` | Rechiffre les secrets avec la première clé de `SECRETS_KEY` (rotation) |
| `secrets-reset [--yes]` | Clé `SECRETS_KEY` perdue : efface tous les secrets chiffrés (à ressaisir) |
| `settings-list` | Valeur effective et source (base / env / défaut) de chaque réglage administrable |
| `settings-reset <clé>` ou `--all` | Supprime la valeur en base d'un réglage (ou de tous) : retour à la valeur d'env, sinon au défaut |

Chaque commande qui modifie quelque chose est inscrite au journal d'audit (acteur « ligne de
commande »). Détails et procédures : [docs/exploitation.md](docs/exploitation.md).

## 🏗️ Architecture

```text
web-app/
├── kml_viewer.py          point d'entrée (gunicorn kml_viewer:app)
├── app/
│   ├── __init__.py        factory Flask
│   ├── errors.py          réponses d'erreur JSON, sans détail interne
│   ├── security.py        CSP, en-têtes, contrôle d'origine (CSRF)
│   ├── auth/              modes d'authentification, OIDC, sessions, jetons, invitations, mots de passe
│   ├── settings/          registre des réglages administrables (bornes) et valeur effective base > env > défaut
│   ├── secrets_store.py   secrets chiffrés (Fernet), rotation
│   ├── outbound.py        liste blanche des destinations sortantes (OUTBOUND_ALLOWED_HOSTS)
│   ├── mailer.py          envoi d'e-mails (SMTP), file d'envoi
│   ├── background.py      ordonnanceur de fond (dossier surveillé, altitude, purge d'audit)
│   ├── ratelimit.py       limiteur de débit en mémoire
│   ├── audit.py           journal d'audit
│   ├── api/               routes REST (visualiseur, analyse, export, bibliothèque, tronçons, invitations)
│   ├── services/          parsing KML/GPX/KMZ, analyse, ingestion, MNT
│   ├── db.py              SQLite et migrations nommées
│   └── cli.py             commandes d'exploitation
├── static/
│   ├── js/viewer/         visualiseur : core, prefs, map, settings, payload, points, files, opener,
│   │                      analysis, editor, library, main
│   ├── js/                pages bibliothèque (dashboard, segments, users) et utilitaires
│   └── vendor/            Leaflet, Bootstrap, Chart.js, Font Awesome (embarqués, sans CDN)
├── templates/
└── tests/
```

- **Backend** : Python 3.15, Flask, gunicorn, SQLite, numpy ; dépendances verrouillées avec empreintes
- **Frontend** : JavaScript sans framework ni étape de build ; toute donnée issue d'un fichier est
  insérée comme texte, et la CSP n'autorise que les scripts de l'application
- **Déploiement** : image non-root, système de fichiers en lecture seule, port publié sur
  `127.0.0.1:8080` (à placer derrière un reverse proxy TLS)

### API du visualiseur

| Route | Rôle |
|-------|------|
| `POST /api/upload`, `POST /api/multi-upload` | Parse un ou plusieurs fichiers |
| `GET /api/sample-files`, `GET /api/load-sample/<nom>` | Fichiers d'exemple |
| `POST /api/analysis/*` | Analyses de trajectoire |
| `POST /api/editor/simplify-trace` | Simplification d'une trace |
| `POST /api/export/<gpx\|csv\|geojson\|kml>` | Export |
| `GET /api/health` | Santé et version |
| `GET /api/stats` | Temps d'exécution cumulés (admin) |

L'API de la bibliothèque est décrite dans [docs/bibliotheque.md](docs/bibliotheque.md).

## 🔧 Configuration

La configuration vient de trois sources, de la plus forte à la plus faible :

1. **la base** (réglages administrables, voir plus bas) : une valeur posée par un admin prime ;
2. **les variables d'environnement** (`.env`, modèle commenté : `.env.example`) ;
3. **le défaut** du code.

Seuls les *réglages administrables* suivent cette règle. Les variables d'amorçage et de sécurité
(`AUTH_MODE`, `SECRET_KEY`, `SECRETS_KEY`, `PUBLIC_URL`, `OUTBOUND_ALLOWED_HOSTS`, `TRUSTED_HOSTS`…)
ne se règlent **que** dans l'environnement : l'administration ne les voit pas.

Les variables sont lues au démarrage du processus. Après avoir modifié `.env`, recréer le
conteneur : `docker compose up -d` (en déploiement Ansible : relancer le playbook, voir
[Déploiement géré par Ansible](#déploiement-géré-par-ansible)). `docker compose restart` et `./restart.sh`
redémarrent le conteneur existant **sans relire** `.env`. Pour changer le port, modifier `ports` dans
`docker-compose.yml`.

Sans aucune variable, l'application tourne en mode local sans authentification, joignable uniquement
sous le nom `localhost`. Une configuration incomplète ou dangereuse **empêche le démarrage**, avec le
motif dans les logs (`./logs.sh`).

> **Statut** : tout ce qui est décrit ici est livré, sauf ce qui porte la mention **non livré**
> (liste récapitulative : [Non livré](#non-livré)). Le mode `password` démarre mais **personne ne peut
> encore s'y connecter** ; ne pas l'utiliser.

### Déploiement géré par Ansible

Cadre de référence : la stack `docker-compose.yml` tourne sur un **serveur dédié**, déployée par Ansible, avec **un seul
opérateur** (l'administrateur du serveur). Détail, exemples et procédures : [docs/exploitation.md](docs/exploitation.md#déploiement-géré-par-ansible).

- **Environnement** : le `.env` est généré par un template Ansible ; secrets (`SECRET_KEY`, `SECRETS_KEY`,
  `OIDC_CLIENT_SECRET`) dans **Ansible Vault**, `.env` en mode `0600`, `no_log` sur les tâches concernées.
- **Changement de configuration** : par **recréation** du conteneur (`docker compose up -d`, module
  `community.docker.docker_compose_v2` en `state: present`), **jamais** par `restart`, qui ne relit pas `.env`.
  `start.sh`, `stop.sh` et `restart.sh` sont des outils de développement local.
- **Réglages administrables** : ce qu'Ansible écrit n'est qu'une **valeur par défaut** (base > env > défaut, pas de
  verrouillage). Une valeur saisie dans l'admin l'emporte et survit aux redéploiements ; écarts visibles avec
  `flask settings-list`, retour à la valeur d'Ansible avec `flask settings-reset <clé>`. Ne régler dans l'interface que
  ce qu'Ansible ne gère pas.
- **Données** : volume nommé `kml-viewer-data` (`project_name` fixe), sauvegardes à sortir du serveur ; la `SECRETS_KEY`
  est dans le Vault, pas dans les sauvegardes.
- **CLI** : canal **opérateur de confiance**, sans authentification propre : l'accès à `docker` équivaut à l'accès à la
  CLI, et aucun autre utilisateur système ne doit exister sur le serveur. Les commandes se lancent à la main
  (`docker compose exec` ou `run`), pas depuis un playbook.
- **Premier admin** : en mode `oidc`, `OIDC_ADMIN_ROLE` ou première connexion SSO puis `flask user-admin <login>` ;
  en mode `password` (non utilisable aujourd'hui), `flask user-invite --admin <adresse>`.

### Modes d'authentification (`AUTH_MODE`)

| Mode | Connexion | Exigences au démarrage | État |
|------|-----------|------------------------|------|
| `oidc` | SSO obligatoire (FusionAuth ou autre IdP OIDC) ; en plus, comptes à mot de passe **sur invitation** si `SECRETS_KEY` **et** `PUBLIC_URL` sont renseignées | `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `SECRET_KEY` (32 caractères au moins), `TRUSTED_HOSTS` | livré (SSO) ; comptes à mot de passe : invitations livrées, connexion **non livrée** |
| `local` | aucune : mono-utilisateur, **chaque visiteur est administrateur** | aucune ; interdit avec `OIDC_ISSUER` | livré |
| `password` | identifiant + mot de passe + second facteur, sans SSO | `SECRET_KEY` (32 caractères au moins), `TRUSTED_HOSTS`, `SECRETS_KEY`, `PUBLIC_URL` couverte par `TRUSTED_HOSTS` ; interdit avec `OIDC_ISSUER` | démarrage livré, **connexion non livrée** (T10, T11, T12) |

- `AUTH_MODE=password` s'écrit toujours explicitement : un `AUTH_MODE` vide ne le choisit jamais
  (il donne `oidc` si `OIDC_ISSUER` est renseigné, sinon `local`).
- En production, écrire `AUTH_MODE=oidc` : une variable OIDC perdue arrête alors l'appli au lieu de
  l'ouvrir à tous.
- Sans `AUTH_MODE` ni `OIDC_ISSUER`, le démarrage est refusé dès que `TRUSTED_HOSTS`, `TRUST_PROXY`,
  `SESSION_COOKIE_SECURE`, `OIDC_CLIENT_ID` ou `OIDC_CLIENT_SECRET` est renseigné (cela ressemble à une
  production sans authentification) : écrire `AUTH_MODE=local` si c'est voulu.
- Le mode est celui validé au démarrage : le garde d'accès, les routes d'authentification et les
  gabarits ne le déduisent jamais de la présence d'`OIDC_ISSUER`. Seul le mode `local` crée le compte
  `local`. Une session dont le compte n'a pas une méthode d'authentification permise par le mode courant
  (`password` ⇒ compte à mot de passe ; `oidc` ⇒ SSO ou mot de passe ; `local` ⇒ compte local) est vidée :
  après une bascule de mode, aucun cookie émis avant ne vaut plus rien.
- Mode `oidc` **sans** `SECRETS_KEY` ou sans `PUBLIC_URL` : comptes à mot de passe, invitations et saisie de
  secrets désactivés (routes 404) ; pas de refus de démarrage.
- Mode `local` : routes de mot de passe, d'invitation et de jetons en 404.

Détails OIDC (FusionAuth), session, comptes SSO : [docs/bibliotheque.md](docs/bibliotheque.md).

### Variables d'amorçage et de sécurité (environnement seulement)

| Variable | Rôle | Défaut |
|----------|------|--------|
| `AUTH_MODE` | `oidc`, `local` ou `password` (voir ci-dessus) | déduit |
| `PUBLIC_URL` | Origine publique : `https://hôte[:port]`, sans chemin, identifiants, paramètres (`http://` toléré pour `localhost` seulement). **Seule source** des liens envoyés ou affichés ; l'en-tête `Host` n'est jamais utilisé pour cela. Renseignée, elle sert aussi de référence au contrôle d'`Origin` des requêtes modifiantes : derrière un proxy TLS, c'est l'URL `https` publique, et une instance jointe sous un autre nom que celui-ci reçoit 403 sur ses POST. Obligatoire en mode `password` | vide |
| `SECRET_KEY` | Signature des cookies de session. Refus de démarrer en `oidc`/`password` si elle est par défaut ou fait moins de 32 caractères (`openssl rand -hex 32`) | valeur par défaut (refusée) |
| `SECRETS_KEY` | Clé(s) Fernet qui chiffrent les secrets en base (mot de passe SMTP, clé d'API, secrets TOTP). **Plusieurs clés séparées par des virgules** : la première chiffre, toutes déchiffrent. Distincte de `SECRET_KEY`. Obligatoire en mode `password` ; variable d'environnement seulement, **jamais dans la base ni dans `flask backup`**. Voir ci-dessous | vide |
| `TRUSTED_HOSTS` | Noms d'hôte acceptés (virgules ; `.example.com` couvre aussi les sous-domaines). Tout autre en-tête `Host` reçoit 400, sauf pour `/api/health`. Obligatoire en `oidc` et `password` ; en `local`, vide = `localhost` et `127.0.0.1` | vide |
| `TRUST_PROXY` | Nombre de reverse proxies de confiance (`X-Forwarded-For/Proto`) ; indispensable derrière un proxy TLS pour l'URL de callback OIDC | `0` |
| `TRUST_PROXY_HOST` | `1` seulement si le proxy réécrit `Host` **et** pose `X-Forwarded-Host` (HAProxy : laisser à `0`) | `0` |
| `SESSION_COOKIE_SECURE` | Cookie marqué `Secure`. Vide : activé d'office en `oidc` et `password` ; `0` : dérogation de développement en HTTP, signalée dans les logs | vide |
| `SESSION_LIFETIME_HOURS` | Durée absolue d'une session, depuis la connexion | `12` |
| `OUTBOUND_ALLOWED_HOSTS` | Liste blanche des destinations sortantes. Voir ci-dessous | vide |
| `SCHEDULER_ENABLED` | `0` coupe l'ordonnanceur de fond (réplique secondaire, diagnostic). Par défaut il démarre dans tout processus servi, jamais en ligne de commande | `1` |
| `WEBAUTHN_RP_ID` | Identifiant de partie de confiance WebAuthn. **Non lue** (WebAuthn : T12, non livré) | — |
| `DATA_DIR`, `DB_PATH`, `INBOX_DIR` | Dossier de données (`/data` dans l'image), base SQLite (`DATA_DIR/kmlviewer.sqlite3`), dossier surveillé (`/inbox` avec le compose) | — |
| `LOG_LEVEL` | Niveau de logs | `INFO` |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES`, `OIDC_ADMIN_ROLE` | Client OIDC ; `OIDC_ADMIN_ROLE` : le droit admin suit ce rôle à chaque connexion | — |
| `KMLVIEWER_MEM_LIMIT` | Limite mémoire du conteneur (lue par `docker-compose.yml`) | `2g` |

#### `SECRETS_KEY` et rotation

Générer une clé :

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Une valeur mal formée (autre chose qu'une clé Fernet) refuse le démarrage ; une valeur chiffrée en base
qu'aucune clé fournie ne déchiffre aussi (le message donne le nombre de valeurs). Rotation :
`SECRETS_KEY=<nouvelle>,<ancienne>`, recréer le conteneur, `flask secrets-rotate`, retirer l'ancienne,
recréer à nouveau ; clé perdue : `flask secrets-reset`. Pas à pas : [docs/exploitation.md](docs/exploitation.md).

#### `OUTBOUND_ALLOWED_HOSTS` : destinations sortantes

Toute adresse vers laquelle l'application envoie des données d'utilisateurs ou un secret est une
frontière de sécurité. La variable liste, **par service**, ce qui est permis. Syntaxe : entrées
séparées par des virgules, de la forme `service=destination`.

```text
OUTBOUND_ALLOWED_HOSTS=routing=graphhopper:8989,dem=opentopodata:5000,smtp=relay.example.com:587,tiles=a.tile.openstreetmap.org
```

| Service | Ce qu'il autorise | Destination attendue |
|---------|-------------------|----------------------|
| `routing` | `routing.base_url` (moteur de routage ; **non livré**) | `hôte:port` |
| `dem` | `DEM_URL` (service d'altitude) | `hôte:port` |
| `smtp` | `smtp.host` + `smtp.port` (envoi d'e-mails) | `hôte:port` |
| `tiles` | hôtes des fonds de carte (navigateur des utilisateurs) | `hôte` seul (https, 443) |

- **Un hôte autorisé pour un service ne vaut pas pour un autre** (`dem=h:5000` n'ouvre rien à `smtp`).
- **Port obligatoire** pour `routing`, `dem` et `smtp`, comparé exactement au port réel (`https://moteur`
  vaut le port 443). Pas de port sur `tiles`.
- **Littéral IPv6 entre crochets** : `routing=[fd00::1]:8989`.
- Une entrée invalide (port manquant, IPv6 sans crochets, service inconnu) **refuse le démarrage**.
- `DEM_URL` (et `ROUTING_BASE_URL`) posée **dans l'environnement** est autorisée d'office, pour son seul
  service : le `.env.example` n'a rien à ajouter pour un `DEM_URL` d'env. Une destination posée par
  l'administration (base) doit au contraire figurer dans la liste, sinon elle est refusée à l'écriture (400) et à l'usage.
- La liste vide n'autorise aucune destination nouvelle depuis l'administration.
- Comparaison sur le nom, sans résolution DNS ; les redirections HTTP ne sont jamais suivies.
- Fonds de carte : sans entrée `tiles=`, les hôtes des 5 fonds embarqués restent autorisés (aucun
  changement à la mise à jour). **La première entrée `tiles=` remplace toute cette liste par défaut.**
  La CSP `img-src` vaut `'self' data: blob:` plus exactement les hôtes de tuiles autorisés : une carte
  grise après avoir posé un `tiles=` signifie qu'il manque un hôte (par exemple `a|b|c.tile.openstreetmap.org`,
  `server.arcgisonline.com`, `a|b|c.tile.opentopomap.org`, `a|b|c.basemaps.cartocdn.com`). La liste est lue
  au démarrage : ajouter un fournisseur de tuiles = ajouter son `tiles=`, recréer le conteneur, puis saisir
  le fond dans l'administration (**non livré** : les fonds restent codés en dur dans `map.js`, voir plus bas).
- Une destination effective qui sort de la liste (liste réduite après coup) est refusée à l'appel, inscrite
  au journal d'audit (dédoublonnée), et ne reçoit jamais de secret.

### Réglages administrables : base > env > défaut

Un registre borné (`web-app/app/settings/registry.py`) liste ce qu'un admin peut changer sans
redémarrer. Chaque réglage a des bornes **basse et haute** obligatoires, et une variable d'environnement
facultative qui donne sa valeur initiale.

- **Valeur effective** : la valeur en base si elle existe, sinon la variable d'environnement, sinon le défaut.
- **Valeur d'environnement invalide** (illisible ou hors bornes) : **ignorée**, le défaut s'applique,
  `WARNING` au démarrage (le nom de la variable, jamais sa valeur). Jamais de refus de démarrage pour un
  réglage du registre. Des variables jadis libres sont désormais bornées : `INBOX_SCAN_INTERVAL` < 5 vaut
  maintenant 30 (et non plus 5), `DEM_TOTAL_TIMEOUT` > 600 vaut 90, `DEM_TIMEOUT` < 1 vaut 15.
- **Surcharge** : une variable d'environnement masquée par une valeur en base produit un `WARNING` au
  démarrage et un marqueur « surcharge `VAR` » dans le diagnostic. L'admin prime sur `.env` : modifier le
  fichier ne change rien tant que la valeur en base existe.
- **Effet immédiat** : toute écriture incrémente une version en base. Le serveur web la relit à chaque
  requête, l'ordonnanceur à chaque tour (5 s au plus) : une valeur changée (même par la CLI, depuis un autre
  processus) s'applique à la requête ou au tour suivant, y compris l'activation du dossier surveillé et
  l'intervalle des tâches. Aucun réglage n'exige de redémarrage.
- **Diagnostic** : `flask settings-list` (valeur, source `base` / `env` / `défaut`, surcharge). Exemple :

  ```text
  INBOX_SCAN_INTERVAL = 30  (défaut)
  USER_QUOTA_MB = 2048  (env)
  DEM_TIMEOUT = 15.0  (défaut)  [DEM_TIMEOUT invalide, ignorée]
  ```

- **Retour à la valeur d'environnement ou au défaut** : `flask settings-reset <clé>` (une valeur) ou
  `flask settings-reset --all` (toutes les valeurs en base). Tracé dans l'audit.
- **Écrire une valeur en base** : par l'API d'administration (`/api/admin/settings`), **non livrée (T14)**.
  Tant qu'elle n'existe pas, la valeur effective est celle de l'environnement, ou le défaut.

| Réglage | Variable d'env | Défaut | Bornes |
|---------|----------------|--------|--------|
| `USER_QUOTA_MB` (quota de stockage par utilisateur, Mo) | `USER_QUOTA_MB` | 2048 | 0 – 102 400 (0 = illimité) |
| `audit.retention_days` (rétention du journal d'audit) | — | 90 | 30 – 3650 |
| `OIDC_AUTO_ACTIVATE` | `OIDC_AUTO_ACTIVATE` | non | oui / non |
| `auth.invite_ttl_hours` (validité d'une invitation) | — | 168 | 1 – 720 |
| `auth.mfa_policy` (second facteur obligatoire pour) | — | `admins` | `admins` \| `all` |
| `auth.forgot_enabled` (mot de passe oublié ; exige le SMTP) | — | oui | oui / non |
| `INBOX_ENABLED` | `INBOX_ENABLED` | oui | oui / non |
| `INBOX_SCAN_INTERVAL` (s) | `INBOX_SCAN_INTERVAL` | 30 | 5 – 3600 |
| `INBOX_DEFAULT_USER` | `INBOX_DEFAULT_USER` | vide | ≤ 254 car. |
| `DEM_URL` (destination sortante, service `dem`) | `DEM_URL` | vide | URL http(s) ≤ 500 car. |
| `DEM_BATCH_SIZE` | `DEM_BATCH_SIZE` | 100 | 1 – 1000 |
| `DEM_TIMEOUT` (s) | `DEM_TIMEOUT` | 15 | 1 – 120 |
| `DEM_TOTAL_TIMEOUT` (s) | `DEM_TOTAL_TIMEOUT` | 90 | 10 – 600 |
| `DEM_REQUEST_DELAY` (s) | `DEM_REQUEST_DELAY` | 0 | 0 – 10 |
| `DEM_BACKFILL_BATCH` | `DEM_BACKFILL_BATCH` | 25 | 1 – 1000 |
| `DEM_BACKFILL_INTERVAL` (s) | `DEM_BACKFILL_INTERVAL` | 300 | 30 – 3600 |
| `APP_TIMEZONE` | `APP_TIMEZONE` | `Europe/Paris` | fuseau IANA connu |
| `smtp.host`, `smtp.port`, `smtp.security`, `smtp.username`, `smtp.sender`, `smtp.timeout_s` | — | vide, 587, `starttls`, vide, vide, 10 | voir [E-mail](#e-mail-et-notifications) |
| `ratelimit.password_per_identifier` (tentatives / 15 min / identifiant) | — | 5 | 3 – 20 |
| `ratelimit.password_per_ip` (/ 15 min / IP) | — | 20 | 10 – 100 |
| `ratelimit.mfa_per_account` (second facteur, / 15 min / compte) | — | 5 | 3 – 10 |
| `ratelimit.forgot_per_identifier` (mot de passe oublié, / h / identifiant) | — | 3 | 1 – 10 |
| `ratelimit.forgot_per_ip` (/ h / IP) | — | 10 | 10 – 100 |
| `ratelimit.token_per_ip` (essais de jeton, / 15 min / IP) | — | 10 | 5 – 30 |
| `map.basemaps`, `map.default_basemap`, `map.center_lat`, `map.center_lon`, `map.zoom` | — | 5 fonds embarqués, `osm`, 46,603354 / 1,888334, zoom 6 | ≤ 10 fonds ; lat −85 – 85, lon −180 – 180, zoom 1 – 22 |
| `routing.*` (traces planifiées : délais, budgets, tolérances, `routing.base_url` ← `ROUTING_BASE_URL`) | `ROUTING_BASE_URL` pour l'URL | — | bornées dans le registre ; **fonction non livrée** |

Les seuils du limiteur s'appliquent à chaud, sans remettre les compteurs à zéro. Ne sont **pas**
administrables (environnement ou code seulement) : tout ce qui figure dans « Variables d'amorçage et de
sécurité », `DEM_RETRIES`, `DEM_RETRY_BACKOFF`, `DEM_MAX_GAP_M` et les limites dures contre les abus
(taille de fichier, nombre de points, cache).

**Fonds de carte** : les réglages `map.*` existent et se valident, mais le visualiseur (`map.js`,
`segments.js`) **ne les lit pas encore** : les 5 fonds restent déclarés dans
`web-app/static/js/viewer/map.js` (`initMap`) et `web-app/templates/viewer/map_settings.html`. Tant que
le branchement n'est pas livré (T15), ils n'ont aucun effet visible.

### E-mail et notifications

L'application envoie des e-mails en texte brut, en français, par SMTP : invitation, validation d'adresse,
mot de passe oublié, notifications de sécurité. Tous les liens sont construits sur `PUBLIC_URL`.

- **Réglages** : `smtp.host` et `smtp.sender` (si l'un des deux est vide, ou si la destination n'est pas dans la
  liste blanche : aucun e-mail), `smtp.port` (587), `smtp.security`, `smtp.username`, `smtp.timeout_s` (10 s), plus le **secret** `smtp.password`. Il n'existe **aucune variable
  d'environnement SMTP** : ils s'écrivent par l'API d'administration (`/api/admin/settings`,
  `/api/admin/secrets`), **non livrée (T14)**. En l'état, aucun e-mail ne part ; les liens d'invitation et
  d'amorçage sont montrés à l'émetteur, qui les transmet lui-même.
- **Sécurité** : `starttls` (par défaut ; si la négociation échoue, l'envoi échoue, **jamais de repli en
  clair**), `tls` (TLS dès la connexion) ou `none` (**aucune authentification n'est envoyée**, un identifiant
  ou un mot de passe enregistré avec `none` est refusé). Le certificat du serveur est toujours vérifié.
- **Liste blanche** : `smtp=<hôte>:<port>` dans `OUTBOUND_ALLOWED_HOSTS`, contrôlée à chaque envoi.
- **Mot de passe SMTP lié à sa destination** : il n'est envoyé qu'à l'hôte et au port pour lesquels il a été
  saisi. Changer `smtp.host` ou `smtp.port` (ou `settings-reset`) **efface** le mot de passe, à ressaisir.
  Jamais réaffiché, jamais journalisé.
- **Deux modes d'envoi** : synchrone (invitation, e-mail de test : l'émetteur sait si le message est parti)
  et file d'envoi en mémoire pour le reste (1 000 messages au plus ; perdue au redémarrage, journalisé).
  Un échec n'affiche jamais de détail SMTP.
- **Notifications des commandes CLI** : envoyées **de façon synchrone** avant que la commande rende la main,
  si le SMTP est configuré. Un échec d'envoi n'annule pas l'action (faite et tracée) : la commande le dit et
  sort avec le **code 1**.

  | Commande | Destinataires | Message |
  |----------|---------------|---------|
  | `user-invite --admin` | l'adresse visée | le lien d'amorçage |
  | `user-mfa-reset` | l'adresse validée du compte | facteurs réinitialisés |
  | `secrets-reset` | les admins actifs ayant une adresse, **avant** l'effacement | secrets effacés |
  | `settings-reset` (`smtp.*` ou `--all`) | les admins actifs ayant une adresse, **avant** le changement | configuration e-mail réinitialisée |

  Sans SMTP configuré, la commande le signale (« E-mail non configuré ») et réussit.
- **Mot de passe oublié** : actif dès que le SMTP est configuré (désactivable : `auth.forgot_enabled`) ;
  **connexion non livrée (T10)**.

### Mots de passe : politique et liste de mots de passe courants

Politique des comptes à mot de passe : 12 à 128 caractères (mesurés après normalisation NFKC), aucune règle
de composition, refus s'il est égal à l'identifiant ou à l'adresse e-mail (casse ignorée) ou s'il figure dans
la liste de mots de passe courants. Hachage argon2id (64 Mo, 3 passes), au plus **2 hachages en parallèle**
(une 3e requête attend 5 s au plus, puis reçoit un 429 identique pour tous). Mémoire du conteneur :
[docs/exploitation.md](docs/exploitation.md).

La liste est embarquée dans l'image (`web-app/app/auth/wordlists/`) ; l'application **ne télécharge
jamais rien** :

| Fichier | Contenu |
|---------|---------|
| `common_passwords.txt` | 46 187 entrées (12 caractères et plus, NFKC + casefold, triées, sans doublon), SHA-256 `ec1dce68…fecaa` |
| `common_passwords_local.txt` | 71 entrées propres au projet (dont des mots de passe français), fusionnées à la génération |
| `common_passwords.SOURCE` | provenance, empreintes, effectifs, avis de licence |

Source publique : SecLists (`xato-net-10-million-passwords-1000000.txt`, commit figé, licence MIT du dépôt ;
données publiées par Mark Burnett en 2015, dont le statut juridique n'a pas pu être vérifié, voir
`common_passwords.SOURCE`). Régénération, sur décision seulement (réseau requis, ou fichier local en argument ;
le SHA-256 de la source est vérifié, sinon arrêt sans rien écrire) :

```bash
cd web-app
python -I scripts/build_common_passwords.py [fichier_source] [--out-dir DOSSIER]
```

Relire les effectifs et le nombre d'entrées ajoutées et retirées qu'affiche le script, mettre à jour
`common_passwords.SOURCE`, puis committer. La liste est chargée **au démarrage**, quand les comptes à mot de
passe sont disponibles (mode `password`, ou `oidc` avec `SECRETS_KEY` et `PUBLIC_URL`) ; absente, illisible ou
vide, elle **refuse le démarrage** (jamais de politique silencieusement affaiblie). En mode `local`, elle n'est
pas chargée.

### Routes ajoutées

| Route | Accès | Rôle |
|-------|-------|------|
| `GET /auth/token`, `POST /auth/token/inspect`, `POST /auth/token/consume` | publiques | Page et API de consommation d'un lien à usage unique (invitation, réinitialisation, validation d'adresse). Le jeton est dans le **fragment** de l'URL (`/auth/token#t=…`), jamais dans le chemin ni la requête, donc absent des journaux d'accès |
| `GET /api/invitations`, `POST /api/invitations`, `DELETE /api/invitations/<id>` | session complète ; comptes à mot de passe disponibles | Ses invitations, création, révocation. **Pas d'interface** (T15) |
| `GET /api/map-config` | session | Fonds de carte autorisés (liste blanche `tiles=`), fond par défaut, cadrage. Pas encore lue par le front |

Invitation : une adresse cible, validité `auth.invite_ttl_hours` (7 jours par défaut), lien montré **une seule
fois** à l'émetteur (et envoyé par e-mail si le SMTP est configuré). Un admin invite sans limite ; un
utilisateur actif, à adresse validée, avec `invite_credits ≥ 1` (crédit non rendu en cas de révocation ou
d'expiration). Le compte créé est actif, adresse **en attente** de validation, et n'est jamais admin. Un lien
consommé n'ouvre jamais de session : il renvoie à la page de connexion.

Les jetons de réinitialisation et de définition de mot de passe, la validation d'adresse par un compte
connecté et la conversion SSO → mot de passe dépendent de la connexion et de l'API d'administration (voir
ci-dessous).

### Non livré

Écrit à la date du 2026-11-24 d'après le code et [docs/specs/admin-space.md](docs/specs/admin-space.md) §14.
Ce tableau est la liste de référence ; chaque mention **non livré** ci-dessus y renvoie.

| Tâche | Fonction | Conséquence aujourd'hui |
|-------|----------|-------------------------|
| **T10** | Connexion par mot de passe en deux étapes, mot de passe oublié | En cours (formulaire d'étape 1 dans l'arbre de travail, non revu ; l'étape 2 répond 501). **Personne ne peut se connecter par mot de passe** |
| **T11** | TOTP, codes de secours, enrôlement obligatoire, amorçage d'un admin | `flask user-invite --admin` émet un jeton, mais le consommer répond **501** (jeton intact, aucun compte créé). Aucun admin ne peut être créé en mode `password`. Les colonnes TOTP n'existent pas encore |
| **T12** | WebAuthn (clés FIDO2) | `WEBAUTHN_RP_ID` n'est pas lue |
| **T13** | Validation et changement d'adresse par un compte connecté | Un compte invité garde son adresse en attente |
| **T14** | API d'administration : utilisateurs, invitations de tous, réglages, secrets, e-mail de test, fonctions bêta, journal d'audit, conversion SSO → mot de passe | Aucun réglage ni secret ne s'écrit (donc pas de SMTP) ; le journal d'audit n'est lisible qu'en base ; `flask user-invite --reset-password` (reprise d'amorçage avec mot de passe perdu) absent |
| **T15** | Interface d'administration et de profil | Pas d'écran pour les invitations ni les réglages ; les fonds de carte ne sont pas lus depuis `map.*` ; `/admin/users` est l'écran existant (activer, promouvoir, supprimer) |
| Routage | Traces planifiées (moteur GraphHopper) | `routing.*` et `ROUTING_BASE_URL` existent mais aucune fonction ne les utilise |

Conséquence pratique : une instance `oidc` ou `local` fonctionne comme avant ; les nouveautés utilisables
sont les protections de démarrage, la liste blanche des destinations (et la CSP des tuiles), les réglages en
lecture et leur retour au défaut, la rotation des secrets, la CLI et l'API d'invitation.

## 🐛 Dépannage

- **Port déjà utilisé** : changer le port dans `docker-compose.yml`, ou trouver le service qui
  l'occupe (`sudo lsof -i :8080`).
- **Fichier refusé** : le message indique la cause (extension, encodage UTF-8 attendu, archive
  KMZ illisible). Les détails d'une erreur interne sont dans les logs : `./logs.sh`.
- **Fond de carte vide** : les tuiles viennent de serveurs tiers, une connexion Internet est
  nécessaire ; le reste de l'application fonctionne hors ligne. Si le fond était affiché avant que
  `OUTBOUND_ALLOWED_HOSTS` ne reçoive une entrée `tiles=`, l'hôte de ce fond n'y figure pas (la CSP
  `img-src` bloque tout autre hôte) : voir [OUTBOUND_ALLOWED_HOSTS](#outbound_allowed_hosts--destinations-sortantes).
- **Le conteneur redémarre en boucle** : le motif du refus de démarrer est dans `./logs.sh`
  (`SECRET_KEY` par défaut, `TRUSTED_HOSTS` manquant, `PUBLIC_URL` non couverte, valeur chiffrée
  indéchiffrable avec `SECRETS_KEY`, entrée invalide d'`OUTBOUND_ALLOWED_HOSTS`…).
- **Une modification de `.env` n'a aucun effet** : `docker compose restart` et `./restart.sh` ne relisent
  pas `.env` ; recréer le conteneur (`docker compose up -d`, ou le playbook Ansible). Pour un réglage administrable, vérifier aussi qu'une valeur
  en base ne le masque pas (`flask settings-list`).
- **Flèches du clavier sans effet** : un fichier contenant des points doit être chargé, et le
  curseur ne doit pas être dans un champ de saisie.

## 📄 Licence

Ce projet est libre d'utilisation.
