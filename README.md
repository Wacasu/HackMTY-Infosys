# The Courier — Simulador de Turno (HackMTY 2026 / Reto Infosys)

Simulador asíncrono de repartidores en Monterrey que compara, lado a lado
(split-screen), un **Agente Base** (Greedy, `alpha=0`) contra un
**Agente Inteligente** adverso al riesgo (`alpha>0`), corriendo ambos en
paralelo sobre **exactamente el mismo turno no visto** (misma semilla
aleatoria, mismo grafo vial real, mismos pedidos).

## Arquitectura

```
city_graph.py          Grafo vial real de Monterrey (OSMnx), reducido al
                        componente fuertemente conexo más grande. Único
                        punto de acceso a nearest_nodes / tiempos de viaje /
                        rutas más cortas. Todo I/O bloqueante se despacha
                        con asyncio.to_thread.

agent_interface.py      Modelos de dominio (Offer, DriverState, ShiftState,
                        AgentDecision) y el contrato abstracto BaseAgent.

order_generator.py      PASO 1. Genera pedidos deterministas (estilo
                        Solomon VRPTW) georreferenciados a Monterrey y
                        proyectados a nodos reales del grafo.

greedy_agent.py          PASO 3. Agente Base: maximiza MXN/minuto de viaje
                        real, ignora el riesgo por diseño.

risk_averse_agent.py     PASO 3. Agente Inteligente: Costo = Te + alpha*Re.
                        Te viene del grafo real; Re se deriva de eventos de
                        clima activos y de la proximidad de la ruta real a
                        zonas de Monterrey propensas a inundación. El
                        batching multi-pedido se resuelve con Google
                        OR-Tools (PDPTW: pickup-and-delivery con ventanas
                        de tiempo), usando esa misma matriz de costo
                        ponderada por riesgo.

server.py                PASO 4. FastAPI + WebSocket. Corre dos
                        ShiftSimulatorEngine (uno por agente) en paralelo
                        sobre la misma sesión, aplica el timeout duro de
                        200 ms a cada decisión, y expone inyección de
                        eventos sorpresa por REST y por WebSocket.
```

## Reglas de arquitectura garantizadas en el código

1. **100% asíncrono**: toda llamada a OSMnx/NetworkX (CPU/IO-bound) pasa por
   `asyncio.to_thread` dentro de `CityGraphProvider`; el ruteo con OR-Tools
   del agente inteligente se invoca igual desde `server.py`. Ninguna
   corrutina de FastAPI bloquea el event loop.
2. **Timeout de 200 ms**: aplicado en `ShiftSimulatorEngine._evaluate_new_offer`
   con `asyncio.wait_for(..., timeout=0.2)`. Si el agente no responde a
   tiempo, la oferta se rechaza automáticamente y se registra en
   `driver_state.timeouts_incurred`.
3. **Determinismo**: `OrderGenerator.create(random_seed=...)` es la única
   fuente de pedidos; ambos motores comparten la misma instancia, por lo
   que ven el mismo turno. Cada motor mantiene su propio set de pedidos ya
   evaluados, para que la comparación sea justa (uno no le quita pedidos al
   otro).
4. **Malla vial real obligatoria**: `order_generator.py` proyecta cada
   coordenada con `CityGraphProvider.nearest_node` (que usa
   `osmnx.distance.nearest_nodes`); toda distancia/tiempo usado para decidir
   o rutear viene de Dijkstra sobre el grafo real. La única distancia
   euclidiana (`straight_line_distance_km`) se expone solo como referencia
   informativa y nunca se usa para decisiones de agente.

## Cómo correr

```bash
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

Abre el panel visual en [http://127.0.0.1:8000](http://127.0.0.1:8000):
split-screen de ambos agentes, mapa de Monterrey, métricas en vivo e
inyección de eventos sorpresa. El cliente de consola sigue disponible
con `python test_client.py`.

La primera conexión (o el arranque del servidor) dispara la descarga del
grafo de Monterrey vía OSMnx; puede tardar uno o varios minutos dependiendo
de la conexión. Requiere acceso saliente a la API de Overpass/Nominatim.

## Contrato del WebSocket `/ws/shift-simulation`

**Cliente → Servidor** (primer mensaje, obligatorio):

```json
{
    "random_seed": 42,
    "shift_duration_sec": 10800,
    "num_orders": 40,
    "tick_interval_sec": 30,
    "time_scale": 20.0,
    "risk_alpha": 0.35
}
```

**Cliente → Servidor** (en cualquier momento, para inyectar un evento sorpresa):

```json
{ "type": "inject_event", "event_type": "HEAVY_RAIN", "severity": 0.85 }
```

**Servidor → Cliente** (mensajes intercalados de ambos motores):

```json
{
    "type": "tick",
    "agent_name": "risk_averse_smart",
    "alpha": 0.35,
    "driver_state": { "...": "..." },
    "shift_state": { "...": "..." },
    "current_order_in_progress": { "...": "..." },
    "recent_decisions": [{ "...": "..." }]
}
```

El mismo evento también puede inyectarse vía REST, útil para un panel de
control externo al cliente que renderiza el split-screen:

```
POST /sessions/{session_id}/events
{"event_type": "HEAVY_RAIN", "severity": 0.85}
```

`session_id` llega al cliente en el primer mensaje `session_started` que
envía el servidor tras abrir el WebSocket.
.
