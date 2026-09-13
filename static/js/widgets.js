    // Capa visual sobre el mapa (lluvia cayendo, tinte de inundación,
    // franjas de tráfico, calor shimmer) -- refuerza el evento activo sin
    // depender solo de leer el banner de texto. `eventTypes` es un arreglo
    // por si en el futuro hay más de un evento simultáneo; por ahora el
    // backend manda uno a la vez y se usa el primero reconocido.
    const WEATHER_OVERLAY_CLASSES = {
      HEAVY_RAIN: "rain",
      FLASH_FLOOD: "flood",
      HEAVY_TRAFFIC: "traffic",
      EXTREME_HEAT: "heat",
    };

    function setWeather(eventTypes, severity) {
      const overlay = document.getElementById("weatherOverlay");
      if (!overlay) return;
      const activeType = (eventTypes || []).find((t) => WEATHER_OVERLAY_CLASSES[t]);
      overlay.className = "weather-overlay" + (activeType ? " " + WEATHER_OVERLAY_CLASSES[activeType] : "");
      overlay.style.opacity = activeType ? String(Math.max(0.35, Math.min(1, Number(severity) || 0.8))) : "0";
    }

    function showEvent(eventType, severity) {
      const banner = document.getElementById("eventBanner");
      const icons = {
        HEAVY_RAIN: "🌧️", FLASH_FLOOD: "🌊", HEAVY_TRAFFIC: "🚗",
        EXTREME_HEAT: "🔥", CLEAR: "☀️",
      };
      const labels = {
        HEAVY_RAIN: "Lluvia intensa",
        FLASH_FLOOD: "Inundación",
        HEAVY_TRAFFIC: "Tráfico pesado",
        EXTREME_HEAT: "Calor extremo",
        CLEAR: "Clima despejado",
      };
      const classes = {
        HEAVY_RAIN: "rain", FLASH_FLOOD: "flood", HEAVY_TRAFFIC: "traffic",
        EXTREME_HEAT: "heat", CLEAR: "clear",
      };
      document.getElementById("eventIcon").textContent = icons[eventType] || "⚠️";
      document.getElementById("eventText").textContent =
        `${labels[eventType] || eventType} activo · severidad ${Number(severity).toFixed(2)}`;
      banner.className = "event-banner visible " + (classes[eventType] || "");
      document.getElementById("envLine").textContent = `Clima: ${labels[eventType] || eventType} · severidad ${Number(severity).toFixed(2)}`;
      setWeather([eventType], severity);
      if (eventType === "CLEAR") window.setTimeout(() => banner.classList.remove("visible"), 2600);
    }

    function renderKpis() {
      ["greedy", "smart"].forEach((key) => {
        const a = state.agents[key];
        const values = {
          earnings: "$" + a.earnings.toFixed(2),
          completed: String(a.completed),
          late: String(a.deliveredLate),
          rejected: String(a.rejected),
          timeouts: String(a.timeouts),
        };
        document.getElementById("kpis-" + key).innerHTML = [
          kpi(values.earnings, "Ganancia", values.earnings !== a.prevKpi.earnings),
          kpi(values.completed, "Completados", values.completed !== a.prevKpi.completed),
          kpi(values.late, "Tarde", values.late !== a.prevKpi.late),
          kpi(values.rejected, "Rechazados", values.rejected !== a.prevKpi.rejected),
          kpi(values.timeouts, "Timeouts 200ms", values.timeouts !== a.prevKpi.timeouts),
        ].join("");
        a.prevKpi = values;
        const cur = a.current;
        document.getElementById("cur-" + key).textContent = cur
          ? `Entregando #${cur.order_id} · $${cur.base_fare_mxn} · ${cur.straight_line_distance_km} km`
          : (a.active ? `${a.active} pedidos en cola` : "Sin pedido en curso");
      });
      updateRaceBar();
      updateImpactPanel();
    }

    function updateRaceBar() {
      const g = state.agents.greedy.earnings;
      const s = state.agents.smart.earnings;
      const max = Math.max(g, s, 1);
      const gPct = g > 0 ? Math.max((g / max) * 100, 3) : 0;
      const sPct = s > 0 ? Math.max((s / max) * 100, 3) : 0;
      document.getElementById("raceFillGreedy").style.width = gPct + "%";
      document.getElementById("raceFillSmart").style.width = sPct + "%";
      document.getElementById("raceValueGreedy").textContent = "$" + g.toFixed(2);
      document.getElementById("raceValueSmart").textContent = "$" + s.toFixed(2);
      document.getElementById("raceGreedy").classList.toggle("leading", g > s);
      document.getElementById("raceSmart").classList.toggle("leading", s > g);
    }

    function updateImpactPanel() {
      const g = state.agents.greedy;
      const s = state.agents.smart;
      const avgRisk = (a) => (a.acceptedRiskCount > 0 ? a.acceptedRiskSum / a.acceptedRiskCount : null);
      const gRisk = avgRisk(g);
      const sRisk = avgRisk(s);

      document.getElementById("impactRiskGreedy").textContent = gRisk === null ? "—" : (gRisk * 100).toFixed(0) + "%";
      document.getElementById("impactRiskSmart").textContent = sRisk === null ? "—" : (sRisk * 100).toFixed(0) + "%";
      document.getElementById("impactRiskyGreedy").textContent = String(g.riskyAccepted);
      document.getElementById("impactRiskySmart").textContent = String(s.riskyAccepted);
      document.getElementById("impactSafetyGreedy").textContent = String(g.safetyRejections);
      document.getElementById("impactSafetySmart").textContent = String(s.safetyRejections);

      const summaryEl = document.getElementById("impactSummary");
      const costPerIncident = Number(document.getElementById("incidentCost").value) || 0;
      const accidentRatePer1000h = Number(document.getElementById("accidentRate").value) || 0;
      const incidentsAvoided = s.safetyRejections;
      const directSavings = incidentsAvoided * costPerIncident;

      const hoursWorked = (agent) => (agent.elapsed > 0 ? agent.elapsed / 3600 : 0);
      const perHour = (amount, hours) => (hours > 0 ? amount / hours : null);
      const gHours = hoursWorked(g);
      const sHours = hoursWorked(s);
      const gEarnPerHour = perHour(g.earnings, gHours);
      const sEarnPerHour = perHour(s.earnings, sHours);

      // Costo ESPERADO por accidentes = incidencia base sin mitigar (input
      // ajustable, ver el porqué en el HTML) x costo promedio por
      // incidente x hora trabajada, reducido en el % de riesgo que ESTE
      // modelo concreto ya logró bajar -- medido en vivo a partir de
      // `gRisk`/`sRisk`, no un segundo supuesto inventado. Greedy no
      // reduce nada por diseño (alpha=0) y absorbe la incidencia base
      // completa; Risk-averse la reduce en la misma proporción en que
      // baja su riesgo promedio aceptado. Es un modelo de EXPOSICIÓN
      // esperada -- esta simulación no genera accidentes reales -- por
      // eso antes esta fila dependía solo de `safetyRejections` (un
      // evento raro que casi nunca ocurría en una prueba corta, de ahí
      // que "no se mostrara nada" útil) y ahora depende de la reducción de
      // riesgo, que SIEMPRE existe en cuanto hay algo de riesgo medido.
      const riskReductionFrac = (gRisk !== null && sRisk !== null && gRisk > 0)
        ? Math.max((gRisk - sRisk) / gRisk, 0)
        : null;
      const baseAccidentCostPerHour = (accidentRatePer1000h / 1000) * costPerIncident;
      const gAccidentCostPerHour = gHours > 0 ? baseAccidentCostPerHour : null;
      const sAccidentCostPerHour = sHours > 0
        ? baseAccidentCostPerHour * (1 - (riskReductionFrac ?? 0))
        : null;
      const preventionSavingsPerHour = (gAccidentCostPerHour !== null && sAccidentCostPerHour !== null)
        ? gAccidentCostPerHour - sAccidentCostPerHour
        : null;

      const fmtMxnHour = (value) => value === null ? "—" : "$" + value.toLocaleString("es-MX", { maximumFractionDigits: 1 }) + "/h";
      document.getElementById("impactEarnHourGreedy").textContent = fmtMxnHour(gEarnPerHour);
      document.getElementById("impactEarnHourSmart").textContent = fmtMxnHour(sEarnPerHour);
      document.getElementById("impactSavingsHourGreedy").textContent = fmtMxnHour(gAccidentCostPerHour);
      document.getElementById("impactSavingsHourSmart").textContent = fmtMxnHour(sAccidentCostPerHour);

      const parts = [];
      if (riskReductionFrac !== null) {
        parts.push(`Risk-averse acepta viajes con ${(riskReductionFrac * 100).toFixed(0)}% menos riesgo promedio que Greedy`);
      }
      if (g.riskyAccepted > 0 || incidentsAvoided > 0) {
        parts.push(`Greedy tomó ${g.riskyAccepted} pedido${g.riskyAccepted === 1 ? "" : "s"} de alto riesgo que Risk-averse rechazó ${incidentsAvoided} vez${incidentsAvoided === 1 ? "" : "es"} por seguridad`);
      }
      if (incidentsAvoided > 0 && costPerIncident > 0) {
        parts.push(`ahorro directo por rechazo: $${directSavings.toLocaleString("es-MX", { maximumFractionDigits: 0 })} MXN (a $${costPerIncident.toLocaleString("es-MX")}/incidente)`);
      }
      if (preventionSavingsPerHour !== null && preventionSavingsPerHour > 0.01) {
        parts.push(`por cada hora trabajada, reduce ~$${preventionSavingsPerHour.toFixed(1)} MXN el costo esperado por accidentes frente a Greedy (supuestos: $${costPerIncident.toLocaleString("es-MX")}/incidente, ${accidentRatePer1000h}/1,000h de incidencia base)`);
      }
      summaryEl.textContent = parts.length
        ? parts.join(" · ") + "."
        : "Corre un turno (idealmente con un evento de clima activo) para ver el impacto en vivo.";

      // Adelanto de una línea visible en el título aunque el panel esté
      // colapsado -- así no hace falta desplegarlo solo para chismosear
      // si ya hay algo que mostrar.
      const hintEl = document.getElementById("impactToggleHint");
      if (gRisk === null && sRisk === null) {
        hintEl.textContent = "— corre un turno para verlo";
      } else if (incidentsAvoided > 0) {
        hintEl.textContent = `— ${incidentsAvoided} incidente${incidentsAvoided === 1 ? "" : "s"} evitado${incidentsAvoided === 1 ? "" : "s"}`;
      } else if (gRisk !== null && sRisk !== null && gRisk > 0) {
        const reduction = Math.max(((gRisk - sRisk) / gRisk) * 100, 0);
        hintEl.textContent = `— ${reduction.toFixed(0)}% menos riesgo aceptado`;
      } else {
        hintEl.textContent = "";
      }
    }

    function kpi(value, label, changed) {
      return `<div class="kpi${changed ? " flash" : ""}"><b>${value}</b><span>${label}</span></div>`;
    }

    function appendLog(html) {
      const line = document.createElement("div");
      line.innerHTML = html;
      logEl.prepend(line);
      while (logEl.children.length > 40) logEl.removeChild(logEl.lastChild);
    }

    function formatMin(sec) {
      return (sec / 60).toFixed(1) + " min";
    }

    function formatClock(sec) {
      const startHour = 8;
      const totalSeconds = startHour * 3600 + Math.max(0, Math.floor(sec));
      const hour = Math.floor(totalSeconds / 3600) % 24;
      const minute = Math.floor((totalSeconds % 3600) / 60);
      const second = totalSeconds % 60;
      return [hour, minute, second].map((part) => String(part).padStart(2, "0")).join(":");
    }

    function updateSimClock(elapsedSec) {
      const speed = state.speed || Number(document.getElementById("speed").value) || 1;
      document.getElementById("simClock").textContent = formatClock(elapsedSec);
      document.getElementById("simClockMeta").textContent = `hora simulada · ${formatMin(elapsedSec)} transcurridos · velocidad ${speed}x`;
      updateShiftProgress(elapsedSec);
    }

    function updateShiftProgress(elapsedSec) {
      const duration = state.shiftDurationSec || 1;
      const pct = Math.max(0, Math.min(100, (elapsedSec / duration) * 100));
      document.getElementById("shiftProgress").style.width = pct + "%";
    }

    function drawChart() {
      const canvas = document.getElementById("chart");
      const ctx = canvas.getContext("2d");
      const w = canvas.width;
      const h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      const series = [
        { data: state.history.greedy, color: "#e08a3c" },
        { data: state.history.smart, color: "#3db8a0" },
      ];
      const all = series.flatMap((s) => s.data);
      if (!all.length) return;
      const maxT = Math.max(...all.map((p) => p.t), 1);
      const maxY = Math.max(...all.map((p) => p.y), 1);
      series.forEach((s) => {
        if (!s.data.length) return;
        ctx.beginPath();
        ctx.strokeStyle = s.color;
        ctx.lineWidth = 2;
        s.data.forEach((p, i) => {
          const x = (p.t / maxT) * (w - 8) + 4;
          const y = h - 6 - (p.y / maxY) * (h - 14);
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        ctx.stroke();
      });
    }
