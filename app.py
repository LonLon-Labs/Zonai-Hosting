import os
import io
import secrets
import zipfile
from datetime import datetime, timedelta
from functools import wraps
from dotenv import load_dotenv

from flask import (
    Flask, request, render_template, redirect, url_for, jsonify, make_response, session, abort, send_file
)
from werkzeug.utils import secure_filename

import models
import docker_manager
import save_converter
from scheduler import start_scheduler

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("TOTK_SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1GB hard cap (matches largest upload, romfs mods)

ADMIN_PASSWORD = os.environ.get("TOTK_ADMIN_PASSWORD")  # set this in production!
DEFAULT_PUBLIC_HOST = os.environ.get("TOTK_PUBLIC_HOST", "")  # fallback if no DB setting yet
MAX_ROOMS_PER_IP = 2
MAX_INITIAL_HOURS = 8
MAX_EXTEND_HOURS = 4
MAX_TOTAL_HOURS = 48
EXTEND_WINDOW_MINUTES = 30
MAX_SAVE_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB
MAX_ROMFS_UPLOAD_BYTES = 1024 * 1024 * 1024  # 1GB, mod texture packs can be large
DEFAULT_MAX_UPLOAD_PER_SECOND = 10_000_000  # matches serverCreator.ktml's own default
MIN_MAX_UPLOAD_PER_SECOND = 50_000  # ~50KB/s -- below this the server itself is barely usable
MAX_MAX_UPLOAD_PER_SECOND = 100_000_000

models.init_db()
os.makedirs(docker_manager.ROOMS_ROOT, exist_ok=True)
start_scheduler()


# ---------- helpers ----------

def get_public_host(conn):
    return models.get_setting(conn, "public_host", DEFAULT_PUBLIC_HOST) or "your-server-ip"


def get_client_ip():
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr


def get_owner_key():
    key = request.cookies.get("totk_owner_key")
    return key


def require_owner_key(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        # Admin can access any room without an owner cookie
        if session.get("is_admin"):
            return f(*args, **kwargs, owner_key=None)

        key = get_owner_key()
        if not key:
            abort(403)
        return f(*args, **kwargs, owner_key=key)
    return wrapper


def owned_room_or_403(conn, room_id, owner_key):
    room = models.get_room(conn, room_id)
    if room is None or room["deleted"]:
        abort(404)

    # Admin can view/operate any room
    if session.get("is_admin"):
        return room

    if room["owner_key"] != owner_key:
        abort(403)
    return room


def ensure_owner_cookie(resp):
    if not request.cookies.get("totk_owner_key"):
        key = secrets.token_hex(24)
        resp.set_cookie("totk_owner_key", key, max_age=60 * 60 * 24 * 365, httponly=False, samesite="Lax")
    return resp


@app.after_request
def _attach_cookie(resp):
    return ensure_owner_cookie(resp)


# ---------- public pages ----------

@app.route("/")
def index():
    owner_key = get_owner_key()
    conn = models.get_db()
    try:
        rooms = models.rooms_for_owner(conn, owner_key) if owner_key else []
        return render_template(
            "index.html",
            rooms=rooms,
            max_rooms=MAX_ROOMS_PER_IP,
            max_hours=MAX_INITIAL_HOURS,
            now=datetime.utcnow(),
            public_host=get_public_host(conn),
        )
    finally:
        conn.close()


@app.route("/rooms", methods=["POST"])
def create_room():
    ip = get_client_ip()
    owner_key = get_owner_key() or secrets.token_hex(24)
    try:
        hours = float(request.form.get("hours", 1))
    except ValueError:
        hours = 1
    hours = max(0.25, min(hours, MAX_INITIAL_HOURS))

    conn = models.get_db()
    try:
        # Admin can bypass the 2‑room limit
        if not session.get("is_admin"):
            if models.active_room_count_for_ip(conn, ip) >= MAX_ROOMS_PER_IP:
                return render_template("error.html", message="You already have the maximum of 2 servers. Delete a server to create a new one."), 400

        now = models.now_iso()
        cur = conn.execute(
            "INSERT INTO rooms (owner_key, creator_ip, status, duration_hours, created_at, pvp_enabled, max_upload_per_second) "
            "VALUES (?, ?, 'stopped', ?, ?, 0, ?)",
            (owner_key, ip, hours, now, DEFAULT_MAX_UPLOAD_PER_SECOND),
        )
        conn.commit()
        room_id = cur.lastrowid
        models.log_event(conn, room_id, "created", ip)

        _start_room(conn, room_id, hours)
    finally:
        conn.close()

    resp = make_response(redirect(url_for("index")))
    resp.set_cookie("totk_owner_key", owner_key, max_age=60 * 60 * 24 * 365, httponly=False, samesite="Lax")
    return resp


def _start_room(conn, room_id, hours):
    """hours <= 0 means indefinite (no auto-expiry) -- admin-only, see
    start_room()/admin_start_room(). _expire_rooms() in scheduler.py never
    matches a NULL expires_at, so these rooms are simply left alone.

    Wrapped in docker_manager.room_lock so a double-clicked Start button
    (or any other near-simultaneous call for the same room) can't race two
    requests into allocating two ports / creating two containers for one
    room -- the second call re-checks status once it has the lock and
    becomes a no-op if the first one already started it."""
    with docker_manager.room_lock(room_id):
        room = models.get_room(conn, room_id)
        if room["status"] == "running":
            return
        port = models.allocate_port(conn)
        docker_manager.create_and_start(room_id, port)
        # Explicitly (re)apply the room's stored PVP state to the container's
        # persistent data on every start. Without this, a fresh room's actual
        # on-disk PlayerPuppet pack depends on winning a race against
        # docker-entrypoint.sh's first-run seed copy (see its comments) --
        # this makes the *stored* pvp_enabled value (0/disabled by default
        # for every new room) authoritative regardless of that race.
        docker_manager.set_pvp_enabled(room_id, bool(room["pvp_enabled"]))
        # Same reasoning as the PVP line above -- re-apply the room's
        # stored upload-speed cap on every start so it can't be lost to
        # the same first-run seeding race.
        docker_manager.set_max_upload_per_second(room_id, room["max_upload_per_second"] or DEFAULT_MAX_UPLOAD_PER_SECOND)
        now = datetime.utcnow()
        expires_at = (now + timedelta(hours=hours)).isoformat() if hours and hours > 0 else None
        conn.execute(
            "UPDATE rooms SET status = 'running', port = ?, started_at = ?, expires_at = ? "
            "WHERE id = ?",
            (port, now.isoformat(), expires_at, room_id),
        )
        conn.commit()
        models.log_event(conn, room_id, "started", detail=f"port={port}" + (", indefinite" if expires_at is None else ""))


@app.route("/rooms/<int:room_id>")
@require_owner_key
def room_detail(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        is_admin = bool(session.get("is_admin"))
        can_extend = False
        if room["status"] == "running" and room["expires_at"]:
            if is_admin:
                can_extend = True
            else:
                expires_at = datetime.fromisoformat(room["expires_at"])
                started_at = datetime.fromisoformat(room["started_at"])
                already_hours = (expires_at - started_at).total_seconds() / 3600
                within_window = datetime.utcnow() >= expires_at - timedelta(minutes=EXTEND_WINDOW_MINUTES)
                can_extend = within_window and already_hours < MAX_TOTAL_HOURS
        return render_template(
            "room.html",
            room=room,
            can_extend=can_extend,
            is_admin=is_admin,
            max_extend_hours=MAX_EXTEND_HOURS,
            max_total_hours=MAX_TOTAL_HOURS,
            max_hours=MAX_INITIAL_HOURS,
            logs=docker_manager.recent_logs(room_id) if room["status"] == "running" else "",
            public_host=get_public_host(conn),
            players=models.players_for_room(conn, room_id),
        )
    finally:
        conn.close()


@app.route("/rooms/<int:room_id>/start", methods=["POST"])
@require_owner_key
def start_room(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] == "running":
            return redirect(url_for("room_detail", room_id=room_id))
        try:
            hours = float(request.form.get("hours", room["duration_hours"] or 1))
        except ValueError:
            hours = room["duration_hours"] or 1
        if session.get("is_admin"):
            hours = max(0, hours)  # 0 (or below) = indefinite; no upper cap for admin
        else:
            hours = max(0.25, min(hours, MAX_INITIAL_HOURS))
        _start_room(conn, room_id, hours)
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/stop", methods=["POST"])
@require_owner_key
def stop_room(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        docker_manager.stop_and_remove(room_id)
        models.release_port(conn, room["port"])
        conn.execute(
            "UPDATE rooms SET status = 'stopped', port = NULL, last_stopped_at = ? WHERE id = ?",
            (models.now_iso(), room_id),
        )
        conn.commit()
        models.log_event(conn, room_id, "stopped")
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/restart", methods=["POST"])
@require_owner_key
def restart_room(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] != "running":
            return render_template("error.html", message="Room isn't running."), 400
        docker_manager.restart(room_id)
        models.log_event(conn, room_id, "restarted")
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/delete", methods=["POST"])
@require_owner_key
def delete_room(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        docker_manager.delete_room_data(room_id)
        if room["port"]:
            models.release_port(conn, room["port"])
        conn.execute(
            "UPDATE rooms SET status = 'deleted', deleted = 1, port = NULL WHERE id = ?",
            (room_id,),
        )
        conn.commit()
        models.log_event(conn, room_id, "deleted")
    finally:
        conn.close()
    return redirect(url_for("index"))


@app.route("/rooms/<int:room_id>/extend", methods=["POST"])
@require_owner_key
def extend_room(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] != "running":
            return render_template("error.html", message="Room isn't running."), 400
        if not room["expires_at"]:
            return render_template("error.html", message="This room has no expiry -- nothing to extend."), 400

        is_admin = bool(session.get("is_admin"))
        expires_at = datetime.fromisoformat(room["expires_at"])

        try:
            extra_hours = float(request.form.get("hours", MAX_EXTEND_HOURS))
        except ValueError:
            extra_hours = MAX_EXTEND_HOURS

        if is_admin:
            extra_hours = max(0.25, extra_hours)  # no window or total-hours cap for admin
        else:
            if datetime.utcnow() < expires_at - timedelta(minutes=EXTEND_WINDOW_MINUTES):
                return render_template(
                    "error.html",
                    message=f"You can only extend within {EXTEND_WINDOW_MINUTES} minutes of expiry.",
                ), 400
            started_at = datetime.fromisoformat(room["started_at"])
            already_hours = (expires_at - started_at).total_seconds() / 3600
            remaining_allowed = MAX_TOTAL_HOURS - already_hours
            if remaining_allowed < 0.25:
                return render_template(
                    "error.html",
                    message=f"This room has already reached the {MAX_TOTAL_HOURS}-hour maximum.",
                ), 400
            extra_hours = max(0.25, min(extra_hours, MAX_EXTEND_HOURS, remaining_allowed))

        new_expiry = expires_at + timedelta(hours=extra_hours)
        conn.execute(
            "UPDATE rooms SET expires_at = ? WHERE id = ?",
            (new_expiry.isoformat(), room_id),
        )
        conn.commit()
        models.log_event(conn, room_id, "extended", detail=f"+{extra_hours}h" + (" (admin)" if is_admin else ""))
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/upload_save", methods=["POST"])
@require_owner_key
def upload_save(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] == "running":
            return render_template("error.html", message="Stop the room before uploading a save."), 400
        f = request.files.get("save_file")
        if f is None or f.filename == "":
            return render_template("error.html", message="No file selected.", sound="warning"), 400
        filename = secure_filename(f.filename)
        if not filename.lower().endswith(".sav"):
            return render_template("error.html", message="Only .sav files are accepted.", sound="warning"), 400
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if size > MAX_SAVE_UPLOAD_BYTES:
            return render_template("error.html", message="Save file is too large.", sound="warning"), 400
        docker_manager.replace_save(room_id, f)
        models.log_event(conn, room_id, "save_uploaded", detail=filename)
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/upload_romfs", methods=["POST"])
@require_owner_key
def upload_romfs(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] == "running":
            return render_template("error.html", message="Stop the room before uploading mods."), 400
        f = request.files.get("romfs_file")
        if f is None or f.filename == "":
            return render_template("error.html", message="No file selected."), 400
        filename = secure_filename(f.filename)
        if not filename.lower().endswith(".zip"):
            return render_template("error.html", message="Please upload a .zip file containing your Romfs mod files."), 400
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if size > MAX_ROMFS_UPLOAD_BYTES:
            return render_template("error.html", message="Mod archive is too large."), 400
        try:
            count = docker_manager.replace_romfs(room_id, f)
        except zipfile.BadZipFile:
            return render_template("error.html", message="That doesn't look like a valid .zip file."), 400
        models.log_event(conn, room_id, "romfs_uploaded", detail=f"{filename} ({count} files)")
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/logs")
@require_owner_key
def room_logs(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] != "running":
            return jsonify({"logs": ""})
        return jsonify({"logs": docker_manager.recent_logs(room_id, tail=60)})
    finally:
        conn.close()


@app.route("/rooms/<int:room_id>/command", methods=["POST"])
@require_owner_key
def run_command(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] != "running":
            return jsonify({"error": "Room isn't running."}), 400
        command_text = (request.form.get("command") or "").strip()
        if not command_text:
            return jsonify({"error": "No command given."}), 400
        try:
            docker_manager.send_console_command(room_id, command_text)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        models.log_event(conn, room_id, "command_run", detail=command_text[:200])
        import time
        time.sleep(1.0)  # give the server a moment to respond before we read logs back
        logs = docker_manager.recent_logs(room_id, tail=40)
        return jsonify({"logs": logs})
    finally:
        conn.close()


@app.route("/rooms/<int:room_id>/players/<uid>/kick", methods=["POST"])
@require_owner_key
def kick_player_route(room_id, owner_key, uid):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        player = models.get_player(conn, room_id, uid)
        if room["status"] != "running" or player is None:
            return render_template("error.html", message="Room must be running and player must exist."), 400
        try:
            # uid, not nickname -- nicknames aren't unique, and commands.py
            # resolves either one, so this always targets the exact player.
            docker_manager.send_console_command(room_id, f"kick {uid}")
        except Exception as e:
            return render_template("error.html", message=str(e)), 500
        models.log_event(conn, room_id, "player_kicked", detail=player["nickname"])
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/players/<uid>/ban", methods=["POST"])
@require_owner_key
def ban_player_route(room_id, owner_key, uid):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        player = models.get_player(conn, room_id, uid)
        if room["status"] != "running" or player is None:
            return render_template("error.html", message="Ban requires the room to be running and the player online."), 400
        try:
            # banip (uid + IP), matching this button's historical behavior.
            # A same-network-only "ban" (uid only, doesn't block housemates)
            # is also available via the console if that's ever wanted here.
            docker_manager.send_console_command(room_id, f"banip {uid}")
        except Exception as e:
            return render_template("error.html", message=str(e)), 500
        models.log_event(conn, room_id, "player_banned", detail=player["nickname"])
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/players/<uid>/delete_save", methods=["POST"])
@require_owner_key
def delete_player_save_route(room_id, owner_key, uid):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] == "running":
            return render_template("error.html", message="Stop the room before deleting a player's save."), 400
        docker_manager.delete_player_save(room_id, uid)
        models.log_event(conn, room_id, "player_save_deleted", detail=uid)
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/players/<uid>/upload_save", methods=["POST"])
@require_owner_key
def upload_player_save(room_id, owner_key, uid):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] == "running":
            return render_template("error.html", message="Stop the room before uploading a player's save."), 400
        f = request.files.get("save_file")
        if f is None or f.filename == "":
            return render_template("error.html", message="No file selected.", sound="warning"), 400
        filename = secure_filename(f.filename)
        is_sav = filename.lower().endswith(".sav")
        is_ktml = filename.lower().endswith(".ktml")
        if not (is_sav or is_ktml):
            return render_template("error.html", message="Only .ktml or .sav files are accepted.", sound="warning"), 400
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if size > MAX_SAVE_UPLOAD_BYTES:
            return render_template("error.html", message="Save file is too large.", sound="warning"), 400

        raw = f.read()
        converted = False  # only .sav uploads actually run the sav<->ktml converter
        if is_sav:
            try:
                ktml_text = save_converter.progress_sav_to_ktml(raw)
            except save_converter.ConversionError as e:
                return render_template("error.html", message=f"Couldn't convert this save to .ktml: {e}", sound="error"), 400
            content_bytes = ktml_text.encode("utf-8")
            converted = True
        else:
            try:
                save_converter.parse_ktml(raw.decode("utf-8", errors="strict"))
            except Exception as e:
                return render_template("error.html", message=f"This doesn't look like a valid .ktml save: {e}", sound="error"), 400
            content_bytes = raw

        # The file on disk is always named after the player's UID -- that's
        # how SaveServer.getClientUID()/save() looks it up server-side --
        # regardless of what the uploaded file was called.
        docker_manager.replace_player_save(room_id, uid, content_bytes)
        models.log_event(conn, room_id, "player_save_uploaded", detail=f"{uid} ({filename})")
    finally:
        conn.close()
    # A .sav upload just ran through the sav -> ktml converter -- cue the
    # success sound the same way the .sav download side does. A plain
    # .ktml upload didn't touch the converter, so it stays silent.
    if converted:
        return redirect(url_for("room_detail", room_id=room_id, sound="success"))
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/download_save")
@require_owner_key
def download_main_save(room_id, owner_key):
    conn = models.get_db()
    try:
        owned_room_or_403(conn, room_id, owner_key)
    finally:
        conn.close()
    docker_manager.fix_save_permissions(room_id)
    path = docker_manager.main_save_path(room_id)
    if not os.path.isfile(path):
        return render_template("error.html", message="No server save file exists yet.", sound="warning"), 404
    if request.args.get("format") == "sav":
        with open(path, "r", encoding="utf-8", errors="strict") as f:
            server_ktml_text = f.read()
        try:
            sav_bytes = save_converter.ktml_to_progress_sav(server_ktml_text)
        except save_converter.ConversionError as e:
            return render_template("error.html", message=f"Couldn't convert this save to .sav: {e}", sound="error"), 400
        return send_file(
            io.BytesIO(sav_bytes),
            as_attachment=True,
            download_name=f"totk-server-{room_id}-save.sav",
            mimetype="application/octet-stream",
        )
    return send_file(path, as_attachment=True, download_name=f"totk-server-{room_id}-save.ktml")


@app.route("/rooms/<int:room_id>/players/<uid>/download_save")
@require_owner_key
def download_player_save(room_id, owner_key, uid):
    conn = models.get_db()
    try:
        owned_room_or_403(conn, room_id, owner_key)
        player = models.get_player(conn, room_id, uid)
    finally:
        conn.close()
    if player is None:
        abort(404)
    docker_manager.fix_save_permissions(room_id)
    path = docker_manager.player_save_path(room_id, uid)
    if not os.path.isfile(path):
        return render_template("error.html", message="No save file exists for this player yet.", sound="warning"), 404
    safe_name = secure_filename(player["nickname"] or uid) or uid
    if request.args.get("format") == "sav":
        # A player's actual usable save is a merge of the server-wide save
        # (shared/world progress) with this player's own overrides -- a
        # standalone conversion of just their file would be missing
        # everything that only lives in the server save. Mirrors exactly
        # how the real server generates a specific player's progress.sav.
        server_path = docker_manager.main_save_path(room_id)
        if not os.path.isfile(server_path):
            return render_template("error.html", message="No server save file exists yet to merge this player's save with.", sound="warning"), 400
        with open(server_path, "r", encoding="utf-8", errors="strict") as f:
            server_ktml_text = f.read()
        with open(path, "r", encoding="utf-8", errors="strict") as f:
            player_ktml_text = f.read()
        try:
            sav_bytes = save_converter.ktml_to_progress_sav(server_ktml_text, player_ktml_text)
        except save_converter.ConversionError as e:
            return render_template("error.html", message=f"Couldn't convert this save to .sav: {e}", sound="error"), 400
        return send_file(
            io.BytesIO(sav_bytes),
            as_attachment=True,
            download_name=f"{safe_name}.sav",
            mimetype="application/octet-stream",
        )
    return send_file(path, as_attachment=True, download_name=f"{safe_name}.ktml")


@app.route("/rooms/<int:room_id>/pvp", methods=["POST"])
@require_owner_key
def set_pvp(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] == "running":
            return render_template("error.html", message="Stop the room before changing PVP."), 400
        enabled = request.form.get("enabled") == "1"
        docker_manager.set_pvp_enabled(room_id, enabled)
        models.set_pvp(conn, room_id, enabled)
        models.log_event(conn, room_id, "pvp_enabled" if enabled else "pvp_disabled")
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/rooms/<int:room_id>/max_upload", methods=["POST"])
@require_owner_key
def set_max_upload(room_id, owner_key):
    conn = models.get_db()
    try:
        room = owned_room_or_403(conn, room_id, owner_key)
        if room["status"] == "running":
            return render_template("error.html", message="Stop the room before changing the upload speed."), 400
        try:
            value = int(request.form.get("max_upload_per_second", DEFAULT_MAX_UPLOAD_PER_SECOND))
        except ValueError:
            return render_template("error.html", message="Upload speed must be a whole number of bytes/second."), 400
        value = max(MIN_MAX_UPLOAD_PER_SECOND, min(value, MAX_MAX_UPLOAD_PER_SECOND))
        docker_manager.set_max_upload_per_second(room_id, value)
        models.set_max_upload_per_second(conn, room_id, value)
        models.log_event(conn, room_id, "max_upload_changed", detail=str(value))
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


# ---------- admin ----------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if ADMIN_PASSWORD and request.form.get("password") == ADMIN_PASSWORD:
            session.permanent = True
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html", error="Wrong password."), 401
    return render_template("admin_login.html", error=None)


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


app.permanent_session_lifetime = timedelta(days=30)


@app.route("/admin")
@require_admin
def admin_dashboard():
    conn = models.get_db()
    try:
        total_rooms = conn.execute("SELECT COUNT(*) c FROM rooms").fetchone()["c"]
        all_rooms = models.all_rooms(conn)
        recent_events = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT 100"
        ).fetchall()
        by_ip = conn.execute(
            "SELECT creator_ip, COUNT(*) c FROM rooms WHERE deleted = 0 GROUP BY creator_ip ORDER BY c DESC"
        ).fetchall()
        return render_template(
            "admin.html",
            total_rooms=total_rooms,
            active_rooms=[r for r in all_rooms if r["status"] == "running"],
            all_rooms=all_rooms,
            recent_events=recent_events,
            by_ip=by_ip,
            public_host=get_public_host(conn),
        )
    finally:
        conn.close()


@app.route("/admin/settings", methods=["POST"])
@require_admin
def admin_settings():
    conn = models.get_db()
    try:
        host = (request.form.get("public_host") or "").strip()
        models.set_setting(conn, "public_host", host)
    finally:
        conn.close()
    return redirect(url_for("admin_dashboard"))


def _admin_room_or_404(conn, room_id):
    room = models.get_room(conn, room_id)
    if room is None:
        abort(404)
    return room


@app.route("/admin/rooms/<int:room_id>/start", methods=["POST"])
@require_admin
def admin_start_room(room_id):
    conn = models.get_db()
    try:
        room = _admin_room_or_404(conn, room_id)
        if room["status"] != "running":
            _start_room(conn, room_id, room["duration_hours"] or MAX_INITIAL_HOURS)
            models.log_event(conn, room_id, "admin_started")
    finally:
        conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/<int:room_id>/stop", methods=["POST"])
@require_admin
def admin_stop_room(room_id):
    conn = models.get_db()
    try:
        room = _admin_room_or_404(conn, room_id)
        docker_manager.stop_and_remove(room_id)
        models.release_port(conn, room["port"])
        conn.execute(
            "UPDATE rooms SET status = 'stopped', port = NULL, last_stopped_at = ? WHERE id = ?",
            (models.now_iso(), room_id),
        )
        conn.commit()
        models.log_event(conn, room_id, "admin_stopped")
    finally:
        conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/<int:room_id>/restart", methods=["POST"])
@require_admin
def admin_restart_room(room_id):
    conn = models.get_db()
    try:
        room = _admin_room_or_404(conn, room_id)
        if room["status"] == "running":
            docker_manager.restart(room_id)
            models.log_event(conn, room_id, "admin_restarted")
    finally:
        conn.close()
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/rooms/<int:room_id>/recreate", methods=["POST"])
@require_admin
def admin_recreate_room(room_id):
    conn = models.get_db()
    try:
        room = _admin_room_or_404(conn, room_id)
        if room["status"] != "running":
            return render_template("error.html", message="Room isn't running."), 400
        try:
            docker_manager.recreate_container(room_id, room["port"])
        except Exception as e:
            return render_template("error.html", message=f"Recreate failed: {e}"), 500
        models.log_event(conn, room_id, "admin_recreated")
    finally:
        conn.close()
    return redirect(url_for("room_detail", room_id=room_id))


@app.route("/admin/rooms/<int:room_id>/delete", methods=["POST"])
@require_admin
def admin_delete_room(room_id):
    conn = models.get_db()
    try:
        room = _admin_room_or_404(conn, room_id)
        docker_manager.delete_room_data(room_id)
        if room["port"]:
            models.release_port(conn, room["port"])
        conn.execute(
            "UPDATE rooms SET status = 'deleted', deleted = 1, port = NULL WHERE id = ?",
            (room_id,),
        )
        conn.commit()
        models.log_event(conn, room_id, "admin_deleted")
    finally:
        conn.close()
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5100)
