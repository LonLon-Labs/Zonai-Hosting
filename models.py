import sqlite3
import os
from datetime import datetime, timedelta

DB_PATH = os.environ.get("TOTK_DB_PATH", "totk_rooms.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_key       TEXT NOT NULL,
    creator_ip      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'stopped',  -- running | stopped | deleted
    port            INTEGER,
    duration_hours  REAL,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    expires_at      TEXT,
    extended        INTEGER NOT NULL DEFAULT 0,
    last_stopped_at TEXT,
    last_log_scan   TEXT,
    deleted         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS port_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    port         INTEGER NOT NULL,
    released_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id     INTEGER,
    event_type  TEXT NOT NULL,
    ip          TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

CREATE TABLE IF NOT EXISTS players (
    room_id     INTEGER NOT NULL,
    uid         TEXT NOT NULL,
    nickname    TEXT,
    first_seen  TEXT,
    last_seen   TEXT,
    online      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (room_id, uid)
);
"""


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _ensure_column(conn, "rooms", "pvp_enabled", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "rooms", "max_upload_per_second", "INTEGER NOT NULL DEFAULT 10000000")
    _ensure_column(conn, "rooms", "romfs_modified", "INTEGER NOT NULL DEFAULT 0")
    if not get_setting(conn, "_pvp_default_fixed"):
        # pvp_enabled was briefly added with DEFAULT 1 (PVP "on") before
        # this was corrected to DEFAULT 0 -- the room's base/untouched
        # state is actually PVP disabled. If that old default already
        # landed in the DB on a prior run, clear it back to 0 once. Never
        # runs again after this, so it won't stomp a real toggle later.
        conn.execute("UPDATE rooms SET pvp_enabled = 0 WHERE pvp_enabled = 1")
        set_setting(conn, "_pvp_default_fixed", "1")
    conn.commit()
    conn.close()


def _ensure_column(conn, table, column, decl):
    """Idempotent 'ALTER TABLE ADD COLUMN' for simple migrations -- SQLite
    has no IF NOT EXISTS for columns, so check PRAGMA table_info first."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def now_iso():
    return datetime.utcnow().isoformat()


def log_event(conn, room_id, event_type, ip=None, detail=None):
    conn.execute(
        "INSERT INTO events (room_id, event_type, ip, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (room_id, event_type, ip, detail, now_iso()),
    )
    conn.commit()


def active_room_count_for_ip(conn, ip):
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM rooms WHERE creator_ip = ? AND deleted = 0",
        (ip,),
    ).fetchone()
    return row["c"]


def get_room(conn, room_id):
    return conn.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()


def rooms_for_owner(conn, owner_key):
    return conn.execute(
        "SELECT * FROM rooms WHERE owner_key = ? AND deleted = 0 ORDER BY id",
        (owner_key,),
    ).fetchall()


def allocate_port(conn):
    """Random free port in [11000, 13000], excluding ports currently in
    use by a running room and ports released within the last 7 days."""
    import random

    in_use = {
        r["port"]
        for r in conn.execute(
            "SELECT port FROM rooms WHERE status = 'running' AND port IS NOT NULL"
        ).fetchall()
    }
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    cooling = {
        r["port"]
        for r in conn.execute(
            "SELECT DISTINCT port FROM port_history WHERE released_at > ?",
            (cutoff,),
        ).fetchall()
    }
    candidates = [p for p in range(11000, 13001) if p not in in_use and p not in cooling]
    if not candidates:
        raise RuntimeError("No ports available right now -- try again later.")
    return random.choice(candidates)


def release_port(conn, port):
    if port is None:
        return
    conn.execute(
        "INSERT INTO port_history (port, released_at) VALUES (?, ?)",
        (port, now_iso()),
    )
    conn.commit()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def all_rooms(conn):
    return conn.execute("SELECT * FROM rooms ORDER BY id DESC").fetchall()


def upsert_player_seen(conn, room_id, uid, nickname, online, ts):
    conn.execute(
        """
        INSERT INTO players (room_id, uid, nickname, first_seen, last_seen, online)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(room_id, uid) DO UPDATE SET
            nickname = excluded.nickname,
            last_seen = excluded.last_seen,
            online = excluded.online
        """,
        (room_id, uid, nickname, ts, ts, 1 if online else 0),
    )
    conn.commit()


def mark_room_players_offline(conn, room_id):
    """Called when a room is stopped -- nobody can be 'online' if the
    container isn't running."""
    conn.execute("UPDATE players SET online = 0 WHERE room_id = ?", (room_id,))
    conn.commit()


def set_players_offline_except(conn, room_id, keep_uids):
    """Mark every currently-online player in a room offline, except the
    given UIDs. Used by the live 'list' roster poll to reconcile who's
    actually still connected."""
    keep_uids = list(keep_uids)
    if keep_uids:
        placeholders = ",".join("?" for _ in keep_uids)
        conn.execute(
            f"UPDATE players SET online = 0 WHERE room_id = ? AND online = 1 "
            f"AND uid NOT IN ({placeholders})",
            (room_id, *keep_uids),
        )
    else:
        conn.execute(
            "UPDATE players SET online = 0 WHERE room_id = ? AND online = 1",
            (room_id,),
        )
    conn.commit()


def players_for_room(conn, room_id):
    return conn.execute(
        "SELECT * FROM players WHERE room_id = ? ORDER BY online DESC, last_seen DESC",
        (room_id,),
    ).fetchall()


def all_players(conn):
    return conn.execute(
        "SELECT * FROM players ORDER BY room_id, online DESC, last_seen DESC"
    ).fetchall()


def get_player(conn, room_id, uid):
    return conn.execute(
        "SELECT * FROM players WHERE room_id = ? AND uid = ?", (room_id, uid)
    ).fetchone()


def set_pvp(conn, room_id, enabled):
    conn.execute(
        "UPDATE rooms SET pvp_enabled = ? WHERE id = ?",
        (1 if enabled else 0, room_id),
    )
    conn.commit()


def set_max_upload_per_second(conn, room_id, value):
    conn.execute(
        "UPDATE rooms SET max_upload_per_second = ? WHERE id = ?",
        (value, room_id),
    )
    conn.commit()


def set_romfs_modified(conn, room_id, modified):
    conn.execute(
        "UPDATE rooms SET romfs_modified = ? WHERE id = ?",
        (1 if modified else 0, room_id),
    )
    conn.commit()
