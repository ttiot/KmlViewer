// Thème clair / sombre. Chargé dans <head> (bloquant et minuscule) pour poser l'attribut
// avant le premier rendu et éviter tout flash. Le choix de l'utilisateur prime sur le système.
'use strict';

const AppTheme = (() => {
    const KEY = 'kmlviewer-theme';
    const media = window.matchMedia('(prefers-color-scheme: dark)');

    function stored() {
        try {
            const value = window.localStorage.getItem(KEY);
            return value === 'dark' || value === 'light' ? value : null;
        } catch (_) {
            return null; // stockage indisponible (navigation privée) : on suit le système
        }
    }

    const current = () => document.documentElement.getAttribute('data-bs-theme') || 'light';

    function apply(theme) {
        document.documentElement.setAttribute('data-bs-theme', theme);
    }

    // Valeur d'un jeton CSS (ex. '--viz-1') dans le thème courant
    function color(token) {
        return getComputedStyle(document.documentElement).getPropertyValue(token).trim();
    }

    // Les graphiques lisent leurs couleurs dans les jetons : grille et axes en retrait,
    // texte dans les couleurs de texte, jamais dans la couleur de la série.
    function applyChartDefaults() {
        if (typeof Chart === 'undefined') return;
        Chart.defaults.color = color('--text-2');
        Chart.defaults.borderColor = color('--viz-grid');
        Chart.defaults.font.family = color('--font') || 'system-ui, sans-serif';
        Chart.defaults.font.size = 12;
        Object.assign(Chart.defaults.plugins.tooltip, {
            backgroundColor: color('--text'),
            titleColor: color('--surface'),
            bodyColor: color('--surface'),
            padding: 10,
            cornerRadius: 8,
            displayColors: false,
        });
    }

    function refresh() {
        applyChartDefaults();
        const icon = document.querySelector('#themeToggle i');
        if (icon) icon.className = current() === 'dark' ? 'fas fa-sun' : 'fas fa-moon';
        document.dispatchEvent(new CustomEvent('themechange', { detail: current() }));
    }

    function toggle() {
        const next = current() === 'dark' ? 'light' : 'dark';
        try { window.localStorage.setItem(KEY, next); } catch (_) { /* choix non mémorisé */ }
        apply(next);
        refresh();
    }

    apply(stored() || (media.matches ? 'dark' : 'light'));

    media.addEventListener('change', (event) => {
        if (stored()) return;
        apply(event.matches ? 'dark' : 'light');
        refresh();
    });

    document.addEventListener('DOMContentLoaded', () => {
        const button = document.getElementById('themeToggle');
        if (button) button.addEventListener('click', toggle);
        refresh();
    });

    return { current, color, toggle, applyChartDefaults };
})();
