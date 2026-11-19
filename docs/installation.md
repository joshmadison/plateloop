# Installation

PlateLoop is a single web app with no database and no volumes: uploads are
processed in memory and nothing is written to disk.

## Option A — Docker (recommended)

```bash
git clone https://github.com/joshmadison/plateloop.git
cd plateloop
docker compose up -d
# open http://localhost:8080
```

The compose file ships sane defaults: non-root container, a 300 MB upload cap,
a healthcheck (`/` every 30 seconds) and `restart: unless-stopped`. To raise
the upload cap:

```yaml
environment:
  - MAX_UPLOAD_MB=600
```

## Option B — local Python

Python 3.12 or newer:

```bash
git clone https://github.com/joshmadison/plateloop.git
cd plateloop
python -m venv venv

venv/Scripts/pip install -r requirements.txt   # Windows
venv/bin/pip install -r requirements.txt       # Linux / macOS

venv/Scripts/python -m app.main                # Windows
venv/bin/python -m app.main                    # Linux / macOS
# open http://localhost:8000
```

## Homeserver vs. public hosting

> [!NOTE]
> The app has no login. The plain `docker-compose.yml` is meant for your LAN
> or tailnet. For a public instance, use the hardened Caddy setup in
> [`compose.prod.yaml`](../compose.prod.yaml) and follow
> [Public hosting](DEPLOY.md): rate limits, upload caps, a read-only container,
> TLS, subdomains and the legal pages are already prepared.

## Updating

```bash
git pull --ff-only
docker compose up -d --build
```

---

[← Back to README](../README.md) · [Installation](installation.md) · [Usage](usage.md) · [Configuration](configuration.md) · [Print window](print-window.md) · [Printers](printers.md) · [Safety](safety.md) · [Troubleshooting](troubleshooting.md) · [Development](development.md)
