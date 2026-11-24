# ADR-0003 — Secrets au repos : Fernet et `SECRETS_KEY` dédiée

Statut : **Accepté** (2e vague ; rotation précisée post-review M15, relayée par coord le 2026-11-24) · Spec : [admin-space](../specs/admin-space.md) EF-5, EF-2.3

## Contexte
L'admin saisit des secrets (mot de passe SMTP, clés de services) et l'appli stocke des secrets TOTP. Ils vivent en SQLite, copiée par `flask backup`. Ils doivent être illisibles sans une clé absente de la base et des sauvegardes.

## Décision
- Chiffrement authentifié **Fernet** (`cryptography`, à déclarer en dépendance directe).
- Clé maîtresse `SECRETS_KEY` en env, **distincte de `SECRET_KEY`**, jamais en base.
- **Rotation** (M15) : `SECRETS_KEY` accepte une liste de clés Fernet séparées par des virgules ; la première chiffre, toutes déchiffrent (`MultiFernet`). Procédure : nouvelle clé en tête, redémarrage, `flask secrets-rotate` (rechiffre secrets **et** secrets TOTP avec la première clé, ligne par ligne en transaction, rapport et audit), retrait de l'ancienne clé, redémarrage.
- Démarrage refusé si une donnée chiffrée n'est déchiffrable par aucune clé fournie. Perte de toutes les clés : `flask secrets-reset` (efface secrets et TOTP, rétrograde les admins `password` qui n'ont plus de facteur).
- Écriture seule ; les 4 derniers caractères ne sont conservés que pour les secrets de type `api_key`, **jamais pour un mot de passe** (n3).
- Chaque secret est **lié à sa destination** (ADR-0011) : il n'est envoyé qu'à elle et est effacé si elle change.

## Conséquences
- Une variable d'env à sauvegarder **hors** des sauvegardes de la base ; procédure de rotation à documenter (ops).
- En mode `password`, `SECRETS_KEY` est obligatoire (secrets TOTP).

## Options écartées
- Clé dérivée de `SECRET_KEY` : faire tourner `SECRET_KEY` rendrait tous les secrets illisibles.
- Secrets uniquement en env : contraire à la demande ; un secret d'env reste accepté en repli (base prioritaire, ADR-0008), envoyé seulement à la destination définie en env.
- Coffre externe : surdimensionné ; l'interface `secrets_store` permettra un adaptateur plus tard.
