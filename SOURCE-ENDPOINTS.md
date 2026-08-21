# Source endpoints

All built-in sources are public and require no credentials in this build.

| Module | Endpoint | Role |
|---|---|---|
| Epic status | `https://status.epicgames.com/api/v2/summary.json` | Official Epic components and overall state |
| Discord status | `https://discordstatus.com/api/v2/summary.json` | Official Discord status |
| PlayStation | `https://status.playstation.com/data/statuses/region/SCEA.json` | Official PSN regional services |
| Xbox | `https://xnotify.xboxlive.com/servicestatusv6/US/en-US` | Official Xbox service state |
| Steam health | `https://api.steampowered.com/ISteamWebAPIUtil/GetServerInfo/v1/` | Steam Web API reachability |
| Twitch status | `https://status.twitch.tv/api/v2/summary.json` | Official Twitch status |
| Roblox status | `https://api.status.io/1.0/status/59db90dbcdeb2f04dadcf16d` | Roblox public Status.io representation |
| Epic promotions | `https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions` | Region-aware free/upcoming games |
| GamerPower | `https://www.gamerpower.com/api/giveaways` | Giveaways, game keys, betas and loot |
| CheapShark | `https://www.cheapshark.com/api/1.0/deals` | Cross-store PC offers |
| GOG catalog | `https://catalog.gog.com/v1/catalog` | Official GOG discounts |
| Steam featured | `https://store.steampowered.com/api/featuredcategories` | Region-aware specials, sellers and releases |
| Steam News | `https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/` | Official announcements for tracked App IDs |
| FreeToGame | `https://www.freetogame.com/api/games` | Free-to-play game catalog |
| Piped search | `https://api.piped.private.coffee/search` | No-key representation of YouTube live search |

The Windows-local edition can monitor user-supplied public endpoints with private-address blocking. The Vercel edition deliberately disables arbitrary URL fetching so the public function cannot be abused as an SSRF proxy.

Public/undocumented storefront and status schemas can change. Every provider is isolated so one failure does not stop the rest of the platform. Provider health exposes latency and exact errors.
