const statusEl = document.getElementById("status");
const logEl = document.getElementById("log");

function setStatus(text, kind) {
    statusEl.textContent = text;
    statusEl.className = "status-pill" + (kind ? " " + kind : "");
}

function kpi(value, label) {
    return `<div class="kpi"><b>${value}</b><span>${label}</span></div>`;
}

function renderKpis() {
    ["greedy", "smart"].forEach((key) => {
        const a = state.agents[key];
        document.getElementById("kpis-" + key).innerHTML = [
            kpi("$" + a.earnings.toFixed(2), "Ganancia"),
            kpi(a.completed, "Completados"),
            kpi(a.rejected, "Rechazados"),
            kpi(a.timeouts, "Timeouts 2s"),
        ].join("");
        const cur = a.current;
        document.getElementById("cur-" + key).textContent = cur
            ? `Entregando #${cur.order_id} · $${cur.base_fare_mxn} · ${cur.straight_line_distance_km} km`
            : a.active
              ? `${a.active} pedidos en cola`
              : "Sin pedido en curso";
    });
}

function renderIncidents() {
    const g = state.agents.greedy.incidents || 0;
    const s = state.agents.smart.incidents || 0;
    const avoided = g - s;
    const maxVal = Math.max(g, s, 1);
    document.getElementById("incident-percent-greedy").textContent =
        Math.round((g / maxVal) * 100) + "%";
    document.getElementById("incident-percent-smart").textContent =
        Math.round((s / maxVal) * 100) + "%";
    document.getElementById("incident-count-greedy").textContent = g;
    document.getElementById("incident-count-smart").textContent = s;
    const zonesG = [
        ...new Set(state.agents.greedy.zonesCrossed || []),
    ];
    const zonesS = [
        ...new Set(state.agents.smart.zonesCrossed || []),
    ];
    document.getElementById("incident-zones-greedy").textContent =
        zonesG.length ? "Zonas cruzadas: " + zonesG.join(", ") : "";
    document.getElementById("incident-zones-smart").textContent =
        zonesS.length ? "Zonas cruzadas: " + zonesS.join(", ") : "";
    const summary = document.getElementById("incident-summary");
    if (g === 0 && s === 0) {
        summary.textContent =
            "Sin datos de incidentes aún — inyecta un evento (lluvia, tráfico) para ver la comparativa";
        summary.className = "incident-summary neutral";
    } else if (avoided > 0) {
        summary.textContent =
            avoided +
            " incidente" +
            (avoided !== 1 ? "s" : "") +
            " evitado" +
            (avoided !== 1 ? "s" : "") +
            " por el agente inteligente";
        summary.className = "incident-summary positive";
    } else if (avoided < 0) {
        summary.textContent =
            Math.abs(avoided) +
            " incidente" +
            (Math.abs(avoided) !== 1 ? "s" : "") +
            " extra para el agente inteligente";
        summary.className = "incident-summary negative";
    } else {
        summary.textContent =
            "Ambos agentes con la misma exposición a incidentes";
        summary.className = "incident-summary neutral";
    }
}

function appendLog(html) {
    const line = document.createElement("div");
    line.innerHTML = html;
    logEl.prepend(line);
    while (logEl.children.length > 40)
        logEl.removeChild(logEl.lastChild);
}

function formatMin(sec) {
    return (sec / 60).toFixed(1) + " min";
}

function formatClock(sec) {
    const startHour = 8;
    const totalSeconds =
        startHour * 3600 + Math.max(0, Math.floor(sec));
    const hour = Math.floor(totalSeconds / 3600) % 24;
    const minute = Math.floor((totalSeconds % 3600) / 60);
    const second = totalSeconds % 60;
    return [hour, minute, second]
        .map((part) => String(part).padStart(2, "0"))
        .join(":");
}

function updateSimClock(elapsedSec) {
    const speed =
        state.speed ||
        Number(document.getElementById("speed").value) ||
        1;
    document.getElementById("simClock").textContent =
        formatClock(elapsedSec);
    document.getElementById("simClockMeta").textContent =
        `hora simulada · ${formatMin(elapsedSec)} transcurridos · velocidad ${speed}x`;
}
