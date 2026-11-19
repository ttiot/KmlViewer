'use strict';

(() => {
    const { api, el, icon, notify, dateTime } = App;
    let me = null;
    let adminManagedByIdp = false;

    function toggle(user, field, disabled) {
        const name = user.display_name || user.username || 'Compte';
        return el('label', { class: 'check-target' }, el('input', {
            type: 'checkbox', class: 'form-check-input', checked: user[field], disabled,
            'aria-label': `${name} : ${field === 'is_admin' ? 'administrateur' : 'compte actif'}`,
            onchange: async (event) => {
                try {
                    await api(`/api/users/${user.id}`, { method: 'PATCH', body: { [field]: event.target.checked } });
                } catch (error) { notify(error.message, 'danger'); }
                load();
            },
        }));
    }

    async function remove(user) {
        if (!window.confirm(`Supprimer ${user.username} et TOUTES ses activités ? Action irréversible.\n\n`
            + 'Supprimer ne bannit pas : si les comptes sont activés automatiquement, la personne pourra se '
            + 'reconnecter. Pour bloquer un accès, désactive le compte.')) return;
        try {
            await api(`/api/users/${user.id}`, { method: 'DELETE' });
            load();
        } catch (error) { notify(error.message, 'danger'); }
    }

    function row(user) {
        const self = user.id === me.id;
        return el('tr', {},
            el('td', {}, el('div', { class: 'user-name' },
                el('span', { class: 'avatar', 'aria-hidden': 'true' }, (user.display_name || user.username || '?').slice(0, 1)),
                el('span', {}, user.display_name || '—'),
                self ? el('span', { class: 'chip' }, 'toi') : null)),
            el('td', { class: 'mono' }, user.username || '—'),
            el('td', {}, user.email || '—'),
            el('td', { class: 'text-end' }, user.nb_activities),
            el('td', { class: 'text-nowrap' }, dateTime(user.last_login)),
            el('td', { class: 'text-center' }, toggle(user, 'is_active', self)),
            el('td', { class: 'text-center' }, toggle(user, 'is_admin', self || adminManagedByIdp)),
            el('td', { class: 'text-end' }, self ? null :
                el('button', { type: 'button', class: 'icon-btn', title: 'Supprimer', 'aria-label': `Supprimer ${user.username || 'ce compte'}`,
                    onclick: () => remove(user) }, icon('trash'))));
    }

    async function load() {
        try {
            me = me || (await api('/api/me')).user;
            const data = await api('/api/users');
            adminManagedByIdp = data.admin_managed_by_idp;
            document.getElementById('users').replaceChildren(...data.users.map(row));
            document.getElementById('usersCount').textContent = `${data.users.length} compte${data.users.length > 1 ? 's' : ''}`;
        } catch (error) { notify(error.message, 'danger'); }
    }

    document.addEventListener('DOMContentLoaded', load);
})();
