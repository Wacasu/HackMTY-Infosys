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

    // Aceleración/desaceleración suave (arranca despacio, acelera a la
    // mitad, frena al llegar) en vez de una desaceleración simple desde el
    // primer frame -- se nota menos "de golpe" al empezar cada tramo.
    function easeInOutCubic(t) {
      return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
    }

    function animateDriverMarker(marker, target, traveledPath) {
      // Antes esto interpolaba en LÍNEA RECTA entre la posición anterior y
      // la nueva -- si la calle daba vuelta entre dos ticks, el marcador
      // cortaba en diagonal a través de la manzana en vez de seguir la
      // curva (justo la ruta que sí se dibuja en el mapa). Ahora, cuando
      // tenemos el tramo real recorrido (`traveledPath`, un pedazo de la
      // polilínea del servidor), el marcador avanza PUNTO A PUNTO sobre
      // esa curva a velocidad constante -- no repartiendo el tiempo por
      // igual entre segmentos (que se ve entrecortado si un segmento es
      // mucho más largo que otro), sino proporcional a la distancia
      // recorrida en cada uno.
      if (marker._animationFrame) cancelAnimationFrame(marker._animationFrame);
      const start = marker.getLatLng();

      let points = [[start.lat, start.lng]];
      if (traveledPath && traveledPath.length > 1) {
        points = traveledPath.map((p) => [p.lat, p.lon]);
        // Ancla el primer punto a donde el marcador visualmente está ahora
        // (evita un salto si el punto más cercano de la ruta no coincide
        // exactamente con la posición interpolada del tick anterior).
        points[0] = [start.lat, start.lng];
      } else {
        points.push(target);
      }
      points[points.length - 1] = target;

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
        const eased = easeInOutCubic(progress);

        if (totalDist <= 1e-9) {
          marker.setLatLng(target);
        } else {
          const distAlong = eased * totalDist;
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
