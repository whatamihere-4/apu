# APU

Upload workflow UI and sidecars (hasher, thumber, splitter). **All web traffic must go through your existing Caddy container** — this stack does not publish any host ports.

## Quick start

1. Ensure your Caddy stack is running and attached to the shared Docker network (default: `caddy_net`):

```bash
docker network create caddy_net   # skip if it already exists
```

2. Copy `.env.example` to `.env` and fill in your settings.
3. Create data directories if needed: `downloads/`, `thumbs/`, `cache/`.
4. Start the stack:

```bash
docker compose up -d --build
```

5. Add or update a site block in **your** Caddy config (see below), then reload Caddy.

The `apu` container listens on port 5000 inside the Docker network only. Sidecars (`hasher-http`, `thumber-http`, `splitter-http`) are internal-only as well.

## Caddy configuration

This repo does **not** include a Caddy container or Caddyfile. Use your own.

**Upstream container name:** wire to `apu:5000` on the shared network (`caddy_net` by default). Both Caddy and `apu` must be on that network.

### Example site block

```caddyfile
apu.example.com {
	reverse_proxy apu:5000
}
```

### Example with basic auth

```caddyfile
apu.example.com {
	basicauth {
		# bcrypt hash — generate with: caddy hash-password
		you $2a$14$...
	}
	reverse_proxy apu:5000
}
```

After editing your Caddyfile, reload your Caddy container as you normally would, e.g.:

```bash
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

## Services

| Service         | Role                         | Host exposure |
|-----------------|------------------------------|---------------|
| `apu`           | Flask web UI + upload jobs   | none          |
| `hasher-http`   | OSHASH / MD5 / PHASH sidecar | none          |
| `thumber-http`  | Thumbnail generation         | none          |
| `splitter-http` | Optional ffmpeg splitter     | none          |
| `thumber`       | One-off CLI (`compose run`)  | none          |

Internal sidecar URLs (container-to-container only) are set in `.env.example` under advanced wiring.

## One-off thumber CLI

```bash
docker compose run --rm thumber /downloads/some-video.mp4
```
