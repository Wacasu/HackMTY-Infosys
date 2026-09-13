    const statusEl = document.getElementById("status");
    const logEl = document.getElementById("log");
    const map = L.map("map", { zoomControl: true }).setView([25.6714, -100.3092], 13.6);
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap",
      maxZoom: 19,
    }).addTo(map);

    const greedyMarker = L.marker([25.6714, -100.3092], {
      icon: L.divIcon({ className: "driver-icon", html: '<div class="driver-wrap"><span class="driver-heading"></span><span class="driver-marker greedy">G</span></div>', iconSize: [34, 34], iconAnchor: [17, 17] }),
      zIndexOffset: 1000,
    }).addTo(map).bindTooltip("Greedy · ruta común");
    const smartMarker = L.marker([25.6714, -100.3092], {
      icon: L.divIcon({ className: "driver-icon", html: '<div class="driver-wrap"><span class="driver-heading"></span><span class="driver-marker smart">R</span></div>', iconSize: [34, 34], iconAnchor: [17, 17] }),
      zIndexOffset: 1100,
    }).addTo(map).bindTooltip("Risk-averse · ruta común");
    const depotMarker = L.circleMarker([25.6714, -100.3092], { radius: 6, color: "#e7ecf3", fillColor: "#0e1116", fillOpacity: 1, weight: 2 }).addTo(map).bindTooltip("Depósito");
