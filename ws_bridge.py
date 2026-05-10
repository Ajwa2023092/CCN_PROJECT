#!/usr/bin/env python3
"""
SwiftChat — WebSocket ↔ TCP Bridge
====================================
Browsers speak WebSocket (ws://).
The chat server speaks raw TCP with newline-delimited JSON.

This bridge sits in the middle:
  Browser  ──WS──►  ws_bridge.py (port 5050)  ──TCP──►  server.py (port 5000)

Each WebSocket connection gets its own TCP socket to the chat server,
so the chat server doesn't need to know anything about WebSocket.

Requires:   pip install websockets
Run:        python ws_bridge.py
"""

import asyncio
import websockets
import logging
import sys

# ── Configuration ─────────────────────────────────────────────────────────
WS_HOST  = '0.0.0.0'     # Accept WS connections on all interfaces
WS_PORT  = 5050          # Browser connects to ws://host:5050

TCP_HOST = '127.0.0.1'   # TCP server address (localhost)
TCP_PORT = 5000          # TCP server port (must match server.py)

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  [Bridge]  %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('ws_bridge.log'),
    ]
)
log = logging.getLogger('WS-Bridge')

# ══════════════════════════════════════════════════════════════════════════
#  PER-CLIENT BRIDGE
# ══════════════════════════════════════════════════════════════════════════

async def bridge_client(websocket):
    """
    Handle one WebSocket client.

    Two coroutines run concurrently:
      ws_to_tcp  — reads from WebSocket, writes to TCP server
      tcp_to_ws  — reads from TCP server, writes to WebSocket
    Both exit when either side closes the connection.
    """
    peer = websocket.remote_address
    log.info("⟶  Browser connected   %s", peer)

    # ── Open a TCP connection to the chat server ───────────────────────────
    try:
        reader, writer = await asyncio.open_connection(TCP_HOST, TCP_PORT)
        log.info("   TCP tunnel open  →  %s:%d", TCP_HOST, TCP_PORT)
    except ConnectionRefusedError:
        log.warning("   Chat server not reachable on %s:%d", TCP_HOST, TCP_PORT)
        await websocket.close(1011, 'Chat server is not running.')
        return
    except OSError as exc:
        log.error("   TCP error: %s", exc)
        await websocket.close(1011, 'Internal bridge error.')
        return

    stop_event = asyncio.Event()

    # ── WebSocket → TCP ───────────────────────────────────────────────────
    async def ws_to_tcp():
        """
        Forward every WebSocket text frame to the TCP server.
        The TCP protocol expects newline-terminated JSON, so append '\\n'.
        """
        try:
            async for message in websocket:
                if isinstance(message, str):
                    # Forward the JSON string with a newline delimiter
                    writer.write((message.rstrip('\n') + '\n').encode('utf-8'))
                    await writer.drain()
        except websockets.exceptions.ConnectionClosed:
            pass
        except OSError:
            pass
        finally:
            stop_event.set()

    # ── TCP → WebSocket ───────────────────────────────────────────────────
    async def tcp_to_ws():
        """
        Forward every newline-terminated JSON line from the TCP server
        back to the WebSocket browser client.
        """
        buf = ''
        try:
            while not stop_event.is_set():
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=1.0)
                except asyncio.TimeoutError:
                    continue   # No data yet, check stop_event and retry

                if not data:
                    break   # TCP server closed the connection

                buf += data.decode('utf-8', errors='replace')

                # Deliver all complete messages
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if line:
                        try:
                            await websocket.send(line)
                        except websockets.exceptions.ConnectionClosed:
                            return
        except OSError:
            pass
        finally:
            stop_event.set()

    # ── Run both directions concurrently ──────────────────────────────────
    try:
        await asyncio.gather(ws_to_tcp(), tcp_to_ws())
    except Exception as exc:
        log.debug("Bridge gather exception: %s", exc)
    finally:
        # Clean up TCP connection
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        log.info("✘  Browser disconnected  %s", peer)

# ══════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════

async def main():
    log.info("═══════════════════════════════════════════════════")
    log.info("  SwiftChat WebSocket Bridge")
    log.info("  Listening  →  ws://%s:%d",  WS_HOST, WS_PORT)
    log.info("  Forwarding →  tcp://%s:%d", TCP_HOST, TCP_PORT)
    log.info("  Open  index.html  and connect to ws://localhost:%d", WS_PORT)
    log.info("═══════════════════════════════════════════════════")

    async with websockets.serve(
        bridge_client,
        WS_HOST,
        WS_PORT,
        ping_interval=20,    # WebSocket keep-alive every 20 s
        ping_timeout=10,
        max_size=2**20,      # 1 MB max WebSocket message size
    ):
        await asyncio.Future()   # Run forever until Ctrl+C

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bridge stopped.")
