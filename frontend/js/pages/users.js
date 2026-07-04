let _currentUserRole = null;

async function loadCurrentUserRole() {
    const me = await apiRequest('/api/users/me');
    if (me) _currentUserRole = me.role;
}

async function loadUsersData() {
    const tbody = document.getElementById('users-table-body');
    if (!tbody) return;
    const users = await apiRequest('/api/users');
    if (!users || !users.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-600 text-sm">No users found</td></tr>';
        return;
    }
    const isAdmin = _currentUserRole === 'admin';
    tbody.innerHTML = users.map(u => {
        const roleColors = {
            admin: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
            operator: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20',
            viewer: 'bg-slate-700 text-slate-300 border-slate-600',
        };
        const roleCls = roleColors[u.role] || roleColors.viewer;
        const actions = isAdmin
            ? `<button onclick="toggleUser('${u.id}')" class="text-xs px-2 py-1 rounded border ${u.is_active ? 'border-amber-500/30 text-amber-400 hover:bg-amber-500/10' : 'border-emerald-500/30 text-emerald-400 hover:bg-emerald-500/10'} transition-all">
                   ${u.is_active ? 'Deactivate' : 'Activate'}
               </button>`
            : '<span class="text-xs text-slate-600">—</span>';
        return `<tr class="border-b border-slate-800/50">
            <td class="px-4 py-3 font-medium text-white">${u.username}</td>
            <td class="px-4 py-3 text-slate-300">${u.email || '—'}</td>
            <td class="px-4 py-3"><span class="px-2 py-0.5 rounded text-[10px] font-bold border ${roleCls}">${u.role.toUpperCase()}</span></td>
            <td class="px-4 py-3">
                <span class="${u.is_active ? 'text-emerald-400' : 'text-rose-400'} font-semibold">${u.is_active ? 'Active' : 'Inactive'}</span>
            </td>
            <td class="px-4 py-3 text-slate-500 text-xs">${u.last_login ? new Date(u.last_login).toLocaleString() : 'Never'}</td>
            <td class="px-4 py-3">${actions}</td>
        </tr>`;
    }).join('');
    // Show/hide Add User button based on role
    const addBtn = document.getElementById('btn-add-user');
    if (addBtn) addBtn.style.display = isAdmin ? 'inline-flex' : 'none';
}

async function loadAuditData() {
    const tbody = document.getElementById('audit-table-body');
    if (!tbody) return;
    const logs = await apiRequest('/api/audit?limit=100');
    if (!logs || !logs.length) {
        tbody.innerHTML = '<tr><td colspan="5" class="px-4 py-8 text-center text-slate-600 text-sm">No audit entries</td></tr>';
        return;
    }
    tbody.innerHTML = logs.map(l => `
        <tr class="border-b border-slate-800/50">
            <td class="px-4 py-3 text-slate-500 text-xs">${l.timestamp ? new Date(l.timestamp).toLocaleString() : '—'}</td>
            <td class="px-4 py-3 text-white font-medium">${l.username}</td>
            <td class="px-4 py-3"><code class="text-xs bg-slate-800 px-1.5 py-0.5 rounded text-indigo-300">${l.action}</code></td>
            <td class="px-4 py-3 text-slate-300 text-xs">${l.resource || '—'}</td>
            <td class="px-4 py-3 text-slate-400 text-xs">${l.detail || '—'}</td>
        </tr>
    `).join('');
}

async function toggleUser(userId) {
    // Fetch current state first
    const users = await apiRequest('/api/users');
    if (!users) return;
    const u = users.find(x => x.id === userId);
    if (!u) return;
    const newState = !u.is_active;
    const res = await apiRequest(`/api/users/${userId}`, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({is_active: newState})
    });
    if (res) {
        showToast({severity: 'info', title: 'User Updated', message: `User ${u.username} ${newState ? 'activated' : 'deactivated'}.`});
        await loadUsersData();
    } else {
        showToast({severity: 'warning', title: 'Failed', message: 'Could not update user. Admin role required.'});
    }
}

function showAddUserForm() {
    if (_currentUserRole !== 'admin') {
        showToast({severity: 'warning', title: 'Access Denied', message: 'Admin role required to create users.'});
        return;
    }
    const existing = document.getElementById('add-user-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'add-user-modal';
    modal.className = 'fixed inset-0 z-50 flex items-center justify-center bg-black/70';
    modal.innerHTML = `
        <div class="bg-slate-900 border border-slate-700 rounded-xl p-6 w-96">
            <h3 class="text-lg font-bold text-white mb-4">Add User</h3>
            <div class="space-y-3">
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Username</label>
                    <input id="new-user-name" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Email</label>
                    <input id="new-user-email" type="email" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Password</label>
                    <input id="new-user-pass" type="password" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                </div>
                <div>
                    <label class="text-xs font-semibold text-slate-400 mb-1 block">Role</label>
                    <select id="new-user-role" class="w-full bg-slate-800 border border-slate-700 rounded px-3 py-2 text-white text-sm">
                        <option value="viewer">Viewer</option>
                        <option value="operator">Operator</option>
                        <option value="admin">Admin</option>
                    </select>
                </div>
            </div>
            <div class="flex gap-2 mt-5">
                <button onclick="submitNewUser()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-indigo-600 hover:bg-indigo-500 text-white transition-all">Create User</button>
                <button onclick="document.getElementById('add-user-modal').remove()" class="flex-1 py-2 rounded-lg text-sm font-semibold bg-slate-700 hover:bg-slate-600 text-white transition-all">Cancel</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
}

async function submitNewUser() {
    const body = {
        username: document.getElementById('new-user-name')?.value,
        email: document.getElementById('new-user-email')?.value || '',
        password: document.getElementById('new-user-pass')?.value,
        role: document.getElementById('new-user-role')?.value || 'viewer',
    };
    if (!body.username || !body.password) {
        showToast({severity: 'warning', title: 'Missing Fields', message: 'Username and password required.'});
        return;
    }
    const res = await apiRequest('/api/users', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body)
    });
    if (res) {
        document.getElementById('add-user-modal').remove();
        showToast({severity: 'info', title: 'User Created', message: `User ${body.username} created.`});
        await loadUsersData();
    } else {
        showToast({severity: 'warning', title: 'Failed', message: 'Could not create user. Admin role required.'});
    }
}
