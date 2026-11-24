# ADR-0009 — Traces planifiées : colonne `activities.kind`

Statut : **Accepté** (principe : arbitrage Q-R5 ; forme et vocabulaire : 2e vague relayée par coord le 2026-11-24) · Spec : [gpx-route-builder](../specs/gpx-route-builder.md) §5.5

> Nom de fichier conservé pour la stabilité des liens ; le terme « itinéraire » est abandonné dans l'UI au profit de « trace planifiée ».

## Contexte
Une trace générée doit pouvoir être enregistrée et rouverte comme une trace, sans compter dans les statistiques, records et efforts sur tronçons. Toute la chaîne (stockage, quota, DEM, affichage, analyse, export) est construite autour de la table `activities`.

## Décision
- Colonne `activities.kind TEXT NOT NULL DEFAULT 'activity'`, valeurs `activity` | `planned` ; index `(user_id, kind, start_time)`.
- Seul le générateur pose `planned` ; tout import (upload, inbox) reste `activity`.
- Statistiques, records, types, compteurs et tronçons filtrent **explicitement** sur `kind = 'activity'` ; la liste et le rattrapage d'altitude incluent les deux.
- Liste de la bibliothèque : traces planifiées **visibles par défaut** (filtre « Toutes »), badge **Planifiée**.
- `kind` non modifiable par l'API en v1.

## Conséquences
- Migration simple (`ADD COLUMN` avec défaut), aucune donnée existante modifiée.
- Chaque nouvelle requête de statistiques doit penser au filtre : couvert par un test qui enregistre une trace planifiée et vérifie que les agrégats ne bougent pas.
- `start_time` reste obligatoire : date d'enregistrement avec `start_time_known=0`.

## Options écartées
- Table séparée : isolation parfaite des statistiques, mais duplication du stockage, du quota, du DEM, de l'affichage et de l'export.
- Booléen `is_planned` : moins extensible.
- Exclure les traces sans horodatage : un vrai GPX sans temps n'est pas une trace planifiée ; critère implicite et fragile.
