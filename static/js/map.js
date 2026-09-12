const map = L.map("map", { zoomControl: true }).setView(
    [25.6714, -100.3092],
    11.5,
);
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: "&copy; OpenStreetMap",
    maxZoom: 19,
}).addTo(map);

const greedyMarker = L.marker([25.6714, -100.3092], {
    icon: L.divIcon({
        className: "driver-icon",
        html: '<span class="driver-marker greedy">G</span>',
        iconSize: [34, 34],
        iconAnchor: [17, 17],
    }),
    zIndexOffset: 1000,
})
    .addTo(map)
    .bindTooltip("Greedy · ruta más rápida");

const smartMarker = L.marker([25.6714, -100.3092], {
    icon: L.divIcon({
        className: "driver-icon",
        html: '<span class="driver-marker smart">R</span>',
        iconSize: [34, 34],
        iconAnchor: [17, 17],
    }),
    zIndexOffset: 1100,
})
    .addTo(map)
    .bindTooltip("Risk-averse · ruta anti-riesgo");

const depotMarker = L.circleMarker([25.6714, -100.3092], {
    radius: 6,
    color: "#e7ecf3",
    fillColor: "#0e1116",
    fillOpacity: 1,
    weight: 2,
})
    .addTo(map)
    .bindTooltip("Depósito");

function drawZones(zones) {
    state.layers.zones.forEach((z) => map.removeLayer(z));
    state.layers.zones = zones.map((z) =>
        L.circle([z.lat, z.lon], {
            radius: z.radius_km * 1000,
            color: "#d45d5d",
            weight: 1,
            fillColor: "#d45d5d",
            fillOpacity: 0.12,
        })
            .addTo(map)
            .bindTooltip(z.name),
    );
}

function drawOrders(orders, reset = true) {
    if (reset) {
        state.layers.orders.forEach((m) => map.removeLayer(m));
        state.layers.orders = [];
        state.visibleOrderIds.clear();
    }
    orders.forEach((o) => {
        if (state.visibleOrderIds.has(o.order_id)) return;
        state.visibleOrderIds.add(o.order_id);
        const pickupIcon = L.divIcon({
            className: "order-icon",
            html: `<span class="order-marker pickup pending">P${o.order_id}</span>`,
            iconSize: [28, 28],
            iconAnchor: [14, 14],
        });
        const dropoffIcon = L.divIcon({
            className: "order-icon",
            html: `<span class="order-marker dropoff">D${o.order_id}</span>`,
            iconSize: [28, 28],
            iconAnchor: [14, 14],
        });
        state.layers.orders.push(
            L.marker([o.pickup_lat, o.pickup_lon], {
                icon: pickupIcon,
                zIndexOffset: 200,
            })
                .addTo(map)
                .bindTooltip(
                    "Recoger pedido #" +
                        o.order_id +
                        " · $" +
                        o.base_fare_mxn,
                ),
        );
        state.layers.orders.push(
            L.marker([o.dropoff_lat, o.dropoff_lon], {
                icon: dropoffIcon,
                zIndexOffset: 100,
            })
                .addTo(map)
                .bindTooltip("Entregar pedido #" + o.order_id),
        );
    });
}

function revealOrders(elapsedSec) {
    const newlyAvailable = state.orders.filter(
        (order) =>
            order.ready_time_sec <= elapsedSec &&
            !state.visibleOrderIds.has(order.order_id),
    );
    if (!newlyAvailable.length) return;
    drawOrders(newlyAvailable, false);
    document.getElementById("mapSummary").textContent =
        `${state.visibleOrderIds.size}/${state.orders.length} pedidos disponibles · rutas por calles`;
    newlyAvailable.forEach((order) => {
        appendLog(
            `<span class="event">NUEVO PEDIDO #${order.order_id} · recoger y entregar</span>`,
        );
    });
}

function drawCurrentRoutes() {
    state.layers.routes.forEach((l) => map.removeLayer(l));
    state.layers.routes = [];
    [
        ["greedy", "#d45d5d"],
        ["smart", "#3db8a0"],
    ].forEach(([key, color]) => {
        const route = state.agents[key].route;
        if (!route.length) return;
        const line = L.polyline(
            route.map((point) => [point.lat, point.lon]),
            {
                color,
                weight: key === "greedy" ? 5 : 4,
                opacity: 0.9,
                dashArray: "8 7",
                className: `live-route ${key}-route`,
            },
        ).addTo(map);
        state.layers.routes.push(line);
    });
}
