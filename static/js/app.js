document
    .getElementById("severity")
    .addEventListener("input", (e) => {
        document.getElementById("sevLabel").textContent = Number(
            e.target.value,
        ).toFixed(2);
    });

document.getElementById("speed").addEventListener("input", (e) => {
    const speed = Number(e.target.value) || 1;
    document.getElementById("speedLabel").textContent = speed + "x";
    state.speed = speed;
    state.timeScale = speed;
    updateSimClock(state.agents.greedy.elapsed || 0);
});

document
    .getElementById("start")
    .addEventListener("click", startSession);
document
    .getElementById("stop")
    .addEventListener("click", stopSession);
document.querySelectorAll(".events button").forEach((btn) => {
    btn.addEventListener("click", () =>
        injectEvent(btn.dataset.event),
    );
});

async function loadMeta() {
    try {
        const meta = await fetch("/meta").then((r) => r.json());
        depotMarker.setLatLng([meta.depot.lat, meta.depot.lon]);
        drawZones(meta.flood_zones || []);
        map.setView([meta.depot.lat, meta.depot.lon], 11.5);
        if (meta.graph_ready) {
            if (!state.ws) setStatus("Grafo de Monterrey listo");
        } else {
            if (!state.ws)
                setStatus(
                    "Cargando grafo de Monterrey en segundo plano…",
                    "loading",
                );
            setTimeout(loadMeta, 4000);
        }
    } catch (err) {
        setStatus("No se pudo leer /meta", "error");
    }
}

loadMeta();
renderKpis();
