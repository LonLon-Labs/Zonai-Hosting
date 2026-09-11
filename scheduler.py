import logging
import re
import time
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler

import models
import docker_manager

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

CONNECT_RE = re.compile(r"^(?P<name>.+?) connected - save UID: (?P<uid>\S+)\s*$")
DISCONNECT_RE = re.compile(r"^(?P<name>.+?) disconnected\s*$")

# Output format of the in-game "list" console command (see commands.py's
# list_players()):
#   " - <name> (save uid=<uid>)"
#   "No players connected."
LIST_LINE_RE = re.compile(r"^-\s*(?P<name>.+?)\s*\(save uid=(?P<uid>\S+)\)\s*$")
LIST_EMPTY_RE = re.compile(r"^No players connected\.\s*$")


def _expire_rooms():
    conn = models.get_db()
    try:
        now = models.now_iso()
        rooms = conn.execute(
            "SELECT * FROM rooms WHERE status = 'running' AND deleted = 0 AND expires_at <= ?",
            (now,),
        ).fetchall()
        for room in rooms:
            try:
                docker_manager.stop_and_remove(room["id"])
            except Exception:
                log.exception("Failed to stop expired room %s", room["id"])
                continue
            models.release_port(conn, room["port"])
            models.mark_room_players_offline(conn, room["id"])
            conn.execute(
                "UPDATE rooms SET status = 'stopped', port = NULL, last_stopped_at = ? WHERE id = ?",
                (now, room["id"]),
            )
            models.log_event(conn, room["id"], "expired")
            conn.commit()
    finally:
        conn.close()


def _scan_connect_disconnect_lines(conn, room):
    """Best-effort: tail new log lines for the connect/disconnect prints so
    first_seen/online status can update between roster polls without
    waiting on the (slower) live 'list' check below. This is NOT relied on
    as the sole source of truth for who's online -- see
    _poll_room_roster_live, which is authoritative and is what actually
    keeps the Players panel correct even if this scan misses something
    (e.g. due to log timing/formatting quirks)."""
    since = None
    if room["last_log_scan"]:
        try:
            since = datetime.fromisoformat(room["last_log_scan"])
        except ValueError:
            since = None
    if since is None and room["started_at"]:
        since = datetime.fromisoformat(room["started_at"])

    text = docker_manager.logs_since(room["id"], since) if since else ""

    now = datetime.utcnow()
    for line in text.splitlines():
        stripped = line.strip()
        m = CONNECT_RE.match(stripped)
        if m:
            models.upsert_player_seen(
                conn, room["id"], m.group("uid"), m.group("name"), True, now.isoformat()
            )
            continue
        m = DISCONNECT_RE.match(stripped)
        if m:
            row = conn.execute(
                "SELECT uid FROM players WHERE room_id = ? AND nickname = ? "
                "ORDER BY last_seen DESC LIMIT 1",
                (room["id"], m.group("name")),
            ).fetchone()
            if row:
                models.upsert_player_seen(
                    conn, room["id"], row["uid"], m.group("name"), False, now.isoformat()
                )

    conn.execute(
        "UPDATE rooms SET last_log_scan = ? WHERE id = ?",
        (now.isoformat(), room["id"]),
    )
    conn.commit()


def _poll_room_roster_live(conn, room):
    """Authoritative roster check: actually run 'list' in the room's
    console and parse the reply -- the exact same mechanism as typing
    'list' into the website's console box, which is confirmed to work.
    Runs every scheduler tick for every running room so the Players panel
    always reflects who's really connected, independent of whether the
    passive connect/disconnect log scan above catches anything."""
    room_id = room["id"]
    try:
        docker_manager.send_console_command(room_id, "list")
    except Exception:
        log.exception("Failed to send 'list' command to room %s", room_id)
        return

    time.sleep(0.75)  # give the server a moment to print its reply

    try:
        text = docker_manager.recent_logs(room_id, tail=80)
    except Exception:
        log.exception("Failed to read logs back from room %s", room_id)
        return

    # Walk the tail backwards collecting the trailing block of "list"
    # output -- this is the reply to the command we *just* sent, since
    # nothing else should be printing after it in the ~0.75s window.
    online = {}
    saw_block = False
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        m = LIST_LINE_RE.match(stripped)
        if m:
            online[m.group("uid")] = m.group("name")
            saw_block = True
            continue
        if LIST_EMPTY_RE.match(stripped):
            saw_block = True
            break
        if saw_block:
            break

    if not saw_block:
        # The reply hadn't landed in the log tail by the time we read it
        # back -- try again next cycle rather than wrongly marking
        # everyone offline based on incomplete output.
        log.warning("No 'list' reply seen yet for room %s; will retry", room_id)
        return

    now = datetime.utcnow().isoformat()
    for uid, nickname in online.items():
        models.upsert_player_seen(conn, room_id, uid, nickname, True, now)
    models.set_players_offline_except(conn, room_id, online.keys())


def _scan_player_rosters():
    conn = models.get_db()
    try:
        rooms = conn.execute(
            "SELECT * FROM rooms WHERE status = 'running' AND deleted = 0"
        ).fetchall()
        for room in rooms:
            try:
                _scan_connect_disconnect_lines(conn, room)
            except Exception:
                log.exception("Connect/disconnect scan failed for room %s", room["id"])
            try:
                _poll_room_roster_live(conn, room)
            except Exception:
                log.exception("Live roster poll failed for room %s", room["id"])
    finally:
        conn.close()


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(_expire_rooms, "interval", seconds=60, id="expire_rooms")
    scheduler.add_job(_scan_player_rosters, "interval", seconds=30, id="scan_player_rosters")
    scheduler.start()
    return scheduler
