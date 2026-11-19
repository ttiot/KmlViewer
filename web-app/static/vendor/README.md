# Bibliothèques front embarquées

Servies depuis l'application : aucune dépendance à un CDN, et la CSP se limite à `'self'`.
Fichiers copiés tels quels depuis les distributions officielles, empreintes dans `SHA256SUMS`
(vérification : `sha256sum -c SHA256SUMS` depuis ce dossier).

| Dossier | Bibliothèque | Version | Source |
|---------|--------------|---------|--------|
| `leaflet/` | Leaflet | 1.9.4 | `https://unpkg.com/leaflet@1.9.4/dist/` |
| `bootstrap/` | Bootstrap | 5.3.0 | `https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/` |
| `chartjs/` | Chart.js | 4.4.1 | `https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/` |
| `fontawesome/` | Font Awesome Free | 6.4.0 | `https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/` |
| `fonts/` | Barlow, Barlow Condensed (sous-ensemble latin) | Fontsource 5.0.13 | `https://cdn.jsdelivr.net/npm/@fontsource/barlow@5.0.13/files/`, `…/@fontsource/barlow-condensed@5.0.13/files/` |
| `fonts/` | JetBrains Mono (sous-ensemble latin) | Fontsource 5.0.18 | `https://cdn.jsdelivr.net/npm/@fontsource/jetbrains-mono@5.0.18/files/` |

Les polices sont sous licence SIL Open Font License 1.1 ; elles sont déclarées (`@font-face`) en
tête de `static/css/theme.css`.

Pour mettre à jour une bibliothèque : remplacer ses fichiers, régénérer `SHA256SUMS`
(`find . -type f ! -name SHA256SUMS ! -name README.md | sort | xargs sha256sum > SHA256SUMS`)
et mettre à jour ce tableau.
