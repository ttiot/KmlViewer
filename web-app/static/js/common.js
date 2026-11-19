// Utilitaires partagés par les pages bibliothèque.
// Règle : toute donnée venant du serveur est insérée via textContent, jamais innerHTML.
'use strict';

const App = (() => {
    // Requête vers l'API. Gère ce qui est commun à tous les appels : session expirée
    // (retour au login, puis à la page en cours, paramètres compris) et réponse en erreur.
    // Renvoie la réponse brute : `api` la lit en JSON, l'export la lit en fichier.
    async function request(path, options = {}) {
        const init = { credentials: 'same-origin', ...options };
        if (init.body && !(init.body instanceof FormData)) {
            init.headers = { 'Content-Type': 'application/json', ...(init.headers || {}) };
            init.body = JSON.stringify(init.body);
        }
        const response = await fetch(path, init);
        if (response.status === 401) {
            const here = window.location.pathname + window.location.search;
            window.location.assign('/auth/login?next=' + encodeURIComponent(here));
            throw new Error('Session expirée');
        }
        if (!response.ok) {
            let data = null;
            try { data = await response.json(); } catch (_) { /* réponse non JSON */ }
            throw new Error((data && data.error) || `Erreur ${response.status}`);
        }
        return response;
    }

    // Appel JSON. Une réponse réussie mais illisible (corps vide, JSON invalide) est une
    // erreur : la renvoyer telle quelle ferait planter l'appelant plus loin, sans message utile.
    async function api(path, options = {}) {
        const response = await request(path, options);
        try {
            return await response.json();
        } catch (_) {
            throw new Error('Réponse illisible du serveur');
        }
    }

    // Crée un élément DOM ; les enfants textuels passent par des nœuds texte.
    function el(tag, attrs = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
            if (value === null || value === undefined || value === false) continue;
            if (key === 'class') node.className = value;
            else if (key === 'dataset') Object.assign(node.dataset, value);
            else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
            else node.setAttribute(key, value === true ? '' : value);
        }
        for (const child of children.flat()) {
            if (child === null || child === undefined) continue;
            node.append(child instanceof Node ? child : document.createTextNode(String(child)));
        }
        return node;
    }

    function icon(name) { return el('i', { class: `fas fa-${name}` }); }

    function notify(message, type = 'info') {
        const box = document.getElementById('pageAlert');
        if (!box) return;
        box.className = `alert alert-${type}`;
        box.textContent = message;
        clearTimeout(notify.timer);
        notify.timer = setTimeout(() => box.classList.add('d-none'), 6000);
    }

    const number = (value, digits = 0) =>
        Number(value || 0).toLocaleString('fr-FR', { minimumFractionDigits: digits, maximumFractionDigits: digits });

    // Durée lisible ; `empty` est rendu pour une durée inconnue.
    function duration(seconds, empty = '—') {
        if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return empty;
        const s = Math.round(Number(seconds));
        if (s < 60) return `${s} s`;
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        const pad = (v) => String(v).padStart(2, '0');
        return h > 0 ? `${h} h ${pad(m)} min` : `${m} min ${pad(sec)} s`;
    }

    function chrono(seconds) {
        const s = Math.round(seconds);
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
        const pad = (v) => String(v).padStart(2, '0');
        return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
    }

    // En mètres sous le kilomètre (tronçon court, périmètre d'un polygone), en km au-delà
    const distance = (meters) => (Math.abs(meters) < 1000 ? `${number(meters)} m` : `${number(meters / 1000, 1)} km`);
    const speed = (meters, seconds) => (seconds > 0 ? `${number(meters / seconds * 3.6, 1)} km/h` : '—');
    const date = (iso) => (iso ? new Date(iso).toLocaleDateString('fr-FR', { day: '2-digit', month: 'short', year: 'numeric' }) : '—');
    const dateTime = (iso) => (iso ? new Date(iso).toLocaleString('fr-FR', { dateStyle: 'short', timeStyle: 'short' }) : '—');

    // Grand chiffre suivi de son unité en petit : setFigure(node, '302,4', { unit: 'km' }).
    // Les textes passent par des nœuds texte ; une unité devient un <span class="unit">.
    function setFigure(node, ...parts) {
        const children = [];
        parts.forEach((part, index) => {
            if (index) children.push(' ');
            children.push(part && typeof part === 'object' ? el('span', { class: 'unit' }, part.unit) : String(part));
        });
        node.replaceChildren(...children);
        return node;
    }

    // Durée en morceaux pour setFigure : « 7 h 00 », « 42 min », « 35 s » ; null si inconnue
    function durationParts(seconds) {
        if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return null;
        const s = Math.round(Number(seconds));
        if (s < 60) return [s, { unit: 's' }];
        const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
        return h > 0 ? [h, { unit: 'h' }, String(m).padStart(2, '0')] : [m, { unit: 'min' }];
    }

    // Libellé d'un type d'activité, lu dans une liste déroulante rendue par le serveur
    // (le catalogue des types n'existe qu'à un endroit : app/services/activity_types.py)
    function typeLabels(select) {
        const labels = {};
        Array.from(select ? select.options : []).forEach((option) => {
            if (option.value) labels[option.value] = option.textContent;
        });
        return labels;
    }

    return { request, api, el, icon, notify, number, duration, chrono, distance, speed, date, dateTime,
             setFigure, durationParts, typeLabels };
})();
