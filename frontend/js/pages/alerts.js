function showAlertToast(alert) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const severity = alert.severity || 'info';
    const colors = { critical: 'rose', warning: 'amber', info: 'blue' };
    const icons = { critical: 'alert-circle', warning: 'alert-triangle', info: 'info' };
    const color = colors[severity] || 'slate';
    const icon = icons[severity] || 'bell';
    const id = 'toast-' + Date.now() + '-' + Math.random().toString(36).slice(2, 6);

    const el = document.createElement('div');
    el.id = id;
    el.className = 'pointer-events-auto flex items-start gap-3 px-4 py-3 rounded-xl border shadow-2xl transition-all translate-x-full opacity-0';
    el.style.borderColor = 'rgba(var(--color-border), 0.2)';
    el.style.background = 'rgba(2, 6, 23, 0.95)';
    el.style.backdropFilter = 'blur(12px)';
    el.style.borderLeft = '3px solid ' + (severity === 'critical' ? '#ef4444' : severity === 'warning' ? '#f59e0b' : '#3b82f6');
    el.innerHTML = `
        <i data-lucide="${icon}" class="h-5 w-5 flex-shrink-0 mt-0.5 text-${color}-400"></i>
        <div class="flex-1 min-w-0">
            <p class="text-sm font-bold text-white truncate">${escapeHtml(alert.title || 'Alert')}</p>
            <p class="text-xs text-slate-400 mt-0.5">${escapeHtml(alert.message || '')}</p>
            ${alert.camera_id ? `<p class="text-[10px] text-slate-500 mt-0.5">${escapeHtml(alert.camera_id)}</p>` : ''}
        </div>
        <button onclick="document.getElementById('${id}').remove()" class="text-slate-500 hover:text-slate-300 flex-shrink-0">
            <i data-lucide="x" class="h-3.5 w-3.5"></i>
        </button>`;
    container.appendChild(el);
    lucide.createIcons();

    requestAnimationFrame(() => {
        el.classList.remove('translate-x-full', 'opacity-0');
        el.classList.add('translate-x-0', 'opacity-100');
    });

    setTimeout(() => {
        el.classList.add('translate-x-full', 'opacity-0');
        el.classList.remove('translate-x-0', 'opacity-100');
        setTimeout(() => el.remove(), 400);
    }, 8000);

    while (container.children.length > 5) {
        container.firstChild.remove();
    }
}

// showToast alias — every function in the codebase calls showToast()
function showToast(alert) { showAlertToast(alert); }

async function updateAlertBadge() {
    try {
        const countData = await apiRequest('/api/alerts/count');
        const count = countData ? countData.count : 0;
        const badge = document.getElementById('alert-count-badge');
        const topbar = document.getElementById('alert-topbar');
        const topbarText = document.getElementById('alert-topbar-text');

        if (badge) {
            badge.innerText = count;
            count > 0 ? badge.classList.remove('hidden') : badge.classList.add('hidden');
        }

        if (topbar && topbarText) {
            if (count > 0) {
                topbarText.innerText = count + ' Active Alert' + (count !== 1 ? 's' : '');
                topbar.classList.remove('hidden');
                topbar.classList.add('flex');
            } else {
                topbar.classList.add('hidden');
                topbar.classList.remove('flex');
            }
        }
    } catch (e) {}
}

async function refreshAlerts() {
    const [active, history] = await Promise.all([
        apiRequest('/api/alerts'),
        apiRequest('/api/alerts/history?limit=50')
    ]);

    let crit = 0, warn = 0, info = 0;
    if (active) {
        active.forEach(a => {
            if (a.severity === 'critical') crit++;
            else if (a.severity === 'warning') warn++;
            else info++;
        });
    }
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.innerText = val; };
    set('alerts-count-critical', crit);
    set('alerts-count-warning', warn);
    set('alerts-count-info', info);

    const container = document.getElementById('alerts-active-container');
    const canResolve = ['admin', 'operator'].includes(localStorage.getItem('user_role'));
    if (container) {
        if (active && active.length) {
            container.innerHTML = active.map(a => {
                const sev = a.severity || 'info';
                const icons = { critical: 'wifi-off', warning: 'traffic-cone', info: 'car' };
                const colors = { critical: 'rose', warning: 'amber', info: 'blue' };
                const ico = icons[sev] || 'bell';
                const color = colors[sev] || 'slate';
                const ts = a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '—';
                return `<div class="alert-${sev} bg-${color}-500/5 rounded-lg p-4 flex items-start gap-4">
                    <i data-lucide="${ico}" class="h-5 w-5 text-${color}-400 mt-0.5 flex-shrink-0"></i>
                    <div class="flex-1">
                        <div class="flex justify-between">
                            <p class="text-sm font-bold text-${color}-400">${escapeHtml(a.title || 'Alert')}</p>
                            <span class="text-xs text-slate-500">${ts}</span>
                        </div>
                        <p class="text-xs text-slate-400 mt-1">${escapeHtml(a.message || '')}</p>
                        ${a.camera_id ? `<p class="text-xs text-slate-500 mt-1">Camera: ${escapeHtml(a.camera_id)}</p>` : ''}
                    </div>
                    ${canResolve ? `<button onclick="dismissAlert('${escapeHtml(a.id)}')" class="text-xs text-${color}-400 border border-${color}-500/30 rounded px-2 py-1 hover:bg-${color}-500/10 flex-shrink-0">Dismiss</button>` : ''}
                </div>`;
            }).join('');
        } else {
            container.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No active alerts</p>';
        }
        lucide.createIcons();
    }

    const tbody = document.getElementById('alerts-history-tbody');
    if (tbody) {
        if (history && history.length) {
            tbody.innerHTML = history.map(a => {
                const sev = a.severity || 'info';
                const sevColors = {
                    critical: 'bg-rose-500/10 text-rose-400 border-rose-500/20',
                    warning: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
                    info: 'bg-blue-500/10 text-blue-400 border-blue-500/20'
                };
                const sevClass = sevColors[sev] || 'bg-slate-500/10 text-slate-400 border-slate-500/20';
                const ts = a.timestamp ? new Date(a.timestamp).toLocaleTimeString() : '—';
                const resolved = a.resolved_at ? new Date(a.resolved_at).toLocaleTimeString() : (a.resolved ? 'Resolved' : 'Active');
                const resolvedClass = a.resolved || a.resolved_at ? 'text-emerald-400' : 'text-amber-400';
                return `<tr class="border-b border-slate-800/50 hover:bg-slate-900/20">
                    <td class="px-4 py-3 text-slate-500">${ts}</td>
                    <td class="px-4 py-3"><span class="text-[10px] font-bold px-2 py-0.5 rounded border ${sevClass}">${sev.toUpperCase()}</span></td>
                    <td class="px-4 py-3">${escapeHtml(a.alert_type || '—')}</td>
                    <td class="px-4 py-3 text-white">${escapeHtml(a.camera_id || '—')}</td>
                    <td class="px-4 py-3 text-slate-300">${escapeHtml(a.title || '')}</td>
                    <td class="px-4 py-3 ${resolvedClass}">${resolved}</td>
                </tr>`;
            }).join('');
        } else {
            tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-600 text-xs">No alert history</td></tr>';
        }
    }
}

async function dismissAlert(alertId) {
    const token = localStorage.getItem('access_token');
    if (!token) return;
    try {
        const res = await fetch(BASE_URL + '/api/alerts/' + alertId + '/resolve', {
            method: 'PATCH',
            headers: { 'Authorization': 'Bearer ' + token }
        });
        if (res.ok) {
            await refreshAlerts();
            await updateAlertBadge();
        }
    } catch (e) {
        console.warn('Failed to dismiss alert:', e);
    }
}
