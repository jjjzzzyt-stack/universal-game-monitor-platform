#!/usr/bin/env python3
"""Universal Gaming Radar — local gaming command center.

Keyless providers track service health, free games, giveaways, deals, Steam
storefront collections, and verified live gameplay. State and notification
history are persisted locally; the dashboard is bound to localhost only.
"""
from __future__ import annotations

import concurrent.futures
import csv
import datetime as dt
import html as html_lib
import io
import ipaddress
import json
import mimetypes
import os
import pathlib
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

APP_NAME = "Universal Gaming Radar"
APP_VERSION = "2026.08.21-universal-gaming-radar-v2-platform"
HOST = "127.0.0.1"
PORT = 8896
ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ASSETS_DIR = ROOT / "assets"
STATE_PATH = DATA_DIR / "state.json"
EVENTS_PATH = DATA_DIR / "events.jsonl"
DASHBOARD_PATH = ROOT / "dashboard.html"
ICON_PATH = ASSETS_DIR / "radar-icon.png"
MAX_EVENTS = 1200
MAX_SEEN = 6000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36 "
    "UniversalGamingRadar/1.0"
)
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9", "Accept": "application/json,text/plain,*/*"}

URLS = {
    "epic_status": "https://status.epicgames.com/api/v2/summary.json",
    "discord_status": "https://discordstatus.com/api/v2/summary.json",
    "playstation_status": "https://status.playstation.com/data/statuses/region/SCEA.json",
    "xbox_status": "https://xnotify.xboxlive.com/servicestatusv6/US/en-US",
    "steam_health": "https://api.steampowered.com/ISteamWebAPIUtil/GetServerInfo/v1/",
    "epic_free": "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions?locale=en-US&country=IN&allowCountries=IN",
    "gamerpower": "https://www.gamerpower.com/api/giveaways",
    "cheapshark": "https://www.cheapshark.com/api/1.0/deals?pageSize=40&sortBy=Deal%20Rating&onSale=1",
    "steam_featured": "https://store.steampowered.com/api/featuredcategories?cc=in&l=en",
    "twitch_status": "https://status.twitch.tv/api/v2/summary.json",
    "roblox_status": "https://api.status.io/1.0/status/59db90dbcdeb2f04dadcf16d",
    "free_to_play": "https://www.freetogame.com/api/games?sort-by=popularity",
    "gog_deals": "https://catalog.gog.com/v1/catalog?limit=40&order=desc:discount&discounted=eq:true&countryCode=IN&locale=en-US&currencyCode=USD",
    "steam_news": "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
}
PIPED_INSTANCES = ("https://api.piped.private.coffee",)

DEFAULT_SETTINGS = {
    "region": "IN",
    "currency": "INR",
    "deal_min_discount": 75,
    "notifications": {
        "service_incidents": True,
        "service_recoveries": True,
        "free_games": True,
        "giveaways": True,
        "deals": False,
        "price_watches": True,
        "live_streams": False,
        "game_news": False,
    },
    "quiet_hours": {"enabled": False, "start": "23:00", "end": "07:00"},
    "watchlist": [
        "Fortnite", "Grand Theft Auto V", "Minecraft", "Valorant", "Counter-Strike 2",
        "BGMI", "Rocket League", "League of Legends", "PUBG", "Apex Legends",
    ],
    "steam_apps": [
        {"id": "730", "name": "Counter-Strike 2"},
        {"id": "570", "name": "Dota 2"},
        {"id": "578080", "name": "PUBG: Battlegrounds"},
        {"id": "1172470", "name": "Apex Legends"},
        {"id": "271590", "name": "Grand Theft Auto V"},
    ],
    "custom_monitors": [],
    "price_rules": [],
    "favorites": [],
    "last_read_at": None,
    "ui": {"accent": "cyan", "compact": False},
}

# Intervals deliberately match the volatility and rate limits of each source.
PROVIDER_INTERVALS = {
    "epic_status": 20,
    "discord_status": 30,
    "playstation_status": 60,
    "xbox_status": 60,
    "steam_health": 30,
    "epic_free": 300,
    "gamerpower": 600,
    "cheapshark": 600,
    "steam_featured": 600,
    "twitch_status": 30,
    "roblox_status": 60,
    "free_to_play": 3600,
    "gog_deals": 900,
    "steam_news": 900,
    "custom_health": 60,
    "live_radar": 30,  # one watchlist game per run; polite rotating discovery
}

STATUS_SEVERITY = {"operational": 0, "maintenance": 1, "degraded": 2, "partial": 3, "major": 4, "unknown": 5}


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def load_json(path: pathlib.Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return fallback


def deep_merge(default: dict[str, Any], supplied: Any) -> dict[str, Any]:
    out = json.loads(json.dumps(default))
    if not isinstance(supplied, dict):
        return out
    for key, value in supplied.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def fetch_json(url: str, timeout: int = 20, max_bytes: int = 4_000_000) -> Any:
    request = urllib.request.Request(url, headers={**HEADERS, "Cache-Control": "no-cache", "Pragma": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(max_bytes + 1)
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            if len(raw) > max_bytes:
                raise RuntimeError("response exceeded safety limit")
        return json.loads(raw.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid JSON response") from exc


def parse_iso(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def price_number(value: Any) -> float | None:
    match = re.search(r"\d+(?:[.,]\d+)?", str(value or "").replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def statuspage_state(indicator: str) -> str:
    return {"none": "operational", "minor": "degraded", "major": "partial", "critical": "major", "maintenance": "maintenance"}.get(str(indicator).lower(), "unknown")


def component_state(value: str) -> str:
    return {
        "operational": "operational", "degraded_performance": "degraded",
        "partial_outage": "partial", "major_outage": "major", "under_maintenance": "maintenance",
    }.get(str(value).lower(), "unknown")


def service_item(service_id: str, name: str, status: str, url: str, description: str, details: list[dict[str, Any]], source: str) -> dict[str, Any]:
    return {
        "id": service_id, "name": name, "status": status, "url": url,
        "description": description, "details": details[:30], "source": source,
        "checked_at": iso_now(),
    }


def provider_epic_status() -> dict[str, Any]:
    data = fetch_json(URLS["epic_status"])
    page = data.get("status", {})
    overall = statuspage_state(page.get("indicator", "unknown"))
    components = data.get("components", [])
    details = []
    for comp in components:
        if comp.get("group") is True or comp.get("status") != "operational":
            details.append({"name": comp.get("name", "Epic component"), "status": component_state(comp.get("status", "unknown"))})
    return {"kind": "services", "items": [service_item("epic", "Epic Games", overall, "https://status.epicgames.com/", page.get("description", "Official Epic Games status"), details, "Official Epic Status")]}


def provider_discord_status() -> dict[str, Any]:
    data = fetch_json(URLS["discord_status"])
    page = data.get("status", {})
    components = data.get("components", [])
    details = [{"name": x.get("name", "Discord component"), "status": component_state(x.get("status", "unknown"))} for x in components if not x.get("group_id")][:24]
    return {"kind": "services", "items": [service_item("discord", "Discord", statuspage_state(page.get("indicator", "unknown")), "https://discordstatus.com/", page.get("description", "Official Discord status"), details, "Official Discord Status")]}


def provider_twitch_status() -> dict[str, Any]:
    data = fetch_json(URLS["twitch_status"])
    page = data.get("status", {})
    details = [{"name": x.get("name", "Twitch component"), "status": component_state(x.get("status", "unknown"))} for x in data.get("components", [])][:24]
    return {"kind": "services", "items": [service_item("twitch", "Twitch", statuspage_state(page.get("indicator", "unknown")), "https://status.twitch.tv/", page.get("description", "Official Twitch status"), details, "Official Twitch Status")]}


def roblox_state(value: Any) -> str:
    text = str(value or "").lower()
    if "operational" in text:
        return "operational"
    if "maintenance" in text:
        return "maintenance"
    if "degraded" in text or "minor" in text:
        return "degraded"
    if "partial" in text:
        return "partial"
    if "major" in text or "disruption" in text or "outage" in text:
        return "major"
    return "unknown"


def provider_roblox_status() -> dict[str, Any]:
    data = fetch_json(URLS["roblox_status"])
    result = data.get("result", {})
    overall = result.get("status_overall", {})
    details = []
    for group in result.get("status", []):
        details.append({"name": group.get("name", "Roblox component"), "status": roblox_state(group.get("status"))})
        for item in group.get("containers", [])[:8]:
            if roblox_state(item.get("status")) != "operational":
                details.append({"name": f"{group.get('name', 'Roblox')} · {item.get('name', 'service')}", "status": roblox_state(item.get("status"))})
    return {"kind": "services", "items": [service_item("roblox", "Roblox", roblox_state(overall.get("status")), "https://status.roblox.com/", f"Official Roblox status · updated {overall.get('updated', 'recently')}", details, "Official Roblox Status")]} 


def provider_playstation_status() -> dict[str, Any]:
    data = fetch_json(URLS["playstation_status"])
    countries = data.get("countries", [])
    country = next((x for x in countries if x.get("countryCode") == "US"), countries[0] if countries else {})
    details = []
    any_issue = bool(data.get("status") or country.get("status"))
    for svc in country.get("services", []):
        issue = bool(svc.get("status"))
        any_issue = any_issue or issue
        details.append({"name": svc.get("serviceName", "PlayStation service"), "status": "partial" if issue else "operational"})
    status = "partial" if any_issue else "operational"
    return {"kind": "services", "items": [service_item("playstation", "PlayStation Network", status, "https://status.playstation.com/", "Official PlayStation Network service status for North America", details, "Official PlayStation Status")]}


def xbox_state(value: Any) -> str:
    name = str(value or "").lower()
    if name in {"none", "0", "1", "operational"}:
        return "operational"
    if "limited" in name or "warning" in name:
        return "degraded"
    if "major" in name or "outage" in name or "unavailable" in name:
        return "major"
    return "partial" if name else "unknown"


def provider_xbox_status() -> dict[str, Any]:
    data = fetch_json(URLS["xbox_status"])
    overall_raw = data.get("Status", {}).get("Overall", {}).get("State")
    details = []
    for svc in (data.get("CoreServices", []) + data.get("Titles", []))[:30]:
        raw = (svc.get("Status") or {}).get("Name")
        details.append({"name": svc.get("Name", "Xbox service"), "status": xbox_state(raw)})
    status = xbox_state(overall_raw)
    if details:
        worst = max((x["status"] for x in details), key=lambda x: STATUS_SEVERITY.get(x, 5))
        if STATUS_SEVERITY.get(worst, 0) > STATUS_SEVERITY.get(status, 0):
            status = worst
    return {"kind": "services", "items": [service_item("xbox", "Xbox Network", status, "https://support.xbox.com/xbox-live-status", "Official Xbox service status", details, "Official Xbox Status")]}


def provider_steam_health() -> dict[str, Any]:
    data = fetch_json(URLS["steam_health"], max_bytes=100_000)
    server_time = data.get("servertimestring", "Steam Web API responded")
    details = [{"name": "Steam Web API", "status": "operational"}, {"name": "Steam Store discovery", "status": "operational"}]
    return {"kind": "services", "items": [service_item("steam", "Steam", "operational", "https://store.steampowered.com/", str(server_time), details, "Official Steam endpoints")]}


def epic_slug(element: dict[str, Any]) -> str:
    mappings = element.get("offerMappings") or (element.get("catalogNs") or {}).get("mappings") or []
    for item in mappings:
        slug = item.get("pageSlug")
        if slug:
            return str(slug)
    return ""


def epic_image(element: dict[str, Any]) -> str:
    images = element.get("keyImages") or []
    for wanted in ("OfferImageWide", "DieselStoreFrontWide", "featuredMedia", "Thumbnail"):
        found = next((x.get("url") for x in images if x.get("type") == wanted and x.get("url")), None)
        if found:
            return str(found)
    return str(images[0].get("url", "")) if images else ""


def provider_epic_free(region: str = "IN") -> dict[str, Any]:
    region = region.upper() if re.fullmatch(r"[A-Za-z]{2}", region or "") else "IN"
    url = re.sub(r"country=[A-Z]{2}", f"country={region}", URLS["epic_free"])
    url = re.sub(r"allowCountries=[A-Z]{2}", f"allowCountries={region}", url)
    data = fetch_json(url)
    elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
    now = utc_now()
    items = []
    for element in elements:
        promotions = element.get("promotions") or {}
        buckets = [("free_now", promotions.get("promotionalOffers") or []), ("upcoming", promotions.get("upcomingPromotionalOffers") or [])]
        for status, groups in buckets:
            for group in groups:
                for offer in group.get("promotionalOffers", []):
                    discount = offer.get("discountSetting", {}).get("discountPercentage")
                    start, end = parse_iso(offer.get("startDate")), parse_iso(offer.get("endDate"))
                    if discount != 0 or not end or end <= now:
                        continue
                    if status == "free_now" and start and start > now:
                        continue
                    slug = epic_slug(element)
                    item_id = f"epic:{element.get('id') or element.get('productSlug') or slug}:{offer.get('endDate')}"
                    price = element.get("price", {}).get("totalPrice", {})
                    items.append({
                        "id": item_id, "title": element.get("title", "Epic free game"), "store": "Epic Games Store",
                        "status": status, "original_price": price.get("fmtPrice", {}).get("originalPrice", ""),
                        "end_at": offer.get("endDate"), "start_at": offer.get("startDate"),
                        "url": f"https://store.epicgames.com/en-US/p/{slug}" if slug else "https://store.epicgames.com/en-US/free-games",
                        "thumbnail": epic_image(element), "description": element.get("description", ""), "source": "Official Epic Store",
                    })
    unique = {x["id"]: x for x in items}
    return {"kind": "free_games", "items": list(unique.values())}


def provider_gamerpower() -> dict[str, Any]:
    data = fetch_json(URLS["gamerpower"])
    items = []
    for x in data if isinstance(data, list) else []:
        items.append({
            "id": f"gamerpower:{x.get('id')}", "title": x.get("title", "Gaming giveaway"),
            "type": x.get("type", "giveaway"), "platforms": x.get("platforms", ""), "worth": x.get("worth", ""),
            "end_at": x.get("end_date") or x.get("end_date_date"), "url": x.get("open_giveaway_url") or x.get("gamerpower_url") or "https://www.gamerpower.com/",
            "thumbnail": x.get("image") or x.get("thumbnail") or "", "description": x.get("description", ""),
            "instructions": x.get("instructions", ""), "source": "GamerPower",
        })
    return {"kind": "giveaways", "items": items[:100]}


def provider_cheapshark() -> dict[str, Any]:
    data = fetch_json(URLS["cheapshark"])
    items = []
    for x in data if isinstance(data, list) else []:
        discount = int(round(float(x.get("savings") or 0)))
        deal_id = str(x.get("dealID") or "")
        items.append({
            "id": f"cheapshark:{deal_id}", "title": x.get("title", "PC game deal"), "store": "PC Store",
            "sale_price": f"${x.get('salePrice')}", "normal_price": f"${x.get('normalPrice')}",
            "discount": discount, "rating": safe_int(x.get("dealRating")), "steam_rating": safe_int(x.get("steamRatingPercent")),
            "url": "https://www.cheapshark.com/redirect?dealID=" + urllib.parse.quote(deal_id),
            "thumbnail": x.get("thumb", ""), "source": "CheapShark", "steam_app_id": x.get("steamAppID"),
        })
    return {"kind": "deals", "items": items}


def steam_category(data: dict[str, Any], *needles: str) -> dict[str, Any]:
    for value in data.values():
        if not isinstance(value, dict):
            continue
        label = str(value.get("name", "")).lower()
        if all(needle in label for needle in needles):
            return value
    return {}


def format_minor_price(value: Any, currency: str) -> str:
    if value is None:
        return ""
    amount = safe_int(value) / 100
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(currency.upper(), currency.upper() + " ")
    return f"{symbol}{amount:,.2f}"


def steam_product(x: dict[str, Any], section: str) -> dict[str, Any]:
    app_id = str(x.get("id") or "")
    currency = str(x.get("currency") or "INR")
    return {
        "id": f"steam:{section}:{app_id}", "app_id": app_id, "title": x.get("name", "Steam game"),
        "section": section, "discount": safe_int(x.get("discount_percent")),
        "sale_price": format_minor_price(x.get("final_price"), currency),
        "normal_price": format_minor_price(x.get("original_price"), currency),
        "url": f"https://store.steampowered.com/app/{app_id}/", "thumbnail": x.get("large_capsule_image") or x.get("small_capsule_image") or "",
        "source": "Official Steam Store",
    }


def provider_steam_featured(region: str = "IN") -> dict[str, Any]:
    region = region.lower() if re.fullmatch(r"[A-Za-z]{2}", region or "") else "in"
    url = re.sub(r"cc=[a-z]{2}", f"cc={region}", URLS["steam_featured"])
    data = fetch_json(url)
    sections = {}
    candidates = {
        "specials": steam_category(data, "special"),
        "top_sellers": steam_category(data, "top", "seller"),
        "new_releases": steam_category(data, "new", "release"),
        "coming_soon": steam_category(data, "coming", "soon"),
    }
    for name, category in candidates.items():
        sections[name] = [steam_product(x, name) for x in category.get("items", [])[:30]]
    return {"kind": "steam", "sections": sections, "items": sections.get("specials", [])}


def provider_free_to_play() -> dict[str, Any]:
    data = fetch_json(URLS["free_to_play"], max_bytes=6_000_000)
    items = []
    for x in data if isinstance(data, list) else []:
        items.append({
            "id": f"f2p:{x.get('id')}", "title": x.get("title", "Free-to-play game"),
            "genre": x.get("genre", ""), "platform": x.get("platform", ""), "publisher": x.get("publisher", ""),
            "developer": x.get("developer", ""), "release_date": x.get("release_date"),
            "description": x.get("short_description", ""), "url": x.get("game_url") or x.get("freetogame_profile_url") or "https://www.freetogame.com/",
            "thumbnail": x.get("thumbnail", ""), "source": "FreeToGame",
        })
    return {"kind": "catalog", "items": items[:500]}


def provider_gog_deals(region: str = "IN") -> dict[str, Any]:
    region = region.upper() if re.fullmatch(r"[A-Za-z]{2}", region or "") else "IN"
    url = re.sub(r"countryCode=[A-Z]{2}", f"countryCode={region}", URLS["gog_deals"])
    data = fetch_json(url, max_bytes=5_000_000)
    items = []
    for x in data.get("products", []):
        price = x.get("price") or {}
        discount = safe_int(str(price.get("discount", "0")).replace("%", "").replace("-", ""))
        items.append({
            "id": f"gog:{x.get('id')}", "title": x.get("title", "GOG deal"), "store": "GOG",
            "sale_price": price.get("final", ""), "normal_price": price.get("base", ""), "discount": discount,
            "rating": safe_int(x.get("reviewsRating")), "url": x.get("storeLink") or f"https://www.gog.com/en/game/{x.get('slug', '')}",
            "thumbnail": x.get("coverHorizontal") or x.get("coverVertical") or "", "source": "Official GOG Catalog",
            "release_date": x.get("releaseDate"), "genres": [g.get("name") for g in x.get("genres", [])[:4]],
        })
    return {"kind": "deals", "items": items}


def provider_steam_news(apps: list[dict[str, str]]) -> dict[str, Any]:
    apps = [x for x in apps if re.fullmatch(r"\d{1,12}", str(x.get("id", "")))][:15]
    if not apps:
        return {"kind": "news", "items": []}

    def one(app: dict[str, str]) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"appid": app["id"], "count": 6, "maxlength": 700, "format": "json"})
        data = fetch_json(URLS["steam_news"] + "?" + query, timeout=18, max_bytes=1_000_000)
        out = []
        for x in data.get("appnews", {}).get("newsitems", []):
            out.append({
                "id": f"steamnews:{x.get('gid')}", "app_id": str(app["id"]), "game": app.get("name") or f"Steam {app['id']}",
                "title": x.get("title", "Game update"), "author": x.get("author", ""), "url": x.get("url", ""),
                "feed": x.get("feedlabel", "Steam News"), "date": dt.datetime.fromtimestamp(safe_int(x.get("date")), dt.timezone.utc).isoformat().replace("+00:00", "Z") if x.get("date") else None,
                "contents": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(x.get("contents", "")))).strip()[:700],
                "source": "Steam News",
            })
        return out

    news = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(apps))) as pool:
        futures = [pool.submit(one, app) for app in apps]
        for future in concurrent.futures.as_completed(futures):
            try:
                news.extend(future.result())
            except Exception:
                continue
    news.sort(key=lambda x: x.get("date") or "", reverse=True)
    return {"kind": "news", "items": news[:100]}


def safe_public_url(value: str) -> str:
    value = value.strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Monitor URL must be an http or https URL")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("Local/private monitor URLs are not allowed")
    try:
        for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM):
            address = ipaddress.ip_address(info[4][0])
            if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_multicast:
                raise ValueError("Local/private monitor URLs are not allowed")
    except socket.gaierror as exc:
        raise ValueError(f"Monitor host could not be resolved: {exc}") from exc
    return value


def provider_custom_health(monitors: list[dict[str, Any]]) -> dict[str, Any]:
    monitors = monitors[:20]
    items = []

    def check(monitor: dict[str, Any]) -> dict[str, Any]:
        url = safe_public_url(str(monitor.get("url", "")))
        started = time.monotonic()
        request = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                response.read(1024)
                code = response.status
            status = "operational" if 200 <= code < 400 else "major"
            description = f"HTTP {code} · {round((time.monotonic()-started)*1000)} ms"
        except Exception as exc:
            status, description = "major", f"{type(exc).__name__}: {exc}"
        return service_item(f"custom:{monitor.get('id')}", str(monitor.get("name") or "Custom endpoint"), status, url, description, [], "Custom health monitor")

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, max(1, len(monitors)))) as pool:
        futures = [pool.submit(check, x) for x in monitors]
        for future in concurrent.futures.as_completed(futures):
            items.append(future.result())
    return {"kind": "services", "items": items}


def game_tokens(game: str) -> list[str]:
    normalized = re.sub(r"[^a-z0-9 ]+", " ", game.lower())
    stop = {"the", "of", "and", "game", "mobile"}
    return [x for x in normalized.split() if len(x) > 2 and x not in stop]


def provider_live_game(game: str) -> dict[str, Any]:
    query = f"{game} live gameplay"
    last_error: Exception | None = None
    for base in PIPED_INSTANCES:
        try:
            url = base + "/search?" + urllib.parse.urlencode({"q": query, "filter": "all"})
            data = fetch_json(url, timeout=20)
            items = []
            tokens = game_tokens(game)
            for x in data.get("items", [])[:40]:
                if x.get("type") != "stream" or x.get("duration") != -1:
                    continue
                watch = str(x.get("url", ""))
                match = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", watch)
                if not match:
                    continue
                title = str(x.get("title") or "").strip()
                uploader = str(x.get("uploaderName") or "").strip()
                blob = f"{title} {uploader}".lower()
                if tokens and not any(token in blob for token in tokens):
                    continue
                if any(bad in blob for bad in ("24/7 radio", "free vbucks", "giveaway scam", "pre recorded")):
                    continue
                video_id = match.group(1)
                items.append({
                    "id": f"youtube:{video_id}", "video_id": video_id, "game": game, "title": title,
                    "channel": uploader, "viewers": max(0, safe_int(x.get("views"))),
                    "url": f"https://www.youtube.com/watch?v={video_id}", "thumbnail": x.get("thumbnail", "") or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    "source": "Piped / YouTube", "discovered_at": iso_now(),
                })
            items.sort(key=lambda x: (-x["viewers"], x["channel"].lower()))
            return {"kind": "live", "game": game, "items": items[:10]}
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"live discovery failed: {last_error}")


PROVIDER_FUNCTIONS: dict[str, Callable[[], dict[str, Any]]] = {
    "epic_status": provider_epic_status,
    "discord_status": provider_discord_status,
    "playstation_status": provider_playstation_status,
    "xbox_status": provider_xbox_status,
    "steam_health": provider_steam_health,
    "twitch_status": provider_twitch_status,
    "roblox_status": provider_roblox_status,
    "epic_free": provider_epic_free,
    "gamerpower": provider_gamerpower,
    "cheapshark": provider_cheapshark,
    "steam_featured": provider_steam_featured,
    "free_to_play": provider_free_to_play,
    "gog_deals": provider_gog_deals,
}


class NotificationManager:
    def __init__(self):
        self._winotify = None
        self.backend = "Windows only"
        if os.name == "nt":
            try:
                import winotify  # type: ignore
                self._winotify = winotify
                self.backend = "winotify"
            except Exception as exc:
                self.backend = f"PowerShell fallback ({type(exc).__name__})"

    def send(self, title: str, message: str, url: str = "") -> tuple[bool, str]:
        if os.name != "nt":
            return False, "Windows notification delivery is unavailable on this operating system"
        try:
            if self._winotify is not None:
                w = self._winotify
                toast = w.Notification(app_id=APP_NAME, title=title, msg=message, icon=str(ICON_PATH) if ICON_PATH.exists() else "", duration="long")
                toast.set_audio(w.audio.Default, loop=False)
                if url:
                    toast.add_actions(label="Open", launch=url)
                toast.show()
                return True, "Toast sent with default sound"
            return self._powershell(title, message, url)
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _powershell(title: str, message: str, url: str) -> tuple[bool, str]:
        action = ""
        if url:
            escaped_url = html_lib.escape(url, quote=True)
            action = f'<actions><action content="Open" activationType="protocol" arguments="{escaped_url}"/></actions>'
        xml = (
            '<toast duration="long"><visual><binding template="ToastGeneric">'
            f'<text>{html_lib.escape(title)}</text><text>{html_lib.escape(message)}</text>'
            f'</binding></visual>{action}<audio src="ms-winsoundevent:Notification.Default"/></toast>'
        )
        script_xml = xml.replace("'", "''")
        app = APP_NAME.replace("'", "''")
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]>$null;"
            "[Windows.Data.Xml.Dom.XmlDocument,Windows.Data.Xml.Dom.XmlDocument,ContentType=WindowsRuntime]>$null;"
            "$x=New-Object Windows.Data.Xml.Dom.XmlDocument;"
            f"$x.LoadXml('{script_xml}');$t=[Windows.UI.Notifications.ToastNotification]::new($x);"
            f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{app}').Show($t)"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], capture_output=True, text=True, timeout=15, creationflags=flags)
        return (True, "PowerShell toast sent with default sound") if result.returncode == 0 else (False, result.stderr.strip() or f"PowerShell exit {result.returncode}")


class RadarApp:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.notifier = NotificationManager()
        self.events = self._load_events()
        loaded = load_json(STATE_PATH, {})
        self.persist = {
            "schema": 1,
            "created_at": loaded.get("created_at", iso_now()) if isinstance(loaded, dict) else iso_now(),
            "settings": deep_merge(DEFAULT_SETTINGS, loaded.get("settings") if isinstance(loaded, dict) else {}),
            "baselined": loaded.get("baselined", {}) if isinstance(loaded, dict) else {},
            "seen": loaded.get("seen", {}) if isinstance(loaded, dict) else {},
            "cache": loaded.get("cache", {}) if isinstance(loaded, dict) else {},
            "service_states": loaded.get("service_states", {}) if isinstance(loaded, dict) else {},
            "live_index": safe_int(loaded.get("live_index", 0)) if isinstance(loaded, dict) else 0,
            "metrics": loaded.get("metrics", [])[-720:] if isinstance(loaded, dict) and isinstance(loaded.get("metrics", []), list) else [],
        }
        self.providers = {
            name: {"ok": None, "last_success": None, "last_error": None, "last_attempt": None, "latency": None, "items": 0, "next_due": 0.0}
            for name in PROVIDER_INTERVALS
        }
        self.runtime = {"started_at": iso_now(), "checking": False, "last_cycle": None, "last_error": None}
        self.add_event("app_started", "Gaming Radar started", "All keyless providers are being initialized.", "info")

    def _load_events(self) -> list[dict[str, Any]]:
        out = []
        try:
            for line in EVENTS_PATH.read_text(encoding="utf-8").splitlines()[-MAX_EVENTS:]:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        out.append(value)
                except json.JSONDecodeError:
                    pass
        except FileNotFoundError:
            pass
        return out

    def add_event(self, kind: str, title: str, message: str, level: str = "info", url: str = "") -> None:
        event = {"id": f"{time.time_ns():x}", "time": iso_now(), "kind": kind, "title": title, "message": message, "level": level, "url": url}
        with self.lock:
            self.events.append(event)
            self.events = self.events[-MAX_EVENTS:]
            try:
                with EVENTS_PATH.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                if EVENTS_PATH.stat().st_size > 2_500_000:
                    EVENTS_PATH.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.events), encoding="utf-8")
            except OSError:
                pass

    def save(self) -> None:
        with self.lock:
            self.persist["updated_at"] = iso_now()
            atomic_json(STATE_PATH, self.persist)

    def in_quiet_hours(self) -> bool:
        quiet = self.persist["settings"].get("quiet_hours", {})
        if not quiet.get("enabled"):
            return False
        try:
            now = dt.datetime.now().time()
            start = dt.time.fromisoformat(quiet.get("start", "23:00"))
            end = dt.time.fromisoformat(quiet.get("end", "07:00"))
            return start <= now < end if start < end else now >= start or now < end
        except ValueError:
            return False

    def notify(self, category: str, title: str, message: str, url: str = "") -> None:
        if not self.persist["settings"].get("notifications", {}).get(category, False):
            return
        if self.in_quiet_hours():
            self.add_event("quiet_notification", title, "Suppressed by quiet hours: " + message, "info", url)
            return
        ok, detail = self.notifier.send(title, message, url)
        self.add_event("notification", title, message, "success" if ok else "warning", url)
        if not ok:
            self.add_event("notification_error", "Notification delivery failed", detail, "warning")

    def execute_provider(self, name: str) -> dict[str, Any]:
        with self.lock:
            settings = json.loads(json.dumps(self.persist["settings"]))
        if name == "live_radar":
            watchlist = list(settings.get("watchlist", []))
            if not watchlist:
                return {"kind": "live", "game": "", "items": []}
            with self.lock:
                index = self.persist.get("live_index", 0) % len(watchlist)
                game = watchlist[index]
                self.persist["live_index"] = (index + 1) % len(watchlist)
            return provider_live_game(game)
        if name == "epic_free":
            return provider_epic_free(settings.get("region", "IN"))
        if name == "steam_featured":
            return provider_steam_featured(settings.get("region", "IN"))
        if name == "gog_deals":
            return provider_gog_deals(settings.get("region", "IN"))
        if name == "steam_news":
            return provider_steam_news(settings.get("steam_apps", []))
        if name == "custom_health":
            return provider_custom_health(settings.get("custom_monitors", []))
        return PROVIDER_FUNCTIONS[name]()

    def process_result(self, provider: str, result: dict[str, Any]) -> None:
        kind = result.get("kind")
        with self.lock:
            if kind == "live":
                game = result.get("game", "")
                live_cache = self.persist["cache"].setdefault("live", {})
                if game:
                    live_cache[game] = result.get("items", [])
            elif kind == "steam":
                self.persist["cache"]["steam"] = result.get("sections", {})
            else:
                self.persist["cache"][provider] = result.get("items", [])
        if kind == "services":
            self._process_services(result.get("items", []))
        elif kind in {"free_games", "giveaways", "deals", "live", "news"}:
            self._process_new_items(provider, kind, result.get("items", []))
        elif kind == "steam":
            self._process_new_items(provider, "deals", result.get("items", []))
        self.save()

    def _process_services(self, items: list[dict[str, Any]]) -> None:
        with self.lock:
            states = self.persist.setdefault("service_states", {})
            for item in items:
                sid, current = item["id"], item["status"]
                previous = states.get(sid)
                states[sid] = current
                if previous is None and current not in {"operational", "unknown"}:
                    self.notify("service_incidents", f"{item['name']} issue detected", item.get("description", current), item.get("url", ""))
                elif previous and previous != current:
                    if current == "operational":
                        self.notify("service_recoveries", f"{item['name']} recovered", "Official status is operational again.", item.get("url", ""))
                    elif current != "unknown":
                        self.notify("service_incidents", f"{item['name']} status changed", f"New status: {current}.", item.get("url", ""))
            self.persist["baselined"]["services"] = True

    def _matching_price_rules(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        title = str(item.get("title", "")).lower()
        discount = safe_int(item.get("discount"))
        price = price_number(item.get("sale_price"))
        matches = []
        for rule in self.persist["settings"].get("price_rules", []):
            if not rule.get("enabled", True):
                continue
            keyword = str(rule.get("keyword", "")).strip().lower()
            if keyword and keyword not in title:
                continue
            if discount < safe_int(rule.get("min_discount", 0)):
                continue
            max_price = rule.get("max_price")
            if max_price not in {None, ""} and (price is None or price > float(max_price)):
                continue
            matches.append(rule)
        return matches

    def _process_new_items(self, provider: str, kind: str, items: list[dict[str, Any]]) -> None:
        key = "live" if kind == "live" else provider
        with self.lock:
            seen_list = list(self.persist.setdefault("seen", {}).get(key, []))
            seen = set(seen_list)
            baseline = not self.persist["baselined"].get(key)
            new_items = []
            for item in items:
                item_id = str(item.get("id", ""))
                if not item_id:
                    continue
                if item_id not in seen:
                    seen.add(item_id)
                    seen_list.append(item_id)
                    if not baseline:
                        new_items.append(item)
            self.persist["seen"][key] = seen_list[-MAX_SEEN:]
            self.persist["baselined"][key] = True
        if baseline:
            self.add_event("baseline", f"{provider.replace('_', ' ').title()} baseline ready", f"Recorded {len(items)} current items silently.", "success")
            return
        for item in new_items[:10]:
            if kind == "free_games" and item.get("status") == "free_now":
                self.notify("free_games", "New free game", f"{item.get('title')} is free on {item.get('store')}.", item.get("url", ""))
            elif kind == "giveaways":
                self.notify("giveaways", "New gaming giveaway", str(item.get("title")), item.get("url", ""))
            elif kind == "deals":
                rules = self._matching_price_rules(item)
                if rules:
                    self.notify("price_watches", "Price watch matched", f"{item.get('title')} · {item.get('sale_price', '')} · {item.get('discount', 0)}% off", item.get("url", ""))
                elif safe_int(item.get("discount")) >= safe_int(self.persist["settings"].get("deal_min_discount", 75)):
                    self.notify("deals", f"{item.get('discount')}% gaming deal", str(item.get("title")), item.get("url", ""))
            elif kind == "live":
                self.notify("live_streams", f"{item.get('game')} stream discovered", str(item.get("title")), item.get("url", ""))
            elif kind == "news":
                self.notify("game_news", f"{item.get('game')} news", str(item.get("title")), item.get("url", ""))

    def run_cycle(self, force: bool = False) -> None:
        now_mono = time.monotonic()
        with self.lock:
            due = [name for name, status in self.providers.items() if force or status["next_due"] <= now_mono]
            self.runtime["checking"] = bool(due)
        if not due:
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(due)), thread_name_prefix="RadarProvider") as pool:
            futures = {pool.submit(self.execute_provider, name): (name, time.monotonic()) for name in due}
            for future in concurrent.futures.as_completed(futures):
                name, started = futures[future]
                latency = round(time.monotonic() - started, 2)
                status = self.providers[name]
                was_error = status.get("ok") is False
                status["last_attempt"] = iso_now()
                status["latency"] = latency
                status["next_due"] = time.monotonic() + PROVIDER_INTERVALS[name]
                try:
                    result = future.result()
                    self.process_result(name, result)
                    count = len(result.get("items", [])) if isinstance(result.get("items", []), list) else 0
                    status.update(ok=True, last_success=iso_now(), last_error=None, items=count)
                    if was_error:
                        self.add_event("provider_recovered", f"{name.replace('_', ' ').title()} recovered", "Provider is responding again.", "success")
                except Exception as exc:
                    first_error = status.get("ok") is not False
                    status.update(ok=False, last_error=f"{type(exc).__name__}: {exc}")
                    if first_error:
                        self.add_event("provider_error", f"{name.replace('_', ' ').title()} failed", status["last_error"], "error")
        with self.lock:
            self.runtime["checking"] = False
            self.runtime["last_cycle"] = iso_now()
            errors = [f"{k}: {v['last_error']}" for k, v in self.providers.items() if v["ok"] is False]
            self.runtime["last_error"] = "; ".join(errors) if errors else None
            metrics = self.persist.setdefault("metrics", [])
            last_metric = parse_iso(metrics[-1].get("time")) if metrics else None
            if not last_metric or (utc_now() - last_metric).total_seconds() >= 55:
                latencies = [x.get("latency") for x in self.providers.values() if isinstance(x.get("latency"), (int, float))]
                metrics.append({
                    "time": iso_now(),
                    "healthy": sum(1 for x in self.providers.values() if x.get("ok") is True),
                    "total": len(self.providers),
                    "incidents": sum(1 for x in self.persist.get("service_states", {}).values() if x not in {"operational", "unknown"}),
                    "avg_latency": round(sum(latencies) / len(latencies), 2) if latencies else None,
                })
                self.persist["metrics"] = metrics[-720:]
                self.save()

    def loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_cycle()
            except Exception as exc:
                self.add_event("internal_error", "Radar cycle failed", f"{type(exc).__name__}: {exc}", "error")
                with self.lock:
                    self.runtime["checking"] = False
            self.wake_event.wait(timeout=1)
            self.wake_event.clear()

    def test_notification(self) -> tuple[bool, str]:
        ok, detail = self.notifier.send("Universal Gaming Radar test", "Notifications and sound are ready. Links open only when clicked.", "http://localhost:8896/")
        self.add_event("test_notification", "Test notification", detail, "success" if ok else "warning")
        return ok, detail

    def update_settings(self, supplied: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = self.persist["settings"]
            if "deal_min_discount" in supplied:
                settings["deal_min_discount"] = max(0, min(100, safe_int(supplied["deal_min_discount"], 75)))
            if str(supplied.get("region", "")).upper() in {"IN", "US", "GB", "CA", "AU", "DE", "FR", "JP", "BR"}:
                settings["region"] = str(supplied["region"]).upper()
                settings["currency"] = {"IN": "INR", "US": "USD", "GB": "GBP", "CA": "CAD", "AU": "AUD", "DE": "EUR", "FR": "EUR", "JP": "JPY", "BR": "BRL"}[settings["region"]]
                for provider in ("epic_free", "steam_featured", "gog_deals"):
                    self.providers[provider]["next_due"] = 0
            if isinstance(supplied.get("ui"), dict):
                accent = str(supplied["ui"].get("accent", settings["ui"].get("accent", "cyan")))
                if accent in {"cyan", "violet", "green", "orange", "red"}:
                    settings["ui"]["accent"] = accent
                settings["ui"]["compact"] = bool(supplied["ui"].get("compact", settings["ui"].get("compact", False)))
            if isinstance(supplied.get("notifications"), dict):
                for key in settings["notifications"]:
                    if key in supplied["notifications"]:
                        settings["notifications"][key] = bool(supplied["notifications"][key])
            if isinstance(supplied.get("quiet_hours"), dict):
                q = supplied["quiet_hours"]
                settings["quiet_hours"]["enabled"] = bool(q.get("enabled", settings["quiet_hours"]["enabled"]))
                for key in ("start", "end"):
                    if re.fullmatch(r"\d{2}:\d{2}", str(q.get(key, ""))):
                        settings["quiet_hours"][key] = str(q[key])
        self.save()
        self.add_event("settings", "Settings updated", "Notification preferences were saved.", "success")
        return self.persist["settings"]

    def update_watchlist(self, action: str, game: str) -> list[str]:
        game = re.sub(r"\s+", " ", game).strip()[:80]
        if not game:
            raise ValueError("Enter a game name")
        with self.lock:
            watchlist = self.persist["settings"]["watchlist"]
            if action == "add" and game.lower() not in {x.lower() for x in watchlist}:
                if len(watchlist) >= 30:
                    raise ValueError("Watchlist limit is 30 games")
                watchlist.append(game)
                self.add_event("watchlist", "Game added", game, "success")
            elif action == "remove":
                watchlist[:] = [x for x in watchlist if x.lower() != game.lower()]
                self.persist["cache"].setdefault("live", {}).pop(game, None)
                self.add_event("watchlist", "Game removed", game, "info")
        self.save()
        self.wake_event.set()
        return list(self.persist["settings"]["watchlist"])

    def toggle_favorite(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        item_id = str(item.get("id", ""))[:240]
        if not item_id:
            raise ValueError("Favorite item ID is required")
        safe = {key: item.get(key) for key in ("id", "title", "url", "thumbnail", "store", "source", "sale_price", "normal_price", "discount", "status", "type", "game", "channel", "video_id", "genre", "platform", "publisher", "developer", "feed", "date", "contents", "app_id")}
        safe["saved_at"] = iso_now()
        with self.lock:
            favorites = self.persist["settings"].setdefault("favorites", [])
            if any(str(x.get("id")) == item_id for x in favorites):
                favorites[:] = [x for x in favorites if str(x.get("id")) != item_id]
                self.add_event("favorite", "Removed from favorites", str(item.get("title") or item_id), "info")
            else:
                favorites.insert(0, safe)
                del favorites[200:]
                self.add_event("favorite", "Saved to favorites", str(item.get("title") or item_id), "success")
        self.save()
        return list(self.persist["settings"]["favorites"])

    def update_price_rule(self, action: str, supplied: dict[str, Any]) -> list[dict[str, Any]]:
        with self.lock:
            rules = self.persist["settings"].setdefault("price_rules", [])
            if action == "remove":
                rid = str(supplied.get("id", ""))
                rules[:] = [x for x in rules if str(x.get("id")) != rid]
            elif action == "toggle":
                rid = str(supplied.get("id", ""))
                for rule in rules:
                    if str(rule.get("id")) == rid:
                        rule["enabled"] = not rule.get("enabled", True)
            else:
                keyword = re.sub(r"\s+", " ", str(supplied.get("keyword", ""))).strip()[:80]
                if not keyword:
                    raise ValueError("Enter a game/title keyword")
                if len(rules) >= 50:
                    raise ValueError("Price-rule limit is 50")
                max_price = supplied.get("max_price")
                try:
                    max_price = None if max_price in {None, ""} else max(0.0, float(max_price))
                except (TypeError, ValueError):
                    raise ValueError("Maximum price must be a number")
                rules.append({"id": f"rule-{time.time_ns():x}", "keyword": keyword, "min_discount": max(0, min(100, safe_int(supplied.get("min_discount"), 0))), "max_price": max_price, "enabled": True, "created_at": iso_now()})
        self.save()
        return list(self.persist["settings"]["price_rules"])

    def update_steam_app(self, action: str, supplied: dict[str, Any]) -> list[dict[str, str]]:
        app_id = str(supplied.get("id", "")).strip()
        name = re.sub(r"\s+", " ", str(supplied.get("name", ""))).strip()[:100]
        if not re.fullmatch(r"\d{1,12}", app_id):
            raise ValueError("Steam App ID must contain only digits")
        with self.lock:
            apps = self.persist["settings"].setdefault("steam_apps", [])
            if action == "remove":
                apps[:] = [x for x in apps if str(x.get("id")) != app_id]
            elif app_id not in {str(x.get("id")) for x in apps}:
                if len(apps) >= 15:
                    raise ValueError("Tracked Steam-app limit is 15")
                apps.append({"id": app_id, "name": name or f"Steam App {app_id}"})
            self.providers["steam_news"]["next_due"] = 0
        self.save()
        self.wake_event.set()
        return list(self.persist["settings"]["steam_apps"])

    def update_custom_monitor(self, action: str, supplied: dict[str, Any]) -> list[dict[str, Any]]:
        monitor_id = str(supplied.get("id", ""))
        if action != "remove":
            name = re.sub(r"\s+", " ", str(supplied.get("name", ""))).strip()[:80]
            if not name:
                raise ValueError("Monitor name is required")
            url = safe_public_url(str(supplied.get("url", "")))
        with self.lock:
            monitors = self.persist["settings"].setdefault("custom_monitors", [])
            if action == "remove":
                monitors[:] = [x for x in monitors if str(x.get("id")) != monitor_id]
                self.persist.get("service_states", {}).pop(f"custom:{monitor_id}", None)
                self.persist.get("cache", {}).setdefault("custom_health", [])[:] = [x for x in self.persist.get("cache", {}).get("custom_health", []) if x.get("id") != f"custom:{monitor_id}"]
            else:
                if len(monitors) >= 20:
                    raise ValueError("Custom-monitor limit is 20")
                monitors.append({"id": f"monitor-{time.time_ns():x}", "name": name, "url": url, "created_at": iso_now()})
            self.providers["custom_health"]["next_due"] = 0
        self.save()
        self.wake_event.set()
        return list(self.persist["settings"]["custom_monitors"])

    def mark_events_read(self) -> None:
        with self.lock:
            self.persist["settings"]["last_read_at"] = iso_now()
        self.save()

    def import_user_data(self, supplied: dict[str, Any]) -> dict[str, Any]:
        source = supplied.get("settings") if isinstance(supplied.get("settings"), dict) else supplied
        if not isinstance(source, dict):
            raise ValueError("Import must contain a settings object")
        allowed = {"region", "deal_min_discount", "notifications", "quiet_hours", "watchlist", "steam_apps", "custom_monitors", "price_rules", "favorites", "ui"}
        clean = {k: v for k, v in source.items() if k in allowed}
        with self.lock:
            merged = deep_merge(DEFAULT_SETTINGS, clean)
            merged["watchlist"] = [str(x)[:80] for x in merged.get("watchlist", []) if str(x).strip()][:30]
            merged["steam_apps"] = [x for x in merged.get("steam_apps", []) if isinstance(x, dict) and re.fullmatch(r"\d{1,12}", str(x.get("id", "")))][:15]
            merged["custom_monitors"] = [x for x in merged.get("custom_monitors", []) if isinstance(x, dict)][:20]
            for monitor in merged["custom_monitors"]:
                safe_public_url(str(monitor.get("url", "")))
            merged["price_rules"] = [x for x in merged.get("price_rules", []) if isinstance(x, dict)][:50]
            merged["favorites"] = [x for x in merged.get("favorites", []) if isinstance(x, dict)][:200]
            self.persist["settings"] = merged
            if isinstance(supplied.get("seen"), dict):
                self.persist["seen"] = {str(k): [str(v) for v in values][-MAX_SEEN:] for k, values in supplied["seen"].items() if isinstance(values, list)}
            if isinstance(supplied.get("baselined"), dict):
                self.persist["baselined"] = {str(k): bool(v) for k, v in supplied["baselined"].items()}
            for provider in self.providers.values():
                provider["next_due"] = 0
        self.save()
        self.wake_event.set()
        self.add_event("import", "User data imported", "Settings, watchlists and saved items were restored.", "success")
        return self.persist["settings"]

    def export_data(self) -> dict[str, Any]:
        with self.lock:
            return {
                "app": APP_NAME, "version": APP_VERSION, "exported_at": iso_now(),
                "settings": json.loads(json.dumps(self.persist["settings"])),
                "seen": json.loads(json.dumps(self.persist.get("seen", {}))),
                "baselined": json.loads(json.dumps(self.persist.get("baselined", {}))),
                "events": list(self.events[-MAX_EVENTS:]),
            }

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            cache = json.loads(json.dumps(self.persist.get("cache", {})))
            services = []
            for name in ("epic_status", "discord_status", "playstation_status", "xbox_status", "steam_health", "twitch_status", "roblox_status", "custom_health"):
                services.extend(cache.get(name, []))
            free_games = cache.get("epic_free", [])
            giveaways = cache.get("gamerpower", [])
            cheapshark_deals = cache.get("cheapshark", [])
            gog_deals = cache.get("gog_deals", [])
            deals = cheapshark_deals + gog_deals
            steam = cache.get("steam", {})
            news = cache.get("steam_news", [])
            free_to_play = cache.get("free_to_play", [])
            live_by_game = cache.get("live", {})
            live = []
            for game in self.persist["settings"].get("watchlist", []):
                live.extend(live_by_game.get(game, []))
            healthy = sum(1 for x in services if x.get("status") == "operational")
            incidents = sum(1 for x in services if x.get("status") not in {"operational", "unknown"})
            provider_view = {}
            now_mono = time.monotonic()
            for name, value in self.providers.items():
                item = {k: v for k, v in value.items() if k != "next_due"}
                item["next_in"] = max(0, round(value["next_due"] - now_mono))
                item["interval"] = PROVIDER_INTERVALS[name]
                provider_view[name] = item
            last_read = parse_iso(self.persist["settings"].get("last_read_at"))
            unread = sum(1 for event in self.events if not last_read or (parse_iso(event.get("time")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)) > last_read)
            return {
                "app": {"name": APP_NAME, "version": APP_VERSION, "port": PORT, "started_at": self.runtime["started_at"]},
                "runtime": json.loads(json.dumps(self.runtime)),
                "providers": provider_view,
                "settings": json.loads(json.dumps(self.persist["settings"])),
                "services": services,
                "free_games": free_games,
                "giveaways": giveaways,
                "deals": deals,
                "cheapshark_deals": cheapshark_deals,
                "gog_deals": gog_deals,
                "steam": steam,
                "news": news,
                "free_to_play": free_to_play,
                "favorites": json.loads(json.dumps(self.persist["settings"].get("favorites", []))),
                "metrics": json.loads(json.dumps(self.persist.get("metrics", []))),
                "live_streams": live,
                "live_by_game": live_by_game,
                "events": list(reversed(self.events[-300:])),
                "unread_events": unread,
                "stats": {
                    "services_healthy": healthy, "service_incidents": incidents, "services_total": len(services),
                    "free_now": sum(1 for x in free_games if x.get("status") == "free_now"),
                    "giveaways": len(giveaways), "deals": len(deals), "live": len(live),
                    "news": len(news), "free_to_play": len(free_to_play),
                    "favorites": len(self.persist["settings"].get("favorites", [])), "price_rules": len(self.persist["settings"].get("price_rules", [])),
                    "providers_healthy": sum(1 for x in self.providers.values() if x["ok"] is True),
                    "providers_total": len(self.providers),
                },
                "notification_backend": self.notifier.backend,
                "generated_at": iso_now(),
            }


def ics_escape(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def build_calendar(state: dict[str, Any]) -> bytes:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Universal Gaming Radar//EN", "CALSCALE:GREGORIAN"]
    for item in state.get("free_games", []):
        moment = parse_iso(item.get("start_at") if item.get("status") == "upcoming" else item.get("end_at"))
        if not moment:
            continue
        stamp = moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.extend([
            "BEGIN:VEVENT", f"UID:{ics_escape(item.get('id'))}@gaming-radar", f"DTSTAMP:{utc_now().strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART:{stamp}", f"SUMMARY:{ics_escape(('Free starts: ' if item.get('status') == 'upcoming' else 'Free ends: ') + str(item.get('title', 'Game')))}",
            f"DESCRIPTION:{ics_escape(item.get('store'))}", f"URL:{item.get('url', '')}", "END:VEVENT",
        ])
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def build_deals_csv(state: dict[str, Any]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Title", "Store", "Sale price", "Normal price", "Discount percent", "URL", "Source"])
    for item in state.get("deals", []):
        writer.writerow([item.get("title"), item.get("store"), item.get("sale_price"), item.get("normal_price"), item.get("discount"), item.get("url"), item.get("source")])
    return out.getvalue().encode("utf-8-sig")


class RadarHandler(BaseHTTPRequestHandler):
    server_version = "UniversalGamingRadar/2.0"

    @property
    def app(self) -> RadarApp:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, cache: str = "no-store", filename: str = "") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("X-Gaming-Radar-Version", APP_VERSION)
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, obj: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def read_json(self) -> dict[str, Any]:
        length = min(safe_int(self.headers.get("Content-Length"), 0), 2_000_000)
        if not length:
            return {}
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path in {"/", "/dashboard.html"}:
            self.send_bytes(DASHBOARD_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self.send_json(self.app.public_state())
        elif path == "/api/healthz":
            self.send_json({"ok": True, "app": APP_NAME, "version": APP_VERSION, "time": iso_now()})
        elif path == "/api/export":
            self.send_bytes(json.dumps(self.app.export_data(), ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", filename="gaming-radar-backup.json")
        elif path == "/api/calendar.ics":
            self.send_bytes(build_calendar(self.app.public_state()), "text/calendar; charset=utf-8", filename="gaming-radar-calendar.ics")
        elif path == "/api/deals.csv":
            self.send_bytes(build_deals_csv(self.app.public_state()), "text/csv; charset=utf-8", filename="gaming-radar-deals.csv")
        elif path == "/manifest.webmanifest":
            self.send_bytes((ROOT / "manifest.webmanifest").read_bytes(), "application/manifest+json; charset=utf-8", cache="no-cache")
        elif path == "/service-worker.js":
            self.send_bytes((ROOT / "service-worker.js").read_bytes(), "application/javascript; charset=utf-8", cache="no-cache")
        elif path.startswith("/assets/"):
            name = pathlib.Path(urllib.parse.unquote(path)).name
            file = ASSETS_DIR / name
            if file.is_file() and file.parent == ASSETS_DIR:
                self.send_bytes(file.read_bytes(), mimetypes.guess_type(file.name)[0] or "application/octet-stream", cache="public, max-age=86400")
            else:
                self.send_json({"error": "Not found"}, 404)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        body = self.read_json()
        try:
            if path == "/api/check-now":
                with self.app.lock:
                    for status in self.app.providers.values():
                        status["next_due"] = 0
                self.app.wake_event.set()
                self.send_json({"ok": True, "message": "Full refresh requested"})
            elif path == "/api/test-notification":
                ok, detail = self.app.test_notification()
                self.send_json({"ok": ok, "detail": detail}, 200 if ok else 503)
            elif path == "/api/settings":
                self.send_json({"ok": True, "settings": self.app.update_settings(body)})
            elif path == "/api/watchlist":
                self.send_json({"ok": True, "watchlist": self.app.update_watchlist(str(body.get("action", "add")), str(body.get("game", "")))})
            elif path == "/api/favorite":
                item = body.get("item") if isinstance(body.get("item"), dict) else body
                self.send_json({"ok": True, "favorites": self.app.toggle_favorite(item)})
            elif path == "/api/price-rule":
                self.send_json({"ok": True, "price_rules": self.app.update_price_rule(str(body.get("action", "add")), body)})
            elif path == "/api/steam-app":
                self.send_json({"ok": True, "steam_apps": self.app.update_steam_app(str(body.get("action", "add")), body)})
            elif path == "/api/custom-monitor":
                self.send_json({"ok": True, "custom_monitors": self.app.update_custom_monitor(str(body.get("action", "add")), body)})
            elif path == "/api/mark-read":
                self.app.mark_events_read()
                self.send_json({"ok": True, "message": "Events marked read"})
            elif path == "/api/import":
                self.send_json({"ok": True, "settings": self.app.import_user_data(body)})
            elif path == "/api/shutdown":
                self.send_json({"ok": True, "message": "Stopping Gaming Radar"})
                self.app.stop_event.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_json({"error": "Not found"}, 404)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


class RadarServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app: RadarApp):
        self.app = app
        super().__init__(address, handler)


def existing_instance() -> bool:
    try:
        with urllib.request.urlopen(f"http://{HOST}:{PORT}/api/healthz", timeout=2) as response:
            return json.loads(response.read()).get("app") == APP_NAME
    except Exception:
        return False


def main() -> int:
    if existing_instance():
        return 0
    app = RadarApp()
    try:
        server = RadarServer((HOST, PORT), RadarHandler, app)
    except OSError as exc:
        app.add_event("server_error", "Local server could not start", str(exc), "error")
        return 2
    worker = threading.Thread(target=app.loop, daemon=True, name="GamingRadarScheduler")
    worker.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_event.set()
        server.server_close()
        worker.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
