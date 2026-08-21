# Universal Gaming Radar — Vercel Platform Edition

A complete keyless gaming-intelligence platform adapted for Vercel's free request-driven serverless environment. It combines seven gaming/network service cards, free games, giveaways, CheapShark/GOG/Steam deals, Steam News, more than 400 free-to-play titles, verified gameplay streams, favorites, price watches, analytics, exports and an installable PWA.

**Cloud version:** `2026.08.21-universal-gaming-radar-v2-platform-vercel`

## What remains fully available

- Epic, Discord, PlayStation, Xbox, Steam, Twitch and Roblox status
- Epic free/upcoming games
- GamerPower giveaways, keys, betas and loot
- CheapShark and GOG deals
- Steam specials, top sellers, releases and coming soon
- Tracked Steam News
- FreeToGame catalog
- Rotating Piped/YouTube live gameplay
- Region selection, themes, compact mode and Ctrl+K search
- Favorites, price rules and watchlists
- Persistent browser event history and analytics
- Browser notifications while the tab/PWA is active
- JSON backup/import, ICS calendar and CSV export
- Installable PWA shell

## Serverless architecture

Vercel Functions do not keep a permanent daemon or durable local disk. The browser therefore owns continuity:

1. `index.html` loads the last verified state from `localStorage`.
2. Every 30 seconds while open, it POSTs bounded continuity to `/api/state`.
3. The Python function refreshes only providers whose own interval is due.
4. The function returns a complete state plus updated continuity.
5. The browser stores it and displays/optionally notifies new events.

A warm continuity refresh with no due providers performs no upstream work. The first cold scan fetches all 15 safe built-in providers concurrently. Browser continuity was measured at approximately 349 KB and a full first response at approximately 730 KB during verification.

## Intentional cloud differences

- **No 24/7 background loop:** provider checks happen only while a visitor has the dashboard/PWA open.
- **Browser notifications only:** notifications require an open tab/installed PWA and granted browser permission.
- **Browser-local persistence:** settings/history are separate per browser profile/device unless exported and imported.
- **No arbitrary custom endpoints:** public server-side custom URL fetching is disabled to prevent the deployment becoming an SSRF proxy. Use the Windows-local build for custom endpoints.
- **No Windows toast/startup scripts:** those belong to the separate local package.

## Deploy

See `DEPLOY-VERCEL.md`. No environment variables, API keys, database, credit card or paid service are required.

## Key files

- `index.html` — full cloud dashboard and browser continuity
- `api/state.py` — visitor-driven provider scheduler, transitions, de-duplication and state response
- `api/healthz.py` — lightweight cloud health endpoint
- `radar.py` — shared keyless provider implementations
- `manifest.webmanifest` / `service-worker.js` — PWA shell
- `vercel.json` — function durations and security headers
- `.python-version` — Python 3.13 selector
- `.vercelignore` — excludes Windows and test-only files from deployment

## Local cloud-mode test

```bash
python dev_server.py
```

Open <http://127.0.0.1:8897/>. This emulates the static and `/api/state` routes with the same request-driven state builder.

## Tests

```bash
python -m py_compile radar.py api/state.py api/healthz.py dev_server.py
python -m unittest discover -s tests -v
node --check extracted-dashboard.js
```

## Limitations

- Free Vercel plans have quotas, execution limits and cold starts.
- Upstream sites can throttle/block Vercel cloud IPs.
- Piped is community infrastructure and can fail.
- Public search is not exhaustive.
- `localStorage` can be cleared by the user/browser; export backups periodically.
- A PWA service worker caches the shell, not live API data while closed.
- Individual YouTube videos may disallow embedding.

Original code is MIT licensed. Third-party data and marks remain with their owners; see `THIRD_PARTY_NOTICES.md`.
