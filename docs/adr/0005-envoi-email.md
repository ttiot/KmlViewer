# ADR-0005 — Envoi d'e-mails : SMTP via la bibliothèque standard

Statut : **Accepté** (Q4/Q5 et 2e vague ; compléments post-review B3, M2, M12 relayés par coord le 2026-11-24) · Spec : [admin-space](../specs/admin-space.md) §5.4

## Contexte
Validation d'adresse, invitation, réinitialisation, notifications de sécurité. Volume faible. La review (M2) a relevé qu'un envoi synchrone dans le seul cas « compte existant » du mot de passe oublié en fait un oracle temporel.

## Décision
- `smtplib` + `email.message.EmailMessage` (bibliothèque standard) dans `app/mailer.py`.
- Sécurité (B3) : certificat toujours vérifié ; `starttls` sans repli en clair ; en `none`, **aucune authentification envoyée** (identifiant et mot de passe refusés dans ce mode). Hôte soumis à `OUTBOUND_ALLOWED_HOSTS`, mot de passe lié à l'hôte et au port (ADR-0011).
- Deux modes d'envoi (M2) :
  - **synchrone** pour l'invitation et l'e-mail de test (l'appelant a besoin du résultat) ;
  - **file en mémoire** traitée par un thread dédié pour tout le reste ; aucune requête publique n'attend un envoi. File bornée, perte au redémarrage journalisée ;
  - **en CLI**, pas de thread : les commandes qui notifient (réinitialisation de facteur, `secrets-reset`…) envoient de façon synchrone et sortent en erreur si l'envoi échoue, pour ne jamais perdre une alerte de sécurité (v5-3).
- E-mail de test : réponse ok/ko seulement, durée constante (B3).
- Liens construits sur `PUBLIC_URL` (M12).

## Conséquences
- Le mot de passe oublié répond en temps constant, l'envoi étant hors du thread de la requête.
- Sans SMTP, l'appli fonctionne ; les adresses restent « en attente ».

## Options écartées
- Tout synchrone (décision initiale) : oracle temporel sur le mot de passe oublié.
- File persistante en base : plus robuste aux redémarrages, plus de code ; à reconsidérer si le volume le justifie.
- Service d'envoi tiers.
