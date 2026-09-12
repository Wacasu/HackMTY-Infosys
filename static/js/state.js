const AGENT_KEY = {
    greedy_base: "greedy",
    risk_averse_smart: "smart",
};

function emptyAgent() {
    return {
        earnings: 0,
        completed: 0,
        rejected: 0,
        timeouts: 0,
        active: 0,
        position: null,
        current: null,
        elapsed: 0,
        duration: 1,
        events: ["CLEAR"],
        seenDecisions: new Set(),
        incidents: 0,
        zonesCrossed: [],
    };
}

const state = {
    ws: null,
    sessionId: null,
    completed: false,
    sessionReady: false,
    pendingEvent: null,
    speed: 40,
    tickIntervalSec: 30,
    timeScale: 40,
    agents: {
        greedy: emptyAgent(),
        smart: emptyAgent(),
    },
    history: { greedy: [], smart: [] },
    orders: [],
    visibleOrderIds: new Set(),
    layers: { orders: [], zones: [], routes: [] },
};
