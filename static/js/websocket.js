function startSession() {
    stopSession();
    state.completed = false;
    state.agents = { greedy: emptyAgent(), smart: emptyAgent() };
    state.history = { greedy: [], smart: [] };
    logEl.textContent = "Conectando…";
    setStatus("Conectando y generando pedidos…", "loading");
    document.getElementById("start").disabled = true;
    document.getElementById("stop").disabled = false;

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
        `${proto}://${location.host}/ws/shift-simulation`,
    );
    state.ws = ws;

    ws.onopen = () => {
        state.speed = Number(
            document.getElementById("speed").value,
        );
        const fixedSeed =
            document.getElementById("fixedSeed").checked;
        const seedInput = document.getElementById("seed");
        if (!fixedSeed) {
            const newSeed =
                Math.floor(Math.random() * 2147483647) + 1;
            seedInput.value = newSeed;
        }
        ws.send(
            JSON.stringify({
                random_seed: Number(seedInput.value),
                shift_duration_sec: Number(
                    document.getElementById("duration").value,
                ),
                num_orders: Number(
                    document.getElementById("orders").value,
                ),
                tick_interval_sec: Number(
                    document.getElementById("tick").value,
                ),
                time_scale: Number(
                    document.getElementById("speed").value,
                ),
                risk_alpha: Number(
                    document.getElementById("alpha").value,
                ),
            }),
        );
    };
    ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
    ws.onerror = () => setStatus("Error de WebSocket", "error");
    ws.onclose = () => {
        state.sessionReady = false;
        document.getElementById("start").disabled = false;
        document.getElementById("stop").disabled = true;
        if (
            !state.completed &&
            (statusEl.textContent.includes("Conectando") ||
                statusEl.classList.contains("running"))
        ) {
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
}

function injectEvent(eventType) {
    const eventMessage = {
        type: "inject_event",
        event_type: eventType,
        severity: Number(document.getElementById("severity").value),
    };
    if (
        !state.ws ||
        state.ws.readyState !== WebSocket.OPEN ||
        !state.sessionReady
    ) {
        state.pendingEvent = eventMessage;
        setStatus(
            "Evento listo; esperando que inicie la sesión",
            "loading",
        );
        return;
    }
    state.ws.send(JSON.stringify(eventMessage));
}

function handleMessage(msg) {
    if (msg.type === "session_started") {
        state.sessionId = msg.session_id;
        state.sessionReady = true;
        state.shiftDurationSec =
            msg.config?.shift_duration_sec || 0;
        state.speed =
            msg.config?.time_scale ||
            Number(document.getElementById("speed").value);
        state.tickIntervalSec =
            msg.config?.tick_interval_sec || 30;
        state.timeScale = state.speed;
        state.orders = msg.orders || [];
        document.getElementById("sessionLine").textContent =
            "sesión " +
            msg.session_id.slice(0, 8) +
            " · " +
            msg.total_orders +
            " pedidos";
        if (msg.depot)
            depotMarker.setLatLng([msg.depot.lat, msg.depot.lon]);
        if (msg.flood_zones) drawZones(msg.flood_zones);
        drawOrders([]);
        document.getElementById("mapSummary").textContent =
            `0/${state.orders.length} pedidos disponibles · esperando turno`;
        setStatus("Turno en curso", "running");
        logEl.innerHTML = "";
        updateSimClock(0);
        renderIncidents();
        if (state.pendingEvent) {
            state.ws.send(JSON.stringify(state.pendingEvent));
            state.pendingEvent = null;
        }
    } else if (msg.type === "tick" || msg.type === "shift_ended") {
        applyTick(msg);
    } else if (msg.type === "event_ack") {
        showEvent(msg.event_type, msg.severity);
        appendLog(
            `<span class="event">EVENTO ${msg.event_type} · severidad ${Number(msg.severity).toFixed(2)}</span>`,
        );
    } else if (msg.type === "session_complete") {
        state.completed = true;
        setStatus(
            `Turno completado — ${formatMin(state.shiftDurationSec)} simulados`,
        );
        document.getElementById("sessionLine").textContent +=
            " · turno finalizado";
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
    agent.route = msg.current_route || [];
    const prevEarnings = agent.earnings || 0;
    agent.earnings = driver.earnings_mxn || 0;
    const earnedDelta = agent.earnings - prevEarnings;
    if (earnedDelta > 0.01 && msg.driver_position) {
        showMoneyFloat(key, msg.driver_position, earnedDelta);
    }
    agent.completed = driver.orders_completed || 0;
    agent.rejected = driver.orders_rejected || 0;
    agent.timeouts = driver.timeouts_incurred || 0;
    agent.active = (driver.active_orders || []).length;
    const prevCurrent = agent.current;
    agent.current = msg.current_order_in_progress;
    if (!prevCurrent && agent.current && msg.driver_position) {
        showCoinBurst(key, msg.driver_position);
    }
    agent.incidents = msg.incident_stats?.incidents_exposed || 0;
    agent.zonesCrossed = msg.incident_stats?.zones_crossed || [];
    agent.highRiskAccepted = msg.high_risk_stats?.high_risk_accepted || 0;
    agent.avgRiskOfAccepted = msg.high_risk_stats?.avg_risk_of_accepted || 0;
    agent.elapsed = shift.elapsed_sec || 0;
    agent.duration = shift.shift_duration_sec || 1;
    agent.events = shift.active_events || ["CLEAR"];
    updateSimClock(agent.elapsed);
    revealOrders(agent.elapsed);
    setWeather(agent.events, shift.event_severity || 0);
    if (msg.driver_position) {
        agent.position = msg.driver_position;
        const marker =
            key === "greedy" ? greedyMarker : smartMarker;
        animateDriverMarker(marker, [
            msg.driver_position.lat,
            msg.driver_position.lon,
        ], agent.route);
    }
    state.history[key].push({
        t: agent.elapsed,
        y: agent.earnings,
    });
    if (state.history[key].length > 240) state.history[key].shift();
    document.getElementById("envLine").textContent =
        "Clima: " +
        agent.events.join(", ") +
        " · t=" +
        formatMin(agent.elapsed);
    renderKpis();
    renderIncidents();
    drawCurrentRoutes();
    drawChart();
    (msg.recent_decisions || []).forEach((d) => {
        const fingerprint = `${msg.agent_name}:${d.order_id}:${d.accepted}:${d.timed_out}:${d.reasoning}`;
        if (agent.seenDecisions.has(fingerprint)) return;
        agent.seenDecisions.add(fingerprint);
        const cls = d.timed_out
            ? "timeout"
            : d.accepted
                ? "accept"
                : "reject";
        const verb = d.timed_out
            ? "TIMEOUT"
            : d.accepted
                ? "ACEPTA"
                : "RECHAZA";
        appendLog(
            `<span class="${cls}">${msg.agent_name} ${verb} #${d.order_id}</span> score=${Number(d.score).toFixed(2)} — ${d.reasoning}`,
        );
    });
}
