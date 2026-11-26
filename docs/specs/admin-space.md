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
  - compte qui a obtenu un facteur, a été désactivé ou converti entre l'émission et la consommation ⇒ jeton refusé, **non consommé**. Réponse : formulaire `confirm_password` ⇒ 400 « Vérification impossible : mot de passe ou lien non valable. », la même que pour un mauvais mot de passe, donc rien n'est révélé ; formulaire `set_password` ⇒ réponse neutre de EF-3.6 (404) (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-9 : code accepté tel quel).
- **EF-0.4** Mode `oidc` sans `SECRETS_KEY` ou sans `PUBLIC_URL` : comptes à mot de passe, invitations et saisie de secrets désactivés (routes 404, **toutes les routes d'invitations comprises, côté admin aussi** : `GET /api/admin/invitations`, création, révocation ; formulaire masqué, explication dans l'admin) ; pas de refus de démarrage.
- **EF-0.5 (M5)** Conversion SSO → mot de passe (mode `password` seulement, action admin) : un lien « définir un mot de passe » (jeton `set_password`, 24 h). À sa consommation, dans une transaction : `sub` réécrit en `pwd:<uuid4>` (l'IdP ne peut plus retrouver le compte) ; `auth_method='password'` ; **identifiant choisi** à cette étape, conforme EF-1.2 (le `username` SSO, qui peut être un e-mail, n'est pas repris d'office) ; mot de passe défini ; `session_token` renouvelé ; adresse : l'e-mail présent dans `users.email` (vérifié par l'IdP, `email_verified`) **reste validé** (défaut, §13), **sauf** s'il est déjà l'adresse validée d'un compte `password` (collision tolérée côté SSO, EF-1.2) : il passe alors en `pending_email` (aller-retour EF-4.3b, qui échouera de façon neutre tant que l'adresse reste prise). L'admin le voit dans la liste des comptes (adresse « en attente ») et dans l'audit (`user.convert`, `email_pending`) : la conversion se termine chez l'utilisateur, il n'y a pas de réponse à l'admin à ce moment-là (v5-4 ; décision de l'utilisateur, 2026-11-25, matrice de conformité, E-11 : code accepté tel quel). Toute la conversion se fait dans une transaction `BEGIN IMMEDIATE` : jamais d'erreur d'intégrité en cours de parcours ; **`is_admin` remis à 0** si le compte était admin (invariant B5 : il sera repromu après enrôlement d'un facteur). Action tracée.
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
- **EF-1.8** Réinitialisation par un admin : jeton `reset` **24 h**, envoyé à l'adresse validée et montré à l'admin. Ne retire pas le second facteur. Exigence confirmée (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-1 : le code livré, 1 h et sans envoi, est à mettre en conformité). Précisions :
  - **envoi** par la file d'envoi (EF-4.8) si SMTP est configuré **et** le compte a une adresse validée ; le mail nomme l'identifiant du compte, donne le lien construit sur `PUBLIC_URL` (fragment, EF-S8) et sa durée, et dit « si tu n'as rien demandé, préviens ton administrateur » ;
  - le lien est **toujours montré une fois à l'admin**, avec ou sans SMTP ; sans SMTP ou sans adresse validée, c'est le seul canal (il le transmet lui-même) ;
  - la réponse de l'API indique si un mail a été mis en file (`sent_by_mail: true|false`, nom repris du code en cours de correction), sans détail SMTP ;
  - consommation : nouveau mot de passe (politique EF-1.4), sessions du compte révoquées, aucune session ouverte (EF-1.12) ; usage unique ; émission tracée (audit de création du jeton, sans le jeton) ;
  - **un seul lien de réinitialisation valable à la fois par compte** (décision de coord, 2026-11-25, review du lot 2, E-1-1). Cela vaut pour les jetons `reset`, qu'ils viennent de cette route ou du mot de passe oublié (EF-1.9) :
    - **émission** d'un nouveau lien (admin ou mot de passe oublié) ⇒ les jetons `reset` du compte encore valables sont **révoqués dans la même transaction** que la création du nouveau ; seul le dernier lien émis vaut ;
    - **usage** d'un lien ⇒ tous les autres jetons `reset` du compte encore valables sont révoqués dans la transaction de la consommation (filet si deux émissions se croisent) ;
    - révocations tracées comme les autres (audit de révocation, sans le jeton) ; les autres types de jetons (`invite`, `bootstrap`, `verify_email`, `set_password`) ne sont pas touchés ;
    - effet assumé : un mot de passe oublié demandé pour un compte éligible invalide le lien envoyé plus tôt par un admin. Le limiteur du mot de passe oublié (EF-1.9, EF-6.5) borne cet effet. Un compte non éligible ne reçoit aucun jeton et ne perd aucun lien, avec une réponse identique (aucun oracle) (CA-56b).
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
  - passage de `auth.mfa_policy` à `all` **refusé** (409, nombre de comptes concernés) tant qu'un compte `password` actif n'a pas de facteur (lecture de l'arbitrage « idem », §13 n° 1, **confirmée par l'utilisateur** ; rappel Doc-12, 2026-11-24). Précisions :
    - le contrôle est fait **à l'écriture** de la valeur `all` (`PATCH /api/admin/settings`, donc l'UI), dans la **même transaction** que l'écriture ; refus ⇒ **tout le lot** est rejeté (EF-6.3), la version des réglages n'est pas incrémentée, ligne d'audit du refus ;
    - « compte concerné » = compte `auth_method = 'password'`, `is_active = 1`, sans facteur actif (EF-2.2 : TOTP activé ou au moins une clé WebAuthn ; les codes de secours ne comptent pas) ; les comptes désactivés et SSO ne comptent pas ;
    - le réglage n'a pas de variable d'env et `settings-reset` ne peut que revenir à `admins` : l'écriture par l'API est le seul chemin à contrôler ;
    - une fois `all` en place, un compte **créé ensuite** (invitation) ou un compte sans facteur réactivé passe par l'enrôlement obligatoire à sa connexion (EF-1.6 §4, CA-45) : c'est le filet de sécurité, il ne remplace pas le refus ;
  - défense en profondeur : `admin_required` vérifie en plus, pour un compte `password`, la présence d'un facteur actif.
  Réglage `auth.mfa_policy` : `admins` (défaut) | `all`.
- **EF-2.3 (m2)** TOTP : secret 160 bits, QR code SVG généré serveur ; activation après un code valide ; secret chiffré. Vérification ±1 pas ; acceptation **atomique et anti-rejeu** : `UPDATE users SET totp_last_step = :pas WHERE id = :id AND (totp_last_step IS NULL OR totp_last_step < :pas)`, 0 ligne ⇒ refus (refuse tout pas ≤ dernier accepté, y compris deux soumissions concurrentes du même code).
- **EF-2.4 (M3, M12)** WebAuthn : RP ID = hôte de `PUBLIC_URL` (surcharge `WEBAUTHN_RP_ID`, qui doit être un suffixe de domaine enregistrable de cet hôte, vérifié contre la **Public Suffix List complète embarquée**, voir EF-2.4b) ; origine attendue = origine de `PUBLIC_URL` ; `http://localhost` accepté **seulement** si `TESTING` ou mode debug explicite. Attestation `none`, vérification utilisateur `preferred`. Défi ≥ 128 bits stocké **côté serveur** (`pending.py`), lié à l'état ou à la session, valable 2 min, **consommé atomiquement**. Régression d'un compteur de signature non nul ⇒ refus + audit. Clés nommées, supprimables (sauf EF-2.2).
- **EF-2.4b Public Suffix List embarquée** (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-13 : remplace la liste courte de 18 suffixes du code livré ; aucune dépendance réseau) :
  - fichier `public_suffix_list.dat` du projet publicsuffix (sections ICANN **et** PRIVATE), **figé** dans le dépôt à un commit précis, avec un fichier `SOURCE` (URL figée, commit, SHA-256, date, avis de licence complet), sur le modèle de EF-1.4b ; mise à jour sur décision, par un script qui vérifie le SHA-256, jamais à l'exécution ;
  - algorithme standard de la liste : règles normales, jokers (`*.ck`) et exceptions (`!www.ck`), règle implicite `*` ; comparaison sur la forme ASCII (punycode, minuscules) ;
  - `WEBAUTHN_RP_ID` (ou l'hôte de `PUBLIC_URL`) est accepté si et seulement s'il est égal à l'hôte de `PUBLIC_URL` ou en est un suffixe par libellés entiers, **et** s'il n'est pas lui-même un suffixe public (il contient au moins le domaine enregistrable) ; une adresse IP n'est jamais un RP ID ;
  - chargement au démarrage quand les comptes à mot de passe sont disponibles ; fichier absent, illisible ou vide ⇒ **refus de démarrer** (même règle que EF-1.4b, fermé par défaut) ; `WEBAUTHN_RP_ID` (ou hôte de `PUBLIC_URL`) refusé ⇒ **refus de démarrer**, message qui nomme la variable (le code livré déclarait seulement WebAuthn indisponible) (décision de coord, 2026-11-25, à relire par l'utilisateur) ;
  - **licence MPL-2.0 acceptée** (décision de coord, 2026-11-25, à relire par l'utilisateur) : instantané épinglé, fichier `SOURCE` avec le texte de la licence et la mention de provenance.
- **EF-2.5 (m3)** Codes de secours : 10 codes de **16 caractères base32** (80 bits), générés à l'activation du premier facteur, montrés une fois, stockés en SHA-256, comparés en temps constant, usage unique (marquage atomique), régénérables (anciens invalidés). Usage ⇒ notification (`recovery_used`), **quel que soit le chemin** : connexion, et aussi profil (changement de mot de passe EF-1.7, changement d'adresse EF-4.6, retrait de TOTP ou de clé, P-T11-7) (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-3 a : à corriger, le code ne notifie qu'à la connexion).
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
- **EF-3.12 Section « Invitations » du profil** (défaut trouvé par l'utilisateur, 2026-11-25 : la section était réservée aux comptes `password` dans le gabarit, ce qui contredit EF-3.2 et §9) :
  - **visible pour tout compte, SSO ou `password`**, dès que les invitations sont disponibles. Elles le sont en mode `password`, et en mode `oidc` avec `SECRETS_KEY` et `PUBLIC_URL` (EF-0.4) ;
  - elle montre toujours :
    - les crédits restants (« illimité » pour un admin) ;
    - la liste des invitations émises par le compte, avec leur statut (mêmes libellés que l'administration, EF-3.13) et la révocation de celles en attente (EF-3.7) ;
  - le **formulaire de création** n'apparaît que si `can_invite` est vrai (EF-3.2 : admin, ou compte actif à adresse validée avec au moins un crédit). L'adresse d'un compte SSO, fournie par l'IdP (`email`), compte comme validée ;
  - si `can_invite` est faux, une explication remplace le formulaire, et elle en donne le motif :
    - « aucun crédit d'invitation : demande-en à un administrateur » ;
    - ou « valide d'abord ton adresse e-mail » ;
  - la section est **absente** quand les invitations ne sont pas disponibles : mode `local`, ou mode `oidc` sans `SECRETS_KEY` ou sans `PUBLIC_URL`. `/api/invitations` répond alors 404 (EF-0.4, CA-61) ;
  - le serveur reste l'autorité : `POST /api/invitations` applique EF-3.2 et EF-3.5 quel que soit l'affichage (CA-81).
- **EF-3.13 Onglet « Invitations » de l'administration** (demande de coord, 2026-11-25) : la liste de **toutes** les invitations (`GET /api/admin/invitations`) dit qui a émis chaque invitation, dans quel état elle est, et quel compte elle a créé.
  - **Colonne « Émise par »** : le compte émetteur (`created_by`), désigné par son identifiant, à défaut son nom affiché, à défaut son adresse, à défaut `#<id>`. Émetteur supprimé (`created_by` à `NULL` : `ON DELETE SET NULL`) ⇒ « compte supprimé ». La CLI n'émet que des jetons `bootstrap`, absents de cette liste : un `created_by` vide y signifie donc toujours un compte supprimé ;
  - **statut en français** : `pending` ⇒ « En attente », `used` ⇒ « Acceptée », `expired` ⇒ « Expirée », `revoked` ⇒ « Révoquée ». Le statut est calculé par le serveur, dans cet ordre de priorité : acceptée, puis révoquée, puis expirée (date d'expiration passée), sinon en attente ;
  - **même vocabulaire partout** (décision de coord, 2026-11-25) : la liste du profil (EF-3.12) emploie les mêmes quatre mots. « Acceptée » remplace « Utilisée » dans l'administration ; le mot « utilisée » n'apparaît comme statut ni dans le profil ni dans l'administration. Seule la majuscule initiale peut suivre la position du mot (début de cellule ou milieu de ligne) ;
  - **invitation acceptée** : la ligne montre le compte créé (`used_by`), désigné comme l'émetteur (« compte créé : `alice` »). Compte créé puis supprimé ⇒ « compte créé : compte supprimé » ;
  - **API** : chaque invitation de `GET /api/admin/invitations` porte en plus `created_by_name` et `used_by_name` (`null` si le compte n'existe plus). Jamais le jeton ni son empreinte. La route reste réservée aux admins, et à 404 quand EF-0.4 s'applique (CA-61) ;
  - noms et adresses rendus en texte (`textContent`), jamais en HTML ; la révocation ne change pas (bouton seulement sur une invitation en attente, crédit non rendu, EF-3.7).

### 5.4 E-mail et validation d'adresse (ADR-0005)

- **EF-4.1 (B3)** Réglages SMTP : hôte, port (587), sécurité `starttls` | `tls` | `none`, identifiant, mot de passe (secret), expéditeur, délai (10 s). Certificat toujours vérifié. `starttls` : échec de la négociation ⇒ échec de l'envoi, **jamais de repli en clair**. `none` : **aucune authentification envoyée** ; un identifiant ou un mot de passe renseigné avec `none` ⇒ 400 à l'écriture, et refus d'envoi si l'état en base est incohérent. Hôte soumis à la liste blanche (EF-6.8) ; mot de passe lié à `smtp.host:port` (EF-5.5).
- **EF-4.2 (B3, r4)** « Envoyer un e-mail de test » à l'adresse de l'admin connecté : réponse **ok / ko seulement**, sans code ni message SMTP. **Durée = `smtp.timeout_s`** : la réponse est rendue à cette échéance quel que soit le résultat (succès anticipé compris). La tentative est **abandonnée** à l'échéance, pas interrompue : un envoi lent peut encore aboutir après une réponse « ko », sans effet sur la réponse ni sur sa durée (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-14 : code accepté tel quel).
- **EF-4.3 (B2)** **Toute adresse d'un compte `password` est validée par aller-retour** : à la création du compte, l'adresse de l'invitation va dans `pending_email` et un mail de validation part (lien `verify_email`, 48 h, renvoi limité à 3 / h par compte). **Aucune validation d'office**, même si le lien d'invitation a été reçu par mail. Sans SMTP, l'adresse reste en attente. Seule exception : conversion SSO (EF-0.5).
- **EF-4.3b (R2)** **La consommation d'un jeton `verify_email` exige une session complète du compte cible** (`target_user_id == g.user.id`) :
  - sans session, ou avec la session d'un autre compte ⇒ le jeton n'est **pas** consommé, l'adresse reste en attente, page neutre « connecte-toi au compte concerné, puis rouvre ce lien » (aucun identifiant affiché) ;
  - la protection anti-login-CSRF (EF-S2) empêche un tiers de connecter la victime à son propre compte pour lui faire valider l'adresse ;
  - défense en profondeur : le mail de validation **nomme l'identifiant du compte** et dit « si tu n'as pas créé ce compte, ignore ce message » ;
  - même règle pour la validation d'un changement d'adresse (EF-4.6).
- **EF-4.4** Invariant : `users.email` ne contient qu'une adresse validée ; `pending_email` n'est jamais utilisée par l'inbox, `match_login` ni le mot de passe oublié.
- **EF-4.5** Adresse non validée : compte utilisable ; pas d'invitation ; pas de mot de passe oublié ; bandeau avec renvoi. Précisions (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-15 : bandeau et renvoi par l'utilisateur absents du code livré, à corriger) :
  - **bandeau** sur toutes les pages, pour un compte `password` dont `pending_email` est renseignée : « Adresse à valider : <adresse> », bouton « Renvoyer le mail de validation » ; sans SMTP, pas de bouton, texte « la validation n'est pas disponible, contacte un administrateur » ;
  - **renvoi par l'utilisateur** : `POST /api/me/email/resend-verification` (session complète, CSRF) ; nouveau jeton `verify_email` (48 h), **les jetons `verify_email` précédents non utilisés du compte sont révoqués** *(proposé par spec)* ; mail par la file d'envoi ;
  - **limite de 3 renvois par heure et par compte** (EF-4.3), **partagée** entre le renvoi de l'utilisateur et celui de l'admin (EF-8.3) ; au-delà ⇒ 429 générique avec `Retry-After` ; la création du compte (premier mail) ne compte pas ;
  - réponse sans détail SMTP ; aucune adresse dans les journaux applicatifs (EF-S5) ;
  - compte SSO ⇒ **404** (la route n'existe pas pour lui, comme `/api/me/mfa/*`) ; compte `password` sans adresse en attente, ou SMTP non configuré ⇒ **409** (décision de coord, 2026-11-25, test 3a).
- **EF-4.6** Changement d'adresse : mot de passe + second facteur ; nouvelle adresse en attente ; l'ancienne reste active et reçoit une notification. À la validation (session du titulaire exigée, EF-4.3b), adresse déjà validée sur un autre compte ⇒ refus avec message neutre (« impossible de valider cette adresse »), `pending_email` effacée, audit ; contrôle et écriture dans une même transaction (EF-1.2, r7).
  **Limite des changements d'adresse** (décision de coord, 2026-11-25, review du lot 2, E-15-1) :
  - `POST /api/me/email` : **3 changements par heure et par compte**, et **10 par heure et par IP** *(valeur par IP proposée par spec)*. Fenêtres glissantes approchées (EF-S3). Valeurs **fixes en code**, hors registre, comme le budget de renvoi (EF-4.5) ;
  - **ce qui compte** : un changement **accepté** (adresse mise en attente). Le compteur est consulté et l'unité **réservée avant tout hachage** (r9) ; la réservation est **rendue** si la requête est ensuite refusée (mot de passe ou second facteur faux, adresse invalide, adresse identique ⇒ 409). Ces refus relèvent des limiteurs du mot de passe et du second facteur, qui existent déjà. Même mécanique que la réservation du limiteur du second facteur (MFA-1) : deux requêtes concurrentes ne dépassent jamais le seuil ;
  - au-delà ⇒ **429** générique avec `Retry-After`. Rien n'est changé : ni mail de validation, ni notification à l'ancienne adresse, ni audit `user.email_change_requested`. Une ligne d'audit ou de journal du refus, sans adresse, est permise ;
  - **deux budgets distincts** : le **premier mail de validation** d'un changement compte dans le budget des **changements**, jamais dans celui des **renvois** (EF-4.5 : 3 par heure, partagé entre l'utilisateur et l'admin) ; les renvois qui suivent comptent dans le budget des renvois. Un compte reçoit donc au plus 6 mails de validation par heure, et son ancienne adresse au plus 3 notifications de changement ;
  - un nouveau changement révoque les jetons `verify_email` précédents du compte : seul le dernier lien vaut (règle du renvoi, EF-4.5) (CA-60b).
- **EF-4.7 (M12)** Mails en texte brut, français ; **tous les liens sont construits sur `PUBLIC_URL`**. Envoyés : invitation, validation, réinitialisation, notifications de sécurité (mot de passe changé, facteur ajouté/retiré/réinitialisé, code de secours utilisé, adresse changée).
- **EF-4.8 (M2)** Deux modes d'envoi :
  - **synchrone** (résultat utile à l'appelant) : invitation (« envoi échoué, transmets le lien ci-dessous »), e-mail de test ;
  - **file d'envoi** en mémoire, traitée par un thread dédié, pour tout le reste (mot de passe oublié, validation, notifications) : la requête ne dépend jamais de l'envoi. File bornée (1 000 messages ; pleine ⇒ message abandonné et journalisé) ; perte au redémarrage acceptée et journalisée : à l'arrêt du processus, **une ligne `WARNING`** donne le nombre de messages non envoyés et leur type (jamais l'adresse ni le lien) (à corriger, décision de coord, 2026-11-25, à relire par l'utilisateur, E-16, CA-66). Cette ligne part d'un **crochet d'arrêt enregistré par le processus** (`atexit`, ou crochet de sortie du worker gunicorn) : un arrêt normal du conteneur (`SIGTERM` reçu par gunicorn, `docker compose stop`, `up -d --force-recreate`) l'écrit ; le crochet n'attend pas le thread d'envoi bloqué. Un `SIGKILL` ne l'écrit pas (accepté) (CA-66b).
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
  - **secrets en base seulement en v1** : aucun secret n'est lu dans l'environnement (aucune variable prévue) ; la règle « un secret d'env n'est envoyé qu'à la destination d'env » est donc sans objet (décision de coord, 2026-11-25, à relire par l'utilisateur, E-17 : spec alignée sur le code ; ADR-0008 amendé).
- **EF-5.6 (B3, r4)** « Tester » par service : réponse **ok / ko seulement** ; aucun code HTTP, message ou délai distinctif ; uniquement vers une destination autorisée (EF-6.8). Services testables en v1 : **moteur** et **SMTP** ; pas de bouton « Tester » pour le DEM (décision de coord, 2026-11-25, à relire par l'utilisateur, E-18 : accepté en v1). **Durée = délai du service** (`routing.timeout_s` pour le moteur, `smtp.timeout_s` pour SMTP) : la réponse est rendue à cette échéance, quel que soit le résultat ; la sonde est abandonnée à l'échéance, pas interrompue (même règle que EF-4.2, E-14).
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

### 5.6b Page Réglages de l'administration (refonte, 2026-11-25)

Origine : en recette, l'utilisateur juge l'onglet « Réglages » « trop fouillis » et demande des onglets (relayé par coord le 2026-11-25). Cette section **remplace le formulaire générique** de P-T15-5. Les règles de §5.5, §5.6 et §6 ne changent pas : **le serveur reste l'autorité** ; les contrôles de la page servent au confort et ne remplacent jamais ceux du serveur.

- **EF-6.9 Sous-onglets par section du registre** :
  - un sous-onglet par **section** du registre (`Setting.section`), dans l'ordre déclaré par le registre. Au registre du 2026-11-26 (51 réglages, dont `routing.zone_margin_km` ajouté par le lot Z2, EF-6.22) : **Sécurité** (11) · **E-mail** (6) · **Carte** (5) · **Traces planifiées** (17) · **Altitude** (7) · **Dossier surveillé** (3) · **Stockage** (1) · **Général** (1). Les noms cités par coord correspondent ainsi : « Comptes » ⇒ Sécurité ; « Routage » ⇒ Traces planifiées ; « Messagerie » ⇒ E-mail ; « Tâches/DEM » ⇒ Altitude et Dossier surveillé ;
  - la liste ordonnée des sections est **déclarée dans le registre** (tuple `SECTIONS`) ; un réglage dont la section n'y figure pas ⇒ registre refusé à la construction ; une section sans réglage visible n'a pas d'onglet ;
  - chaque onglet porte **au plus une pastille de nombre** : celle des réglages **modifiés** (m), seulement quand m > 0 ; **pas de total** des réglages de l'onglet (retour de l'utilisateur, 2026-11-25, EF-6.18). Un réglage est **modifié** quand son origine est `base` ou `env`, pas `défaut`. La pastille montre le nombre seul (« 3 ») et porte un nom accessible complet (« 3 réglages modifiés »). S'y ajoutent une **marque compacte « non enregistré »** (un point, nom accessible « saisies non enregistrées ») quand l'onglet porte des saisies non enregistrées, et une pastille **d'alerte** (nom accessible « alerte ») quand un de ses réglages a une variable d'env invalide (`env_invalid`) ou une destination hors liste blanche (CA-64) ;
  - le sous-onglet est repéré dans l'ancre de l'URL : `#reglages/<section>`, la section étant écrite en minuscules, sans accents, avec des tirets (`#reglages/traces-planifiees`) ; une ancre inconnue ouvre le premier onglet ; un rechargement rouvre le même onglet ;
  - accessibilité : rôles `tablist` / `tab` / `tabpanel` ; on passe d'un onglet à l'autre avec les flèches du clavier.
- **EF-6.10 Recherche et filtre** :
  - champ **« Rechercher un réglage »** : il filtre sur le libellé, l'aide, la clé (`smtp.host`) et le nom de la variable d'env (`DEM_URL`) ; la recherche porte sur une sous-chaîne, **sans tenir compte de la casse ni des accents** (`delai` trouve « Délai ») ;
  - la recherche porte sur **tous les onglets** : pendant une recherche, la pastille de nombre de chaque onglet montre les **réglages trouvés** « k » (nom accessible « k réglages trouvés ») à la place des modifiés ; un onglet sans résultat est grisé mais reste cliquable et affiche « Aucun réglage ne correspond » ; l'onglet ouvert ne change jamais tout seul. Quand l'onglet ouvert n'a aucun résultat, il donne des liens vers les onglets qui en ont ;
  - interrupteur **« Afficher seulement les valeurs modifiées »** : il ne garde que les réglages d'origine `base` ou `env` ; il se combine avec la recherche (les deux conditions s'appliquent) ;
  - un réglage masqué par la recherche ou le filtre **garde sa saisie** non enregistrée ; l'enregistrement de l'onglet l'inclut, et la confirmation l'affiche (EF-6.13) ;
  - la recherche et l'interrupteur ne sont écrits ni dans l'URL ni dans le stockage du navigateur.
- **EF-6.11 Ligne de réglage compacte** :
  - **une ligne par réglage** : libellé ; éditeur qui montre la **valeur effective** ; pastille d'origine **« Environnement »** ou **« Base »**, **aucune** pour l'origine défaut (décision de coord, 2026-11-25 : la pastille « Défaut » disparaît, EF-6.18) ; bouton de rétablissement quand il s'applique. Le nom de la variable n'est pas répété dans la pastille ;
  - **aide** groupée avec le libellé (EF-6.18), sur une ligne : unité et bornes (« 1 à 60 s », « 254 caractères au plus », « 10 fonds au plus ») et, s'il y en a une, « variable `VAR` », seul endroit où la variable est nommée ;
  - **unité** déclarée dans le registre (attribut `unit` : `s`, `Mo`, `km`, `m`, `%`, `h`, `jours`, `/ h`…) et retirée des libellés (« Délai d'un appel au moteur » et `s`, au lieu de « … (s) ») ; tout réglage `int` ou `float` déclare `unit`, la chaîne vide étant permise quand il n'y en a pas (décision de coord, 2026-11-25 ; les tests qui comparent des libellés suivent) ;
  - **description** (`help`) repliée par défaut derrière un bouton « ? » (élément `<details>`) ;
  - base qui **surcharge** une variable d'env : pastille « Base » et, dans l'aide, « variable `VAR`, remplacée par la valeur en base » (le marqueur du polissage E-21 devient exigé) ; variable d'env **invalide** : alerte « `VAR` invalide : ignorée », avec le nom de la variable seulement, jamais sa valeur (EF-6.3) ;
  - **Rétablir** : bouton présent seulement quand l'origine est `base`. Son libellé dépend de la valeur qui s'appliquera après la suppression de la ligne : « **Rétablir la valeur d'environnement** » si la variable du réglage est définie et valide, sinon « **Rétablir la valeur par défaut** ». Le bouton annonce cette valeur (« → 15 s ») ;
    - effet : `DELETE /api/admin/settings/<clé>` (EF-6.3b, audit `setting.reset`), après une **confirmation dans la page** ; l'action est **immédiate**, elle ne passe pas par l'enregistrement de l'onglet ;
    - si le rétablissement change une destination qui porte un secret configuré (EF-5.5 : `store.reset` efface alors ce secret), la confirmation le dit (même texte que CA-65) ;
    - la saisie non enregistrée de ce réglage est abandonnée ; celles des autres réglages sont conservées ;
  - **API** : `GET /api/admin/settings` donne en plus :
    - `sections`, la liste ordonnée des sections ;
    - pour chaque réglage : `unit`, `fallback` (la valeur effective sans la ligne en base), `fallback_source` (`env` ou `défaut`), `env_invalid`, `env_overridden` (déjà présent) et `restart_pending` (EF-6.15) ;
    - pour `map.basemaps` : `allowlist`, la liste des hôtes `tiles=` effectifs ;
    - pour un réglage `secret`, `value` et `fallback` valent `null`.
- **EF-6.12 Éditeurs structurés** :
  - `bool` : interrupteur. `enum` : liste déroulante. `int` et `float` : champ numérique avec `min`, `max` et `step` (1 pour `int`). `str`, `email` et `url` : champ texte avec `maxlength`. `timezone` : champ texte, sans changement ;
  - **`basemaps`** : une **liste ordonnée de cartes**, une par fond. Champs de chaque carte :
    - nom (40 caractères au plus) ;
    - URL (500 caractères au plus), avec le rappel « `https://…/{z}/{x}/{y}…` ; `{s}` = sous-domaine, `{r}` = suffixe rétine facultatif » ;
    - sous-domaines (1 à 4 lettres minuscules, `abc` par défaut) ;
    - attribution (200 caractères au plus, texte brut : `< > & " '` refusés) ;
    - zoom maximal (1 à 22).
  - Boutons de la liste des fonds :
    - **Ajouter**, désactivé à 10 fonds ;
    - **Supprimer**, avec une confirmation dans la page ;
    - **Monter** et **Descendre** : l'ordre est celui que voient les utilisateurs. Ces boutons sont utilisables au clavier ; le glisser-déposer, s'il existe, n'est jamais le seul moyen.
  - **Contrôle d'hôte** à la saisie, fait par la page, sans appel réseau :
    - la page calcule les hôtes effectifs de l'URL, en remplaçant `{s}` par chacun des sous-domaines ;
    - elle les compare à l'`allowlist` de `map.basemaps` ;
    - hôte absent ⇒ message sous l'URL : « L'hôte `b.tiles.exemple.fr` n'est pas autorisé : ajoute-le à `OUTBOUND_ALLOWED_HOSTS` (`tiles=`), puis redémarre l'application. » (la liste vient de l'env et de la CSP, calculées au démarrage, EF-6.8) ;
    - URL qui n'est pas en `https://`, ou sans `{z}`, `{x}` ou `{y}` ⇒ message sous l'URL ;
    - le serveur garde son contrôle (400, EF-6.5).
  - **`map.default_basemap`** devient une liste déroulante des **noms des fonds**, saisies non enregistrées comprises. Si le fond par défaut est supprimé ou renommé dans la saisie ⇒ erreur sous « Fond par défaut » (« choisis un autre fond par défaut ») avant l'enregistrement. Le serveur garde le bandeau d'usage existant (`default_basemap_alert`) ;
  - ~~**`rectangle`**~~ : **retiré par le lot Z1** (2026-11-26) avec son seul réglage, `routing.coverage_bbox`, et son éditeur sur la carte (EF-6.19). Le type disparaît du registre : aucun réglage ne l'utilise plus ;
  - **`polygon`** (EF-6.20, EF-6.21) : zone éditée sur une **carte d'aperçu** Leaflet, avec la liste des sommets. Fond = le fond par défaut s'il est autorisé, sinon le polygone seul, sans tuiles ; aucune requête hors tuiles des hôtes autorisés. Emprise affichée « ≈ … km × … km » avec **le même calcul que le serveur** (largeur le long du parallèle le plus proche de l'équateur, hauteur le long d'un méridien, haversine ; zone par défaut : « ≈ 371 km × 302 km ») ;
  - **`list`** : une **pastille** par élément, avec une croix pour le retirer ; un champ et « Ajouter » (ou la touche Entrée) pour en ajouter ; doublon ou élément trop long ⇒ erreur sous le champ ; « Ajouter » désactivé à `max_items`. **Aucun réglage `list` au registre v1** : cet éditeur se teste avec un registre de test ;
  - **volet « Avancé »**, replié, pour `basemaps` et `polygon` seulement :
    - il montre le JSON brut de la valeur **en cours de saisie** ;
    - JSON modifié puis « Appliquer » ⇒ l'éditeur structuré est mis à jour ;
    - JSON invalide ou de mauvaise forme ⇒ erreur sous le volet, éditeur structuré inchangé ;
    - aucun autre type n'a de JSON brut ;
  - le test « aucun type inéditable » (`CONTROL_FOR_TYPE`) est conservé, mais il change : chaque type du registre a un éditeur, et aucun ne passe par une zone JSON hors du volet « Avancé ».
- **EF-6.13 Enregistrer par onglet** :
  - **un bouton « Enregistrer » par onglet**. Il n'est actif que si l'onglet porte au moins un changement, c'est-à-dire une valeur saisie différente de la valeur chargée après normalisation (`15` = `15.0`). « Annuler les modifications » remet les valeurs chargées de l'onglet ;
  - un clic ouvre un **panneau de confirmation dans la page**, qui liste chaque changement :
    - libellé, **avant** (valeur effective et origine) et **après** ;
    - pour les fonds de carte, un résumé par fond : ajouté, supprimé, déplacé ou modifié (avec les champs en cause) ;
    - pour une destination avec un secret lié configuré : « le secret … sera effacé » (CA-65, dans le même panneau) ;
    - pour un réglage `requires_restart` : « pris en compte au prochain redémarrage ».
  - Le panneau propose **Confirmer** et **Annuler** ; Annuler ⇒ aucune requête ;
  - avant d'ouvrir le panneau, la page fait ses propres contrôles (bornes, polygone, hôtes, fond par défaut). Une erreur s'affiche sous son champ et bloque l'ouverture du panneau ;
  - Confirmer ⇒ **un seul** `PATCH /api/admin/settings`, qui porte **les seuls changements de l'onglet** et la `version` chargée ; le lot est tout ou rien (EF-6.3) ;
  - **succès** ⇒ l'onglet est rechargé depuis le serveur, les compteurs sont mis à jour et un message s'affiche (« n réglages enregistrés ») ; **les saisies des autres onglets sont conservées** ;
  - **refus 400** ⇒ rien n'est écrit. L'erreur s'affiche **sous le champ en cause** (pour `basemaps`, sur la carte du fond en cause) ; le champ reçoit `aria-invalid` et le focus ; toutes les saisies de l'onglet restent. Pour cela, l'API renvoie en plus de `error` :
    - `field`, la clé du réglage ;
    - `index`, le rang du fond en cause (à partir de 0) pour `basemaps`.
  - **409** de `auth.mfa_policy` (CA-13b) ⇒ message sous le champ ;
  - **contrôle de version** (décision de coord, 2026-11-25) : le « avant » de la confirmation doit être vrai au moment de l'écriture.
    - La page envoie **toujours** la `version` chargée avec son `PATCH`.
    - Le serveur la compare à `settings_meta.version` **dans la transaction d'écriture**. Si la version a changé depuis le chargement (autre admin, CLI, rétablissement, autre onglet du navigateur) ⇒ **409**, `code: "settings_version_conflict"`, `version` = la version courante, et le message « Un autre admin a modifié ces réglages : recharge la page avant d'enregistrer. » ; **rien n'est écrit, aucun audit `setting.update`**.
    - Le champ `code` distingue ce 409 de celui de `auth.mfa_policy`, qui porte `field`.
    - Dans la page, ce message s'affiche dans le panneau de confirmation avec un bouton **« Recharger »**. Ce bouton recharge les valeurs du serveur sans perdre les saisies ; un nouvel « Enregistrer » montre alors les nouveaux « avant ». Pas de nouvel essai automatique.
    - `PATCH` sans `version` ⇒ accepté sans contrôle, pour les autres clients de l'API ; la page n'en envoie jamais. La CLI écrit par le store et n'est pas concernée.
    - `PATCH` et `DELETE` renvoient la nouvelle `version`, que la page garde ; l'onglet qui vient d'enregistrer ne se met donc pas lui-même en conflit.
- **EF-6.14 Rien ne se perd** :
  - les saisies non enregistrées sont gardées en mémoire, onglet par onglet, quand on change de sous-onglet, quand on change d'onglet d'administration (Utilisateurs, Journal…) et quand on applique la recherche ou le filtre ;
  - quitter ou recharger la page avec des saisies non enregistrées déclenche l'avertissement natif du navigateur (`beforeunload`), seul dialogue natif permis ici ; les confirmations restent dans la page (P-T15-3) ;
  - aucune valeur de réglage n'est écrite dans l'URL, `localStorage` ou `sessionStorage` : une valeur peut être un hôte interne.
- **EF-6.15 Réglages `requires_restart`** :
  - pastille « Redémarrage nécessaire » sur la ligne ;
  - l'API expose `restart_pending` : la valeur effective diffère de celle lue au démarrage du processus. Tant que `restart_pending` est vrai, l'onglet affiche le bandeau « Valeur enregistrée, prise en compte au prochain redémarrage » ;
  - **aucun réglage v1 n'est `requires_restart`** (EF-6.4) : ce comportement se teste avec un registre de test.
- **EF-6.16 Sécurité inchangée** :
  - les secrets ne sont **jamais affichés**. Un réglage `secret` n'a pas d'éditeur sur cette page ; les secrets restent dans l'onglet « Clés API et e-mail », en saisie seule (EF-5.3) ; `value` et `fallback` d'un réglage `secret` valent `null` ;
  - bornes et validation du serveur **inchangées** ; audit **inchangé** : une ligne `setting.update` par clé modifiée, une ligne `setting.reset` par rétablissement, effacement d'un secret lié tracé (CA-34) ;
  - rendu par le DOM et `textContent` ; jamais d'`innerHTML` avec une donnée (le nom et l'attribution d'un fond sont du texte) ; aucun script ni style en ligne (CSP) ;
  - `admin_required`, CSRF et `no-store` inchangés ; l'`allowlist` des tuiles n'est exposée qu'à l'API d'administration ;
  - l'aperçu de la zone ne charge que des tuiles d'hôtes déjà autorisés : la CSP ne change pas.
- **Cas limites** :
  - un onglet dont tous les réglages sont masqués par le filtre peut encore être enregistré, et la confirmation montre ses changements ;
  - deux onglets modifiés, un seul enregistré ⇒ l'autre garde ses saisies et sa pastille ;
  - un « Rétablir » dans un onglet qui porte d'autres saisies ⇒ ces saisies sont conservées et la `version` est mise à jour ;
  - un fond de carte en double (nom identique, sans tenir compte de la casse) ⇒ erreur sous le nom, avant l'envoi ;
  - un admin rétrogradé pendant la saisie ⇒ `PATCH` en 403 ; le message est affiché et les saisies sont gardées jusqu'au départ de la page.
- **EF-6.17 Banc de test navigateur** (décision de coord, 2026-11-25) :
  - **local et facultatif, hors CI** : jamais lancé par le job pytest de la CI. Le dossier `web-app/tests_browser/` est hors de `testpaths` (`pytest.ini` : `tests`), et ses dépendances ne vont pas dans `requirements-dev.txt` ;
  - **image Docker épinglée** : `mcr.microsoft.com/playwright/python`, avec **version et digest** (`…:vX.Y.Z-<distribution>@sha256:…`). Ce sont dev ou ops qui choisissent une version publiée depuis au moins deux semaines, jamais `latest`. Le paquet Python `playwright` éventuellement installé a **la même version** que l'image, épinglée avec empreinte (`--require-hashes`) dans `web-app/tests_browser/requirements.txt` ;
  - **une commande documentée**, une seule, dans `web-app/tests_browser/README.md`. Elle lance le conteneur sur le code monté, démarre l'application de test **dans le conteneur** et joue les tests. L'application de test utilise :
    - une base et un `DATA_DIR` temporaires ;
    - une configuration de test : session d'un admin ouverte par une fixture, SMTP factice ;
    - `OUTBOUND_ALLOWED_HOSTS` de test ;
  - **aucune sortie réseau** pendant les tests : toute requête du navigateur vers un autre hôte que l'application de test est **interceptée** (`page.route`), **enregistrée** puis bloquée. Les tuiles des hôtes autorisés reçoivent une image locale de 1 pixel. La liste des requêtes interceptées sert de preuve (CA-76) ;
  - **qui le lance** : `test` le joue avant chaque lot qui touche au front (`static/`, `templates/`) et en note le résultat dans son compte rendu ; la recette manuelle reste en complément ;
  - **périmètre initial** : CA-72 à CA-78, puis CA-82 et CA-86 (CA-76 et CA-83 supprimés par le lot Z1). D'autres critères du front pourront s'y ajouter : RB-2.x, P-TR6-* et la déconnexion, aujourd'hui vérifiés par recherche de chaînes.
- **EF-6.18 Lisibilité** (retour de l'utilisateur en recette, 2026-11-25 : « je ne trouve toujours pas ça super lisible ») : présentation seulement. **Aucune fonction, donnée ni route ne change** ; les textes de compteurs et de pastilles de EF-6.9 à EF-6.11 ont été mis à jour en conséquence. Mesures faites par le banc (EF-6.17) à deux tailles de fenêtre, **bureau 1280 × 800** et **téléphone 390 × 844**, en thème clair et en thème sombre.
  - **Onglets sur une ligne** :
    - à 1280 px, les 8 onglets tiennent sur **une seule ligne** ; aucune pastille de total (EF-6.9) ;
    - plus étroit, la barre d'onglets **ne passe jamais sur deux lignes** : elle défile horizontalement dans sa propre zone, sans défilement horizontal de la page, et l'onglet ouvert est amené dans la partie visible (au chargement, au clic, aux flèches du clavier).
  - **Libellé, pastille et aide groupés** : chaque ligne a un **en-tête** et un **éditeur**.
    - En-tête : le libellé ; à sa suite sur la même ligne, le bouton « ? » de la description puis la pastille d'origine quand elle existe ; en dessous, l'aide (unité, bornes, variable) ; en dessous, la description quand elle est dépliée.
    - Éditeur : le champ et son unité ; en dessous, le bouton « Rétablir … » et les erreurs du champ.
    - À partir de 992 px de large : **deux colonnes**, alignées en haut ; l'en-tête occupe 35 à 45 % de la largeur de la ligne ; au moins 24 px entre les deux colonnes. Les éditeurs `basemaps`, `rectangle` et `polygon` (carte comprise) prennent toute la largeur, sous l'en-tête.
    - Sous 992 px : une colonne, l'en-tête au-dessus de l'éditeur.
  - **Largeur maximale des éditeurs**, unité comprise, à 1280 px :
    - `int` et `float` : 12 rem (192 px) ;
    - `enum` et `timezone` : 20 rem (320 px) ;
    - `str` et `email` : 28 rem (448 px) ; `url` : 40 rem (640 px) ;
    - `polygon` : 10 rem par champ de la liste des sommets, longitude et latitude d'un sommet sur une ligne ;
    - `bool` : l'interrupteur à sa taille naturelle ;
    - aucun éditeur ne dépasse sa colonne, ni l'écran à 390 px.
  - **Contraste et taille** :
    - aide et description dépliée : taille calculée **d'au moins 13 px**, contraste **d'au moins 7:1** avec le fond du panneau, dans les deux thèmes. Aujourd'hui, l'aide fait 12 px et `--text-3` donne 6,9:1 en thème clair ;
    - libellé : graisse d'au moins 600, taille supérieure à celle de l'aide, contraste d'au moins 7:1 ;
    - texte des pastilles et des onglets : contraste d'au moins 4,5:1, onglets grisés par une recherche compris (aujourd'hui, `opacity: 0.45`) ;
    - les champs `int` et `float` affichent leurs chiffres en chasse fixe (`font-variant-numeric: tabular-nums`).
  - **Espacement** : au moins 12 px entre deux réglages, avec un séparateur ; au plus 4 px entre le libellé et l'aide. À 1280 px, la ligne d'un réglage `int`, `float`, `enum` ou `bool`, description repliée, fait **au plus 80 px** de haut.
  - **Barre d'outils** : la recherche et l'interrupteur « seulement les valeurs modifiées » sur une ligne à 1280 px. « Version des réglages » quitte la barre d'outils et passe dans le pied de l'onglet, en aide. La valeur de la version reste exposée et utilisée comme avant (EF-6.13).
- ~~**EF-6.19 Rectangle édité sur la carte**~~ (2026-11-25) : **supprimée par le lot Z1** (2026-11-26), avec le réglage `routing.coverage_bbox` et le type `rectangle`. Les principes qu'EF-6.21 lui empruntait (carte, recadrage, sécurité et réseau) sont désormais écrits dans EF-6.21. Texte d'origine : historique git de ce fichier.
- **EF-6.20 Zone couverte en polygone** (demande de l'utilisateur, 2026-11-25 ; décision de coord : **polygone libre**). La zone couverte du constructeur peut épouser un territoire, par exemple le nord de la France et la Belgique, au lieu d'un rectangle. **Depuis le lot Z1 (2026-11-26), c'est le seul réglage de zone** : `routing.coverage_bbox` est supprimé. Son usage par le constructeur est décrit dans [gpx-route-builder](gpx-route-builder.md) RB-1.3b.
  - **réglage** `routing.coverage_polygon`, nouveau type de registre **`polygon`**, section « Traces planifiées », libellé « Zone couverte (polygone) ». Pas de variable d'env, comme les autres réglages `routing.*` hormis `routing.base_url` (P-TR5-1) ;
  - **valeur** : liste JSON de sommets `[lon, lat]` en degrés décimaux, dans l'ordre du contour, **sans** répéter le premier sommet à la fin : le contour se referme seul, du dernier sommet au premier. Le sens (horaire ou non) est indifférent ;
  - **défaut** (lot Z1) : les **4 sommets du rectangle par défaut** `[1.35, 48.83, 6.42, 51.55]`, dans l'ordre sud-ouest, sud-est, nord-est, nord-ouest : `[[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]`. La zone par défaut est donc exactement le rectangle par défaut d'avant. « Rétablir la valeur par défaut » (EF-6.3b, §5.6b : `DELETE` immédiat après confirmation dans la page) remet ces 4 sommets ;
  - **contrôles du serveur** à l'écriture (400 avec le champ, rien n'est écrit, EF-6.3). Ils s'appliquent dans cet ordre ; le premier qui échoue donne son message :
    1. une liste de **3 à 50 sommets** : « Zone : de 3 à 50 sommets. ». **`[]` est refusé** avec ce message (lot Z1 : il n'y a plus de rectangle de repli). Le nombre se contrôle **en premier**, avant de lire un seul sommet : c'est la borne de taille du réglage (EF-6.1). Une valeur valide fait au plus ≈ 2 Kio en JSON compact (50 sommets de deux nombres à 17 chiffres) ; il n'y a pas de borne en octets distincte, la limite du corps JSON de l'API (2 Mio) s'applique déjà ;
    2. chaque sommet est une liste de **2 nombres finis** (ni booléen, ni texte, ni `NaN`) : « Zone : chaque sommet est [longitude, latitude]. ». Un entier démesuré n'est jamais converti en flottant (règle reprise du rectangle, T-TR5-B) ;
    3. longitude de −180 à 180, latitude de **−85,0511 à 85,0511** : « Zone : longitude de −180 à 180, latitude de −85,0511 à 85,0511. ». C'est la limite de la projection de la carte (Web Mercator) : un polygone est toujours entièrement dessinable et éditable ;
    4. **emprise** (rectangle englobant des sommets) de **2 000 km de côté au plus**, avec le calcul de P-TR5-1 (largeur le long du parallèle le plus proche de l'équateur, hauteur le long d'un méridien, haversine) : « Zone : emprise de 2 000 km de côté au plus. ». C'est la borne de taille du contour, celle de l'ancien rectangle ;
    5. **polygone simple** : jamais deux sommets identiques ; deux arêtes non voisines ne se touchent pas ; deux arêtes voisines n'ont en commun que leur sommet (pas de retour en arrière sur la même ligne). Message : « Zone : le contour se croise (arêtes i–j et k–l). », numéros de sommets comptés à partir de 1, avec la première paire d'arêtes trouvée en parcourant les arêtes dans l'ordre. Un polygone dont tous les sommets sont alignés échoue à ce contrôle. « Alignés » s'entend **sur la carte**, dans la projection Web Mercator (géométrie commune, ci-dessous), pas en longitude et latitude brutes (décision de coord, 2026-11-25) : des sommets sur un même parallèle ou un même méridien sont alignés dans les deux systèmes, des sommets alignés en diagonale en longitude et latitude ne le sont en général pas sur la carte ;
  - la valeur stockée est la liste reçue, nombres en flottants, **sans arrondi** du serveur (l'éditeur arrondit à 4 décimales, EF-6.21) ;
  - **géométrie commune** : les arêtes, le contrôle 5 et l'appartenance d'un point à la zone (RB-1.3b) se calculent **dans la projection de la carte**, Web Mercator : x = longitude, y = (180/π) · ln(tan(π/4 + latitude · π/360)), en degrés. Raison : la carte trace chaque arête en ligne droite dans cette projection. En longitude et latitude brutes, une arête diagonale de 300 km vers 50° de latitude s'écarte jusqu'à ≈ 2 km du trait affiché : un point vu dedans sur la carte serait refusé par le serveur, ou l'inverse. Un rectangle de 4 sommets, comme la zone par défaut, reste exactement l'ancien rectangle : ses arêtes suivent des méridiens et des parallèles, droites dans les deux systèmes ;
  - **valeur en base invalide** (donnée forgée, `[]` compris) : le registre la remplace par le **défaut** (les 4 sommets), avec le `WARNING` habituel du registre. Jamais « aucune zone » ;
  - ~~**rectangle et polygone**, mention « Inutilisé : la zone polygonale s'applique. »~~ : **supprimés par le lot Z1**, avec la ligne `routing.coverage_bbox` de la page Réglages ;
  - **diagnostic** de l'onglet « Fonctions bêta » (P-TR5-9) : `coverage_contained` compare l'**emprise de la zone en vigueur** au rectangle de `/info`. Le message parle de « la zone couverte (réglage `routing.coverage_polygon`) », plus de `routing.coverage_bbox` ;
  - **reprise de l'ancien réglage** (lot Z1) : **migration nommée `0017_coverage_polygon_only`** (§7, ADR-0012). Principe : **la zone en vigueur juste avant la migration est la zone en vigueur juste après**. Avant la migration, la zone en vigueur était le polygone s'il était valide et non vide, sinon le rectangle s'il était valide, sinon le rectangle par défaut. La migration lit les deux lignes de `settings` telles quelles, puis applique les étapes ci-dessous. Elle porte **sa propre copie, figée, des deux contrôles** : contrôles 1 à 5 du polygone tels qu'écrits le 2026-11-26, projection Web Mercator comprise, et anciennes règles du rectangle. Elle **n'importe ni le registre (`app.settings`) ni `app.services.routing`**. Une évolution future d'EF-6.20 ne change donc pas son comportement, et `db.py` ne dépend pas des réglages (ADR-0012 : une migration publiée n'est jamais modifiée ; revue Z1-1) :
    1. ligne `routing.coverage_polygon` **valide et non vide** (contrôles 1 à 5 ci-dessus) ⇒ gardée telle quelle ;
    2. sinon, ligne `routing.coverage_bbox` **valide** selon les anciennes règles (4 nombres finis, min < max, lon de −180 à 180, lat de −90 à 90, côté ≤ 2 000 km) et **différente du rectangle par défaut** (égalité des 4 nombres) ⇒ `routing.coverage_polygon` reçoit ses 4 sommets (sud-ouest, sud-est, nord-est, nord-ouest), sans arrondi ;
       - seul cas où ces 4 sommets échouent aux contrôles 1 à 5 : une latitude au-delà de ±85,0511, que l'ancien rectangle acceptait. La ligne du polygone est alors supprimée (zone par défaut), avec un `WARNING` « routing.coverage_bbox hors de la projection de la carte : zone remise au défaut » (sans coordonnée). **Hypothèse** : ce cas n'existe sur aucune instance (zone Hauts-de-France + Belgique) ; il n'est pas ramené au bord de la projection ;
    3. sinon (pas de rectangle en base, rectangle invalide, rectangle égal au défaut, polygone `[]` ou invalide) ⇒ la ligne `routing.coverage_polygon` est **supprimée** : la zone est le défaut, origine « défaut » ;
    4. dans tous les cas, la ligne `routing.coverage_bbox` est **supprimée** (sinon le registre écrirait à chaque démarrage « Réglage inconnu de cette version ») ;
    5. si une ligne a changé : `settings_meta.version` incrémentée (une page Réglages ouverte reçoit le 409 de EF-6.13), `updated_by = 'system'`, une entrée d'audit par réglage modifié ou supprimé, acteur `system`, avec le nom de la migration ;
    - idempotente (ADR-0012) : rejouée, elle ne trouve plus de ligne `routing.coverage_bbox` et ne change rien. Une transaction, avec son enregistrement dans `schema_migrations` ;
    - aucune coordonnée dans les journaux ; les valeurs avant / après vont dans l'audit, comme tout changement de réglage ;
    - le retour à une image antérieure au lot Z1 après la migration retrouve un polygone et pas de rectangle : la zone est la même. Une instance migrée qui garde `[]` n'existe pas (cas 3) ;
    - **limite** : retour à une image d'avant Z1, puis modification du rectangle (possible au cas 3), puis nouvelle montée de version ⇒ `0017` ne se rejoue pas (elle est déjà dans `schema_migrations`). Le rectangle est alors ignoré, avec le `WARNING` « Réglage inconnu de cette version » à chaque démarrage. La procédure de retour arrière de l'exploitation doit le dire (doc).
- **EF-6.21 Polygone édité sur la carte** (principes repris de l'ancienne EF-6.19, supprimée par le lot Z1) :
  - **carte** : hauteur d'au moins 320 px à 1280 px de large et d'au moins 240 px à 390 px ; toute la largeur de l'éditeur. Nom accessible : « Carte de la zone couverte : déplace les sommets, ou saisis-les dans la liste ». Fond et tuiles : §5.6b (`polygon`) ;
  - **recadrage** : la carte n'est **jamais recréée** à la saisie (zoom et centre conservés). Elle se recadre sur le polygone seulement au chargement, après « Appliquer » du volet « Avancé », après « Annuler les modifications », après « Rétablir la valeur par défaut » et avec un bouton « Recadrer » ;
  - **saisie comme une autre** : un geste sur la carte est une saisie non enregistrée (EF-6.13, EF-6.14) : ligne et onglet marqués, confirmation avant / après, un seul `PATCH` à l'enregistrement de l'onglet ; rien n'est écrit avant. « Annuler les modifications » remet le polygone chargé ;
  - **sécurité et réseau** : aucune requête de plus. Tuiles du fond par défaut seulement si tous ses hôtes sont dans la liste blanche (EF-6.12) ; sinon carte sans tuiles, polygone toujours éditable. CSP inchangée ; ni script ni style en ligne ;
  - **état de départ** : les sommets de la valeur **chargée** de `routing.coverage_polygon`, toujours 3 à 50 (lot Z1) ; au défaut, les 4 sommets du rectangle par défaut (1 sud-ouest, 2 sud-est, 3 nord-est, 4 nord-ouest), origine « défaut » comme tout réglage. Il n'y a plus de mention « Zone actuelle : le rectangle… » ni d'état « valeur `[]` » ;
  - **sommets** : une poignée par sommet ; zone de prise d'au moins 24 × 24 px ; focus visible ; numéro du sommet affiché au survol et au focus. Nom accessible : « Sommet 3 sur 7 : flèches pour déplacer, Suppr pour retirer, Insér pour ajouter un sommet après ». Glisser une poignée déplace le sommet, à la souris et au toucher. Glisser ailleurs que sur une poignée ou une arête déplace la carte ; la molette et le pincement zooment ;
  - **ajouter un sommet** : un appui sur une **arête** crée un sommet à cet endroit. Ce sommet suit le pointeur jusqu'au relâchement et s'insère, à sa position au relâchement, entre les deux sommets de cette arête (arête de fermeture comprise). Clic simple ou glisser : c'est le même geste, sur le modèle de l'insertion de RB-2.7.
    - tolérance de l'appui : 10 px à la souris, 16 px au toucher et au stylet. Une poignée de sommet a priorité sur une arête, et une arête sur la carte. Au toucher, un glisser qui commence sur une arête crée un sommet au lieu de déplacer la carte (effet accepté, comme RB-2.7) ;
    - survol d'une arête à la souris : curseur `pointer`, sommet fantôme sous le pointeur, bulle « Clique ou glisse pour ajouter un sommet ». Une ligne d'aide sous la carte dit la même chose, pour le toucher ;
    - à 50 sommets : aucun sommet créé, message « 50 sommets au plus. » ;
    - Échap pendant le glisser ⇒ le nouveau sommet disparaît, polygone inchangé ;
  - **retirer un sommet** :
    - clic droit sur un sommet (souris) ;
    - clic ou appui bref sur un sommet (moins de 3 px de mouvement) ⇒ sommet **sélectionné**, mis en évidence ; le bouton « Retirer le sommet » sous la carte retire le sommet sélectionné. Sans sélection, ce bouton est inactif ;
    - Suppr ou Retour arrière sur le sommet qui a le focus ;
    - à 3 sommets : retrait impossible (bouton inactif, clic droit et Suppr sans effet), message « 3 sommets au moins. » ;
  - **clavier sur la carte**, en plus de la liste des sommets :
    - Tab parcourt les sommets dans l'ordre 1 … n ;
    - flèches : 0,01° par appui ; Maj + flèches : 0,1° ;
    - Insér (ou +) ajoute un sommet au milieu de l'arête qui mène au sommet suivant (pour le dernier : au premier), milieu pris dans la projection de la carte, et lui donne le focus ;
    - Suppr retire le sommet ;
    - chaque touche est un geste complet : synchronisation et contrôles immédiats, sans Entrée ;
  - **liste des sommets** : volet dépliable « Coordonnées des sommets (n) », replié par défaut. Une ligne par sommet : numéro, champs Longitude et Latitude (pas de 0,0001), boutons « Ajouter après » et « Retirer ». C'est l'**alternative complète** à la carte, au clavier ;
  - **synchronisation dans les deux sens** :
    - pendant un geste, la liste, la ligne « Emprise ≈ … km × … km · n sommets » et le JSON du volet « Avancé » suivent, avec des valeurs arrondies à 4 décimales ; au relâchement, le polygone se recale sur ces valeurs arrondies ;
    - une saisie valide dans la liste déplace le sommet, sans recréer la carte ;
    - le volet « Avancé » accepte aussi un contour fermé (dernier sommet égal au premier) : la page retire ce doublon avant d'appliquer ;
  - **contrôles** : la page applique **les mêmes contrôles et les mêmes messages** que le serveur (EF-6.20), par une seule fonction, à chaque geste et à chaque saisie.
    - pendant un geste, seules les bornes arrêtent un sommet : ±180 en longitude, bord de la carte (±85,0511) en latitude ;
    - polygone invalide (contour qui se croise, sommets identiques, emprise trop grande) ⇒ polygone en **style d'erreur** (trait pointillé), message sous la carte, ouverture de la confirmation bloquée (EF-6.13). Les poignées **restent actives** pour corriger sur la carte ;
    - saisie invalide dans un champ de la liste (vide, hors bornes) ⇒ erreur sous le champ ; le polygone garde sa dernière valeur valide, en style d'erreur ;
  - ~~**retour au rectangle**, bouton « Revenir au rectangle »~~ : **supprimé par le lot Z1**. Le retour à la zone par défaut passe par « Rétablir la valeur par défaut » (EF-6.3b, §5.6b), commun à tous les réglages ;
  - **confirmation d'enregistrement** (EF-6.13) : avant et après se résument en « n sommets, emprise ≈ … km × … km », suivi de « (défaut) » pour la valeur par défaut. Jamais la liste complète des coordonnées ;
  - **aucune nouvelle dépendance** : le Leaflet du dépôt suffit (`L.polygon`, marqueurs déplaçables natifs). Aucun greffon (Leaflet.Editable, Geoman…) sans nouvelle décision de coord.
- **EF-6.22 Marge de la zone** (lot Z2, décision de coord du 2026-11-26) : réglage `routing.zone_margin_km`, entier de **0 à 50**, unité `km`, défaut **10**, section « Traces planifiées ». Il fixe la marge de l'emprise d'extraction des données OSM et des dalles MNT ([routing-zone-donnees](routing-zone-donnees.md) ZD-5).
  - **aucune demande** : l'enregistrer, ou le rétablir (EF-6.3b), ne crée pas de demande de reconstruction et ne change pas le statut de la couverture. La nouvelle marge s'applique à la **prochaine construction** (changement de zone ou mise à jour mensuelle), lue à la prise de la demande ;
  - **aide sous le champ**, toujours visible (pas repliée derrière « ? ») : « **S'applique à la prochaine construction de la couverture.** » ;
  - la confirmation d'enregistrement (EF-6.13) reste la confirmation ordinaire : pas d'annonce de régions, de téléchargement ni de redémarrage (contrairement à la zone, ZD-4).
- **EF-6.23 Altitude et tâche de couverture** (lot Z2, revue TZ2c-m5, décision de coord du 2026-11-26) : la gestion des dalles MNT par la tâche de couverture (téléchargement, plafond `DEM_MAX_TILES`, purge) suit la **variable d'environnement** `DEM_URL` seule, jamais le réglage `DEM_URL` enregistré en base ([routing-zone-donnees](routing-zone-donnees.md) ZD-1, ZD-4.3, ZD-8). L'onglet Altitude le dit :
  - **aide sous le champ** `DEM_URL`, toujours visible (pas repliée derrière « ? »), affichée seulement avec `ROUTING_ZONE_BUILD=1` (masquée avec `0`, confirmé par coord le 2026-11-26) : « **La gestion des dalles par la tâche de couverture suit la variable d'environnement DEM_URL, pas une valeur enregistrée ici.** » ;
  - le réglage garde son effet actuel sur la correction d'altitude des activités (EF-6.8, tâche `dem_backfill`) ; rien d'autre ne change.
- **Décisions de coord sur les questions de spec** (2026-11-25) :
  1. **8 onglets** = les sections réelles du registre, sans fusion de Stockage et Général ;
  2. « **écarts** » = valeurs **modifiées** (origine `base` ou `env`) ; les alertes ont leur propre pastille ;
  3. **contrôle de version au `PATCH`** (409) : oui (EF-6.13) ;
  4. **attribut `unit`** et libellés sans l'unité : oui (EF-6.11) ;
  5. **banc de test navigateur** Playwright local, hors CI : oui (EF-6.17) ;
  6. **fond par défaut inexistant** : contrôlé par la page seulement, sans 400 du serveur ni lot couplé ; le serveur garde son bandeau d'usage (`default_basemap_alert`).

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
  L'état « verrou détenu / non détenu » figure dans le diagnostic : **reporté après la v1** (décision de coord, 2026-11-25, à relire par l'utilisateur, E-22 ; `Scheduler.status()` existe, non exposé).
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
  - jetons (`inspect`, `consume`) : par IP ;
  - changement d'adresse (`POST /api/me/email`) : par compte et par IP (EF-4.6) ; renvoi du mail de validation : par compte (EF-4.5).
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
- **`0017_coverage_polygon_only`** (lot Z1, 2026-11-26) : migration de **données** seulement, sans changement de schéma : reprise de `routing.coverage_bbox` dans `routing.coverage_polygon`, puis suppression de la ligne du rectangle (EF-6.20, CA-87).
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

- Administration : onglets **Utilisateurs · Invitations · Fonctions bêta · Réglages · Clés API et e-mail · Journal** (Clés API et E-mail réunis : accepté, E-21) ; bandeaux : SMTP absent, collision d'identité, destination hors liste blanche (bandeaux « SMTP absent » et « destination hors liste » **à faire**, décision de coord, 2026-11-25, à relire par l'utilisateur, E-20/E-21, CA-64).
- Profil : invitations (pour **tout** compte, SSO ou `password`, quand elles sont disponibles : EF-3.12), mot de passe, adresse, second facteur (ces trois derniers pour les comptes `password`).
- Connexion : `password` ⇒ formulaire seul ; `oidc` ⇒ SSO + formulaire toujours visible (masqué seulement si EF-0.4) : **une seule page** qui porte le bouton SSO et le formulaire ; `/auth/login` ne redirige plus d'office vers l'IdP quand les comptes à mot de passe sont disponibles (à faire, décision de coord, 2026-11-25, à relire par l'utilisateur, E-21, CA-63).
- Réglages : **sous-onglets par section, recherche, éditeurs structurés et enregistrement par onglet : §5.6b** (refonte du 2026-11-25, CA-67 à CA-80) ; lisibilité et rectangle édité sur la carte : EF-6.18, EF-6.19 (CA-82, CA-83) ; zone couverte en polygone : EF-6.20, EF-6.21 (CA-85, CA-86) ; marge de la zone (lot Z2) : EF-6.22 (CA-88) ; aide de `DEM_URL` sur les dalles de la tâche (lot Z2) : EF-6.23 (CA-89) ; source, surcharge, retour au défaut ; avertissement « modifier la destination efface le secret associé » sous forme de **confirmation avant l'enregistrement** quand un secret lié est configuré (à faire, décision de coord, 2026-11-25, à relire par l'utilisateur, E-21, CA-65) ; fonds de carte : hôtes autorisés affichés, fond hors liste signalé.
- Polissage **accepté pour l'instant** (E-21) : marqueur de surcharge d'env, hôtes de tuiles autorisés affichés, libellés d'acteur du journal (« Compte `cli` » / « Ligne de commande » / « Système »), lien du bandeau de collision vers les comptes, diagnostic du moteur mis en forme.
- Diagnostic : état de chaque tâche de fond (active, dernière exécution, dernier résultat ok/erreur) : **reporté après la v1** (E-22).
- Liens d'invitation et codes de secours : affichés une fois, bouton « Copier ».

---

## 10. Critères d'acceptation

| # | Critère |
|---|---|
| CA-1 | Aucune route ne crée de compte `password` sans jeton valide. |
| CA-2 | 0 crédit ⇒ 409 ; 1 crédit et deux créations concurrentes ⇒ une seule réussit, crédit final 0. |
| CA-3 | Un admin crée N invitations sans que ses crédits bougent. |
| CA-4 | Non-admin qui modifie des crédits par `PATCH /api/admin/users/<id>` ⇒ 403. Par `POST /api/invitations` : `initial_credits` ou une note ⇒ **400** « Seul un administrateur peut joindre une note ou des crédits. » ; `invite_credits` dans le corps ⇒ ignoré, crédits inchangés (décision de coord, 2026-11-25, à relire par l'utilisateur, E-23 : aligné sur le code). |
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
| CA-13b (Doc-12) | Un compte `password` actif sans facteur (`bob`) : `PATCH /api/admin/settings {"auth.mfa_policy": "all"}` ⇒ **409** avec le nombre de comptes concernés (1), valeur effective toujours `admins`, version des réglages inchangée, ligne d'audit du refus ; même requête dans un lot avec un autre réglage valide ⇒ 409, **aucun** des deux n'est écrit. `bob` désactivé, ou après enrôlement d'un facteur ⇒ 200, valeur `all`. Un compte qui n'a que des codes de secours compte comme sans facteur. Avec `all` en place, un compte nouvellement invité est conduit à l'enrôlement à sa première connexion. |
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
| CA-34 (B3) | Changer `smtp.host` ou `routing.base_url` ⇒ secret lié effacé (audit). Secret dont `bound_to` ≠ destination effective ⇒ non envoyé (serveur factice : aucune réception). Aucun secret n'est lu dans l'environnement (E-17). |
| CA-35 (B3) | `smtp.security = none` avec identifiant ou mot de passe ⇒ 400 ; envoi en `none` : aucune commande `AUTH` reçue par le serveur factice. `starttls` refusé par le serveur ⇒ échec, aucun envoi en clair. |
| CA-36 (B3) | Destination hors `OUTBOUND_ALLOWED_HOSTS` ⇒ 400 à l'écriture ; forcée en base ⇒ appel refusé à l'usage (requête et ordonnanceur de fond), audit. Destination d'env hors liste ⇒ autorisée. |
| CA-37 (B3, r4) | « Tester » et e-mail de test : réponses ok et ko identiques en forme, sans code ni message ; durée de réponse = délai du service (`routing.timeout_s`, `smtp.timeout_s` ; pas de « Tester » DEM en v1, E-18) à 10 % près, que la cible factice réponde tout de suite, refuse la connexion ou ne réponde jamais. |
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
| CA-56 (EF-1.8, E-1) | Admin ⇒ `POST /api/admin/users/<id>/reset-link` pour un compte `password` à adresse validée, SMTP factice configuré : réponse avec le lien et `sent_by_mail: true` ; **un** mail reçu (drain de la file) qui nomme l'identifiant et contient le même lien, construit sur `PUBLIC_URL`, jeton dans le fragment ; jeton `reset` valable **24 h** (horloge injectée : accepté à 23 h 59, refusé à 24 h 01) ; usage unique. Sans SMTP, ou adresse seulement en attente ⇒ `sent_by_mail: false`, aucun mail, lien montré. Consommation ⇒ sessions antérieures refusées (CA-14), aucune session ouverte (CA-45), second facteur conservé. Aucun jeton dans les journaux ni l'audit. |
| CA-56b (EF-1.8, EF-1.9, E-1-1) | Compte `password` à adresse validée, SMTP factice. L'admin émet le lien A (`reset-link`), puis un second lien B ⇒ A est refusé à `inspect` et à `consume` (réponse générique de jeton invalide), B reste valable. Mot de passe oublié pour ce compte ⇒ lien C mis en file, B refusé, C valable. Mot de passe oublié pour un identifiant inexistant, ou pour un compte sans adresse validée ⇒ réponse identique, aucun jeton émis, aucun lien du compte réel révoqué. Usage de C ⇒ mot de passe changé, aucun autre jeton `reset` du compte valable ; filet : deux jetons `reset` valables insérés directement en base, l'usage de l'un ⇒ l'autre est refusé. Émission et révocation dans la même transaction : une émission concurrente ne laisse jamais deux liens valables à la fin. Jetons `invite`, `verify_email` et `bootstrap` du compte non touchés. Audits de révocation présents, sans jeton. |
| CA-57 (EF-2.5, E-3 a) | Un code de secours utilisé avec succès ⇒ **une** notification `recovery_used` à l'adresse validée, pour chacun des chemins : connexion, `POST /api/me/password`, `POST /api/me/email`, `DELETE /api/me/mfa/totp`, `DELETE /api/me/mfa/webauthn/<id>`. Code refusé ⇒ aucune notification. Compte sans adresse validée ⇒ aucun mail (P-T11-6). |
| CA-58 (P-T11-7, E-3 b) | Compte `password` qui n'a **que** des clés WebAuthn (authentificateur virtuel) : `POST /api/me/mfa/webauthn/proof/options` puis `DELETE /api/me/mfa/webauthn/<id>` avec mot de passe + assertion ⇒ 200, **aucun** code de secours consommé ; `POST /api/me/mfa/recovery-codes` avec mot de passe + assertion ⇒ 10 nouveaux codes, anciens refusés. Refus : défi de connexion ou d'enrôlement présenté au profil, défi d'une autre session, défi expiré (> 2 min), défi rejoué, mauvaise origine, assertion d'une clé d'un autre compte ⇒ **403**, rien n'est changé, l'échec compte pour le limiteur du second facteur (6e échec ⇒ 429). Retrait de la dernière clé d'un admin ⇒ 409 (EF-2.2), même avec une assertion valide. Mot de passe faux ⇒ refus avant toute vérification de l'assertion. |
| CA-59 (EF-2.4b, E-13) | Avec la liste embarquée : `PUBLIC_URL=https://app.exemple.fr` et `WEBAUTHN_RP_ID=exemple.fr` ⇒ accepté ; `WEBAUTHN_RP_ID` = `fr`, `gouv.fr`, `blogspot.com`, `github.io`, `co.uk` ⇒ refusé (suffixes publics, sections ICANN et PRIVATE) ; jokers et exceptions de la liste réelle respectés (une entrée `*.X` refuse `a.X` ; une exception `!b.X` accepte `b.X`) ; nom internationalisé comparé en punycode ; adresse IP ⇒ refusée. Le SHA-256 du fichier livré est celui de `SOURCE` ; **aucun appel réseau** au démarrage ni à l'usage (test avec le réseau coupé) ; fichier absent ou vide ⇒ démarrage refusé quand les comptes à mot de passe sont disponibles. |
| CA-60 (EF-4.5, E-15) | Compte à adresse en attente, SMTP configuré : bandeau présent sur le tableau de bord, le visualiseur et le profil, avec le bouton ; compte à adresse validée ⇒ pas de bandeau ; sans SMTP ⇒ bandeau sans bouton. `POST /api/me/email/resend-verification` : 3 renvois dans l'heure ⇒ 200 et 3 mails ; 4e ⇒ 429 avec `Retry-After` (horloge injectée) ; un renvoi par l'admin entre-temps compte dans les 3 ; après un renvoi, le lien du mail précédent ne valide plus rien et le dernier valide l'adresse (avec la session du compte, CA-17b). Sans session ⇒ 401 ; compte SSO ⇒ **404** (comme les routes `/api/me/mfa`) ; compte `password` sans adresse en attente, ou SMTP non configuré ⇒ **409**. |
| CA-60b (EF-4.6, EF-S3, E-15-1) | Compte `password`, SMTP factice, horloge injectée. 3 `POST /api/me/email` acceptés dans l'heure ⇒ 200, 3 mails de validation et 3 notifications à l'ancienne adresse. 4e ⇒ **429** avec `Retry-After` : `pending_email` inchangée, aucun mail, aucune notification, aucun audit `user.email_change_requested`. Une heure plus tard ⇒ de nouveau accepté. Mot de passe faux, second facteur faux, adresse invalide ou identique ⇒ refus habituel (401, 403, 400 ou 409), et le budget des changements n'est pas consommé : 3 refus puis 3 changements valides ⇒ les 3 acceptés. Le 429 est rendu sans vérifier le mot de passe (aucun hachage, compté par une sonde). **Budgets distincts** : après 3 changements, `POST /api/me/email/resend-verification` ⇒ 3 renvois acceptés (200), puis 429 ; inversement, 3 renvois ne bloquent pas un changement. Par IP : 10 changements acceptés depuis la même IP sur 4 comptes ⇒ le 11e ⇒ 429 ; depuis une autre IP ⇒ accepté. Concurrence : 5 requêtes valides simultanées sur un compte neuf ⇒ exactement 3 acceptées. Après 2 changements, seul le lien du dernier mail valide l'adresse. |
| CA-61 (EF-0.4, E-10) | Mode `oidc` **avec** `SECRETS_KEY` mais **sans** `PUBLIC_URL` (et inversement) : `PUT /api/admin/secrets/<nom>` et `POST …/test` ⇒ **404** ; routes des comptes à mot de passe et des invitations ⇒ 404, **liste admin des invitations comprise** (`GET /api/admin/invitations`, ainsi que la création et la révocation) ; page d'administration : formulaire des secrets absent, explication qui nomme la ou les variables manquantes. Avec les deux variables ⇒ saisie possible. Démarrage jamais refusé pour ce motif. |
| CA-62 (EF-1.2, CA-31, E-12) | Mode `oidc`, un compte `password` `alice` existe : connexion SSO dont l'IdP fournit l'identifiant `Alice` ⇒ connexion acceptée, **un** `WARNING` et **une** ligne d'audit `identity.collision` (`kind = 'identifiant'`, comptes concernés) ; reconnexions suivantes avec le même identifiant ⇒ aucune nouvelle ligne ; changement d'identifiant côté IdP vers un autre identifiant pris ⇒ nouvelle ligne. Le login par mot de passe et le mot de passe oublié de `alice` fonctionnent toujours. |
| CA-63 (§9, E-21) | Mode `oidc` avec comptes à mot de passe disponibles, sans session : `GET /auth/login` et une route protégée ⇒ **une page** qui contient le bouton « Se connecter avec le SSO » et le formulaire identifiant / mot de passe (aucune redirection automatique vers l'IdP) ; le bouton mène à l'IdP (redirection OIDC habituelle, `next` conservé) ; le formulaire mène à la connexion en deux étapes (EF-1.6). Comptes à mot de passe indisponibles (EF-0.4) ⇒ comportement actuel (redirection vers l'IdP). Mode `password` ⇒ formulaire seul, pas de bouton SSO. |
| CA-64 (§9, EF-6.8, E-20/E-21) | Page d'administration : sans réglages SMTP (hôte ou expéditeur vides) ⇒ bandeau « E-mail non configuré : les adresses ne peuvent pas être validées, le mot de passe oublié est désactivé » ; SMTP configuré ⇒ pas de bandeau. Destination effective d'un service (`routing.base_url`, `DEM_URL`, `smtp.host:port`, fond de carte) **hors** `OUTBOUND_ALLOWED_HOSTS` (forcée en base) ⇒ bandeau qui nomme le service, pas la valeur d'un secret ; `GET /api/admin/settings/diagnostic` le signale aussi. Remise dans la liste ⇒ bandeau absent. |
| CA-65 (§9, EF-5.5, E-21) | Réglages : modifier `smtp.host`, `smtp.port` ou `routing.base_url` alors que le secret lié est configuré ⇒ confirmation **dans la page** (pas de `confirm()` du navigateur) qui dit que le secret sera effacé ; annuler ⇒ aucune requête ; confirmer ⇒ PATCH, secret effacé, audit (CA-34). Sans secret configuré ⇒ pas de confirmation. |
| CA-66 (EF-4.8, E-16) | File d'envoi contenant N messages non envoyés (envoi bloqué par un serveur factice) ; arrêt de la file (`stop`) ⇒ **une** ligne `WARNING` avec N et les types de messages ; aucune adresse, aucun lien ni jeton dans la ligne. File vide à l'arrêt ⇒ aucune ligne. |
| CA-66b (EF-4.8, E-16, test 3a) | **Vrai processus** : l'application lancée par gunicorn comme dans l'image (`--workers 1 --threads 8`), serveur SMTP factice qui accepte la connexion puis ne répond plus ; N ≥ 2 messages mis en file (ex. deux mots de passe oubliés) ; aucune requête en cours ; `SIGTERM` au processus maître ⇒ dans les **5 s**, **une** ligne `WARNING` dans la sortie du processus, avec N et les types de messages, sans adresse, lien ni jeton ; le processus se termine avant le délai d'arrêt de Docker (10 s). File vide ⇒ aucune ligne. Le crochet est enregistré une seule fois par processus (pas de ligne en double). |
| CA-67 (EF-6.9, EF-6.11, API) | Registre : `SECTIONS` liste les 8 sections dans l'ordre de EF-6.9 (pas de fusion) ; tout réglage appartient à une section déclarée ; un registre de test avec une section non déclarée ⇒ `ValueError` à la construction ; tout réglage `int` ou `float` déclare `unit`, et aucun libellé ne finit par une unité entre parenthèses. `GET /api/admin/settings` (admin) ⇒ `sections` dans cet ordre, et pour chaque réglage les champs `unit`, `fallback`, `fallback_source`, `env_invalid`, `restart_pending` ; `map.basemaps` porte l'`allowlist` des hôtes `tiles=` effectifs (liste par défaut sans entrée `tiles=`, liste exacte sinon). Sans droit admin ⇒ 403. |
| CA-68 (EF-6.11, API) | `DEM_TIMEOUT=30` dans l'env, valeur 45 en base ⇒ `source: base`, `env_overridden`, `fallback: 30`, `fallback_source: env` ; `DELETE` ⇒ valeur effective 30, audit `setting.reset`. `DEM_TIMEOUT=abc` dans l'env, valeur 45 en base ⇒ `fallback: 15`, `fallback_source: défaut`, `env_invalid: true`. `map.zoom` en base (aucune variable d'env) ⇒ `fallback` = défaut. Registre de test avec un réglage `secret` ⇒ `value` et `fallback` valent `null`, aucune valeur d'env dans la réponse. `DELETE` de `routing.base_url` quand le secret lié est configuré et que la valeur d'env est une autre destination ⇒ secret effacé et tracé (CA-34). |
| CA-69 (EF-6.13, API) | `PATCH` `{map.zoom: 30, map.center_lat: 45}` ⇒ 400 avec `field: "map.zoom"`, aucune des deux clés écrite, aucun audit `setting.update`. `map.basemaps` dont le 3e fond a une URL sans `{y}` ⇒ 400, `field: "map.basemaps"`, `index: 2`. Fond dont un hôte effectif est hors de `tiles=` ⇒ 400, `field` et `index` renseignés, message qui nomme l'hôte. `auth.mfa_policy` refusée (CA-13b) ⇒ 409 avec `field`. |
| CA-70 (EF-6.13, API) | `PATCH` avec la `version` courante ⇒ 200 ; la réponse porte la nouvelle `version` (+1). Second `PATCH` avec l'ancienne `version` ⇒ **409**, `code: "settings_version_conflict"`, `version` courante, message « Un autre admin a modifié ces réglages : recharge la page avant d'enregistrer. ». Rien n'est écrit, aucun audit `setting.update`, `version` inchangée. Même 409 après un `DELETE` ou un `flask settings-reset` survenu entre le chargement et le `PATCH`. Concurrence : deux `PATCH` portant la même `version`, lancés en parallèle ⇒ un 200 et un 409, jamais deux écritures. `PATCH` sans `version` ⇒ 200 (compatibilité). `DELETE` ⇒ la réponse porte la nouvelle `version`. Le 409 de `auth.mfa_policy` porte `field` et pas ce `code`. |
| CA-71 (EF-6.15, API) | Registre de test avec un réglage `requires_restart` : au démarrage ⇒ `restart_pending: false` ; après `PATCH` ⇒ `true` ; nouvelle application créée sur la même base (nouveau processus) ⇒ `false`. Réglage sans l'attribut ⇒ jamais `true`. Registre v1 : aucun réglage `requires_restart` (EF-6.4). |
| CA-72 (EF-6.9, UI) | Page Réglages ; en base, seulement `map.zoom` ; dans l'env, seulement `DEM_TIMEOUT=30` : 8 sous-onglets dans l'ordre de EF-6.9 ; pastille « 1 » (nom accessible « 1 réglage modifié ») sur Carte et sur Altitude, et aucune pastille de nombre sur les 6 autres onglets (EF-6.18). `/admin#reglages/carte` ouvre Carte ; un rechargement rouvre Carte ; `#reglages/inconnu` ⇒ Sécurité. Rôles `tablist`, `tab` et `tabpanel` présents ; les flèches du clavier changent d'onglet. `DEM_TIMEOUT=abc` ⇒ pastille d'alerte sur Altitude. |
| CA-73 (EF-6.10, UI) | Recherche « delai » ⇒ résultats dans Traces planifiées, Altitude et E-mail, pastille de chaque onglet = nombre de réglages trouvés ; « DEM_URL » ⇒ Altitude seule ; « smtp.host » ⇒ E-mail seule ; les onglets sans résultat sont grisés, et l'onglet ouvert sans résultat donne les liens des onglets qui en ont. Avec l'état du CA-72, l'interrupteur « seulement les valeurs modifiées » ⇒ 2 réglages au total (`DEM_TIMEOUT`, `map.zoom`) ; combiné à la recherche « zoom » ⇒ 1. Une saisie sur un réglage ensuite masqué par la recherche ⇒ conservée, présente dans la confirmation et dans le `PATCH`. Ni l'URL ni le stockage du navigateur ne contiennent le texte recherché. |
| CA-74 (EF-6.11, UI) | Pastille « Environnement » ou « Base » selon l'origine ; aucune pastille pour l'origine défaut ; aucune pastille ne contient un nom de variable. Base sur `DEM_TIMEOUT=30` ⇒ aide « … variable DEM_TIMEOUT, remplacée par la valeur en base » et bouton « Rétablir la valeur d'environnement → 30 s » ; `map.zoom` en base ⇒ « Rétablir la valeur par défaut → 6 » ; origine défaut ou env ⇒ aucun bouton. Aide « 1 à 120 s · variable DEM_TIMEOUT » ; description repliée au chargement. Variable invalide ⇒ alerte qui nomme la variable, sans sa valeur. Rétablir ⇒ confirmation dans la page ; annuler ⇒ aucune requête ; confirmer ⇒ `DELETE`, la pastille d'origine change, les autres saisies de l'onglet restent. |
| CA-75 (EF-6.12, fonds de carte, UI) | Les cartes reprennent les fonds effectifs dans l'ordre. `tiles=a.tiles.exemple.fr` seulement ; ajout d'un fond `https://{s}.tiles.exemple.fr/{z}/{x}/{y}.png` avec les sous-domaines `ab` ⇒ message sous l'URL qui nomme `b.tiles.exemple.fr` et `tiles=`, confirmation bloquée, aucune requête. URL sans `{y}` ou en `http://` ⇒ message ; nom contenant `<` ou nom en double ⇒ erreur sous le nom. « Ajouter » désactivé à 10 fonds. Monter / Descendre puis enregistrer ⇒ le visualiseur reçoit les fonds dans le nouvel ordre. Supprimer ou renommer le fond par défaut ⇒ erreur sous « Fond par défaut » ; ce champ liste les noms saisis. Attribution `A & B` refusée par la page (texte brut). Volet « Avancé » : JSON = valeur en cours de saisie ; JSON modifié puis « Appliquer » ⇒ cartes à jour ; JSON invalide ⇒ erreur, cartes inchangées. Envoi forcé hors de la page d'un fond hors `tiles=` ⇒ 400 du serveur (CA-69). |
| ~~CA-76 (EF-6.12, rectangle, UI)~~ | **Supprimé par le lot Z1** (2026-11-26) : plus de réglage `rectangle`. Le calcul de l'emprise « ≈ 371 km × 302 km » de la zone par défaut est repris par CA-86. |
| CA-77 (EF-6.13, EF-6.14, UI) | Deux réglages modifiés dans Carte et un dans Altitude ⇒ pastille « non enregistré » sur les deux onglets. « Enregistrer » de Carte ⇒ panneau avec 2 lignes avant (valeur et origine) / après ; Annuler ⇒ aucune requête ; Confirmer ⇒ **un** `PATCH` avec exactement ces 2 clés et la `version`, puis 2 audits `setting.update`. Altitude garde sa saisie et sa pastille. Passage à l'onglet Journal puis retour ⇒ saisies présentes. Rechargement avec des saisies ⇒ avertissement du navigateur ; `localStorage` et `sessionStorage` sans valeur de réglage. 400 du serveur ⇒ message sous le champ en cause, `aria-invalid` et focus sur ce champ, saisies gardées, rien d'écrit. Version périmée (un autre admin a enregistré entre-temps) ⇒ dans le panneau, le message « Un autre admin a modifié ces réglages : recharge la page avant d'enregistrer. » et le bouton « Recharger » ; rien n'est écrit. « Recharger » ⇒ valeurs du serveur rechargées, saisies gardées ; un nouvel « Enregistrer » montre les nouveaux « avant ». Deux enregistrements successifs dans la même page ⇒ aucun 409. Destination avec secret lié ⇒ ligne « le secret … sera effacé » dans le même panneau (CA-65). « Enregistrer » inactif sans changement ; `15` saisi pour `15.0` ⇒ pas un changement. |
| CA-78 (EF-6.12, EF-6.15, registre de test, UI) | Réglage `list` (`max_items` 3) ⇒ pastilles ; ajout par Entrée ; doublon ⇒ erreur ; « Ajouter » désactivé à 3 éléments ; la croix retire un élément ; l'enregistrement envoie la liste. Réglage `requires_restart` ⇒ pastille « Redémarrage nécessaire », mention dans la confirmation et bandeau après l'enregistrement tant que `restart_pending` est vrai. |
| CA-79 (EF-6.16) | Test statique étendu au code de la page Réglages : aucun `innerHTML` alimenté par une donnée, aucun script ni style en ligne, aucun `confirm()`, `alert()` ou `prompt()`. CSP inchangée. Un fond dont l'attribution contient un texte de balise est refusé par le serveur (400). Registre de test avec un réglage `secret` ⇒ absent de la page. Le test `CONTROL_FOR_TYPE` vérifie que chaque type a un éditeur et qu'aucun ne passe par du JSON hors du volet « Avancé ». |
| CA-80 (EF-6.17) | `web-app/tests_browser/README.md` donne **une** commande ; l'image y est référencée par version **et** digest `sha256` ; aucune référence `latest`. `web-app/tests_browser/requirements.txt` est épinglé avec empreintes, et sa version de `playwright` est celle de l'image. `python -m pytest --collect-only` lancé dans `web-app/` comme dans la CI ⇒ aucun test de `tests_browser/` collecté ; le workflow CI ne mentionne ni `tests_browser` ni Playwright. La commande jouée sur un poste ⇒ CA-72 à CA-78 passent ; un test qui provoque une requête vers un hôte externe la voit interceptée, enregistrée et bloquée (aucune connexion sortante). |
| CA-81 (EF-3.12, EF-3.2, EF-0.4) | Mode `oidc` avec `SECRETS_KEY` et `PUBLIC_URL`. Compte SSO actif avec 2 crédits ⇒ `/profile` montre la section « Invitations », 2 crédits et le formulaire ; création ⇒ 201, crédits à 1, invitation listée et révocable. Compte SSO à 0 crédit ⇒ section visible avec la liste, sans formulaire, avec le motif « aucun crédit » ; `POST /api/invitations` forcé ⇒ 409, aucun jeton créé. Admin SSO ⇒ formulaire, crédits « illimité ». Compte `password` à adresse seulement en attente ⇒ section sans formulaire, motif « valide d'abord ton adresse » ; `POST` forcé ⇒ 403. Compte `password` à adresse validée avec crédits ⇒ formulaire. Mode `oidc` sans `PUBLIC_URL` (ou sans `SECRETS_KEY`), et mode `local` ⇒ section absente du HTML pour tous les comptes, `GET /api/invitations` ⇒ 404. Mode `password` ⇒ même comportement que le premier cas pour un compte `password`. |
| CA-82 (EF-6.18, banc, UI) | État du CA-72, fenêtre 1280 × 800, thèmes clair puis sombre. Les 8 onglets ont le même `top` (une ligne). Seuls Carte et Altitude portent une pastille de nombre. Aide de `DEM_TIMEOUT` : taille calculée ≥ 13 px, contraste ≥ 7:1 avec le fond du panneau (calculé à partir des couleurs calculées). Libellé : graisse ≥ 600, plus grand que l'aide, contraste ≥ 7:1. Onglets grisés par une recherche sans résultat : contraste ≥ 4,5:1. Largeur de la boîte de l'éditeur, unité comprise : `DEM_TIMEOUT` ≤ 192 px ; `smtp.security` ≤ 320 px ; `smtp.host` ≤ 448 px ; `DEM_URL` ≤ 640 px ; chaque champ de la liste des sommets de `routing.coverage_polygon` ≤ 160 px, longitude et latitude d'un sommet sur une ligne (lot Z1 : plus de `routing.coverage_bbox`). Libellé et éditeur de `DEM_TIMEOUT` côte à côte, alignés en haut, écart ≥ 24 px ; hauteur de la ligne ≤ 80 px ; « ? » et pastille sur la ligne du libellé. Aucune pastille « Défaut ». « Version des réglages » absente de la barre d'outils, présente dans le pied de l'onglet. Fenêtre 390 × 844 : barre d'onglets sur une ligne, défilable (`scrollWidth` > `clientWidth`) ; `scrollWidth` de la page ≤ 390 ; flèche droite jusqu'à « Général » ⇒ onglet visible dans la barre ; en-tête au-dessus de l'éditeur. Les CA-67 à CA-81 passent toujours. |
| ~~CA-83 (EF-6.19, banc, UI)~~ | **Supprimé par le lot Z1** (2026-11-26), avec EF-6.19. Carte, recadrage et réseau de l'éditeur de zone : CA-86. |
| CA-84 (EF-3.13, API et UI) | Mode `password`. `alice` (non admin, crédits) invite `x@exemple.fr` ; un admin invite `y@exemple.fr` ; une invitation révoquée, une expirée (date forcée), une acceptée par le compte `bob`. `GET /api/admin/invitations` (admin) ⇒ chaque entrée porte `created_by_name` et `used_by_name` ; aucune ne porte de jeton ni d'empreinte. Onglet Invitations : colonne « Émise par » = `alice` pour la première, l'identifiant de l'admin pour la seconde ; statuts « En attente », « Révoquée », « Expirée », « Acceptée » ; la ligne acceptée montre « compte créé : bob ». Une invitation acceptée puis révoquée en base reste « Acceptée ». Profil de l'émetteur de l'invitation acceptée : même statut « Acceptée » (casse de l'initiale indifférente) ; aucune page ne montre « Utilisée » comme statut. Suppression du compte `alice` ⇒ « Émise par » = « compte supprimé » (ses invitations en attente sont révoquées, EF-3.7) ; suppression de `bob` ⇒ « compte créé : compte supprimé ». Un identifiant `<b>x</b>` s'affiche tel quel, sans balise interprétée. Bouton « Révoquer » sur les seules invitations en attente. Non admin ⇒ 403 ; EF-0.4 ⇒ 404 (CA-61). |
| CA-85 (EF-6.20, API) | `routing.coverage_polygon` jamais écrit ⇒ valeur effective `[[1.35, 48.83], [6.42, 48.83], [6.42, 51.55], [1.35, 51.55]]`, origine défaut ; `routing.coverage_bbox` absent de `GET` des réglages, et son `PATCH` ⇒ 400 « Réglage inconnu », version inchangée. `PATCH` d'un polygone de 8 sommets qui suit le nord de la France et la Belgique (sommets fixés dans le test) ⇒ 200, valeur relue identique, version incrémentée, entrée d'audit. Polygone concave (forme en U) ⇒ 200 ; même polygone parcouru dans l'autre sens ⇒ 200 ; `DELETE /api/admin/settings/routing.coverage_polygon` (EF-6.3b) ⇒ les 4 sommets par défaut, origine défaut. Chacun de ces `PATCH` ⇒ 400 avec le message de EF-6.20, version inchangée, rien d'écrit : `[]` (« de 3 à 50 sommets ») ; 2 sommets ; 51 sommets ; 10 000 sommets (même message « de 3 à 50 sommets ») ; un sommet `[1, "50"]`, `[true, 50]`, `[1, 50, 3]` ou `[NaN, 50]` ; une longitude `10**400` (entier) ; une latitude de 86 ; une longitude de 181 ; une emprise de 2 100 km de côté ; le nœud papillon `[[0,0],[1,1],[1,0],[0,1]]` ⇒ message qui nomme les arêtes 1–2 et 3–4 ; un sommet répété ; 3 sommets alignés sur la carte (même latitude) ; un retour en arrière `[[0,0],[2,0],[1,0],[1,1]]`. Valeur forgée en base (`"x"`, `null`, `[]`, 2 sommets, nœud papillon) ⇒ valeur effective = les 4 sommets par défaut (effet sur la zone : CA-R49). |
| CA-86 (EF-6.21, banc, UI) | `routing.coverage_polygon` au défaut, fenêtre 1280 × 800 : carte ≥ 320 px de haut, 4 sommets aux angles du rectangle par défaut (1.35 / 48.83 / 6.42 / 51.55), ligne « Emprise ≈ 371 km × 302 km · 4 sommets » (calcul serveur à 1 km près), origine « défaut », onglet non marqué ; aucune ligne `routing.coverage_bbox` dans l'onglet ni mention « Zone actuelle : le rectangle… ». Glisser le sommet 3 (nord-est) jusqu'à (4,0 ; 48,0) ⇒ trait pointillé, message « Zone : le contour se croise (arêtes 1–2 et 3–4). », « Enregistrer » n'ouvre pas la confirmation ; le ramener à sa place ⇒ erreur levée. Appui au milieu de l'arête sud puis glisser de 50 px vers le sud ⇒ 5 sommets, le nouveau entre les sommets 1 et 2, à la position du relâchement ; pendant le geste, la liste, l'emprise et le JSON du volet « Avancé » suivent ; valeurs à 4 décimales au plus ; onglet marqué « non enregistré ». Clic simple sur l'arête est ⇒ sommet inséré au point cliqué. Clic à 9 px d'une arête ⇒ sommet ajouté ; à 12 px ⇒ aucun sommet ; au toucher (`hasTouch`) à 14 px ⇒ sommet ajouté. Appui sur une poignée de sommet ⇒ le sommet se déplace, aucun sommet créé. Survol d'une arête ⇒ curseur `pointer` et bulle. Clic droit sur un sommet ⇒ retiré ; à 3 sommets ⇒ « 3 sommets au moins. » et aucun retrait. Polygone de 50 sommets appliqué par le volet « Avancé », puis appui sur une arête ⇒ « 50 sommets au plus. », toujours 50 sommets. Contour fermé collé dans le volet « Avancé » ⇒ appliqué sans le doublon. Clavier : Tab jusqu'à « Sommet 2 sur 5… », flèche droite ⇒ longitude + 0,01 ; Insér ⇒ 6 sommets, focus sur le nouveau ; Suppr ⇒ 5 sommets. Liste des sommets : saisir une latitude ⇒ sommet déplacé, même instance de carte, zoom et centre inchangés. Enregistrer ⇒ confirmation « 5 sommets, emprise ≈ … » avant / après (avant suivi de « (défaut) ») ; Confirmer ⇒ un seul `PATCH`, avec la valeur de la carte. Après cet enregistrement : pastille « Base » et bouton « Rétablir la valeur par défaut → 4 sommets, emprise ≈ 371 km × 302 km » (aucune variable d'env pour ce réglage, §5.6b) ; appui, puis confirmation dans la page ⇒ un seul `DELETE /api/admin/settings/routing.coverage_polygon`, **immédiat**, sans passer par « Enregistrer » ni la confirmation avant / après, aucun `PATCH` ; puis les 4 sommets par défaut, origine défaut (aucune pastille), bouton absent, carte recadrée sur le polygone ; aucun bouton « Revenir au rectangle ». Saisir 2.0 dans la longitude du sommet 1 ⇒ sommet déplacé, même instance de carte ; « Recadrer » ⇒ carte recadrée sur le polygone. Fenêtre 390 × 844 : carte ≥ 240 px, poignées ≥ 24 × 24 px, aucun défilement horizontal de la page. Aucune requête hors API et tuiles d'hôtes autorisés pendant tout le scénario. |
| CA-87 (EF-6.20, lot Z1, migration `0017_coverage_polygon_only`) [CI] | Base au schéma d'avant Z1, une instance par cas, migration jouée au démarrage. Sur chaque cas sauf (g) : zone en vigueur avant (calculée par le code d'avant, fixée dans le test) = zone en vigueur après ; plus aucune ligne `routing.coverage_bbox` ; migration présente dans `schema_migrations`. (a) Aucune ligne ⇒ aucune ligne `routing.coverage_polygon`, origine défaut, version inchangée, aucune entrée d'audit. (b) Rectangle `[3.0, 50.6, 3.2, 50.7]`, polygone absent ⇒ polygone `[[3.0, 50.6], [3.2, 50.6], [3.2, 50.7], [3.0, 50.7]]`, `updated_by = 'system'`, version + 1, entrées d'audit acteur `system` (polygone écrit, rectangle supprimé) ; un point à (3,1 ; 50,65) accepté, Lille (3,06 ; 50,63) accepté, Liège refusé en 400. (c) Même rectangle et polygone `[]` ⇒ comme (b). (d) Même rectangle et polygone en U valide ⇒ polygone inchangé, rectangle supprimé. (e) Rectangle égal au défaut, polygone `[]` ⇒ ligne du polygone supprimée, origine défaut. (f) Rectangle forgé (`"x"`, `[0,0,100,100]`, `[NaN,0,1,1]`) et polygone absent ⇒ défaut. (g) Rectangle `[1, 80, 2, 89]` ⇒ défaut et un `WARNING` sans coordonnée. (h) Polygone forgé (nœud papillon) et rectangle `[3.0, 50.6, 3.2, 50.7]` ⇒ polygone = les 4 sommets du rectangle. Rejouer la migration (nom retiré de `schema_migrations`, lignes inchangées) ⇒ aucun changement, aucune entrée d'audit de plus. Aucun journal ne contient de coordonnée. Test statique : le code de `0017_coverage_polygon_only` n'importe ni `app.settings` ni `app.services.routing`. |
| CA-88 (EF-6.22, lot Z2) [CI et banc] | Avec `ROUTING_ZONE_BUILD=1`, statut `idle`. `PATCH` de `routing.zone_margin_km` à 20 ⇒ 200, version + 1, entrée d'audit ; statut toujours `idle`, numéro de demande inchangé, `claim` ⇒ `{}`. Puis `PATCH` d'une nouvelle zone et `claim` après le délai de stabilité ⇒ `margin_km` = 20 et emprise calculée avec 20 km. `PATCH` à 51 ou à -1 ⇒ 400. Banc : sous le champ de la marge, le texte « S'applique à la prochaine construction de la couverture. » est visible sans ouvrir « ? » ; Enregistrer ⇒ confirmation ordinaire avant / après (« 10 km » ⇒ « 20 km »), sans mention de régions ni de redémarrage. |
| CA-89 (EF-6.23, lot Z2) [CI et banc] | Avec `ROUTING_ZONE_BUILD=1`, variable d'environnement `DEM_URL` vide, réglage `DEM_URL` en base = `http://opentopodata:5000/v1/copernicus30`, `DEM_MAX_TILES` = 10 : `PATCH` d'une zone de 24 dalles ⇒ 200, aucun refus pour les dalles. Banc : onglet Altitude, sous le champ `DEM_URL`, le texte « La gestion des dalles par la tâche de couverture suit la variable d'environnement DEM_URL, pas une valeur enregistrée ici. » est visible sans ouvrir « ? » ; avec `ROUTING_ZONE_BUILD=0`, il est absent. |

**CA-72 à CA-78 (sauf CA-76, supprimé), CA-82, CA-86 et la partie banc de CA-88 et de CA-89** se vérifient avec le banc de test navigateur local (EF-6.17), joué par `test` avant chaque lot qui touche au front. La recette manuelle reste en complément.

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
| **Z1** | Un seul réglage de zone, `routing.coverage_polygon` (défaut : les 4 sommets du rectangle par défaut) ; `routing.coverage_bbox`, le type `rectangle` et EF-6.19 supprimés ; migration nommée `0017_coverage_polygon_only` (EF-6.20, CA-87) ; tâche TZ1 (gpx-route-builder §14) | demande relayée par coord, 2026-11-26 |

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

## 15. Précisions pour T11, T14 et T15 (2026-11-24)

Points que la spec laissait implicites, relevés avant le développement. Chacun porte un **défaut proposé** : dev l'applique pour ne pas s'arrêter ; l'utilisateur peut le corriger (liste à confirmer, relayée par coord). Aucun ne change un arbitrage ni un invariant.

**T11 (TOTP, codes de secours, enrôlement, fin d'amorçage)**
- **P-T11-1 Secret TOTP en cours d'enrôlement** : gardé **côté serveur, en mémoire**, dans l'état `mfa_pending` (étape `enroll`) ou, depuis le profil, dans un état d'enrôlement lié au `session_token` (10 min) ; **jamais en base avant confirmation**. Code valide ⇒ une transaction `BEGIN IMMEDIATE` : `totp_secret` chiffré, `totp_enabled_at`, `totp_last_step` = pas accepté, 10 codes de secours si c'est le premier facteur, `is_admin = 1` si l'état porte l'octroi d'admin ; puis l'état est détruit.
- **P-T11-2 Échéance de l'étape `enroll`** : **15 min** (installer une application et scanner prend plus que les 5 min de l'étape `mfa`, EF-1.6) ; 5 essais de code comme EF-2.6. Échéance dépassée après un amorçage ⇒ le jeton est consommé, le compte reste non admin sans facteur : reprise par `flask user-invite --admin --account <login>` (EF-0.3b). *Proposé : à confirmer ; en attendant, dev peut garder 5 min.*
- **P-T11-3 Approvisionnement TOTP** : URI `otpauth://totp/KmlViewer:<identifiant>?secret=<base32>&issuer=KmlViewer&algorithm=SHA1&digits=6&period=30` ; émetteur constant `KmlViewer` ; QR code SVG produit côté serveur (`segno`, ADR-0004) et inclus dans la page, aucun service externe ; secret affiché aussi en texte (base32 par groupes de 4) pour la saisie manuelle. Dépendances `pyotp` et `segno` (ADR-0004) à ajouter, épinglées avec empreintes : elles ne sont pas encore dans `requirements`.
- **P-T11-4 Codes de secours** : montrés **une fois**, sur la page d'enrôlement, après la confirmation du premier facteur et **avant** la redirection vers la connexion (bouton « J'ai noté mes codes ») ; page fermée ⇒ codes perdus, régénérables depuis le profil. Affichage `XXXX-XXXX-XXXX-XXXX` ; à la saisie, espaces et tirets ignorés, casse ignorée ; l'empreinte SHA-256 porte sur la forme normalisée.
- **P-T11-5 Page `/auth/password/mfa`** : un seul champ, qui accepte un code TOTP (6 chiffres) ou un code de secours (16 caractères après normalisation), distingués par la longueur ; chaque échec compte pour l'état (5 essais) et pour le limiteur par compte. Message d'échec unique : « code invalide ou déjà utilisé » (le même code TOTP ne sert pas deux fois, EF-2.3 : juste après l'enrôlement, il faut attendre le code suivant).
- **P-T11-6 Notifications de sécurité** (EF-4.7) : envoyées **à l'adresse validée seulement** (`users.email`) ; pas d'adresse validée ⇒ rien n'est envoyé, ligne de log `INFO`. Vaut pour toutes les notifications.
- **P-T11-7 Gestion depuis le profil** (`/api/me/mfa`, session complète, compte `password`) : `GET` état (TOTP actif, nombre de clés, codes de secours restants) ; `POST totp/setup` (**mot de passe actuel exigé**) ⇒ approvisionnement ; `POST totp/confirm {code}` ; `DELETE totp` (mot de passe + code valide ; refusé pour le dernier facteur d'un admin, EF-2.2) ; `POST recovery-codes` (mot de passe + **preuve de second facteur** : code TOTP valide **ou assertion WebAuthn**) ⇒ nouveaux codes, anciens invalidés ; `DELETE webauthn/<id>` (mot de passe + **preuve de second facteur** : code TOTP, code de secours **ou assertion WebAuthn**). **Assertion WebAuthn au profil** (décision de l'utilisateur, 2026-11-25, matrice de conformité, E-3 b : un compte qui n'a que des clés ne doit plus brûler un code de secours pour retirer une clé, ni être privé de régénération) : `POST /api/me/mfa/webauthn/proof/options` ⇒ défi ≥ 128 bits côté serveur, **dédié au profil** (préfixe `profile:`, distinct des défis de connexion et d'enrôlement), lié au `session_token` de la session, valable 2 min, consommé atomiquement par l'action qui le présente ; l'assertion est vérifiée comme à la connexion (origine, RP ID, compteur, EF-2.4) contre **n'importe quelle clé du compte** (y compris celle qu'on retire) ; un échec compte comme un échec de second facteur (même limiteur par compte, EF-2.6). Assertion refusée ⇒ **403** (jamais 401 : le front lit un 401 comme une session expirée et efface alors les points du constructeur, TR6-n1 ; décision de coord, 2026-11-25). Les autres actions du profil (changement de mot de passe EF-1.7, changement d'adresse EF-4.6, `DELETE totp`) **n'acceptent pas** l'assertion : elles gardent leur preuve actuelle, code TOTP ou code de secours (décision de coord, 2026-11-25, à relire par l'utilisateur : une clé prouve la possession, pas la connaissance ; le mot de passe actuel reste exigé partout). Conséquence assumée : un compte qui n'a que des clés utilise un code de secours pour changer de mot de passe ou d'adresse, puis peut régénérer ses codes avec une assertion. Vérification du mot de passe par le limiteur puis le sémaphore (EF-1.5). Motif : une session volée ne suffit ni à ajouter ni à retirer un facteur.

**T14 (API admin)**
- **P-T14-1 `/api/users` existante** : une seule couche de service ; `/api/admin/users*` est la route de référence, `/api/users*` reste un **alias** qui appelle les mêmes fonctions (mêmes contrôles B5, dernier admin, audit) jusqu'à ce que T15 bascule `users.js`, puis disparaît (choix noté dans la PR, §8). Le test d'autorisation paramétré couvre les deux.
- **P-T14-2 Suppression d'un compte** : `DELETE /api/admin/users/<id>` (absente du tableau §8) ; comportement existant conservé (soi-même refusé en 409, invitations en attente de l'émetteur révoquées, données et fichiers supprimés, états intermédiaires détruits) ; dernier admin actif refusé en 409 (EF-8.2) ; audit.
- **P-T14-3 `PATCH /api/admin/users/<id>`** : `{is_admin?, is_active?, invite_credits?}`, lot tout ou rien. `invite_credits` = **valeur absolue** 0–1000, un seul `UPDATE`, audit ancienne → nouvelle valeur ; une invitation créée au même instant par ce compte peut être « écrasée » (la valeur de l'admin l'emporte) : accepté. Retirer ses propres droits reste refusé (409, comportement existant).
- **P-T14-4 Actions sur soi-même** : `mfa-reset` sur son propre compte refusé (409 : EF-2.7 parle d'un **autre** admin) ; `revoke-sessions` sur soi révoque toutes ses sessions, la courante comprise (reconnexion).
- **P-T14-5 Fonctions bêta** : `GET /api/admin/features` ⇒ `[{key, label, state, users: [{id, username}], diagnostic}]`, avec pour `route_builder` le diagnostic de la spec routage §15 P-TR5-9. `PATCH /api/admin/features/<key> {state?, users?}` : `users` = **liste complète** des ids (remplace la précédente), 1 000 au plus, chaque id doit exister (sinon 400), compte inactif accepté ; liste conservée quel que soit l'état (EF-7.2) ; audit.
- **P-T14-6 Contrat de `GET /api/admin/settings`** (pour une UI générique, T15) : par réglage, `key`, `section`, `label`, `help`, `type`, bornes (`min`, `max`, `max_length`, `choices`, `max_items`), `default`, nom de la variable d'env, valeur effective, `source` (`base` / `env` / `défaut`), surcharge d'env (valeur masquée pour un secret), et pour une destination la liste blanche du service (EF-6.8). `PATCH /api/admin/settings {clé: valeur, …}` en lot ; réponse `{success, changed}` (liste des clés écrites), **sans** la version des réglages (décision de coord, 2026-11-25, à relire par l'utilisateur, E-19 : accepté, l'UI n'en a pas besoin ; la version reste incrémentée en base, EF-6.4).
- **P-T14-7 Secrets** : `PUT /api/admin/secrets/<name> {value}` : 1 à 1 024 caractères, sans CR, LF ni NUL, enregistré tel quel ; destination liée vide ou hors liste blanche ⇒ 409 « configure d'abord la destination ». `GET` ⇒ `name`, `configured`, date, auteur, `bound_to`, `last4` (type `api_key` seulement), et un booléen « destination liée = destination effective ».
- **P-T14-8 Journal** : `GET /api/admin/audit?limit=&before_id=&actor_type=&action=&target_type=&target_id=&since=&until=`, `limit` 1–200 (défaut 50), tri par id décroissant, réponse avec `next_before_id`.
- **P-T14-9 E-mail de test** (EF-4.2) : vers l'adresse validée de l'admin, **sinon son adresse en attente** ; sans l'un ni l'autre ⇒ 409. Motif : l'admin amorcé n'a pas d'adresse validée tant que T13 et le SMTP ne sont pas en place, et le test sert justement à régler le SMTP.
- **P-T14-10 EF-3.2 et EF-4.5** : un **admin** invite même sans adresse validée (comportement de `invitations.can_invite`) ; la restriction « pas d'invitation » d'EF-4.5 vise les non-admins. Clarification, sans changement de code.

**T15 (UI admin et profil)**
- **P-T15-1 Page** : `/admin` (gabarit et JS dédiés, `admin_required`, `no-store`), onglets de §9 repérés par l'ancre de l'URL ; lien « Administration » dans la barre de navigation pour les admins seulement ; `/users` redirige vers `/admin#utilisateurs` quand l'onglet couvre l'existant.
- **P-T15-2 Ordre de livraison proposé** (pour une recette au plus tôt) : Utilisateurs (activation, promotion, crédits) → Invitations → Fonctions bêta (état, choix des comptes, diagnostic du moteur) → Réglages (formulaire générique, P-T14-6) → Clés API et E-mail (saisie seule, « Tester », e-mail de test) → Journal. Profil : Invitations (crédits restants, avertissement « crédit non rendu » avant création), Second facteur (P-T11-7), Mot de passe, Adresse.
- **P-T15-3 Rendu** : DOM et `textContent` seulement, jamais `innerHTML` avec une donnée ; aucun script ni style en ligne (CSP) ; confirmations in-page, jamais `confirm()`.
- **P-T15-4 Lien d'invitation et codes de secours** : affichés une fois, bouton « Copier » (`navigator.clipboard`, sinon champ présélectionné) ; jamais écrits en `localStorage`.
- **P-T15-5 Réglages** : **remplacé par §5.6b (2026-11-25)** ; ce qui suit reste vrai. Champ selon le type, bornes en attributs (`min`, `max`, `maxlength`), le serveur restant l'autorité ; badge de source ; « Revenir au défaut » = `DELETE` ; avant d'enregistrer une nouvelle destination (`smtp.host`, `routing.base_url`…), avertissement « le secret associé sera effacé » (EF-5.5).
