import os
import shutil
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from datetime import timezone
import docker
from docker.errors import APIError, NotFound
from dotenv import load_dotenv

load_dotenv()

IMAGE = os.environ.get("TOTK_IMAGE", "wesleyhellewell/totk_online_multiplayer_server:latest")
ROOMS_ROOT = os.environ.get("TOTK_ROOMS_ROOT", "/opt/totk-rooms")
CONTAINER_PORT = 10014  # fixed port the server listens on inside the container

_client = None

_room_locks = {}
_room_locks_guard = threading.Lock()


@contextmanager
def room_lock(room_id):
    """Serialize container lifecycle operations (start/stop/restart/delete)
    for a single room. Without this, two near-simultaneous requests for the
    same room (a double-clicked Start button, a slow page load plus a
    retry, etc.) can both pass the app's 'is it already running?' check
    before either has updated the database, and both then race to `docker
    create --name totk-server-<id>` -- the loser gets a 409 Conflict since
    the winner already holds that container name."""
    with _room_locks_guard:
        lock = _room_locks.get(room_id)
        if lock is None:
            lock = threading.RLock()
            _room_locks[room_id] = lock
    with lock:
        yield


def client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def container_name(room_id):
    return f"totk-server-{room_id}"


def room_dir(room_id):
    return os.path.join(ROOMS_ROOT, str(room_id))


def ensure_room_dir(room_id):
    d = room_dir(room_id)
    os.makedirs(d, exist_ok=True)
    return d


def get_container(room_id):
    try:
        return client().containers.get(container_name(room_id))
    except NotFound:
        return None


def create_and_start(room_id, port):
    """Create (or recreate) the container for a room bound to `port`,
    with its persistent data directory mounted at /app/run."""
    with room_lock(room_id):
        name = container_name(room_id)
        existing = get_container(room_id)
        if existing is not None:
            try:
                existing.remove(force=True)
            except NotFound:
                pass

        data_dir = ensure_room_dir(room_id)
        run_kwargs = dict(
            name=name,
            detach=True,
            stdin_open=True,
            tty=True,
            ports={f"{CONTAINER_PORT}/tcp": port},
            volumes={data_dir: {"bind": "/app/run", "mode": "rw"}},
            restart_policy={"Name": "no"},
        )
        try:
            container = client().containers.run(IMAGE, **run_kwargs)
        except APIError as e:
            if e.response is not None and e.response.status_code == 409:
                # A container is still sitting on this name despite the
                # check above -- e.g. a stop/removal from another code
                # path that didn't fully finish. Force it out and retry
                # once rather than surfacing a 500 to the user.
                stale = get_container(room_id)
                if stale is not None:
                    try:
                        stale.remove(force=True)
                    except NotFound:
                        pass
                container = client().containers.run(IMAGE, **run_kwargs)
            else:
                raise
        return container


def stop_and_remove(room_id):
    with room_lock(room_id):
        container = get_container(room_id)
        if container is None:
            return
        try:
            container.stop(timeout=15)
        except Exception:
            pass
        try:
            container.remove(force=True)
        except NotFound:
            pass
        except APIError as e:
            if "already in progress" in str(e):
                # A removal is already in flight for this container (e.g. it
                # raced a previous call, or Docker is auto-removing it). That
                # removal will finish on its own shortly -- poll for the
                # container to actually disappear instead of failing the
                # request outright.
                for _ in range(10):
                    time.sleep(1)
                    try:
                        client().containers.get(container.name)
                    except NotFound:
                        return
                raise
            else:
                raise


def restart(room_id):
    with room_lock(room_id):
        container = get_container(room_id)
        if container is None:
            raise RuntimeError("Container is not running.")
        container.restart(timeout=15)


def pull_latest_image():
    """Pull the latest version of the configured server image from its
    registry. `containers.run()` only pulls automatically when the image
    is completely absent locally, so a plain restart/start keeps using
    whatever build is already cached -- this is what actually fetches a
    newly published image."""
    client().api.pull(IMAGE)


def recreate_container(room_id, port):
    """Admin action: pull the latest image and recreate this room's
    container on it, on the same port and data directory, without
    touching the room's started_at/expires_at timer. Used to roll out a
    new server build (or a config/plugin fix baked into the image) to an
    already-running room. This *will* disconnect anyone currently
    connected, since the running container is replaced."""
    with room_lock(room_id):
        pull_latest_image()
        return create_and_start(room_id, port)


def delete_room_data(room_id):
    """Fully remove a room's container and its on-disk data. Called when
    a room is deleted, not on an ordinary stop/expiry."""
    with room_lock(room_id):
        stop_and_remove(room_id)
        d = room_dir(room_id)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)


def recent_logs(room_id, tail=60):
    container = get_container(room_id)
    if container is None:
        return ""
    return container.logs(tail=tail).decode("utf-8", errors="replace")


def logs_since(room_id, since_dt):
    """Fetch only log lines emitted after `since_dt` (a datetime), for the
    background player-roster scan. Returns "" if the room isn't running.

    `since_dt` is a naive datetime that represents UTC (everywhere in this
    app builds these with datetime.utcnow()). docker-py's own since-handling
    calls `dt.astimezone(timezone.utc)` internally, and for a *naive*
    datetime that assumes local server time, not UTC -- so a naive UTC value
    gets silently shifted by the host's UTC offset before being sent to the
    Docker daemon. On any host not already set to UTC, that pushes `since`
    into the future and Docker (correctly) returns nothing, ever. Attaching
    real UTC tzinfo here makes that internal astimezone() call a no-op."""
    container = get_container(room_id)
    if container is None:
        return ""
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    return container.logs(since=since_dt).decode("utf-8", errors="replace")


def send_console_command(room_id, command_text):
    """Write a line into the container's stdin, the same as typing into
    `docker attach`. Requires the container to have been started with
    stdin_open=True (it is, in create_and_start)."""
    container = get_container(room_id)
    if container is None:
        raise RuntimeError("Room isn't running.")
    sock = client().api.attach_socket(container.id, params={"stdin": 1, "stream": 1})
    try:
        raw = sock._sock if hasattr(sock, "_sock") else sock
        data = (command_text.rstrip("\n") + "\n").encode("utf-8")
        raw.sendall(data)
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _run_privileged(room_id, script, extra_mounts=None):
    data_dir = ensure_room_dir(room_id)
    volumes = {data_dir: {'bind': '/data', 'mode': 'rw'}}
    if extra_mounts:
        volumes.update(extra_mounts)
    client().containers.run(
        IMAGE,
        entrypoint=['/bin/sh', '-c'],
        command=[script],
        volumes=volumes,
        remove=True,
        detach=False,
        network_disabled=True,
    )


def replace_save(room_id, file_storage):
    """Delete server-data/User/SaveServer/save.ktml and drop the uploaded
    .sav file in as progress.sav, per the server's expected layout.

    The room's data directory is written to by the game server running as
    root inside the container, so an existing progress.sav on disk is
    typically root-owned. This process (running as a regular user) can
    still create/replace directory *entries* it has write access to, but
    can't necessarily truncate an existing root-owned file's contents in
    place -- that raised PermissionError here before. Writing the upload
    to a temp file in the same directory and atomically renaming it over
    the destination sidesteps that: rename() only needs write access to
    the directory, never to the file it replaces."""
    save_dir = os.path.join(room_dir(room_id), "User", "SaveServer")
    os.makedirs(save_dir, exist_ok=True)
    old_save = os.path.join(save_dir, "save.ktml")

    def _remove_old_save():
        try:
            os.remove(old_save)
            return True
        except FileNotFoundError:
            return True  # nothing to delete
        except OSError:
            return False  # exists but we can't touch it (permissions)

    if not _remove_old_save():
        # save.ktml (and/or save_dir itself) is typically root-owned, since
        # the game server inside the container wrote it as root. Note this
        # isn't just the removal failing: os.path.isfile() on a directory
        # we can't read silently reports "not found" rather than raising,
        # so gating the removal on an isfile() check first (as this used to)
        # can skip it entirely on a root-owned save_dir -- the file then
        # only gets its permissions fixed as a side effect of the
        # progress.sav write below, one upload too late, and isn't actually
        # removed until the *next* upload. Always attempting the removal
        # itself, not a prior existence check, avoids that. Fix permissions
        # the same way the tempfile fallback below does, then retry once.
        _run_privileged(room_id, "chmod -R a+rwX /data/User 2>/dev/null || true")
        _remove_old_save()

    dest = os.path.join(save_dir, "progress.sav")
    try:
        fd, tmp_path = tempfile.mkstemp(dir=save_dir, prefix=".progress-upload-", suffix=".tmp")
    except PermissionError:
        # save_dir itself (not just files in it) is root-owned, because the
        # game server inside the container wrote it as root -- this host
        # process can't create directory entries there either. Fix it the
        # same way the container would: run a short-lived privileged
        # container against the bind-mounted room data and retry once.
        _run_privileged(room_id, "chmod -R a+rwX /data/User 2>/dev/null || true")
        fd, tmp_path = tempfile.mkstemp(dir=save_dir, prefix=".progress-upload-", suffix=".tmp")
    os.close(fd)
    try:
        file_storage.save(tmp_path)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def replace_player_save(room_id, uid, content_bytes):
    """Write `content_bytes` (a KTML save, UTF-8 encoded) to
    User/SaveServer/users/<uid>.ktml, replacing whatever save that player
    currently has -- per SaveServer.getClientUID()/save() in the actual
    server source, this is the exact path/filename the game server itself
    reads from, so the file is always named after `uid`, never after
    whatever the uploaded file was called.

    Same permission dance as replace_save(): users/ is typically
    root-owned (written by the game server running as root inside the
    container), so this writes to a temp file in the same directory and
    atomically renames it over the destination -- that only needs write
    access to the directory itself, never to the file it replaces."""
    save_dir = os.path.join(room_dir(room_id), "User", "SaveServer", "users")
    os.makedirs(save_dir, exist_ok=True)
    dest = os.path.join(save_dir, f"{uid}.ktml")
    try:
        fd, tmp_path = tempfile.mkstemp(dir=save_dir, prefix=".player-upload-", suffix=".tmp")
    except PermissionError:
        # save_dir itself is root-owned and this process can't create
        # directory entries there either -- fix it the same way
        # replace_save() does and retry once.
        _run_privileged(room_id, "chmod -R a+rwX /data/User 2>/dev/null || true")
        fd, tmp_path = tempfile.mkstemp(dir=save_dir, prefix=".player-upload-", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as f:
            f.write(content_bytes)
        os.replace(tmp_path, dest)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def delete_player_save(room_id, uid):
    """Delete one player's persisted save data (User/SaveServer/users/<uid>.ktml),
    per SaveServer.getClientUID()/save() in the actual server source."""
    path = os.path.join(room_dir(room_id), "User", "SaveServer", "users", f"{uid}.ktml")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False



def fix_save_permissions(room_id):
    """Ensure this host process can read everything under
    User/SaveServer/ (the main save.ktml and every per-player
    users/<uid>.ktml). Both are written by the game server running as
    root inside the container, so a freshly-written one is root-owned
    and this process may not be able to read it (or even list the
    directory to confirm it exists -- see the note on os.path.isfile
    lying about a root-owned directory in replace_save's history). Call
    this before serving either file for download."""
    ensure_room_dir(room_id)
    _run_privileged(room_id, "chmod -R a+rX /data/User 2>/dev/null || true")


def main_save_path(room_id):
    return os.path.join(room_dir(room_id), "User", "SaveServer", "save.ktml")


def player_save_path(room_id, uid):
    return os.path.join(room_dir(room_id), "User", "SaveServer", "users", f"{uid}.ktml")


# Per-room PuppetPacks/ ships two variants of the PlayerPuppet actor pack
# (the actor used to represent *other* players in your world): a 'Dummy'
# version with no collision -- other players can't be hit or hit you -- and
# a 'Physics' version with full collision, which is what makes PVP possible.
PUPPET_PACK_SOURCES = {
    True: "Physics__PlayerPuppet.pack.zs",
    False: "Dummy_PlayerPuppet.pack.zs",
}


def set_pvp_enabled(room_id, enabled):
    """Swap the live PlayerPuppet actor pack under Romfs/Pack for the
    'Physics' (PVP on) or 'Dummy' (PVP off) variant from PuppetPacks/.

    Like Romfs/User/SaveServer, the Romfs tree is written by the game
    server running as root inside the container, so this host process
    can't necessarily read into it (let alone write) -- the copy is done
    via a short-lived privileged container instead, same approach as the
    save-upload permission fix. The exact path of the existing pack file
    under Romfs/Pack isn't hardcoded (it's found by name at run time),
    since it's owned by the container image/version, not this app.

    The source pack is copied from /app/seed/PuppetPacks (the image's own
    baked-in copy), not /data/PuppetPacks (the room's persistent volume).
    A room's persistent PuppetPacks/ only exists once the main server
    container's own docker-entrypoint.sh has finished its first-run seed
    copy into the volume -- and _start_room() calls this function right
    after starting that container, without waiting for that copy to
    finish. Sourcing from the image instead of the volume sidesteps that
    race entirely: /app/seed/PuppetPacks is available immediately in any
    container from this image, with no dependency on the volume's state."""
    ensure_room_dir(room_id)
    source_name = PUPPET_PACK_SOURCES[bool(enabled)]
    script = f"""set -e
SRC=/app/seed/PuppetPacks/{source_name}
TARGET=$(find /data/Romfs/Pack -iname '*PlayerPuppet*' -type f | head -n1)
if [ -z "$TARGET" ]; then
    TARGET=/data/Romfs/Pack/Actor/PlayerPuppet.pack.zs
    mkdir -p "$(dirname "$TARGET")"
fi
cp "$SRC" "$TARGET"
chmod -R a+rwX /data/Romfs/Pack
"""
    _run_privileged(room_id, script)


def set_max_upload_per_second(room_id, value):
    """Update ResourceServer.properties.serverMaxUploadPerSecond in the
    room's persistent Resources/TOTKServer/serverCreator.ktml -- the byte/s
    cap the server enforces on client uploads (mods, saves, etc. sent *to*
    the server). Players on slow connections can time out against the
    10,000,000 default and need it lowered.

    serverCreator.ktml is plain text (not the binary .ktml save format), so
    this is a straightforward in-place substitution of the one line, done
    via a short-lived privileged container the same way as set_pvp_enabled
    -- the file is root-owned on disk for the same reason (written into
    the volume by the container's own first-run seed copy, which runs as
    root).

    Same /app/seed vs /data consideration as set_pvp_enabled's puppet pack:
    a brand-new room's persistent Resources/ tree may not exist yet if
    that first-run seed copy hasn't finished (this can run right after
    create_and_start, before that copy is done). Rather than race it,
    seed serverCreator.ktml from the image's own baked-in copy first if
    it's missing -- that's always available immediately, no race."""
    value = int(value)  # also guards the interpolation below: only digits
    # ever reach the shell script, so there's no injection risk from this
    # being user-supplied.
    if value < 1:
        raise ValueError("serverMaxUploadPerSecond must be a positive integer.")
    ensure_room_dir(room_id)
    script = rf"""set -e
TARGET=/data/Resources/TOTKServer/serverCreator.ktml
if [ ! -f "$TARGET" ]; then
    mkdir -p "$(dirname "$TARGET")"
    cp /app/seed/Resources/TOTKServer/serverCreator.ktml "$TARGET"
fi
sed -i -E 's/("serverMaxUploadPerSecond"[[:space:]]*:[[:space:]]*)[0-9]+/\1{value}/' "$TARGET"
chmod -R a+rwX /data/Resources
"""
    _run_privileged(room_id, script)


def replace_romfs(room_id, file_storage):
    """Extract a .zip of Romfs mod files into the room's persistent Romfs
    folder (bind-mounted at /app/run/Romfs in the container). Files whose
    relative path matches an existing file overwrite it; new files are
    added; anything already on disk that isn't in the archive is left
    alone -- mods merge in rather than replacing the whole tree.

    Accepts a zip rooted directly at the Romfs contents (Pack/, System/,
    ...) or one with a single top-level 'Romfs' folder wrapping those same
    contents -- the wrapper, if present, is detected and stripped so
    either convention works.

    Like Romfs/User/SaveServer, the Romfs tree is owned by root on the
    host (the game server writes it as root inside the container), so
    this process can create new *files* there (the rename-over-an-
    existing-file trick from replace_save) but can't reliably create new
    *subdirectories* under a root-owned parent -- there's no rename
    equivalent for mkdir. A mod archive with new folders (very common)
    would hit exactly that. So the actual copy runs inside a short-lived
    privileged container instead, same pattern as set_pvp_enabled/
    set_max_upload_per_second -- the zip itself is extracted host-side
    first (into a scratch dir this process fully owns, so the zip
    handling/path-safety logic below stays in Python), and only the final
    'copy this already-extracted tree into /data/Romfs' step happens as
    root. That step only needs 'cp', unlike unzip which the image may not
    have installed.

    Returns the number of files written. Raises zipfile.BadZipFile if the
    upload isn't a valid zip."""
    ensure_room_dir(room_id)

    fd, tmp_zip_path = tempfile.mkstemp(prefix='romfs-upload-', suffix='.zip')
    os.close(fd)
    scratch_dir = tempfile.mkdtemp(prefix='romfs-extract-')
    try:
        file_storage.save(tmp_zip_path)

        with zipfile.ZipFile(tmp_zip_path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]

            top_levels = set()
            for m in members:
                if '/' in m.filename:
                    top_levels.add(m.filename.split('/', 1)[0])
            strip_prefix = None
            if len(top_levels) == 1:
                only = next(iter(top_levels))
                if only.lower() == 'romfs':
                    strip_prefix = only + '/'

            scratch_norm = os.path.normpath(scratch_dir)
            written = 0
            for member in members:
                name = member.filename
                if strip_prefix and name.startswith(strip_prefix):
                    name = name[len(strip_prefix):]
                name = name.strip('/')
                if not name:
                    continue

                # zip-slip guard: resolve the final path and make sure
                # it's still contained within our own scratch dir.
                dest_path = os.path.normpath(os.path.join(scratch_dir, name))
                if dest_path != scratch_norm and not dest_path.startswith(scratch_norm + os.sep):
                    continue

                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with zf.open(member) as src, open(dest_path, 'wb') as dst:
                    shutil.copyfileobj(src, dst)
                written += 1

        if written == 0:
            return 0

        # 'cp -a src/. dst/' recursively merges: overwrites files that
        # already exist at that path, adds new ones, and never deletes
        # anything in dst not present in src -- exactly the merge
        # semantics described above.
        _run_privileged(
            room_id,
            "mkdir -p /data/Romfs && cp -a /upload/. /data/Romfs/ && chmod -R a+rwX /data/Romfs",
            extra_mounts={scratch_dir: {'bind': '/upload', 'mode': 'ro'}},
        )
        return written
    finally:
        try:
            os.remove(tmp_zip_path)
        except OSError:
            pass
        shutil.rmtree(scratch_dir, ignore_errors=True)
