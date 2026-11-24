// Sécurité du compte (mot de passe, adresse, second facteur, invitations). Données du serveur : textContent seulement ; le QR code
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
    }
    const creds = () => ({ password: $('mfaPassword').value, code: $('mfaCode').value.trim() });

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
        const r = await act(post('/api/me/mfa/recovery-codes', creds()));
        if (r) { showOnce('Nouveaux codes de secours (les anciens ne valent plus) :', r.recovery_codes.join('\n')); loadMfa(); }
    });
    $('mfaRemove').addEventListener('click', async () => {
        if (await act(post('/api/me/mfa/totp', creds(), 'DELETE'), () => notify('Code TOTP retiré.'))) loadMfa();
    });

    // -- invitations -----------------------------------------------------------------------------------------------------
    async function loadInvitations() {
        const data = await act(api('/api/invitations'));
        if (!data) return;
        $('invCredits').textContent = data.is_admin ? 'Administrateur : invitations illimitées.' : `Crédits restants : ${data.invite_credits}`;
        $('invForm').hidden = !data.can_invite;
        $('invList').replaceChildren(...data.invitations.map((i) => el('li', {}, `${i.email || '—'} · ${i.status}`)));
    }
    $('invForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const r = await act(post('/api/invitations', { email: $('invEmail').value.trim() }));
        if (r) { if (r.link) showOnce('Lien d’invitation (montré une seule fois) :', r.link); $('invEmail').value = ''; loadInvitations(); }
    });

    document.addEventListener('DOMContentLoaded', () => { loadMfa(); loadInvitations(); });
})();
