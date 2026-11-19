// Visualiseur : préférences d'affichage mémorisées dans le navigateur (fond de carte,
// unités, enregistrement en bibliothèque). Rien de sensible n'y est écrit.
// Une valeur relue n'est acceptée que si elle figure dans la liste blanche fournie ;
// stockage indisponible (navigation privée) ou valeur inattendue : null, et l'appelant
// garde son réglage par défaut.
'use strict';

const Prefs = {
    read(key, allowed) {
        try {
            const value = window.localStorage.getItem(key);
            return allowed.includes(value) ? value : null;
        } catch (_) {
            return null;
        }
    },
    write(key, value) {
        try { window.localStorage.setItem(key, value); } catch (_) { /* choix non mémorisé */ }
    },
};
