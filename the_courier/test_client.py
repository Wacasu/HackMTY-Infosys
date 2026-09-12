"""
test_client.py
================
Cliente de consola para probar el WebSocket `/ws/shift-simulation` de
"The Courier" sin necesidad de tener listo el frontend del split-screen.

Uso:
    python test_client.py
    python test_client.py --url ws://127.0.0.1:8000/ws/shift-simulation --seed 42

Mientras corre la simulación, puedes escribir en la terminal para inyectar
eventos sorpresa en vivo:
    rain 0.8        -> HEAVY_RAIN con severidad 0.8
    flood 0.9        -> FLASH_FLOOD con severidad 0.9
    traffic 0.6      -> HEAVY_TRAFFIC con severidad 0.6
    heat 0.7          -> EXTREME_HEAT con severidad 0.7
    clear             -> despeja todos los eventos
    salir             -> cierra la conexión
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Optional

import websockets

EVENT_ALIASES = {
    "rain": "HEAVY_RAIN",
    "lluvia": "HEAVY_RAIN",
    "flood": "FLASH_FLOOD",
    "inundacion": "FLASH_FLOOD",
    "traffic": "HEAVY_TRAFFIC",
    "trafico": "HEAVY_TRAFFIC",
    "heat": "EXTREME_HEAT",
    "calor": "EXTREME_HEAT",
    "clear": "CLEAR",
    "despejado": "CLEAR",
}


def format_tick(message: dict) -> str:
    agent = message.get("agent_name", "?")
    driver = message.get("driver_state", {})
    shift = message.get("shift_state", {})
    in_progress = message.get("current_order_in_progress")

    elapsed_min = shift.get("elapsed_sec", 0) / 60.0
    events = ", ".join(shift.get("active_events", [])) or "CLEAR"

    line = (
        f"[{elapsed_min:6.1f} min] {agent:<18} | "
        f"ganancia=${driver.get('earnings_mxn', 0):>7.2f} | "
        f"completados={driver.get('orders_completed', 0):>2} | "
        f"rechazados={driver.get('orders_rejected', 0):>2} | "
        f"timeouts={driver.get('timeouts_incurred', 0)} | "
        f"activos={len(driver.get('active_orders', []))} | "
        f"clima={events}"
    )
    if in_progress:
        line += f" | entregando pedido #{in_progress.get('order_id')}"
    return line


async def send_console_commands(
    websocket: "websockets.WebSocketClientProtocol", stop_event: asyncio.Event
) -> None:
    """Lee comandos de teclado en un hilo aparte (input() bloquea) y los
    traduce a mensajes `inject_event` sobre el mismo WebSocket ya abierto."""
    loop = asyncio.get_event_loop()
    print(
        "\nComandos disponibles: rain <0-1> | flood <0-1> | traffic <0-1> | "
        "heat <0-1> | clear | salir\n"
    )
    while not stop_event.is_set():
        try:
            raw_command = await loop.run_in_executor(None, input, "> ")
        except (EOFError, KeyboardInterrupt):
            stop_event.set()
            break

        parts = raw_command.strip().lower().split()
        if not parts:
            continue

        command = parts[0]
        if command in ("salir", "exit", "quit"):
            stop_event.set()
            break

        event_type = EVENT_ALIASES.get(command)
        if event_type is None:
            print(f"Comando no reconocido: '{raw_command}'")
            continue

        severity = 0.6
        if len(parts) > 1:
            try:
                severity = float(parts[1])
            except ValueError:
                pass

        await websocket.send(
            json.dumps(
                {"type": "inject_event", "event_type": event_type, "severity": severity}
            )
        )
        print(f"[enviado] evento {event_type} con severidad {severity}")


async def receive_messages(
    websocket: "websockets.WebSocketClientProtocol", stop_event: asyncio.Event
) -> None:
    async for raw_message in websocket:
        message = json.loads(raw_message)
        message_type = message.get("type")

        if message_type == "session_started":
            print(
                f"\nSesión iniciada: {message['session_id']} "
                f"({message['total_orders']} pedidos generados)\n"
            )
        elif message_type == "tick":
            print(format_tick(message))
        elif message_type == "shift_ended":
            print(f">>> Turno finalizado para {message.get('agent_name')}")
        elif message_type == "agent_finished":
            print(f">>> Motor '{message.get('agent_name')}' terminó su turno.")
        elif message_type == "event_ack":
            print(f"[confirmado] evento {message.get('event_type')} aplicado")
        elif message_type == "session_complete":
            print("\n=== Sesión completa: ambos agentes terminaron su turno ===")
            stop_event.set()
            break
        elif message_type == "error":
            print(f"[ERROR del servidor] {message.get('detail')}")
        else:
            print(f"[mensaje sin tipo esperado] {message}")


async def run_client(url: str, config: dict) -> None:
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as websocket:
        await websocket.send(json.dumps(config))

        stop_event = asyncio.Event()
        receiver_task = asyncio.create_task(receive_messages(websocket, stop_event))
        console_task = asyncio.create_task(send_console_commands(websocket, stop_event))

        await stop_event.wait()

        for task in (receiver_task, console_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(receiver_task, console_task, return_exceptions=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cliente de prueba para The Courier")
    parser.add_argument(
        "--url", default="ws://127.0.0.1:8000/ws/shift-simulation", help="URL del WebSocket"
    )
    parser.add_argument("--seed", type=int, default=42, help="random_seed determinista")
    parser.add_argument("--duration", type=int, default=3600, help="Duración del turno en segundos simulados")
    parser.add_argument("--orders", type=int, default=40, help="Número de pedidos a generar")
    parser.add_argument("--tick", type=int, default=30, help="Segundos simulados por tick")
    parser.add_argument(
        "--speed", type=float, default=20.0, help="Segundos simulados por segundo real"
    )
    parser.add_argument("--alpha", type=float, default=0.35, help="Alpha del agente inteligente")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = {
        "random_seed": args.seed,
        "shift_duration_sec": args.duration,
        "num_orders": args.orders,
        "tick_interval_sec": args.tick,
        "time_scale": args.speed,
        "risk_alpha": args.alpha,
    }
    print(f"Conectando a {args.url} con configuración: {config}")
    try:
        asyncio.run(run_client(args.url, config))
    except KeyboardInterrupt:
        print("\nConexión cerrada por el usuario.")


if __name__ == "__main__":
    main()
