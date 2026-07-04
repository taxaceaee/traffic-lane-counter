async function renderDashboardCharts() {
    destroyChart(chartHourly); chartHourly = null;
    destroyChart(chartVehicleTypes); chartVehicleTypes = null;
    clearContainer('#chart-hourly-traffic');
    clearContainer('#chart-vehicle-types');

    const [dashData, hourlyData, alerts] = await Promise.all([
        apiRequest('/api/dashboard/summary'),
        apiRequest('/api/dashboard/hourly'),
        apiRequest('/api/alerts'),
    ]);

    const totalVeh = dashData ? dashData.total_vehicles : 0;
    const perCamera = dashData ? dashData.per_camera : [];
    const typeDist = dashData ? dashData.type_distribution || {} : {};
    const activeAlerts = dashData ? dashData.active_alerts : 0;
    const totalCam = dashData ? dashData.total_cameras : 0;
    const totalLanes = dashData ? dashData.total_lanes : 0;

    document.getElementById('kpi-total-vehicles').innerText = totalVeh ? totalVeh.toLocaleString() : '0';
    document.getElementById('kpi-active-cameras').innerText = totalCam || (camerasList.length || '—');
    document.getElementById('kpi-cameras-sub').innerText = totalCam ? totalCam + ' total registered' : (camerasList.length + ' total registered');
    document.getElementById('kpi-active-lanes').innerText = totalLanes ? totalLanes : '—';

    // Vehicle type donut
    const typeColors = ['#6366f1','#10b981','#f59e0b','#ef4444','#38bdf8','#a78bfa','#fb923c'];
    const typeLabels = Object.keys(typeDist);
    const typeValues = Object.values(typeDist);
    const typeTotal = typeValues.reduce((a, b) => a + b, 0) || 1;

    if (typeLabels.length) {
        const typeOpts = {
            series: typeValues,
            chart: { type: 'donut', height: 160, background: 'transparent' },
            colors: typeColors.slice(0, typeLabels.length),
            labels: typeLabels,
            legend: { show: false },
            plotOptions: { pie: { donut: { size: '70%' } } },
            dataLabels: { enabled: false },
            theme: { mode: 'dark' },
            tooltip: { theme: 'dark' }
        };
        clearContainer('#chart-vehicle-types');
        chartVehicleTypes = new ApexCharts(document.querySelector('#chart-vehicle-types'), typeOpts);
        chartVehicleTypes.render();

        const legendContainer = document.getElementById('type-legend-container');
        legendContainer.innerHTML = typeLabels.map((label, i) => {
            const pct = Math.round(typeValues[i] / typeTotal * 100);
            const color = typeColors[i % typeColors.length];
            return `<div class="flex justify-between text-xs">
                <span class="text-slate-400 flex items-center gap-1.5">
                    <span class="w-2 h-2 rounded-full" style="background:${color}"></span>${label}
                </span>
                <span class="text-white font-semibold">${pct}%</span>
            </div>`;
        }).join('');
    } else {
        document.getElementById('type-legend-container').innerHTML = '<p class="text-xs text-slate-500 text-center">No data yet</p>';
    }

    // Top busiest cameras
    const topCamList = document.getElementById('top-cameras-list');
    if (perCamera.length) {
        const maxTotal = Math.max(...perCamera.map(c => c.total), 1);
        topCamList.innerHTML = perCamera.map((c, i) => {
            const rank = i + 1;
            const pct = Math.round(c.total / maxTotal * 100);
            const rankColor = rank === 1 ? 'text-amber-400' : 'text-slate-400';
            const barOpacity = rank === 1 ? '' : rank === 2 ? '/60' : '/40';
            return `<div class="flex items-center justify-between">
                <div class="flex items-center gap-2">
                    <span class="text-xs font-bold ${rankColor} w-4">#${rank}</span>
                    <span class="text-sm text-slate-200">${c.camera_id}</span>
                </div>
                <div class="flex items-center gap-2">
                    <div class="w-24 bg-slate-800 rounded-full h-1.5">
                        <div class="bg-indigo-500${barOpacity} h-1.5 rounded-full" style="width:${pct}%"></div>
                    </div>
                    <span class="text-xs text-white font-semibold w-12 text-right">${c.total.toLocaleString()}</span>
                </div>
            </div>`;
        }).join('');
    } else {
        topCamList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No data yet</p>';
    }

    // Peak hours — real data from backend
    if (hourlyData && hourlyData.peak_hours && hourlyData.peak_hours.length) {
        const peaks = hourlyData.peak_hours;
        document.getElementById('peak-morning').innerText =
            (peaks.find(p => p.label === 'morning_peak')?.count || '—').toLocaleString();
        document.getElementById('peak-evening').innerText =
            (peaks.find(p => p.label === 'evening_peak')?.count || '—').toLocaleString();
        document.getElementById('peak-offpeak').innerText =
            (hourlyData.offpeak_avg || 0).toLocaleString();
    } else {
        ['peak-morning','peak-evening','peak-offpeak'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.innerText = '—';
        });
    }

    // Active alerts
    const alertsList = document.getElementById('active-alerts-list');
    if (alerts && alerts.length) {
        const severityColors = { critical: 'rose', warning: 'amber', info: 'blue' };
        alertsList.innerHTML = alerts.slice(0, 5).map(a => {
            const sev = a.severity || 'info';
            const color = severityColors[sev] || 'slate';
            return `<div class="alert-${sev} bg-${color}-500/5 rounded-lg p-3">
                <p class="text-xs font-bold text-${color}-400">${a.title || a.camera_id || 'Alert'}</p>
                <p class="text-xs text-slate-500 mt-0.5">${a.message || ''}</p>
            </div>`;
        }).join('');
    } else if (activeAlerts > 0) {
        alertsList.innerHTML = `<p class="text-xs text-slate-500 text-center py-4">${activeAlerts} active alert(s)</p>`;
    } else {
        alertsList.innerHTML = '<p class="text-xs text-slate-500 text-center py-4">No active alerts</p>';
    }

    // Hourly chart
    let chartData = null;
    if (hourlyData && hourlyData.hourly) {
        chartData = hourlyData.hourly.map(h => h.count);
    }
    if (!chartData) {
        // Real-time note: no data from backend — show zeros instead of synthetic
        chartData = Array(24).fill(0);
    }

    const cats = Array.from({length: 24}, (_, i) => i + 'h');

    const hourlyOpts = {
        series: [{ name: 'Vehicles', data: chartData }],
        chart: { type: 'area', height: 210, toolbar: { show: false } },
        theme: { mode: 'dark' },
        stroke: { curve: 'smooth', width: 2 },
        fill: { type: 'gradient', gradient: { shadeIntensity: 1, opacityFrom: 0.35, opacityTo: 0.02, stops: [0, 100] } },
        colors: ['#6366f1'],
        xaxis: {
            type: 'category',
            categories: cats,
            tickAmount: 24,
            axisBorder: { show: false },
            labels: { style: { fontSize: '10px' }, hideOverlappingLabels: false, rotate: 0 },
        },
        yaxis: { labels: { style: { fontSize: '10px' } } },
        grid: { borderColor: '#1e293b', strokeDashArray: 3 },
        tooltip: { theme: 'dark' },
    };
    clearContainer('#chart-hourly-traffic');
    chartHourly = new ApexCharts(document.querySelector('#chart-hourly-traffic'), hourlyOpts);
    chartHourly.render();
    setTimeout(() => {
        if (!chartHourly) return;
        try { chartHourly.updateOptions(hourlyOpts, true, false); } catch (e) {}
    }, 100);
}
