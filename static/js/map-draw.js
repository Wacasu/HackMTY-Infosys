    function drawZones(zones) {
      state.layers.zones.forEach((z) => map.removeLayer(z));
      state.layers.zones = zones.map((z) =>
        L.circle([z.lat, z.lon], {
          radius: z.radius_km * 1000,
          color: "#d45d5d",
          weight: 1,
          fillColor: "#d45d5d",
          fillOpacity: 0.12,
        }).addTo(map).bindTooltip(`🌊 ${z.name} (histórico de inundación, severidad base ${z.base_severity})`)
      );
    }

    // Intersecciones con más accidentes reales registrados (dataset oficial
    // del municipio, ver risk_model.py) -- riesgo activo todo el año, no
    // solo con lluvia, por eso se dibujan en un color distinto al de las
    // zonas de inundación.
    function drawAccidentHotspots(hotspots) {
      (state.layers.accidentHotspots || []).forEach((z) => map.removeLayer(z));
      state.layers.accidentHotspots = hotspots.map((z) =>
        L.circle([z.lat, z.lon], {
          radius: z.radius_km * 1000,
          color: "#c77dff",
          weight: 1,
          fillColor: "#c77dff",
          fillOpacity: 0.10,
        }).addTo(map).bindTooltip(`🚧 ${z.name} — ${z.accident_count.toLocaleString("es-MX")} accidentes registrados (2017-2024)`)
      );
    }

    function drawOrders(orders) {
      state.layers.orders.forEach((m) => map.removeLayer(m));
      state.layers.orders = [];
      state.orderMarkers = {};
      orders.forEach((o) => {
        const pickupMarker = L.marker([o.pickup_lat, o.pickup_lon], {
          icon: L.divIcon({ className: "order-pin", html: '<div class="order-marker pickup pending">P</div>', iconSize: [28, 28], iconAnchor: [14, 14] }),
          zIndexOffset: 200,
        }).addTo(map).bindTooltip("Pickup #" + o.order_id + " · $" + o.base_fare_mxn);
        const dropoffMarker = L.marker([o.dropoff_lat, o.dropoff_lon], {
          icon: L.divIcon({ className: "order-pin", html: '<div class="order-marker dropoff pending">D</div>', iconSize: [28, 28], iconAnchor: [14, 14] }),
          zIndexOffset: 200,
        }).addTo(map).bindTooltip("Dropoff #" + o.order_id);
        state.layers.orders.push(pickupMarker, dropoffMarker);
        state.orderMarkers[o.order_id] = {
          pickup: pickupMarker, dropoff: dropoffMarker,
          pickupDone: false, dropoffDone: false,
        };
      });
    }

    // Anillo que se expande y desaparece sobre un punto del mapa -- marca
    // el instante exacto en que un agente recoge o entrega un pedido, sin
    // dejar rastro permanente (se auto-elimina al terminar la animación).
    function spawnPing(lat, lon, colorVar) {
      const marker = L.marker([lat, lon], {
        icon: L.divIcon({ className: "order-ping-wrap", html: `<div class="order-ping" style="--ping-color:${colorVar}"></div>`, iconSize: [16, 16], iconAnchor: [8, 8] }),
        interactive: false,
        zIndexOffset: 900,
      }).addTo(map);
      setTimeout(() => map.removeLayer(marker), 780);
    }

    // Texto de ganancia ("+$X.XX") que sube flotando y se desvanece sobre
    // el punto de entrega -- refuerzo visual inmediato de que ese agente
    // acaba de cobrar, sin tener que ir a leer el panel de KPIs.
    function spawnFloatEarn(lat, lon, text, colorVar) {
      const marker = L.marker([lat, lon], {
        icon: L.divIcon({ className: "float-earn-wrap", html: `<div class="float-earn" style="--ping-color:${colorVar}">${text}</div>`, iconSize: [80, 20], iconAnchor: [40, 24] }),
        interactive: false,
        zIndexOffset: 950,
      }).addTo(map);
      setTimeout(() => map.removeLayer(marker), 1200);
    }

    // Marca un punto (pickup o dropoff) de un pedido como "ya visitado" por
    // un agente: dispara el anillo de una sola vez y, la primera vez que
    // ocurre, apaga el pulso de "pendiente" y lo atenúa con el color de
    // ese agente. Si el otro agente pasa después por el mismo punto, solo
    // repite el anillo (no hay dos pines por agente -- el mapa es uno solo).
    function markOrderPoint(orderId, phase, colorVar) {
      const entry = state.orderMarkers[orderId];
      if (!entry) return;
      const marker = entry[phase];
      if (!marker) return;
      const latlng = marker.getLatLng();
      spawnPing(latlng.lat, latlng.lng, colorVar);
      const doneKey = phase + "Done";
      if (!entry[doneKey]) {
        entry[doneKey] = true;
        const el = marker.getElement();
        const inner = el && el.querySelector(".order-marker");
        if (inner) {
          inner.classList.remove("pending");
          inner.classList.add("collected");
          inner.style.setProperty("--agent-color", colorVar);
        }
      }
    }

    function drawCurrentRoutes() {
      state.layers.routes.forEach((l) => map.removeLayer(l));
      state.layers.routes = [];
      [["greedy", "#e0524a"], ["smart", "#3db8a0"]].forEach(([key, color]) => {
        const route = state.agents[key].remainingRoute || state.agents[key].route || [];
        if (!route.length) return;
        const line = L.polyline(route.map((point) => [point.lat, point.lon]), {
          color, weight: 4, opacity: 0.9, dashArray: "8 7",
        }).addTo(map);
        state.layers.routes.push(line);
      });
    }

    function nearestRouteIndex(route, position) {
      if (!route.length || !position) return -1;
      let bestIdx = 0;
      let bestDist = Infinity;
      for (let i = 0; i < route.length; i++) {
        const dLat = route[i].lat - position.lat;
        const dLon = route[i].lon - position.lon;
        const dist = dLat * dLat + dLon * dLon;
        if (dist < bestDist) {
          bestDist = dist;
          bestIdx = i;
        }
      }
      return bestIdx;
    }

    function remainingRouteFrom(route, position) {
      // El backend manda la ruta completa (pickup -> dropoff) sin cambios
      // mientras dura el pedido; para que se vea como un trayecto que se
      // "consume" al avanzar (en vez de una línea fija todo el viaje),
      // recortamos al punto más cercano a la posición actual del repartidor
      // y dibujamos solo lo que falta por recorrer.
      const bestIdx = nearestRouteIndex(route, position);
      if (bestIdx < 0) return route;
      return route.slice(bestIdx);
    }
