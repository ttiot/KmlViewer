# Spec — Espace d'administration, comptes à mot de passe, second facteur, e-mail

Statut : **v6, validée par l'utilisateur, en développement** (re-review v5 intégrée) · Mise à jour : 2026-11-24 · Rôle : spec
Arbitrages utilisateur relayés par coord le 2026-11-24 (cf. §12). Corrections de [REVIEW.md](../../REVIEW.md) intégrées : review v3 (B…, M…, m…, D…, n1-n6), « Re-review v4 » (R1-R3, r1-r9, n7-n8) et « Re-review v5 » (v5-1 à v5-6, v5-n1 à v5-n3) ; chaque exigence touchée porte la référence du point. Points tranchés par défaut : §13.
Spec liée : [gpx-route-builder.md](gpx-route-builder.md) (consomme les clés, réglages, destinations sortantes et fonctions bêta définis ici).
ADR : [docs/adr/](../adr/README.md).

Principes imposés : **modulaire** et **sécurité d'abord** (exigence utilisateur pour tout kmlViewer).

---

## 1. Contexte et existant

Chemins relatifs à `web-app/` (n1).

| Élément | Où | Constat |
|---|---|---|
| Modes d'authentification | `app/auth/checks.py` | `oidc` (SSO obligatoire) ou `local` (mono-utilisateur **sans** auth, chaque visiteur admin). Le mode validé est écrit dans `config['AUTH_MODE']`. |
| Garde | `app/auth/guard.py` | **Décide du mode par `oidc_enabled() = bool(OIDC_ISSUER)`** ; si faux, crée/charge le compte `local` **admin** à chaque requête. Même test dans le context processor (`oidc_enabled` des gabarits). |
| Comptes | `app/auth/users.py`, table `users` (`app/db.py`) | Créés à la 1re connexion OIDC (`upsert_from_claims`) ; `sub` unique ; `username` et `email` réécrits depuis l'IdP à chaque connexion, **sans contrainte d'unicité**. Invariant : `users.email` ne contient qu'un e-mail vérifié (il désigne un dossier de l'inbox via `match_login`). |
| Sessions | `app/auth/guard.py`, `app/auth/oidc.py` | Cookie Flask signé **côté client** + `session_token` par compte (révocation globale), durée absolue `login_at`. Liste blanche `PUBLIC_ENDPOINTS`. |
| Admin actuel | `/admin/users`, `/api/users` (`app/api/users_routes.py`), `static/js/users.js` | Activer, promouvoir, supprimer. Garde-fous : pas d'auto-rétrogradation, rôle admin piloté par l'IdP si `OIDC_ADMIN_ROLE`. |
| CLI | `app/cli.py` | `user-admin` (promeut **et active**), `backup`, `inbox-scan`, `recompute-stats`. |
| Anti-CSRF / en-têtes | `app/security.py` | Contrôle `Origin`/`Sec-Fetch-Site`, CSP `script-src 'self'`, `connect-src 'self'`. |
| Processus | `Dockerfile`, `app/__init__.py`, `app/services/inbox_watcher.py` | gunicorn **1 worker, 8 threads**, **`--access-logfile -`** (chemin et query de chaque requête journalisés) ; thread de fond (inbox/DEM) **démarré seulement si `INBOX_ENABLED` ou `DEM_URL`**, jamais en `TESTING`, protégé par un verrou fichier (un seul processus), **une seule boucle à un seul intervalle**, config lue une fois au démarrage. |
| Migrations | `app/db.py` | 5 migrations **positionnelles** (`PRAGMA user_version`) ; `_add_column` idempotent. La CI publie une image par branche poussée (`.github/workflows/docker.yml`). |
| CSP images | `app/security.py` | `img-src 'self' data: blob: https:` (tout hôte HTTPS, pour les tuiles). |
| Traitements lourds | `app/api/workload.py` | `heavy_slot()` : 2 places pour toute l'instance, attente 20 s puis 503. |
| Clés API, e-mail | — | Aucun aujourd'hui. |

### Inventaire de ce qui est « en dur »

| Catégorie | Exemples | Source actuelle |
|---|---|---|
| Amorçage / sécurité | `SECRET_KEY`, `AUTH_MODE`, `OIDC_*`, `TRUSTED_HOSTS`, `TRUST_PROXY*`, `SESSION_COOKIE_SECURE`, `SESSION_LIFETIME_HOURS`, `DATA_DIR`, `DB_PATH` | env (`app/config.py`) |
| Exploitation | `USER_QUOTA_MB`, `OIDC_AUTO_ACTIVATE`, `INBOX_*`, `DEM_URL`, `DEM_*`, `DEM_BACKFILL_BATCH`, `APP_TIMEZONE`, `LOG_LEVEL` | env |
| Carte | 5 fonds de carte, centre/zoom initiaux | `static/js/viewer/map.js`, `static/js/segments.js` |
| Métier | catalogue des types d'activité, distances des records, seuils d'analyse | code Python |
| Bornes anti-abus | `MAX_CONTENT_LENGTH`, `MAX_FILE_POINTS`, `MAX_KMZ_UNCOMPRESSED`, limites du cache, `MAX_TILES` | code Python |

---

## 2. Objectifs

1. **Nouveau mode `password`** : instance sans IdP, identifiant + mot de passe + second facteur (ADR-0001).
2. **Comptes à mot de passe sur invitation uniquement**, en modes `password` et `oidc`.
3. **Second facteur** TOTP et WebAuthn, **obligatoire pour les admins** (ADR-0004).
4. **Droit d'inviter** : admin sans limite ; utilisateur via un compteur (défaut 0) crédité par un admin seulement.
5. **E-mail** : SMTP configurable ; validation de l'adresse par aller-retour **systématique** ; e-mails d'invitation, de réinitialisation et de sécurité (ADR-0005).
6. **Secrets des services externes**, chiffrés, liés à leur destination, jamais réaffichés (ADR-0003, ADR-0011).
7. **Réglages** administrables, base > env (ADR-0008), destinations sortantes bornées par une liste blanche d'env (ADR-0011).
8. **Fonctions bêta** pour une liste d'utilisateurs ou pour tous.
9. **Journal d'audit**.

## 3. Hors périmètre (v1)

- Inscription libre ; clés API personnelles ; second facteur des comptes SSO (IdP) ; connexion sans mot de passe (passkey seule) ; liaison SSO ↔ mot de passe sur un même compte (la **conversion** EF-0.5 n'est pas une liaison).
- Administrer depuis l'UI les réglages d'amorçage/sécurité et les bornes anti-abus (EF-6.6).
- Proxy de tuiles côté serveur ; `SETTINGS_LOCKED` ; réglages `critical`.

---

## 4. Découpage modulaire

| Module | Rôle | Dépend de |
|---|---|---|
| `app/auth/modes.py` | `auth_mode()` (lu dans `config['AUTH_MODE']` validé), méthodes d'auth permises par mode, disponibilité des comptes à mot de passe. **Seule source** du mode pour le garde, les routes et les gabarits (B1). | config |
| `app/auth/passwords.py` | Hachage (sous sémaphore), vérification, rehachage, politique. | ADR-0002 |
| `app/auth/tokens.py` | Jetons à usage unique : création, inspection, consommation atomique, révocation. | db |
| `app/auth/invitations.py` | Règles d'invitation et de crédits. | tokens |
| `app/auth/pending.py` | État **serveur** des connexions en cours (`mfa_pending`) et des défis WebAuthn : identifiant opaque, échéance, essais, consommation atomique (M3). | — |
| `app/auth/mfa/totp.py` · `webauthn.py` · `recovery.py` | Second facteur. | ADR-0004, secrets_store, pending |
| `app/auth/password_login.py` | Routes `/auth/password/*` (connexion, enrôlement, oubli) et `/auth/token*`. | modes, passwords, pending, mfa, ratelimit |
| `app/mailer.py` | Construction, validation des adresses, envoi synchrone ou **file d'envoi** (M2). | settings, secrets_store, outbound |
| `app/outbound.py` | Liste blanche des destinations sortantes **par service** (routage, DEM, SMTP, **tuiles**), normalisation, liaison secret ↔ destination, résultat de test neutre, hôtes de tuiles pour la CSP (B3, R1, r3). | config, settings |
| `app/background.py` | **Ordonnanceur de fond** : un thread, des tâches à échéances propres (inbox, DEM, purge d'audit, sondage du moteur…), chacune exécutable unitairement (R3). | settings, db |
| `app/db.py` (évolution) | Registre des migrations **par nom** (r2, ADR-0012). | — |
| `app/ratelimit.py` | Limiteur (familles de clés à mémoire bornée, M8). | ADR-0006 |
| `app/secrets_store.py` | Chiffrement multi-clés, rotation (M15). | ADR-0003 |
| `app/settings/registry.py` · `store.py` | Registre **borné** (M7) ; valeur effective, version en base (M10), diagnostic. | secrets_store, db |
| `app/features.py` | Fonctions bêta. | settings, db |
| `app/audit.py` | Journal et purge. | db |
| `app/admin/` (blueprint) | Pages `/admin/*`, API `/api/admin/*`. | ci-dessus |

Règle : aucun module métier (`services/*`) ne lit `os.environ` ni la table des réglages ; il passe par `settings.store`.

---

## 5. Exigences fonctionnelles

### 5.0 Modes d'authentification (ADR-0001)

| `AUTH_MODE` | Connexion | Comptes à mot de passe | Comptes SSO |
|---|---|---|---|
| `local` | aucune, mono-utilisateur | désactivés (routes 404) | — |
| `oidc` | SSO + mot de passe | sur invitation ; routes actives dès que `SECRETS_KEY` **et** `PUBLIC_URL` sont présentes, quel que soit le nombre de comptes (m1) | oui |
| `password` | mot de passe + second facteur | sur invitation | refusés (EF-0.6) |

- **EF-0.0 (B1)** Le garde, les routes d'authentification et le context processor se fondent **uniquement** sur `auth_mode()` (mode validé par `checks.py`), jamais sur la présence d'`OIDC_ISSUER`. **Seul le mode `local`** crée ou charge le compte `sub='local'`. En modes `oidc` et `password`, une requête sans session valide sur une route hors liste blanche ⇒ 401 (API) ou redirection vers la page de connexion du mode. Les gabarits reçoivent `auth_mode` (et `oidc_enabled = auth_mode == 'oidc'` pour compatibilité).
- **EF-0.1** `AUTH_MODE=password` s'écrit explicitement ; incompatible avec `OIDC_ISSUER` (démarrage refusé). Le mode déduit (variable vide) ne choisit jamais `password`.
- **EF-0.2** Démarrage du mode `password` refusé si : `SECRET_KEY` par défaut ou < 32 caractères ; `TRUSTED_HOSTS` vide ; `SECRETS_KEY` absente ; **`PUBLIC_URL` absente ou non couverte par `TRUSTED_HOSTS`** (M12). Cookie `Secure` par défaut. SMTP absent : démarrage autorisé, avertissement (« les adresses ne peuvent pas être validées »).
- **EF-0.3** Amorçage : `flask user-invite --admin <email>` crée un jeton d'**amorçage** (seul jeton qui donne l'admin), lien affiché sur la console et envoyé par mail si SMTP est configuré, validité 24 h. Sa consommation crée le compte **non admin** (identifiant, mot de passe) et un état `mfa_pending` à l'étape `enroll` portant l'octroi d'admin ; l'enrôlement d'un second facteur passe `is_admin` à 1 **dans la même transaction** et détruit l'état, puis redirige vers la connexion (EF-1.12) (M11, B5, r8). Enrôlement abandonné ⇒ compte non admin, sans facteur. Reprise (v5-n2) : `flask user-invite --admin --account <login>` émet un nouveau jeton d'amorçage **pour ce compte existant** (compte `password` actif sans facteur seulement ; sinon refus) ; relancer l'amorçage sans `--account` créerait un second compte (identifiant différent obligatoire). Alternative : enrôler un facteur depuis le profil puis `flask user-admin`.
- **EF-0.3b (T7a-n1)** Consommation d'un jeton d'amorçage lié à un compte existant (`--account`) :
  - le formulaire (type `confirm_password` renvoyé par `inspect`) demande le **mot de passe actuel du compte** : le lien seul ne suffit pas, un lien intercepté ne permet pas de prendre le compte ni d'y attacher son propre facteur ;
  - la vérification passe par le limiteur (par identifiant du compte cible, avant hachage) et le sémaphore de hachage (EF-1.5, EF-S3) ; mauvais mot de passe ⇒ message neutre, **jeton non consommé** ;
  - mot de passe correct ⇒ jeton consommé, état `mfa_pending` à l'étape `enroll` portant l'octroi d'admin ⇒ **mêmes routes d'enrôlement** que EF-1.6 §4 (`/auth/password/enroll/*`) ; l'enregistrement du premier facteur passe `is_admin` à 1 dans la même transaction, puis redirection vers la connexion (EF-1.12) ;
  - **le mot de passe déjà posé est conservé** (hash inchangé) ;
  - mot de passe oublié : `flask user-invite --admin --account <login> --reset-password` émet un jeton dont le formulaire (type `set_password`) **remplace** le mot de passe (politique EF-1.4, sessions du compte révoquées) avant le même enrôlement ;
  - compte qui a obtenu un facteur, a été désactivé ou converti entre l'émission et la consommation ⇒ jeton refusé (réponse neutre de EF-3.6).
- **EF-0.4** Mode `oidc` sans `SECRETS_KEY` ou sans `PUBLIC_URL` : comptes à mot de passe, invitations et saisie de secrets désactivés (routes 404, formulaire masqué, explication dans l'admin) ; pas de refus de démarrage.
- **EF-0.5 (M5)** Conversion SSO → mot de passe (mode `password` seulement, action admin) : un lien « définir un mot de passe » (jeton `set_password`, 24 h). À sa consommation, dans une transaction : `sub` réécrit en `pwd:<uuid4>` (l'IdP ne peut plus retrouver le compte) ; `auth_method='password'` ; **identifiant choisi** à cette étape, conforme EF-1.2 (le `username` SSO, qui peut être un e-mail, n'est pas repris d'office) ; mot de passe défini ; `session_token` renouvelé ; adresse : l'e-mail présent dans `users.email` (vérifié par l'IdP, `email_verified`) **reste validé** (défaut, §13), **sauf** s'il est déjà l'adresse validée d'un compte `password` (collision tolérée côté SSO, EF-1.2) : il passe alors en `pending_email` (aller-retour EF-4.3b, qui échouera de façon neutre tant que l'adresse reste prise) et l'admin en est informé dans la réponse (v5-4). Toute la conversion se fait dans une transaction `BEGIN IMMEDIATE` : jamais d'erreur d'intégrité en cours de parcours ; **`is_admin` remis à 0** si le compte était admin (invariant B5 : il sera repromu après enrôlement d'un facteur). Action tracée.
- **EF-0.6 (M4)** Le garde refuse toute session dont le compte n'a pas une `auth_method` permise par le mode courant (`password` ⇒ `password` ; `oidc` ⇒ `oidc`, `password` ; `local` ⇒ `none`) : session vidée, traitée comme absente.

### 5.1 Comptes à mot de passe

- **EF-1.1** Un compte à mot de passe n'est créé **que** par la consommation d'un jeton d'invitation ou d'amorçage valide.
- **EF-1.2 (M6, r7)** Identifiant : 3 à 64 caractères `[a-z0-9._-]`, sans `@`. Unicité insensible à la casse : **à la création** parmi tous les comptes ; **en permanence** parmi les comptes `password` (index unique partiel). Même règle pour l'**adresse validée** : adresses stockées sous forme canonique (EF-4.10), index unique partiel sur `email` des comptes `password`, et contrôle contre les autres comptes dans la même transaction `BEGIN IMMEDIATE` que l'écriture (deux validations concurrentes de la même adresse : une seule réussit). Une collision ultérieure créée par l'IdP (un compte SSO prend l'identifiant ou l'e-mail d'un autre compte) n'est pas bloquée mais : log `WARNING`, ligne d'audit `identity.collision`, bandeau dans l'admin nommant les comptes (EF-8.5). Le login par mot de passe et le mot de passe oublié ne considèrent que les comptes `password`.
- **EF-1.2b (test T2)** **Identifiants réservés** (défense en profondeur, en plus de `actor_type`) : `cli`, `system`, `local` (identifiant du compte du mode `local`), `admin`, `root` — comparaison insensible à la casse, après normalisation. Refusés partout où un identifiant de compte `password` est **choisi** : acceptation d'invitation, amorçage, conversion SSO → mot de passe (EF-0.5). Message neutre « identifiant indisponible », identique à celui d'un identifiant déjà pris. Ne s'applique pas aux identifiants SSO fournis par l'IdP (non refusables à la connexion, EF-1.2) : c'est pourquoi l'audit s'appuie sur `actor_type` (EF-9.1). Liste en constante dans le code, pas dans le registre.
- **EF-1.3** `sub` : `pwd:<uuid4>`. Colonne `auth_method` = `password` | `oidc` | `none`.
- **EF-1.4 (m5)** Politique : longueur 12 à 128 **mesurée après NFKC** ; pas de règles de composition ; refus si égal (casse ignorée) à l'identifiant ou à l'e-mail, ou présent dans la liste embarquée de mots de passe courants (EF-1.4b).
- **EF-1.4b Liste de mots de passe courants** (arbitrage utilisateur : **top 1 000 000** public de la même source, embarqué dans le dépôt, en union avec la liste d'amorce locale ; ADR-0002) :
  - **Source** : SecLists, fichier `Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt`, **commit `c205c36a445bff37f8e58a9ec829105cd4975c58`** (2025-05-08, dernier commit du fichier), blob `e5e73232c8f4f53edfe9c0fd699f415da8a336e3`, 1 000 000 lignes, 8 557 632 octets, **SHA-256 du fichier brut `424a3e03a17df0a2bc2b3ca749d81b04e79d59cb7aeec8876a5a3f308d0caf51`** (vérifié le 2026-11-24 sur deux téléchargements indépendants : API GitHub et `raw.githubusercontent.com`) ; URL figée : `https://raw.githubusercontent.com/danielmiessler/SecLists/c205c36a445bff37f8e58a9ec829105cd4975c58/Passwords/Common-Credentials/xato-net-10-million-passwords-1000000.txt`. Fichier de la série `xato-net`, retenu parce que sa provenance est documentée (README du dossier : jeu « ten million passwords » publié par Mark Burnett en 2015), contrairement à `10k-most-common.txt`.
  - **Licence** (arbitrage : acceptée en l'état) : le dépôt SecLists est sous **licence MIT** (`LICENSE` : « Copyright (c) 2018 Daniel Miessler », SPDX `MIT` selon l'API GitHub, vérifié le 2026-11-24). Les données viennent de la publication de Mark Burnett (2015), que la presse décrit comme versée au domaine public ; le texte d'origine n'a pas pu être lu (Medium : HTTP 403 ; archive xato.net inaccessible). **Obligation** : l'avis MIT de SecLists et cette provenance figurent intégralement dans `common_passwords.SOURCE`, livré avec la liste.
  - **Fichiers embarqués** (dossier `web-app/app/auth/wordlists/`, hors du motif `data/` du `.dockerignore`, donc inclus dans l'image) :
    - `common_passwords.txt` : UTF-8, une entrée par ligne, fin de ligne `\n`, **déjà normalisée** (NFKC puis `casefold()`), filtrée sur la longueur 12–128 **après** normalisation, sans doublon, **triée** (ordre des points de code), sans commentaire ni ligne vide (une entrée pourrait commencer par `#`) ; **46 187 entrées, 675 446 octets, SHA-256 attendu `ec1dce6813aac5688c42bdb30f181f790ae56b3e974dd769732753fb0e1fecaa`** (mesuré en appliquant ces règles à la source ci-dessus et à la liste locale actuelle) ;
    - `common_passwords.SOURCE` : URL figée, commit, blob, SHA-256 de la source et de la sortie, paramètres de filtrage, effectifs, commande de génération, avis de licence MIT de SecLists reproduit intégralement, provenance des données (Burnett, 2015) ;
    - `common_passwords_local.txt` : la liste d'amorce actuelle (71 entrées de `app/auth/common_passwords.py`, dont des mots de passe français et propres au projet), même format ; **fusionnée** à la génération (arbitré, §13 n° 27). `app/auth/common_passwords.py` disparaît au profit de ces fichiers.
  - **Génération reproductible** : `web-app/scripts/build_common_passwords.py` (bibliothèque standard seulement, lancé avec `python -I`) : télécharge l'URL figée en flux (ou lit un fichier local passé en argument), **vérifie le SHA-256** et s'arrête sinon, découpe sur `\n` et retire un éventuel `\r` final (pas les espaces), décode en UTF-8 strict (ligne invalide ⇒ ignorée et comptée ; 0 dans la source actuelle), normalise (NFKC, `casefold()`), filtre 12–128, fusionne la liste locale, dédoublonne, trie, écrit les deux fichiers, et affiche les effectifs (lignes lues, invalides, retenues, communes avec la liste locale, total) ainsi que le nombre d'entrées ajoutées et retirées par rapport au fichier existant. Aucune date ni donnée variable dans `common_passwords.txt` : mêmes entrées ⇒ mêmes octets.
  - **Effectif mesuré** : 1 000 000 lignes ; **46 146** entrées de 12 caractères ou plus après normalisation ; 30 sont aussi dans la liste locale ; **union : 46 187 entrées**.
  - **Chargement** : **au démarrage**, une fois par processus, dès que les comptes à mot de passe sont disponibles (mode `password`, ou mode `oidc` avec `SECRETS_KEY` et `PUBLIC_URL`, EF-0.4) ; pas chargée en mode `local` ni quand les comptes à mot de passe sont désactivés. Lecture ligne à ligne dans un `frozenset` : **~60 ms et ~5 Mo** de mémoire mesurés (Python, poste de dev ; à reconfirmer dans l'image), soit un surcoût négligeable devant la limite mémoire du conteneur (2 Go par défaut). Fichier absent, illisible ou vide ⇒ **refus de démarrer** quand la liste doit être chargée : jamais de politique silencieusement affaiblie. Comparaison inchangée : `normalize(mot de passe).casefold() in liste`.
  - **Mise à jour** : jamais à l'exécution (aucun appel sortant). Sur décision : modifier le commit et les SHA-256 dans le script, relancer, vérifier les effectifs et le nombre d'entrées ajoutées et retirées affichés par le script (un diff ligne à ligne de 46 000 entrées n'est pas relu à la main), mettre à jour `SOURCE`, committer. Pas de fréquence imposée.
- **EF-1.5 (m4)** Hash argon2id (ADR-0002), rehaché à la connexion si les paramètres ont changé. Tout hachage ou vérification (hash factice compris) passe par un **sémaphore de 2 places** ; attente ≤ 5 s puis 429 générique (identique pour tous les identifiants). Mémoire du conteneur à dimensionner en conséquence (doc ops). **Ordre (r9)** : le limiteur (EF-S3) est consulté **avant** l'acquisition du sémaphore ; une requête refusée par le limiteur ne hache rien et n'occupe aucune place.
- **EF-1.6** Connexion en deux étapes :
  1. `POST /auth/password/login`. Échec : message **unique et générique** (compte inconnu, désactivé, SSO, mauvais mot de passe), hash factice si compte absent.
  2. Succès : création d'un état **serveur** `mfa_pending` (M3) — identifiant aléatoire ≥ 128 bits, compte, étape (`mfa` ou `enroll`), échéance 5 min, compteur d'essais, `next`. Le cookie ne porte que cet identifiant, sous une clé distincte (`pending_id`) ; il ne contient **jamais** `user_id`. Le garde ne le considère pas comme une session.
  3. Facteur valide : l'état est **consommé atomiquement**, puis session complète (`session.clear()`, `user_id`, `token`, `login_at`, `permanent`), redirection via `safe_next`.
  4. Compte sans facteur alors que la politique l'exige : étape `enroll`, seules les routes `/auth/password/enroll/*` sont accessibles (M11).
  Échéance, essais et consommation sont contrôlés côté serveur : rejouer un ancien cookie ne remet rien à zéro. Mémoire bornée (10 000 états ; au-delà, les plus anciens sont évincés, ce qui oblige seulement à se reconnecter). Redémarrage ⇒ connexions en cours perdues.
- **EF-1.7** Changement de mot de passe : mot de passe actuel **et** second facteur s'il existe ; révoque les autres sessions, réémet la courante ; notification.
- **EF-1.8** Réinitialisation par un admin : jeton `reset` 24 h, envoyé à l'adresse validée et montré à l'admin. Ne retire pas le second facteur.
- **EF-1.9 (M1, M2)** Mot de passe oublié (actif dès que SMTP est configuré, désactivable) : `POST /auth/password/forgot` ⇒ **toujours** la même réponse, rendue **sans attendre l'envoi** ; le mail n'est jamais envoyé dans le thread de la requête (file d'envoi, EF-4.8). Lien (1 h) envoyé seulement si le compte est `password`, actif, à adresse validée, désigné sans ambiguïté. Limitation par **identifiant ou e-mail saisi** (qu'il existe ou non) et par IP. Ne retire pas le second facteur ; révoque les sessions à l'usage.
- **EF-1.10** Un compte SSO n'a jamais de mot de passe ; un compte `password` ne se connecte jamais par OIDC.
- **EF-1.12 (r8)** **La consommation d'un jeton n'ouvre jamais de session complète** (invitation, amorçage, réinitialisation, définition de mot de passe) : elle se termine par une redirection vers la page de connexion, où s'appliquent le second facteur et, si la politique l'exige, l'enrôlement obligatoire (EF-1.6 §4). Seule exception de forme : l'amorçage enrôle son facteur avant cette redirection (EF-0.3). `verify_email` n'ouvre pas non plus de session : il exige au contraire une session existante (EF-4.3).
- **EF-1.11** Routes publiques ajoutées à `PUBLIC_ENDPOINTS` : `/auth/password/login`, `/auth/password/forgot`, `/auth/token` (page), `/auth/token/inspect`, `/auth/token/consume`. Routes accessibles **uniquement** avec un état `mfa_pending` valide : `/auth/password/mfa*`, `/auth/password/enroll/*`. Rien d'autre.

### 5.2 Second facteur (ADR-0004)

- **EF-2.1** Méthodes : TOTP (RFC 6238, SHA-1, 6 chiffres, 30 s) et WebAuthn (clés FIDO2/U2F, authentificateur de plateforme). Un compte peut cumuler TOTP et jusqu'à 10 clés.
- **EF-2.2 (B5, r6)** **Invariant : tout compte `password` admin a au moins un facteur actif, à tout instant.** « Facteur actif » = TOTP activé **ou** au moins une clé WebAuthn enregistrée. Les **codes de secours ne sont pas un facteur** : ils servent à se connecter quand un facteur est indisponible, et sont invalidés dès que le compte n'a plus aucun facteur. En conséquence :
  - promotion d'un compte `password` sans facteur actif **refusée** (409, « le compte doit d'abord enrôler un second facteur »), par l'API, l'UI **et** `flask user-admin` ;
  - suppression du dernier facteur d'un admin refusée ;
  - réinitialisation du second facteur d'un admin (EF-2.7) ⇒ **rétrogradation** dans la même transaction (défaut, §13) ;
  - passage de `auth.mfa_policy` à `all` **refusé** (409, nombre de comptes concernés) tant qu'un compte `password` actif n'a pas de facteur (lecture de l'arbitrage « idem », §13) ;
  - défense en profondeur : `admin_required` vérifie en plus, pour un compte `password`, la présence d'un facteur actif.
  Réglage `auth.mfa_policy` : `admins` (défaut) | `all`.
- **EF-2.3 (m2)** TOTP : secret 160 bits, QR code SVG généré serveur ; activation après un code valide ; secret chiffré. Vérification ±1 pas ; acceptation **atomique et anti-rejeu** : `UPDATE users SET totp_last_step = :pas WHERE id = :id AND (totp_last_step IS NULL OR totp_last_step < :pas)`, 0 ligne ⇒ refus (refuse tout pas ≤ dernier accepté, y compris deux soumissions concurrentes du même code).
- **EF-2.4 (M3, M12)** WebAuthn : RP ID = hôte de `PUBLIC_URL` (surcharge `WEBAUTHN_RP_ID`, qui doit être un suffixe de domaine enregistrable de cet hôte) ; origine attendue = origine de `PUBLIC_URL` ; `http://localhost` accepté **seulement** si `TESTING` ou mode debug explicite. Attestation `none`, vérification utilisateur `preferred`. Défi ≥ 128 bits stocké **côté serveur** (`pending.py`), lié à l'état ou à la session, valable 2 min, **consommé atomiquement**. Régression d'un compteur de signature non nul ⇒ refus + audit. Clés nommées, supprimables (sauf EF-2.2).
- **EF-2.5 (m3)** Codes de secours : 10 codes de **16 caractères base32** (80 bits), générés à l'activation du premier facteur, montrés une fois, stockés en SHA-256, comparés en temps constant, usage unique (marquage atomique), régénérables (anciens invalidés). Usage ⇒ notification.
- **EF-2.6** Échecs : 5 essais par état `mfa_pending` (compteur serveur) puis état détruit ; limitation par identifiant (EF-S3).
- **EF-2.7** Perte de tous les facteurs : un **autre** admin réinitialise (sessions révoquées, audit, notification ; rétrogradation si admin). Secours : `flask user-mfa-reset <login>` (mêmes effets, audit acteur `cli`) ; **cette commande peut rétrograder le dernier admin** et laisser l'instance sans admin : elle l'annonce avant d'agir et rappelle `flask user-invite --admin` (r6).
- **EF-2.8** JS WebAuthn servi par l'appli, sans bibliothèque externe (CSP inchangée).

### 5.3 Invitations et crédits

- **EF-3.1** `users.invite_credits INTEGER NOT NULL DEFAULT 0` (0–1000).
- **EF-3.2** Peut inviter : tout admin (sans limite) ; tout utilisateur actif, **à adresse validée**, avec `invite_credits ≥ 1`.
- **EF-3.3 (B4)** Invitation = une adresse e-mail cible. Jeton ≥ 256 bits stocké haché (SHA-256). **Le jeton ne figure jamais dans le chemin ni dans la query** : lien `<PUBLIC_URL>/auth/token#t=<jeton>` (EF-S8). Expiration 7 j (réglage 1 h – 30 j).
- **EF-3.4 (B2)** Envoi par mail si SMTP est configuré ; le même lien est montré une fois à l'émetteur. **La consommation du jeton ne valide jamais l'adresse**, quel que soit le canal (EF-4.3).
- **EF-3.5** Création par un non-admin : décrément atomique (`… WHERE id = ? AND invite_credits > 0`, 0 ligne ⇒ 409).
- **EF-3.6** Consommation atomique ; jeton réutilisé, expiré, révoqué ou inconnu ⇒ même réponse, sans distinction.
- **EF-3.7** Révocation par l'émetteur (les siennes) ou un admin (toutes). Crédit **non rendu** (révocation, expiration, échec d'envoi) ; l'UI le dit avant création. Émetteur désactivé ou supprimé ⇒ ses invitations en attente révoquées.
- **EF-3.8** Seul un admin modifie `invite_credits`.
- **EF-3.9 (m7)** Une invitation porte : émetteur, e-mail cible, dates, crédits initiaux du futur compte (0 si émetteur non admin), statut. **Note libre réservée aux admins** (≤ 200 caractères, texte brut, présentée dans le mail comme citation attribuée : « Message de <identifiant> : … ») (défaut, §13). Une invitation de l'UI ne crée jamais d'admin.
- **EF-3.10 (m6)** Adresse déjà portée par un compte ou visée par une invitation en attente :
  - émetteur **admin** : 409 explicite (il a déjà accès à la liste des comptes) ;
  - émetteur **non admin** : aucun contrôle visible, invitation créée et envoyée normalement (201) ; l'unicité est imposée à la **validation** de l'adresse (EF-4.6), où elle échoue sans rien révéler à l'émetteur (défaut, §13).
- **EF-3.11** Compte créé **actif** immédiatement, adresse **en attente** (EF-4.3) ; `users.invited_by` trace l'émetteur.

### 5.4 E-mail et validation d'adresse (ADR-0005)

- **EF-4.1 (B3)** Réglages SMTP : hôte, port (587), sécurité `starttls` | `tls` | `none`, identifiant, mot de passe (secret), expéditeur, délai (10 s). Certificat toujours vérifié. `starttls` : échec de la négociation ⇒ échec de l'envoi, **jamais de repli en clair**. `none` : **aucune authentification envoyée** ; un identifiant ou un mot de passe renseigné avec `none` ⇒ 400 à l'écriture, et refus d'envoi si l'état en base est incohérent. Hôte soumis à la liste blanche (EF-6.8) ; mot de passe lié à `smtp.host:port` (EF-5.5).
- **EF-4.2 (B3, r4)** « Envoyer un e-mail de test » à l'adresse de l'admin connecté : réponse **ok / ko seulement**, sans code ni message SMTP. **Durée = `smtp.timeout_s`** : la tentative est interrompue à cette échéance, et la réponse est rendue à cette échéance quel que soit le résultat (succès anticipé compris).
- **EF-4.3 (B2)** **Toute adresse d'un compte `password` est validée par aller-retour** : à la création du compte, l'adresse de l'invitation va dans `pending_email` et un mail de validation part (lien `verify_email`, 48 h, renvoi limité à 3 / h par compte). **Aucune validation d'office**, même si le lien d'invitation a été reçu par mail. Sans SMTP, l'adresse reste en attente. Seule exception : conversion SSO (EF-0.5).
- **EF-4.3b (R2)** **La consommation d'un jeton `verify_email` exige une session complète du compte cible** (`target_user_id == g.user.id`) :
  - sans session, ou avec la session d'un autre compte ⇒ le jeton n'est **pas** consommé, l'adresse reste en attente, page neutre « connecte-toi au compte concerné, puis rouvre ce lien » (aucun identifiant affiché) ;
  - la protection anti-login-CSRF (EF-S2) empêche un tiers de connecter la victime à son propre compte pour lui faire valider l'adresse ;
  - défense en profondeur : le mail de validation **nomme l'identifiant du compte** et dit « si tu n'as pas créé ce compte, ignore ce message » ;
  - même règle pour la validation d'un changement d'adresse (EF-4.6).
- **EF-4.4** Invariant : `users.email` ne contient qu'une adresse validée ; `pending_email` n'est jamais utilisée par l'inbox, `match_login` ni le mot de passe oublié.
- **EF-4.5** Adresse non validée : compte utilisable ; pas d'invitation ; pas de mot de passe oublié ; bandeau avec renvoi.
- **EF-4.6** Changement d'adresse : mot de passe + second facteur ; nouvelle adresse en attente ; l'ancienne reste active et reçoit une notification. À la validation (session du titulaire exigée, EF-4.3b), adresse déjà validée sur un autre compte ⇒ refus avec message neutre (« impossible de valider cette adresse »), `pending_email` effacée, audit ; contrôle et écriture dans une même transaction (EF-1.2, r7).
- **EF-4.7 (M12)** Mails en texte brut, français ; **tous les liens sont construits sur `PUBLIC_URL`**. Envoyés : invitation, validation, réinitialisation, notifications de sécurité (mot de passe changé, facteur ajouté/retiré/réinitialisé, code de secours utilisé, adresse changée).
- **EF-4.8 (M2)** Deux modes d'envoi :
  - **synchrone** (résultat utile à l'appelant) : invitation (« envoi échoué, transmets le lien ci-dessous »), e-mail de test ;
  - **file d'envoi** en mémoire, traitée par un thread dédié, pour tout le reste (mot de passe oublié, validation, notifications) : la requête ne dépend jamais de l'envoi. File bornée (1 000 messages ; pleine ⇒ message abandonné et journalisé) ; perte au redémarrage acceptée et journalisée.
  Échec ⇒ log sans le lien ; jamais de détail SMTP renvoyé.
  **En CLI (v5-3)** : la file n'est pas traitée par un thread ; toute commande qui notifie (`user-mfa-reset`, `secrets-reset`, `user-invite`, `settings-reset` si elle touche SMTP) envoie **de façon synchrone** avant de rendre la main, et sort avec un **code de retour non nul** si un envoi échoue (l'action elle-même reste faite et tracée ; le message dit quelle notification n'est pas partie).
- **EF-4.10 (T7a-n2)** **Forme canonique des adresses** : `canonical_email(adresse) = NFKC(adresse.strip()).casefold()` (partie locale et domaine ; pas de conversion IDNA). C'est la forme **stockée** et **comparée** partout :
  - à l'écriture : cible d'une invitation, `pending_email`, `email` après validation, changement d'adresse, et **`email` des comptes SSO à chaque connexion** (`upsert_from_claims`) ;
  - à la lecture : `match_login`, mot de passe oublié, contrôles d'unicité comparent `canonical_email(saisie)` à la valeur stockée, sans `lower()` SQL (qui ne replie que l'ASCII) ;
  - **migration nommée unique** sur les comptes existants (SSO compris) : réécrit `email` et `pending_email` sous forme canonique ; idempotente ; si deux comptes se retrouvent avec la même adresse, la migration **n'échoue pas** (pas d'index unique hors comptes `password`) : `WARNING`, audit `identity.collision`, bandeau (règle de collision de EF-1.2) ; deux comptes `password` en collision ne sont pas possibles avant la migration (aucun n'existe encore) ;
  - effets assumés : l'adresse affichée et utilisée pour l'envoi est en minuscules ; le dossier de l'inbox, déjà comparé sans casse (ASCII), reste trouvé ; les rares serveurs de messagerie dont la partie locale est sensible à la casse ne sont pas pris en charge.
- **EF-4.9** Anti-injection : adresses validées (syntaxe, ≤ 254, aucun CR/LF), en-têtes par `email.message.EmailMessage`, aucune donnée utilisateur dans les en-têtes hormis l'adresse.

### 5.5 Secrets des services externes (ADR-0003, ADR-0011)

- **EF-5.1** Secrets déclarés dans le registre (`routing.api_key`, `smtp.password`) ; chacun déclare sa **destination** (`routing.base_url` ; `smtp.host` + `smtp.port`).
- **EF-5.2** Chiffrement Fernet avec `SECRETS_KEY` (env seulement, distincte de `SECRET_KEY`). Donnée chiffrée indéchiffrable par toutes les clés fournies ⇒ démarrage refusé (nombre de lignes en cause).
- **EF-5.3 (n3)** Écriture seule : l'API renvoie `configured`, date, auteur, destination liée ; les **4 derniers caractères** seulement pour les secrets de type `api_key`, jamais pour un mot de passe.
- **EF-5.4** Déchiffrement au seul moment de l'appel sortant ; jamais au navigateur, jamais journalisé ; `flask backup` contient le chiffré, jamais `SECRETS_KEY`.
- **EF-5.5 (B3)** **Liaison secret ↔ destination** :
  - à l'écriture d'un secret, la destination effective normalisée (schéma, hôte, port) est enregistrée avec lui (`bound_to`) ;
  - à l'usage, destination effective ≠ `bound_to` ⇒ le secret **n'est pas envoyé**, l'appel échoue (« secret à ressaisir »), audit ;
  - modifier la destination d'un service (PATCH du réglage, `settings-reset` compris) **efface** les secrets liés dans la même transaction, audit ;
  - un secret défini **en env** n'est envoyé qu'à la destination définie **en env** ; si la base surcharge la destination, le secret d'env n'est pas utilisé.
  Priorité des secrets : base > env (ADR-0008).
- **EF-5.6 (B3, r4)** « Tester » par service : réponse **ok / ko seulement** ; aucun code HTTP, message ou délai distinctif ; uniquement vers une destination autorisée (EF-6.8). **Durée = délai du service** (`routing.timeout_s` pour le moteur, `DEM_TIMEOUT` pour le DEM, `smtp.timeout_s` pour SMTP) : la sonde est interrompue à cette échéance et la réponse rendue à cette échéance, quel que soit le résultat.
- **EF-5.7 (M15)** **Rotation de `SECRETS_KEY`** : la variable accepte une **liste de clés Fernet séparées par des virgules** ; la première chiffre, toutes déchiffrent (`MultiFernet`). Procédure : ajouter la nouvelle clé en tête, redémarrer, `flask secrets-rotate` (rechiffre secrets **et** secrets TOTP avec la première clé, ligne par ligne en transaction, rapport du nombre de lignes, audit acteur `cli`), retirer l'ancienne clé, redémarrer. Perte de toutes les clés : `flask secrets-reset` (efface secrets et TOTP ; les clés WebAuthn, non chiffrées, restent ; rétrograde les admins `password` qui n'ont **plus aucun** facteur et invalide leurs codes de secours ; peut laisser l'instance sans admin, comme EF-2.7 ; procédure documentée).

### 5.6 Réglages administrables

- **EF-6.1 (M7)** Chaque réglage du registre déclare : clé, type, défaut, **bornes explicites obligatoires** (min et max pour les nombres, longueur max pour les chaînes, nombre max d'éléments pour les listes, valeurs pour les énumérations), variable d'env, `requires_restart`, section, libellé, aide. Un test vérifie que **toute** entrée a des bornes.
- **EF-6.2** Valeur effective = base > env > défaut (ADR-0008). Ne concerne que le registre.
- **EF-6.3** Écriture validée (type, bornes) ; lot tout ou rien ; 400 avec le champ. Valeur d'env invalide ⇒ ignorée, défaut appliqué, signalée ; jamais de refus de démarrage pour un réglage du registre.
- **EF-6.3b** Revenir à la valeur par défaut = supprimer la ligne en base. Tracé.
- **EF-6.3c** Diagnostic : source par réglage, marqueur « surcharge `VAR` » (valeur d'env masquée si secret), `WARNING` au démarrage par variable surchargée (nom seul), `GET /api/admin/settings/diagnostic`, `flask settings-list`, `flask settings-reset <clé>|--all` (audit acteur `cli`).
- **EF-6.4 (M10, R3)** **Version des réglages en base** dès la v1 : toute écriture (UI, API, CLI) incrémente `settings_meta.version` dans la même transaction. Le serveur web relit la version à chaque requête (une lecture d'une ligne) et recharge son cache si elle a changé ; l'ordonnanceur de fond (§5.10) la relit **à chaque tour** et applique les nouvelles valeurs, y compris l'activation ou la désactivation des tâches. L'ordonnanceur tournant toujours, **aucun réglage v1 n'est `requires_restart`** (`INBOX_ENABLED` et `DEM_URL` compris) ; l'attribut reste dans le registre pour l'avenir.
- **EF-6.5 (M7)** Périmètre v1, bornes par défaut :
  - Comptes et sécurité : `OIDC_AUTO_ACTIVATE` ; `USER_QUOTA_MB` 0–102 400 (0 = illimité conservé pour compatibilité, défaut §13) ; validité des invitations 1 h – 30 j ; `auth.mfa_policy` `admins|all` ; mot de passe oublié on/off ; seuils du limiteur, chacun borné en bas **et en haut** (ex. échecs de mot de passe par identifiant 3–20 / 15 min ; par IP 10–100 / 15 min ; second facteur 3–10 / 15 min ; mot de passe oublié 1–10 / h par identifiant ; jetons 5–30 / 15 min par IP) ; rétention d'audit 30–3650 j.
  - E-mail : EF-4.1.
  - Dossier surveillé : `INBOX_ENABLED`, `INBOX_SCAN_INTERVAL` 5–3600 s, `INBOX_DEFAULT_USER`.
  - Altitude : `DEM_URL` (destination sortante), `DEM_BATCH_SIZE` 1–1000, `DEM_TIMEOUT` 1–120 s, `DEM_TOTAL_TIMEOUT` 10–600 s, `DEM_REQUEST_DELAY` 0–10 s, `DEM_BACKFILL_BATCH` 1–1000, `DEM_BACKFILL_INTERVAL` 30–3600 s, défaut 300 s (balayage périodique de la tâche DEM, R3) ; un import réveille la tâche immédiatement (EF-10.3, v5-5).
  - Général : `APP_TIMEZONE` (liste des fuseaux connus).
  - Carte (R1) : fonds (≤ 10 ; nom ≤ 40 ; URL `https://` ≤ 500 avec `{z}/{x}/{y}` et éventuellement `{s}`, `{r}` ; sous-domaines `{s}` : 1 à 4 lettres minuscules, défaut `abc` ; attribution ≤ 200 ; zoom max 1–22), fond par défaut, centre et zoom initiaux bornés. **Chaque hôte effectif** d'un fond (l'hôte de l'URL après remplacement de `{s}` par chacun des sous-domaines) **doit figurer dans la liste blanche des tuiles** (EF-6.8) ; sinon 400. Avertissement « pas de clé privée » conservé (l'URL part au navigateur).
  - Routage : [gpx-route-builder.md](gpx-route-builder.md) §5.6.
- **EF-6.6** **Hors registre** (env ou code seulement) : `SECRET_KEY`, `SECRETS_KEY`, `AUTH_MODE`, `OIDC_*`, `PUBLIC_URL`, `WEBAUTHN_RP_ID`, **`OUTBOUND_ALLOWED_HOSTS`**, `TRUSTED_HOSTS`, `TRUST_PROXY*`, `SESSION_*`, `DATA_DIR`, `DB_PATH`, `LOG_LEVEL`, bornes anti-abus. **Toute URL ou tout hôte vers lequel partent des données d'utilisateurs ou un secret est une frontière de sécurité** (B3), **qu'il soit appelé par le serveur ou par le navigateur des utilisateurs** (fonds de carte : IP et zones consultées, R1) : il reste dans le registre seulement s'il est soumis à EF-6.8 (et à EF-5.5 s'il porte un secret). Tout ajout au registre touchant l'authentification, les sessions, les limites dures ou une destination sortante demande une revue sécurité.
- **EF-6.7** Catalogue des types d'activité, records, seuils d'analyse : en code.
- **EF-6.8 (B3, R1, r3)** **Liste blanche des destinations sortantes** `OUTBOUND_ALLOWED_HOSTS` (env, hors registre), **une seule variable, entrées préfixées par service** (pas de joker) :
  ```
  OUTBOUND_ALLOWED_HOSTS=routing=graphhopper:8989,dem=opentopodata:5000,smtp=relay.example.com:587,tiles=a.tile.openstreetmap.org
  ```
  - services : `routing` (`routing.base_url`), `dem` (`DEM_URL`), `smtp` (`smtp.host` + `smtp.port`), `tiles` (hôtes des fonds de carte) ; une entrée n'autorise **que son service** (un hôte autorisé pour `dem` ne vaut pas pour `smtp`) ;
  - services appelés par le serveur (`routing`, `dem`, `smtp`) : **port obligatoire** (`hôte:port`), comparé exactement au port effectif (défaut du schéma compris : `https://moteur` ⇒ 443) ; entrée sans port ⇒ variable invalide, démarrage refusé ;
  - `tiles` : hôte seul, schéma `https` et port 443 implicites (seuls permis pour une tuile) ;
  - littéral IPv6 : **entre crochets obligatoirement** (`routing=[fd00::1]:8989`) ; une adresse IPv6 sans crochets ⇒ variable invalide, démarrage refusé (v5-n1) ;
  - à l'écriture d'une valeur en base : destination hors liste du service ⇒ 400, rien n'est écrit ;
  - à l'usage (requête, ordonnanceur, CLI) : destination effective hors liste ⇒ appel refusé, audit, erreur générique ; pour un fond de carte, le fond est retiré de la liste envoyée au navigateur ;
  - destinations définies **par l'env** implicitement autorisées **pour leur seul service** (`DEM_URL` d'env autorise ce `dem`, pas un `smtp`) ;
  - **tuiles par défaut** : en l'absence de toute entrée `tiles=`, la liste vaut les hôtes des fonds embarqués aujourd'hui (`a|b|c.tile.openstreetmap.org`, `server.arcgisonline.com`, `a|b|c.tile.opentopomap.org`, `a|b|c.basemaps.cartocdn.com`) ; la première entrée `tiles=` remplace entièrement cette liste par défaut ;
  - **CSP** : `img-src` passe de `https:` à `'self' data: blob:` + la liste exacte des hôtes de tuiles autorisés (`https://<hôte>`), calculée au démarrage (la liste vient de l'env, elle ne change pas à chaud) ;
  - redirections HTTP jamais suivies ; comparaison sur le nom (pas de résolution DNS) ;
  - le diagnostic affiche la liste par service et signale toute destination effective hors liste.

### 5.7 Fonctions bêta

- **EF-7.1** Registre dans `app/features.py`. Premier client : `route_builder`.
- **EF-7.2** État : `off` (défaut) | `users` (liste) | `all`. Liste conservée entre états.
- **EF-7.3** Indisponible ⇒ routes 404, entrées absentes du HTML.
- **EF-7.4** Contrôle serveur (`feature_required`). Activation **uniquement** via l'API admin (T14) : sans elle, la fonction n'est activable que par SQL (D4).

### 5.8 Gestion des utilisateurs

- **EF-8.1** Liste enrichie : méthode d'auth, adresse (validée / en attente), facteurs, crédits, invité par, invitations en attente.
- **EF-8.2 (r6)** Dernier admin actif : ni désactivé, ni rétrogradé, ni supprimé **par l'UI ou l'API**. Les commandes CLI de secours (`flask user-mfa-reset`, `flask secrets-reset`) peuvent laisser l'instance sans admin ; elles l'annoncent, et la sortie est `flask user-invite --admin`.
- **EF-8.3** Actions : crédits, lien de réinitialisation, réinitialisation du second facteur (EF-2.7), révocation des sessions, renvoi de validation, conversion SSO (EF-0.5).
- **EF-8.4 (B5)** Promotion admin : EF-2.2. Avec `OIDC_ADMIN_ROLE`, seuls les comptes SSO suivent l'IdP.
- **EF-8.5 (M6)** Bandeau « collision d'identité » tant qu'au moins deux comptes partagent (casse ignorée) un identifiant ou une adresse validée ; lien vers les comptes concernés.

### 5.9 Journal d'audit

- **EF-9.1 (m9, test T2)** `audit_log` : horodatage UTC, **`actor_type`** (`user` | `cli` | `system`, NOT NULL), `actor_id` (renseigné si et seulement si `actor_type = 'user'`), **`actor_label`** (instantané de l'identifiant pour `user` ; vide pour `cli` et `system`), action, cible, détails JSON sans secret, mot de passe, jeton ni code, IP.
  - **L'origine d'une action se lit sur `actor_type`, jamais sur `actor_label`** : un compte qui s'appellerait `cli` ou `system` (compte SSO dont l'identifiant vient de l'IdP, EF-1.2) reste `actor_type = 'user'` et ne peut pas se faire passer pour une action CLI ou système.
  - L'UI et l'API du journal affichent le type (« Compte `cli` » / « Ligne de commande » / « Système ») et filtrent sur `actor_type`, pas sur le libellé.
  - Contrainte en base : `CHECK ((actor_type = 'user') = (actor_id IS NOT NULL))` ; une ligne `cli` ou `system` ne porte aucun identifiant de compte.
- **EF-9.2 (m8)** Actions tracées : comptes, crédits, invitations, réinitialisations, second facteur, adresses, réglages (valeurs masquées pour les secrets), secrets (modifié, effacé, effacé par changement de destination, refusé pour destination différente), destinations refusées (EF-6.8), collisions d'identité, fonctions bêta, rotation et purge. Échecs de connexion : **une ligne par couple (identifiant normalisé, IP) chaque fois qu'un seuil du limiteur est atteint**, avec le nombre d'échecs de la fenêtre ; pas de ligne par échec.
- **EF-9.3** Consultation paginée, filtrable, sans modification. Rétention 90 j (30–3650), purge quotidienne par l'ordonnanceur (§5.10), tracée.

### 5.10 Ordonnanceur de fond (R3)

- **EF-10.1** L'ordonnanceur démarre **toujours** dans les modes servis (gunicorn, `flask run`), quels que soient `INBOX_ENABLED` et `DEM_URL` ; jamais en CLI ni en `TESTING` ; pas de démarrage dans le parent du reloader.
- **EF-10.1b (v5-1)** Deux catégories de tâches :
  - **tâches partagées** (`inbox`, `dem_backfill`, `audit_purge`), qui agissent sur des ressources communes à tous les processus (dossier, base) : protégées par le **verrou fichier** existant (`flock` non bloquant) ; un processus qui ne l'obtient pas **ne les exécute pas** mais **retente le verrou à chaque tour** (il prend le relais si l'autre processus s'arrête) ;
  - **tâches de processus** (`routing_health`, `settings_reload`), dont l'état vit dans le processus (cache de santé, cache des réglages) : exécutées dans **tout** processus servi, verrou ou non.
  L'état « verrou détenu / non détenu » figure dans le diagnostic.
- **EF-10.2** Il porte des **tâches à échéances propres**, chacune déclarée avec : nom, intervalle (réglage), condition d'activation (relue à chaque tour), fonction `run(db, cfg)` :

| Tâche | Intervalle | Active si |
|---|---|---|
| `inbox` | `INBOX_SCAN_INTERVAL` | `INBOX_ENABLED` |
| `dem_backfill` | `DEM_BACKFILL_INTERVAL` | `DEM_URL` défini et autorisé (EF-6.8) |
| `audit_purge` | 24 h | toujours |
| `routing_health` | `routing.health_interval_s` | moteur configuré (spec routage RB-1.2) ; premier passage **au démarrage** |
| `settings_reload` | à chaque tour | toujours (EF-6.4) |

- **EF-10.3** Un tour toutes les 5 s au plus ; chaque tâche ne s'exécute que si son échéance est atteinte. Une tâche en erreur est journalisée et replanifiée à son intervalle, sans affecter les autres.
  - **Réveil ciblé (v5-5)** : `wake(task)` rend la tâche **due immédiatement** et réveille l'ordonnanceur. Un import (upload, inbox) qui diffère la correction d'altitude appelle `wake('dem_backfill')` : l'altitude est corrigée aussitôt, comme aujourd'hui, l'intervalle de 300 s ne servant qu'au balayage périodique.
  - **Tâches longues hors de la boucle (v5-6)** : `inbox` et `dem_backfill` (jusqu'à `DEM_TOTAL_TIMEOUT`, ou un gros scan) s'exécutent dans **un thread de travail dédié**, une à la fois ; l'ordonnanceur ne fait que les planifier et exécute lui-même les tâches courtes (`routing_health`, `settings_reload`, `audit_purge`). Une tâche longue en cours n'est pas relancée tant qu'elle n'a pas fini (pas d'empilement). Le sondage de santé et la relecture des réglages ne sont donc jamais figés par une tâche longue.
- **EF-10.4** **Testabilité** : chaque tâche est une fonction appelable seule (`background.run_task('routing_health', db, cfg)`), et l'ordonnanceur expose `tick(now)` avec horloge injectée et un exécuteur des tâches longues injectable (synchrone en test). Les tests appellent ces fonctions directement, sans thread.
- **EF-10.5** La file d'envoi des mails (EF-4.8) garde son thread propre (envoi immédiat), démarré dans les mêmes conditions (sans verrou : c'est une tâche de processus) ; elle expose `drain()` pour les tests. En CLI, pas de thread : envoi synchrone (EF-4.8, v5-3).

---

## 6. Exigences de sécurité

- **EF-S1** Routes `/admin/*` et `/api/admin/*` sous `admin_required` ; test paramétré sur `app.url_map`.
- **EF-S2** Formulaires publics : POST same-origin + jeton CSRF de formulaire.
- **EF-S3 (M1, M8, M15)** Limitation de débit (ADR-0006) : **fenêtres glissantes à seuil fixe**, pas d'attente progressive ; 429 générique ; aucun verrouillage définitif. Clés :
  - mot de passe : par **identifiant saisi normalisé** (casefold, NFKC), qu'il existe ou non, et par IP ;
  - second facteur : par compte (l'identifiant est connu à ce stade, aucun oracle) ;
  - mot de passe oublié : par identifiant ou e-mail saisi normalisé, et par IP ;
  - jetons (`inspect`, `consume`) : par IP.
  Valeurs : EF-6.5. IP via `ProxyFix`. Le limiteur est consulté avant tout hachage (EF-1.5, r9).
  **Fenêtre approchée (v5-2)** : avec deux compteurs, la garantie est « seuil par fenêtre fixe » et « au plus 2 × seuil sur toute fenêtre glissante » ; les critères d'acceptation sont écrits sur ce modèle.
  **Tailles fixées (r1)** : famille « identifiant » (mot de passe, mot de passe oublié) = **65 536 compartiments** par famille, indexés par HMAC-SHA-256 (clé aléatoire tirée au démarrage) de l'identifiant normalisé ; famille « IP » = **LRU de 50 000 entrées** ; chaque compteur est une **fenêtre glissante approchée à deux compteurs** (fenêtre courante et précédente pondérées), donc de mémoire constante, au lieu d'une liste d'horodatages (ADR-0006). Tailles injectables par configuration de test.
- **EF-S4** Comparaisons en temps constant.
- **EF-S5** Logs applicatifs sans mot de passe, jeton, code ni secret ; identifiants en `%r`.
- **EF-S6** `Cache-Control: no-store` sur `/admin/*`, `/auth/*`, `/api/*`.
- **EF-S7 (M12)** `PUBLIC_URL` (schéma, hôte, port) : source unique des liens envoyés ou affichés et du RP ID ; jamais l'en-tête `Host` ni `X-Forwarded-Host`.
- **EF-S8 (B4, n4)** **Jetons hors des journaux d'accès** :
  - le jeton n'est **jamais** dans le chemin ni la query : il est dans le fragment (`#t=…`), que ni le navigateur ni le proxy n'envoient au serveur ;
  - `GET /auth/token` sert une page statique (aucune donnée du jeton côté serveur, aucune ressource tierce) ; son JS lit le fragment, l'efface de l'URL (`history.replaceState`), puis appelle `POST /auth/token/inspect` (**sans consommer**) et enfin `POST /auth/token/consume` ;
  - **`inspect` (r5)** est soumis à EF-3.6 : réponse **identique** (code, corps) pour tout jeton inconnu, expiré, utilisé ou révoqué ; pour un jeton valide, il ne renvoie **que le type de formulaire** à afficher (`choose_credentials`, `confirm_password`, `set_password`, `verify_email`), **jamais** l'adresse visée, l'émetteur, la note ni l'échéance ;
  - **aucun GET ne consomme** un jeton (les prévisualisations de messagerie et scanners de liens ne font que des GET) ;
  - conséquence : les journaux d'accès gunicorn et du proxy ne contiennent aucun jeton sans changer leur format.
- **EF-S9 (M3)** État `mfa_pending` et défis WebAuthn côté serveur (EF-1.6, EF-2.4) ; test dédié : rejouer un cookie antérieur ne restaure ni essais ni défi.
- **EF-S10 (B3)** Destinations sortantes : EF-6.8 + EF-5.5 + tests neutres (EF-4.2, EF-5.6).

---

## 7. Modèle de données

```
users              + auth_method TEXT NOT NULL DEFAULT 'oidc'     -- 'oidc' | 'password' | 'none'
                   + password_hash TEXT, password_changed_at TEXT
                   + pending_email TEXT, email_verified_at TEXT
                   + invite_credits INTEGER NOT NULL DEFAULT 0
                   + invited_by INTEGER                           -- → users(id), SET NULL
                   + totp_secret BLOB, totp_last_step INTEGER, totp_enabled_at TEXT
                   UNIQUE INDEX ON users(lower(username)) WHERE auth_method = 'password'   (M6)
                   UNIQUE INDEX ON users(email)           WHERE auth_method = 'password'   (r7 ; adresse canonique, EF-4.10)
schema_migrations  (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)                       (r2, ADR-0012)
webauthn_credentials (id, user_id → CASCADE, credential_id BLOB UNIQUE, public_key BLOB, sign_count,
                      transports, name, created_at, last_used_at)
recovery_codes     (id, user_id → CASCADE, code_hash, used_at)
auth_tokens        (id, token_hash UNIQUE, kind 'invite'|'bootstrap'|'reset'|'verify_email'|'set_password',
                    created_by, target_user_id, email, is_admin_grant, initial_credits, note,
                    sent_by_mail, created_at, expires_at, used_at, used_by, revoked_at)
settings           (key PRIMARY KEY, value, updated_at, updated_by)
settings_meta      (id = 1, version INTEGER NOT NULL)                                         (M10)
secrets            (name PRIMARY KEY, ciphertext, last4 NULL, bound_to TEXT NOT NULL, updated_at, updated_by)  (B3, n3)
feature_flags      (key PRIMARY KEY, state, updated_at, updated_by)
feature_users      (feature_key, user_id → CASCADE, PRIMARY KEY (feature_key, user_id))
audit_log          (id, at, actor_type NOT NULL, actor_id NULL, actor_label, action, target_type, target_id, details, ip)  (m9, test T2)
                   CHECK ((actor_type = 'user') = (actor_id IS NOT NULL))
```

En mémoire (pas de table) : états `mfa_pending`, défis WebAuthn, compteurs du limiteur, file d'envoi.

**Migrations (D1, r2 ; ADR-0012)** : chaque tâche apporte **sa** migration (pas de migration monolithique). Règle retenue : **migrations repérées par nom**.
- Les 5 migrations existantes restent positionnelles (`PRAGMA user_version` ≤ 5, inchangées). Toute nouvelle migration a un **nom unique et stable** (ex. `0006_audit_log`, `0011_activities_kind` — le préfixe n'est qu'un ordre de lecture) et s'enregistre dans `schema_migrations` une fois appliquée.
- Au démarrage : migrations positionnelles jusqu'à 5, puis chaque migration nommée **absente** de `schema_migrations` est appliquée, dans l'ordre de la liste du code, chacune dans sa transaction avec son enregistrement.
- Nom présent en base mais inconnu du code (base passée par une image de branche dont la migration a été renommée ou abandonnée) ⇒ `WARNING` au démarrage listant ces noms ; rien n'est annulé automatiquement.
- **« Publiée » = fusionnée sur la branche principale** (v5-n3). Une migration publiée n'est jamais modifiée ; une correction est une nouvelle migration. Une migration **modifiée avant fusion** (après avoir été poussée, donc présente dans une image `unstable-<branche>`) **change de nom** (suffixe `_v2`…) : une base passée par l'ancienne image l'appliquera bien dans sa nouvelle version, l'ancien nom apparaissant comme inconnu (`WARNING`). Chaque migration reste **idempotente** et vérifie le schéma avant d'agir (`_add_column`, `CREATE … IF NOT EXISTS`) : une base déjà « en avance » ne casse pas.
- Deux migrations dépendantes l'une de l'autre (ex. un index sur une colonne ajoutée par l'autre) vivent dans la même tâche.
- Règle d'exploitation, en complément : une image `unstable-<branche>` ne tourne jamais sur une base persistante de production (doc ops).
- Test commun : base v5 (état actuel) ⇒ toutes les migrations ; base ayant appliqué un sous-ensemble **dans un autre ordre** ⇒ les manquantes seulement, aucune sautée.
Cette règle vaut pour les deux specs (`activities.kind` de la spec routage compris).

---

## 8. API (indicative)

| Méthode | Chemin | Accès | Effet |
|---|---|---|---|
| GET/POST | `/auth/password/login` | public | Étape 1 |
| GET/POST | `/auth/password/mfa` | état `mfa_pending` | TOTP ou code de secours |
| POST | `/auth/password/mfa/webauthn/options` · `/verify` | état `mfa_pending` | WebAuthn |
| GET/POST | `/auth/password/enroll/totp` · POST `/enroll/webauthn/options` · `/enroll/webauthn/verify` | état `mfa_pending` étape `enroll` | Enrôlement obligatoire (M11) |
| GET/POST | `/auth/password/forgot` | public | Mot de passe oublié |
| GET | `/auth/token` | public | Page statique (jeton dans le fragment) |
| POST | `/auth/token/inspect` · `/auth/token/consume` | public (`consume` de `verify_email` : session complète du compte cible, R2) | Type de formulaire seulement (r5) ; consommation sans ouverture de session (B4, r8) |
| POST | `/api/me/password` · `/api/me/email` | compte `password` | EF-1.7, EF-4.6 |
| GET/POST/DELETE | `/api/me/mfa/…` | compte `password`, session complète | Gestion de ses facteurs |
| GET/POST · DELETE | `/api/invitations` · `/api/invitations/<id>` | qui peut inviter | Siennes ; création ; révocation |
| GET · PATCH | `/api/admin/users` · `/api/admin/users/<id>` | admin | `is_admin` (EF-2.2), `is_active`, `invite_credits` |
| POST | `/api/admin/users/<id>/{reset-link,mfa-reset,revoke-sessions,resend-verification,convert}` | admin | EF-8.3 |
| GET | `/api/admin/invitations` | admin | Toutes |
| GET/PATCH · DELETE · GET | `/api/admin/settings` · `/settings/<clé>` · `/settings/diagnostic` | admin | Réglages |
| GET · PUT/DELETE · POST | `/api/admin/secrets` · `/secrets/<name>` · `/secrets/<name>/test` | admin | Secrets |
| POST | `/api/admin/mail/test` | admin | E-mail de test |
| GET/PATCH | `/api/admin/features` | admin | Fonctions bêta |
| GET | `/api/admin/audit` | admin | Journal |

`/api/users` existante : conservée ou redirigée, choix noté dans la PR.

## 9. UI

- Administration : onglets **Utilisateurs · Invitations · Clés API · E-mail · Réglages · Fonctions bêta · Journal** ; bandeaux : SMTP absent, collision d'identité, destination hors liste blanche.
- Profil : invitations, mot de passe, adresse, second facteur.
- Connexion : `password` ⇒ formulaire seul ; `oidc` ⇒ SSO + formulaire toujours visible (masqué seulement si EF-0.4).
- Réglages : source, surcharge, retour au défaut ; avertissement « modifier la destination efface le secret associé » ; fonds de carte : hôtes autorisés affichés, fond hors liste signalé.
- Diagnostic : état de chaque tâche de fond (active, dernière exécution, dernier résultat ok/erreur).
- Liens d'invitation et codes de secours : affichés une fois, bouton « Copier ».

---

## 10. Critères d'acceptation

| # | Critère |
|---|---|
| CA-1 | Aucune route ne crée de compte `password` sans jeton valide. |
| CA-2 | 0 crédit ⇒ 409 ; 1 crédit et deux créations concurrentes ⇒ une seule réussit, crédit final 0. |
| CA-3 | Un admin crée N invitations sans que ses crédits bougent. |
| CA-4 | Non-admin qui envoie `invite_credits` ⇒ 403. |
| CA-5 (B4, r5) | Jeton réutilisé / expiré / révoqué / inconnu ⇒ même réponse, **pour `consume` comme pour `inspect`**. `inspect` d'un jeton valide ne contient ni l'adresse, ni l'émetteur, ni la note, ni l'échéance. Après un parcours complet d'invitation, de réinitialisation et d'amorçage, le jeton clair n'apparaît **ni en base, ni dans les logs applicatifs, ni dans les logs d'accès** (capture du flux gunicorn en test). Un GET (avec ou sans fragment) ne consomme jamais. |
| CA-6 | Révocation ou expiration ⇒ crédit non rendu. |
| CA-7 (M1, v5-2) | Login : compte inconnu / désactivé / SSO / mauvais mot de passe ⇒ même message, même code. Horloge injectée, modèle à deux compteurs (EF-S3) : **6 échecs dans une même fenêtre fixe de 15 min ⇒ le 6e est refusé (429)** ; sur **toute** fenêtre glissante de 15 min, **au plus 2 × seuil** échecs acceptés (vérifié en balayant le début de fenêtre) ; comportement identique pour un identifiant existant et un identifiant inexistant. |
| CA-8 (m5) | Mot de passe < 12 après NFKC, = identifiant ou e-mail (casse ignorée), ou courant ⇒ refusé ; hash argon2id. |
| CA-8b (EF-1.4b) | `qwerty123456`, `QWERTY123456` et sa forme pleine chasse (NFKC) ⇒ refusés ; une entrée de la liste locale (`motdepasse1234`) ⇒ refusée ; le fichier livré compte 46 187 entrées et son SHA-256 est celui de EF-1.4b ; `common_passwords.SOURCE` contient l'avis MIT complet. Test sur le fichier : chaque ligne est NFKC + casefold, de longueur 12–128, unique, triée, sans ligne vide. Le script relancé sur la copie de la source (SHA-256 vérifié) produit un `common_passwords.txt` identique octet pour octet ; une source au SHA-256 différent ⇒ arrêt du script. Fichier absent ou vide ⇒ refus de démarrage en mode `password` ; en mode `local`, la liste n'est pas chargée. Chargement mesuré au démarrage < 500 ms dans l'image. |
| CA-9 (M3) | Avec un état `mfa_pending` : toutes les routes protégées répondent comme sans session ; état expiré au bout de 5 min **côté serveur** ; rejouer un cookie antérieur ne remet pas les essais à zéro ; le cookie ne contient pas `user_id`. |
| CA-10 (m2) | TOTP : code valide accepté ; même code rejoué, code d'un pas antérieur ou deux soumissions concurrentes du même code ⇒ une seule acceptation ; secret chiffré. |
| CA-11 (M3, M12) | WebAuthn : défi réutilisé (y compris via cookie rejoué), origine ≠ `PUBLIC_URL`, RP ID différent, compteur en régression ⇒ refus ; `http://localhost` refusé hors test/debug. |
| CA-12 (m3) | Codes de secours : 10 × 16 caractères base32, usage unique (concurrence comprise), régénération invalide les anciens. |
| CA-13 (B5) | Promotion d'un compte `password` sans facteur ⇒ 409 (API et `flask user-admin`) ; avec facteur ⇒ OK. Suppression du dernier facteur d'un admin ⇒ refus. Réinitialisation du second facteur d'un admin ⇒ compte rétrogradé. `mfa_policy=all` avec un compte `password` actif sans facteur ⇒ 409. Un compte `password` admin sans facteur (état forcé en base) ⇒ 403 sur les routes admin. |
| CA-14 | Après changement / réinitialisation de mot de passe ou de second facteur, un cookie antérieur est refusé. |
| CA-15 (M2) | Mot de passe oublié : même code et même corps pour compte existant, inexistant, SSO, adresse non validée ; **l'envoi n'a jamais lieu dans le thread de la requête** (mailer simulé : appel de `send` depuis un autre thread uniquement) ; un seul mail dans le cas éligible. |
| CA-16 | Adresse non validée : absente de `users.email`, inutilisée par l'inbox et `match_login`, invitation refusée. |
| CA-17 (B2) | Compte créé depuis une invitation (lien reçu par mail **ou** affiché à l'émetteur) ⇒ adresse **en attente** et mail de validation ; seule la consommation du jeton `verify_email` la valide. |
| CA-17b (R2) | `verify_email` consommé **sans session**, ou avec la **session d'un autre compte** ⇒ adresse non validée, jeton non consommé (réutilisable par le titulaire), page neutre sans identifiant. Avec la session complète du compte cible ⇒ adresse validée. Le mail de validation contient l'identifiant du compte et la phrase « si tu n'as pas créé ce compte, ignore ce message ». Même comportement pour un changement d'adresse. |
| CA-18 | Adresse avec CR/LF ⇒ refusée avant envoi. |
| CA-19 | Aucun secret relu en clair par aucune route ; 4 derniers caractères absents pour `smtp.password`. |
| CA-20 | Chiffré en base indéchiffrable ⇒ démarrage refusé. Mode `password` sans `SECRETS_KEY`, `PUBLIC_URL`, `TRUSTED_HOSTS`, avec `OIDC_ISSUER`, ou avec `PUBLIC_URL` hors `TRUSTED_HOSTS` ⇒ démarrage refusé. |
| CA-21 | Variable d'env seule ⇒ `source: env` ; PATCH ⇒ `source: base`, marqueur de surcharge, `WARNING` au démarrage suivant ; reset ⇒ `source: env`. Invalide ⇒ 400, lot non écrit. Aucun réglage de EF-6.6 dans le registre. |
| CA-21b | Valeur d'env invalide ⇒ démarrage OK, défaut appliqué, diagnostic. |
| CA-21c (M7) | Toute entrée du registre a des bornes ; valeur au-delà de la borne haute (ex. seuil du limiteur) ⇒ 400. |
| CA-21d (M10, R3) | Après `flask settings-reset` (autre processus), la **requête suivante** du serveur en cours voit la nouvelle valeur. Avec `tick(now)` et horloge injectée : après modification d'`INBOX_SCAN_INTERVAL` ou passage d'`INBOX_ENABLED` à vrai, le tour suivant applique la nouvelle échéance ou active la tâche, sans redémarrage. |
| CA-22 | Fonction bêta : `off` ou `users` sans l'utilisateur ⇒ 404 et absente du HTML ; `users` avec lui ou `all` ⇒ accès. |
| CA-23 | Chaque route admin : 401 sans session, 403 non-admin. |
| CA-24 (m8, m9) | Chaque action de EF-9.2 produit une ligne d'audit sans secret ; seuil du limiteur atteint ⇒ une ligne par (identifiant, IP) et par fenêtre ; action CLI ⇒ `actor_type = 'cli'`, `actor_id` nul ; tâche de l'ordonnanceur (purge) ⇒ `actor_type = 'system'`. |
| CA-24b (test T2) | Compte SSO dont l'IdP fournit l'identifiant `cli` (ou `system`, `CLI`) : ses actions sont enregistrées avec `actor_type = 'user'` et son `actor_id` ; le filtre « Ligne de commande » du journal ne les renvoie pas ; l'affichage les distingue d'une action CLI. Insertion d'une ligne `actor_type = 'cli'` avec `actor_id` non nul ⇒ refusée par la base. Création ou conversion vers un compte `password` nommé `cli`, `System`, `local`… ⇒ refus (EF-1.2b). |
| CA-25 | `AUTH_MODE=local` ⇒ routes mot de passe, second facteur et jetons en 404. |
| CA-26 | Migrations depuis une base v5 : données intactes ; instance OIDC sans `SECRETS_KEY` démarre. |
| CA-27 (r6) | Dernier admin actif : ni désactivé, ni rétrogradé, ni supprimé **par l'API** (409). `flask user-mfa-reset` sur le dernier admin ⇒ avertissement affiché, rétrogradation effectuée, audit. Un admin dont il ne reste que des codes de secours (TOTP effacé par `secrets-reset`, aucune clé WebAuthn) ⇒ rétrogradé, codes invalidés ; un admin qui garde une clé WebAuthn ⇒ reste admin. |
| CA-28 (B1) | Mode `password`, sans session : chaque route du `url_map` hors liste blanche ⇒ 401 ou redirection vers la connexion ; **aucune ligne `sub='local'` créée** ; le context processor expose `auth_mode='password'`. |
| CA-29 (M4) | Bascule `oidc` → `password` : un cookie de session d'un compte `oidc` émis avant ⇒ refusé, session vidée. |
| CA-30 (M5) | Conversion : `sub` réécrit en `pwd:…` ; une connexion OIDC ultérieure avec l'ancien `sub` (instance repassée en `oidc`) crée un **nouveau** compte et ne retrouve pas le converti ; identifiant choisi conforme EF-1.2 ; ancien admin ⇒ `is_admin = 0`. |
| CA-31 (M6) | Création d'un compte `password` avec un identifiant pris (casse ignorée) par n'importe quel compte ⇒ refus. Connexion OIDC qui donne à un compte SSO l'identifiant d'un compte `password` ⇒ connexion acceptée, `WARNING`, audit `identity.collision`, bandeau admin ; le login et le mot de passe oublié du compte `password` fonctionnent toujours. |
| CA-32 (M8, r1) | Tailles injectées en test (ex. 64 compartiments, LRU de 100) : un nombre de clés d'IP distinctes supérieur à la LRU n'efface pas le compteur d'un identifiant visé ; un nombre d'identifiants distincts 100 fois supérieur au nombre de compartiments ne fait pas croître la mémoire du limiteur (taille des structures constante). En configuration par défaut : 65 536 compartiments, LRU de 50 000. |
| CA-33 (M11) | Amorçage : le lien d'amorçage ne donne aucune session complète avant enrôlement d'un facteur ; le compte n'est admin qu'après enrôlement. |
| CA-34 (B3) | Changer `smtp.host` ou `routing.base_url` ⇒ secret lié effacé (audit). Secret dont `bound_to` ≠ destination effective ⇒ non envoyé (serveur factice : aucune réception). Destination en base surchargeant une destination d'env ⇒ le secret d'env n'est pas envoyé. |
| CA-35 (B3) | `smtp.security = none` avec identifiant ou mot de passe ⇒ 400 ; envoi en `none` : aucune commande `AUTH` reçue par le serveur factice. `starttls` refusé par le serveur ⇒ échec, aucun envoi en clair. |
| CA-36 (B3) | Destination hors `OUTBOUND_ALLOWED_HOSTS` ⇒ 400 à l'écriture ; forcée en base ⇒ appel refusé à l'usage (requête et ordonnanceur de fond), audit. Destination d'env hors liste ⇒ autorisée. |
| CA-37 (B3, r4) | « Tester » et e-mail de test : réponses ok et ko identiques en forme, sans code ni message ; durée de réponse = délai du service (`routing.timeout_s`, `DEM_TIMEOUT`, `smtp.timeout_s`) à 10 % près, que la cible factice réponde tout de suite, refuse la connexion ou ne réponde jamais. |
| CA-38 (M15) | `SECRETS_KEY=k2,k1` : lecture d'un secret chiffré par `k1` OK ; `flask secrets-rotate` ⇒ tous les secrets et TOTP rechiffrés par `k2` ; ensuite `SECRETS_KEY=k2` seule ⇒ démarrage OK et secrets lisibles. |
| CA-39 (m6, m7) | Non-admin invitant une adresse déjà portée ⇒ 201, même réponse qu'une adresse libre ; la validation ultérieure échoue avec message neutre. Non-admin envoyant une note ⇒ 400. |
| CA-40 (m4) | 10 connexions simultanées : au plus 2 hachages en parallèle (instrumentation du sémaphore). |
| CA-41 (R1) | Fond de carte dont un hôte effectif (après `{s}`) est hors des entrées `tiles=` ⇒ 400, rien n'est écrit. Fond forcé en base hors liste ⇒ absent de la liste envoyée au navigateur, audit. En-tête CSP : `img-src` ne contient plus `https:` mais exactement `'self' data: blob:` et les hôtes de tuiles autorisés. Sans entrée `tiles=` : les 5 fonds actuels s'affichent comme avant (hôtes par défaut). |
| CA-42 (r3) | `OUTBOUND_ALLOWED_HOSTS` avec une entrée `routing`, `dem` ou `smtp` sans port ⇒ démarrage refusé. Entrée `dem=h:5000` : `DEM_URL=http://h:5000/…` accepté, `smtp.host=h` port 5000 refusé (autre service), `routing.base_url=http://h:5001` refusé (autre port). `DEM_URL` venue de l'env sans entrée : autorisée pour `dem` seulement. |
| CA-43 (R3) | Instance servie avec `INBOX_ENABLED=0` et sans `DEM_URL` : l'ordonnanceur tourne, la purge d'audit supprime les lignes de plus de 90 j (horloge injectée), le sondage du moteur s'exécute au démarrage puis à son intervalle. Chaque tâche est appelable seule en test (`run_task`). Une tâche qui lève une exception n'empêche pas les autres de s'exécuter au même tour. |
| CA-44 (r7) | Deux validations concurrentes de la même adresse sur deux comptes `password` ⇒ une seule réussit ; l'autre reçoit le refus neutre. |
| CA-45 (r8) | Consommation d'une invitation, d'une réinitialisation ou d'une définition de mot de passe ⇒ aucune session complète (pas de `user_id` en session), redirection vers la connexion ; avec `mfa_policy=all`, la connexion suivante passe par l'enrôlement. |
| CA-46 (r9) | Identifiant au-delà de son seuil : 20 requêtes simultanées ⇒ toutes en 429, **aucune** acquisition du sémaphore de hachage (instrumentation). |
| CA-48 (v5-1) | Deux processus servis sur le même `DATA_DIR` : celui sans verrou exécute `routing_health` et `settings_reload`, pas `inbox`, `dem_backfill` ni `audit_purge` ; le processus détenteur s'arrête ⇒ l'autre obtient le verrou au tour suivant et reprend les tâches partagées. |
| CA-49 (v5-3) | `flask user-mfa-reset <login>` avec un serveur SMTP factice ⇒ notification reçue avant la fin de la commande ; serveur SMTP en panne ⇒ réinitialisation faite et tracée, code de retour non nul, message nommant la notification non envoyée. |
| CA-50 (v5-4) | Conversion d'un compte SSO dont l'adresse est déjà validée sur un compte `password` ⇒ aucune erreur d'intégrité, conversion faite, adresse en `pending_email`, réponse à l'admin le signalant ; sans collision ⇒ adresse conservée validée. |
| CA-51 (v5-5, v5-6) | Exécuteur des tâches longues injecté et bloqué (simulant un passage DEM de 600 s) : `tick(now)` continue d'exécuter `routing_health` et `settings_reload` à leurs échéances ; un second `dem_backfill` n'est pas lancé tant que le premier tourne. Après un import avec correction différée, `wake('dem_backfill')` ⇒ la tâche est exécutée au tour suivant, sans attendre 300 s. |
| CA-52 (v5-n1, v5-n2) | `OUTBOUND_ALLOWED_HOSTS=routing=fd00::1:8989` ⇒ démarrage refusé ; `routing=[fd00::1]:8989` ⇒ accepté. `flask user-invite --admin --account <login>` sur un compte `password` sans facteur ⇒ jeton d'amorçage lié à ce compte ; sur un compte qui a un facteur ou qui est SSO ⇒ refus. |
| CA-54 (T7a-n1) | `flask user-invite --admin --account bob` sur un compte `password` sans facteur ⇒ jeton émis ; `inspect` ⇒ `confirm_password` ; mauvais mot de passe ⇒ refus neutre, jeton toujours utilisable, compteur du limiteur incrémenté ; bon mot de passe ⇒ état `enroll`, aucune session complète ; après enrôlement ⇒ `is_admin = 1`, hash du mot de passe **identique** à avant, redirection vers la connexion. Avec `--reset-password` ⇒ formulaire `set_password`, ancien mot de passe refusé ensuite, nouvelles règles de politique appliquées. Compte ayant obtenu un facteur entre-temps ⇒ jeton refusé. |
| CA-55 (T7a-n2) | Invitation de `Alice@Example.COM` ⇒ `pending_email = 'alice@example.com'` ; validation ⇒ `email` identique ; seconde invitation / validation pour `ALICE@example.com` sur un autre compte `password` ⇒ refus d'unicité. Connexion SSO avec `Bob@Corp.FR` ⇒ `email = 'bob@corp.fr'`. `Élodie@Exemple.fr` ⇒ `élodie@exemple.fr`. Migration sur une base contenant deux comptes SSO `X@a.fr` et `x@a.fr` ⇒ migration réussie, `WARNING`, audit `identity.collision` ; rejouée ⇒ aucun changement. `match_login('Alice@EXAMPLE.com')` trouve le compte. |
| CA-47 (r2) | Base v5 ⇒ toutes les migrations nommées appliquées et enregistrées. Base où `B` a été appliquée avant `A` (ordre inverse de la liste) ⇒ au démarrage, seule `A` est appliquée, aucune n'est sautée ni rejouée. Nom inconnu du code dans `schema_migrations` ⇒ `WARNING`, démarrage OK. Rejouer une migration sur une base qui l'a déjà (table vidée à la main) ⇒ aucune erreur (idempotence). |

## 11. Cas limites

- Deux acceptations simultanées avec le même identifiant ⇒ une seule réussit ; le jeton de l'autre reste utilisable.
- Émetteur rétrogradé après émission ⇒ invitations valides ; il perd l'illimité.
- Expiration pendant que le formulaire est ouvert ⇒ refus au `consume`.
- Horloge : UTC serveur ; dérive > 30 s ⇒ TOTP en échec (NTP, doc ops).
- Changement de `PUBLIC_URL` ⇒ clés WebAuthn inutilisables et anciens liens invalides (hôte) : documenté ; `WEBAUTHN_RP_ID` permet de figer le RP ID sur le domaine parent.
- SMTP en panne à l'invitation ⇒ lien affiché, crédit consommé.
- Variable d'env modifiée mais surchargée en base ⇒ `WARNING`, marqueur.
- Bascule `oidc` → `password` alors que tous les admins sont SSO ⇒ plus d'admin connectable : `flask user-invite --admin` (amorçage) ; les admins SSO convertis perdent l'admin (EF-0.5) et sont repromus après enrôlement.
- Perte de `SECRETS_KEY` ⇒ `flask secrets-reset` ; admins `password` sans clé WebAuthn rétrogradés (plus de facteur), réamorçage par CLI si plus aucun admin.
- Victime qui reçoit un mail de validation pour un compte qu'elle n'a pas créé et clique ⇒ rien n'est validé (pas de session du compte), le mail lui a dit d'ignorer (R2).
- Opérateur qui ajoute un fond de carte d'un nouveau fournisseur ⇒ entrée `tiles=` à ajouter dans l'env et redémarrage (la CSP est calculée au démarrage), puis saisie du fond dans l'admin.
- Instance sans inbox ni DEM ⇒ l'ordonnanceur tourne quand même (purge, sondage, rechargement des réglages).
- Bascule depuis `local` ⇒ compte `local` inaccessible, réattribution hors v1.
- `OUTBOUND_ALLOWED_HOSTS` réduite alors qu'une destination en base n'y figure plus ⇒ appels refusés, bandeau admin, diagnostic.
- Redémarrage ⇒ connexions en deux étapes en cours et file d'envoi perdues (journalisé).

---

## 12. Arbitrages intégrés

| Sujet | Arbitrage (relayé par coord, 2026-11-24) | Vague |
|---|---|---|
| Q1 | `AUTH_MODE=password` (ADR-0001) | 1 |
| Q2 | Secrets = services externes | 1 |
| Q4 / Q5 | Compte actif tout de suite, validation d'adresse, SMTP | 1 |
| Q6 | TOTP et WebAuthn en v1 | 1 |
| Q11 | Bêta : liste d'utilisateurs ou tous | 1 |
| Q7, Q8, Q10 | Valeurs proposées par défaut | 1 |
| Q9 | Base > env (ADR-0008) | 2 |
| ADR 0002–0006 | Acceptés | 2 |
| MFA | Obligatoire pour les admins | 2 |
| Q3 · Q12 · Q13 · Q14 | Crédit non rendu · compte `password` admin possible · audit 90 j · formulaire toujours visible | 2 |
| — | Mot de passe oublié, restrictions avant validation, e-mails de sécurité, conversion SSO → mot de passe | 2 |
| — | Ni `SETTINGS_LOCKED` ni réglage `critical` en v1 | 3 |
| **B2** | **Toujours** un aller-retour de validation d'adresse | post-review |
| **B3** | Secret lié à sa destination ; SMTP `none` sans authentification ; `OUTBOUND_ALLOWED_HOSTS` (env, hors registre) sur `routing.base_url`, `DEM_URL`, `smtp.host` ; « Tester » sans détail distinctif (ADR-0011) | post-review |
| **B5** | Promotion admin refusée sans second facteur actif ; idem pour `mfa_policy=all` | post-review |
| **M15** | Rotation de `SECRETS_KEY` dans la spec | post-review |
| B1, B4, M1–M14, mineurs, découpage | Attendu de la review appliqué (écarts signalés en §13) | post-review |
| **R1** | Fonds de carte sous liste blanche d'hôtes en env (entrées `tiles=` d'`OUTBOUND_ALLOWED_HOSTS`), CSP `img-src` restreinte à ces hôtes, URL hors liste ⇒ 400 | re-review |
| R2, R3, r1–r9, n7–n8 | Attendu de la re-review appliqué (choix de r2 justifié dans l'ADR-0012) | re-review |
| v5-1 à v5-6, v5-n1 à v5-n3 | Attendu de la re-review v5 appliqué, sans nouvel arbitrage | re-review v5 |

## 13. Tranché par défaut

Statut (relayé par coord le 2026-11-24) : **tous confirmés par l'utilisateur** — n° 1 à 10 ; n° 11 à 20 (formulaire « Défauts v5 », aucune objection, specs v5 validées ; n° 16 entériné via l'ADR-0012) ; n° 21 à 26.

1. **B5, « idem `mfa_policy=all` »** lu comme : passage à `all` **refusé** tant qu'un compte `password` actif n'a pas de facteur. Alternative : passage accepté, sessions de ces comptes révoquées et enrôlement imposé à leur prochaine connexion.
2. **Réinitialisation du second facteur d'un admin ⇒ rétrogradation** (conséquence de l'invariant B5) ; repromotion après enrôlement.
3. **Conversion SSO** : l'adresse vérifiée par l'IdP reste validée (exception à B2, qui vise les invitations) ; un admin converti perd l'admin jusqu'à enrôlement.
4. **Collision d'identité créée par l'IdP** : connexion acceptée + alerte (log, audit, bandeau), pas de refus.
5. **`USER_QUOTA_MB = 0` (illimité)** conservé dans les bornes pour ne pas casser les déploiements existants ; le supprimer fermerait le risque disque (M7) mais une valeur d'env `0` serait alors ignorée au profit du défaut 2048.
6. **m6** : contrôle d'adresse visible des seuls admins ; pour un non-admin, l'invitation est créée et l'unicité joue à la validation.
7. **m7** : note d'invitation réservée aux admins.
8. **M2** : envoi synchrone conservé pour l'invitation et l'e-mail de test, file d'envoi pour tout le reste.
9. **`OUTBOUND_ALLOWED_HOSTS` vide** ⇒ seules les destinations définies en env sont autorisées (aucune destination nouvelle depuis l'UI).
10. **Sémaphore de hachage** à 2 places (m4) et file d'envoi à 1 000 messages.

*Ajoutés en v5 (re-review) — confirmés :*

11. **R1 — une seule variable** `OUTBOUND_ALLOWED_HOSTS` avec des entrées préfixées par service (`tiles=` compris) plutôt qu'une liste dédiée aux tuiles : un seul endroit à auditer, et le préfixe empêche qu'un hôte autorisé pour un service serve à un autre (r3).
12. **R1 — tuiles par défaut** : sans entrée `tiles=`, les hôtes des 5 fonds actuels restent autorisés (aucun changement visible à la mise à jour) ; la première entrée `tiles=` remplace toute la liste par défaut.
13. **R2 — pas de reprise automatique** après connexion : l'utilisateur rouvre le lien de validation une fois connecté (pas de jeton conservé côté navigateur).
14. **R3 — ordonnanceur** : un tour toutes les 5 s au plus, purge d'audit toutes les 24 h, nouvel intervalle `DEM_BACKFILL_INTERVAL` (défaut 300 s) pour séparer la tâche DEM de l'inbox ; la file d'envoi garde son propre thread.
15. **r1 — fenêtre glissante approchée à deux compteurs** au lieu d'une liste d'horodatages par compartiment (que suggérait la re-review) : avec un seuil max de 100 par IP, une liste d'horodatages coûterait jusqu'à ~40 Mo pour la LRU ; deux compteurs pondérés tiennent en mémoire constante, au prix d'une approximation de la fenêtre (erreur bornée par le seuil de la fenêtre précédente). Tailles : 65 536 compartiments, LRU de 50 000 entrées.
16. **r2 — migrations repérées par nom** (ADR-0012, choix délégué à la spec, **entériné**) plutôt que la seule règle « idempotence + pas d'image de branche sur une base persistante » : la règle seule repose sur la discipline et ne détecte rien, alors qu'avec `PRAGMA user_version` une base passée par une image de branche **saute une migration sans erreur**. Le registre par nom rend l'ordre de fusion indifférent ; l'idempotence et la règle d'exploitation sont gardées en défense en profondeur.
17. **r3 — port obligatoire** pour les entrées `routing`, `dem`, `smtp` (pas de « port par défaut implicite », qui pour SMTP serait ambigu entre 25, 465 et 587).
18. **r6 — les codes de secours ne comptent pas comme facteur actif** ; ils sont invalidés quand le compte n'a plus de facteur.
19. **r8 — redirection vers la connexion** après toute consommation de jeton (plutôt qu'un état `mfa_pending` créé à la consommation) : un seul chemin d'ouverture de session, celui de EF-1.6.
20. **n8 (spec routage)** — avant le premier sondage du moteur, la fonction est traitée comme **indisponible** (503), ce qui remplace le défaut v4 « réputé joignable » ; le premier sondage a lieu au démarrage de l'ordonnanceur.

*Ajoutés en v6 (re-review v5), là où la review laissait le choix — confirmés :*

21. **v5-3** : envoi **synchrone** en CLI (plutôt que `drain()` de la file) — même résultat, sans file à vider.
22. **v5-4** : à la conversion, adresse en collision **mise en attente** plutôt que conversion refusée — la conversion n'est pas bloquée par une collision que l'IdP a créée.
23. **v5-5** : `wake(task)` ciblé, **défaut de 300 s conservé** pour le balayage (plutôt que revenir à 30 s) — la latence visible est réglée par le réveil, le balayage n'attrape que les reliquats.
24. **v5-6** : **thread de travail dédié** aux tâches longues (plutôt que documenter la latence) — l'état de santé et les réglages ne sont jamais figés.
25. **v5-n2** : option `--account <login>` pour reprendre un amorçage abandonné.
26. **v5-n3** : renommage obligatoire d'une migration modifiée avant fusion ; l'empreinte du code de la migration (autre option de la review) est écartée : un simple reformatage déclencherait des alertes.

*Ajoutés pour la liste de mots de passe courants (EF-1.4b) — confirmés par l'utilisateur :*

27. **Liste d'amorce conservée en union** avec la liste publique (`common_passwords_local.txt`) : les mots de passe français et propres au projet restent refusés.
28. **Liste publique = top 1 000 000** (arbitrage, au lieu du top 10 000 qui ne donnait que 24 entrées à 12 caractères ou plus) : 46 146 entrées utiles, 46 187 avec la liste locale.
29. **Licence acceptée en l'état** : dépôt SecLists MIT (vérifié), données de Burnett (2015) au statut non vérifiable ; avis MIT et provenance conservés dans `common_passwords.SOURCE`.

*Ajouté après le test de T2 — confirmé par l'utilisateur :*

30. **Usurpation de l'acteur dans l'audit** : option retenue et **confirmée par l'utilisateur** = **colonne `actor_type`** (structurelle, couvre aussi les comptes SSO dont l'identifiant vient de l'IdP), **plus** une liste d'identifiants réservés pour les comptes `password` (défense en profondeur). La liste réservée seule ne suffit pas : un compte SSO nommé `cli` par l'IdP ne peut pas être refusé à la connexion. Le préfixe dans `actor_label` (autre option proposée) est écarté : il garde l'information de type dans une chaîne libre, que l'UI et les filtres devraient analyser.

*Ajoutés après T7a (dev / review) — confirmés par l'utilisateur :*

31. **Reprise d'amorçage (`--account`)** — **confirmé par l'utilisateur** : le jeton exige le **mot de passe actuel** et le conserve, puis mène aux routes d'enrôlement standard ; option `--reset-password` pour le cas du mot de passe perdu. Recommandé plutôt qu'un lien qui mènerait directement à l'enrôlement (un lien intercepté donnerait un compte admin à son porteur) ou qu'une réinitialisation systématique du mot de passe (inutile quand il est connu).
32. **Adresses sous forme canonique** (NFKC + `casefold`, sans IDNA) stockées pour **tous** les comptes, SSO compris, avec une migration des adresses existantes — **confirmé par l'utilisateur**. Retenu plutôt que de canonicaliser les seuls comptes `password` : `lower()` de SQLite ne replie que l'ASCII, et des formes différentes entre comptes SSO et `password` rendraient la détection des collisions incomplète. Effet visible : adresses affichées en minuscules.

**Écarts à la review v3, jugés par la re-review** : M8 (compartiments HMAC) **accepté** ; D2 (T10 avant T11) **accepté** ; D1 (numéro attribué à la fusion) **accepté sous condition** ⇒ remplacé par le choix r2 ci-dessus.

## 14. Découpage en tâches

Chaque tâche apporte sa migration nommée (§7) et ses tests.

0. **T0** Registre des migrations par nom (`schema_migrations`, compatibilité `user_version` ≤ 5, `WARNING` sur nom inconnu, règle « publiée = fusionnée », v5-n3) ; CA-47. **Avant toute tâche qui ajoute une migration**, dans les deux specs (r2).
1. **T2** `audit.py` (`actor_type` + `actor_id` + `actor_label`, contrainte CHECK, CA-24b) ; la purge est une tâche de l'ordonnanceur (T19).
2. **T3** `settings/registry.py` (bornes obligatoires) + `store.py` (base > env, **version en base**, rechargement par requête), diagnostic, `settings-list` / `settings-reset` ; `USER_QUOTA_MB` en pilote.
3. **T19** **Ordonnanceur de fond** (`background.py`, §5.10, R3) : démarrage systématique en mode servi, tâches à échéances propres, `run_task` et `tick(now)` testables ; migration des tâches `inbox` et `dem_backfill` depuis `InboxWatcher` (verrou fichier conservé) ; tâches `settings_reload` et `audit_purge` ; **verrou limité aux tâches partagées et retenté à chaque tour (v5-1)**, **`wake(task)` ciblé (v5-5)**, **thread de travail pour les tâches longues (v5-6)** ; CA-43, CA-48, CA-51. Dépend de T2, T3. **Prérequis de TR5** (sondage du moteur).
4. **T4** `secrets_store.py` (multi-clés, `secrets-rotate`, `secrets-reset`) + contrôles `SECRETS_KEY`.
5. **T18** `outbound.py` : `OUTBOUND_ALLOWED_HOSTS` par service (ports obligatoires, autorisation implicite par service, **entrées `tiles=` et hôtes par défaut**), **CSP `img-src` calculée depuis les hôtes de tuiles** (R1), liaison secret ↔ destination, tests neutres à durée = délai du service (r4). Dépend de T3, T4.
6. **T5** `features.py` + `feature_required`. Dépend de T3.
7. **T6** `passwords.py` (sémaphore) + `ratelimit.py` (familles bornées, fenêtre à deux compteurs) ; CA-7 et CA-32 écrits sur le modèle à deux compteurs (v5-2).
8. **T6b** (petite tâche, après T6) Liste de mots de passe courants : script de génération (top 1 000 000, SHA-256 vérifié), fichiers `wordlists/` (46 187 entrées), `SOURCE` avec avis MIT et provenance, chargement au démarrage, suppression de `common_passwords.py`, CA-8b (EF-1.4b).
9. **T7a** `tokens.py` : jetons à usage unique de tous types (`invite`, `bootstrap`, `reset`, `verify_email`, `set_password`) — création (≥ 256 bits, stockage haché SHA-256), inspection, consommation atomique, révocation, expiration ; migration `auth_tokens`. Module sans route HTTP. Dépend de T0. **Avant T8**, qui en a besoin pour `flask user-invite --admin`.
10. **T8** `modes.py` + **refonte du garde et du context processor sur `AUTH_MODE`** (B1), refus des sessions de méthode non permise (M4), mode `password` et `PUBLIC_URL` dans `checks.py`, `flask user-invite --admin`, `flask user-mfa-reset`, `flask user-admin` soumis à B5, `--account` de l'amorçage (v5-n2) ; **commandes CLI qui notifient : envoi synchrone et code de retour (v5-3, avec T9)** ; CA-49, CA-52. Dépend de T2, **T7a**.
11. **T7b** `invitations.py` (règles d'invitation, crédits et décrément atomique, révocation sans remboursement) + routes `/auth/token` (page, jeton dans le fragment), `/auth/token/inspect`, `/auth/token/consume` et leur entrée dans `PUBLIC_ENDPOINTS` ; l'adresse est créée en attente (B2), **sous forme canonique** (`canonical_email`, EF-4.10) ; formulaire `confirm_password` de la reprise d'amorçage (EF-0.3b, l'enrôlement lui-même relevant de T11) ; CA-54. Dépend de T7a, T8.
12. **T9** `mailer.py` (synchrone + file d'envoi, **mode CLI synchrone**, v5-3) + réglages SMTP (EF-4.1). Dépend de T3, T4, T18.
13. **T10** Connexion en deux étapes, `pending.py`, `PUBLIC_ENDPOINTS`, mot de passe oublié. Dépend de T4, T6, T8, T9 ; testable avec des comptes non admins.
14. **T11** TOTP + codes de secours + enrôlement obligatoire et amorçage (`/auth/password/enroll/*`). Dépend de T10 ; débloque la connexion des admins `password`.
15. **T12** WebAuthn (défis serveur, `PUBLIC_URL`). Dépend de T11.
16. **T13** Validation et changement d'adresse : **session complète du compte cible exigée** (R2), mail nommant l'identifiant, index unique sur l'adresse canonique (r7), **canonicalisation des adresses SSO à la connexion et migration des adresses existantes (EF-4.10)** ; CA-55. Dépend de T7b, T9, T10.
17. **T14** API admin (dont fonctions bêta, promotion B5, **conversion SSO en transaction avec collision d'adresse gérée, v5-4, CA-50**) + test d'autorisation paramétré ; **T15** UI admin et profil.
18. **T16** Réglages EF-6.5 vers le registre, un par un, avec leurs bornes ; **fonds de carte validés contre les entrées `tiles=`** (R1, CA-41). Dépend de T18 pour les fonds de carte.
19. **T17** Doc : README, `.env.example` (`AUTH_MODE=password`, `SECRETS_KEY` multi-clés, `PUBLIC_URL`, `WEBAUTHN_RP_ID`, `OUTBOUND_ALLOWED_HOSTS` avec ses préfixes de service et ses ports, `DEM_BACKFILL_INTERVAL`, mention « l'admin prime sur ce fichier »), procédures (rotation et perte de clé, perte de facteur, NTP, mémoire du hachage, surcharges, ajout d'un fournisseur de tuiles, images de branche jamais sur une base persistante).

Prérequis de la spec routage : T0, T3, T4, T5, T6, T14, T18, **T19**.

### Ordre de départ recommandé (les deux specs)

Préalable levé : **ADR-0012 accepté** (2026-11-24). Lancement validé par l'utilisateur : **vague 1 = T0, T6, TR1** (TR0, spike, reste à déclencher côté dev + ops).

| Vague | Tâches (en parallèle dans une vague) | Dépend de | Remarque |
|---|---|---|---|
| 1 | **T0** · **T6** · **TR1** · **TR0** (spike, dev + ops) | — | T6 et TR1 sont des modules purs sans migration ; TR0 est long (import OSM) et ne bloque que TR2, TR3, TR7. |
| 2 | **T2** · **T3** · **T4** · **T7a** · **TR4** | T0 | Chacune apporte sa migration nommée. T7a (module `tokens.py`, sans route) part ici pour être prêt avant T8. |
| 3 | **T8** · **T19** · **T18** · **T5** | T2+T7a ; T2+T3 ; T3+T4 ; T3 | **T8 tôt** : refonte du garde sur `AUTH_MODE` (B1), à fusionner avant tout code du mode `password`. |
| 4 | **T7b** · **T9** · **T16** · (TR2, TR3, TR7 si TR0 conclu) | T7a+T8 ; T18 ; T18 | — |
| 5 | **T10** · **TR5** | T4+T6+T8+T9 ; T3+T5+T6+T18+T19+TR1 | T10 testable avec des comptes non admins. |
| 6 | **T11** · **T13** · **T14** | T10 ; T7b+T9+T10 ; T5+T8 | T11 débloque la connexion des admins `password` ; T14 rend la bêta activable sans SQL. |
| 7 | **T12** · **T15** · **TR6** | T11 ; T14 ; TR5 | — |
| continu | **T17** (doc), **TR8** | — | Mis à jour à chaque tâche qui change l'exploitation. |

Chemin critique côté comptes : T0 → (T2, T7a) → T8 → T7b / (T3, T4 → T18 → T9) → T10 → T11. Côté traces planifiées : TR0 (spike) et T0 → T3 → T19 → TR5 → TR6.
