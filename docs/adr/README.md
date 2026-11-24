# Décisions d'architecture (ADR)

Format : contexte, décision, conséquences, options écartées. Statuts : **Proposé** (recommandation de la spec, à accepter), **Accepté** (arbitré par l'utilisateur), **En attente**, **Remplacé**.

| # | Sujet | Statut | Spec |
|---|---|---|---|
| [0001](0001-modes-authentification.md) | Modes d'authentification : ajout de `password` ; mode validé seule source de vérité du garde ; `PUBLIC_URL` | Accepté | admin-space |
| [0002](0002-hachage-mots-de-passe.md) | Hachage des mots de passe : argon2id | Accepté | admin-space |
| [0003](0003-chiffrement-secrets.md) | Secrets au repos : Fernet, `SECRETS_KEY` multi-clés et rotation | Accepté | admin-space |
| [0004](0004-second-facteur.md) | Second facteur : TOTP + WebAuthn, état serveur, invariant admin | Accepté | admin-space |
| [0005](0005-envoi-email.md) | E-mail : SMTP standard, file d'envoi hors requête | Accepté | admin-space |
| [0006](0006-limiteur-debit.md) | Limiteur de débit en mémoire, clés sur l'identifiant saisi, compteurs à mémoire constante | Accepté | admin-space, gpx-route-builder |
| [0007](0007-moteur-routage.md) | Moteur de routage : GraphHopper auto-hébergé | Accepté | gpx-route-builder |
| [0008](0008-priorite-reglages.md) | Priorité des réglages : base > env, bornes, version en base | Accepté | admin-space |
| [0009](0009-itineraires-planifies.md) | Traces planifiées : `activities.kind = 'planned'` | Accepté | gpx-route-builder |
| [0010](0010-strategie-detours.md) | Stratégie de détours : hybride S1 puis pétale S2, essais de graine | Accepté | gpx-route-builder |
| [0011](0011-destinations-sortantes.md) | Destinations sortantes en liste blanche par service (fonds de carte et CSP compris), secrets liés à leur destination | Accepté | admin-space, gpx-route-builder |
| [0012](0012-migrations-par-nom.md) | Migrations de schéma repérées par nom | Accepté | admin-space, gpx-route-builder |
