    document.getElementById("severity").addEventListener("input", (e) => {
      document.getElementById("sevLabel").textContent = Number(e.target.value).toFixed(2);
    });

    document.getElementById("speed").addEventListener("input", (e) => {
      const speed = Number(e.target.value) || 1;
      document.getElementById("speedLabel").textContent = speed + "x";
      state.speed = speed;
      updateSimClock(state.agents.greedy.elapsed || 0);
      updateDurationRealHint();
    });

    // Turno en horas (más intuitivo para configurar que segundos crudos),
    // traducido en vivo a cuántos segundos de reloj real tarda en
    // reproducirse a la velocidad elegida -- así se sabe de un vistazo si
    // una prueba de "8 horas de turno" en realidad dura 6 minutos o media
    // hora antes de darle a "Iniciar turno".
    function shiftDurationSecFromInput() {
      const hours = Number(document.getElementById("durationHours").value) || 1;
      return Math.max(60, Math.round(hours * 3600));
    }

    function formatRealDuration(sec) {
      if (sec < 60) return `${sec.toFixed(0)} s`;
      const minutes = Math.floor(sec / 60);
      const remSec = Math.round(sec % 60);
      return remSec > 0 ? `${minutes} min ${remSec}s` : `${minutes} min`;
    }

    function updateDurationRealHint() {
      const speed = Number(document.getElementById("speed").value) || 1;
      const realSec = shiftDurationSecFromInput() / speed;
      document.getElementById("durationRealHint").textContent =
        `≈ ${formatRealDuration(realSec)} reales a ${speed}x`;
    }

    document.getElementById("durationHours").addEventListener("input", updateDurationRealHint);
    updateDurationRealHint();

    document.getElementById("alpha").addEventListener("input", (e) => {
      document.getElementById("raceAlphaLabel").textContent = `(α=${Number(e.target.value).toFixed(2)})`;
    });

    document.getElementById("incidentCost").addEventListener("input", updateImpactPanel);
    document.getElementById("accidentRate").addEventListener("input", updateImpactPanel);

    const impactEl = document.getElementById("impact");
    const impactToggleBtn = document.getElementById("impactToggle");

    function setImpactCollapsed(collapsed) {
      impactEl.classList.toggle("collapsed", collapsed);
      impactToggleBtn.setAttribute("aria-expanded", String(!collapsed));
      try {
        localStorage.setItem("courier_impact_collapsed", collapsed ? "1" : "0");
      } catch (err) {
        // Almacenamiento no disponible (ventana privada, etc.): no pasa
        // nada, simplemente no se recuerda la preferencia entre recargas.
      }
    }

    impactToggleBtn.addEventListener("click", () => {
      setImpactCollapsed(!impactEl.classList.contains("collapsed"));
    });

    // Colapsado por defecto (para no tapar el mapa); si el usuario ya lo
    // había abierto/cerrado antes en este navegador, respeta esa elección.
    let storedImpactCollapsed = null;
    try {
      storedImpactCollapsed = localStorage.getItem("courier_impact_collapsed");
    } catch (err) {
      // Sin acceso a localStorage: usar el default (colapsado).
    }
    setImpactCollapsed(storedImpactCollapsed === null ? true : storedImpactCollapsed === "1");

    const configPanelEl = document.getElementById("configPanel");
    const configToggleBtn = document.getElementById("configToggle");

    function setConfigCollapsed(collapsed) {
      configPanelEl.classList.toggle("config-collapsed", collapsed);
      configToggleBtn.setAttribute("aria-expanded", String(!collapsed));
      try {
        localStorage.setItem("courier_config_collapsed", collapsed ? "1" : "0");
      } catch (err) {
        // Sin localStorage: no se recuerda la preferencia, sin problema.
      }
    }

    configToggleBtn.addEventListener("click", () => {
      setConfigCollapsed(!configPanelEl.classList.contains("config-collapsed"));
    });

    // Expandido por defecto (son los controles para arrancar el turno);
    // respeta la última elección del usuario si ya la guardó antes.
    let storedConfigCollapsed = null;
    try {
      storedConfigCollapsed = localStorage.getItem("courier_config_collapsed");
    } catch (err) {
      // Sin acceso a localStorage: usar el default (expandido).
    }
    setConfigCollapsed(storedConfigCollapsed === "1");

    document.getElementById("start").addEventListener("click", startSession);
    document.getElementById("stop").addEventListener("click", stopSession);
    document.querySelectorAll(".events button").forEach((btn) => {
      btn.addEventListener("click", () => injectEvent(btn.dataset.event));
    });

    document.getElementById("presentToggle").addEventListener("click", (e) => {
      const app = document.querySelector(".app");
      const isPresent = app.classList.toggle("present");
      e.currentTarget.textContent = isPresent ? "🗂 Ver panel completo" : "🎤 Modo presentación";
      e.currentTarget.classList.toggle("active", isPresent);
      // Leaflet no recalcula su tamaño solo: hay que avisarle tras el
      // cambio de layout, cuando el CSS ya terminó de aplicarse.
      setTimeout(() => map.invalidateSize(), 80);
    });

    loadMeta();
    renderKpis();

    async function loadMeta() {
      try {
        const meta = await fetch("/meta").then((r) => r.json());
        depotMarker.setLatLng([meta.depot.lat, meta.depot.lon]);
        drawZones(meta.flood_zones || []);
        drawAccidentHotspots(meta.accident_hotspots || []);
        map.setView([meta.depot.lat, meta.depot.lon], 13.6);
        if (meta.graph_ready) {
          if (!state.ws) setStatus("Grafo de Monterrey listo");
        } else {
          if (!state.ws) setStatus("Cargando grafo de Monterrey en segundo plano…", "loading");
          setTimeout(loadMeta, 4000);
        }
      } catch (err) {
        setStatus("No se pudo leer /meta", "error");
      }
    }

    function setStatus(text, kind) {
      statusEl.textContent = text;
      statusEl.className = "status-pill" + (kind ? " " + kind : "");
    }
