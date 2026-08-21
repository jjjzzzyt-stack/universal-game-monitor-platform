# Vercel mode limitations

Universal Gaming Radar's cloud edition is request-driven by design.

- No permanent process survives between requests.
- No durable server filesystem or shared in-memory cache is assumed.
- No short-interval Vercel cron is used.
- Browser localStorage carries verified state, seen IDs, settings and history.
- The dashboard must remain open for 30-second refreshes and browser notifications.
- Browser notification delivery is not equivalent to Windows `winotify` background toasts.
- Arbitrary custom URL monitors are disabled on the public API.
- Different browser profiles/devices do not share state automatically.
- First requests after inactivity can have a cold-start delay.
- Upstream providers may block datacenter addresses or enforce rate limits.
- Vercel Hobby quotas can pause or limit the app after heavy public usage.

These are platform constraints, not hidden defects. The separate Windows-local package remains the always-on choice while the PC is awake.
