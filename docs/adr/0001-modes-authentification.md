# ADR-0001 — Modes d'authentification : ajout de `password`

Statut : **Accepté** (arbitrage utilisateur Q1 ; compléments post-review B1, M4, M12 relayés par coord le 2026-11-24) · Spec : [admin-space](../specs/admin-space.md) §5.0

## Contexte
Deux modes existent (`web-app/app/auth/checks.py`) : `oidc` (SSO obligatoire) et `local` (mono-utilisateur sans authentification). Sans IdP, une instance partagée n'a aucune protection. L'utilisateur veut des comptes à mot de passe, sur invitation, y compris sans OIDC.

Constat de la review (B1) : le garde actuel (`guard.py`) déduit le mode de `bool(OIDC_ISSUER)` et, s'il est faux, donne le compte `local` **admin** à chaque visiteur. Le mode `password` étant défini *sans* `OIDC_ISSUER`, l'ajouter à `checks.py` sans toucher au garde ouvrirait l'instance.

## Décision
- Nouveau mode explicite `AUTH_MODE=password` : identifiant + mot de passe + second facteur (ADR-0004), comptes créés uniquement par invitation.
- **Le mode validé (`config['AUTH_MODE']`) est la seule source de vérité** : garde, routes d'authentification et gabarits passent par `auth_mode()` ; plus aucune décision sur la présence d'`OIDC_ISSUER`. **Seul `local`** crée ou charge le compte `local`.
- Le garde refuse toute session dont le compte n'a pas une méthode d'authentification permise par le mode courant (`password` ⇒ `password` ; `oidc` ⇒ `oidc` et `password` ; `local` ⇒ `none`) (M4).
- En mode `oidc`, comptes à mot de passe actifs dès que `SECRETS_KEY` et `PUBLIC_URL` sont présentes.
- `PUBLIC_URL` (schéma, hôte, port) : source unique des liens et du RP ID WebAuthn ; obligatoire en mode `password`, couverte par `TRUSTED_HOSTS` (M12).
- Démarrage refusé en `password` si : `OIDC_ISSUER` posé, `SECRET_KEY` faible ou par défaut, `TRUSTED_HOSTS` vide, `SECRETS_KEY` absente, `PUBLIC_URL` absente ou non couverte.
- Premier admin : `flask user-invite --admin <email>` (lien d'amorçage 24 h, enrôlement d'un second facteur imposé avant toute session).
- Le mode déduit (variable vide) ne choisit jamais `password`.

## Conséquences
- `checks.py`, `guard.py` (refonte : mode, méthode permise, état `mfa_pending` serveur), context processor et `PUBLIC_ENDPOINTS` évoluent ; test de non-régression : en mode `password`, aucune route hors liste blanche n'est servie sans session et aucun compte `local` n'est créé.
- Une instance `oidc` existante démarre toujours après mise à jour.
- Bascule `oidc` → `password` : les sessions SSO en cours sont coupées ; les comptes SSO sont convertis par un admin (`sub` réécrit, admin retiré jusqu'à enrôlement d'un facteur).

## Options écartées
- Mot de passe ajouté au mode `local` : ce mode est par définition sans authentification.
- Mode déduit de la présence de comptes à mot de passe ou d'`OIDC_ISSUER` : un réglage perdu changerait silencieusement le mode d'authentification (c'est précisément le défaut relevé en B1).
