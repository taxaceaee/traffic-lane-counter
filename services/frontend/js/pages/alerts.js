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

let _alertsPollingTimer = null;
let _alertsRefreshInFlight = false;

function startAlertPolling() {
    if (_alertsPollingTimer) clearInterval(_alertsPollingTimer);
    _alertsPollingTimer = setInterval(async () => {
        if (typeof activeTab !== 'undefined' && activeTab !== 'alerts') return;
        if (_alertsRefreshInFlight) return;
        _alertsRefreshInFlight = true;
        try {
            await refreshAlerts();
            await updateAlertBadge();
        } finally {
            _alertsRefreshInFlight = false;
        }
    }, 3000);
}

function stopAlertPolling() {
    if (_alertsPollingTimer) clearInterval(_alertsPollingTimer);
    _alertsPollingTimer = null;
}

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
                const details = a.details || {};
                const detailCode = details.code || '';
                const fixSteps = Array.isArray(details.fix_steps) ? details.fix_steps : [];
                const verifySteps = Array.isArray(details.verify_steps) ? details.verify_steps : [];
                const verificationCommand = details.verification_command
                    ? `<code class="block mt-2 p-2 rounded bg-slate-950 text-[10px] text-sky-300 break-all">${escapeHtml(details.verification_command)}</code>`
                    : '';
                const detailBlock = detailCode ? `
                    <div class="mt-3 grid grid-cols-1 lg:grid-cols-2 gap-3 rounded-lg bg-slate-950/40 p-3">
                        <div>
                            <p class="text-[10px] uppercase font-bold text-amber-300 mb-1">Cách khắc phục</p>
                            <ol class="list-decimal list-inside space-y-1 text-[11px] text-slate-400">${fixSteps.map(step => '<li>' + escapeHtml(step) + '</li>').join('')}</ol>
                        </div>
                        <div>
                            <p class="text-[10px] uppercase font-bold text-sky-300 mb-1">Cách xác minh</p>
                            <ol class="list-decimal list-inside space-y-1 text-[11px] text-slate-400">${verifySteps.map(step => '<li>' + escapeHtml(step) + '</li>').join('')}</ol>
                            ${verificationCommand}
                        </div>
                    </div>` : '';
                const canVerifySource = ['admin', 'operator', 'Administrator'].includes(localStorage.getItem('user_role'));
                const verifyButton = canVerifySource && (detailCode === 'YOUTUBE_ANTIBOT_BLOCKED' || detailCode === 'YOUTUBE_NO_FORMATS')
                    ? `<button type="button" data-verify-camera="${escapeHtml(a.camera_id || '')}" class="mt-2 text-[11px] px-2.5 py-1.5 rounded border border-sky-500/30 text-sky-300 hover:bg-sky-500/10">Verify source now</button>`
                    : '';
                return `<div class="alert-${sev} bg-${color}-500/5 rounded-lg p-4 flex items-start gap-4">
                    <i data-lucide="${ico}" class="h-5 w-5 text-${color}-400 mt-0.5 flex-shrink-0"></i>
                    <div class="flex-1">
                        <div class="flex justify-between">
                            <p class="text-sm font-bold text-${color}-400">${escapeHtml(a.title || 'Alert')}</p>
                            <span class="text-xs text-slate-500">${ts}</span>
                        </div>
                        <p class="text-xs text-slate-400 mt-1">${escapeHtml(a.message || '')}</p>
                        ${detailCode ? `<p class="text-[10px] font-mono text-slate-500 mt-1">Code: ${escapeHtml(detailCode)}</p>` : ''}
                        ${a.camera_id ? `<p class="text-xs text-slate-500 mt-1">Camera: ${escapeHtml(a.camera_id)}</p>` : ''}
                        ${details.cause ? `<p class="text-xs text-slate-500 mt-1"><span class="font-semibold text-slate-400">Nguyên nhân:</span> ${escapeHtml(details.cause)}</p>` : ''}
                        ${detailBlock}
                        ${verifyButton}
                    </div>
                    ${canResolve ? `<button onclick="dismissAlert('${escapeHtml(a.id)}')" class="text-xs text-${color}-400 border border-${color}-500/30 rounded px-2 py-1 hover:bg-${color}-500/10 flex-shrink-0">Dismiss</button>` : ''}
                </div>`;
            }).join('');
        } else {
            container.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No active alerts</p>';
        }
        lucide.createIcons();
        container.querySelectorAll('[data-verify-camera]').forEach(button => {
            if (button.dataset.verifyBound === 'true') return;
            button.dataset.verifyBound = 'true';
            button.addEventListener('click', event => {
                event.preventDefault();
                event.stopPropagation();
                verifyAlertSource(button.dataset.verifyCamera, button);
            });
        });
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
                    <td class="px-4 py-3 text-[10px] font-mono text-slate-500">${escapeHtml(a.details?.code || '—')}</td>
                    <td class="px-4 py-3 text-slate-300">${escapeHtml(a.title || '')}</td>
                    <td class="px-4 py-3 ${resolvedClass}">${resolved}</td>
                </tr>`;
            }).join('');
        } else {
            tbody.innerHTML = '<tr><td colspan="6" class="px-4 py-8 text-center text-slate-600 text-xs">No alert history</td></tr>';
        }
    }
}

async function verifyAlertSource(cameraId, button = null) {
    if (!cameraId) {
        showToast({severity: 'warning', title: 'Missing camera', message: 'The alert has no camera ID to verify.'});
        return;
    }
    const token = localStorage.getItem('access_token');
    if (!token) {
        showToast({severity: 'warning', title: 'Session expired', message: 'Please log in again before verifying the source.'});
        return;
    }
    const originalLabel = button ? button.innerText : '';
    if (button) {
        button.disabled = true;
        button.innerText = 'Checking source...';
        button.classList.add('opacity-60', 'cursor-wait');
    }
    try {
        const response = await fetch(
            BASE_URL + '/live/' + encodeURIComponent(cameraId) + '/verify-source',
            {method: 'POST', headers: {Authorization: 'Bearer ' + token}},
        );
        const payload = await response.json().catch(() => ({}));
        if (response.status === 401 || response.status === 403) {
            showToast({severity: 'warning', title: 'Permission denied', message: payload.detail || 'Operator permission is required to verify a source.'});
        } else if (payload.ok) {
            showToast({severity: 'info', title: 'Source verified', message: 'yt-dlp can resolve the source. The stream will retry automatically.'});
        } else {
            showToast({severity: 'warning', title: 'Source still blocked', message: payload.diagnostic?.message || 'Verification failed.'});
        }
        await refreshAlerts();
        await updateAlertBadge();
    } catch (e) {
        showToast({severity: 'warning', title: 'Verification error', message: e.message || 'Could not verify source.'});
    } finally {
        if (button && button.isConnected) {
            button.disabled = false;
            button.innerText = originalLabel || 'Verify source now';
            button.classList.remove('opacity-60', 'cursor-wait');
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
