// Invitations depuis le profil : crédits restants, formulaire (si can_invite), liste. Pour tout compte, SSO comme mot de passe
// (EF-3.2) ; fichier séparé de profile-security.js pour ne pas embarquer WebAuthn / TOTP chez un compte SSO.
// Données du serveur : textContent seulement ; le lien d'invitation n'est jamais écrit dans le stockage du navigateur.
'use strict';

(() => {
    const { api, el, notify } = App;
    const $ = (id) => document.getElementById(id);

    async function act(promise) {
        try { return await promise; } catch (error) { notify(error.message, 'danger'); return null; }
    }

    function showOnce(text, value) {
        $('invOneShotText').textContent = text;
        $('invOneShotValue').textContent = value;
        $('invOneShot').classList.remove('d-none');
    }
    $('invOneShotCopy').addEventListener('click', async () => {
        try { await navigator.clipboard.writeText($('invOneShotValue').textContent); notify('Copié.'); } catch (_) {
            const range = document.createRange(); range.selectNodeContents($('invOneShotValue'));
            const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
        }
    });
    $('invOneShotClose').addEventListener('click', () => { $('invOneShotValue').textContent = ''; $('invOneShot').classList.add('d-none'); });

    async function loadInvitations() {
        const data = await act(api('/api/invitations'));
        if (!data) return;
        $('invCredits').textContent = data.is_admin ? 'Administrateur : invitations illimitées.' : `Crédits restants : ${data.invite_credits}`;
        $('invForm').hidden = !data.can_invite;
        $('invList').replaceChildren(...data.invitations.map(row));
    }
    const STATUS = { pending: 'en attente', used: 'acceptée', expired: 'expirée', revoked: 'révoquée' };

    // Une invitation en attente se révoque ; le crédit n'est pas rendu. Confirmation dans la page, jamais confirm().
    function row(invitation) {
        const item = el('li', { class: 'd-flex gap-2 align-items-center flex-wrap' },
            el('span', {}, `${invitation.email || '—'} · ${STATUS[invitation.status] || invitation.status}`));
        if (invitation.status !== 'pending') return item;
        const start = el('button', { type: 'button', class: 'btn btn-sm btn-outline-danger', onclick: () => item.replaceChild(confirmBox(), start) }, 'Révoquer');
        const confirmBox = () => el('span', {}, el('span', { class: 'hint me-1' }, 'Révoquer ? Le crédit n’est pas rendu.'),
            el('button', { type: 'button', class: 'btn btn-sm btn-danger me-1', onclick: async () => {
                if (await act(api(`/api/invitations/${invitation.id}`, { method: 'DELETE' }))) { notify('Invitation révoquée.'); loadInvitations(); }
            } }, 'Confirmer'),
            el('button', { type: 'button', class: 'btn btn-sm btn-link', onclick: loadInvitations }, 'Annuler'));
        item.appendChild(start);
        return item;
    }
    $('invForm').addEventListener('submit', async (event) => {
        event.preventDefault();
        const r = await act(api('/api/invitations', { method: 'POST', body: { email: $('invEmail').value.trim() } }));
        if (r) { if (r.link) showOnce('Lien d’invitation (montré une seule fois) :', r.link); $('invEmail').value = ''; loadInvitations(); }
    });

    document.addEventListener('DOMContentLoaded', loadInvitations);
})();
