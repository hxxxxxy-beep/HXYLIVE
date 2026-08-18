# HXYLIVE architecture

## VPS responsibilities

The FastAPI application serves the web UI and APIs, discovers provider streams, starts and supervises recording processes, converts recordings, maintains the media library, and issues short-lived download jobs for the Mac Helper.

Docker Compose runs:

- `hxylive`: the application, FFmpeg, Playwright Chromium, and provider logic;
- `flaresolverr`: optional Cloudflare-assisted provider access.

Nginx is the public entry point. It proxies normal HTTP/WebSocket traffic to the private application port and serves protected recording ranges through an internal alias. The application container does not bind directly to a public interface.

## Persistent state

The container sees `/data`; the VPS stores it at `/opt/hxylive/data`. Source code and persistent state are deliberately separate.

Typical state includes:

- application database and settings;
- recording files and conversion work;
- thumbnails and profile images;
- provider cookies/session state;
- playback progress and profile metadata.

GitHub contains schemas and code, not this mutable state.

## Mac Helper responsibilities

The helper is a Python standard-library HTTP service bound only to `127.0.0.1:17899`. It:

- accepts requests only from the configured HXYLIVE web origin;
- scans the configured Mac video directory;
- reports downloaded recording IDs and file sizes to the VPS on a short heartbeat;
- retrieves short-lived download jobs from the VPS (browser dispatch or helper poll);
- opens each selected download URL in Google Chrome.

The Media page can show Mac files, open or delete them, and start downloads even when the browser cannot reach localhost (Chrome local-network restrictions, or with the system proxy off). If `--proxy` is configured and that proxy is down, the helper reaches the VPS directly.

The helper does not expose a LAN listener and does not store VPS credentials.

## Security boundaries

- Web access should be protected by `PASSWORD`.
- `.env`, provider cookies, databases, recordings, and generated Mac plist files are ignored.
- The application port and FlareSolverr bind to loopback on the VPS.
- The Mac Helper binds to loopback and enforces one allowed web origin.
- Automatic Docker update access is disabled by default and must not receive the Docker socket casually.
