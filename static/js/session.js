    function startSession() {
      stopSession();
      state.agents = { greedy: emptyAgent(), smart: emptyAgent() };
      state.history = { greedy: [], smart: [] };
      state.completed = false;
      state.sessionReady = false;
      state.pendingEvent = null;
      state.orderDecisionStatus = {};
      logEl.textContent = "Conectando…";
      setStatus("Conectando y generando pedidos…", "loading");
      document.getElementById("start").disabled = true;
      document.getElementById("stop").disabled = false;
      document.getElementById("raceAlphaLabel").textContent =
        `(α=${Number(document.getElementById("alpha").value).toFixed(2)})`;
      updateRaceBar();
      updateShiftProgress(0);
      updateSimClock(0);
      setWeather([], 0);

      const proto = location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${location.host}/ws/shift-simulation`);
      state.ws = ws;

      ws.onopen = () => {
        ws.send(JSON.stringify({
          random_seed: Number(document.getElementById("seed").value),
          shift_duration_sec: shiftDurationSecFromInput(),
          num_orders: Number(document.getElementById("orders").value),
          tick_interval_sec: Number(document.getElementById("tick").value),
          time_scale: Number(document.getElementById("speed").value),
          risk_alpha: Number(document.getElementById("alpha").value),
        }));
      };
      ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
      ws.onerror = () => setStatus("Error de WebSocket", "error");
      ws.onclose = () => {
        document.getElementById("start").disabled = false;
        document.getElementById("stop").disabled = true;
        if (statusEl.textContent.includes("Conectando") || statusEl.classList.contains("running")) {
          setStatus("Sesión cerrada");
        }
      };
    }

    function stopSession() {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: "stop" }));
        state.ws.close();
      }
      state.ws = null;
      state.sessionReady = false;
    }

    function injectEvent(eventType) {
      const eventMessage = {
        type: "inject_event",
        event_type: eventType,
        severity: Number(document.getElementById("severity").value),
      };
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN || !state.sessionReady) {
        // Si el turno ya terminó (p. ej. un clic tardío que llega después
        // de "Turno completado"), no lo dejamos en cola ni pisamos ese
        // mensaje final con "esperando que inicie la sesión".
        if (!state.completed) {
          state.pendingEvent = eventMessage;
          setStatus("Evento listo; esperando que inicie la sesión", "loading");
        }
        return;
      }
      state.ws.send(JSON.stringify(eventMessage));
    }

    function handleMessage(msg) {
      if (msg.type === "session_started") {
        state.sessionId = msg.session_id;
        state.sessionReady = true;
        state.shiftDurationSec = msg.config?.shift_duration_sec || 0;
        state.speed = msg.config?.time_scale || Number(document.getElementById("speed").value);
        state.tickIntervalSec = msg.config?.tick_interval_sec || state.tickIntervalSec;
        state.orders = msg.orders || [];
        document.getElementById("sessionLine").textContent = "sesión " + msg.session_id.slice(0, 8) + " · " + msg.total_orders + " pedidos";
        if (msg.depot) depotMarker.setLatLng([msg.depot.lat, msg.depot.lon]);
        if (msg.flood_zones) drawZones(msg.flood_zones);
        if (msg.accident_hotspots) drawAccidentHotspots(msg.accident_hotspots);
        drawOrders(state.orders);
        document.getElementById("mapSummary").textContent = `${state.orders.length} pedidos · rutas por calles · turno compartido`;
        setStatus("Turno en curso", "running");
        logEl.innerHTML = "";
        updateSimClock(0);
        if (state.pendingEvent) {
          state.ws.send(JSON.stringify(state.pendingEvent));
          state.pendingEvent = null;
        }
      } else if (msg.type === "tick" || msg.type === "shift_ended") {
        applyTick(msg);
      } else if (msg.type === "event_ack") {
        showEvent(msg.event_type, msg.severity);
        appendLog(`<span class="event">EVENTO ${msg.event_type} · severidad ${Number(msg.severity).toFixed(2)}</span>`);
      } else if (msg.type === "session_complete") {
        state.completed = true;
        state.sessionReady = false;
        setStatus("Turno completo — compara ganancias y rechazos");
      } else if (msg.type === "error") {
        setStatus(msg.detail, "error");
      }
    }

    function applyTick(msg) {
      const key = AGENT_KEY[msg.agent_name];
      if (!key) return;
      const driver = msg.driver_state || {};
      const shift = msg.shift_state || {};
      const agent = state.agents[key];
      const colorVar = key === "greedy" ? "var(--greedy)" : "var(--smart)";
      const previousCompleted = agent.completed;
      const previousOrder = agent.current;
      agent.route = msg.current_route || [];
      agent.earnings = driver.earnings_mxn || 0;
      agent.completed = driver.orders_completed || 0;
      agent.rejected = driver.orders_rejected || 0;
      agent.timeouts = driver.timeouts_incurred || 0;
      agent.active = (driver.active_orders || []).length;
      agent.current = msg.current_order_in_progress;

      // Recogida: el pedido en curso pasó de "en camino al pickup" a "ya
      // levantado" (lo manda el backend en `pickup_reached`). Se anima y
      // se loguea una sola vez por pedido con `pickupLoggedOrderId`.
      if (agent.current && msg.pickup_reached && agent.pickupLoggedOrderId !== agent.current.order_id) {
        agent.pickupLoggedOrderId = agent.current.order_id;
        markOrderPoint(agent.current.order_id, "pickup", colorVar);
        appendLog(`<span class="pickup ${key}">📦 ${msg.agent_name} recogió el pedido #${agent.current.order_id}</span>`);
      }

      // Entrega: el conteo de completados subió respecto al tick anterior.
      // El pedido recién entregado ya no viene en `current_order_in_progress`
      // de este tick (el backend lo limpia al completarlo), así que se usa
      // el que traíamos guardado del tick anterior para saber cuál fue.
      if (agent.completed > previousCompleted && previousOrder) {
        markOrderPoint(previousOrder.order_id, "dropoff", colorVar);
        // Aceptado dentro de su ventana no garantiza entregado a tiempo --
        // si para cuando le tocó turno en la cola ya venció, se marca
        // distinto en vez de verse idéntico a una entrega puntual.
        const isLate = (shift.elapsed_sec || 0) > previousOrder.due_time_sec;
        appendLog(`<span class="delivered ${key}${isLate ? " late" : ""}">${isLate ? "⏰" : "✅"} ${msg.agent_name} entregó${isLate ? " TARDE" : ""} el pedido #${previousOrder.order_id} <span class="fare">· $${Number(previousOrder.base_fare_mxn).toFixed(2)}</span></span>`);
        const dropoffMarker = state.orderMarkers[previousOrder.order_id]?.dropoff;
        if (dropoffMarker) {
          const latlng = dropoffMarker.getLatLng();
          spawnFloatEarn(latlng.lat, latlng.lng, "+$" + Number(previousOrder.base_fare_mxn).toFixed(2), colorVar);
        }
      }

      agent.acceptedRiskSum = driver.accepted_risk_sum || 0;
      agent.acceptedRiskCount = driver.accepted_risk_count || 0;
      agent.riskyAccepted = driver.risky_orders_accepted || 0;
      agent.safetyRejections = driver.safety_rejections || 0;
      agent.deliveredLate = driver.orders_delivered_late || 0;
      agent.elapsed = shift.elapsed_sec || 0;
      agent.duration = shift.shift_duration_sec || 1;
      agent.events = shift.active_events || ["CLEAR"];
      if (msg.driver_position) {
        agent.position = msg.driver_position;
        const marker = key === "greedy" ? greedyMarker : smartMarker;
        const newIndex = nearestRouteIndex(agent.route, agent.position);
        // Tramo real de la ruta recorrido desde el tick anterior hasta
        // este (no solo el punto de llegada): así el marcador se anima
        // SIGUIENDO la curva de la calle, en vez de cortar en línea recta
        // de un punto al otro (que se veía atravesando manzanas cuando la
        // calle daba vuelta entre dos ticks).
        let traveledPath = null;
        if (newIndex >= 0 && agent.lastRouteIndex != null && newIndex >= agent.lastRouteIndex) {
          traveledPath = agent.route.slice(agent.lastRouteIndex, newIndex + 1);
        }
        agent.lastRouteIndex = newIndex >= 0 ? newIndex : null;
        animateDriverMarker(marker, [msg.driver_position.lat, msg.driver_position.lon], traveledPath);
      }
      agent.remainingRoute = remainingRouteFrom(agent.route, agent.position);
      state.history[key].push({ t: agent.elapsed, y: agent.earnings });
      if (state.history[key].length > 240) state.history[key].shift();
      document.getElementById("envLine").textContent = "Clima: " + agent.events.join(", ") + " · t=" + formatMin(agent.elapsed);
      updateSimClock(agent.elapsed);
      renderKpis();
      drawCurrentRoutes();
      drawChart();
      (msg.recent_decisions || []).forEach((d) => {
        const fingerprint = `${msg.agent_name}:${d.order_id}:${d.accepted}:${d.timed_out}:${d.reasoning}`;
        if (agent.seenDecisions.has(fingerprint)) return;
        agent.seenDecisions.add(fingerprint);
        const cls = d.timed_out ? "timeout" : d.accepted ? "accept" : "reject";
        const verb = d.timed_out ? "TIMEOUT" : d.accepted ? "ACEPTA" : "RECHAZA";
        const latencyMs = Number(d.decision_latency_ms ?? 0);
        const latencyClass = d.timed_out ? "timeout" : latencyMs > 120 ? "reject" : "";
        const latencyBadge = `<span class="latency ${latencyClass}">${latencyMs.toFixed(1)}ms</span>`;
        appendLog(`<span class="${cls}">${msg.agent_name} ${verb} #${d.order_id}</span> score=${Number(d.score).toFixed(2)} ${latencyBadge} — ${d.reasoning}`);

        // Si NINGÚN agente va a recoger este pedido (ambos ya lo evaluaron
        // y lo rechazaron/timeout), su pin ya no le sirve a nadie -- se
        // programa para desvanecerse en vez de quedarse pegado en el mapa
        // el resto del turno.
        const trackedStatus = state.orderDecisionStatus[d.order_id]
          || (state.orderDecisionStatus[d.order_id] = { rejectedBy: new Set(), acceptedByAny: false, fadeScheduled: false });
        if (d.accepted) {
          trackedStatus.acceptedByAny = true;
        } else {
          trackedStatus.rejectedBy.add(key);
        }
        maybeScheduleOrderFade(d.order_id);
      });
    }

    function maybeScheduleOrderFade(orderId) {
      const status = state.orderDecisionStatus[orderId];
      if (!status || status.fadeScheduled || status.acceptedByAny) return;
      if (status.rejectedBy.size < 2) return; // faltan agentes por decidir
      status.fadeScheduled = true;
      setTimeout(() => fadeOutOrderPoint(orderId), 3500);
    }

    function fadeOutOrderPoint(orderId) {
      const entry = state.orderMarkers[orderId];
      if (!entry) return;
      [entry.pickup, entry.dropoff].forEach((marker) => {
        const el = marker && marker.getElement && marker.getElement();
        const inner = el && el.querySelector(".order-marker");
        if (inner) inner.classList.add("expired");
      });
      // Se quita del mapa hasta que termine la transición de desvanecido
      // (ver duración en la regla `.order-marker.expired`), no de golpe.
      setTimeout(() => {
        [entry.pickup, entry.dropoff].forEach((marker) => {
          if (marker) map.removeLayer(marker);
        });
        delete state.orderMarkers[orderId];
      }, 650);
    }
