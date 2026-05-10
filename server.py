#!/usr/bin/env python3
"""
SwiftChat — Multi-threaded TCP Chat Server with SQLite Database
================================================================
Architecture:
  Browser  ──WebSocket──►  ws_bridge.py  ──TCP──►  server.py  ──►  SQLite DB
                           (port 5050)            (port 5000)

Protocol: Newline-delimited JSON over TCP.

Client → Server messages:
  {"type": "login",  "username": "alice"}
  {"type": "chat",   "to": "bob",   "text": "hi"}   # DM
  {"type": "chat",   "to": null,    "text": "hi"}   # Broadcast

Server → Client messages:
  {"type": "login_ok"}
  {"type": "error",     "text": "..."}
  {"type": "user_list", "users": ["alice","bob"]}
  {"type": "chat",      "from": "alice", "to": null, "text": "hi", "ts": "14:22"}
  {"type": "system",    "text": "alice joined the chat."}
  {"type": "history",   "messages": [...]}           # sent on login
"""

import socket
import threading
import json
import sqlite3
import datetime
import logging
import sys

# ── Configuration ─────────────────────────────────────────────────────────
HOST        = '0.0.0.0'   # Listen on all network interfaces
PORT        = 5000        # TCP port for the chat server
DB_FILE     = 'swiftchat.db'
BUFFER_SIZE = 4096
MAX_MSG_LEN = 2000        # Max characters per message
HISTORY_ON_LOGIN = 30     # Number of recent group messages shown on login

# ── Logging setup ─────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  [%(levelname)-5s]  %(message)s',
    datefmt='%H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('swiftchat.log'),   # Also save logs to file
    ]
)
log = logging.getLogger('SwiftChat')

# ── Shared state (guarded by clients_lock) ────────────────────────────────
clients_lock = threading.Lock()
clients: dict = {}   # username -> ClientHandler

# ══════════════════════════════════════════════════════════════════════════
#  DATABASE LAYER
# ══════════════════════════════════════════════════════════════════════════

def get_db() -> sqlite3.Connection:
    """Open a new SQLite connection (one per thread is safest)."""
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row          # Rows behave like dicts
    conn.execute("PRAGMA journal_mode=WAL") # Better concurrent write performance
    return conn

def init_db() -> None:
    """Create tables if they don't already exist."""
    ddl = """
    -- ── users table ──────────────────────────────────────────────────────
    -- Stores every username that has ever connected.
    CREATE TABLE IF NOT EXISTS users (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT    UNIQUE NOT NULL,
        first_seen  TEXT    NOT NULL,   -- ISO-8601 UTC timestamp
        last_seen   TEXT    NOT NULL    -- updated on every login
    );

    -- ── messages table ────────────────────────────────────────────────────
    -- Stores every chat message permanently.
    -- to_user = NULL  →  group broadcast  (Everyone)
    -- to_user = name  →  private DM
    CREATE TABLE IF NOT EXISTS messages (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        from_user   TEXT    NOT NULL,
        to_user     TEXT,               -- NULL = broadcast
        text        TEXT    NOT NULL,
        timestamp   TEXT    NOT NULL    -- HH:MM display time
    );

    CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_user);
    CREATE INDEX IF NOT EXISTS idx_messages_to   ON messages(to_user);
    """
    conn = get_db()
    conn.executescript(ddl)
    conn.commit()
    conn.close()
    log.info("✔  Database ready  →  %s", DB_FILE)

def db_upsert_user(username: str) -> None:
    """Insert a new user or update last_seen on every login."""
    now = datetime.datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        """
        INSERT INTO users (username, first_seen, last_seen)
        VALUES (?, ?, ?)
        ON CONFLICT(username) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (username, now, now)
    )
    conn.commit()
    conn.close()

def db_save_message(from_user: str, to_user, text: str, ts: str) -> None:
    """Persist one message to the database."""
    conn = get_db()
    conn.execute(
        "INSERT INTO messages (from_user, to_user, text, timestamp) VALUES (?, ?, ?, ?)",
        (from_user, to_user, text, ts)
    )
    conn.commit()
    conn.close()

def db_get_recent_broadcast(limit: int = HISTORY_ON_LOGIN) -> list:
    """Return the most recent group (broadcast) messages for the history replay."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT from_user, to_user, text, timestamp
        FROM   messages
        WHERE  to_user IS NULL
        ORDER  BY id DESC
        LIMIT  ?
        """,
        (limit,)
    ).fetchall()
    conn.close()
    # Reverse so oldest message is first
    return [dict(r) for r in reversed(rows)]

def db_get_all_users() -> list:
    """Return list of all registered usernames (ever connected)."""
    conn = get_db()
    rows = conn.execute("SELECT username, first_seen, last_seen FROM users ORDER BY username").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_user_message_count(username: str) -> int:
    """Return total number of messages sent by a user."""
    conn = get_db()
    row = conn.execute("SELECT COUNT(*) as n FROM messages WHERE from_user = ?", (username,)).fetchone()
    conn.close()
    return row['n'] if row else 0

# ══════════════════════════════════════════════════════════════════════════
#  CLIENT HANDLER
# ══════════════════════════════════════════════════════════════════════════

class ClientHandler(threading.Thread):
    """
    One thread per connected client.
    Reads newline-terminated JSON lines from the socket and dispatches them.
    """

    def __init__(self, conn: socket.socket, addr):
        super().__init__(daemon=True, name=f"client-{addr}")
        self.conn      = conn
        self.addr      = addr
        self.username  = None     # Set after successful login
        self._recv_buf = ''       # Partial-line receive buffer

    # ── Thread entry point ────────────────────────────────────────────────
    def run(self):
        log.info("⟶  New connection  %s", self.addr)
        try:
            while True:
                chunk = self.conn.recv(BUFFER_SIZE)
                if not chunk:
                    break   # Client closed connection
                self._recv_buf += chunk.decode('utf-8', errors='replace')
                # Process every complete line (newline = message boundary)
                while '\n' in self._recv_buf:
                    line, self._recv_buf = self._recv_buf.split('\n', 1)
                    line = line.strip()
                    if line:
                        self._process_line(line)
        except (ConnectionResetError, OSError, BrokenPipeError):
            pass   # Client disconnected ungracefully
        finally:
            self._on_disconnect()

    # ── Incoming message processing ───────────────────────────────────────
    def _process_line(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            self._send({'type': 'error', 'text': 'Malformed JSON.'})
            return

        msg_type = msg.get('type', '')

        if msg_type == 'login':
            self._handle_login(msg)
        elif msg_type == 'chat':
            if not self.username:
                self._send({'type': 'error', 'text': 'You must log in first.'})
            else:
                self._handle_chat(msg)
        else:
            if self.username:   # Only warn if already logged in
                self._send({'type': 'error', 'text': f'Unknown message type: {msg_type}'})

    # ── Login handler ─────────────────────────────────────────────────────
    def _handle_login(self, msg: dict) -> None:
        username = str(msg.get('username', '')).strip()

        # Validate: 1–20 alphanumeric/underscore/hyphen chars
        if not username or len(username) > 20:
            self._send({'type': 'error', 'text': 'Username must be 1–20 characters.'})
            return
        if not all(c.isalnum() or c in ('_', '-') for c in username):
            self._send({'type': 'error', 'text': 'Username: letters, digits, _ or - only.'})
            return

        with clients_lock:
            if username in clients:
                self._send({'type': 'error', 'text': f'"{username}" is already taken.'})
                return
            self.username = username
            clients[username] = self

        # Save / update user in DB
        db_upsert_user(username)
        msg_count = db_user_message_count(username)

        # Acknowledge login
        self._send({'type': 'login_ok'})
        log.info("✔  %s  logged in  (total msgs sent: %d)", username, msg_count)

        # Send recent group chat history so the user sees past messages
        history = db_get_recent_broadcast()
        if history:
            self._send({
                'type': 'history',
                'messages': [
                    {
                        'type': 'chat',
                        'from': m['from_user'],
                        'to':   None,
                        'text': m['text'],
                        'ts':   m['timestamp'],
                    }
                    for m in history
                ]
            })

        # Announce arrival and broadcast updated user list
        broadcast_system(f'{username} joined the chat.', exclude=username)
        broadcast_userlist()

    # ── Chat handler ──────────────────────────────────────────────────────
    def _handle_chat(self, msg: dict) -> None:
        text    = str(msg.get('text', '')).strip()
        to_user = msg.get('to')   # None or 'Everyone' = broadcast

        if not text:
            return
        if len(text) > MAX_MSG_LEN:
            self._send({'type': 'error', 'text': f'Message too long (max {MAX_MSG_LEN} chars).'})
            return

        # Normalise "Everyone" keyword to None (broadcast)
        if to_user == 'Everyone':
            to_user = None

        ts = datetime.datetime.now().strftime('%H:%M')
        db_save_message(self.username, to_user, text, ts)

        packet = {
            'type': 'chat',
            'from': self.username,
            'to':   to_user,
            'text': text,
            'ts':   ts,
        }

        if to_user is None:
            # ── Broadcast to all connected users ──────────────────────────
            broadcast(packet)
        else:
            # ── Direct message ────────────────────────────────────────────
            with clients_lock:
                recipient = clients.get(to_user)

            if recipient is None:
                self._send({'type': 'error', 'text': f'"{to_user}" is not online.'})
                return

            recipient._send(packet)         # Send to recipient
            if to_user != self.username:
                self._send(packet)          # Echo back to sender (own bubble)

    # ── Send a JSON object to this client ─────────────────────────────────
    def _send(self, obj: dict) -> None:
        try:
            payload = json.dumps(obj, ensure_ascii=False) + '\n'
            self.conn.sendall(payload.encode('utf-8'))
        except OSError:
            pass   # Socket was already closed

    # ── Cleanup when client disconnects ───────────────────────────────────
    def _on_disconnect(self) -> None:
        if self.username:
            with clients_lock:
                clients.pop(self.username, None)
            log.info("✘  %s  disconnected", self.username)
            broadcast_system(f'{self.username} left the chat.')
            broadcast_userlist()
        try:
            self.conn.close()
        except OSError:
            pass

# ══════════════════════════════════════════════════════════════════════════
#  BROADCAST HELPERS
# ══════════════════════════════════════════════════════════════════════════

def broadcast(obj: dict, exclude: str = None) -> None:
    """Send a JSON packet to all connected clients (optionally skipping one)."""
    with clients_lock:
        targets = list(clients.values())
    for client in targets:
        if client.username != exclude:
            client._send(obj)

def broadcast_system(text: str, exclude: str = None) -> None:
    """Send a system notification message to all clients."""
    broadcast({'type': 'system', 'text': text}, exclude=exclude)

def broadcast_userlist() -> None:
    """Push the current online user list to every connected client."""
    with clients_lock:
        user_list = sorted(clients.keys())
    broadcast({'type': 'user_list', 'users': user_list})

# ══════════════════════════════════════════════════════════════════════════
#  MAIN — Start the server
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    # 1. Initialise the SQLite database
    init_db()

    # 2. Create the TCP server socket
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen(100)   # Backlog of 100 pending connections

    log.info("═══════════════════════════════════════════")
    log.info("  SwiftChat TCP Server  —  %s:%d", HOST, PORT)
    log.info("  Database             →  %s",     DB_FILE)
    log.info("  Waiting for clients …")
    log.info("═══════════════════════════════════════════")

    try:
        while True:
            conn, addr = server_sock.accept()
            # Spawn a dedicated thread for each new connection
            handler = ClientHandler(conn, addr)
            handler.start()
    except KeyboardInterrupt:
        log.info("\nShutting down gracefully …")
    finally:
        server_sock.close()
        log.info("Server stopped.")

if __name__ == '__main__':
    main()
