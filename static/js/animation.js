function realMsPerTick() {
  // Tiempo de reloj real que transcurre entre dos ticks del servidor:
  // exactamente lo que el motor duerme entre snapshots
  // (`tick_interval_sec / time_scale`). Antes la animación duraba
  // siempre 650ms fijos, así que a velocidades altas el marcador
  // llegaba y se quedaba esperando (se veía "a tirones"), y a
  // velocidades bajas terminaba antes de que llegara el próximo tick
  // (se veía "congelado"). Un margen del 95% deja el movimiento
  // terminado justo antes de que llegue el siguiente tick, y el piso
  // de 160ms evita que a velocidades muy altas el tramo se sienta como
  // un salto instantáneo en vez de un desplazamiento visible.
  const speed = state.speed || 1;
  const ms = (state.tickIntervalSec / speed) * 1000 * 0.95;
  return Math.min(2400, Math.max(160, ms));
}

function closestPointOnRoute(route, point) {
  let best = null;
  for (let i = 0; i < route.length - 1; i++) {
    const start = route[i];
    const end = route[i + 1];
    const latSpan = end.lat - start.lat;
    const lonSpan = end.lon - start.lon;
    const lengthSquared = latSpan * latSpan + lonSpan * lonSpan;
    const t = lengthSquared > 1e-18
      ? Math.max(0, Math.min(1, ((point.lat - start.lat) * latSpan + (point.lng - start.lon) * lonSpan) / lengthSquared))
      : 0;
    const lat = start.lat + latSpan * t;
    const lon = start.lon + lonSpan * t;
    const distance = Math.hypot(point.lat - lat, point.lng - lon);
    if (!best || distance < best.distance) {
      best = { index: i, t, lat, lon, distance };
    }
  }
  return best;
}

function animateDriverMarker(marker, target, route) {
  // Interpola sobre los segmentos de la ruta real, a velocidad constante.
  // Reiniciar una curva ease-in/ease-out en cada tick hacía que el marcador
  // frenara y acelerara continuamente.
  if (marker._animationFrame) cancelAnimationFrame(marker._animationFrame);
  const start = marker.getLatLng();

  let points = [[start.lat, start.lng], target];
  if (route && route.length > 1) {
    const routeStart = closestPointOnRoute(route, start);
    const routeTarget = closestPointOnRoute(route, { lat: target[0], lng: target[1] });
    if (routeStart && routeTarget && routeTarget.index >= routeStart.index) {
      points = [[start.lat, start.lng]];
      if (routeStart.index === routeTarget.index) {
        points.push([routeTarget.lat, routeTarget.lon]);
      } else {
        points.push([routeStart.lat, routeStart.lon]);
        for (let i = routeStart.index + 1; i <= routeTarget.index; i++) {
          points.push([route[i].lat, route[i].lon]);
        }
        points.push([routeTarget.lat, routeTarget.lon]);
      }
      points.push(target);
    }
  }

  const cumulative = [0];
  for (let i = 1; i < points.length; i++) {
    const dLat = points[i][0] - points[i - 1][0];
    const dLon = points[i][1] - points[i - 1][1];
    cumulative.push(cumulative[i - 1] + Math.hypot(dLat, dLon));
  }
  const totalDist = cumulative[cumulative.length - 1];

  // Ángulo inicial (para rotar el ícono hacia el rumbo de avance) con
  // el primer segmento que tenga longitud real.
  let bearing = null;
  for (let i = 1; i < points.length; i++) {
    if (cumulative[i] > cumulative[i - 1] + 1e-9) {
      const dLat = points[i][0] - points[i - 1][0];
      const dLon = points[i][1] - points[i - 1][1];
      bearing = (Math.atan2(dLon, dLat) * 180) / Math.PI;
      break;
    }
  }
  if (bearing !== null) rotateMarkerIcon(marker, bearing);

  const startedAt = performance.now();
  const duration = realMsPerTick();
  let segIdx = 1;

  const step = (now) => {
    const progress = Math.min((now - startedAt) / duration, 1);
    const distanceProgress = progress;

    if (totalDist <= 1e-9) {
      marker.setLatLng(target);
    } else {
      const distAlong = distanceProgress * totalDist;
      while (segIdx < cumulative.length - 1 && cumulative[segIdx] < distAlong) segIdx++;
      const segStart = cumulative[segIdx - 1];
      const segEnd = cumulative[segIdx];
      const segT = segEnd > segStart ? (distAlong - segStart) / (segEnd - segStart) : 1;
      const [lat1, lon1] = points[segIdx - 1];
      const [lat2, lon2] = points[segIdx];
      marker.setLatLng([lat1 + (lat2 - lat1) * segT, lon1 + (lon2 - lon1) * segT]);
    }

    if (progress < 1) marker._animationFrame = requestAnimationFrame(step);
    else marker._animationFrame = null;
  };
  marker._animationFrame = requestAnimationFrame(step);
}

function rotateMarkerIcon(marker, bearingDeg) {
  const el = marker.getElement && marker.getElement();
  if (!el) return;
  const heading = el.querySelector(".driver-heading");
  if (!heading) return;
  // Solo la "aguja" de rumbo rota -- el círculo con la letra G/R se
  // queda siempre derecho y legible.
  heading.style.setProperty("--bearing", bearingDeg + "deg");
}
