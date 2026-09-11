# Zonai Hosting - TotK Online Multiplayer Server Hosting Site

Lets visitors spin up, manage, and tear down `totk-server-N` containers
themselves. Runs directly on the Docker host and talks to the Docker
daemon over its local socket.

## Setup
Install the python packages:
```bash
pip install -r requirements.txt
```
Make a `.env` file and add the following:
```txt
TOTK_ADMIN_PASSWORD=pick-a-real-password
TOTK_ROOMS_ROOT=/opt/totk-rooms     # where per-room data dirs live
TOTK_IMAGE=wesleyhellewell/totk_online_multiplayer_server:latest
TOTK_SECRET_KEY=make-a-random-string-32-characters-long
```
You may need to run these:
```bash
sudo mkdir -p /opt/totk-rooms
sudo chown $(whoami) /opt/totk-rooms
```
Start the webserver:
```bash
python app.py
```
Runs on `:5100` by default.
Docker socket access
This app calls the Docker Engine API directly (via the `docker` Python
package), which by default talks to `/var/run/docker.sock`. If you run
the website itself in a container too, you'd mount that socket in:
```bash
docker run -v /var/run/docker.sock:/var/run/docker.sock ...
```

## Files
`app.py` - routes

`docker_manager.py` - all Docker interaction (create/stop/remove containers, per-room data dirs, console command injection, save uploads)

`models.py` - SQLite schema and queries

`scheduler.py` - background job that stops expired rooms

`templates/`, `static/style.css` - frontend