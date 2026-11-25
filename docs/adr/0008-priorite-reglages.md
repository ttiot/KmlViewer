# ADR-0008 — Priorité des réglages : base > environnement > défaut

Statut : **Accepté** (arbitrage utilisateur Q9, 2e vague ; garde-fous complétés en 3e vague, post-review B3, M7, M10 et re-review R1, R3, relayées par coord le 2026-11-24 ; inverse de la recommandation initiale de la spec) · Spec : [admin-space](../specs/admin-space.md) EF-6.1 à EF-6.8, EF-5.5

## Contexte
Des réglages aujourd'hui en env deviennent modifiables depuis l'admin. La spec recommandait env > base (l'opérateur garde la main) ; l'utilisateur a retenu l'inverse : l'admin de l'appli a le dernier mot.

## Décision
- Valeur effective = **valeur en base** si définie > variable d'env > défaut du registre.
- L'env devient valeur initiale et de repli. Tout réglage **du registre** est modifiable dans l'UI.
- « Revenir à la valeur par défaut » = supprimer la ligne en base ; l'env (ou le défaut) redevient effectif.
- Les secrets suivent la même règle (secret en base > secret en env).
  - **Amendement du 2026-11-25** (décision de coord, 2026-11-25, à relire par l'utilisateur, matrice de conformité E-17) : en v1, **les secrets ne viennent que de la base** ; aucune variable d'env de secret n'est lue. La règle ci-dessus reste le cadre si un secret d'env est ajouté un jour (avec la liaison à la destination d'env d'ADR-0011).

## Garde-fous rendus nécessaires par ce choix
- **Frontière stricte** : les réglages d'amorçage et de sécurité (`SECRET_KEY`, `SECRETS_KEY`, `AUTH_MODE`, `OIDC_*`, `PUBLIC_URL`, `WEBAUTHN_RP_ID`, `OUTBOUND_ALLOWED_HOSTS`, `TRUSTED_HOSTS`, `TRUST_PROXY*`, `SESSION_*`, `DATA_DIR`, `DB_PATH`, `LOG_LEVEL`) et les bornes anti-abus **ne sont pas dans le registre** : ni stockables en base, ni modifiables dans l'UI. Tout ajout au registre touchant l'authentification, les sessions, les limites dures ou une destination sortante passe par une revue sécurité.
- **Destinations sortantes** (B3) : toute URL ou tout hôte vers lequel partent des données d'utilisateurs ou un secret (`routing.base_url`, `DEM_URL`, `smtp.host`, hôtes des fonds de carte) reste dans le registre **seulement** sous la liste blanche `OUTBOUND_ALLOWED_HOSTS` et avec des secrets liés à leur destination (ADR-0011).
- **`SECRETS_KEY` reste exclusivement en env** : elle déchiffre la base, elle ne peut ni y être stockée ni être surchargée par elle.
- **Bornes obligatoires, basse et haute** (M7) pour chaque entrée du registre (test sur le registre) : une valeur énorme désactive une protection aussi sûrement qu'une valeur nulle. `auth.mfa_policy` jamais en dessous de `admins`, seuils du limiteur bornés des deux côtés, rétention d'audit ≥ 30 j, quotas plafonnés.
- **Prise en compte à chaud** (M10, R3) : numéro de version des réglages en base, incrémenté par toute écriture (UI, API, **CLI**) ; le serveur web le relit à chaque requête, l'ordonnanceur de fond à chaque tour. L'ordonnanceur démarrant toujours en mode servi, **aucun réglage v1 n'exige de redémarrage** (`INBOX_ENABLED` et `DEM_URL` compris).
- **Fonds de carte** (R1) : leurs URL partent vers le navigateur de tous les utilisateurs ; ils restent dans le registre seulement sous les entrées `tiles=` de la liste blanche d'env (ADR-0011), qui fixent aussi la CSP `img-src`.
- **Diagnostic** :
  - UI : source de chaque valeur (`base` / `env` / `défaut`) et marqueur « surcharge `VAR` » avec la valeur d'env ignorée (masquée pour un secret) ;
  - démarrage : une ligne `WARNING` par variable d'env surchargée (nom seul) ;
  - `GET /api/admin/settings/diagnostic` et `flask settings-list` ;
  - secours hors UI : `flask settings-reset <clé>|--all` (tracé dans l'audit).
- Une valeur d'env invalide est ignorée (défaut appliqué) et signalée ; elle ne bloque **jamais** le démarrage (aucun réglage `critical` en v1, arbitré).

## Conséquences
- Modifier `.env` (ou l'inventaire Ansible) n'a **aucun effet** sur un réglage surchargé en base : c'est le principal piège d'exploitation, d'où le diagnostic au démarrage et la mention en tête de `.env.example`.
- Une session admin volée peut modifier tous les réglages du registre : la frontière ci-dessus et l'audit en limitent la portée.
- Les sauvegardes de la base portent désormais une partie de la configuration effective.

## Option écartée
- Env > base (recommandation initiale) : l'opérateur garde la main et une valeur en base ne survit pas à un changement d'env ; écartée par l'utilisateur au profit de l'administration dans l'appli. Un verrouillage sélectif par l'opérateur (`SETTINGS_LOCKED`) est écarté en v1 (arbitré) ; il pourra être ajouté sans changer cette règle si le besoin apparaît.
