# Deploy the SkybyDay demo

The public demo uses one Python process for the built frontend, HTTP API, and
WebSocket feed. It binds only to `127.0.0.1:8091`; the existing web server handles
HTTPS for `fly.donutmath.com`. No database or paid API key is required.

## Prepare a release

On a machine with Node.js 24:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

Transfer the source plus `frontend/dist` to a new release directory such as
`/opt/skybyday/releases/2026-11-01`. Do not transfer `.env`, `.env.airlabs`,
local captures, development virtual environments, or `node_modules`.

On the server, install uv and Python 3.12 if unavailable. Keep the managed Python
outside a home directory because the service uses `ProtectHome=true`:

```bash
sudo mkdir -p /opt/skybyday/python /opt/skybyday/cache
cd /opt/skybyday/releases/2026-11-01
sudo env UV_PYTHON_INSTALL_DIR=/opt/skybyday/python UV_CACHE_DIR=/opt/skybyday/cache \
  /opt/skybyday/bin/uv sync --project backend --locked --no-dev --python 3.12
sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin skybyday
sudo ln -s /opt/skybyday/releases/2026-11-01 /opt/skybyday/current
sudo cp deploy/skybyday.service /etc/systemd/system/skybyday.service
sudo systemctl daemon-reload
sudo systemctl enable --now skybyday
curl --fail http://127.0.0.1:8091/api/health
```

Use the actual installed uv path. Create the user and `current` link only on the
first installation. Release files must be readable by the `skybyday` user.
Keep exactly one worker and one service instance: the shared poller and request
backoff are process-local. The service limits memory to 384 MiB and CPU to one
core and leaves recording and AirLabs off.

## Domain and HTTPS

Create an A record for `fly` in the `donutmath.com` DNS zone pointing to the
existing server. Add a separate virtual host; do not replace the main website's
configuration. `deploy/nginx.conf` is an Nginx example. On an Apache/Caddy host,
use the equivalent proxy to `127.0.0.1:8091` including WebSocket upgrades.

With Nginx and Certbot already installed:

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/skybyday
sudo ln -s /etc/nginx/sites-available/skybyday /etc/nginx/sites-enabled/skybyday
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d fly.donutmath.com
```

Require a valid origin certificate even when a DNS proxy is enabled. The backend
allows WebSockets from exactly `https://fly.donutmath.com`; a different hostname
needs an explicit service environment change.

## Verify

```bash
curl --fail https://fly.donutmath.com/api/health
curl --fail https://fly.donutmath.com/api/aircraft
sudo systemctl status skybyday --no-pager
sudo journalctl -u skybyday -n 40 --no-pager
```

Open the public URL in a browser, confirm the globe and live connection, select
an aircraft, and check Map and Weather. A healthy feed may contain zero aircraft.
Verify the main DonutMath website still responds. Provider outages must remain
visible; do not enable synthetic data as a silent production fallback.

## Update and roll back

Build and install each new release in its own directory before switching.
Preserve the prior release and record its path:

```bash
readlink -f /opt/skybyday/current
sudo ln -s /opt/skybyday/releases/NEW_RELEASE /opt/skybyday/current.next
sudo mv -Tf /opt/skybyday/current.next /opt/skybyday/current
sudo systemctl restart skybyday
curl --fail http://127.0.0.1:8091/api/health
```

To roll back, repeat the link switch with the previous release path and restart
only `skybyday`. To withdraw the demo, stop `skybyday`, disable only its virtual
host, validate and reload the web server, and remove only the `fly` DNS record.
Keep the main website and course services running.
