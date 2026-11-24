// Vérification du second facteur. Aucune donnée du serveur n'est insérée autrement que par textContent.
'use strict';

(() => {
    const csrf = document.querySelector('meta[name="csrf-token"]').content;
    const form = document.getElementById('mfaForm');
    const message = document.getElementById('mfaMessage');
    const submit = document.getElementById('mfaSubmit');

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        message.textContent = '';
        submit.disabled = true;
        try {
            const response = await fetch('/auth/password/mfa', {
                method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
                body: JSON.stringify({ code: document.getElementById('mfaCode').value.trim() }),
            });
            let data = {};
            try { data = await response.json(); } catch (_) { /* corps non JSON */ }
            if (response.ok && data.success && typeof data.next === 'string' && data.next.startsWith('/')) {
                window.location.assign(data.next);
                return;
            }
            document.getElementById('mfaCode').value = '';
            message.textContent = data.error || 'La vérification a échoué.';
            if (data.restart) { window.setTimeout(() => window.location.assign('/auth/password/login'), 1500); }
        } catch (_) {
            message.textContent = 'Impossible de contacter le serveur.';
        } finally {
            submit.disabled = false;
        }
    });
})();
