# APU

Upload workflow UI and sidecars (hasher, thumber, splitter). **All web traffic must go through Caddy** — the app and sidecar HTTP ports are not published to the host.

## Quick start

1. Copy `.env.example` to `.env` and fill in your settings.
2. Create data directories if needed: `downloads/`, `thumbs/`, `cache/`.
3. Start the stack:

```bash
docker compose up -d --build
```

4. Open the site at `http://localhost` (or your `APU_DOMAIN`).

Caddy is a required service in `docker-compose.yml`. Only Caddy binds host ports (`80`/`443` by default). The Flask app listens on `apu:5000` inside the Docker network and is not exposed directly.

## Caddy configuration

The default Caddyfile lives at `docker/caddy/Caddyfile`. Edit it for your domain, TLS, basic auth, or other Caddy features.

**Upstream container name:** always wire to `apu:5000` (the app container name on the Docker network).

### Example: local HTTP

```caddyfile
localhost {
	reverse_proxy apu:5000
}
```

### Example: public hostname with automatic HTTPS

Set in `.env`:

```env
APU_DOMAIN=apu.example.com
ACME_EMAIL=you@example.com
```

Caddyfile (or use the bundled file — it reads `APU_DOMAIN` and `ACME_EMAIL` from the environment):

```caddyfile
apu.example.com {
	reverse_proxy apu:5000
}
```

### Example: use your own Caddyfile path

Point `CADDYFILE` in `.env` at a file outside this repo:

```env
CADDYFILE=/path/to/my/Caddyfile
```

Your custom file should still reverse-proxy to `apu:5000`:

```caddyfile
apu.example.com {
	reverse_proxy apu:5000
}
```

After editing Caddy config, reload:

```bash
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

Or restart Caddy:

```bash
docker compose restart caddy
```

## Services

| Service        | Role                         | Host exposure |
|----------------|------------------------------|---------------|
| `caddy`        | Reverse proxy (required)     | `80`, `443`   |
| `apu`          | Flask web UI + upload jobs   | none          |
| `hasher-http`  | OSHASH / MD5 / PHASH sidecar | none          |
| `thumber-http` | Thumbnail generation         | none          |
| `splitter-http`| Optional ffmpeg splitter     | none          |
| `thumber`      | One-off CLI (`compose run`)  | none          |

Internal sidecar URLs (container-to-container only) are set in `.env.example` under advanced wiring.

## One-off thumber CLI

```bash
docker compose run --rm thumber /downloads/some-video.mp4
```
