function showCoinBurst(key, position) {
    const coins = [];
    const spread = [-3, 3, -1, 1, 0];
    const upBias = [-16, -22, -14, -20, -26];
    for (let i = 0; i < 5; i++) {
        coins.push(
            `<span class="coin" style="--cx:${spread[i] * 8}px;--cy:${upBias[i]}px;animation-delay:${i * 60}ms"></span>`,
        );
    }
    const burstIcon = L.divIcon({
        className: "coin-burst-icon",
        html: `<div class="coin-burst">${coins.join("")}</div>`,
        iconSize: [0, 0],
        iconAnchor: [0, 0],
    });
    const burstMarker = L.marker([position.lat, position.lon], {
        icon: burstIcon,
        zIndexOffset: 2100,
        interactive: false,
    }).addTo(map);
    window.setTimeout(() => map.removeLayer(burstMarker), 1500);
}

function showMoneyFloat(key, position, amount) {
    const floatIcon = L.divIcon({
        className: "money-float-icon",
        html: `<span class="money-float ${key}">+${amount.toFixed(2)}</span>`,
        iconSize: [60, 24],
        iconAnchor: [30, 22],
    });
    const floatMarker = L.marker([position.lat, position.lon], {
        icon: floatIcon,
        zIndexOffset: 2000,
        interactive: false,
    }).addTo(map);
    window.setTimeout(() => {
        map.removeLayer(floatMarker);
    }, 1900);
}

function animateDriverMarker(marker, target) {
    if (marker._animationFrame)
        cancelAnimationFrame(marker._animationFrame);
    const start = marker.getLatLng();
    const startedAt = performance.now();
    const duration = 650;
    const step = (now) => {
        const progress = Math.min((now - startedAt) / duration, 1);
        const eased = progress * (2 - progress);
        marker.setLatLng([
            start.lat + (target[0] - start.lat) * eased,
            start.lng + (target[1] - start.lng) * eased,
        ]);
        if (progress < 1)
            marker._animationFrame = requestAnimationFrame(step);
        else marker._animationFrame = null;
    };
    marker._animationFrame = requestAnimationFrame(step);
}

function showEvent(eventType, severity) {
    const banner = document.getElementById("eventBanner");
    const labels = {
        HEAVY_RAIN: "Lluvia intensa",
        FLASH_FLOOD: "Inundacion",
        HEAVY_TRAFFIC: "Trafico pesado",
        EXTREME_HEAT: "Calor extremo",
        CLEAR: "Clima despejado",
    };
    banner.textContent = `${labels[eventType] || eventType} activo · severidad ${Number(severity).toFixed(2)}`;
    banner.classList.add("visible");
    document.getElementById("envLine").textContent =
        `Clima: ${labels[eventType] || eventType} · severidad ${Number(severity).toFixed(2)}`;
    setWeather([eventType], severity);
    if (eventType === "CLEAR")
        window.setTimeout(
            () => banner.classList.remove("visible"),
            2200,
        );
}

function setWeather(events, severity) {
    const overlay = document.getElementById("weatherOverlay");
    overlay.className = "weather-overlay";
    if (events.includes("HEAVY_RAIN"))
        overlay.classList.add("rain");
    else if (events.includes("FLASH_FLOOD"))
        overlay.classList.add("flood");
    else if (events.includes("HEAVY_TRAFFIC"))
        overlay.classList.add("traffic");
    else if (events.includes("EXTREME_HEAT"))
        overlay.classList.add("heat");
    overlay.style.opacity = events.includes("CLEAR")
        ? "0"
        : String(0.45 + Number(severity) * 0.55);
}
