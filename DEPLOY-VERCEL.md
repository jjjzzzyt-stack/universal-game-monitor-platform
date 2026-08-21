# Deploy Universal Gaming Radar to Vercel Hobby

This deployment requires no API key, environment variable, database, credit card or paid service.

## Option A — GitHub import (recommended)

1. Create a new public or private GitHub repository.
2. Upload the **contents** of this project folder to the repository root.
3. Sign in to Vercel and choose **Add New → Project**.
4. Import the repository.
5. Configure:
   - **Framework Preset:** `Other`
   - **Root Directory:** repository root
   - **Build Command:** leave empty/default
   - **Output Directory:** leave empty
   - **Install Command:** leave empty/default
6. Do not add environment variables.
7. Click **Deploy**.
8. Open the assigned `https://...vercel.app` URL.
9. Wait for the first provider scan, then use **Test alert** to grant browser notification permission.

## Option B — Vercel CLI

From this project folder:

```bash
npx vercel
```

Answer the prompts, use Framework `Other`, then deploy production:

```bash
npx vercel --prod
```

## Validate after deployment

Open:

```text
https://YOUR-PROJECT.vercel.app/api/healthz
```

Expected JSON includes:

```json
{
  "ok": true,
  "app": "Universal Gaming Radar",
  "version": "2026.08.21-universal-gaming-radar-v2-platform-vercel",
  "mode": "vercel-request-driven"
}
```

Then open the root dashboard. The initial all-provider request may take several seconds. Later browser-continuity requests are much lighter and refresh only due sources.

## Browser continuity

Each browser/device has independent local state. To move it:

1. Open **My Radar** or Settings.
2. Download the JSON backup.
3. On the other browser/device, choose the backup and import it.

Clearing site data/localStorage resets baselines, history, favorites and settings for that browser. It does not affect another visitor.

## Browser notifications

- Click **Test alert** while the site is open.
- Allow notification permission.
- Notifications can appear only while the dashboard or installed PWA is active.
- Vercel does not provide this project with a permanent push worker or background daemon.
- Do Not Disturb/browser settings can suppress alerts.

## PWA installation

On supported Chrome/Edge browsers, use the dashboard's Install app button or the browser address-bar installation icon. The PWA offers a standalone window and cached UI shell, but live provider checks still require the app to be open and online.

## Free-plan realities

- Vercel Functions are ephemeral.
- Hobby quotas and maximum duration apply.
- The app uses a 120-second allowance for the main Python function, but typical verified cold scans were far shorter.
- No Vercel cron is used; Hobby cron cannot provide continuous short-interval monitoring.
- A public provider may reject cloud IPs even when it works from a home PC.
- Keep the Windows-local package for true always-on monitoring while your PC is awake.

## Troubleshooting

### Root works but API fails

Check the Vercel Function logs for `api/state.py`. Confirm Framework Preset is `Other` and `.python-version` is present.

### One provider is red

Other providers continue. Use Refresh All later. The exact upstream error appears under provider telemetry.

### Data disappears

Confirm the browser did not clear site data or use private/incognito mode. Restore a JSON backup.

### Notifications do not appear

Check browser site permissions and operating-system notifications. Keep the tab/PWA active.

### Custom endpoint monitor is missing

This is intentional on the public build for SSRF safety. It remains available in the Windows-local edition.
