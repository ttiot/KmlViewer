'use strict';

// Page Profil : mesures facultatives et type par défaut à l'import. Les valeurs initiales
// sont rendues par le serveur ; seuls les champs modifiés sont envoyés. La validation qui
// fait foi est celle du serveur (app/services/profile_service.py).
(() => {
    const { api, notify } = App;
    const INTEGER_FIELDS = ['height_cm', 'hr_max', 'hr_rest'];

    // Valeur d'un champ telle que l'API l'attend ; null pour un champ vide
    function fieldValue(input) {
        const raw = input.value.trim();
        if (raw === '') return null;
        if (INTEGER_FIELDS.includes(input.name)) {
            if (!/^\d+$/.test(raw)) throw new Error(`${labelOf(input)} : nombre entier attendu`);
            return Number(raw);
        }
        if (input.name === 'weight_kg') {
            const value = Number(raw.replace(',', '.'));
            if (!Number.isFinite(value)) throw new Error(`${labelOf(input)} : nombre attendu`);
            return value;
        }
        return raw;
    }

    function labelOf(input) {
        const label = document.querySelector(`label[for="${input.id}"]`);
        return label ? label.textContent : input.name;
    }

    const fields = (form) => Array.from(form.elements).filter((input) => input.name);

    // Les valeurs enregistrées deviennent les valeurs par défaut du formulaire : « Annuler »
    // (type=reset) y revient, et un nouvel envoi ne porte que ce qui a changé depuis.
    function commit(form) {
        fields(form).forEach((input) => {
            if (input instanceof HTMLSelectElement) {
                Array.from(input.options).forEach((option) => { option.defaultSelected = option.selected; });
            } else {
                input.defaultValue = input.value;
            }
        });
    }

    function changed(input) {
        if (input instanceof HTMLSelectElement) {
            return Array.from(input.options).some((option) => option.selected !== option.defaultSelected);
        }
        return input.value !== input.defaultValue;
    }

    async function save(form) {
        let body;
        try {
            body = Object.fromEntries(fields(form).filter(changed).map((input) => [input.name, fieldValue(input)]));
        } catch (error) {
            notify(error.message, 'warning');
            return;
        }
        if (Object.keys(body).length === 0) {
            notify('Aucune modification à enregistrer.');
            return;
        }
        const button = document.getElementById('saveProfileButton');
        button.disabled = true;
        try {
            await api('/api/me/profile', { method: 'PATCH', body });
            commit(form);
            notify('Profil enregistré.', 'success');
        } catch (error) {
            notify(error.message, 'danger');
        } finally {
            button.disabled = false;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        const form = document.getElementById('profileForm');
        form.addEventListener('submit', (event) => {
            event.preventDefault();
            save(form);
        });
    });
})();
