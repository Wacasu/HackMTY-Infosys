    const AGENT_KEY = { greedy_base: "greedy", risk_averse_smart: "smart" };
    const state = {
      ws: null,
      sessionId: null,
      completed: false,
      sessionReady: false,
      pendingEvent: null,
      speed: 80,
      tickIntervalSec: 30,
      shiftDurationSec: 0,
      agents: {
        greedy: emptyAgent(),
        smart: emptyAgent(),
      },
      history: { greedy: [], smart: [] },
      orders: [],
      orderMarkers: {},
      // Por pedido: quién ya lo rechazó y si alguien lo aceptó -- para
      // saber cuándo un pin ya no le sirve a NINGÚN agente (ambos lo
      // rechazaron/timeout) y puede desvanecerse del mapa.
      orderDecisionStatus: {},
      layers: { orders: [], zones: [], accidentHotspots: [], routes: [] },
    };

    function emptyAgent() {
      return {
        earnings: 0, completed: 0, rejected: 0, timeouts: 0, active: 0,
        position: null, current: null, elapsed: 0, duration: 1, events: ["CLEAR"],
        seenDecisions: new Set(), route: [], remainingRoute: [], prevKpi: {},
        lastRouteIndex: null,
        acceptedRiskSum: 0, acceptedRiskCount: 0, riskyAccepted: 0, safetyRejections: 0,
        deliveredLate: 0,
        // Para detectar, comparando contra el tick anterior, el instante
        // exacto en que este agente recoge o entrega un pedido (no solo
        // que lo aceptó) y animarlo/loguearlo una sola vez.
        pickupLoggedOrderId: null,
      };
    }
