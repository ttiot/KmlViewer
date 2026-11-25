// Sécurité du compte (mot de passe, adresse, second facteur ; les invitations sont dans profile-invitations.js). Données du serveur : textContent seulement ; le QR code
// (SVG produit par le serveur) est inséré via DOMParser dans un nœud dédié ; les codes de secours ne vont jamais dans le stockage du navigateur.
'use strict';

(() => {
    const { api, el, notify } = App;
    const $ = (id) => document.getElementById(id);

    async function act(promise, done) {
        try { const result = await promise; if (done) done(result); return result; } catch (error) { notify(error.message, 'danger'); return null; }
    }
    const post = (path, body, method = 'POST') => api(path, { method, body });

    function showOnce(text, value) {
        $('secOneShotText').textContent = text;
        $('secOneShotValue').textContent = value;
        $('secOneShot').classList.remove('d-none');
    }
    $('secOneShotCopy').addEventListener('click', async () => {
        try { await navigator.clipboard.writeText($('secOneShotValue').textContent); notify('Copié.'); } catch (_) {
            const range = document.createRange(); range.selectNodeContents($('secOneShotValue'));
            const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }
    });
    $('secOneShotClose').addEventListener('click', () => { $('secOneShotValue').textContent = ''; $('secOneShot').classList.add('d-none'); });

    $('passwordForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const body = { current_password: $('pwCurrent').value, new_password: $('pwNew').value };
        if ($('pwCode').value.trim()) body.code = $('pwCode').value.trim();
        await act(post('/api/me/password', body), () => {
            notify('Mot de passe changé ; tes autres sessions sont fermées.');
            ['pwCurrent', 'pwNew', 'pwCode'].forEach((id) => { $(id).value = ''; });
        });
    });

    $('emailForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const body = { email: $('emNew').value.trim(), password: $('emPassword').value };
        if ($('emCode').value.trim()) body.code = $('emCode').value.trim();
        await act(post('/api/me/email', body), () => {
            notify('Un lien de validation est envoyé à la nouvelle adresse ; l’ancienne reste active en attendant.');
            $('emPassword').value = ''; $('emCode').value = '';
        });
    });

    // -- second facteur --------------------------------------------------------------------------------------------------
    async function loadMfa() {
        const data = await act(api('/api/me/mfa'));
        if (!data) return;
        $('mfaState').textContent = `TOTP : ${data.totp ? 'actif' : 'inactif'} · clés de sécurité : ${data.keys} · codes de secours restants : ${data.recovery_codes_remaining}`;
        $('mfaSetup').disabled = data.totp;
        $('mfaRemove').disabled = !data.totp;
        $('mfaRegen').disabled = !(data.totp || data.keys);
        $('mfaKeyAdd').hidden = !(data.webauthn_available && Webauthn.supported());
        $('mfaKeyList').replaceChildren(...data.key_list.map((key) => el('li', { class: 'd-flex gap-2 align-items-center' },
            el('span', {}, key.name || 'Clé de sécurité'), el('span', { class: 'hint' }, key.last_used_at ? `utilisée le ${key.last_used_at}` : 'jamais utilisée'),
            el('button', { type: 'button', class: 'btn btn-sm btn-outline-danger', onclick: async () => {
                const body = await proofBody(); if (!body) return;
                if (await act(post(`/api/me/mfa/webauthn/${key.id}`, body, 'DELETE'), () => notify('Clé retirée.'))) loadMfa();
            } }, 'Retirer'))));
    }
    const creds = () => ({ password: $('mfaPassword').value, code: $('mfaCode').value.trim() });
    // E-3b : sans code saisi, une clé de sécurité du compte prouve le second facteur (retrait d'une clé, codes de secours)
    async function keyProof() {
        const first = await fetch('/api/me/mfa/webauthn/proof/options', { method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: $('mfaPassword').value }) });
        const options = await first.json();
        if (!first.ok) throw new Error(options.error || 'Impossible de démarrer.');
        options.challenge = Webauthn.toBytes(options.challenge);
        (options.allowCredentials || []).forEach((c) => { c.id = Webauthn.toBytes(c.id); });
        const got = await navigator.credentials.get({ publicKey: options });
        return { id: got.id, rawId: Webauthn.toText(got.rawId), type: got.type,
            response: { clientDataJSON: Webauthn.toText(got.response.clientDataJSON), authenticatorData: Webauthn.toText(got.response.authenticatorData),
                signature: Webauthn.toText(got.response.signature), userHandle: got.response.userHandle ? Webauthn.toText(got.response.userHandle) : null },
            clientExtensionResults: got.getClientExtensionResults ? got.getClientExtensionResults() : {} };
    }
    async function proofBody() {
        const body = creds();
        if (!body.code && Webauthn.supported()) {
            try { body.assertion = await keyProof(); } catch (error) { notify(error.message && !error.name ? error.message : 'Clé refusée ou opération annulée.', 'danger'); return null; }
        }
        return body;
    }

    $('mfaKeyAdd').addEventListener('click', async () => {
        try {
            const first = await fetch('/api/me/mfa/webauthn/options', { method: 'POST', credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: $('mfaPassword').value }) });
            const options = await first.json();
            if (!first.ok) { notify(options.error || 'Impossible de démarrer.', 'danger'); return; }
            options.challenge = Webauthn.toBytes(options.challenge);
            options.user.id = Webauthn.toBytes(options.user.id);
            (options.excludeCredentials || []).forEach((c) => { c.id = Webauthn.toBytes(c.id); });
            const created = await navigator.credentials.create({ publicKey: options });
            const credential = { id: created.id, rawId: Webauthn.toText(created.rawId), type: created.type,
                response: { clientDataJSON: Webauthn.toText(created.response.clientDataJSON),
                    attestationObject: Webauthn.toText(created.response.attestationObject),
                    transports: created.response.getTransports ? created.response.getTransports() : [] },
                clientExtensionResults: created.getClientExtensionResults ? created.getClientExtensionResults() : {} };
            const reply = await post('/api/me/mfa/webauthn/verify', { credential, name: 'Clé de sécurité' });
            if (reply.recovery_codes && reply.recovery_codes.length) showOnce('Codes de secours (affichés une seule fois) :', reply.recovery_codes.join('\n'));
            $('mfaPassword').value = '';
            loadMfa();
        } catch (error) { notify(error.message && !error.name ? error.message : 'Clé refusée ou opération annulée.', 'danger'); }
    });
    $('mfaSetup').addEventListener('click', async () => {
        const r = await act(post('/api/me/mfa/totp/setup', { password: $('mfaPassword').value }));
        if (!r) return;
        const svg = new DOMParser().parseFromString(r.qr_svg, 'image/svg+xml').documentElement;
        $('mfaQr').replaceChildren(document.importNode(svg, true));
        $('mfaSecret').textContent = r.secret;
        $('mfaEnroll').hidden = false;
    });
    $('mfaConfirmForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const r = await act(post('/api/me/mfa/totp/confirm', { code: $('mfaConfirmCode').value.trim() }));
        if (!r) return;
        $('mfaEnroll').hidden = true; $('mfaConfirmCode').value = ''; $('mfaPassword').value = '';
        if (r.recovery_codes.length) showOnce('Codes de secours (affichés une seule fois, chacun ne sert qu’une fois) :', r.recovery_codes.join('\n'));
        loadMfa();
    });
    $('mfaRegen').addEventListener('click', async () => {
        const body = await proofBody(); if (!body) return;
        const r = await act(post('/api/me/mfa/recovery-codes', body));
        if (r) { showOnce('Nouveaux codes de secours (les anciens ne valent plus) :', r.recovery_codes.join('\n')); loadMfa(); }
    });
    $('mfaRemove').addEventListener('click', async () => {
        if (await act(post('/api/me/mfa/totp', creds(), 'DELETE'), () => notify('Code TOTP retiré.'))) loadMfa();
    });

    document.addEventListener('DOMContentLoaded', () => { loadMfa(); });
})();
