function loadAnalyticsData() {
    renderAnalyticsCharts('day');
}

function setAnalyticsPeriod(period) {
    ['day','week','month'].forEach(p => {
        const btn = document.getElementById('period-' + p);
        if (p === period) { btn.className = 'px-3 py-1.5 rounded text-xs font-semibold text-white bg-indigo-600'; }
        else { btn.className = 'px-3 py-1.5 rounded text-xs font-semibold text-slate-400 hover:text-white'; }
    });
    renderAnalyticsCharts(period);
}

async function renderAnalyticsCharts(period) {
    chartHeatmap = destroyChart(chartHeatmap);
    chartVolumes = destroyChart(chartVolumes);
    chartAverages = destroyChart(chartAverages);

    const camera_id = document.getElementById('analytics-cam-select').value;
    if (!camera_id) return;

    const now = new Date();
    let since;
    if (period === 'day') since = new Date(now.getTime() - 24*3600000);
    else if (period === 'week') since = new Date(now.getTime() - 7*24*3600000);
    else since = new Date(now.getTime() - 30*24*3600000);

    const sinceStr = since.toISOString();
    const untilStr = now.toISOString();

    const [summary, timeseries, occ] = await Promise.all([
        apiRequest(`/api/cameras/${camera_id}/counts/summary?since=${encodeURIComponent(sinceStr)}&until=${encodeURIComponent(untilStr)}`),
        apiRequest(`/api/cameras/${camera_id}/counts/timeseries?window=1hour&limit=168`),
        apiRequest(`/api/cameras/${camera_id}/occupancy/latest`),
    ]);

    const lanes = summary ? summary.lanes : [];
    const typeColors = ['#6366f1','#10b981','#f59e0b','#ef4444','#38bdf8','#a78bfa','#fb923c'];

    // Heatmap
    if (timeseries && timeseries.data && timeseries.data.length) {
        const ts = timeseries.data;
        const laneGroups = {};
        ts.forEach(d => {
            const lid = d.lane_id;
            if (!laneGroups[lid]) laneGroups[lid] = {};
            const h = d.timestamp ? d.timestamp.substring(11,13) + 'h' : '?';
            laneGroups[lid][h] = (laneGroups[lid][h] || 0) + (d.count || 0);
        });

        const allHours = Array.from({length:24}, (_,i) => String(i).padStart(2,'0') + 'h');
        const heatSeries = Object.entries(laneGroups).map(([lid, hours]) => ({
            name: lid,
            data: allHours.map(h => ({ x: h, y: hours[h] || 0 })),
        }));

        if (heatSeries.length) {
            const heatHeight = Math.max(280, 160 + heatSeries.length * 50);
            const heatEl = document.querySelector('#chart-heatmap');
            if (heatEl) heatEl.style.height = heatHeight + 'px';

            const heatOpts = {
                series: heatSeries,
                chart: { type:'heatmap', height:heatHeight, background:'transparent', toolbar:{show:false} },
                theme: { mode:'dark' },
                colors: ['#6366f1'],
                dataLabels: { enabled:false },
                xaxis: { labels: {style:{fontSize:'9px'}} },
                tooltip: { theme:'dark' },
                plotOptions: { heatmap: { shadeIntensity: 0.6 } }
            };
            chartHeatmap = new ApexCharts(heatEl, heatOpts);
            chartHeatmap.render();
        }
    }

    // Volume per lane
    if (lanes.length) {
        const volHeight = Math.max(210, 160 + Math.max(0, lanes.length - 3) * 36);
        const volEl = document.querySelector('#chart-analytics-volumes');
        if (volEl) volEl.style.height = volHeight + 'px';

        const volOpts = {
            series: [{ name: 'Vehicles', data: lanes.map(l => l.total || 0) }],
            chart: { type:'bar', height:volHeight, background:'transparent', toolbar:{show:false} },
            theme: { mode:'dark' },
            colors: [typeColors[0]],
            xaxis: {
                categories: lanes.map(l => l.lane_id),
                labels: { style:{fontSize:'10px'}, rotate: lanes.length > 6 ? -45 : 0, hideOverlappingLabels:true, maxHeight:80 },
            },
            yaxis: { min: 0, labels: { style: {fontSize:'10px'} } },
            grid: { borderColor:'#1e293b', strokeDashArray: 3 },
            tooltip: { theme:'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius:4, columnWidth: Math.max(30, 80 - lanes.length * 4) + '%' } }
        };
        chartVolumes = new ApexCharts(volEl, volOpts);
        chartVolumes.render();
    }

    // Occupancy
    const occData = occ && occ.occupancy ? occ.occupancy : {};
    const occLanes = Object.keys(occData).filter(k => k !== 'no_recent_data');
    if (occLanes.length) {
        const occHeight = Math.max(210, 160 + Math.max(0, occLanes.length - 3) * 36);
        const occEl = document.querySelector('#chart-analytics-occupancy');
        if (occEl) occEl.style.height = occHeight + 'px';

        const occOpts = {
            series: [{ name: 'Occupancy', data: occLanes.map(k => occData[k] || 0) }],
            chart: { type:'bar', height:occHeight, background:'transparent', toolbar:{show:false} },
            theme: { mode:'dark' },
            colors: ['#10b981'],
            xaxis: {
                categories: occLanes,
                labels: { style:{fontSize:'10px'}, rotate: occLanes.length > 6 ? -45 : 0, hideOverlappingLabels:true, maxHeight:80 },
            },
            yaxis: { min: 0, labels: { style: {fontSize:'10px'} } },
            grid: { borderColor:'#1e293b', strokeDashArray: 3 },
            tooltip: { theme:'dark' },
            dataLabels: { enabled: false },
            plotOptions: { bar: { borderRadius:4, columnWidth: Math.max(30, 80 - occLanes.length * 4) + '%' } }
        };
        chartAverages = new ApexCharts(occEl, occOpts);
        chartAverages.render();
    }
}
