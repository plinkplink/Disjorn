# Disjorn — Debian Deployment Runbook

Single-box deployment: FastAPI/uvicorn serves both the API and the built PWA
(`client/dist`) on one port, behind Tailscale. All paths below assume the
checkout lives at `/home/plink/Disjorn/Disjorn` and runs as user `plink` —
substitute your own.

## 1. Prerequisites

```sh
sudo apt install python3 python3-venv python3-pip nodejs npm git sqlite3
```

Optional, feature-dependent:

- **Ollama** (link summarization) — install per <https://ollama.com/download>,
  then `ollama pull llama3.2` (or whichever model you set as `OLLAMA_MODEL`).
- **NVIDIA driver + CUDA** (fast speech-to-text) — faster-whisper runs on CPU
  too, just slower. The ML extras are only needed on the box that does STT.

## 2. Clone, venv, dependencies, client build

Either run the convenience script (idempotent, safe to re-run):

```sh
git clone <repo-url> Disjorn && cd Disjorn
./deploy/install.sh          # add --ml on the GPU box (faster-whisper + rawpy)
```

…or do the same by hand:

```sh
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Optional — STT (faster-whisper) + RAW/DNG photo support (rawpy).
# Install on the GPU box; the server degrades gracefully without them
# (STT returns 501, RAW conversion is skipped):
.venv/bin/pip install -r requirements-ml.txt

cd ../client
npm ci
npm run build                # -> client/dist, auto-served by the server
```

The server serves `client/dist` with an SPA fallback whenever it exists; no
separate web server is needed.

## 3. Configuration (`server/.env`)

```sh
cp deploy/.env.example server/.env
```

Then edit `server/.env` — every variable is documented in the example file.
The two you MUST set:

1. **SECRET_KEY** (signs media URLs — the dev default is not safe):

   ```sh
   python3 -c "import secrets; print(secrets.token_urlsafe(48))"
   ```

2. **VAPID keys** (Web Push). Generate once and paste the printed lines:

   ```sh
   cd server && .venv/bin/python cli.py gen-vapid
   ```

   Also set `VAPID_CLAIMS_EMAIL` to a mailto: you own.

## 4. Accounts (admin CLI)

There is no open registration — create accounts from the CLI (run from
`server/`, it uses the same `.env`/DB as the server and runs migrations
automatically):

```sh
cd server
.venv/bin/python cli.py create-user alice --admin        # prompts for password
.venv/bin/python cli.py create-user bob --display-name "Bob"
echo 'hunter2' | .venv/bin/python cli.py create-user carol --password-stdin
.venv/bin/python cli.py create-bot claudette             # prints API key ONCE — store it
```

`create-bot` adds the bot to `#main` automatically; bots join DMs only when
explicitly added.

## 5. systemd

```sh
# Edit first if your user/paths/port differ — see comments in the unit file.
sudo cp deploy/disjorn.service /etc/systemd/system/disjorn.service
sudo systemctl daemon-reload
sudo systemctl enable --now disjorn

systemctl status disjorn           # should be active (running)
journalctl -u disjorn -f           # tail logs
curl -s http://127.0.0.1:8399/healthz   # {"ok":true}
```

## 6. Tailscale exposure

The unit binds `127.0.0.1:8399` by default. Options, best first:

- **Recommended — HTTPS via `tailscale serve`:**

  ```sh
  sudo tailscale serve --bg https / http://127.0.0.1:8399
  ```

  Tailscale terminates HTTPS with a valid cert at
  `https://<machine>.<tailnet>.ts.net/` and proxies to the loopback port.
  Then set `COOKIE_SECURE=true` in `server/.env` and restart the unit.

- **Plain HTTP on the tailnet:** change `--host` in the unit to the machine's
  Tailscale IP (`tailscale ip -4`, a `100.x.y.z` address) or to `0.0.0.0`
  (also exposes it to your LAN — fine on a trusted network, your call). Keep
  `COOKIE_SECURE=false` or logins will break over plain HTTP.

**Web Push requires HTTPS** (or `localhost`) — browsers refuse service-worker
push on plain-HTTP origins, so notifications on phones effectively need the
`tailscale serve` setup. Chat itself works fine over plain HTTP.

## 7. Backups

What to back up:

| Path (under `server/`)   | Contents                                   |
|--------------------------|--------------------------------------------|
| `data/disjorn.db`        | all messages/users/state — **see below**   |
| `data/uploads/`          | attachments + originals + avatars          |
| `data/assets/`           | picker images, chibi packs (semi-static)   |
| `.env`                   | secrets — without it, signed URLs + sessions die |

**SQLite: NEVER raw-copy a live database.** The DB runs in WAL mode; copying
`disjorn.db` while the server is running (cp, rsync, snapshot of the file
alone) can capture a torn, unrecoverable state. Use one of:

```sh
# One-shot consistent snapshot (safe while the server runs):
sqlite3 /home/plink/Disjorn/Disjorn/server/data/disjorn.db \
  ".backup '/backup/disjorn-$(date +%F).db'"
```

or [Litestream](https://litestream.io/) for continuous streaming replication
of the DB file.

The uploads/assets dirs are plain files — ordinary `rsync -a` is fine:

```sh
rsync -a /home/plink/Disjorn/Disjorn/server/data/uploads/ /backup/uploads/
rsync -a /home/plink/Disjorn/Disjorn/server/data/assets/  /backup/assets/
```

A nightly cron pairing `sqlite3 .backup` + `rsync` into a directory your
existing server backup already covers is entirely sufficient at this scale.

## 8. Upgrades

```sh
cd /home/plink/Disjorn/Disjorn
git pull
server/.venv/bin/pip install -r server/requirements.txt   # picks up new deps
(cd client && npm ci && npm run build)                    # rebuild the PWA
sudo systemctl restart disjorn
```

Database migrations are numbered and additive; the server applies any new
ones automatically on startup — no manual migration step. Take a
`sqlite3 .backup` first if the release notes mention schema changes.

## 9. Troubleshooting

- **`POST /stt` returns 501** — faster-whisper isn't installed. Install the
  extras: `server/.venv/bin/pip install -r server/requirements-ml.txt`
  (or `./deploy/install.sh --ml`), then restart. First use downloads the
  model into `~/.cache` — allow time and disk.
- **Push endpoints return 503 / notifications never arrive** — VAPID keys are
  missing. Run `cli.py gen-vapid`, paste into `server/.env`, restart. If keys
  are set, check the origin is HTTPS (see §6) and that the user granted
  notification permission in Settings.
- **Summarize fails / times out** — is Ollama running (`curl
  http://localhost:11434`)? Is `OLLAMA_MODEL` pulled (`ollama list`)?
- **Unit fails at start: "address already in use"** — something else owns
  port 8399 (`ss -ltnp | grep 8399`). Stop it or change `--port` in the unit
  (and any `tailscale serve` mapping pointing at it).
- **GET / returns JSON 404 instead of the app** — `client/dist` is missing;
  the server logs `client/dist not found — static serving disabled`. Build
  the client (§2) and restart.
- **Login works but the cookie doesn't stick** — `COOKIE_SECURE=true` on a
  plain-HTTP origin. Match it to how you actually serve (§6).
- **Permission errors on `data/`** — the unit's `User=` must own
  `server/data`; also `ReadWritePaths` in the unit must match `DATA_DIR`.
